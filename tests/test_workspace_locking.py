"""
Тесты Stage 10B.2: app.workspace.locking (WorkspaceFileLock).

Группы:
    A. Базовый acquire/release, персистентность файла
    B. create=False / create=True семантика
    C. Nested acquisition (тот же объект, второй объект того же процесса)
    D. Release / context manager
    E. Кросс-процессная эксклюзивность (реальный subprocess)
    F. Крах процесса (реальный subprocess, kill)
    G. Отсутствие ожидания (non-blocking)
    H. Correction Pass MINOR-2: сбой unlock/close внутри release()
"""

from __future__ import annotations

import msvcrt
import subprocess
import sys
import time
from pathlib import Path

import pytest

from app.workspace import locking as locking_module
from app.workspace.errors import (
    WorkspaceCorruptedError,
    WorkspaceCorruptedReason,
    WorkspaceLockedError,
    WorkspaceLockedReason,
)
from app.workspace.locking import WorkspaceFileLock

# Дочерний процесс передаётся через `python -c <источник>`, а не отдельным
# файлом — File Scope Stage10B.2 ограничен четырьмя файлами (storage.py,
# locking.py и двумя тестовыми модулями), заводить пятый файл только ради
# subprocess-хелпера нет причины. `cwd` дочернего процесса — корень
# репозитория (см. _spawn_child), поэтому `import app...` работает без
# ручной манипуляции sys.path (для `-c` Python подставляет '' первым
# элементом sys.path, что резолвится в текущую рабочую директорию).
_CHILD_SOURCE = """
import sys, time
from app.workspace.locking import WorkspaceFileLock

lock_path = sys.argv[1]
mode = sys.argv[2]

lock = WorkspaceFileLock(lock_path)
lock.acquire()
print("LOCKED", flush=True)

if mode == "hold":
    time.sleep(30)
elif mode == "hold_then_release":
    time.sleep(0.5)
    lock.release()
    print("RELEASED", flush=True)
else:
    raise ValueError("unknown mode: " + mode)
"""


@pytest.fixture()
def lock_path(tmp_path: Path) -> Path:
    return tmp_path / "workspace.lock"


# ---------------------------------------------------------------------------
# A. Базовый acquire/release, персистентность файла
# ---------------------------------------------------------------------------


def test_acquire_release_happy_path(lock_path: Path) -> None:
    lock = WorkspaceFileLock(lock_path)
    lock.acquire(create=True)
    assert lock_path.exists()
    lock.release()
    assert lock_path.exists()


def test_lock_file_persists_after_release(lock_path: Path) -> None:
    lock = WorkspaceFileLock(lock_path)
    lock.acquire(create=True)
    lock.release()
    assert lock_path.exists(), "workspace.lock — персистентный layout-файл, release его не удаляет"


def test_lock_file_stays_zero_bytes(lock_path: Path) -> None:
    lock = WorkspaceFileLock(lock_path)
    lock.acquire(create=True)
    assert lock_path.stat().st_size == 0
    lock.release()
    assert lock_path.stat().st_size == 0


def test_reacquire_after_release_by_new_object(lock_path: Path) -> None:
    first = WorkspaceFileLock(lock_path)
    first.acquire(create=True)
    first.release()

    second = WorkspaceFileLock(lock_path)
    second.acquire()  # файл уже существует, create=False по умолчанию достаточно
    second.release()


# ---------------------------------------------------------------------------
# B. create=False / create=True семантика
# ---------------------------------------------------------------------------


def test_missing_lock_file_create_false_fails_closed(tmp_path: Path, lock_path: Path) -> None:
    before = sorted(p.name for p in tmp_path.iterdir())

    with pytest.raises(WorkspaceCorruptedError) as excinfo:
        WorkspaceFileLock(lock_path).acquire()

    assert excinfo.value.reason is WorkspaceCorruptedReason.LOCK_FILE_MISSING

    after = sorted(p.name for p in tmp_path.iterdir())
    assert before == after == []


