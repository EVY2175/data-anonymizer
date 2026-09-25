"""
Тесты Stage 10B.3: жизненный цикл Workspace — create_workspace/
open_workspace, clean-skeleton retry, layout safety, lock-объект.

Группы:
    A. create_workspace — успешный путь
    B. create_workspace — политика пароля (OD-1)
    C. create_workspace — clean skeleton (retry-safety, §13/§15)
    D. open_workspace — успешный путь
    E. open_workspace — карта ошибок (§ failure matrix)
    F. Workspace.lock() / nested acquisition
    G. Workspace.info / recovery_required
    H. Layout safety — детерминированная проверка reparse-point защиты
       (Correction Pass #1, MINOR-1: monkeypatch на _is_reparse_like
       вместо реального symlink/junction, требующего повышенных прав)
    I. Concurrency (Correction Pass #2, BLOCKER-1): детерминированная
       регрессия на TOCTOU-гонку create_workspace vs create_workspace,
       закрытую post-lock ревалидацией чистого skeleton
"""

from __future__ import annotations

import os
import threading
from pathlib import Path

import pytest

from app.workspace import Workspace, create_workspace, open_workspace
from app.workspace.errors import (
    WorkspaceAuthenticationError,
    WorkspaceBindingError,
    WorkspaceCorruptedError,
    WorkspaceCorruptedReason,
    WorkspaceInputError,
    WorkspaceLockedError,
    WorkspaceLockedReason,
    WorkspaceNotFoundError,
)

PASSWORD = "test-workspace-password-123"


# ---------------------------------------------------------------------------
# I. Concurrency — детерминированный TOCTOU race harness
#
# Не полагаемся на реальный тайминг потоков ОС (запрещено §26/§27
# Correction Pass #2). Вместо этого monkeypatch'им ЕДИНСТВЕННУЮ функцию,
# которую create_workspace/adopt_stores вызывают ДВАЖДЫ (pre-lock и
# post-lock) — app.workspace.workspace._ensure_clean_skeleton_or_absent —
# так, чтобы ПЕРВЫЙ вызов для потока-"неудачника" выполнил РЕАЛЬНУЮ
# проверку (она законно проходит, т.к. root на тот момент ещё чист), а
# затем заблокировался на threading.Event до явного сигнала "победитель
# завершил инициализацию". Именно так voспроизводится устаревшее pre-lock
# предположение, не будучи гонкой по времени. Все ожидания — с таймаутом
# (bounded wait), чтобы зависший тест падал, а не висел вечно.
# ---------------------------------------------------------------------------

_RACE_WAIT_TIMEOUT = 10.0


def _force_stale_pre_lock_race(monkeypatch, loser_thread_name: str):
    """
    Возвращает (loser_is_waiting, release_loser) — два threading.Event.
    loser_is_waiting устанавливается, когда поток loser_thread_name
    прошёл свой ПЕРВЫЙ (pre-lock) вызов _ensure_clean_skeleton_or_absent
    и застрял в ожидании. release_loser должен быть выставлен вызывающим
    тестом, когда "победитель" уже полностью завершил инициализацию —
    только тогда loser продолжит выполнение (mkdir/lock/post-lock-check).
    ВТОРОЙ и последующие вызовы для того же потока (post-lock
    ревалидация) проходят немедленно, без повторной блокировки.
    """
    import app.workspace.workspace as workspace_module

    real_check = workspace_module._ensure_clean_skeleton_or_absent
    loser_is_waiting = threading.Event()
    release_loser = threading.Event()
    call_counts: dict[str, int] = {}
    counts_lock = threading.Lock()

    def _patched(root_path):
        real_check(root_path)
        if threading.current_thread().name == loser_thread_name:
            with counts_lock:
                call_counts[loser_thread_name] = call_counts.get(loser_thread_name, 0) + 1
                is_first_call = call_counts[loser_thread_name] == 1
            if is_first_call:
                loser_is_waiting.set()
                completed = release_loser.wait(timeout=_RACE_WAIT_TIMEOUT)
                if not completed:
                    raise AssertionError(
                        "release_loser не был выставлен вовремя -- тест завис бы без таймаута"
                    )

    monkeypatch.setattr(workspace_module, "_ensure_clean_skeleton_or_absent", _patched)
    return loser_is_waiting, release_loser


