"""
Тесты Stage 10B.4.3: snapshot_stores / verify_backup / list_backups /
Workspace.verify.

Переиспользует helper'ы Stage 10B.4.1/10B.4.2 из tests.test_workspace_artifacts
и tests.test_workspace_artifact_access напрямую импортом.

Группы:
    A. snapshot_stores — happy path / формат / zero-store
    B. snapshot_stores — validation / collision
    C. snapshot_stores — failure atomicity
    D. retention
    E. verify_backup — success / structural invalid / foreign
    F. list_backups
    G. Workspace.verify — clean / store diagnostics / pending
    H. Workspace.verify — artifact/provenance verification
    I. Workspace.verify — orphan semantics
    J. Workspace.verify — layout semantics
    K. Security boundaries / OD-7
"""

from __future__ import annotations

import dataclasses
import os
import shutil

import pytest

from app.workspace import create_workspace, open_workspace
from app.workspace.errors import (
    WorkspaceBindingError,
    WorkspaceBindingReason,
    WorkspaceCorruptedError,
    WorkspaceCorruptedReason,
    WorkspaceInputError,
)
from app.workspace.models import BackupInfo, StoreSyncState, WorkspaceVerification
from tests.test_workspace_artifacts import (
    PASSWORD,
    _internal_frame_locals_containing,
    _make_provenance,
    _register_monthly,
)
from tests.test_workspace_artifact_access import (
    _register_canonical,
    _register_local_restored,
)

_PASSWORD_SENTINEL_10B4_3 = "SENTINEL_10B4_3_PASSWORD_R4T7"


def _backup_dir(root, name):
    return root / "backup" / name


# ---------------------------------------------------------------------------
# A. snapshot_stores — happy path / формат / zero-store
# ---------------------------------------------------------------------------


def test_snapshot_zero_store_happy_path(tmp_path):
    root = tmp_path / "ws"
    ws = create_workspace(root, PASSWORD)
    info = ws.snapshot_stores()
    assert isinstance(info, BackupInfo)
    assert info.revision == 1
    assert info.mapping_entry_count == 0
    assert info.identifier_entry_count == 0

    backup_dir = _backup_dir(root, info.name)
    assert (backup_dir / "workspace.enc").exists()
    assert not (backup_dir / "stores").exists()


def test_snapshot_name_format(tmp_path):
    root = tmp_path / "ws"
    ws = create_workspace(root, PASSWORD)
    info = ws.snapshot_stores()
    prefix, _, suffix = info.name.partition("-")
    assert len(prefix) == 8 and prefix.isdigit()
    assert len(suffix) == 8 and all(ch in "0123456789abcdef" for ch in suffix)
    assert int(prefix) == info.revision


def test_snapshot_revision_plus_zero(tmp_path):
    root = tmp_path / "ws"
    ws = create_workspace(root, PASSWORD)
    revision_before = ws.info.revision
    raw_before = (root / "workspace.enc").read_bytes()
    ws.snapshot_stores()
    assert ws.info.revision == revision_before
    assert (root / "workspace.enc").read_bytes() == raw_before


def test_snapshot_manifest_bytes_exact_copy(tmp_path):
    root = tmp_path / "ws"
    ws = create_workspace(root, PASSWORD)
    _register_monthly(ws, root)
    raw_before = (root / "workspace.enc").read_bytes()
    info = ws.snapshot_stores()
    backup_manifest_bytes = (_backup_dir(root, info.name) / "workspace.enc").read_bytes()
    assert backup_manifest_bytes == raw_before


def test_snapshot_mapping_only(tmp_path):
    root = tmp_path / "ws"
    ws = create_workspace(root, PASSWORD)
    from app.models.entities import EntityType, MappingEntry

    with ws.store_mutation() as (mapping, _identifiers):
        mapping.add(MappingEntry(alias="alias-1", real_value="real-1", entity_type=EntityType.PERSON))

    info = ws.snapshot_stores()
    backup_dir = _backup_dir(root, info.name)
    assert info.mapping_entry_count == 1
    assert info.identifier_entry_count == 0
    assert (backup_dir / "stores" / "mapping.enc").exists()
    assert not (backup_dir / "stores" / "identifiers.enc").exists()
    assert (backup_dir / "stores" / "mapping.enc").read_bytes() == (root / "stores" / "mapping.enc").read_bytes()


def test_snapshot_identifier_only(tmp_path):
    root = tmp_path / "ws"
    ws = create_workspace(root, PASSWORD)
    from app.models.identifiers import IdentifierMappingEntry, IdentifierType

    with ws.store_mutation() as (_mapping, identifiers):
        identifiers.add(IdentifierMappingEntry(token="token-1", identifier_type=IdentifierType.INN, identifier_value="1234567890"))

    info = ws.snapshot_stores()
    backup_dir = _backup_dir(root, info.name)
    assert info.mapping_entry_count == 0
    assert info.identifier_entry_count == 1
    assert not (backup_dir / "stores" / "mapping.enc").exists()
    assert (backup_dir / "stores" / "identifiers.enc").exists()


def test_snapshot_both_stores(tmp_path):
    root = tmp_path / "ws"
    ws = create_workspace(root, PASSWORD)
    from app.models.entities import EntityType, MappingEntry
    from app.models.identifiers import IdentifierMappingEntry, IdentifierType

    with ws.store_mutation() as (mapping, identifiers):
        mapping.add(MappingEntry(alias="alias-1", real_value="real-1", entity_type=EntityType.PERSON))
        identifiers.add(IdentifierMappingEntry(token="token-1", identifier_type=IdentifierType.INN, identifier_value="1234567890"))

    info = ws.snapshot_stores()
    backup_dir = _backup_dir(root, info.name)
    assert (backup_dir / "stores" / "mapping.enc").exists()
    assert (backup_dir / "stores" / "identifiers.enc").exists()
    assert info.mapping_entry_count == 1
    assert info.identifier_entry_count == 1


def test_snapshot_rejects_pending_store_mutation(tmp_path):
    root = tmp_path / "ws"
    ws = create_workspace(root, PASSWORD)

    import app.workspace.workspace as workspace_module

    manifest = workspace_module.load_encrypted_manifest(root / "workspace.enc", PASSWORD)
    pending_manifest = dataclasses.replace(manifest, revision=manifest.revision + 1, pending_store_mutation=True)
    workspace_module.save_encrypted_manifest_atomic(root / "workspace.enc", pending_manifest, PASSWORD)

    ws2 = open_workspace(root, PASSWORD)
    with pytest.raises(WorkspaceBindingError) as excinfo:
        ws2.snapshot_stores()
    assert excinfo.value.reason is WorkspaceBindingReason.RECOVERY_REQUIRED


# ---------------------------------------------------------------------------
# B. snapshot_stores — validation / collision
# ---------------------------------------------------------------------------


def test_snapshot_keep_last_validation(tmp_path):
    ws = create_workspace(tmp_path / "ws", PASSWORD)
    for bad in (0, -1, True, "5", 1.5):
        with pytest.raises(WorkspaceInputError):
            ws.snapshot_stores(keep_last=bad)  # type: ignore[arg-type]


def test_snapshot_collision_retry(tmp_path, monkeypatch):
    root = tmp_path / "ws"
    ws = create_workspace(root, PASSWORD)

    import app.workspace.workspace as workspace_module

    real_token_hex = workspace_module.secrets.token_hex
    forced = "d" * 8
    occupied = _backup_dir(root, f"00000001-{forced}")
    occupied.mkdir(parents=True)
    (occupied / "workspace.enc").write_bytes(b"occupied")

    call_count = {"n": 0}

    def fake_token_hex(n):
        call_count["n"] += 1
        if call_count["n"] == 1:
            return forced
        return real_token_hex(n)

    monkeypatch.setattr(workspace_module.secrets, "token_hex", fake_token_hex)
    info = ws.snapshot_stores()
    assert info.name != f"00000001-{forced}"
    assert call_count["n"] >= 2


def test_snapshot_same_revision_multiple_backups_allowed(tmp_path):
    root = tmp_path / "ws"
    ws = create_workspace(root, PASSWORD)
    info1 = ws.snapshot_stores(keep_last=5)
    info2 = ws.snapshot_stores(keep_last=5)
    assert info1.revision == info2.revision == 1
    assert info1.name != info2.name
    assert _backup_dir(root, info1.name).exists()
    assert _backup_dir(root, info2.name).exists()


