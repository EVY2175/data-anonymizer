"""
Персистентный OS-backed кросс-процессный лок workspace (Stage 10B.2).

======================================================================
Что это и что это НЕ
======================================================================

WorkspaceFileLock — низкоуровневый примитив эксклюзивной блокировки
одного файла `workspace.lock`. Он не реализует create_workspace,
open_workspace, store binding/recovery, artifact registry или что-либо
из будущего жизненного цикла Workspace (Stage 10B.3+) — только сам факт
"кто-то держит этот workspace прямо сейчас".

`workspace.lock` — персистентный файл раскладки: он остаётся на диске
после release(). Авторитетно только фактическое удержание OS-лока
(байтовый диапазон через `msvcrt.locking`); содержимое файла не несёт
никакого владельческого смысла и НЕ читается/не интерпретируется этим
модулем — по контракту (frozen owner decision OD-5) файл всегда 0 байт,
без PID/timestamp/hostname и прочих диагностических меток, которые
могли бы устареть после краха процесса и создать иллюзию авторитетности
там, где её нет.

======================================================================
Технология: msvcrt (стандартная библиотека), без новых зависимостей
======================================================================

Проект целиком таргетирован на Windows (frozen owner decision OD-7).
`msvcrt.locking` экспериментально проверен (Stage 10B.2 Contract
Review) в отдельных процессах: блокирует конкурента как из другого
процесса, так и из другого файлового дескриптора ТОГО ЖЕ процесса; при
завершении процесса (в т.ч. аварийном) ОС снимает лок автоматически;
блокировка байтового диапазона работает даже поверх 0-байтного файла
(байт не обязан физически существовать). `portalocker` или иная
сторонняя зависимость не добавляются.

======================================================================
Почему одного per-object флага недостаточно
======================================================================

`msvcrt.locking` даёт ОДИНАКОВЫЙ неразличимый OSError и для "лок держит
другой процесс", и для "лок уже держит другой файловый дескриптор ТОГО
ЖЕ процесса" (проверено экспериментально). Поэтому для детерминированного
NESTED_ACQUISITION у ВТОРОГО объекта на тот же путь в том же процессе
нужен process-global registry, keyed по каноническому пути и защищённый
`threading.Lock`. Проверка по registry происходит ДО любой попытки
открыть файл или взять OS-лок — nested acquisition обнаруживается, не
дожидаясь ответа ОС.

======================================================================
Блокирующий/неблокирующий режим
======================================================================

Захват — всегда эксклюзивный и неблокирующий (frozen owner decision
OD-4): ни таймаута, ни retry-цикла, ни ожидания нет. Занятость сразу
даёт WorkspaceLockedError(HELD_BY_OTHER).

======================================================================
create=False / create=True
======================================================================

`acquire(create=False)` (умолчание) НЕ создаёт отсутствующий
`workspace.lock` — это соответствует будущему правилу верхнего уровня
"missing workspace.lock => WorkspaceCorruptedError(LOCK_FILE_MISSING),
open_workspace не создаёт ничего". `acquire(create=True)` — низкоуровневая
поддержка для будущего create_workspace: создаёт файл эксклюзивно, если
его нет, но НИКОГДА не усекает/не переписывает уже существующий файл.

======================================================================
Границы (non-goals)
======================================================================

Этот модуль не проверяет layout workspace, не различает junction/
symlink/reparse на `workspace_root`/`workspace.lock` (это задача
будущей Workspace-верификации, Stage 10B.3+) и не решает ситуацию,
когда `workspace.lock` заменён на диске по тому же пути другим файлом,
пока текущий держатель ещё не освободил лок на СТАРОМ открытом файловом
объекте — это принятое ограничение файловых locks вообще, а не дефект
этого слайса.
"""

from __future__ import annotations

import msvcrt
import os
import threading
from pathlib import Path
from typing import Dict, Optional, Union

from app.workspace.errors import (
    WorkspaceCorruptedError,
    WorkspaceCorruptedReason,
    WorkspaceLockedError,
    WorkspaceLockedReason,
)

# Длина блокируемого байтового диапазона. Один байт достаточен для
# эксклюзивности; сам диапазон не обязан физически существовать в файле
# (проверено экспериментально на 0-байтном файле).
_LOCK_RANGE_LENGTH = 1

# Process-global registry: канонический путь -> держащий его объект.
# Единственный способ детерминированно отличить NESTED_ACQUISITION
# (тот же процесс, другой объект) от HELD_BY_OTHER (другой процесс) —
# см. docstring модуля. Потокобезопасность — через _registry_guard.
_registry_guard = threading.Lock()
_registry: Dict[str, "WorkspaceFileLock"] = {}


def _canonical_key(path: Path) -> str:
    """
    Канонический ключ для registry: resolve() нормализует путь (в т.ч.
    относительные сегменты и, где возможно, symlink-и в уже существующих
    родительских каталогах) без требования существования самого файла
    (strict=False по умолчанию — нужно для create=True на ещё не
    созданный workspace.lock); normcase учитывает нечувствительность
    файловой системы Windows к регистру.
    """
    return os.path.normcase(str(path.resolve()))


def _release_reservation(owner: "WorkspaceFileLock") -> None:
    """
    Снимает временную/полную резервацию `owner` в registry — но только
    если текущая запись под этим ключом действительно принадлежит
    именно `owner` (MINOR-2, Independent Review: не затирать чужую
    резервацию, если владение по каким-то причинам уже сменилось).
    """
    with _registry_guard:
        if _registry.get(owner._canonical) is owner:
            _registry.pop(owner._canonical, None)


