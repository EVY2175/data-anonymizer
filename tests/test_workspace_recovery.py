"""
Тесты Stage 10B.3: recover_store_state() -- явное восстановление после
прерванной store_mutation() (НИКОГДА не срабатывает автоматически из
open_workspace).

Группы:
    A. Отказ, когда recovery не требуется
    B. Успешное восстановление (простой AHEAD)
    C. OD-6: смешанное состояние (mapping AHEAD, identifier CONSISTENT)
    D. Отклонение небезопасных состояний при восстановлении
    E. open_workspace никогда не восстанавливает автоматически
    F. Revision semantics (+1)
"""

from __future__ import annotations

import dataclasses

import pytest

from app.models.entities import EntityType, MappingEntry
from app.models.identifiers import IdentifierMappingEntry, IdentifierType
from app.workspace import create_workspace, open_workspace
from app.workspace.errors import WorkspaceBindingError, WorkspaceBindingReason, WorkspaceInputError
from app.workspace.manifest import mapping_entries_prefix_digest
from app.workspace.models import StoreRecoveryResult, StoreState, StoreSyncState
from app.workspace.storage import load_encrypted_manifest, save_encrypted_manifest_atomic

PASSWORD = "test-workspace-password-123"


def _mentry(alias: str, real_value: str = None) -> MappingEntry:
    if real_value is None:
        real_value = f"R_{alias}"
    return MappingEntry(alias=alias, real_value=real_value, entity_type=EntityType.COMPANY, parent_alias=None)


def _ientry(token: str, value: str) -> IdentifierMappingEntry:
    return IdentifierMappingEntry(token=token, identifier_value=value, identifier_type=IdentifierType.INN)


def _manifest_path(root):
    return root / "workspace.enc"


def _interrupt_after_first_add(ws, add_fn):
    """Прерывает store_mutation() после реальной мутации store, оставляя
    manifest в pending=True (имитация краха между шагом 5 и шагом 12)."""
    try:
        with ws.store_mutation() as (mf, idf):
            add_fn(mf, idf)
            raise RuntimeError("simulated crash mid-mutation")
    except RuntimeError:
        pass


# ---------------------------------------------------------------------------
# A. Отказ, когда recovery не требуется
# ---------------------------------------------------------------------------


def test_recover_refuses_when_not_pending(tmp_path):
    root = tmp_path / "ws"
    ws = create_workspace(root, PASSWORD)
    assert ws.recovery_required is False
    with pytest.raises(WorkspaceInputError):
        ws.recover_store_state()


# ---------------------------------------------------------------------------
# B. Успешное восстановление
# ---------------------------------------------------------------------------


def test_recover_adopts_ahead_mapping_state(tmp_path):
    root = tmp_path / "ws"
    ws = create_workspace(root, PASSWORD)
    _interrupt_after_first_add(ws, lambda mf, idf: mf.add(_mentry("C_AAAAAAAAA")))

    assert ws.recovery_required is True
    result = ws.recover_store_state()
    assert isinstance(result, StoreRecoveryResult)
    assert result.mapping_entries_adopted == 1
    assert result.identifier_entries_adopted == 0

    assert ws.recovery_required is False
    assert ws.mapping_sync is StoreSyncState.CONSISTENT
    assert ws.info.mapping_entry_count == 1


def test_recover_adopts_both_stores(tmp_path):
    root = tmp_path / "ws"
    ws = create_workspace(root, PASSWORD)

    def _add_both(mf, idf):
        mf.add(_mentry("C_AAAAAAAAA"))
        idf.add(_ientry("INN_A0000000", "1"))

    _interrupt_after_first_add(ws, _add_both)

    result = ws.recover_store_state()
    assert result.mapping_entries_adopted == 1
    assert result.identifier_entries_adopted == 1
    assert ws.info.mapping_entry_count == 1
    assert ws.info.identifier_entry_count == 1