def test_missing_lock_file_create_false_causes_no_filesystem_mutation(
    tmp_path: Path, lock_path: Path
) -> None:
    assert not lock_path.exists()
    with pytest.raises(WorkspaceCorruptedError):
        WorkspaceFileLock(lock_path).acquire(create=False)
    assert not lock_path.exists()
    assert list(tmp_path.iterdir()) == []


def test_create_true_creates_lock_file(lock_path: Path) -> None:
    assert not lock_path.exists()
    lock = WorkspaceFileLock(lock_path)
    lock.acquire(create=True)
    assert lock_path.exists()
    assert lock_path.stat().st_size == 0
    lock.release()


def test_create_true_on_existing_file_does_not_modify_it(lock_path: Path) -> None:
    lock_path.write_bytes(b"")  # существующий 0-байтный layout-файл
    original_mtime_ns = lock_path.stat().st_mtime_ns

    lock = WorkspaceFileLock(lock_path)
    lock.acquire(create=True)
    lock.release()

    assert lock_path.stat().st_size == 0
    # Содержимое не тронуто (усечение/перезапись не выполнялись) —
    # 0 байт до и после, что и является единственным проверяемым здесь
    # инвариантом контента; mtime не проверяем строго (ОС может
    # обновлять метаданные открытия), важно отсутствие truncate/rewrite.
    assert lock_path.read_bytes() == b""


# ---------------------------------------------------------------------------
# C. Nested acquisition
# ---------------------------------------------------------------------------


def test_same_object_nested_acquisition(lock_path: Path) -> None:
    lock = WorkspaceFileLock(lock_path)
    lock.acquire(create=True)
    try:
        with pytest.raises(WorkspaceLockedError) as excinfo:
            lock.acquire()
        assert excinfo.value.reason is WorkspaceLockedReason.NESTED_ACQUISITION
    finally:
        lock.release()


def test_second_object_same_path_nested_acquisition(lock_path: Path) -> None:
    first = WorkspaceFileLock(lock_path)
    first.acquire(create=True)
    second = WorkspaceFileLock(lock_path)
    try:
        with pytest.raises(WorkspaceLockedError) as excinfo:
            second.acquire()
        assert excinfo.value.reason is WorkspaceLockedReason.NESTED_ACQUISITION
    finally:
        first.release()


def test_nested_acquisition_detected_before_touching_os_lock(lock_path: Path) -> None:
    # Второй объект не должен даже пытаться открыть файл/взять OS-лок —
    # проверяем это косвенно: после неудачной попытки второго объекта
    # первый по-прежнему держит лок и успешно освобождает его сам.
    first = WorkspaceFileLock(lock_path)
    first.acquire(create=True)
    second = WorkspaceFileLock(lock_path)
    with pytest.raises(WorkspaceLockedError):
        second.acquire()
    first.release()  # если бы second повредил состояние first, это бы упало
    reacquired = WorkspaceFileLock(lock_path)
    reacquired.acquire()
    reacquired.release()


def test_failed_acquisition_clears_registry_reservation(lock_path: Path) -> None:
    first = WorkspaceFileLock(lock_path)
    first.acquire(create=True)
    second = WorkspaceFileLock(lock_path)
    with pytest.raises(WorkspaceLockedError):
        second.acquire()
    first.release()

    # После освобождения первым второй объект должен суметь захватить —
    # если бы неудачная попытка second оставила "мусорную" резервацию
    # под его собственным ключом, это не помешало бы first (другой
    # ключ), но если бы second сам оставил СЕБЯ зарегистрированным,
    # третья попытка через второй объект тоже была бы NESTED навсегда.
    second.acquire()
    second.release()


# ---------------------------------------------------------------------------
# D. Release / context manager
# ---------------------------------------------------------------------------


def test_release_without_ownership_raises_not_held(lock_path: Path) -> None:
    lock = WorkspaceFileLock(lock_path)
    with pytest.raises(WorkspaceLockedError) as excinfo:
        lock.release()
    assert excinfo.value.reason is WorkspaceLockedReason.NOT_HELD