# ---------------------------------------------------------------------------
# C. snapshot_stores — failure atomicity
# ---------------------------------------------------------------------------


def test_snapshot_temp_mkdir_failure_leaves_no_backup(tmp_path, monkeypatch):
    root = tmp_path / "ws"
    ws = create_workspace(root, PASSWORD)

    import app.workspace.workspace as workspace_module

    def failing_mkdtemp(*args, **kwargs):
        raise OSError("simulated mkdtemp failure")

    monkeypatch.setattr(workspace_module.tempfile, "mkdtemp", failing_mkdtemp)
    before = set((root / "backup").iterdir())
    with pytest.raises(OSError):
        ws.snapshot_stores()
    after = set((root / "backup").iterdir())
    assert before == after


def test_snapshot_workspace_copy_failure_no_partial_backup(tmp_path, monkeypatch):
    root = tmp_path / "ws"
    ws = create_workspace(root, PASSWORD)

    import app.workspace.workspace as workspace_module

    def failing_copy(source, destination):
        raise OSError("simulated copy failure")

    monkeypatch.setattr(workspace_module, "_atomic_copy_file", failing_copy)
    with pytest.raises(OSError):
        ws.snapshot_stores()

    remaining = list((root / "backup").iterdir())
    assert remaining == []  # temp cleaned up, no valid-looking backup published


def test_snapshot_store_copy_failure_no_partial_backup(tmp_path, monkeypatch):
    root = tmp_path / "ws"
    ws = create_workspace(root, PASSWORD)
    from app.models.entities import EntityType, MappingEntry

    with ws.store_mutation() as (mapping, _identifiers):
        mapping.add(MappingEntry(alias="alias-1", real_value="real-1", entity_type=EntityType.PERSON))

    import app.workspace.workspace as workspace_module

    real_copy = workspace_module._atomic_copy_file
    call_count = {"n": 0}

    def flaky_copy(source, destination):
        call_count["n"] += 1
        if call_count["n"] == 2:  # workspace.enc succeeds, mapping.enc fails
            raise OSError("simulated store copy failure")
        return real_copy(source, destination)

    monkeypatch.setattr(workspace_module, "_atomic_copy_file", flaky_copy)
    with pytest.raises(OSError):
        ws.snapshot_stores()

    remaining = [p for p in (root / "backup").iterdir() if not p.name.startswith(".")]
    assert remaining == []


def test_snapshot_fsync_failure_no_partial_backup(tmp_path, monkeypatch):
    root = tmp_path / "ws"
    ws = create_workspace(root, PASSWORD)

    import app.workspace.workspace as workspace_module

    def failing_fsync(fd):
        raise OSError("simulated fsync failure")

    monkeypatch.setattr(workspace_module.os, "fsync", failing_fsync)
    with pytest.raises(OSError):
        ws.snapshot_stores()

    remaining = [p for p in (root / "backup").iterdir() if not p.name.startswith(".")]
    assert remaining == []


def test_snapshot_publish_failure_no_partial_backup(tmp_path, monkeypatch):
    root = tmp_path / "ws"
    ws = create_workspace(root, PASSWORD)

    import app.workspace.workspace as workspace_module

    real_replace = os.replace

    def failing_replace(src, dst):
        if os.path.isdir(src):
            raise OSError("simulated directory publish failure")
        return real_replace(src, dst)

    monkeypatch.setattr(workspace_module.os, "replace", failing_replace)
    with pytest.raises(OSError):
        ws.snapshot_stores()

    remaining = [p for p in (root / "backup").iterdir() if not p.name.startswith(".")]
    assert remaining == []


def test_snapshot_lock_released_after_failure_and_retry_succeeds(tmp_path, monkeypatch):
    root = tmp_path / "ws"
    ws = create_workspace(root, PASSWORD)

    import app.workspace.workspace as workspace_module

    def failing_copy(source, destination):
        raise OSError("simulated copy failure")

    monkeypatch.setattr(workspace_module, "_atomic_copy_file", failing_copy)
    with pytest.raises(OSError):
        ws.snapshot_stores()
    monkeypatch.undo()

    info = ws.snapshot_stores()
    assert isinstance(info, BackupInfo)


def test_snapshot_manifest_unchanged_after_failure(tmp_path, monkeypatch):
    root = tmp_path / "ws"
    ws = create_workspace(root, PASSWORD)
    revision_before = ws.info.revision
    raw_before = (root / "workspace.enc").read_bytes()

    import app.workspace.workspace as workspace_module

    def failing_copy(source, destination):
        raise OSError("simulated copy failure")

    monkeypatch.setattr(workspace_module, "_atomic_copy_file", failing_copy)
    with pytest.raises(OSError):
        ws.snapshot_stores()

    assert ws.info.revision == revision_before
    assert (root / "workspace.enc").read_bytes() == raw_before


# ---------------------------------------------------------------------------
# D. retention
# ---------------------------------------------------------------------------


def test_retention_default_keep_last_five(tmp_path):
    # OD-10B4.3-5: сортировка retention -- по (revision, name), НЕ по
    # порядку создания (все backup здесь одной revision) -- поэтому
    # ожидаемое множество вычисляется через сортировку имён, а не через
    # срез infos по хронологии создания.
    root = tmp_path / "ws"
    ws = create_workspace(root, PASSWORD)
    infos = [ws.snapshot_stores() for _ in range(7)]
    remaining = {p.name for p in (root / "backup").iterdir()}
    expected = set(sorted(info.name for info in infos)[-5:])
    assert remaining == expected


def test_retention_keep_last_one(tmp_path):
    root = tmp_path / "ws"
    ws = create_workspace(root, PASSWORD)
    infos = [ws.snapshot_stores(keep_last=1) for _ in range(3)]
    remaining = {p.name for p in (root / "backup").iterdir()}
    expected = {sorted(info.name for info in infos)[-1]}
    assert remaining == expected


def test_retention_keep_last_greater_than_existing(tmp_path):
    root = tmp_path / "ws"
    ws = create_workspace(root, PASSWORD)
    infos = [ws.snapshot_stores(keep_last=10) for _ in range(2)]
    remaining = {p.name for p in (root / "backup").iterdir()}
    assert remaining == {info.name for info in infos}


def test_retention_does_not_delete_foreign_backup(tmp_path):
    root = tmp_path / "ws"
    ws = create_workspace(root, PASSWORD)

    foreign_root = tmp_path / "foreign_ws"
    foreign_ws = create_workspace(foreign_root, PASSWORD)
    foreign_info = foreign_ws.snapshot_stores()
    foreign_backup_src = _backup_dir(foreign_root, foreign_info.name)
    foreign_backup_dst = _backup_dir(root, foreign_info.name)
    shutil.copytree(foreign_backup_src, foreign_backup_dst)

    for _ in range(6):
        ws.snapshot_stores(keep_last=1)

    assert foreign_backup_dst.exists()  # foreign never a deletion candidate


def test_retention_does_not_delete_invalid_name_entry(tmp_path):
    root = tmp_path / "ws"
    ws = create_workspace(root, PASSWORD)
    weird = root / "backup" / "not-a-valid-backup-name"
    weird.mkdir()
    (weird / "junk.txt").write_bytes(b"junk")

    for _ in range(6):
        ws.snapshot_stores(keep_last=1)

    assert weird.exists()


def test_retention_does_not_delete_temp_crash_entry(tmp_path):
    root = tmp_path / "ws"
    ws = create_workspace(root, PASSWORD)
    crash_temp = root / "backup" / ".snapshot-abcd1234.tmp"
    crash_temp.mkdir()

    for _ in range(6):
        ws.snapshot_stores(keep_last=1)

    assert crash_temp.exists()


def test_retention_deterministic_ordering_same_revision(tmp_path):
    root = tmp_path / "ws"
    ws = create_workspace(root, PASSWORD)
    infos = [ws.snapshot_stores(keep_last=10) for _ in range(4)]
    assert len({info.revision for info in infos}) == 1  # все revision==1
    last_info = ws.snapshot_stores(keep_last=2)  # 5-й snapshot, затем retention keep_last=2

    all_names = sorted(info.name for info in infos) + [last_info.name]
    expected_kept = set(sorted(all_names)[-2:])  # по (revision, name): последние 2 по имени
    remaining = {p.name for p in (root / "backup").iterdir()}
    assert remaining == expected_kept