class WorkspaceFileLock:
    """
    Эксклюзивный неблокирующий OS-backed кросс-процессный лок одного
    персистентного файла `workspace.lock`. Содержимое файла не несёт
    авторитетного смысла — авторитетен только факт удержания OS-лока.
    """

    def __init__(self, path: Union[str, Path]) -> None:
        if not isinstance(path, (str, Path)):
            raise TypeError(f"path должен быть str или Path, получено: {type(path)!r}")

        self._path = Path(path)
        self._canonical = _canonical_key(self._path)
        self._file = None
        self._held = False

    @property
    def path(self) -> Path:
        return self._path

    def acquire(self, *, create: bool = False) -> None:
        """
        Захватывает эксклюзивный неблокирующий лок. Таймаута и retry нет.

        :raises WorkspaceLockedError: NESTED_ACQUISITION (этот же объект
            уже держит лок, либо другой объект того же процесса уже
            держит лок на тот же канонический путь) или HELD_BY_OTHER
            (лок занят другим процессом).
        :raises WorkspaceCorruptedError: LOCK_FILE_MISSING, если
            create=False и workspace.lock не существует. Ничего на диске
            при этом не создаётся и не изменяется.
        """
        if self._held:
            # Тот же объект: обнаруживается дёшево, без обращения к
            # registry и без попытки открыть файл.
            raise WorkspaceLockedError(WorkspaceLockedReason.NESTED_ACQUISITION)

        with _registry_guard:
            if self._canonical in _registry:
                # Другой объект этого же процесса уже держит тот же путь.
                # Обнаружено ДО любой попытки открыть файл или взять
                # OS-лок — nested acquisition не может задеадлочиться.
                raise WorkspaceLockedError(WorkspaceLockedReason.NESTED_ACQUISITION)
            # Временная резервация: предотвращает гонку с другим потоком
            # этого же процесса, пытающимся захватить тот же путь, пока
            # мы ещё открываем файл/берём OS-лок ниже.
            _registry[self._canonical] = self

        try:
            handle = self._open(create)
        except Exception:
            _release_reservation(self)
            raise

        try:
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, _LOCK_RANGE_LENGTH)
        except OSError:
            try:
                handle.close()
            except OSError:
                # Best-effort: ошибка закрытия не должна маскировать
                # исходную причину отказа в захвате лока.
                pass
            _release_reservation(self)
            raise WorkspaceLockedError(WorkspaceLockedReason.HELD_BY_OTHER) from None

        self._file = handle
        self._held = True

    def release(self) -> None:
        """
        Снимает OS-лок и закрывает файловый handle. `workspace.lock` НЕ
        удаляется — файл персистентен по контракту.

        :raises WorkspaceLockedError: NOT_HELD, если объект не держит
            лок в данный момент.
        :raises OSError: сбой самого unlock и/или close на уровне ОС
            (крайне маловероятно при нормальной работе Windows). Если
            сбоят ОБА шага, наружу проходит ошибка unlock (первична);
            ошибка close вторична и не маскирует её. В ЛЮБОМ случае —
            даже при таком двойном сбое — внутреннее состояние объекта
            и резервация в process-global registry снимаются полностью,
            чтобы канонический путь не оставался заблокированным для
            новых попыток захвата до конца жизни процесса (MINOR-2,
            Independent Review).
        """
        if not self._held:
            raise WorkspaceLockedError(WorkspaceLockedReason.NOT_HELD)

        # Приоритет ошибок (frozen для этого исправления): ошибка unlock
        # первична, если unlock не удался; ошибка close первична, только
        # если unlock прошёл успешно, а close — нет. Зачистка состояния
        # ниже выполняется БЕЗУСЛОВНО, независимо от результата обоих
        # шагов — именно это и не гарантировал прежний `finally`-блок,
        # прерывавшийся, если close() поднимал исключение вслед за
        # unlock().
        primary_error: Optional[OSError] = None
        try:
            self._file.seek(0)
            msvcrt.locking(self._file.fileno(), msvcrt.LK_UNLCK, _LOCK_RANGE_LENGTH)
        except OSError as exc:
            primary_error = exc

        try:
            self._file.close()
        except OSError as exc:
            if primary_error is None:
                primary_error = exc
            # Иначе unlock уже дал первичную ошибку — вторичный сбой
            # close() не должен её маскировать (best-effort, отбрасываем).

        self._file = None
        self._held = False
        _release_reservation(self)

        if primary_error is not None:
            raise primary_error

    def _open(self, create: bool):
        """
        Открывает `workspace.lock` в бинарном read/write режиме, не
        затрагивая его содержимое.

        create=False: файл должен уже существовать; отсутствие
            транслируется в WorkspaceCorruptedError(LOCK_FILE_MISSING)
            без какой-либо попытки его создать.
        create=True: создаёт файл эксклюзивно (O_CREAT | O_EXCL), если
            его ещё нет; если он уже существует — открывает как есть,
            без усечения/перезаписи содержимого (низкоуровневая
            поддержка будущего create_workspace).
        """
        if create:
            try:
                fd = os.open(str(self._path), os.O_RDWR | os.O_CREAT | os.O_EXCL)
            except FileExistsError:
                fd = os.open(str(self._path), os.O_RDWR)
        else:
            try:
                fd = os.open(str(self._path), os.O_RDWR)
            except FileNotFoundError:
                raise WorkspaceCorruptedError(WorkspaceCorruptedReason.LOCK_FILE_MISSING) from None
        return os.fdopen(fd, "r+b")

    def __enter__(self) -> "WorkspaceFileLock":
        self.acquire()
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        # release() снимает лок независимо от того, поднялось ли
        # исключение в теле with — и не поглощает его (не возвращает
        # True), так что исходное исключение тела распространяется как
        # есть.
        self.release()