def test_recover_after_reopen_in_new_workspace_object(tmp_path):
    root = tmp_path / "ws"
    ws = create_workspace(root, PASSWORD)
    _interrupt_after_first_add(ws, lambda mf, idf: mf.add(_mentry("C_AAAAAAAAA")))

    reopened = open_workspace(root, PASSWORD)
    assert reopened.recovery_required is True
    assert reopened.mapping_sync is StoreSyncState.AHEAD

    result = reopened.recover_store_state()
    assert result.mapping_entries_adopted == 1
    assert reopened.recovery_required is False


def test_recover_zero_net_when_conflict_added_nothing(tmp_path):
    # Конфликт данных не изменяет store -- recovery корректно адаптирует
    # "0 новых записей" (см. Stage10B.3 §OD-5/§18 шаг10 обсуждение).
    root = tmp_path / "ws"
    ws = create_workspace(root, PASSWORD)
    with ws.store_mutation() as (mf, idf):
        mf.add(_mentry("C_AAAAAAAAA", real_value="R1"))

    from app.mapping.base import MappingConflictError

    try:
        with ws.store_mutation() as (mf, idf):
            mf.add(_mentry("C_AAAAAAAAA", real_value="CONFLICTING"))
    except MappingConflictError:
        pass

    assert ws.recovery_required is True
    result = ws.recover_store_state()
    assert result.mapping_entries_adopted == 0
    assert ws.info.mapping_entry_count == 1


# ---------------------------------------------------------------------------
# C. OD-6: смешанное состояние
# ---------------------------------------------------------------------------


def test_recover_mixed_mapping_ahead_identifier_consistent(tmp_path):
    root = tmp_path / "ws"
    ws = create_workspace(root, PASSWORD)
    # Устанавливаем стабильную identifier-запись первой мутацией.
    with ws.store_mutation() as (mf, idf):
        idf.add(_ientry("INN_A0000000", "1"))

    # Прерываем ВТОРУЮ мутацию, где мутировался ТОЛЬКО mapping ->
    # identifier остаётся ровно в consistent-состоянии manifest, а
    # mapping реально ahead.
    _interrupt_after_first_add(ws, lambda mf, idf: mf.add(_mentry("C_AAAAAAAAA")))

    assert ws.recovery_required is True
    result = ws.recover_store_state()
    assert result.mapping_entries_adopted == 1
    assert result.identifier_entries_adopted == 0
    assert ws.info.mapping_entry_count == 1
    assert ws.info.identifier_entry_count == 1


def test_recover_mixed_identifier_ahead_mapping_consistent(tmp_path):
    root = tmp_path / "ws"
    ws = create_workspace(root, PASSWORD)
    with ws.store_mutation() as (mf, idf):
        mf.add(_mentry("C_AAAAAAAAA"))

    _interrupt_after_first_add(ws, lambda mf, idf: idf.add(_ientry("INN_A0000000", "1")))

    result = ws.recover_store_state()
    assert result.mapping_entries_adopted == 0
    assert result.identifier_entries_adopted == 1


# ---------------------------------------------------------------------------
# D. Отклонение небезопасных состояний
# ---------------------------------------------------------------------------


def test_recover_rejects_regressed(tmp_path):
    root = tmp_path / "ws"
    ws = create_workspace(root, PASSWORD)
    _interrupt_after_first_add(ws, lambda mf, idf: mf.add(_mentry("C_AAAAAAAAA")))

    manifest = load_encrypted_manifest(_manifest_path(root), PASSWORD)
    inflated = dataclasses.replace(
        manifest, mapping_state=StoreState(entry_count=5, prefix_digest=manifest.mapping_state.prefix_digest)
    )
    save_encrypted_manifest_atomic(_manifest_path(root), inflated, PASSWORD)

    with pytest.raises(WorkspaceBindingError) as excinfo:
        ws.recover_store_state()
    assert excinfo.value.reason is WorkspaceBindingReason.STORE_REGRESSED