def test_retention_partial_deletion_failure_keeps_new_and_raises(tmp_path, monkeypatch):
    """
    Correction Pass (обнаружено вживую при full regression MINOR-4/5
    Final Correction: тот же класс ~25%-flaky дефекта, что был исправлен
    в test_od7_retention_deletion_failure_no_password_leak_via_public_api,
    присутствовал и здесь -- failing_path был жёстко привязан к
    old_infos[0] без учёта случайной (revision, name)-сортировки при
    keep_last=1 среди 4 кандидатов одной revision). Исправлено тем же
    надёжным паттерном: инъекция срабатывает на ПЕРВОЙ попытке удалить
    ЛЮБОЙ backup с валидным именем, а keep_last выставлен равным числу
    уже существующих backup, поэтому retention планирует РОВНО ОДНО
    удаление -- гарантированно перехватываемое независимо от того, какое
    случайное имя оказалось выбрано сортировкой.
    """
    root = tmp_path / "ws"
    ws = create_workspace(root, PASSWORD)
    old_infos = [ws.snapshot_stores(keep_last=10) for _ in range(3)]

    import app.workspace.workspace as workspace_module

    before_names = {
        p.name for p in (root / "backup").iterdir() if workspace_module._is_valid_backup_name(p.name)
    }

    real_rmtree = shutil.rmtree
    injected_failure = {"done": False}

    def flaky_rmtree(path, *args, **kwargs):
        name = os.path.basename(str(path))
        if workspace_module._is_valid_backup_name(name) and not injected_failure["done"]:
            injected_failure["done"] = True
            raise OSError("simulated retention delete failure")
        return real_rmtree(path, *args, **kwargs)

    monkeypatch.setattr(workspace_module.shutil, "rmtree", flaky_rmtree)

    with pytest.raises(OSError):
        ws.snapshot_stores(keep_last=len(old_infos))

    # Новый snapshot остаётся; единственная (провалившаяся) попытка
    # удаления означает, что НИ ОДИН из существовавших backup фактически
    # не удалён.
    remaining = {
        p.name for p in (root / "backup").iterdir() if workspace_module._is_valid_backup_name(p.name)
    }
    assert before_names <= remaining
    assert len(remaining - before_names) == 1


def test_retention_failure_already_deleted_stay_deleted(tmp_path, monkeypatch):
    root = tmp_path / "ws"
    ws = create_workspace(root, PASSWORD)
    old_infos = [ws.snapshot_stores(keep_last=10) for _ in range(4)]
    # OD-10B4.3-5: retention сортирует по (revision, name) -- вычисляем
    # порядок удаления по имени, а не по порядку создания.
    sorted_old = sorted(old_infos, key=lambda info: info.name)

    import app.workspace.workspace as workspace_module

    real_rmtree = shutil.rmtree
    # sorted_old[1] (второй по имени среди подлежащих удалению) -- сбой удаления.
    failing_path = str(_backup_dir(root, sorted_old[1].name))

    def flaky_rmtree(path, *args, **kwargs):
        if str(path) == failing_path:
            raise OSError("simulated retention delete failure")
        return real_rmtree(path, *args, **kwargs)

    monkeypatch.setattr(workspace_module.shutil, "rmtree", flaky_rmtree)

    with pytest.raises(OSError):
        ws.snapshot_stores(keep_last=1)

    remaining = {p.name for p in (root / "backup").iterdir()}
    assert sorted_old[0].name not in remaining  # удалён раньше сбойного -- успешно
    assert sorted_old[1].name in remaining  # не удалось удалить -- остался


# ---------------------------------------------------------------------------
# E. verify_backup — success / structural invalid / foreign
# ---------------------------------------------------------------------------


def test_verify_backup_success_zero_store(tmp_path):
    root = tmp_path / "ws"
    ws = create_workspace(root, PASSWORD)
    info = ws.snapshot_stores()
    result = ws.verify_backup(info.name)
    assert result == info


def test_verify_backup_success_with_stores(tmp_path):
    root = tmp_path / "ws"
    ws = create_workspace(root, PASSWORD)
    from app.models.entities import EntityType, MappingEntry

    with ws.store_mutation() as (mapping, _identifiers):
        mapping.add(MappingEntry(alias="alias-1", real_value="real-1", entity_type=EntityType.PERSON))
    info = ws.snapshot_stores()
    result = ws.verify_backup(info.name)
    assert result == info


def test_verify_backup_rejects_non_str_and_path(tmp_path):
    ws = create_workspace(tmp_path / "ws", PASSWORD)
    with pytest.raises(WorkspaceInputError):
        ws.verify_backup(12345)  # type: ignore[arg-type]
    with pytest.raises(WorkspaceInputError):
        ws.verify_backup(tmp_path)  # type: ignore[arg-type]


def test_verify_backup_rejects_malformed_name_and_traversal(tmp_path):
    ws = create_workspace(tmp_path / "ws", PASSWORD)
    for bad in ("not-a-backup", "0000001-abcdef01", "00000001-ABCDEF01", "../00000001-abcdef01", "00000001-abcdef01/../x"):
        with pytest.raises(WorkspaceInputError):
            ws.verify_backup(bad)


def test_verify_backup_unknown_name_is_invalid(tmp_path):
    ws = create_workspace(tmp_path / "ws", PASSWORD)
    with pytest.raises(WorkspaceCorruptedError) as excinfo:
        ws.verify_backup("00000001-deadbeef")
    assert excinfo.value.reason is WorkspaceCorruptedReason.BACKUP_INVALID


def test_verify_backup_tampered_manifest_is_invalid(tmp_path):
    root = tmp_path / "ws"
    ws = create_workspace(root, PASSWORD)
    info = ws.snapshot_stores()
    (_backup_dir(root, info.name) / "workspace.enc").write_bytes(b"tampered garbage")
    with pytest.raises(WorkspaceCorruptedError) as excinfo:
        ws.verify_backup(info.name)
    assert excinfo.value.reason is WorkspaceCorruptedReason.BACKUP_INVALID


def test_verify_backup_wrong_password_manifest_is_invalid(tmp_path):
    root = tmp_path / "ws"
    ws = create_workspace(root, PASSWORD)
    info = ws.snapshot_stores()

    other_root = tmp_path / "other"
    other_ws = create_workspace(other_root, "different-password-123")
    other_info = other_ws.snapshot_stores()
    # Подменяем backup нашего workspace содержимым, зашифрованным другим паролем.
    shutil.rmtree(_backup_dir(root, info.name))
    shutil.copytree(_backup_dir(other_root, other_info.name), _backup_dir(root, info.name))

    with pytest.raises(WorkspaceCorruptedError) as excinfo:
        ws.verify_backup(info.name)
    assert excinfo.value.reason is WorkspaceCorruptedReason.BACKUP_INVALID


def test_verify_backup_foreign_workspace_id(tmp_path):
    root = tmp_path / "ws"
    ws = create_workspace(root, PASSWORD)
    info = ws.snapshot_stores()

    foreign_root = tmp_path / "foreign"
    foreign_ws = create_workspace(foreign_root, PASSWORD)  # тот же пароль, другой workspace_id
    foreign_info = foreign_ws.snapshot_stores()

    shutil.rmtree(_backup_dir(root, info.name))
    shutil.copytree(_backup_dir(foreign_root, foreign_info.name), _backup_dir(root, info.name))

    with pytest.raises(WorkspaceCorruptedError) as excinfo:
        ws.verify_backup(info.name)
    assert excinfo.value.reason is WorkspaceCorruptedReason.BACKUP_FOREIGN


def test_verify_backup_tampered_store_bytes_is_invalid(tmp_path):
    root = tmp_path / "ws"
    ws = create_workspace(root, PASSWORD)
    from app.models.entities import EntityType, MappingEntry

    with ws.store_mutation() as (mapping, _identifiers):
        mapping.add(MappingEntry(alias="alias-1", real_value="real-1", entity_type=EntityType.PERSON))
    info = ws.snapshot_stores()
    (_backup_dir(root, info.name) / "stores" / "mapping.enc").write_bytes(b"tampered")
    with pytest.raises(WorkspaceCorruptedError) as excinfo:
        ws.verify_backup(info.name)
    assert excinfo.value.reason is WorkspaceCorruptedReason.BACKUP_INVALID