def _wait_ok(event: threading.Event) -> bool:
    return event.wait(timeout=_RACE_WAIT_TIMEOUT)


def test_create_vs_create_race_exactly_one_winner(tmp_path, monkeypatch):
    """
    Точное воспроизведение сценария из Independent Review (BLOCKER-1):
    contender-B проходит pre-lock clean-skeleton проверку, затем
    contender-A ПОЛНОСТЬЮ завершает create_workspace, и только потом B
    возобновляется и пытается захватить (уже использованный и
    освобождённый) лок. До Correction Pass #2 это приводило к тихой
    перезаписи workspace A. После исправления B обязан отказать УЖЕ
    ПОСЛЕ захвата лока (post-lock ревалидация), не тронув workspace A.
    """
    root = tmp_path / "ws"
    loser_waiting, release_loser = _force_stale_pre_lock_race(monkeypatch, "contender-B")

    results: dict = {}

    def run_b():
        try:
            ws = create_workspace(root, "password-contender-B-1", label="loser-B")
            results["b_error"] = None
            results["b_workspace_id"] = ws.info.workspace_id
        except Exception as exc:  # noqa: BLE001 -- фиксируем любое исключение для проверки типа ниже
            results["b_error"] = exc

    thread_b = threading.Thread(target=run_b, name="contender-B")
    thread_b.start()
    assert _wait_ok(loser_waiting), "contender-B не вошёл в ожидание вовремя"

    ws_a = create_workspace(root, "password-contender-A-1", label="winner-A")

    release_loser.set()
    thread_b.join(timeout=_RACE_WAIT_TIMEOUT)
    assert not thread_b.is_alive(), "contender-B не завершился вовремя"

    assert isinstance(results.get("b_error"), WorkspaceCorruptedError)
    assert results["b_error"].reason is WorkspaceCorruptedReason.UNSAFE_LAYOUT
    assert "b_workspace_id" not in results

    final = open_workspace(root, "password-contender-A-1")
    assert final.info.workspace_id == ws_a.info.workspace_id
    assert final.info.label == "winner-A"
    assert final.info.revision == 1

    # Пароль неудачника НЕ стал паролем workspace.
    with pytest.raises(WorkspaceAuthenticationError):
        open_workspace(root, "password-contender-B-1")


def test_post_lock_revalidation_detects_full_skeleton_not_just_workspace_enc(tmp_path, monkeypatch):
    """
    Доказывает, что post-lock ревалидация — это ПОЛНАЯ clean-skeleton
    проверка (_ensure_clean_skeleton_or_absent), а не узкая проверка
    "существует ли workspace.enc": пока contender-B заблокирован между
    pre-lock и post-lock, тест кладёт в root постороннее содержимое, НЕ
    являющееся workspace.enc (непустую структурную директорию), и
    проверяет, что B всё равно корректно отказывает.
    """
    root = tmp_path / "ws"
    loser_waiting, release_loser = _force_stale_pre_lock_race(monkeypatch, "contender-B")

    results: dict = {}

    def run_b():
        try:
            create_workspace(root, "password-contender-B-1", label="loser-B")
            results["b_error"] = None
        except Exception as exc:  # noqa: BLE001
            results["b_error"] = exc

    thread_b = threading.Thread(target=run_b, name="contender-B")
    thread_b.start()
    assert _wait_ok(loser_waiting)

    # НЕ полноценный workspace -- просто постороннее содержимое,
    # нарушающее clean-skeleton контракт, но никак не связанное с
    # workspace.enc конкретно.
    root.mkdir(parents=True, exist_ok=True)
    (root / "stores").mkdir()
    (root / "stores" / "unexpected.bin").write_bytes(b"synthetic marker, not workspace.enc")

    release_loser.set()
    thread_b.join(timeout=_RACE_WAIT_TIMEOUT)
    assert not thread_b.is_alive()

    assert isinstance(results.get("b_error"), WorkspaceCorruptedError)
    assert results["b_error"].reason is WorkspaceCorruptedReason.UNSAFE_LAYOUT