def test_recover_rejects_diverged(tmp_path):
    root = tmp_path / "ws"
    ws = create_workspace(root, PASSWORD)
    _interrupt_after_first_add(ws, lambda mf, idf: mf.add(_mentry("C_AAAAAAAAA")))

    manifest = load_encrypted_manifest(_manifest_path(root), PASSWORD)
    corrupted = dataclasses.replace(
        manifest, mapping_state=StoreState(entry_count=0, prefix_digest="c" * 64)
    )
    save_encrypted_manifest_atomic(_manifest_path(root), corrupted, PASSWORD)

    with pytest.raises(WorkspaceBindingError) as excinfo:
        ws.recover_store_state()
    assert excinfo.value.reason is WorkspaceBindingReason.STORE_DIVERGED


def test_recover_rejects_missing_store(tmp_path):
    # STORE_MISSING при diagnosis требует entry_count > 0 в manifest ДО
    # прерванной мутации -- поэтому сперва фиксируем одну запись обычной
    # успешной мутацией, и только затем прерываем вторую.
    root = tmp_path / "ws"
    ws = create_workspace(root, PASSWORD)
    with ws.store_mutation() as (mf, idf):
        mf.add(_mentry("C_AAAAAAAAA"))
    _interrupt_after_first_add(ws, lambda mf, idf: mf.add(_mentry("C_BBBBBBBBB")))
    (root / "stores" / "mapping.enc").unlink()

    with pytest.raises(WorkspaceBindingError) as excinfo:
        ws.recover_store_state()
    assert excinfo.value.reason is WorkspaceBindingReason.STORE_MISSING


def test_recover_rejects_unopenable_store(tmp_path):
    root = tmp_path / "ws"
    ws = create_workspace(root, PASSWORD)
    _interrupt_after_first_add(ws, lambda mf, idf: mf.add(_mentry("C_AAAAAAAAA")))
    (root / "stores" / "mapping.enc").write_bytes(b"corrupted bytes")

    with pytest.raises(WorkspaceBindingError) as excinfo:
        ws.recover_store_state()
    assert excinfo.value.reason is WorkspaceBindingReason.STORE_UNOPENABLE


def test_recover_rejection_does_not_clear_pending(tmp_path):
    root = tmp_path / "ws"
    ws = create_workspace(root, PASSWORD)
    with ws.store_mutation() as (mf, idf):
        mf.add(_mentry("C_AAAAAAAAA"))
    _interrupt_after_first_add(ws, lambda mf, idf: mf.add(_mentry("C_BBBBBBBBB")))
    (root / "stores" / "mapping.enc").unlink()

    with pytest.raises(WorkspaceBindingError):
        ws.recover_store_state()

    manifest = load_encrypted_manifest(_manifest_path(root), PASSWORD)
    assert manifest.pending_store_mutation is True


# ---------------------------------------------------------------------------
# E. open_workspace никогда не восстанавливает автоматически
# ---------------------------------------------------------------------------


def test_open_workspace_does_not_auto_recover(tmp_path):
    root = tmp_path / "ws"
    ws = create_workspace(root, PASSWORD)
    _interrupt_after_first_add(ws, lambda mf, idf: mf.add(_mentry("C_AAAAAAAAA")))

    revision_before = load_encrypted_manifest(_manifest_path(root), PASSWORD).revision
    reopened = open_workspace(root, PASSWORD)
    revision_after = load_encrypted_manifest(_manifest_path(root), PASSWORD).revision

    assert revision_before == revision_after
    assert reopened.recovery_required is True


# ---------------------------------------------------------------------------
# F. Revision semantics
# ---------------------------------------------------------------------------


def test_recover_increments_revision_by_one(tmp_path):
    root = tmp_path / "ws"
    ws = create_workspace(root, PASSWORD)
    _interrupt_after_first_add(ws, lambda mf, idf: mf.add(_mentry("C_AAAAAAAAA")))

    revision_before = load_encrypted_manifest(_manifest_path(root), PASSWORD).revision
    ws.recover_store_state()
    revision_after = load_encrypted_manifest(_manifest_path(root), PASSWORD).revision

    assert revision_after == revision_before + 1