def test_verify_backup_zero_count_but_file_present_is_invalid(tmp_path):
    root = tmp_path / "ws"
    ws = create_workspace(root, PASSWORD)
    info = ws.snapshot_stores()  # оба count == 0, stores/ отсутствует
    backup_dir = _backup_dir(root, info.name)
    (backup_dir / "stores").mkdir()
    (backup_dir / "stores" / "mapping.enc").write_bytes(b"unexpected")
    with pytest.raises(WorkspaceCorruptedError) as excinfo:
        ws.verify_backup(info.name)
    assert excinfo.value.reason is WorkspaceCorruptedReason.BACKUP_INVALID


def test_verify_backup_positive_count_missing_file_is_invalid(tmp_path):
    root = tmp_path / "ws"
    ws = create_workspace(root, PASSWORD)
    from app.models.entities import EntityType, MappingEntry

    with ws.store_mutation() as (mapping, _identifiers):
        mapping.add(MappingEntry(alias="alias-1", real_value="real-1", entity_type=EntityType.PERSON))
    info = ws.snapshot_stores()
    (_backup_dir(root, info.name) / "stores" / "mapping.enc").unlink()
    with pytest.raises(WorkspaceCorruptedError) as excinfo:
        ws.verify_backup(info.name)
    assert excinfo.value.reason is WorkspaceCorruptedReason.BACKUP_INVALID


def test_verify_backup_unexpected_top_level_file_is_invalid(tmp_path):
    root = tmp_path / "ws"
    ws = create_workspace(root, PASSWORD)
    info = ws.snapshot_stores()
    (_backup_dir(root, info.name) / "extra.txt").write_bytes(b"unexpected")
    with pytest.raises(WorkspaceCorruptedError) as excinfo:
        ws.verify_backup(info.name)
    assert excinfo.value.reason is WorkspaceCorruptedReason.BACKUP_INVALID


def test_verify_backup_unexpected_nested_dir_is_invalid(tmp_path):
    root = tmp_path / "ws"
    ws = create_workspace(root, PASSWORD)
    info = ws.snapshot_stores()
    (_backup_dir(root, info.name) / "safe").mkdir()
    with pytest.raises(WorkspaceCorruptedError) as excinfo:
        ws.verify_backup(info.name)
    assert excinfo.value.reason is WorkspaceCorruptedReason.BACKUP_INVALID


def test_verify_backup_reparse_backup_dir_is_invalid(tmp_path, monkeypatch):
    root = tmp_path / "ws"
    ws = create_workspace(root, PASSWORD)
    info = ws.snapshot_stores()

    import app.workspace.workspace as workspace_module

    target = _backup_dir(root, info.name)
    real_check = workspace_module._is_reparse_like

    def fake_check(path):
        if path == target:
            return True
        return real_check(path)

    monkeypatch.setattr(workspace_module, "_is_reparse_like", fake_check)
    with pytest.raises(WorkspaceCorruptedError) as excinfo:
        ws.verify_backup(info.name)
    assert excinfo.value.reason is WorkspaceCorruptedReason.BACKUP_INVALID


def test_verify_backup_no_lock_required(tmp_path):
    root = tmp_path / "ws"
    w1 = create_workspace(root, PASSWORD)
    info = w1.snapshot_stores()
    w2 = open_workspace(root, PASSWORD)
    with w1.lock():
        result = w2.verify_backup(info.name)
    assert result == info


# ---------------------------------------------------------------------------
# F. list_backups
# ---------------------------------------------------------------------------


def test_list_backups_empty(tmp_path):
    ws = create_workspace(tmp_path / "ws", PASSWORD)
    assert ws.list_backups() == ()


def test_list_backups_ordering(tmp_path):
    root = tmp_path / "ws"
    ws = create_workspace(root, PASSWORD)
    infos = [ws.snapshot_stores(keep_last=10) for _ in range(3)]
    listed = ws.list_backups()
    assert [info.name for info in listed] == sorted(info.name for info in infos)
    assert all(info.revision == 1 for info in listed)


def test_list_backups_ignores_invalid_name_entries(tmp_path):
    root = tmp_path / "ws"
    ws = create_workspace(root, PASSWORD)
    info = ws.snapshot_stores()
    (root / "backup" / "not-valid-name").mkdir()
    listed = ws.list_backups()
    assert [i.name for i in listed] == [info.name]


def test_list_backups_ignores_temp_crash_entries(tmp_path):
    root = tmp_path / "ws"
    ws = create_workspace(root, PASSWORD)
    info = ws.snapshot_stores()
    (root / "backup" / ".snapshot-abcd1234.tmp").mkdir()
    listed = ws.list_backups()
    assert [i.name for i in listed] == [info.name]


def test_list_backups_excludes_foreign(tmp_path):
    root = tmp_path / "ws"
    ws = create_workspace(root, PASSWORD)
    info = ws.snapshot_stores()

    foreign_root = tmp_path / "foreign"
    foreign_ws = create_workspace(foreign_root, PASSWORD)
    foreign_info = foreign_ws.snapshot_stores()
    shutil.copytree(_backup_dir(foreign_root, foreign_info.name), _backup_dir(root, foreign_info.name))

    listed = ws.list_backups()
    assert [i.name for i in listed] == [info.name]


def test_list_backups_excludes_tampered(tmp_path):
    root = tmp_path / "ws"
    ws = create_workspace(root, PASSWORD)
    info = ws.snapshot_stores()
    bad = ws.snapshot_stores()
    (_backup_dir(root, bad.name) / "workspace.enc").write_bytes(b"tampered")

    listed = ws.list_backups()
    assert [i.name for i in listed] == [info.name]


def test_list_backups_does_not_delete_anything(tmp_path):
    root = tmp_path / "ws"
    ws = create_workspace(root, PASSWORD)
    info = ws.snapshot_stores()
    (root / "backup" / "garbage").mkdir()
    ws.list_backups()
    assert _backup_dir(root, info.name).exists()
    assert (root / "backup" / "garbage").exists()


def test_list_backups_stale_workspace_sees_new_backup(tmp_path):
    root = tmp_path / "ws"
    w1 = create_workspace(root, PASSWORD)
    w2 = open_workspace(root, PASSWORD)
    info = w1.snapshot_stores()
    assert [i.name for i in w2.list_backups()] == [info.name]


# ---------------------------------------------------------------------------
# G. Workspace.verify — clean / store diagnostics / pending
# ---------------------------------------------------------------------------


def test_verify_clean_workspace(tmp_path):
    ws = create_workspace(tmp_path / "ws", PASSWORD)
    result = ws.verify()
    assert isinstance(result, WorkspaceVerification)
    assert result.layout_ok is True
    assert result.recovery_required is False
    assert result.mapping_sync is StoreSyncState.CONSISTENT
    assert result.identifier_sync is StoreSyncState.CONSISTENT
    assert result.artifact_count == 0
    assert result.artifacts_missing == 0
    assert result.artifacts_hash_mismatch == 0
    assert result.provenance_missing == 0
    assert result.provenance_hash_mismatch == 0
    assert result.orphan_file_count == 0
    assert result.has_integrity_problems is False


def test_verify_reflects_pending_store_mutation(tmp_path):
    root = tmp_path / "ws"
    ws = create_workspace(root, PASSWORD)

    import app.workspace.workspace as workspace_module

    manifest = workspace_module.load_encrypted_manifest(root / "workspace.enc", PASSWORD)
    pending_manifest = dataclasses.replace(manifest, revision=manifest.revision + 1, pending_store_mutation=True)
    workspace_module.save_encrypted_manifest_atomic(root / "workspace.enc", pending_manifest, PASSWORD)

    ws2 = open_workspace(root, PASSWORD)
    result = ws2.verify()
    assert result.recovery_required is True
    assert result.has_integrity_problems is True


