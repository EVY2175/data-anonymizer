"""
Тесты Stage 10B.3: store_mutation() -- append-only façade, 14-шаговый
протокол, OD-7 санитизация исключений store-слоя.

Группы:
    A. Успешная мутация: revision, счётчики, sync-состояние
    B. Ограниченный façade: нет clear(), инвалидация вне `with`
    C. RECOVERY_REQUIRED -- отказ без побочных эффектов
    D. Тело подняло произвольное исключение -- пробрасывается как есть
    E. Конфликт данных (MappingConflictError/IdentifierMappingConflictError)
    F. Append-only violation (обход façade)
    G. Nested lock
    H. OD-7: конфиденциальность исключений store-слоя + негативный контроль
"""

from __future__ import annotations

import os

import pytest

from app.mapping.base import MappingConflictError
from app.mapping.encrypted_file import EncryptedFileMappingStore
from app.mapping.identifier_base import IdentifierMappingConflictError
from app.models.entities import EntityType, MappingEntry
from app.models.identifiers import IdentifierMappingEntry, IdentifierType
from app.workspace import create_workspace, open_workspace
from app.workspace.errors import (
    WorkspaceBindingError,
    WorkspaceBindingReason,
    WorkspaceInputError,
    WorkspaceLockedError,
    WorkspaceLockedReason,
)
from app.workspace.storage import load_encrypted_manifest

PASSWORD_SENTINEL = "SENTINEL_MUTATION_PASSWORD_4D21"


def _mentry(alias: str, real_value: str = None) -> MappingEntry:
    if real_value is None:
        real_value = f"R_{alias}"
    return MappingEntry(alias=alias, real_value=real_value, entity_type=EntityType.COMPANY, parent_alias=None)


def _ientry(token: str, value: str) -> IdentifierMappingEntry:
    return IdentifierMappingEntry(token=token, identifier_value=value, identifier_type=IdentifierType.INN)


def _manifest_path(root):
    return root / "workspace.enc"


# ---------------------------------------------------------------------------
# A. Успешная мутация
# ---------------------------------------------------------------------------


def test_successful_mutation_revision_plus_two(tmp_path):
    root = tmp_path / "ws"
    ws = create_workspace(root, PASSWORD_SENTINEL)
    assert ws.info.revision == 1

    with ws.store_mutation() as (mf, idf):
        mf.add(_mentry("C_AAAAAAAAA"))
        idf.add(_ientry("INN_A0000000", "1"))

    assert ws.info.revision == 3
    assert ws.info.mapping_entry_count == 1
    assert ws.info.identifier_entry_count == 1

    from app.workspace.models import StoreSyncState

    assert ws.mapping_sync is StoreSyncState.CONSISTENT
    assert ws.identifier_sync is StoreSyncState.CONSISTENT


def test_empty_body_mutation_still_costs_two_revisions(tmp_path):
    root = tmp_path / "ws"
    ws = create_workspace(root, PASSWORD_SENTINEL)
    with ws.store_mutation() as (mf, idf):
        pass
    assert ws.info.revision == 3
    assert ws.info.mapping_entry_count == 0


def test_idempotent_duplicate_add_still_costs_two_revisions(tmp_path):
    root = tmp_path / "ws"
    ws = create_workspace(root, PASSWORD_SENTINEL)
    with ws.store_mutation() as (mf, idf):
        mf.add(_mentry("C_AAAAAAAAA"))
    assert ws.info.revision == 3

    with ws.store_mutation() as (mf, idf):
        mf.add(_mentry("C_AAAAAAAAA"))  # тот же alias/real_value -- no-op
    assert ws.info.revision == 5
    assert ws.info.mapping_entry_count == 1