def test_release_twice_raises_not_held_second_time(lock_path: Path) -> None:
    lock = WorkspaceFileLock(lock_path)
    lock.acquire(create=True)
    lock.release()
    with pytest.raises(WorkspaceLockedError) as excinfo:
        lock.release()
    assert excinfo.value.reason is WorkspaceLockedReason.NOT_HELD


def test_context_manager_acquires_and_releases(lock_path: Path) -> None:
    lock = WorkspaceFileLock(lock_path)
    lock.acquire(create=True)
    lock.release()

    other = WorkspaceFileLock(lock_path)
    with WorkspaceFileLock(lock_path) as ctx_lock:
        assert ctx_lock.path == lock_path
        # Пока тело with активно, лок реально удержан: другой объект
        # получает NESTED_ACQUISITION (тот же процесс, тот же путь).
        with pytest.raises(WorkspaceLockedError) as excinfo:
            other.acquire()
        assert excinfo.value.reason is WorkspaceLockedReason.NESTED_ACQUISITION

    # После выхода из with лок снят — реакквизиция удаётся.
    reacquired = WorkspaceFileLock(lock_path)
    reacquired.acquire()
    reacquired.release()


def test_context_manager_releases_when_body_raises_and_propagates_exception(
    lock_path: Path,
) -> None:
    lock_path.write_bytes(b"")

    class _BodyError(Exception):
        pass

    with pytest.raises(_BodyError):
        with WorkspaceFileLock(lock_path):
            raise _BodyError("boom")

    # Лок должен быть снят, несмотря на исключение тела — иначе
    # следующий acquire получил бы NESTED/HELD_BY_OTHER.
    fresh = WorkspaceFileLock(lock_path)
    fresh.acquire()
    fresh.release()


def test_body_exception_is_not_replaced_by_lock_cleanup(lock_path: Path) -> None:
    lock_path.write_bytes(b"")

    class _BodyError(Exception):
        pass

    try:
        with WorkspaceFileLock(lock_path):
            raise _BodyError("original body exception")
    except _BodyError as exc:
        assert str(exc) == "original body exception"
    else:  # pragma: no cover
        pytest.fail("исключение тела должно было распространиться")


# ---------------------------------------------------------------------------
# E. Кросс-процессная эксклюзивность (реальный subprocess)
# ---------------------------------------------------------------------------


def _spawn_child(lock_path: Path, mode: str) -> subprocess.Popen:
    return subprocess.Popen(
        [sys.executable, "-c", _CHILD_SOURCE, str(lock_path), mode],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        cwd=str(Path(__file__).resolve().parent.parent),
    )


def _read_signal(child: subprocess.Popen, expected: str, timeout: float = 10.0) -> None:
    deadline = time.monotonic() + timeout
    line = child.stdout.readline()
    assert line.strip() == expected, (
        f"ожидался сигнал {expected!r}, получено {line!r}; stderr={child.stderr.read() if child.stderr else ''}"
    )
    assert time.monotonic() < deadline + 1  # защита от неожиданно долгого запуска


def test_cross_process_contention_real_subprocess(lock_path: Path) -> None:
    lock_path.write_bytes(b"")
    child = _spawn_child(lock_path, "hold")
    try:
        _read_signal(child, "LOCKED")

        parent_lock = WorkspaceFileLock(lock_path)
        with pytest.raises(WorkspaceLockedError) as excinfo:
            parent_lock.acquire()
        assert excinfo.value.reason is WorkspaceLockedReason.HELD_BY_OTHER
    finally:
        child.terminate()
        child.wait(timeout=10)


def test_acquire_succeeds_after_child_releases(lock_path: Path) -> None:
    lock_path.write_bytes(b"")
    child = _spawn_child(lock_path, "hold_then_release")
    try:
        _read_signal(child, "LOCKED")

        parent_lock = WorkspaceFileLock(lock_path)
        with pytest.raises(WorkspaceLockedError):
            parent_lock.acquire()

        _read_signal(child, "RELEASED")
        child.wait(timeout=10)

        # После явного и штатного release в дочернем процессе родитель
        # должен суметь захватить лок.
        parent_lock.acquire()
        parent_lock.release()
    finally:
        if child.poll() is None:
            child.terminate()
            child.wait(timeout=10)