def test_verify_reflects_genuine_ahead_store_during_recovery(tmp_path):
    """
    Correction Pass (MINOR-3 Independent Review): создаёт ГЕНУИННОЕ
    физическое опережение mapping-стора относительно manifest StoreState
    (НЕ monkeypatch _diagnose_store/её возвращаемого значения) -- точная
    имитация состояния, которое оставляет прерванный store_mutation()
    между двумя durable-записями (Stage 10B.3, frozen OD-5/OD-6).
    Механически проваливает регрессию, если verify() передаёт pending=
    False в _diagnose_store, игнорирует pending marker или превращает
    AHEAD в STORE_AHEAD_UNMARKED/STORE_UNEXPECTED_ENTRIES (verify() в
    этих случаях raise'нул бы WorkspaceBindingError вместо возврата
    WorkspaceVerification, и `result = ws2.verify()` ниже сам провалит
    тест).
    """
    root = tmp_path / "ws"
    ws = create_workspace(root, PASSWORD)
    from app.models.entities import EntityType, MappingEntry

    import app.workspace.workspace as workspace_module

    manifest_before = workspace_module.load_encrypted_manifest(root / "workspace.enc", PASSWORD)
    with ws.store_mutation() as (mapping, _identifiers):
        mapping.add(MappingEntry(alias="alias-1", real_value="real-1", entity_type=EntityType.PERSON))
    manifest_after = workspace_module.load_encrypted_manifest(root / "workspace.enc", PASSWORD)

    # Откатываем manifest на СТАРОЕ (нулевое) mapping_state, но помечаем
    # pending=True -- физически mapping.enc уже содержит запись, о
    # которой manifest "не знает": ровно то, что оставляет прерванный
    # store_mutation() между шагами pending=True/pending=False.
    forged = dataclasses.replace(
        manifest_after,
        revision=manifest_after.revision + 1,
        pending_store_mutation=True,
        mapping_state=manifest_before.mapping_state,
    )
    workspace_module.save_encrypted_manifest_atomic(root / "workspace.enc", forged, PASSWORD)

    ws2 = open_workspace(root, PASSWORD)
    result = ws2.verify()
    assert result.mapping_sync is StoreSyncState.AHEAD
    assert result.identifier_sync is StoreSyncState.CONSISTENT
    assert result.recovery_required is True
    assert result.has_integrity_problems is True


def test_verify_does_not_mutate_manifest(tmp_path):
    root = tmp_path / "ws"
    ws = create_workspace(root, PASSWORD)
    _register_monthly(ws, root)
    revision_before = ws.info.revision
    raw_before = (root / "workspace.enc").read_bytes()
    ws.verify()
    assert ws.info.revision == revision_before
    assert (root / "workspace.enc").read_bytes() == raw_before


def test_verify_stale_workspace_reloads_disk_manifest(tmp_path):
    root = tmp_path / "ws"
    w1 = create_workspace(root, PASSWORD)
    w2 = open_workspace(root, PASSWORD)
    _register_monthly(w1, root)
    result = w2.verify()
    assert result.artifact_count == 1


def test_verify_holds_lock(tmp_path):
    """verify() держит workspace.lock -- второй объект того же процесса
    на тот же путь получает NESTED_ACQUISITION при попытке взять лок."""
    root = tmp_path / "ws"
    w1 = create_workspace(root, PASSWORD)
    w2 = open_workspace(root, PASSWORD)

    import app.workspace.workspace as workspace_module
    import threading

    entered = threading.Event()
    release = threading.Event()
    result_holder = {}

    def hold_lock_during_verify():
        real_diagnose = workspace_module._diagnose_store

        def blocking_diagnose(*args, **kwargs):
            entered.set()
            release.wait(timeout=5)
            return real_diagnose(*args, **kwargs)

        workspace_module._diagnose_store = blocking_diagnose
        try:
            result_holder["value"] = w1.verify()
        finally:
            workspace_module._diagnose_store = real_diagnose

    thread = threading.Thread(target=hold_lock_during_verify)
    thread.start()
    assert entered.wait(timeout=5)
    from app.workspace.errors import WorkspaceLockedError

    with pytest.raises(WorkspaceLockedError):
        w2.verify()
    release.set()
    thread.join(timeout=5)
    assert "value" in result_holder


def test_verify_no_revision_increment_even_with_findings(tmp_path):
    root = tmp_path / "ws"
    ws = create_workspace(root, PASSWORD)
    record = _register_monthly(ws, root)
    (root / "safe" / f"{record.artifact_id}.xlsx").unlink()
    revision_before = ws.info.revision
    result = ws.verify()
    assert result.artifacts_missing == 1
    assert ws.info.revision == revision_before


# ---------------------------------------------------------------------------
# H. Workspace.verify — artifact/provenance verification
# ---------------------------------------------------------------------------


def test_verify_missing_artifact(tmp_path):
    root = tmp_path / "ws"
    ws = create_workspace(root, PASSWORD)
    record = _register_monthly(ws, root)
    (root / "safe" / f"{record.artifact_id}.xlsx").unlink()
    result = ws.verify()
    assert result.artifacts_missing == 1
    assert result.has_integrity_problems is True


def test_verify_artifact_wrong_type_directory(tmp_path):
    root = tmp_path / "ws"
    ws = create_workspace(root, PASSWORD)
    record = _register_monthly(ws, root)
    target = root / "safe" / f"{record.artifact_id}.xlsx"
    target.unlink()
    target.mkdir()
    result = ws.verify()
    assert result.artifacts_missing == 1


def test_verify_artifact_reparse(tmp_path, monkeypatch):
    root = tmp_path / "ws"
    ws = create_workspace(root, PASSWORD)
    record = _register_monthly(ws, root)
    target = root / "safe" / f"{record.artifact_id}.xlsx"

    import app.workspace.workspace as workspace_module

    real_check = workspace_module._is_reparse_like

    def fake_check(path):
        if path == target:
            return True
        return real_check(path)

    monkeypatch.setattr(workspace_module, "_is_reparse_like", fake_check)
    result = ws.verify()
    assert result.artifacts_missing == 1
    assert result.layout_ok is False


def test_verify_artifact_hash_mismatch(tmp_path):
    root = tmp_path / "ws"
    ws = create_workspace(root, PASSWORD)
    record = _register_monthly(ws, root)
    (root / "safe" / f"{record.artifact_id}.xlsx").write_bytes(b"tampered bytes")
    result = ws.verify()
    assert result.artifacts_hash_mismatch == 1
    assert result.artifacts_missing == 0


def test_verify_artifact_hash_read_oserror_counts_as_missing(tmp_path, monkeypatch):
    root = tmp_path / "ws"
    ws = create_workspace(root, PASSWORD)
    _register_monthly(ws, root)

    import app.workspace.workspace as workspace_module

    def failing_sha256_file(path):
        raise OSError("simulated I/O failure")

    monkeypatch.setattr(workspace_module, "sha256_file", failing_sha256_file)
    result = ws.verify()
    assert result.artifacts_missing == 1
    assert result.artifacts_hash_mismatch == 0


def test_verify_missing_provenance(tmp_path):
    root = tmp_path / "ws"
    ws = create_workspace(root, PASSWORD)
    record = _register_monthly(ws, root)
    (root / "provenance" / f"{record.provenance_id}.enc").unlink()
    result = ws.verify()
    assert result.provenance_missing == 1


def test_verify_provenance_wrong_type_directory(tmp_path):
    root = tmp_path / "ws"
    ws = create_workspace(root, PASSWORD)
    record = _register_monthly(ws, root)
    target = root / "provenance" / f"{record.provenance_id}.enc"
    target.unlink()
    target.mkdir()
    result = ws.verify()
    assert result.provenance_missing == 1


def test_verify_provenance_reparse(tmp_path, monkeypatch):
    root = tmp_path / "ws"
    ws = create_workspace(root, PASSWORD)
    record = _register_monthly(ws, root)
    target = root / "provenance" / f"{record.provenance_id}.enc"

    import app.workspace.workspace as workspace_module

    real_check = workspace_module._is_reparse_like

    def fake_check(path):
        if path == target:
            return True
        return real_check(path)

    monkeypatch.setattr(workspace_module, "_is_reparse_like", fake_check)
    result = ws.verify()
    assert result.provenance_missing == 1
    assert result.layout_ok is False


