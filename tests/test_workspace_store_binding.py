"""
Тесты Stage 10B.3: диагностика привязки store<->manifest (замороженная
таблица состояний, Contract Review §10/§12) и защита от "typo в пути".

Группы:
    A. CONSISTENT (пусто / N записей)
    B. STORE_MISSING (N>0, файл отсутствует) + доказательство, что
       конструктор store НЕ вызывается
    C. STORE_REGRESSED
    D. STORE_DIVERGED
    E. STORE_UNEXPECTED_ENTRIES (N=0, pending=False, записи есть)
    F. STORE_AHEAD_UNMARKED (N>0, pending=False, записей больше)
    G. AHEAD (pending=True) -- безопасно открывается, не авто-восстанавливается
    H. STORE_UNOPENABLE (испорченные байты store-файла)
"""

from __future__ import annotations

import dataclasses

import pytest

from app.mapping.encrypted_file import EncryptedFileMappingStore
from app.models.entities import EntityType, MappingEntry
from app.workspace import create_workspace, open_workspace
from app.workspace.errors import WorkspaceBindingError, WorkspaceBindingReason
from app.workspace.manifest import mapping_entries_prefix_digest
from app.workspace.models import StoreState
from app.workspace.storage import load_encrypted_manifest, save_encrypted_manifest_atomic

PASSWORD = "test-workspace-password-123"


def _entry(alias: str, real_value: str = None) -> MappingEntry:
    # По умолчанию real_value уникален для alias — иначе add() двух
    # записей с разными alias, но одним и тем же real_value корректно
    # трактуется store как конфликт identity (см. app.mapping.base).
    if real_value is None:
        real_value = f"R_{alias}"
    return MappingEntry(alias=alias, real_value=real_value, entity_type=EntityType.COMPANY, parent_alias=None)


def _manifest_path(root):
    return root / "workspace.enc"


def _mapping_path(root):
    return root / "stores" / "mapping.enc"


def _overwrite_manifest(root, **replacements):
    manifest = load_encrypted_manifest(_manifest_path(root), PASSWORD)
    new_manifest = dataclasses.replace(manifest, **replacements)
    save_encrypted_manifest_atomic(_manifest_path(root), new_manifest, PASSWORD)


# ---------------------------------------------------------------------------
# A. CONSISTENT
# ---------------------------------------------------------------------------


def test_consistent_empty_absent(tmp_path):
    root = tmp_path / "ws"
    create_workspace(root, PASSWORD)
    from app.workspace.models import StoreSyncState

    ws = open_workspace(root, PASSWORD)
    assert ws.mapping_sync.name == "CONSISTENT"
    assert ws.identifier_sync.name == "CONSISTENT"


def test_consistent_with_entries(tmp_path):
    from app.workspace.models import StoreSyncState

    root = tmp_path / "ws"
    ws = create_workspace(root, PASSWORD)
    with ws.store_mutation() as (mf, idf):
        mf.add(_entry("C_AAAAAAAAA"))
        mf.add(_entry("C_BBBBBBBBB"))

    reopened = open_workspace(root, PASSWORD)
    assert reopened.mapping_sync is StoreSyncState.CONSISTENT
    assert reopened.info.mapping_entry_count == 2


# ---------------------------------------------------------------------------
# B. STORE_MISSING
# ---------------------------------------------------------------------------


def test_store_missing_when_manifest_expects_entries(tmp_path):
    root = tmp_path / "ws"
    ws = create_workspace(root, PASSWORD)
    with ws.store_mutation() as (mf, idf):
        mf.add(_entry("C_AAAAAAAAA"))

    _mapping_path(root).unlink()

    with pytest.raises(WorkspaceBindingError) as excinfo:
        open_workspace(root, PASSWORD)
    assert excinfo.value.reason is WorkspaceBindingReason.STORE_MISSING