# ---------------------------------------------------------------------------
# A. create_workspace — успешный путь
# ---------------------------------------------------------------------------


def test_create_workspace_basic_layout(tmp_path):
    root = tmp_path / "ws"
    ws = create_workspace(root, PASSWORD, label="My Workspace")

    assert isinstance(ws, Workspace)
    assert ws.root == root
    assert (root / "workspace.enc").is_file()
    assert (root / "workspace.lock").is_file()
    assert (root / "workspace.lock").stat().st_size == 0
    for name in ("stores", "provenance", "safe", "local_plaintext", "backup"):
        assert (root / name).is_dir()

    # OD-2: пустые store-файлы физически не создаются.
    assert not (root / "stores" / "mapping.enc").exists()
    assert not (root / "stores" / "identifiers.enc").exists()

    info = ws.info
    assert info.revision == 1
    assert info.label == "My Workspace"
    assert info.mapping_entry_count == 0
    assert info.identifier_entry_count == 0
    assert info.artifact_count == 0
    assert info.latest_analytical_artifact_id is None
    assert len(info.workspace_id) == 32
    assert all(c in "0123456789abcdef" for c in info.workspace_id)

    assert ws.recovery_required is False


def test_create_workspace_without_label(tmp_path):
    ws = create_workspace(tmp_path / "ws", PASSWORD)
    assert ws.info.label is None


def test_create_workspace_lock_released_after_create(tmp_path):
    root = tmp_path / "ws"
    create_workspace(root, PASSWORD)
    # Если лок не был бы освобождён, повторное открытие тут же упало бы
    # HELD_BY_OTHER (тот же процесс).
    ws2 = open_workspace(root, PASSWORD)
    assert ws2.info.revision == 1


def test_create_workspace_two_distinct_workspaces_have_distinct_ids(tmp_path):
    ws1 = create_workspace(tmp_path / "a", PASSWORD)
    ws2 = create_workspace(tmp_path / "b", PASSWORD)
    assert ws1.info.workspace_id != ws2.info.workspace_id


# ---------------------------------------------------------------------------
# B. create_workspace — политика пароля (OD-1)
# ---------------------------------------------------------------------------


def test_create_workspace_rejects_too_short_password(tmp_path):
    with pytest.raises(WorkspaceInputError):
        create_workspace(tmp_path / "ws", "short1")
    assert not (tmp_path / "ws").exists()


def test_create_workspace_rejects_non_str_password(tmp_path):
    with pytest.raises(WorkspaceInputError):
        create_workspace(tmp_path / "ws", 12345678)  # type: ignore[arg-type]
    assert not (tmp_path / "ws").exists()


def test_create_workspace_accepts_minimum_length_password(tmp_path):
    ws = create_workspace(tmp_path / "ws", "12345678")
    assert ws.info.revision == 1


def test_create_workspace_password_no_strip_or_normalization(tmp_path):
    # Пароль с ведущими/хвостовыми пробелами значим целиком — тримминг
    # не выполняется.
    password = "  pw with spaces  "
    root = tmp_path / "ws"
    create_workspace(root, password)
    with pytest.raises(WorkspaceAuthenticationError):
        open_workspace(root, password.strip())
    # А с исходным (нетронутым) паролем — открывается.
    open_workspace(root, password)


# ---------------------------------------------------------------------------
# C. create_workspace — clean skeleton (retry-safety)
# ---------------------------------------------------------------------------


def test_create_workspace_retries_on_lock_file_only_skeleton(tmp_path):
    root = tmp_path / "ws"
    root.mkdir()
    (root / "workspace.lock").touch()
    ws = create_workspace(root, PASSWORD)
    assert ws.info.revision == 1