def test_mutation_persists_across_reopen(tmp_path):
    root = tmp_path / "ws"
    ws = create_workspace(root, PASSWORD_SENTINEL)
    with ws.store_mutation() as (mf, idf):
        mf.add(_mentry("C_AAAAAAAAA"))
        idf.add(_ientry("INN_A0000000", "1"))

    reopened = open_workspace(root, PASSWORD_SENTINEL)
    assert reopened.info.mapping_entry_count == 1
    assert reopened.info.identifier_entry_count == 1
    assert reopened.recovery_required is False


# ---------------------------------------------------------------------------
# B. Ограниченный façade
# ---------------------------------------------------------------------------


def test_mapping_facade_has_no_clear(tmp_path):
    root = tmp_path / "ws"
    ws = create_workspace(root, PASSWORD_SENTINEL)
    with ws.store_mutation() as (mf, idf):
        assert not hasattr(mf, "clear")
        assert not hasattr(idf, "clear")


def test_facade_invalid_after_successful_exit(tmp_path):
    root = tmp_path / "ws"
    ws = create_workspace(root, PASSWORD_SENTINEL)
    with ws.store_mutation() as (mf, idf):
        mf.add(_mentry("C_AAAAAAAAA"))

    with pytest.raises(WorkspaceInputError):
        mf.add(_mentry("C_BBBBBBBBB"))
    with pytest.raises(WorkspaceInputError):
        mf.entries()
    with pytest.raises(WorkspaceInputError):
        idf.add(_ientry("INN_A0000000", "1"))


def test_facade_invalid_after_body_exception(tmp_path):
    root = tmp_path / "ws"
    ws = create_workspace(root, PASSWORD_SENTINEL)
    captured = {}
    try:
        with ws.store_mutation() as (mf, idf):
            captured["mf"] = mf
            captured["idf"] = idf
            raise ValueError("body error")
    except ValueError:
        pass
    with pytest.raises(WorkspaceInputError):
        captured["mf"].add(_mentry("C_AAAAAAAAA"))
    with pytest.raises(WorkspaceInputError):
        captured["idf"].all_tokens()


def test_facade_read_methods_work_while_active(tmp_path):
    root = tmp_path / "ws"
    ws = create_workspace(root, PASSWORD_SENTINEL)
    with ws.store_mutation() as (mf, idf):
        mf.add(_mentry("C_AAAAAAAAA"))
        assert mf.contains_alias("C_AAAAAAAAA") is True
        assert mf.get_by_alias("C_AAAAAAAAA") is not None
        assert "C_AAAAAAAAA" in mf.all_aliases()
        idf.add(_ientry("INN_A0000000", "1"))
        assert idf.get_by_token("INN_A0000000") is not None
        assert "INN_A0000000" in idf.all_tokens()


def test_identifier_facade_add_many(tmp_path):
    root = tmp_path / "ws"
    ws = create_workspace(root, PASSWORD_SENTINEL)
    with ws.store_mutation() as (mf, idf):
        idf.add_many([_ientry("INN_A0000000", "1"), _ientry("INN_B0000000", "2")])
    assert ws.info.identifier_entry_count == 2


# ---------------------------------------------------------------------------
# C. RECOVERY_REQUIRED
# ---------------------------------------------------------------------------


def test_recovery_required_refuses_new_mutation(tmp_path):
    root = tmp_path / "ws"
    ws = create_workspace(root, PASSWORD_SENTINEL)
    try:
        with ws.store_mutation() as (mf, idf):
            mf.add(_mentry("C_AAAAAAAAA"))
            raise ValueError("interrupt")
    except ValueError:
        pass

    assert ws.recovery_required is True
    revision_before = load_encrypted_manifest(_manifest_path(root), PASSWORD_SENTINEL).revision

    with pytest.raises(WorkspaceBindingError) as excinfo:
        with ws.store_mutation() as (mf, idf):
            pass
    assert excinfo.value.reason is WorkspaceBindingReason.RECOVERY_REQUIRED

    revision_after = load_encrypted_manifest(_manifest_path(root), PASSWORD_SENTINEL).revision
    assert revision_after == revision_before  # никакой дополнительной записи


