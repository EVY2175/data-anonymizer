"""
Зашифрованное хранилище WorkspaceManifest (Stage 10B.2):
`workspace.enc` — аутентифицированные load/save + атомарная замена.

======================================================================
Границы слоя
======================================================================

Этот модуль — тонкая инфраструктурная обёртка. Он НЕ реализует
create_workspace/open_workspace/adopt_stores, привязку/восстановление
сторов, реестр артефактов, backup/restore или что-либо из будущего
жизненного цикла Workspace (Stage 10B.3+). Единственная забота —
безопасно перевести `WorkspaceManifest` в зашифрованные байты на диске
и обратно.

======================================================================
Переиспользование, без нового крипто-формата
======================================================================

Криптография не дублируется и не форкается: используются исключительно
`encrypt_bytes`/`decrypt_bytes` из `app.security.crypto` как есть (AES-
256-GCM, Scrypt, versioned binary container — см. docstring того
модуля). `app/security/crypto.py` этим модулем не изменяется.
Сериализация/разбор manifest — исключительно `serialize_manifest`/
`parse_manifest` из Stage 10B.1 (`app.workspace.manifest`), без
альтернативного JSON-сериализатора. Расшифрованный plaintext всегда
РОВНО `serialize_manifest(manifest)` — ни одного лишнего байта.

======================================================================
Привязка типа payload — НЕ новый AAD-домен
======================================================================

Валидный зашифрованный контейнер другого типа приложения (mapping/
identifier/provenance) под тем же паролем МОЖЕТ успешно расшифроваться
(frozen owner decision OD-2) — крипто-контейнер `app.security.crypto`
не знает и не обязан знать о типе содержимого. Привязка типа
обеспечивается на уровне УЖЕ АУТЕНТИФИЦИРОВАННОГО (GCM) payload:
`serialize_manifest`/`parse_manifest` (Stage 10B.1) кладут и строго
проверяют поле `"kind":"workspace_manifest"` и полную схему manifest.
Это НЕ новый домен AAD-разделения криптоконтейнера — это уже
существующая аутентифицированная привязка типа на уровне payload.

======================================================================
На диске — только ciphertext, никогда plaintext
======================================================================

    WorkspaceManifest
        -> serialize_manifest(...)      (в RAM)
        -> encrypt_bytes(...)           (в RAM, app.security.crypto)
        -> temp-файл В ТОЙ ЖЕ директории, что и target
        -> flush -> os.fsync -> atomic os.replace(temp, target)

Ни plaintext manifest, ни промежуточный JSON никогда не сериализуются
на диск — ни как основной файл, ни как temp, ни как sidecar. Алгоритм
атомарной записи буквально повторяет уже проверенный паттерн
`app.mapping.encrypted_file`/`app.mapping.provenance_encrypted`
(temp рядом с целью, `tempfile.mkstemp`, fsync перед `os.replace`,
best-effort cleanup temp при любой ошибке до успешного replace, без
маскировки исходного исключения).

======================================================================
Атомарность != durability
======================================================================

При нормальной семантике атомарной замены файла читатель `workspace.enc`
видит либо полностью предыдущее содержимое, либо полностью новое —
никогда намеренно частично записанный target. Это НЕ обещает
переживания произвольной потери питания, аппаратного отказа, порчи
файловой системы или действий злонамеренного локального администратора.
Directory fsync на Windows НЕ выполняется (frozen owner decision OD-6):
штатный Python API для fsync каталога на NTFS недоступен
(экспериментально подтверждено — PermissionError), а вводить
`ctypes`/`_winapi` ради этого предельного случая признано
несоразмерным.

======================================================================
Границы размера (frozen owner decision OD-3)
======================================================================

Проверяются ДО дорогих операций — размер зашифрованного контейнера до
`decrypt_bytes`, размер расшифрованного plaintext до `parse_manifest` —
чтобы specially crafted или случайно раздутый файл не заставлял тратить
Scrypt/GCM/JSON-parse ресурсы на заведомо мусорный вход.

MINOR-1 (Independent Review, Correction Pass): проверка размера
контейнера — это не только предварительное наблюдение (`stat()` перед
чтением, что было бы уязвимо к TOCTOU, если файл растёт между проверкой
и чтением), а РЕАЛЬНАЯ граница чтения: `_read_bounded` физически не
запрашивает у ОС больше `MAX_ENCRYPTED_MANIFEST_BYTES + 1` байт за один
вызов `.read(n)`, независимо от фактического размера файла на диске в
момент чтения. Быстрый путь по `stat()` остаётся как оптимизация
(не открывать заведомо огромный файл), но не является единственной
защитой.

======================================================================
Пароль
======================================================================

`password` — только `str`, без нормализации Unicode (та же политика,
что и у `app.security.crypto`), не сохраняется нигде (ни в manifest, ни
в lock-файле, ни в имени temp-файла), не логируется. Пустой пароль и
пароль неверного типа отклоняются на этой границе как
`WorkspaceInputError`, а не как сырое исключение `app.security.crypto`
— вызывающий код Workspace-слоя не должен видеть исключения чужого
модуля напрямую. User-facing password-strength policy сознательно НЕ
вводится этим слайсом (frozen owner decision, откладывается на будущее,
до GUI Stage 11A) — контракт ограничивается "str, не пусто", как и у
`app.security.crypto`.

======================================================================
NOTE-1 (Stage 10B.1) — закрытие на этой границе
======================================================================

`parse_manifest` уже поднимает `WorkspaceCorruptedError` с `from None`,
но исходное `JSONDecodeError`/`UnicodeDecodeError` (способное держать
расшифрованный plaintext через `.doc`/`.object`) остаётся достижимым
программно через `exc.__context__` — `from None` подавляет только ВЫВОД
в traceback (`__suppress_context__`), не сам атрибут.

MAJOR-1 (Independent Review, Correction Pass): одной зачистки
`__context__`/`__cause__` НЕДОСТАТОЧНО. У любого пойманного исключения
есть `exc.__traceback__` — цепочка фреймов от места `raise` до места
перехвата, включающая ВСЕ внутренние фреймы вызова (в т.ч. фрейм самой
`load_encrypted_manifest` с параметром `password` и локальной
`plaintext`, фрейм `_parse_or_sanitize` с `plaintext`, и — до
исправления — фреймы `parse_manifest` с `text`/`data`). Обход
`exc.__traceback__.tb_frame.f_locals` — обычный программный приём,
доступный любому вызывающему коду, не требующий `capture_locals=True` и
не проявляющийся при обычном `str`/`repr`/`traceback.format_exception`.
Экспериментально (Correction Pass) подтверждено и устранено ДВУМЯ
дополняющими механизмами:

    1. `_parse_or_sanitize` не переиспользует объект исключения,
       поднятый `parse_manifest` (его `__traceback__` ведёт внутрь
       `parse_manifest`, а `__context__` остаётся достижим программно) —
       вместо этого безопасная причина извлекается ВНУТРИ except-блока,
       затем except-блок ПОКИДАЕТСЯ, и только ПОСЛЕ этого конструируется
       и поднимается СОВЕРШЕННО НОВЫЙ `WorkspaceCorruptedError` с тем же
       `reason` (тот же приём, что и в `_decrypt_or_map`, см. ниже).
       Экспериментально подтверждено (Correction Pass), что попытка
       просто поднять новый объект через `.with_traceback(None) from
       None` ПРЯМО ВНУТРИ except-блока НЕ РАБОТАЕТ: implicit exception
       chaining интерпретатора всё равно перезаписывает `__context__`
       нового объекта на исходное исключение, пока raise происходит
       внутри активного except — старая цепочка traceback остаётся
       достижима через `exc.__context__.__traceback__`. Только выход из
       except-блока ДО конструирования нового исключения даёт
       `__context__ is None` без ручной перезаписи атрибута.

    2. Каждый внутренний фрейм (`load_encrypted_manifest`,
       `_decrypt_or_map`, `_parse_or_sanitize`), который мог бы попасть
       в traceback финального публичного исключения, явно зачищает свои
       чувствительные локальные переменные (`password`, `encrypted`,
       `plaintext`) непосредственно перед тем, как в нём происходит
       `raise` или через него пропускается исключение (через `finally`
       там, где `raise` происходит в ВЛОЖЕННОМ вызове) —
       экспериментально подтверждено, что переприсваивание локальной
       переменной внутри того же кадра ДО завершения функции отражается
       в `f_locals`, видимых извне через итоговый traceback.

Для ошибок самого крипто-слоя (`_decrypt_or_map`) по-прежнему
дополнительно применяется независимо проверенный механизм: внутреннее
исключение перехватывается, из него извлекается только безопасная
причина, `except`-блок завершается — и уже ПОСЛЕ выхода из него (в той
же функции `sys.exc_info()` уже пуст, проверено экспериментально)
конструируется и поднимается свежее публичное исключение, не
наследующее `__context__` исходного крипто-исключения. `parse_manifest`
из Stage 10B.1 при этом не переписывается и не переоткрывается.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import Union

from app.security.crypto import (
    DecryptionError,
    InvalidContainerError,
    InvalidKDFParametersError,
    UnsupportedContainerVersionError,
    UnsupportedKDFError,
    decrypt_bytes,
    encrypt_bytes,
)
from app.workspace.errors import (
    WorkspaceAuthenticationError,
    WorkspaceCorruptedError,
    WorkspaceCorruptedReason,
    WorkspaceInputError,
    WorkspaceNotFoundError,
)
from app.workspace.manifest import parse_manifest, serialize_manifest
from app.workspace.models import WorkspaceManifest

# Границы размера (frozen owner decision OD-3). Не разбросаны по коду —
# единственное место истины для обеих проверок (load и save).
MAX_ENCRYPTED_MANIFEST_BYTES = 16 * 1024 * 1024
MAX_PLAINTEXT_MANIFEST_BYTES = 8 * 1024 * 1024


# ----------------------------------------------------------------------
# Валидация аргументов публичного API
# ----------------------------------------------------------------------


def _validate_path_argument(path: object) -> Path:
    if not isinstance(path, (str, Path)):
        raise TypeError(f"path должен быть str или Path, получено: {type(path)!r}")
    return Path(path)


def _validate_password_argument(password: object) -> None:
    # Неверный тип/пустой пароль отклоняется здесь как WorkspaceInputError
    # — вызывающий код Workspace-слоя не должен получать сырые
    # TypeError/ValueError из app.security.crypto напрямую (граница
    # слоёв). Сам пароль в сообщение не попадает.
    #
    # MAJOR-1 (Independent Review, Correction Pass #2): даже "невалидный
    # по типу" пароль может оказаться РЕАЛЬНЫМ секретом неверной формы
    # (например, вызывающий код по ошибке передал bytes вместо str) —
    # локальная переменная password зачищается в ЭТОМ (отдельном от
    # load/save) фрейме перед raise, иначе она осталась бы достижима
    # через exc.__traceback__ для этого конкретного кадра-помощника,
    # даже если внешняя функция уже защищена своим try/finally.
    if not isinstance(password, str):
        password = None  # noqa: F841 — зачистка чувствительного локала фрейма
        raise WorkspaceInputError("password должен быть str")
    if password == "":
        password = None  # noqa: F841 — зачистка для единообразия (пустая строка не секрет)
        raise WorkspaceInputError("password не может быть пустым")


# ----------------------------------------------------------------------
# load_encrypted_manifest
# ----------------------------------------------------------------------


def _decrypt_or_map(encrypted: bytes, password: str) -> bytes:
    """
    Расшифровывает container, транслируя исключения app.security.crypto
    в публичные исключения Workspace-слоя по замороженной таблице (см.
    docstring модуля, раздел NOTE-1): неверный пароль и подделка
    аутентифицированных данных неразличимы и дают WorkspaceAuthenticationError
    (frozen owner decision OD-1); все структурные проблемы контейнера
    (bad magic, усечение, неизвестная версия/KDF, недопустимые параметры
    KDF) сворачиваются в единый WorkspaceCorruptedError(MANIFEST_INVALID_CONTAINER).
    Любая ДРУГАЯ (непредвиденная) ошибка app.security.crypto не
    перехватывается и не маскируется — проходит наружу как есть.

    MAJOR-1 (Independent Review): одной только зачистки __context__/
    __cause__ недостаточно — сам ФРЕЙМ этой функции (с параметрами
    `encrypted`/`password`) остаётся частью traceback поднятого ниже
    исключения и был бы виден вызывающему коду через
    `exc.__traceback__.tb_frame.f_locals`. Поэтому `password`/`encrypted`
    явно зачищаются в ЭТОМ фрейме перед raise (проверено
    экспериментально — Correction Pass: реассайн в теле функции ДО
    raise отражается в locals фрейма, видимых через итоговый traceback).
    """
    reason: str = ""
    try:
        return decrypt_bytes(encrypted, password)
    except DecryptionError:
        reason = "auth"
    except (
        InvalidContainerError,
        UnsupportedContainerVersionError,
        UnsupportedKDFError,
        InvalidKDFParametersError,
    ):
        reason = "container"

    # Мы здесь только после того, как исходное crypto-исключение уже
    # обработано и except-блок выше завершён: внутри этой же функции
    # sys.exc_info() в этой точке уже пуст (проверено экспериментально,
    # Stage 10B.2 review) — новое исключение ниже не унаследует
    # __context__ исходного crypto-исключения.
    password = None  # noqa: F841 — зачистка чувствительного локала фрейма
    encrypted = None  # noqa: F841 — зачистка перед тем, как фрейм попадёт в traceback
    if reason == "auth":
        raise WorkspaceAuthenticationError()
    raise WorkspaceCorruptedError(WorkspaceCorruptedReason.MANIFEST_INVALID_CONTAINER)


def _parse_or_sanitize(plaintext: bytes) -> WorkspaceManifest:
    """
    Разбирает manifest. `parse_manifest` (Stage 10B.1) не изменяется и не
    переоткрывается.

    MAJOR-1 (Independent Review): просто зачистить __context__/__cause__
    на ТОМ ЖЕ объекте исключения недостаточно — его собственный
    `__traceback__` по-прежнему ведёт внутрь `parse_manifest` (фреймы с
    `text`/`data`, которые могут содержать расшифрованный plaintext), и
    он остаётся достижимым. Экспериментально (Correction Pass) также
    подтверждено, что `raise новый_объект.with_traceback(None) from None`
    САМ ПО СЕБЕ НЕ РЕШАЕТ проблему: `from None` подавляет только ВЫВОД
    и явно зачищает `__cause__`, но implicit exception chaining
    интерпретатора всё равно устанавливает `__context__` нового объекта
    на исходное исключение `exc`, ПОКА raise выполняется внутри блока
    `except`, перезаписывая любое предварительно присвоенное значение
    `__context__`. Рабочий механизм — тот же, что уже применяется в
    `_decrypt_or_map`: извлечь только безопасную причину ВНУТРИ except,
    затем ПОКИНУТЬ except-блок (после чего `sys.exc_info()` в этой же
    функции пуст — проверено экспериментально), и только тогда
    сконструировать и поднять свежее исключение — на этом этапе
    `__context__` нового объекта остаётся `None` без необходимости
    ручной зачистки. Локальная переменная `plaintext` в ЭТОМ фрейме
    дополнительно зачищается перед raise.
    """
    reason = None
    try:
        return parse_manifest(plaintext)
    except WorkspaceCorruptedError as exc:
        reason = exc.reason

    # Мы здесь только ПОСЛЕ выхода из except-блока выше — см. docstring:
    # sys.exc_info() в этой точке уже пуст, поэтому новое исключение не
    # унаследует __context__ исходного WorkspaceCorruptedError от
    # parse_manifest (и его traceback с text/data остаётся недостижим).
    plaintext = None  # noqa: F841 — зачистка чувствительного локала фрейма
    raise WorkspaceCorruptedError(reason)


def _read_bounded(target: Path) -> bytes:
    """
    Читает `target`, никогда не загружая в память больше
    MAX_ENCRYPTED_MANIFEST_BYTES + 1 байт.

    MINOR-1 (Independent Review): предыдущая версия проверяла
    `target.stat().st_size` ОДИН раз, а затем безусловно вызывала
    `target.read_bytes()` — если файл вырастал МЕЖДУ этими двумя
    вызовами (TOCTOU), проверка размера ничего не защищала: весь
    выросший файл читался в память целиком. `Path.read()`/файловый
    `.read(n)` гарантированно не возвращает больше `n` байт за вызов,
    независимо от фактического размера файла на диске — это и есть
    РЕАЛЬНАЯ граница чтения, а не только предварительное наблюдение.
    """
    with target.open("rb") as f:
        return f.read(MAX_ENCRYPTED_MANIFEST_BYTES + 1)


def load_encrypted_manifest(path: Union[str, Path], password: str) -> WorkspaceManifest:
    """
    Читает `path`, расшифровывает и строго разбирает как
    `WorkspaceManifest`.

    :raises TypeError: path не str/Path.
    :raises WorkspaceInputError: password не str либо пустой.
    :raises WorkspaceNotFoundError: path не существует.
    :raises WorkspaceAuthenticationError: неверный пароль либо
        ciphertext/tag/header повреждены или подделаны (намеренно
        неразличимо, frozen owner decision OD-1).
    :raises WorkspaceCorruptedError: MANIFEST_INVALID_CONTAINER
        (структурно некорректный/неизвестной версии контейнер либо
        контейнер превышает MAX_ENCRYPTED_MANIFEST_BYTES),
        MANIFEST_INVALID_PAYLOAD (после успешной расшифровки — не UTF-8,
        не JSON либо превышает MAX_PLAINTEXT_MANIFEST_BYTES) или
        MANIFEST_UNSUPPORTED_VERSION (после успешной расшифровки —
        неподдерживаемая схема manifest).

    Побочные эффекты: только чтение `path`. Блокировка (workspace.lock)
    — забота вызывающего кода через `WorkspaceFileLock`, не этой
    функции.

    MAJOR-1 (Independent Review, Correction Pass #1 и #2): пароль и
    расшифрованный plaintext зачищаются в ЭТОМ фрейме (через `finally`)
    при ЛЮБОМ выходе из функции. Re-Review (Correction Pass #2) показал,
    что Correction Pass #1 защищал только тело ПОСЛЕ валидации
    аргументов — `TypeError` из `_validate_path_argument` (например,
    невалидный тип `path` при реальном пароле) поднимался ДО входа в
    защищённый `try`, оставляя `password` в этом фрейме. Поэтому теперь
    защищённый `try` начинается с САМОЙ ПЕРВОЙ строки тела функции и
    охватывает валидацию `path`/`password` целиком — экспериментально
    подтверждено (Correction Pass #2), что `finally`-зачистка кадра
    отрабатывает независимо от того, где именно внутри охваченного
    `try` произошёл `raise`. Это гарантирует, что ни одно публичное
    исключение, чей traceback проходит через данный фрейм — включая
    `TypeError`, `WorkspaceInputError`, `WorkspaceNotFoundError` и
    превышение размера контейнера — не оставляет пароль/plaintext
    доступными через `exc.__traceback__.tb_frame.f_locals`.
    """
    try:
        target = _validate_path_argument(path)
        _validate_password_argument(password)

        if not target.exists():
            raise WorkspaceNotFoundError()

        # Быстрый путь по метаданным ФС: не единственная защита (см.
        # _read_bounded) — просто избегает открытия заведомо огромного
        # файла, если размер уже известен из stat().
        if target.stat().st_size > MAX_ENCRYPTED_MANIFEST_BYTES:
            raise WorkspaceCorruptedError(WorkspaceCorruptedReason.MANIFEST_INVALID_CONTAINER)

        encrypted = _read_bounded(target)
        try:
            if len(encrypted) > MAX_ENCRYPTED_MANIFEST_BYTES:
                raise WorkspaceCorruptedError(WorkspaceCorruptedReason.MANIFEST_INVALID_CONTAINER)

            plaintext = _decrypt_or_map(encrypted, password)
        finally:
            encrypted = None  # noqa: F841 — зачистка чувствительного локала фрейма

        try:
            if len(plaintext) > MAX_PLAINTEXT_MANIFEST_BYTES:
                raise WorkspaceCorruptedError(WorkspaceCorruptedReason.MANIFEST_INVALID_PAYLOAD)

            return _parse_or_sanitize(plaintext)
        finally:
            plaintext = None  # noqa: F841 — зачистка чувствительного локала фрейма
    finally:
        password = None  # noqa: F841 — зачистка чувствительного локала фрейма


# ----------------------------------------------------------------------
# save_encrypted_manifest_atomic
# ----------------------------------------------------------------------


def _atomic_write(target: Path, data: bytes) -> None:
    """
    Атомарная запись encrypted bytes на диск: temp-файл в ТОЙ ЖЕ
    директории -> write -> flush -> fsync -> close -> os.replace(temp,
    target). На диск попадают только encrypted bytes. При любой ошибке
    ДО успешного os.replace: старый target не трогается напрямую,
    temp-файл удаляется best-effort, не маскируя исходное исключение
    (буквально тот же паттерн, что app.mapping.encrypted_file/
    app.mapping.provenance_encrypted).
    """
    directory = target.parent
    fd, tmp_name = tempfile.mkstemp(dir=str(directory), prefix=f".{target.name}.", suffix=".tmp")
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as tmp_file:
            tmp_file.write(data)
            tmp_file.flush()
            os.fsync(tmp_file.fileno())
        os.replace(str(tmp_path), str(target))
    except Exception:
        try:
            tmp_path.unlink(missing_ok=True)
        except OSError:
            # Best-effort cleanup: ошибка удаления temp-файла не должна
            # маскировать исходную ошибку записи/замены.
            pass
        raise


def save_encrypted_manifest_atomic(
    path: Union[str, Path], manifest: WorkspaceManifest, password: str
) -> None:
    """
    Сериализует `manifest`, шифрует и атомарно заменяет `path`.

    :raises TypeError: path не str/Path либо manifest не WorkspaceManifest.
    :raises WorkspaceInputError: password не str либо пустой; либо
        canonical-сериализация manifest превышает MAX_PLAINTEXT_MANIFEST_BYTES,
        либо получившийся зашифрованный контейнер превышает
        MAX_ENCRYPTED_MANIFEST_BYTES (оба случая проверяются ДО начала
        публикации в файловую систему — ни временный, ни целевой файл
        не создаются и не изменяются).
    :raises OSError: сбой файловой операции (создание temp/write/flush/
        fsync/replace) — не оборачивается в WorkspaceError, проходит как
        есть (см. docstring модуля, "Атомарность != durability").

    Побочные эффекты: создаёт и (при успехе) удаляет temp-файл в той же
    директории, что и `path`; заменяет `path` только атомарно, целиком
    или не заменяет вовсе. Никакой plaintext temp-файл не создаётся.
    Блокировка — не забота этой функции.

    MAJOR-1 (Independent Review, Correction Pass #2): эта функция
    держит `password` (аргумент) и `plaintext` (канонический JSON
    manifest, полученный от `serialize_manifest`) в собственном фрейме.
    Re-Review показал, что до этого исправления функция не была
    защищена вовсе — любое исключение (`TypeError` на `path`/`manifest`,
    превышение размера, и что особенно важно — `OSError` от
    `_atomic_write`/`os.replace`, штатно тестируемый в группе F) уносило
    оба значения в своём traceback. По тому же принципу, что и
    `load_encrypted_manifest`: единственный `try/finally`, охватывающий
    ВСЮ функцию целиком (от первой строки тела до `_atomic_write`),
    зачищает `password`/`plaintext`/`encrypted` в этом фрейме при любом
    выходе. `encrypted` (ciphertext) зачищается тоже — не потому что это
    требуется контрактом конфиденциальности (ciphertext не секрет), а
    для единообразия с `load_encrypted_manifest` и минимальной сложности
    дизайна.
    """
    try:
        target = _validate_path_argument(path)
        if not isinstance(manifest, WorkspaceManifest):
            raise TypeError(f"manifest должен быть WorkspaceManifest, получено: {type(manifest)!r}")
        _validate_password_argument(password)

        try:
            plaintext = serialize_manifest(manifest)
            if len(plaintext) > MAX_PLAINTEXT_MANIFEST_BYTES:
                raise WorkspaceInputError("manifest превышает допустимый размер для сохранения")

            encrypted = encrypt_bytes(plaintext, password)
        finally:
            plaintext = None  # noqa: F841 — зачистка чувствительного локала фрейма

        try:
            if len(encrypted) > MAX_ENCRYPTED_MANIFEST_BYTES:
                raise WorkspaceInputError("зашифрованный manifest превышает допустимый размер для сохранения")

            _atomic_write(target, encrypted)
        finally:
            encrypted = None  # noqa: F841 — зачистка чувствительного локала фрейма (ciphertext, не секрет, но чистим для единообразия)
    finally:
        password = None  # noqa: F841 — зачистка чувствительного локала фрейма