def test_create_workspace_retries_on_empty_structural_dirs(tmp_path):
    root = tmp_path / "ws"
    root.mkdir()
    for name in ("stores", "provenance", "safe"):
        (root / name).mkdir()
    ws = create_workspace(root, PASSWORD)
    assert ws.info.revision == 1
    for name in ("stores", "provenance", "safe", "local_plaintext", "backup"):
        assert (root / name).is_dir()


def test_create_workspace_rejects_workspace_enc_present(tmp_path):
    root = tmp_path / "ws"
    root.mkdir()
    (root / "workspace.enc").write_bytes(b"not empty")
    with pytest.raises(WorkspaceCorruptedError) as excinfo:
        create_workspace(root, PASSWORD)
    assert excinfo.value.reason is WorkspaceCorruptedReason.UNSAFE_LAYOUT


def test_create_workspace_rejects_unknown_file(tmp_path):
    root = tmp_path / "ws"
    root.mkdir()
    (root / "readme.txt").write_text("hello")
    with pytest.raises(WorkspaceCorruptedError) as excinfo:
        create_workspace(root, PASSWORD)
    assert excinfo.value.reason is WorkspaceCorruptedReason.UNSAFE_LAYOUT


def test_create_workspace_rejects_nonempty_structural_dir(tmp_path):
    root = tmp_path / "ws"
    root.mkdir()
    (root / "stores").mkdir()
    (root / "stores" / "leftover.bin").write_bytes(b"x")
    with pytest.raises(WorkspaceCorruptedError) as excinfo:
        create_workspace(root, PASSWORD)
    assert excinfo.value.reason is WorkspaceCorruptedReason.UNSAFE_LAYOUT


def test_create_workspace_rejects_nonzero_size_lock_file(tmp_path):
    root = tmp_path / "ws"
    root.mkdir()
    (root / "workspace.lock").write_bytes(b"x")
    with pytest.raises(WorkspaceCorruptedError) as excinfo:
        create_workspace(root, PASSWORD)
    assert excinfo.value.reason is WorkspaceCorruptedReason.UNSAFE_LAYOUT


def test_create_workspace_rejects_existing_full_workspace_as_target(tmp_path):
    root = tmp_path / "ws"
    create_workspace(root, PASSWORD)
    with pytest.raises(WorkspaceCorruptedError) as excinfo:
        create_workspace(root, PASSWORD)
    assert excinfo.value.reason is WorkspaceCorruptedReason.UNSAFE_LAYOUT


def test_create_workspace_root_as_file_rejected(tmp_path):
    root = tmp_path / "ws"
    root.write_bytes(b"not a directory")
    with pytest.raises(WorkspaceCorruptedError) as excinfo:
        create_workspace(root, PASSWORD)
    assert excinfo.value.reason is WorkspaceCorruptedReason.UNSAFE_LAYOUT


# ---------------------------------------------------------------------------
# D. open_workspace — успешный путь
# ---------------------------------------------------------------------------


def test_open_workspace_matches_created_state(tmp_path):
    root = tmp_path / "ws"
    created = create_workspace(root, PASSWORD, label="lbl")
    opened = open_workspace(root, PASSWORD)
    assert opened.info == created.info
    assert opened.recovery_required is False


def test_open_workspace_accepts_any_nonempty_password_policy(tmp_path):
    # OD-1: open не повторно проверяет creation-политику -- рабочий
    # пример этого требует workspace, созданный с коротким паролем;
    # поскольку create_workspace теперь сама применяет политику,
    # заведомо короткий пароль сюда получить не получится напрямую, но
    # open_workspace обязана обходиться без вызова creation-валидатора
    # (нет ограничения снизу на длину при открытии) -- это же проверяет
    # test_create_workspace_password_no_strip_or_normalization используя
    # длинный пароль. Здесь дополнительно фиксируем: 1-символьный пароль
    # НЕ отклоняется open_workspace на этапе валидации типа/пустоты,
    # только на этапе фактической аутентификации (несовпадение).
    root = tmp_path / "ws"
    create_workspace(root, PASSWORD)
    with pytest.raises(WorkspaceAuthenticationError):
        open_workspace(root, "x")