def test_verify_provenance_hash_mismatch(tmp_path):
    root = tmp_path / "ws"
    ws = create_workspace(root, PASSWORD)
    record = _register_monthly(ws, root)
    target = root / "provenance" / f"{record.provenance_id}.enc"
    target.write_bytes(target.read_bytes() + b"\x00tampered")
    result = ws.verify()
    assert result.provenance_hash_mismatch == 1
    assert result.provenance_missing == 0


def test_verify_provenance_hash_read_oserror_counts_as_missing(tmp_path, monkeypatch):
    root = tmp_path / "ws"
    ws = create_workspace(root, PASSWORD)
    _register_monthly(ws, root)

    import app.workspace.workspace as workspace_module

    real_sha256_file = workspace_module.sha256_file
    call_count = {"n": 0}

    def flaky_sha256_file(path):
        call_count["n"] += 1
        if str(path).endswith(".enc"):
            raise OSError("simulated I/O failure")
        return real_sha256_file(path)

    monkeypatch.setattr(workspace_module, "sha256_file", flaky_sha256_file)
    result = ws.verify()
    assert result.provenance_missing == 1


def test_verify_local_restored_not_treated_as_ai_safe(tmp_path):
    """
    verify() -- чистый integrity diagnostics; отсутствие can_upload/
    is_safe/authorize подтверждает отсутствие external-AI semantics.
    """
    root = tmp_path / "ws"
    ws = create_workspace(root, PASSWORD)
    monthly = _register_monthly(ws, root)
    _register_local_restored(ws, parent=monthly, period=monthly.period)
    result = ws.verify()
    assert result.artifacts_missing == 0
    assert not hasattr(result, "can_upload")
    assert not hasattr(result, "is_safe")
    assert not hasattr(ws, "authorize")
    assert not hasattr(ws, "external_ai_safe")


# ---------------------------------------------------------------------------
# I. Workspace.verify — orphan semantics
# ---------------------------------------------------------------------------


def test_verify_orphan_safe_file(tmp_path):
    root = tmp_path / "ws"
    ws = create_workspace(root, PASSWORD)
    (root / "safe" / (("a" * 32) + ".xlsx")).write_bytes(b"orphan")
    result = ws.verify()
    assert result.orphan_file_count == 1
    assert result.has_integrity_problems is False


def test_verify_orphan_local_plaintext_file(tmp_path):
    root = tmp_path / "ws"
    ws = create_workspace(root, PASSWORD)
    (root / "local_plaintext" / (("b" * 32) + ".xlsx")).write_bytes(b"orphan")
    result = ws.verify()
    assert result.orphan_file_count == 1


def test_verify_orphan_provenance_file(tmp_path):
    root = tmp_path / "ws"
    ws = create_workspace(root, PASSWORD)
    _make_provenance(root, "c" * 32, "d" * 32)
    result = ws.verify()
    assert result.orphan_file_count == 1


def test_verify_orphan_wrong_extension_regular_file(tmp_path):
    root = tmp_path / "ws"
    ws = create_workspace(root, PASSWORD)
    (root / "safe" / "not-an-artifact.txt").write_bytes(b"junk")
    result = ws.verify()
    assert result.orphan_file_count == 1
    assert result.layout_ok is True


def test_verify_orphan_does_not_set_has_integrity_problems(tmp_path):
    root = tmp_path / "ws"
    ws = create_workspace(root, PASSWORD)
    (root / "safe" / (("e" * 32) + ".xlsx")).write_bytes(b"orphan1")
    (root / "local_plaintext" / (("f" * 32) + ".xlsx")).write_bytes(b"orphan2")
    _make_provenance(root, "1" * 32, "2" * 32)
    result = ws.verify()
    assert result.orphan_file_count == 3
    assert result.has_integrity_problems is False


def test_verify_backup_contents_excluded_from_orphan_scan(tmp_path):
    root = tmp_path / "ws"
    ws = create_workspace(root, PASSWORD)
    ws.snapshot_stores()
    result = ws.verify()
    assert result.orphan_file_count == 0


def test_verify_temp_files_excluded_from_orphan_scan(tmp_path):
    """
    Correction Pass (MINOR-1): позитивный кейс использует РЕАЛИСТИЧНЫЕ
    temp-имена -- привязанные к ФАКТИЧЕСКИ зарегистрированному
    artifact_id/provenance_id и с реальным 8-символьным форматом
    случайной части tempfile.mkstemp -- а не синтетические имена,
    которые production никогда не создаёт (прежняя версия теста
    маскировала бы недостаточно строгий предикат).
    """
    root = tmp_path / "ws"
    ws = create_workspace(root, PASSWORD)
    record = _register_monthly(ws, root)
    # Реалистичный crash-temp для _atomic_copy_file над ЗАРЕГИСТРИРОВАННЫМ
    # артефактом: ".<target.name>.<8 символов [a-z0-9_]>.tmp".
    (root / "safe" / f".{record.artifact_id}.xlsx.abcd1234.tmp").write_bytes(b"crash leftover")
    # Реалистичный crash-temp _read_job_id_from_snapshot: фиксированный
    # литеральный префикс + реальный формат случайной части.
    (root / "provenance" / ".provenance-snapshot-a1b2c3d4.tmp").write_bytes(b"crash leftover")
    result = ws.verify()
    assert result.orphan_file_count == 0
    assert result.layout_ok is True


def test_verify_provenance_target_temp_recognized(tmp_path):
    """Реалистичный crash-temp provenance_encrypted._atomic_write для
    ЗАРЕГИСТРИРОВАННОГО provenance_id также не считается orphan."""
    root = tmp_path / "ws"
    ws = create_workspace(root, PASSWORD)
    record = _register_monthly(ws, root)
    (root / "provenance" / f".{record.provenance_id}.enc.qwerty12.tmp").write_bytes(b"crash leftover")
    result = ws.verify()
    assert result.orphan_file_count == 0


def test_verify_arbitrary_tmp_file_counts_as_orphan(tmp_path):
    """
    Correction Pass (MINOR-1, обязательный негативный regression):
    произвольный ".arbitrary.tmp" НЕ соответствует ни одному реальному
    internal crash-temp шаблону -- ДОЛЖЕН считаться orphan, а не тихо
    игнорироваться. Механически проваливает старый (слишком широкий)
    предикат "startswith('.') and endswith('.tmp')".
    """
    root = tmp_path / "ws"
    ws = create_workspace(root, PASSWORD)
    (root / "safe" / ".arbitrary.tmp").write_bytes(b"not a real crash artifact")
    result = ws.verify()
    assert result.orphan_file_count == 1
    assert result.has_integrity_problems is False


def test_verify_two_segment_tmp_file_counts_as_orphan(tmp_path):
    """".foo.bar.tmp" не соответствует ни known-target-name, ни
    provenance-snapshot шаблону -- orphan."""
    root = tmp_path / "ws"
    ws = create_workspace(root, PASSWORD)
    (root / "safe" / ".foo.bar.tmp").write_bytes(b"not a real crash artifact")
    result = ws.verify()
    assert result.orphan_file_count == 1


def test_verify_provenance_snapshot_empty_or_missing_random_counts_as_orphan(tmp_path):
    """
    Correction Pass (MINOR-5 Focused Independent Review): контракт
    recognized-temp больше НЕ специфицирует алфавит/длину opaque
    random-компонента (это была приватная деталь реализации CPython
    tempfile._RandomNameSequence, не публичный контракт) -- единственное
    реально невалидное provenance-snapshot имя это ОТСУТСТВУЮЩИЙ/ПУСТОЙ
    random-компонент после фиксированного project-owned префикса.
    """
    root = tmp_path / "ws"
    ws = create_workspace(root, PASSWORD)
    (root / "provenance" / ".provenance-snapshot-.tmp").write_bytes(b"empty random")
    (root / "provenance" / ".provenance-snapshot.tmp").write_bytes(b"missing random entirely")
    result = ws.verify()
    assert result.orphan_file_count == 2