def test_store_missing_never_constructs_store(tmp_path, monkeypatch):
    """
    Доказательство (не просто "исключение поднялось"): конструктор
    EncryptedFileMappingStore вообще не вызывается для STORE_MISSING —
    это критично, поскольку конструктор на несуществующем пути МОЛЧА даёт
    пустой store (см. docstring workspace.py, Contract Review §21) и
    маскировал бы typo в пути, если бы проверка exists() не шла первой.
    """
    root = tmp_path / "ws"
    ws = create_workspace(root, PASSWORD)
    with ws.store_mutation() as (mf, idf):
        mf.add(_entry("C_AAAAAAAAA"))
    _mapping_path(root).unlink()

    original_init = EncryptedFileMappingStore.__init__

    def _tracking_init(self, *args, **kwargs):
        raise AssertionError("EncryptedFileMappingStore НЕ должен вызываться при STORE_MISSING")

    monkeypatch.setattr(EncryptedFileMappingStore, "__init__", _tracking_init)
    with pytest.raises(WorkspaceBindingError) as excinfo:
        open_workspace(root, PASSWORD)
    assert excinfo.value.reason is WorkspaceBindingReason.STORE_MISSING


def test_store_missing_typo_path_not_silently_empty(tmp_path):
    """
    Негативный контроль: без явной защиты EncryptedFileMappingStore на
    несуществующем пути дал бы пустой store вместо ошибки -- проверяем,
    что это действительно так для сырого класса (документирует РИСК,
    который закрывает _diagnose_store), и что Workspace-слой его не
    воспроизводит.
    """
    root = tmp_path / "ws"
    ws = create_workspace(root, PASSWORD)
    with ws.store_mutation() as (mf, idf):
        mf.add(_entry("C_AAAAAAAAA"))
    _mapping_path(root).unlink()

    raw = EncryptedFileMappingStore(_mapping_path(root), PASSWORD)
    assert raw.entries() == ()  # сырой класс молча "теряет" данные

    with pytest.raises(WorkspaceBindingError):
        open_workspace(root, PASSWORD)  # Workspace-слой обязан это ловить


# ---------------------------------------------------------------------------
# C. STORE_REGRESSED
# ---------------------------------------------------------------------------


def test_store_regressed(tmp_path):
    root = tmp_path / "ws"
    ws = create_workspace(root, PASSWORD)
    with ws.store_mutation() as (mf, idf):
        mf.add(_entry("C_AAAAAAAAA"))

    # Подделываем manifest: якобы записей больше, чем реально есть в store.
    inflated = StoreState(
        entry_count=5,
        prefix_digest=mapping_entries_prefix_digest((_entry("C_AAAAAAAAA"),)),
    )
    _overwrite_manifest(root, mapping_state=inflated)

    with pytest.raises(WorkspaceBindingError) as excinfo:
        open_workspace(root, PASSWORD)
    assert excinfo.value.reason is WorkspaceBindingReason.STORE_REGRESSED


# ---------------------------------------------------------------------------
# D. STORE_DIVERGED
# ---------------------------------------------------------------------------


def test_store_diverged_same_count_wrong_digest(tmp_path):
    root = tmp_path / "ws"
    ws = create_workspace(root, PASSWORD)
    with ws.store_mutation() as (mf, idf):
        mf.add(_entry("C_AAAAAAAAA"))

    wrong_digest_state = StoreState(entry_count=1, prefix_digest="a" * 64)
    _overwrite_manifest(root, mapping_state=wrong_digest_state)

    with pytest.raises(WorkspaceBindingError) as excinfo:
        open_workspace(root, PASSWORD)
    assert excinfo.value.reason is WorkspaceBindingReason.STORE_DIVERGED


def test_store_diverged_more_entries_wrong_prefix(tmp_path):
    root = tmp_path / "ws"
    ws = create_workspace(root, PASSWORD)
    with ws.store_mutation() as (mf, idf):
        mf.add(_entry("C_AAAAAAAAA"))
        mf.add(_entry("C_BBBBBBBBB"))

    # N=1, но digest, записанный в manifest, не совпадает с первым
    # реальным элементом store -- это дивергенция, а не просто "ahead".
    wrong_state = StoreState(entry_count=1, prefix_digest="b" * 64)
    _overwrite_manifest(root, mapping_state=wrong_state)

    with pytest.raises(WorkspaceBindingError) as excinfo:
        open_workspace(root, PASSWORD)
    assert excinfo.value.reason is WorkspaceBindingReason.STORE_DIVERGED