# ---------------------------------------------------------------------------
# E. open_workspace — карта ошибок
# ---------------------------------------------------------------------------


def test_open_workspace_root_missing(tmp_path):
    with pytest.raises(WorkspaceNotFoundError):
        open_workspace(tmp_path / "does-not-exist", PASSWORD)


def test_open_workspace_lock_file_missing(tmp_path):
    root = tmp_path / "ws"
    create_workspace(root, PASSWORD)
    (root / "workspace.lock").unlink()
    with pytest.raises(WorkspaceCorruptedError) as excinfo:
        open_workspace(root, PASSWORD)
    assert excinfo.value.reason is WorkspaceCorruptedReason.LOCK_FILE_MISSING


def test_open_workspace_manifest_missing(tmp_path):
    root = tmp_path / "ws"
    create_workspace(root, PASSWORD)
    (root / "workspace.enc").unlink()
    with pytest.raises(WorkspaceNotFoundError):
        open_workspace(root, PASSWORD)


def test_open_workspace_wrong_password(tmp_path):
    root = tmp_path / "ws"
    create_workspace(root, PASSWORD)
    with pytest.raises(WorkspaceAuthenticationError):
        open_workspace(root, "totally-different-password")


def test_open_workspace_root_is_file(tmp_path):
    root = tmp_path / "ws"
    root.write_bytes(b"not a directory")
    with pytest.raises(WorkspaceCorruptedError) as excinfo:
        open_workspace(root, PASSWORD)
    assert excinfo.value.reason is WorkspaceCorruptedReason.UNSAFE_LAYOUT


def test_open_workspace_corrupted_manifest_container(tmp_path):
    root = tmp_path / "ws"
    create_workspace(root, PASSWORD)
    (root / "workspace.enc").write_bytes(b"not a valid container")
    with pytest.raises(WorkspaceCorruptedError):
        open_workspace(root, PASSWORD)


def test_open_workspace_empty_password_rejected(tmp_path):
    root = tmp_path / "ws"
    create_workspace(root, PASSWORD)
    with pytest.raises(WorkspaceInputError):
        open_workspace(root, "")


def test_open_workspace_non_str_password_rejected(tmp_path):
    root = tmp_path / "ws"
    create_workspace(root, PASSWORD)
    with pytest.raises(WorkspaceInputError):
        open_workspace(root, None)  # type: ignore[arg-type]


def test_open_workspace_never_mutates_on_failure(tmp_path):
    root = tmp_path / "ws"
    create_workspace(root, PASSWORD)
    before = (root / "workspace.enc").read_bytes()
    with pytest.raises(WorkspaceAuthenticationError):
        open_workspace(root, "wrong-password")
    after = (root / "workspace.enc").read_bytes()
    assert before == after


# ---------------------------------------------------------------------------
# F. Workspace.lock() / nested acquisition
# ---------------------------------------------------------------------------


def test_lock_basic_context_manager(tmp_path):
    root = tmp_path / "ws"
    ws = create_workspace(root, PASSWORD)
    opened = open_workspace(root, PASSWORD)
    with opened.lock():
        pass  # успешно вошли и вышли


def test_lock_nested_acquisition_raises(tmp_path):
    root = tmp_path / "ws"
    create_workspace(root, PASSWORD)
    ws = open_workspace(root, PASSWORD)
    with ws.lock():
        with pytest.raises(WorkspaceLockedError) as excinfo:
            with ws.lock():
                pass
        assert excinfo.value.reason is WorkspaceLockedReason.NESTED_ACQUISITION


def test_lock_nested_via_store_mutation_raises(tmp_path):
    root = tmp_path / "ws"
    create_workspace(root, PASSWORD)
    ws = open_workspace(root, PASSWORD)
    with ws.lock():
        with pytest.raises(WorkspaceLockedError) as excinfo:
            with ws.store_mutation():
                pass
        assert excinfo.value.reason is WorkspaceLockedReason.NESTED_ACQUISITION