# ---------------------------------------------------------------------------
# F. Крах процесса (реальный subprocess, kill)
# ---------------------------------------------------------------------------


def test_lock_released_by_os_after_process_crash(lock_path: Path) -> None:
    lock_path.write_bytes(b"")
    child = _spawn_child(lock_path, "hold")
    try:
        _read_signal(child, "LOCKED")

        parent_lock = WorkspaceFileLock(lock_path)
        with pytest.raises(WorkspaceLockedError) as excinfo:
            parent_lock.acquire()
        assert excinfo.value.reason is WorkspaceLockedReason.HELD_BY_OTHER

        child.kill()  # жёсткое завершение — без шанса на graceful release
        child.wait(timeout=10)

        # ОС обязана снять файловый лок при завершении процесса, включая
        # аварийное — подтверждено экспериментально (Contract Review).
        # Небольшой ограниченный retry ЗДЕСЬ, в тесте (не в продакшн-коде
        # — там неблокирующий контракт OD-4 остаётся строгим): освобождение
        # OS-лока после kill() может отставать от возврата wait() на доли
        # секунды из-за планировщика/фоновой активности ОС — это не связано
        # с логикой WorkspaceFileLock и наблюдалось лишь эпизодически.
        for attempt in range(20):
            try:
                parent_lock.acquire()
                break
            except WorkspaceLockedError:
                if attempt == 19:
                    raise
                time.sleep(0.05)
        parent_lock.release()

        # workspace.lock — персистентный файл, крах процесса его не удаляет.
        assert lock_path.exists()
    finally:
        if child.poll() is None:
            child.kill()
            child.wait(timeout=10)


# ---------------------------------------------------------------------------
# G. Отсутствие ожидания (non-blocking)
# ---------------------------------------------------------------------------


def test_acquire_does_not_wait_when_contended(lock_path: Path) -> None:
    lock_path.write_bytes(b"")
    child = _spawn_child(lock_path, "hold")
    try:
        _read_signal(child, "LOCKED")

        started = time.monotonic()
        with pytest.raises(WorkspaceLockedError):
            WorkspaceFileLock(lock_path).acquire()
        elapsed = time.monotonic() - started

        # Неблокирующий захват должен вернуться практически немедленно;
        # большой запас (2с) исключает флейки на медленной машине, но
        # надёжно отличает non-blocking от случайного индефинит-ожидания.
        assert elapsed < 2.0
    finally:
        child.terminate()
        child.wait(timeout=10)


def test_acquire_has_no_timeout_parameter() -> None:
    import inspect

    signature = inspect.signature(WorkspaceFileLock.acquire)
    assert "timeout" not in signature.parameters


# ---------------------------------------------------------------------------
# H. Correction Pass MINOR-2: сбой unlock/close внутри release()
#
# Independent Review показал: если И msvcrt.locking(LK_UNLCK), И
# self._file.close() поднимают исключение, прежний finally-блок
# прерывался на close() ДО зачистки _held/registry — объект и
# канонический путь "залипали" в процессе навсегда. Тесты ниже
# проверяют, что зачистка внутреннего состояния теперь происходит
# БЕЗУСЛОВНО, а также приоритет первичной/вторичной ошибки.
# ---------------------------------------------------------------------------


class _RealCloseThenFailWriter:
    """
    Обёртка вокруг реального файлового объекта: делегирует seek/fileno
    как есть; close() ВСЕГДА реально закрывает нижележащий файл (чтобы
    OS-ресурс был по-настоящему освобождён — иначе тест не смог бы
    детерминированно проверить, что НОВЫЙ объект способен захватить лок
    впоследствии), но при `fail=True` после этого дополнительно поднимает
    OSError — как это бывает при сбое flush-on-close уже после
    фактического освобождения хэндла на уровне ОС.
    """

    def __init__(self, real, fail: bool) -> None:
        self._real = real
        self._fail = fail

    def seek(self, *args: object, **kwargs: object) -> int:
        return self._real.seek(*args, **kwargs)

    def fileno(self) -> int:
        return self._real.fileno()

    def close(self) -> None:
        self._real.close()
        if self._fail:
            raise OSError("simulated close failure")