# ---------------------------------------------------------------------------
# E. STORE_UNEXPECTED_ENTRIES
# ---------------------------------------------------------------------------


def test_store_unexpected_entries(tmp_path):
    root = tmp_path / "ws"
    ws = create_workspace(root, PASSWORD)
    with ws.store_mutation() as (mf, idf):
        mf.add(_entry("C_AAAAAAAAA"))

    from app.workspace.manifest import EMPTY_MAPPING_STATE

    # manifest теперь утверждает N=0 (и pending=False), хотя store реально
    # содержит запись.
    _overwrite_manifest(root, mapping_state=EMPTY_MAPPING_STATE, pending_store_mutation=False)

    with pytest.raises(WorkspaceBindingError) as excinfo:
        open_workspace(root, PASSWORD)
    assert excinfo.value.reason is WorkspaceBindingReason.STORE_UNEXPECTED_ENTRIES


# ---------------------------------------------------------------------------
# F. STORE_AHEAD_UNMARKED
# ---------------------------------------------------------------------------


def test_store_ahead_unmarked(tmp_path):
    root = tmp_path / "ws"
    ws = create_workspace(root, PASSWORD)
    with ws.store_mutation() as (mf, idf):
        mf.add(_entry("C_AAAAAAAAA"))
        mf.add(_entry("C_BBBBBBBBB"))

    # manifest теперь "думает", что была только 1 запись (N=1, тот же
    # digest, что и первая запись), pending=False -- но реально их 2.
    one_entry_state = StoreState(
        entry_count=1, prefix_digest=mapping_entries_prefix_digest((_entry("C_AAAAAAAAA"),))
    )
    _overwrite_manifest(root, mapping_state=one_entry_state, pending_store_mutation=False)

    with pytest.raises(WorkspaceBindingError) as excinfo:
        open_workspace(root, PASSWORD)
    assert excinfo.value.reason is WorkspaceBindingReason.STORE_AHEAD_UNMARKED


# ---------------------------------------------------------------------------
# G. AHEAD (pending=True) -- безопасно открывается
# ---------------------------------------------------------------------------


def test_ahead_state_opens_safely_without_auto_recovery(tmp_path):
    from app.workspace.models import StoreSyncState

    root = tmp_path / "ws"
    ws = create_workspace(root, PASSWORD)
    with ws.store_mutation() as (mf, idf):
        mf.add(_entry("C_AAAAAAAAA"))
        mf.add(_entry("C_BBBBBBBBB"))

    # Симулируем прерванную мутацию: manifest помнит только первую запись,
    # но помечен pending=True.
    one_entry_state = StoreState(
        entry_count=1, prefix_digest=mapping_entries_prefix_digest((_entry("C_AAAAAAAAA"),))
    )
    _overwrite_manifest(root, mapping_state=one_entry_state, pending_store_mutation=True)
    revision_before = load_encrypted_manifest(_manifest_path(root), PASSWORD).revision

    opened = open_workspace(root, PASSWORD)
    assert opened.mapping_sync is StoreSyncState.AHEAD
    assert opened.recovery_required is True

    revision_after = load_encrypted_manifest(_manifest_path(root), PASSWORD).revision
    assert revision_after == revision_before  # open_workspace ничего не пишет


# ---------------------------------------------------------------------------
# H. STORE_UNOPENABLE
# ---------------------------------------------------------------------------


def test_store_unopenable_corrupted_bytes(tmp_path):
    root = tmp_path / "ws"
    ws = create_workspace(root, PASSWORD)
    with ws.store_mutation() as (mf, idf):
        mf.add(_entry("C_AAAAAAAAA"))

    _mapping_path(root).write_bytes(b"not a valid encrypted container")

    with pytest.raises(WorkspaceBindingError) as excinfo:
        open_workspace(root, PASSWORD)
    assert excinfo.value.reason is WorkspaceBindingReason.STORE_UNOPENABLE