def test_verify_provenance_snapshot_future_format_random_recognized(tmp_path):
    """
    Correction Pass (MINOR-5): будущий/иной random-формат (НЕ
    [a-z0-9_]{8} текущего CPython) всё равно распознаётся -- контракт
    зависит только от фиксированного project-owned префикса и
    непустого opaque-компонента, а не от формата конкретной реализации
    tempfile.
    """
    root = tmp_path / "ws"
    ws = create_workspace(root, PASSWORD)
    (root / "provenance" / ".provenance-snapshot-FutureTempToken-XYZ.tmp").write_bytes(b"future format")
    result = ws.verify()
    assert result.orphan_file_count == 0


def test_verify_fake_unregistered_artifact_temp_counts_as_orphan(tmp_path):
    """
    Temp-имя, структурно похожее на артефакт-crash-temp
    (".<hex32>.xlsx.<8>.tmp"), но для НЕ зарегистрированного artifact_id
    -- не скрывается, считается orphan (§23 Independent Review: не
    прятать temp для несуществующего target).
    """
    root = tmp_path / "ws"
    ws = create_workspace(root, PASSWORD)
    fake_id = "c" * 32
    (root / "safe" / f".{fake_id}.xlsx.abcd1234.tmp").write_bytes(b"fake")
    result = ws.verify()
    assert result.orphan_file_count == 1


def test_verify_fake_unregistered_provenance_temp_counts_as_orphan(tmp_path):
    """Аналогично для provenance -- НЕзарегистрированный provenance_id."""
    root = tmp_path / "ws"
    ws = create_workspace(root, PASSWORD)
    fake_id = "d" * 32
    (root / "provenance" / f".{fake_id}.enc.abcd1234.tmp").write_bytes(b"fake")
    result = ws.verify()
    assert result.orphan_file_count == 1


def test_verify_artifact_temp_empty_or_missing_random_counts_as_orphan(tmp_path):
    """Реальный registered artifact_id, но random-компонент ОТСУТСТВУЕТ/
    ПУСТ -- единственная реально невалидная структура под новым
    контрактом (MINOR-5), считается orphan."""
    root = tmp_path / "ws"
    ws = create_workspace(root, PASSWORD)
    record = _register_monthly(ws, root)
    (root / "safe" / f".{record.artifact_id}.xlsx..tmp").write_bytes(b"empty random")
    (root / "safe" / f".{record.artifact_id}.xlsx.tmp").write_bytes(b"missing random entirely")
    result = ws.verify()
    assert result.orphan_file_count == 2


def test_verify_artifact_temp_future_format_random_recognized(tmp_path):
    """Реальный registered artifact_id с random-компонентом, НЕ
    соответствующим формату/алфавиту текущего CPython (MINOR-5:
    contract не должен зависеть от этой приватной детали) --
    распознаётся, НЕ orphan."""
    root = tmp_path / "ws"
    ws = create_workspace(root, PASSWORD)
    record = _register_monthly(ws, root)
    (root / "safe" / f".{record.artifact_id}.xlsx.FutureToken-123.tmp").write_bytes(b"future format")
    result = ws.verify()
    assert result.orphan_file_count == 0


def test_verify_registered_name_in_wrong_directory_temp_counts_as_orphan(tmp_path):
    """
    Correction Pass (закрытие пробела покрытия, отмеченного Focused
    Independent Review §7/§19): registered provenance filename,
    использованное как target-temp внутри safe/ (чужая директория) --
    НЕ распознаётся, поскольку expected_names строго привязан к
    ТЕКУЩЕЙ сканируемой директории (safe_expected не содержит
    provenance-имена, и наоборот). Закрепляет directory-local изоляцию
    expected_names.
    """
    root = tmp_path / "ws"
    ws = create_workspace(root, PASSWORD)
    monthly = _register_monthly(ws, root)
    local = _register_local_restored(ws, parent=monthly, period=monthly.period)

    # Реально зарегистрированное provenance-имя, temp-паттерн -- но
    # физически лежит в safe/, а не в provenance/.
    (root / "safe" / f".{monthly.provenance_id}.enc.abcd1234.tmp").write_bytes(b"wrong directory")
    # Реально зарегистрированное safe-artifact-имя (LOCAL_RESTORED
    # находится в local_plaintext/), temp-паттерн -- но лежит в provenance/.
    (root / "provenance" / f".{local.artifact_id}.xlsx.abcd1234.tmp").write_bytes(b"wrong directory")

    result = ws.verify()
    assert result.orphan_file_count == 2


def test_verify_registered_file_not_counted_as_orphan(tmp_path):
    root = tmp_path / "ws"
    ws = create_workspace(root, PASSWORD)
    _register_monthly(ws, root)
    result = ws.verify()
    assert result.orphan_file_count == 0


# ---------------------------------------------------------------------------
# J. Workspace.verify — layout semantics
# ---------------------------------------------------------------------------


def test_verify_reparse_structural_dir_sets_layout_ok_false(tmp_path, monkeypatch):
    root = tmp_path / "ws"
    ws = create_workspace(root, PASSWORD)

    import app.workspace.workspace as workspace_module

    target = root / "provenance"
    real_check = workspace_module._is_reparse_like

    def fake_check(path):
        if path == target:
            return True
        return real_check(path)

    monkeypatch.setattr(workspace_module, "_is_reparse_like", fake_check)
    result = ws.verify()
    assert result.layout_ok is False
    assert result.has_integrity_problems is True


def test_verify_orphan_directory_instead_of_file_sets_layout_ok_false(tmp_path):
    root = tmp_path / "ws"
    ws = create_workspace(root, PASSWORD)
    (root / "safe" / "unexpected_dir").mkdir()
    result = ws.verify()
    assert result.layout_ok is False
    assert result.orphan_file_count == 0


def test_verify_missing_structural_directory_raises_layout_incomplete(tmp_path):
    root = tmp_path / "ws"
    ws = create_workspace(root, PASSWORD)
    shutil.rmtree(root / "provenance")
    with pytest.raises(WorkspaceCorruptedError) as excinfo:
        ws.verify()
    assert excinfo.value.reason is WorkspaceCorruptedReason.LAYOUT_INCOMPLETE


# ---------------------------------------------------------------------------
# K. Security boundaries / OD-7
# ---------------------------------------------------------------------------


def test_no_external_ai_api_anywhere(tmp_path):
    ws = create_workspace(tmp_path / "ws", PASSWORD)
    for name in ("can_upload", "is_safe", "authorize", "external_ai_safe", "upload_authorized"):
        assert not hasattr(ws, name)
        assert not hasattr(WorkspaceVerification, name)
        assert not hasattr(BackupInfo, name)


def test_od7_snapshot_manifest_load_failure_no_password_leak(tmp_path):
    root = tmp_path / "ws"
    ws = create_workspace(root, _PASSWORD_SENTINEL_10B4_3)
    (root / "workspace.enc").write_bytes(b"corrupted, not a valid container")
    with pytest.raises(Exception) as excinfo:
        ws.snapshot_stores()
    assert _internal_frame_locals_containing(excinfo.value, (_PASSWORD_SENTINEL_10B4_3,)) == []


def test_od7_verify_backup_manifest_load_failure_no_password_leak(tmp_path):
    root = tmp_path / "ws"
    ws = create_workspace(root, _PASSWORD_SENTINEL_10B4_3)
    info = ws.snapshot_stores()
    (_backup_dir(root, info.name) / "workspace.enc").write_bytes(b"corrupted")
    with pytest.raises(WorkspaceCorruptedError) as excinfo:
        ws.verify_backup(info.name)
    assert _internal_frame_locals_containing(excinfo.value, (_PASSWORD_SENTINEL_10B4_3,)) == []


def test_od7_verify_backup_tampered_store_no_password_leak(tmp_path):
    root = tmp_path / "ws"
    ws = create_workspace(root, _PASSWORD_SENTINEL_10B4_3)
    from app.models.entities import EntityType, MappingEntry

    with ws.store_mutation() as (mapping, _identifiers):
        mapping.add(MappingEntry(alias="alias-1", real_value="real-1", entity_type=EntityType.PERSON))
    info = ws.snapshot_stores()
    (_backup_dir(root, info.name) / "stores" / "mapping.enc").write_bytes(b"tampered")
    with pytest.raises(WorkspaceCorruptedError) as excinfo:
        ws.verify_backup(info.name)
    assert _internal_frame_locals_containing(excinfo.value, (_PASSWORD_SENTINEL_10B4_3,)) == []