# ---------------------------------------------------------------------------
# D. Тело подняло произвольное исключение
# ---------------------------------------------------------------------------


def test_body_arbitrary_exception_propagates_unchanged(tmp_path):
    root = tmp_path / "ws"
    ws = create_workspace(root, PASSWORD_SENTINEL)

    class _CustomError(Exception):
        pass

    with pytest.raises(_CustomError, match="custom sentinel message"):
        with ws.store_mutation() as (mf, idf):
            raise _CustomError("custom sentinel message")

    assert ws.recovery_required is True


def test_body_exception_leaves_pending_true_on_disk(tmp_path):
    root = tmp_path / "ws"
    ws = create_workspace(root, PASSWORD_SENTINEL)
    try:
        with ws.store_mutation() as (mf, idf):
            raise RuntimeError("boom")
    except RuntimeError:
        pass
    manifest = load_encrypted_manifest(_manifest_path(root), PASSWORD_SENTINEL)
    assert manifest.pending_store_mutation is True
    assert manifest.revision == 2  # только шаг 5 (pending=True) состоялся


# ---------------------------------------------------------------------------
# E. Конфликт данных
# ---------------------------------------------------------------------------


def test_mapping_conflict_propagates_as_mapping_conflict_error(tmp_path):
    root = tmp_path / "ws"
    ws = create_workspace(root, PASSWORD_SENTINEL)
    with ws.store_mutation() as (mf, idf):
        mf.add(_mentry("C_AAAAAAAAA", real_value="R1"))

    with pytest.raises(MappingConflictError):
        with ws.store_mutation() as (mf, idf):
            mf.add(_mentry("C_AAAAAAAAA", real_value="DIFFERENT"))

    # По протоколу (§18 шаг 10) ЛЮБОЕ исключение тела -- включая конфликт
    # данных -- оставляет pending=True, требуя явного recovery.
    assert ws.recovery_required is True


def test_identifier_conflict_propagates_as_identifier_conflict_error(tmp_path):
    root = tmp_path / "ws"
    ws = create_workspace(root, PASSWORD_SENTINEL)
    with ws.store_mutation() as (mf, idf):
        idf.add(_ientry("INN_A0000000", "1"))

    with pytest.raises(IdentifierMappingConflictError):
        with ws.store_mutation() as (mf, idf):
            idf.add(_ientry("INN_A0000000", "2"))  # тот же token, другая identity


def test_identifier_conflict_in_add_many_rolls_back_batch(tmp_path):
    root = tmp_path / "ws"
    ws = create_workspace(root, PASSWORD_SENTINEL)
    with ws.store_mutation() as (mf, idf):
        idf.add(_ientry("INN_A0000000", "1"))

    with pytest.raises(IdentifierMappingConflictError):
        with ws.store_mutation() as (mf, idf):
            idf.add_many([_ientry("INN_B0000000", "2"), _ientry("INN_A0000000", "999")])


# ---------------------------------------------------------------------------
# F. Append-only violation
# ---------------------------------------------------------------------------


def test_append_only_violation_detected_on_bypass_clear(tmp_path):
    root = tmp_path / "ws"
    ws = create_workspace(root, PASSWORD_SENTINEL)
    with ws.store_mutation() as (mf, idf):
        mf.add(_mentry("C_AAAAAAAAA"))

    with pytest.raises(WorkspaceBindingError) as excinfo:
        with ws.store_mutation() as (mf, idf):
            mf.add(_mentry("C_BBBBBBBBB"))
            # Намеренный обход façade через приватный атрибут (вне threat
            # model, но append-only проверка на коммите обязана поймать
            # порчу старого префикса).
            mf._store.clear()
    assert excinfo.value.reason is WorkspaceBindingReason.APPEND_ONLY_VIOLATION
    assert ws.recovery_required is True