def test_lock_released_after_exception_in_body(tmp_path):
    root = tmp_path / "ws"
    create_workspace(root, PASSWORD)
    ws = open_workspace(root, PASSWORD)
    with pytest.raises(ValueError):
        with ws.lock():
            raise ValueError("boom")
    # Лок должен быть освобождён -- повторный acquire не должен зависнуть.
    with ws.lock():
        pass


def test_lock_second_workspace_object_same_process_is_nested(tmp_path):
    # WorkspaceFileLock — process-global registry, ключ = канонический
    # путь: второй объект на тот же путь в том же процессе неотличим от
    # реального nested-случая и корректно даёт NESTED_ACQUISITION, а не
    # HELD_BY_OTHER (тот зарезервирован за настоящим межпроцессным
    # конфликтом) -- см. app/workspace/locking.py docstring.
    root = tmp_path / "ws"
    create_workspace(root, PASSWORD)
    ws_a = open_workspace(root, PASSWORD)
    ws_b = open_workspace(root, PASSWORD)
    with ws_a.lock():
        with pytest.raises(WorkspaceLockedError) as excinfo:
            with ws_b.lock():
                pass
        assert excinfo.value.reason is WorkspaceLockedReason.NESTED_ACQUISITION


# ---------------------------------------------------------------------------
# G. Workspace.info / recovery_required
# ---------------------------------------------------------------------------


def test_info_reflects_root_property(tmp_path):
    root = tmp_path / "ws"
    ws = create_workspace(root, PASSWORD)
    assert ws.root == root


def test_recovery_required_false_by_default(tmp_path):
    ws = create_workspace(tmp_path / "ws", PASSWORD)
    assert ws.recovery_required is False


# ---------------------------------------------------------------------------
# H. Layout safety — детерминированная reparse-point защита
#
# Реальный symlink/junction на Windows обычно требует Developer Mode/прав
# администратора, что сделало бы тест хрупким и непереносимым в CI. Вместо
# этого мы monkeypatch'им единственную стабильную точку классификации,
# которую реализация использует ВЕЗДЕ для решения "это reparse-point?" —
# app.workspace.workspace._is_reparse_like — так, чтобы она возвращала
# True именно для проверяемого пути, не трогая остальную файловую систему.
# Это доказывает, что защитный код-путь реально существует и реально
# приводит к отказу с правильной причиной, а не просто "не упало".
# ---------------------------------------------------------------------------


def _patch_reparse_like_for(monkeypatch, *target_paths):
    import app.workspace.workspace as workspace_module

    original = workspace_module._is_reparse_like
    resolved_targets = {Path(p).resolve() for p in target_paths}

    def _fake(path):
        if Path(path).resolve() in resolved_targets:
            return True
        return original(path)

    monkeypatch.setattr(workspace_module, "_is_reparse_like", _fake)


def test_open_workspace_rejects_reparse_like_root(tmp_path, monkeypatch):
    root = tmp_path / "ws"
    create_workspace(root, PASSWORD)
    _patch_reparse_like_for(monkeypatch, root)

    with pytest.raises(WorkspaceCorruptedError) as excinfo:
        open_workspace(root, PASSWORD)
    assert excinfo.value.reason is WorkspaceCorruptedReason.UNSAFE_LAYOUT


def test_open_workspace_rejects_reparse_like_workspace_enc(tmp_path, monkeypatch):
    root = tmp_path / "ws"
    create_workspace(root, PASSWORD)
    _patch_reparse_like_for(monkeypatch, root / "workspace.enc")

    with pytest.raises(WorkspaceCorruptedError) as excinfo:
        open_workspace(root, PASSWORD)
    assert excinfo.value.reason is WorkspaceCorruptedReason.UNSAFE_LAYOUT


def test_open_workspace_rejects_reparse_like_workspace_lock(tmp_path, monkeypatch):
    root = tmp_path / "ws"
    create_workspace(root, PASSWORD)
    _patch_reparse_like_for(monkeypatch, root / "workspace.lock")

    with pytest.raises(WorkspaceCorruptedError) as excinfo:
        open_workspace(root, PASSWORD)
    assert excinfo.value.reason is WorkspaceCorruptedReason.UNSAFE_LAYOUT