def test_od7_list_backups_manifest_load_failure_no_password_leak(tmp_path):
    root = tmp_path / "ws"
    ws = create_workspace(root, _PASSWORD_SENTINEL_10B4_3)
    (root / "workspace.enc").write_bytes(b"corrupted")
    with pytest.raises(Exception) as excinfo:
        ws.list_backups()
    assert _internal_frame_locals_containing(excinfo.value, (_PASSWORD_SENTINEL_10B4_3,)) == []


def test_od7_verify_manifest_load_failure_no_password_leak(tmp_path):
    root = tmp_path / "ws"
    ws = create_workspace(root, _PASSWORD_SENTINEL_10B4_3)
    (root / "workspace.enc").write_bytes(b"corrupted")
    with pytest.raises(Exception) as excinfo:
        ws.verify()
    assert _internal_frame_locals_containing(excinfo.value, (_PASSWORD_SENTINEL_10B4_3,)) == []


def test_od7_retention_scan_failure_no_password_leak(tmp_path):
    root = tmp_path / "ws"
    ws = create_workspace(root, _PASSWORD_SENTINEL_10B4_3)
    ws.snapshot_stores()
    # Испортим существующий backup manifest -- retention-скан молча
    # пропустит его (WorkspaceError), не выбросив наружу пароль.
    infos_dir = root / "backup"
    for entry in infos_dir.iterdir():
        (entry / "workspace.enc").write_bytes(b"corrupted")
    info2 = None
    try:
        info2 = ws.snapshot_stores()
    except Exception as exc:
        assert _internal_frame_locals_containing(exc, (_PASSWORD_SENTINEL_10B4_3,)) == []
    else:
        assert info2 is not None


def test_od7_retention_deletion_failure_no_password_leak_via_public_api(tmp_path, monkeypatch):
    """
    Correction Pass (MINOR-2 Independent Review, детерминизирован в
    MINOR-4 Focused Independent Review): проверяет реальный путь через
    ПУБЛИЧНЫЙ snapshot_stores() -> retention-удаление физически не
    удаётся (НЕ cleanup temp, а именно deletion уже опубликованного
    backup) -> escaped exception graph не содержит пароль. Не тестирует
    _apply_backup_retention изолированно.

    MINOR-4: ранее failing_path был жёстко привязан к old_infos[0], а
    все snapshots этого теста имеют ОДИНАКОВУЮ revision — retention
    сортирует исключительно по случайному имени, поэтому old_infos[0]
    примерно в 25% случаев оказывался "сохраняемым" (keep_last=1) и
    инъецированный сбой НИКОГДА не срабатывал -- эмпирически
    подтверждено (20 прогонов -> 2 failed "DID NOT RAISE OSError";
    полная регрессия сама поймала этот сбой вживую). Исправление:
    patched shutil.rmtree триггерит сбой на ПЕРВОЙ попытке удалить
    ЛЮБУЮ директорию с ВАЛИДНЫМ backup-именем ([0-9]{8}-[0-9a-f]{8},
    через уже существующий _is_valid_backup_name) -- гарантированно
    совпадает именно с retention deletion (не с cleanup temp-директории
    ".snapshot-*.tmp", которая никогда не проходит этот формат)
    независимо от случайного порядка сортировки.

    Обязательный НЕГАТИВНЫЙ КОНТРОЛЬ (без него тест мог бы быть ложно
    зелёным, если бы сканер сам был неисправен): синтетически
    "протекающий" кадр с тем же sentinel ДОЛЖЕН быть пойман тем же
    сканером ДО того, как мы доверяем его результату на реальном пути.
    """
    root = tmp_path / "ws"
    ws = create_workspace(root, _PASSWORD_SENTINEL_10B4_3)

    # --- Негативный контроль сканера ---
    def _leaking_frame(secret):
        raise ValueError("synthetic leak for negative control")

    try:
        _leaking_frame(_PASSWORD_SENTINEL_10B4_3)
    except ValueError as synthetic_exc:
        import tests.test_workspace_artifacts as artifacts_module

        original_suffixes = artifacts_module._INTERNAL_FILENAME_SUFFIXES
        artifacts_module._INTERNAL_FILENAME_SUFFIXES = original_suffixes + (os.path.basename(__file__),)
        try:
            negative_control_findings = _internal_frame_locals_containing(
                synthetic_exc, (_PASSWORD_SENTINEL_10B4_3,)
            )
        finally:
            artifacts_module._INTERNAL_FILENAME_SUFFIXES = original_suffixes
        assert negative_control_findings != [], "негативный контроль: сканер обязан ловить sentinel"

    # --- Реальный путь: retention deletion failure через snapshot_stores() ---
    for _ in range(3):
        ws.snapshot_stores(keep_last=10)

    import app.workspace.workspace as workspace_module

    before_names = {
        p.name for p in (root / "backup").iterdir() if workspace_module._is_valid_backup_name(p.name)
    }
    assert len(before_names) == 3

    real_rmtree = shutil.rmtree
    delete_attempts = []
    injected_failure = {"done": False}

    def flaky_rmtree(path, *args, **kwargs):
        name = os.path.basename(str(path))
        if workspace_module._is_valid_backup_name(name):
            delete_attempts.append(name)
            if not injected_failure["done"]:
                injected_failure["done"] = True
                raise OSError(13, "Permission denied", str(path))
        return real_rmtree(path, *args, **kwargs)

    monkeypatch.setattr(workspace_module.shutil, "rmtree", flaky_rmtree)

    # keep_last РОВНО равен числу уже существующих backup (3): после
    # публикации 4-го (нового) кандидатов становится 4, поэтому retention
    # планирует УДАЛИТЬ РОВНО ОДИН (len(candidates) - keep_last == 1) --
    # эта единственная попытка ГАРАНТИРОВАННО является первой и потому
    # гарантированно перехватывается injected_failure, независимо от
    # того, какое случайное имя (старое или новое) оказалось выбрано
    # retention-сортировкой. Поскольку удаление проваливается, оно НЕ
    # выполняется вовсе -- ВСЕ 4 backup (3 старых + новый) детерминированно
    # остаются на диске. Это устраняет ~25% flakiness прежней версии
    # теста (keep_last=1 с непредсказуемым числом реально удаляемых
    # кандидатов), не полагаясь при этом на угадывание имени нового
    # snapshot.
    with pytest.raises(OSError) as excinfo:
        ws.snapshot_stores(keep_last=len(before_names))

    # 1/2. Retention deletion действительно вызывалась, и сбой
    # действительно был инъецирован (а не пропущен незамеченным).
    assert delete_attempts, "retention deletion ни разу не была вызвана"
    assert len(delete_attempts) == 1, f"ожидалась ровно одна попытка удаления, получено: {delete_attempts}"
    assert injected_failure["done"] is True

    # 3. Публичный API поднял OSError -- уже гарантировано pytest.raises.
    # 4/5. Новый snapshot был опубликован ДО retention failure и
    # остаётся ПОСЛЕ неё -- доказано через множественную разность имён
    # (а не через слабый count alone). Поскольку единственная попытка
    # удаления провалилась, ни один из 4 backup фактически не удалён --
    # ровно один НОВЫЙ (не входивший в before_names) обязан появиться.
    after_names = {
        p.name for p in (root / "backup").iterdir() if workspace_module._is_valid_backup_name(p.name)
    }
    assert before_names <= after_names, "ни один из существовавших backup не должен быть реально удалён"
    new_names = after_names - before_names
    assert len(new_names) == 1, f"ожидался ровно один новый опубликованный backup, получено: {new_names}"

    # 6. Password отсутствует в escaped exception graph.
    exc = excinfo.value
    assert _PASSWORD_SENTINEL_10B4_3 not in str(exc)
    assert _PASSWORD_SENTINEL_10B4_3 not in repr(exc)
    assert all(_PASSWORD_SENTINEL_10B4_3 not in repr(value) for value in exc.args)
    assert all(_PASSWORD_SENTINEL_10B4_3 not in repr(value) for value in vars(exc).values())
    assert exc.__cause__ is None
    assert exc.__context__ is None
    assert _internal_frame_locals_containing(exc, (_PASSWORD_SENTINEL_10B4_3,)) == []