def test_append_only_violation_shrink_detected(tmp_path):
    root = tmp_path / "ws"
    ws = create_workspace(root, PASSWORD_SENTINEL)
    with ws.store_mutation() as (mf, idf):
        mf.add(_mentry("C_AAAAAAAAA"))
        mf.add(_mentry("C_BBBBBBBBB"))

    with pytest.raises(WorkspaceBindingError) as excinfo:
        with ws.store_mutation() as (mf, idf):
            mf._store.clear()
            mf._store.add(_mentry("C_CCCCCCCCC"))
    assert excinfo.value.reason is WorkspaceBindingReason.APPEND_ONLY_VIOLATION


# ---------------------------------------------------------------------------
# G. Nested lock
# ---------------------------------------------------------------------------


def test_nested_store_mutation_raises(tmp_path):
    root = tmp_path / "ws"
    ws = create_workspace(root, PASSWORD_SENTINEL)
    with ws.store_mutation() as (mf, idf):
        with pytest.raises(WorkspaceLockedError) as excinfo:
            with ws.store_mutation() as (mf2, idf2):
                pass
        assert excinfo.value.reason is WorkspaceLockedReason.NESTED_ACQUISITION


# ---------------------------------------------------------------------------
# H. OD-7: конфиденциальность исключений store-слоя
# ---------------------------------------------------------------------------

_INTERNAL_FILENAME_SUFFIXES = (os.path.join("app", "workspace", "workspace.py"),)


def _internal_frame_locals_containing(exc: BaseException, sentinels: tuple) -> list:
    """
    Тот же приём, что закрыл MAJOR-1 в Stage 10B.2 (см.
    tests/test_workspace_storage.py, группа H): обходит traceback exc и
    всей цепочки __context__/__cause__, засчитывая ТОЛЬКО фреймы
    app/workspace/workspace.py (фрейм самого теста неизбежно содержит
    sentinel — это не утечка реализации, а область видимости теста).
    """
    findings: list = []
    visited_ids: set = set()

    def scan_traceback(tb, owner_label: str) -> None:
        idx = 0
        while tb is not None:
            filename = tb.tb_frame.f_code.co_filename
            if filename.endswith(_INTERNAL_FILENAME_SUFFIXES):
                for name, value in tb.tb_frame.f_locals.items():
                    if isinstance(value, str):
                        text = value
                    elif isinstance(value, (bytes, bytearray)):
                        text = value.decode("utf-8", errors="replace")
                    else:
                        text = str(value)
                    for sentinel in sentinels:
                        if sentinel in text:
                            findings.append((owner_label, idx, os.path.basename(filename), tb.tb_frame.f_code.co_name, name))
            tb = tb.tb_next
            idx += 1

    def walk(current: BaseException, owner_label: str, depth: int = 0) -> None:
        if current is None or id(current) in visited_ids or depth > 10:
            return
        visited_ids.add(id(current))
        scan_traceback(current.__traceback__, owner_label)
        walk(current.__context__, owner_label + ".__context__", depth + 1)
        walk(current.__cause__, owner_label + ".__cause__", depth + 1)

    walk(exc, "exc")
    return findings


def test_scanner_would_catch_a_real_regression():
    def _frame_with_uncleared_secret(secret: str) -> None:
        raise ValueError("synthetic leak")

    try:
        _frame_with_uncleared_secret(PASSWORD_SENTINEL)
    except ValueError as synthetic_exc:
        global _INTERNAL_FILENAME_SUFFIXES
        original = _INTERNAL_FILENAME_SUFFIXES
        _INTERNAL_FILENAME_SUFFIXES = original + (os.path.basename(__file__),)
        try:
            findings = _internal_frame_locals_containing(synthetic_exc, (PASSWORD_SENTINEL,))
        finally:
            _INTERNAL_FILENAME_SUFFIXES = original
        assert findings != []