def test_open_workspace_rejects_reparse_like_stores_dir(tmp_path, monkeypatch):
    root = tmp_path / "ws"
    create_workspace(root, PASSWORD)
    _patch_reparse_like_for(monkeypatch, root / "stores")

    with pytest.raises(WorkspaceCorruptedError) as excinfo:
        open_workspace(root, PASSWORD)
    assert excinfo.value.reason is WorkspaceCorruptedReason.UNSAFE_LAYOUT


def test_open_workspace_rejects_reparse_like_mapping_store_file(tmp_path, monkeypatch):
    from app.models.entities import EntityType, MappingEntry

    root = tmp_path / "ws"
    ws = create_workspace(root, PASSWORD)
    with ws.store_mutation() as (mf, idf):
        mf.add(MappingEntry(alias="C_AAAAAAAAA", real_value="R1", entity_type=EntityType.COMPANY, parent_alias=None))
    _patch_reparse_like_for(monkeypatch, root / "stores" / "mapping.enc")

    with pytest.raises(WorkspaceCorruptedError) as excinfo:
        open_workspace(root, PASSWORD)
    assert excinfo.value.reason is WorkspaceCorruptedReason.UNSAFE_LAYOUT


def test_create_workspace_rejects_reparse_like_lock_in_skeleton(tmp_path, monkeypatch):
    # Реальный workspace.lock -- обычный 0-байтный файл (проходит все
    # остальные проверки clean-skeleton), но классифицирован как
    # reparse-like -- create_workspace обязана отказать на этапе
    # _ensure_clean_skeleton_or_absent, не пытаясь его "почистить".
    root = tmp_path / "ws"
    root.mkdir()
    (root / "workspace.lock").touch()
    _patch_reparse_like_for(monkeypatch, root / "workspace.lock")

    with pytest.raises(WorkspaceCorruptedError) as excinfo:
        create_workspace(root, PASSWORD)
    assert excinfo.value.reason is WorkspaceCorruptedReason.UNSAFE_LAYOUT
    # Ничего не создано и не удалено сверх того, что уже было.
    assert (root / "workspace.lock").exists()
    assert not (root / "workspace.enc").exists()


def test_create_workspace_rejects_reparse_like_structural_dir_in_skeleton(tmp_path, monkeypatch):
    root = tmp_path / "ws"
    root.mkdir()
    (root / "stores").mkdir()
    _patch_reparse_like_for(monkeypatch, root / "stores")

    with pytest.raises(WorkspaceCorruptedError) as excinfo:
        create_workspace(root, PASSWORD)
    assert excinfo.value.reason is WorkspaceCorruptedReason.UNSAFE_LAYOUT


def test_create_workspace_rejects_reparse_like_root_itself(tmp_path, monkeypatch):
    root = tmp_path / "ws"
    root.mkdir()
    _patch_reparse_like_for(monkeypatch, root)

    with pytest.raises(WorkspaceCorruptedError) as excinfo:
        create_workspace(root, PASSWORD)
    assert excinfo.value.reason is WorkspaceCorruptedReason.UNSAFE_LAYOUT


def test_reparse_like_patch_does_not_affect_unrelated_paths(tmp_path, monkeypatch):
    # Негативный контроль: патч нацелен только на конкретный путь -- на
    # НЕЗАТРОНУТом workspace с другим корнем create/open по-прежнему
    # работают нормально (доказывает, что предыдущие "reject" тесты
    # действительно проверяют реакцию именно на reparse-подобный путь, а
    # не случайную повсеместную поломку).
    other_root = tmp_path / "unrelated"
    decoy_root = tmp_path / "decoy"
    _patch_reparse_like_for(monkeypatch, decoy_root)

    ws = create_workspace(other_root, PASSWORD)
    assert ws.info.revision == 1
    reopened = open_workspace(other_root, PASSWORD)
    assert reopened.info.revision == 1