def _install_failing_unlock(monkeypatch: pytest.MonkeyPatch, fail: bool) -> None:
    real_locking = msvcrt.locking

    def patched(fd: int, mode: int, n: int) -> None:
        if mode == msvcrt.LK_UNLCK and fail:
            raise OSError("simulated unlock failure")
        return real_locking(fd, mode, n)

    monkeypatch.setattr(locking_module.msvcrt, "locking", patched)


def test_release_unlock_fails_close_succeeds_state_still_cleaned(
    lock_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    lock = WorkspaceFileLock(lock_path)
    lock.acquire(create=True)
    real_file = lock._file
    lock._file = _RealCloseThenFailWriter(real_file, fail=False)

    _install_failing_unlock(monkeypatch, fail=True)
    with pytest.raises(OSError, match="simulated unlock failure"):
        lock.release()
    monkeypatch.undo()

    assert lock._held is False
    assert lock._canonical not in locking_module._registry

    fresh = WorkspaceFileLock(lock_path)
    fresh.acquire()
    fresh.release()


def test_release_unlock_succeeds_close_fails_state_still_cleaned(
    lock_path: Path,
) -> None:
    lock = WorkspaceFileLock(lock_path)
    lock.acquire(create=True)
    real_file = lock._file
    lock._file = _RealCloseThenFailWriter(real_file, fail=True)

    with pytest.raises(OSError, match="simulated close failure"):
        lock.release()

    assert lock._held is False
    assert lock._canonical not in locking_module._registry

    fresh = WorkspaceFileLock(lock_path)
    fresh.acquire()
    fresh.release()


def test_release_unlock_and_close_both_fail_state_still_cleaned(
    lock_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    lock = WorkspaceFileLock(lock_path)
    lock.acquire(create=True)
    real_file = lock._file
    lock._file = _RealCloseThenFailWriter(real_file, fail=True)

    _install_failing_unlock(monkeypatch, fail=True)
    with pytest.raises(OSError, match="simulated unlock failure"):
        # Приоритет: ошибка unlock первична, ошибка close вторична и не
        # должна маскировать первую (см. docstring release()).
        lock.release()
    monkeypatch.undo()

    assert lock._held is False, "внутреннее состояние должно зачищаться, даже если ОБА шага сбоят"
    assert lock._canonical not in locking_module._registry, (
        "канонический путь не должен оставаться зарезервированным навсегда в процессе"
    )

    # D/E: новый объект способен на легитимную последующую попытку, а не
    # получает NESTED_ACQUISITION исключительно из-за устаревшей записи
    # в process-global registry.
    fresh = WorkspaceFileLock(lock_path)
    fresh.acquire()
    fresh.release()


def test_release_failure_does_not_prevent_reuse_of_canonical_path(
    lock_path: Path,
) -> None:
    """
    Отдельная явная проверка постусловия D/E: после ЛЮБОГО сбойного
    release() (даже без повторной попытки тем же объектом) канонический
    путь остаётся пригоден для НОВОГО объекта, а не "отравлен" навсегда.
    """
    lock = WorkspaceFileLock(lock_path)
    lock.acquire(create=True)
    real_file = lock._file
    lock._file = _RealCloseThenFailWriter(real_file, fail=True)

    with pytest.raises(OSError):
        lock.release()

    for _ in range(3):
        fresh = WorkspaceFileLock(lock_path)
        fresh.acquire()
        fresh.release()


def test_release_context_manager_body_exception_survives_normal_release(
    lock_path: Path,
) -> None:
    """
    Повторная проверка (после исправления MINOR-2): если release() внутри
    __exit__ проходит успешно, исключение тела with остаётся неизменным
    — обычная уборка не подменяет и не маскирует его.
    """
    lock_path.write_bytes(b"")

    class _BodyError(Exception):
        pass

    with pytest.raises(_BodyError, match="original"):
        with WorkspaceFileLock(lock_path):
            raise _BodyError("original")