def test_od7_store_missing_no_password_leak(tmp_path):
    root = tmp_path / "ws"
    ws = create_workspace(root, PASSWORD_SENTINEL)
    with ws.store_mutation() as (mf, idf):
        mf.add(_mentry("C_AAAAAAAAA"))
    (root / "stores" / "mapping.enc").unlink()

    with pytest.raises(WorkspaceBindingError) as excinfo:
        open_workspace(root, PASSWORD_SENTINEL)
    assert _internal_frame_locals_containing(excinfo.value, (PASSWORD_SENTINEL,)) == []


def test_od7_wrong_password_no_leak(tmp_path):
    root = tmp_path / "ws"
    create_workspace(root, PASSWORD_SENTINEL)
    from app.workspace.errors import WorkspaceAuthenticationError

    with pytest.raises(WorkspaceAuthenticationError) as excinfo:
        open_workspace(root, "TOTALLY-WRONG-" + PASSWORD_SENTINEL)
    assert _internal_frame_locals_containing(excinfo.value, (PASSWORD_SENTINEL,)) == []


def test_od7_mapping_conflict_no_password_leak(tmp_path):
    root = tmp_path / "ws"
    ws = create_workspace(root, PASSWORD_SENTINEL)
    with ws.store_mutation() as (mf, idf):
        mf.add(_mentry("C_AAAAAAAAA", real_value="R1"))

    with pytest.raises(MappingConflictError) as excinfo:
        with ws.store_mutation() as (mf, idf):
            mf.add(_mentry("C_AAAAAAAAA", real_value="R_DIFFERENT"))
    assert _internal_frame_locals_containing(excinfo.value, (PASSWORD_SENTINEL,)) == []


def test_od7_identifier_conflict_no_password_leak(tmp_path):
    root = tmp_path / "ws"
    ws = create_workspace(root, PASSWORD_SENTINEL)
    with ws.store_mutation() as (mf, idf):
        idf.add(_ientry("INN_A0000000", "1"))

    with pytest.raises(IdentifierMappingConflictError) as excinfo:
        with ws.store_mutation() as (mf, idf):
            idf.add(_ientry("INN_A0000000", "999"))
    assert _internal_frame_locals_containing(excinfo.value, (PASSWORD_SENTINEL,)) == []


def test_od7_append_only_violation_no_password_leak(tmp_path):
    root = tmp_path / "ws"
    ws = create_workspace(root, PASSWORD_SENTINEL)
    with ws.store_mutation() as (mf, idf):
        mf.add(_mentry("C_AAAAAAAAA"))

    with pytest.raises(WorkspaceBindingError) as excinfo:
        with ws.store_mutation() as (mf, idf):
            mf._store.clear()
    assert _internal_frame_locals_containing(excinfo.value, (PASSWORD_SENTINEL,)) == []


def test_od7_construction_raises_forces_sanitized_binding_error(tmp_path, monkeypatch):
    """
    Инструментируем EncryptedFileMappingStore.__init__ так, чтобы он
    поднимал исключение, несущее self (с _password) в своём собственном
    traceback-фрейме -- ровно тот сценарий, который OD-7 обязан
    санитизировать при открытии store внутри store_mutation().
    """
    root = tmp_path / "ws"
    ws = create_workspace(root, PASSWORD_SENTINEL)
    with ws.store_mutation() as (mf, idf):
        mf.add(_mentry("C_AAAAAAAAA"))

    original_init = EncryptedFileMappingStore.__init__

    def _failing_init(self, path, password):
        original_init(self, path, password)
        raise RuntimeError("synthetic store-layer failure carrying self/password")

    monkeypatch.setattr(EncryptedFileMappingStore, "__init__", _failing_init)
    with pytest.raises(WorkspaceBindingError) as excinfo:
        with ws.store_mutation() as (mf, idf):
            pass
    assert excinfo.value.reason is WorkspaceBindingReason.STORE_UNOPENABLE
    assert _internal_frame_locals_containing(excinfo.value, (PASSWORD_SENTINEL,)) == []
