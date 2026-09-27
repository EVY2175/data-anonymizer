"""
Тесты Stage 10B.4.2: allocate_provenance_path / verified_artifact_path /
verified_provenance_path / latest_analytical_artifact / set_latest_analytical.

Переиспользует helper'ы Stage 10B.4.1 (_make_xlsx/_report_for/
_make_provenance/_register_monthly/PASSWORD/M/C/L/_internal_frame_locals_containing
и т.д.) из tests.test_workspace_artifacts напрямую импортом — отдельная
копия этих helper'ов не заводится (см. финальный отчёт Implementation
Pass, раздел "Test file layout").

Группы:
    A. allocate_provenance_path
    B. verified_artifact_path — успех для всех 3 kind
    C. verified_artifact_path — not found / integrity
    D. verified_provenance_path — успех
    E. verified_provenance_path — not found / integrity
    F. latest_analytical_artifact
    G. set_latest_analytical — успех / errors
    H. Same-pointer / explicit rollback (OD-10B4.2-1 / OD-10B4-1)
    I. Pending/recovery поведение (OD-10B4.2-3)
    J. Read locking (OD-10B4.2-2) — read-only без lock
    K. Stale-object поведение
    L. Failure atomicity (set_latest_analytical)
    M. Revision semantics
    N. Security boundary (verified != external-AI-safe)
    O. OD-7: конфиденциальность
"""

from __future__ import annotations

import dataclasses
import secrets

import pytest

from app.workspace import create_workspace, open_workspace
from app.workspace.errors import (
    ArtifactIntegrityError,
    ArtifactNotFoundError,
    ArtifactRegistrationError,
    ArtifactRegistrationReason,
    WorkspaceBindingError,
    WorkspaceBindingReason,
    WorkspaceInputError,
)
from app.workspace.models import ArtifactKind, ArtifactRecord, ProvenanceSlot
from tests.test_workspace_artifacts import (
    M,
    C,
    L,
    PASSWORD,
    _internal_frame_locals_containing,
    _make_provenance,
    _make_xlsx,
    _register_monthly,
    _report_for,
)

_PASSWORD_SENTINEL_10B4_2 = "SENTINEL_10B4_2_PASSWORD_Q9F3"


def _register_monthly_for(ws, root, password, *, period="2026-01", marker="m"):
    """
    Регистрирует monthly-артефакт, provenance которого зашифрован ИМЕННО
    под паролем `password` этого workspace (в отличие от
    tests.test_workspace_artifacts._register_monthly, который жёстко
    использует дефолтный PASSWORD Stage 10B.4.1 — непригодно для
    OD-7-тестов с собственным sentinel-паролем workspace). provenance_id/
    job_id генерируются свежими на КАЖДЫЙ вызов — не переиспользуют общие
    дефолты, чтобы несколько вызовов в одном тесте не сталкивались
    (EncryptedFileProvenanceStore запрещает job_id при открытии уже
    существующего sidecar).
    """
    job_id = secrets.token_hex(16)
    provenance_id = secrets.token_hex(16)
    slot = ws.allocate_artifact_path(M)
    _make_xlsx(slot.path, marker)
    _make_provenance(root, provenance_id, job_id, password=password)
    return ws.register_artifact(
        slot, period=period, source_job_id=job_id, provenance_id=provenance_id,
        safety_report=_report_for(slot.path),
    )


def _register_canonical(ws, root, *, period="2026-01", marker="c", parent_ids=None, password=PASSWORD):
    if parent_ids is None:
        monthly = _register_monthly_for(ws, root, password, period=period, marker=f"{marker}-parent")
        parent_ids = (monthly.artifact_id,)
    slot = ws.allocate_artifact_path(C)
    _make_xlsx(slot.path, marker)
    return ws.register_artifact(
        slot, period=period, parent_artifact_ids=parent_ids,
        safety_report=_report_for(slot.path),
    )


def _register_local_restored(ws, *, parent, period, marker="local"):
    slot = ws.allocate_artifact_path(L)
    _make_xlsx(slot.path, marker)
    return ws.register_artifact(slot, period=period, parent_artifact_ids=(parent.artifact_id,))


# ---------------------------------------------------------------------------
# A. allocate_provenance_path
# ---------------------------------------------------------------------------


def test_allocate_provenance_path_basic(tmp_path):
    root = tmp_path / "ws"
    ws = create_workspace(root, PASSWORD)
    slot = ws.allocate_provenance_path()
    assert isinstance(slot, ProvenanceSlot)
    assert len(slot.provenance_id) == 32
    assert all(ch in "0123456789abcdef" for ch in slot.provenance_id)
    assert slot.path == root / "provenance" / f"{slot.provenance_id}.enc"
    assert slot.path.parent == root / "provenance"
    assert slot.path.suffix == ".enc"
    assert not slot.path.exists()


def test_allocate_provenance_path_does_not_mutate_manifest(tmp_path):
    root = tmp_path / "ws"
    ws = create_workspace(root, PASSWORD)
    revision_before = ws.info.revision
    raw_before = (root / "workspace.enc").read_bytes()
    ws.allocate_provenance_path()
    assert ws.info.revision == revision_before
    assert (root / "workspace.enc").read_bytes() == raw_before


def test_allocate_provenance_path_retries_on_collision(tmp_path, monkeypatch):
    root = tmp_path / "ws"
    ws = create_workspace(root, PASSWORD)

    import app.workspace.workspace as workspace_module

    real_token_hex = workspace_module.secrets.token_hex
    forced_id = "d" * 32
    colliding_path = root / "provenance" / f"{forced_id}.enc"
    colliding_path.parent.mkdir(parents=True, exist_ok=True)
    colliding_path.write_bytes(b"occupied")

    call_count = {"n": 0}

    def fake_token_hex(n):
        call_count["n"] += 1
        if call_count["n"] == 1:
            return forced_id
        return real_token_hex(n)

    monkeypatch.setattr(workspace_module.secrets, "token_hex", fake_token_hex)
    slot = ws.allocate_provenance_path()
    assert slot.provenance_id != forced_id
    assert call_count["n"] >= 2


def test_allocate_provenance_path_exhaustion_raises_input_error(tmp_path, monkeypatch):
    root = tmp_path / "ws"
    ws = create_workspace(root, PASSWORD)

    import app.workspace.workspace as workspace_module

    monkeypatch.setattr(workspace_module.secrets, "token_hex", lambda n: "e" * 32)
    occupied = root / "provenance" / ("e" * 32 + ".enc")
    occupied.parent.mkdir(parents=True, exist_ok=True)
    occupied.write_bytes(b"occupied")

    with pytest.raises(WorkspaceInputError):
        ws.allocate_provenance_path()


def test_allocate_provenance_path_candidate_semantics(tmp_path):
    root = tmp_path / "ws"
    ws = create_workspace(root, PASSWORD)
    slot1 = ws.allocate_provenance_path()
    slot2 = ws.allocate_provenance_path()
    # Два последовательных вызова не гарантируют эксклюзивную
    # резервацию — оба слота допустимо реиспользовать/оставить неиспользуемыми.
    assert slot1.provenance_id != slot2.provenance_id


# ---------------------------------------------------------------------------
# B. verified_artifact_path — успех для всех 3 kind
# ---------------------------------------------------------------------------


def test_verified_artifact_path_monthly_success(tmp_path):
    root = tmp_path / "ws"
    ws = create_workspace(root, PASSWORD)
    record = _register_monthly(ws, root)
    path = ws.verified_artifact_path(record.artifact_id)
    assert path == root / "safe" / f"{record.artifact_id}.xlsx"
    assert path.exists()


def test_verified_artifact_path_canonical_success(tmp_path):
    root = tmp_path / "ws"
    ws = create_workspace(root, PASSWORD)
    record = _register_canonical(ws, root)
    path = ws.verified_artifact_path(record.artifact_id)
    assert path == root / "safe" / f"{record.artifact_id}.xlsx"


def test_verified_artifact_path_local_restored_success(tmp_path):
    root = tmp_path / "ws"
    ws = create_workspace(root, PASSWORD)
    monthly = _register_monthly(ws, root)
    record = _register_local_restored(ws, parent=monthly, period=monthly.period)
    path = ws.verified_artifact_path(record.artifact_id)
    assert path == root / "local_plaintext" / f"{record.artifact_id}.xlsx"


def test_verified_artifact_path_does_not_mutate_manifest(tmp_path):
    root = tmp_path / "ws"
    ws = create_workspace(root, PASSWORD)
    record = _register_monthly(ws, root)
    revision_before = ws.info.revision
    raw_before = (root / "workspace.enc").read_bytes()
    ws.verified_artifact_path(record.artifact_id)
    assert ws.info.revision == revision_before
    assert (root / "workspace.enc").read_bytes() == raw_before


# ---------------------------------------------------------------------------
# C. verified_artifact_path — not found / integrity
# ---------------------------------------------------------------------------


def test_verified_artifact_path_rejects_non_str_id(tmp_path):
    ws = create_workspace(tmp_path / "ws", PASSWORD)
    with pytest.raises(WorkspaceInputError):
        ws.verified_artifact_path(12345)  # type: ignore[arg-type]


def test_verified_artifact_path_unknown_id(tmp_path):
    ws = create_workspace(tmp_path / "ws", PASSWORD)
    with pytest.raises(ArtifactNotFoundError):
        ws.verified_artifact_path("f" * 32)


def test_verified_artifact_path_malformed_id_is_not_found(tmp_path):
    ws = create_workspace(tmp_path / "ws", PASSWORD)
    with pytest.raises(ArtifactNotFoundError):
        ws.verified_artifact_path("not-a-valid-hex32-id")


def test_verified_artifact_path_physical_missing(tmp_path):
    root = tmp_path / "ws"
    ws = create_workspace(root, PASSWORD)
    record = _register_monthly(ws, root)
    (root / "safe" / f"{record.artifact_id}.xlsx").unlink()
    with pytest.raises(ArtifactIntegrityError):
        ws.verified_artifact_path(record.artifact_id)


def test_verified_artifact_path_hash_mismatch(tmp_path):
    root = tmp_path / "ws"
    ws = create_workspace(root, PASSWORD)
    record = _register_monthly(ws, root)
    target = root / "safe" / f"{record.artifact_id}.xlsx"
    target.write_bytes(b"tampered bytes, different from registered SHA")
    with pytest.raises(ArtifactIntegrityError):
        ws.verified_artifact_path(record.artifact_id)


def test_verified_artifact_path_directory_instead_of_file(tmp_path):
    root = tmp_path / "ws"
    ws = create_workspace(root, PASSWORD)
    record = _register_monthly(ws, root)
    target = root / "safe" / f"{record.artifact_id}.xlsx"
    target.unlink()
    target.mkdir()
    with pytest.raises(ArtifactIntegrityError):
        ws.verified_artifact_path(record.artifact_id)


def test_verified_artifact_path_reparse_like(tmp_path, monkeypatch):
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
    with pytest.raises(ArtifactIntegrityError):
        ws.verified_artifact_path(record.artifact_id)


def test_verified_artifact_path_hash_read_failure_maps_to_integrity_error(tmp_path, monkeypatch):
    root = tmp_path / "ws"
    ws = create_workspace(root, PASSWORD)
    record = _register_monthly(ws, root)

    import app.workspace.workspace as workspace_module

    def failing_sha256_file(path):
        raise PermissionError(13, "Permission denied", str(path))

    monkeypatch.setattr(workspace_module, "sha256_file", failing_sha256_file)
    with pytest.raises(ArtifactIntegrityError) as excinfo:
        ws.verified_artifact_path(record.artifact_id)
    assert excinfo.value.__cause__ is None
    assert excinfo.value.__context__ is None


def test_verified_artifact_path_works_during_pending_store_mutation(tmp_path):
    root = tmp_path / "ws"
    ws = create_workspace(root, PASSWORD)
    record = _register_monthly(ws, root)

    with ws.store_mutation() as (mapping, _identifiers):
        pass  # успешный store_mutation, дальше форсируем pending вручную

    # Форсируем pending_store_mutation=True напрямую поверх текущего
    # manifest — тот же приём, что уже используется в тестах на
    # recovery-gate register_artifact (без обращения к приватным полям
    # Workspace, только к manifest-объекту через save_encrypted_manifest_atomic).
    import app.workspace.workspace as workspace_module

    manifest = workspace_module.load_encrypted_manifest(root / "workspace.enc", PASSWORD)
    pending_manifest = dataclasses.replace(manifest, revision=manifest.revision + 1, pending_store_mutation=True)
    workspace_module.save_encrypted_manifest_atomic(root / "workspace.enc", pending_manifest, PASSWORD)

    ws2 = open_workspace(root, PASSWORD)
    assert ws2.recovery_required is True
    path = ws2.verified_artifact_path(record.artifact_id)
    assert path.exists()


# ---------------------------------------------------------------------------
# D. verified_provenance_path — успех
# ---------------------------------------------------------------------------


def test_verified_provenance_path_success(tmp_path):
    root = tmp_path / "ws"
    ws = create_workspace(root, PASSWORD)
    record = _register_monthly(ws, root)
    path = ws.verified_provenance_path(record.provenance_id)
    assert path == root / "provenance" / f"{record.provenance_id}.enc"
    assert path.exists()


def test_verified_provenance_path_does_not_mutate_manifest(tmp_path):
    root = tmp_path / "ws"
    ws = create_workspace(root, PASSWORD)
    record = _register_monthly(ws, root)
    revision_before = ws.info.revision
    raw_before = (root / "workspace.enc").read_bytes()
    ws.verified_provenance_path(record.provenance_id)
    assert ws.info.revision == revision_before
    assert (root / "workspace.enc").read_bytes() == raw_before


def test_verified_provenance_path_no_decryption_required(tmp_path, monkeypatch):
    """Provenance-путь верифицируется без открытия EncryptedFileProvenanceStore."""
    root = tmp_path / "ws"
    ws = create_workspace(root, PASSWORD)
    record = _register_monthly(ws, root)

    import app.workspace.workspace as workspace_module

    def boom(*args, **kwargs):
        raise AssertionError("EncryptedFileProvenanceStore не должен открываться verified_provenance_path")

    monkeypatch.setattr(workspace_module, "EncryptedFileProvenanceStore", boom)
    path = ws.verified_provenance_path(record.provenance_id)
    assert path.exists()


# ---------------------------------------------------------------------------
# E. verified_provenance_path — not found / integrity
# ---------------------------------------------------------------------------


def test_verified_provenance_path_rejects_non_str_id(tmp_path):
    ws = create_workspace(tmp_path / "ws", PASSWORD)
    with pytest.raises(WorkspaceInputError):
        ws.verified_provenance_path(None)  # type: ignore[arg-type]


def test_verified_provenance_path_unknown_id(tmp_path):
    ws = create_workspace(tmp_path / "ws", PASSWORD)
    with pytest.raises(ArtifactNotFoundError):
        ws.verified_provenance_path("f" * 32)


def test_verified_provenance_path_malformed_id_is_not_found(tmp_path):
    ws = create_workspace(tmp_path / "ws", PASSWORD)
    with pytest.raises(ArtifactNotFoundError):
        ws.verified_provenance_path("not-a-valid-hex32-id")


def test_verified_provenance_path_missing_file(tmp_path):
    root = tmp_path / "ws"
    ws = create_workspace(root, PASSWORD)
    record = _register_monthly(ws, root)
    (root / "provenance" / f"{record.provenance_id}.enc").unlink()
    with pytest.raises(ArtifactIntegrityError):
        ws.verified_provenance_path(record.provenance_id)


def test_verified_provenance_path_hash_mismatch(tmp_path):
    root = tmp_path / "ws"
    ws = create_workspace(root, PASSWORD)
    record = _register_monthly(ws, root)
    target = root / "provenance" / f"{record.provenance_id}.enc"
    target.write_bytes(target.read_bytes() + b"\x00tampered")
    with pytest.raises(ArtifactIntegrityError):
        ws.verified_provenance_path(record.provenance_id)


def test_verified_provenance_path_directory_instead_of_file(tmp_path):
    root = tmp_path / "ws"
    ws = create_workspace(root, PASSWORD)
    record = _register_monthly(ws, root)
    target = root / "provenance" / f"{record.provenance_id}.enc"
    target.unlink()
    target.mkdir()
    with pytest.raises(ArtifactIntegrityError):
        ws.verified_provenance_path(record.provenance_id)


def test_verified_provenance_path_reparse_like(tmp_path, monkeypatch):
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
    with pytest.raises(ArtifactIntegrityError):
        ws.verified_provenance_path(record.provenance_id)


def test_verified_provenance_path_hash_read_failure_maps_to_integrity_error(tmp_path, monkeypatch):
    root = tmp_path / "ws"
    ws = create_workspace(root, PASSWORD)
    record = _register_monthly(ws, root)

    import app.workspace.workspace as workspace_module

    def failing_sha256_file(path):
        raise OSError("simulated I/O failure")

    monkeypatch.setattr(workspace_module, "sha256_file", failing_sha256_file)
    with pytest.raises(ArtifactIntegrityError) as excinfo:
        ws.verified_provenance_path(record.provenance_id)
    assert excinfo.value.__cause__ is None
    assert excinfo.value.__context__ is None


def test_verified_provenance_path_works_during_pending_store_mutation(tmp_path):
    root = tmp_path / "ws"
    ws = create_workspace(root, PASSWORD)
    record = _register_monthly(ws, root)

    import app.workspace.workspace as workspace_module

    manifest = workspace_module.load_encrypted_manifest(root / "workspace.enc", PASSWORD)
    pending_manifest = dataclasses.replace(manifest, revision=manifest.revision + 1, pending_store_mutation=True)
    workspace_module.save_encrypted_manifest_atomic(root / "workspace.enc", pending_manifest, PASSWORD)

    ws2 = open_workspace(root, PASSWORD)
    assert ws2.recovery_required is True
    path = ws2.verified_provenance_path(record.provenance_id)
    assert path.exists()


# ---------------------------------------------------------------------------
# F. latest_analytical_artifact
# ---------------------------------------------------------------------------


def test_latest_analytical_artifact_none_on_fresh_workspace(tmp_path):
    ws = create_workspace(tmp_path / "ws", PASSWORD)
    assert ws.latest_analytical_artifact() is None


def test_latest_analytical_artifact_returns_exact_record_after_set(tmp_path):
    root = tmp_path / "ws"
    ws = create_workspace(root, PASSWORD)
    canon = _register_canonical(ws, root)
    ws.set_latest_analytical(canon.artifact_id)
    assert ws.latest_analytical_artifact() == canon


def test_latest_analytical_artifact_does_not_mutate_manifest(tmp_path):
    root = tmp_path / "ws"
    ws = create_workspace(root, PASSWORD)
    canon = _register_canonical(ws, root)
    ws.set_latest_analytical(canon.artifact_id)
    revision_before = ws.info.revision
    raw_before = (root / "workspace.enc").read_bytes()
    ws.latest_analytical_artifact()
    assert ws.info.revision == revision_before
    assert (root / "workspace.enc").read_bytes() == raw_before


def test_latest_analytical_artifact_does_not_verify_physical_bytes(tmp_path):
    root = tmp_path / "ws"
    ws = create_workspace(root, PASSWORD)
    canon = _register_canonical(ws, root)
    ws.set_latest_analytical(canon.artifact_id)
    (root / "safe" / f"{canon.artifact_id}.xlsx").unlink()
    # getter — чистый metadata-lookup, физический файл не проверяется.
    record = ws.latest_analytical_artifact()
    assert record == canon


def test_latest_analytical_artifact_works_during_pending_store_mutation(tmp_path):
    root = tmp_path / "ws"
    ws = create_workspace(root, PASSWORD)
    canon = _register_canonical(ws, root)
    ws.set_latest_analytical(canon.artifact_id)

    import app.workspace.workspace as workspace_module

    manifest = workspace_module.load_encrypted_manifest(root / "workspace.enc", PASSWORD)
    pending_manifest = dataclasses.replace(manifest, revision=manifest.revision + 1, pending_store_mutation=True)
    workspace_module.save_encrypted_manifest_atomic(root / "workspace.enc", pending_manifest, PASSWORD)

    ws2 = open_workspace(root, PASSWORD)
    assert ws2.recovery_required is True
    assert ws2.latest_analytical_artifact().artifact_id == canon.artifact_id


# ---------------------------------------------------------------------------
# G. set_latest_analytical — успех / errors
# ---------------------------------------------------------------------------


def test_set_latest_analytical_success_returns_record(tmp_path):
    root = tmp_path / "ws"
    ws = create_workspace(root, PASSWORD)
    canon = _register_canonical(ws, root)
    result = ws.set_latest_analytical(canon.artifact_id)
    assert result == canon


def test_set_latest_analytical_rejects_non_str_id(tmp_path):
    ws = create_workspace(tmp_path / "ws", PASSWORD)
    with pytest.raises(WorkspaceInputError):
        ws.set_latest_analytical(12345)  # type: ignore[arg-type]


def test_set_latest_analytical_unknown_id(tmp_path):
    ws = create_workspace(tmp_path / "ws", PASSWORD)
    with pytest.raises(ArtifactNotFoundError):
        ws.set_latest_analytical("f" * 32)


def test_set_latest_analytical_rejects_monthly(tmp_path):
    root = tmp_path / "ws"
    ws = create_workspace(root, PASSWORD)
    monthly = _register_monthly(ws, root)
    with pytest.raises(ArtifactRegistrationError) as excinfo:
        ws.set_latest_analytical(monthly.artifact_id)
    assert excinfo.value.reason is ArtifactRegistrationReason.POINTER_KIND_INVALID


def test_set_latest_analytical_rejects_local_restored(tmp_path):
    root = tmp_path / "ws"
    ws = create_workspace(root, PASSWORD)
    monthly = _register_monthly(ws, root)
    local = _register_local_restored(ws, parent=monthly, period=monthly.period)
    with pytest.raises(ArtifactRegistrationError) as excinfo:
        ws.set_latest_analytical(local.artifact_id)
    assert excinfo.value.reason is ArtifactRegistrationReason.POINTER_KIND_INVALID


# ---------------------------------------------------------------------------
# H. Same-pointer / explicit rollback (OD-10B4.2-1 / OD-10B4-1)
# ---------------------------------------------------------------------------


def test_set_latest_analytical_same_pointer_is_still_a_mutation(tmp_path):
    root = tmp_path / "ws"
    ws = create_workspace(root, PASSWORD)
    canon = _register_canonical(ws, root)
    ws.set_latest_analytical(canon.artifact_id)
    revision_after_first = ws.info.revision

    result = ws.set_latest_analytical(canon.artifact_id)
    assert result == canon
    assert ws.info.revision == revision_after_first + 1


def test_set_latest_analytical_explicit_rollback_to_earlier_period_allowed(tmp_path):
    root = tmp_path / "ws"
    ws = create_workspace(root, PASSWORD)
    canon_old = _register_canonical(ws, root, period="2026-08", marker="old")
    canon_new = _register_canonical(ws, root, period="2026-09", marker="new")

    ws.set_latest_analytical(canon_new.artifact_id)
    assert ws.latest_analytical_artifact().artifact_id == canon_new.artifact_id

    # Явный rollback на более ранний period — разрешён (OD-10B4-1),
    # period не сравнивается автоматически.
    result = ws.set_latest_analytical(canon_old.artifact_id)
    assert result == canon_old
    assert ws.latest_analytical_artifact().artifact_id == canon_old.artifact_id


# ---------------------------------------------------------------------------
# I. Pending/recovery поведение (OD-10B4.2-3)
# ---------------------------------------------------------------------------


def test_set_latest_analytical_rejects_pending_store_mutation(tmp_path):
    root = tmp_path / "ws"
    ws = create_workspace(root, PASSWORD)
    canon = _register_canonical(ws, root)

    import app.workspace.workspace as workspace_module

    manifest = workspace_module.load_encrypted_manifest(root / "workspace.enc", PASSWORD)
    pending_manifest = dataclasses.replace(manifest, revision=manifest.revision + 1, pending_store_mutation=True)
    workspace_module.save_encrypted_manifest_atomic(root / "workspace.enc", pending_manifest, PASSWORD)

    ws2 = open_workspace(root, PASSWORD)
    with pytest.raises(WorkspaceBindingError) as excinfo:
        ws2.set_latest_analytical(canon.artifact_id)
    assert excinfo.value.reason is WorkspaceBindingReason.RECOVERY_REQUIRED


def test_allocate_provenance_path_works_during_pending_store_mutation(tmp_path):
    root = tmp_path / "ws"
    ws = create_workspace(root, PASSWORD)

    import app.workspace.workspace as workspace_module

    manifest = workspace_module.load_encrypted_manifest(root / "workspace.enc", PASSWORD)
    pending_manifest = dataclasses.replace(manifest, revision=manifest.revision + 1, pending_store_mutation=True)
    workspace_module.save_encrypted_manifest_atomic(root / "workspace.enc", pending_manifest, PASSWORD)

    ws2 = open_workspace(root, PASSWORD)
    assert ws2.recovery_required is True
    slot = ws2.allocate_provenance_path()
    assert isinstance(slot, ProvenanceSlot)


# ---------------------------------------------------------------------------
# J. Read locking (OD-10B4.2-2) — read-only без lock
# ---------------------------------------------------------------------------


def test_verified_and_latest_methods_do_not_require_lock(tmp_path):
    """
    verified_artifact_path/verified_provenance_path/latest_analytical_artifact
    не берут workspace.lock — вызываются успешно, пока ДРУГОЙ Workspace-
    объект того же процесса держит лок (что было бы NESTED_ACQUISITION
    для любого метода, реально претендующего на лок).
    """
    root = tmp_path / "ws"
    w1 = create_workspace(root, PASSWORD)
    record = _register_monthly(w1, root)
    canon = _register_canonical(w1, root, period="2026-02", marker="c2")
    w1.set_latest_analytical(canon.artifact_id)

    w2 = open_workspace(root, PASSWORD)
    with w1.lock():
        # Если бы эти методы брали workspace.lock, здесь была бы
        # WorkspaceLockedError(NESTED_ACQUISITION).
        assert w2.verified_artifact_path(record.artifact_id).exists()
        assert w2.verified_provenance_path(record.provenance_id).exists()
        assert w2.latest_analytical_artifact().artifact_id == canon.artifact_id


# ---------------------------------------------------------------------------
# K. Stale-object поведение
# ---------------------------------------------------------------------------


def test_stale_object_a_registers_b_sets_latest(tmp_path):
    root = tmp_path / "ws"
    w1 = create_workspace(root, PASSWORD)
    w2 = open_workspace(root, PASSWORD)

    canon = _register_canonical(w1, root)
    # w2 всё ещё держит устаревший (до регистрации canon) manifest в
    # кэше, но set_latest_analytical() обязан перечитать актуальный
    # manifest под локом перед мутацией.
    result = w2.set_latest_analytical(canon.artifact_id)
    assert result == canon


def test_stale_object_no_lost_update_between_two_setters(tmp_path):
    root = tmp_path / "ws"
    w1 = create_workspace(root, PASSWORD)
    w2 = open_workspace(root, PASSWORD)

    canon_x = _register_canonical(w1, root, period="2026-01", marker="x")
    canon_y = _register_canonical(w1, root, period="2026-02", marker="y")

    w1.set_latest_analytical(canon_x.artifact_id)
    w2.set_latest_analytical(canon_y.artifact_id)

    final = open_workspace(root, PASSWORD)
    assert final.latest_analytical_artifact().artifact_id == canon_y.artifact_id
    ids = {r.artifact_id for r in final.list_artifacts()}
    # Оба canonical-артефакта (и их родительские monthly) сохранены —
    # ни один не потерян конкурентными вызовами set_latest_analytical.
    assert {canon_x.artifact_id, canon_y.artifact_id} <= ids


def test_stale_object_latest_getter_sees_change_via_disk_reload(tmp_path):
    root = tmp_path / "ws"
    w1 = create_workspace(root, PASSWORD)
    w2 = open_workspace(root, PASSWORD)

    canon = _register_canonical(w1, root)
    assert w2.latest_analytical_artifact() is None  # w2 ещё не перечитывал

    w1.set_latest_analytical(canon.artifact_id)
    assert w2.latest_analytical_artifact().artifact_id == canon.artifact_id


def test_stale_object_verified_artifact_path_sees_new_registration(tmp_path):
    root = tmp_path / "ws"
    w1 = create_workspace(root, PASSWORD)
    w2 = open_workspace(root, PASSWORD)

    record = _register_monthly(w1, root)
    path = w2.verified_artifact_path(record.artifact_id)
    assert path.exists()


# ---------------------------------------------------------------------------
# L. Failure atomicity (set_latest_analytical)
# ---------------------------------------------------------------------------


def test_set_latest_analytical_save_failure_is_atomic(tmp_path, monkeypatch):
    root = tmp_path / "ws"
    ws = create_workspace(root, PASSWORD)
    canon_x = _register_canonical(ws, root, period="2026-01", marker="x")
    canon_y = _register_canonical(ws, root, period="2026-02", marker="y")

    ws.set_latest_analytical(canon_x.artifact_id)
    revision_before = ws.info.revision
    raw_before = (root / "workspace.enc").read_bytes()

    import app.workspace.workspace as workspace_module

    def failing_save(*args, **kwargs):
        raise OSError("simulated disk failure")

    monkeypatch.setattr(workspace_module, "save_encrypted_manifest_atomic", failing_save)
    with pytest.raises(OSError):
        ws.set_latest_analytical(canon_y.artifact_id)
    monkeypatch.undo()

    # disk manifest не тронут
    assert (root / "workspace.enc").read_bytes() == raw_before
    # self._manifest на объекте не тронут
    assert ws.info.revision == revision_before
    assert ws.latest_analytical_artifact().artifact_id == canon_x.artifact_id
    # lock освобождён — немедленный retry успешен
    result = ws.set_latest_analytical(canon_y.artifact_id)
    assert result == canon_y
    assert ws.latest_analytical_artifact().artifact_id == canon_y.artifact_id


# ---------------------------------------------------------------------------
# M. Revision semantics
# ---------------------------------------------------------------------------


def test_revision_unchanged_by_all_read_only_methods(tmp_path):
    root = tmp_path / "ws"
    ws = create_workspace(root, PASSWORD)
    record = _register_monthly(ws, root)
    canon = _register_canonical(ws, root, period="2026-02", marker="c2")
    ws.set_latest_analytical(canon.artifact_id)

    revision_before = ws.info.revision
    ws.allocate_provenance_path()
    ws.verified_artifact_path(record.artifact_id)
    ws.verified_provenance_path(record.provenance_id)
    ws.latest_analytical_artifact()
    assert ws.info.revision == revision_before


def test_revision_increments_by_one_per_set_latest_call(tmp_path):
    root = tmp_path / "ws"
    ws = create_workspace(root, PASSWORD)
    canon_x = _register_canonical(ws, root, period="2026-01", marker="x")
    canon_y = _register_canonical(ws, root, period="2026-02", marker="y")

    r0 = ws.info.revision
    ws.set_latest_analytical(canon_x.artifact_id)
    r1 = ws.info.revision
    assert r1 == r0 + 1

    ws.set_latest_analytical(canon_x.artifact_id)  # same pointer
    r2 = ws.info.revision
    assert r2 == r1 + 1

    ws.set_latest_analytical(canon_y.artifact_id)  # rollback/switch
    r3 = ws.info.revision
    assert r3 == r2 + 1


# ---------------------------------------------------------------------------
# N. Security boundary (verified != external-AI-safe)
# ---------------------------------------------------------------------------


def test_local_restored_passes_verified_artifact_path_but_is_not_ai_safe(tmp_path):
    """
    Намеренная граница: LOCAL_RESTORED (заведомо НЕ AI-safe) успешно
    проходит verified_artifact_path — verified означает только
    "физические байты соответствуют зарегистрированному SHA", НЕ
    "safe для внешнего AI". Workspace не предоставляет и не обязан
    предоставлять can_upload/is_safe/authorize где-либо в этом API.
    """
    root = tmp_path / "ws"
    ws = create_workspace(root, PASSWORD)
    monthly = _register_monthly(ws, root)
    local = _register_local_restored(ws, parent=monthly, period=monthly.period)

    path = ws.verified_artifact_path(local.artifact_id)
    assert path.exists()
    assert not hasattr(ws, "can_upload")
    assert not hasattr(ws, "is_safe")
    assert not hasattr(ws, "authorize")


# ---------------------------------------------------------------------------
# O. OD-7: конфиденциальность
# ---------------------------------------------------------------------------


def test_od7_verified_artifact_path_manifest_load_failure_no_password_leak(tmp_path):
    root = tmp_path / "ws"
    ws = create_workspace(root, _PASSWORD_SENTINEL_10B4_2)
    record = _register_monthly_for(ws, root, _PASSWORD_SENTINEL_10B4_2)
    (root / "workspace.enc").write_bytes(b"corrupted, not a valid container")

    with pytest.raises(Exception) as excinfo:
        ws.verified_artifact_path(record.artifact_id)
    assert _internal_frame_locals_containing(excinfo.value, (_PASSWORD_SENTINEL_10B4_2,)) == []


def test_od7_verified_provenance_path_manifest_load_failure_no_password_leak(tmp_path):
    root = tmp_path / "ws"
    ws = create_workspace(root, _PASSWORD_SENTINEL_10B4_2)
    record = _register_monthly_for(ws, root, _PASSWORD_SENTINEL_10B4_2)
    (root / "workspace.enc").write_bytes(b"corrupted, not a valid container")

    with pytest.raises(Exception) as excinfo:
        ws.verified_provenance_path(record.provenance_id)
    assert _internal_frame_locals_containing(excinfo.value, (_PASSWORD_SENTINEL_10B4_2,)) == []


def test_od7_latest_getter_manifest_load_failure_no_password_leak(tmp_path):
    root = tmp_path / "ws"
    ws = create_workspace(root, _PASSWORD_SENTINEL_10B4_2)
    canon = _register_canonical(ws, root, password=_PASSWORD_SENTINEL_10B4_2)
    ws.set_latest_analytical(canon.artifact_id)
    (root / "workspace.enc").write_bytes(b"corrupted, not a valid container")

    with pytest.raises(Exception) as excinfo:
        ws.latest_analytical_artifact()
    assert _internal_frame_locals_containing(excinfo.value, (_PASSWORD_SENTINEL_10B4_2,)) == []


def test_od7_set_latest_analytical_manifest_load_failure_no_password_leak(tmp_path):
    root = tmp_path / "ws"
    ws = create_workspace(root, _PASSWORD_SENTINEL_10B4_2)
    canon = _register_canonical(ws, root, password=_PASSWORD_SENTINEL_10B4_2)
    (root / "workspace.enc").write_bytes(b"corrupted, not a valid container")

    with pytest.raises(Exception) as excinfo:
        ws.set_latest_analytical(canon.artifact_id)
    assert _internal_frame_locals_containing(excinfo.value, (_PASSWORD_SENTINEL_10B4_2,)) == []


def test_od7_set_latest_analytical_save_failure_no_password_leak(tmp_path, monkeypatch):
    root = tmp_path / "ws"
    ws = create_workspace(root, _PASSWORD_SENTINEL_10B4_2)
    canon = _register_canonical(ws, root, password=_PASSWORD_SENTINEL_10B4_2)

    import app.workspace.workspace as workspace_module

    def failing_save(*args, **kwargs):
        raise OSError("simulated disk failure")

    monkeypatch.setattr(workspace_module, "save_encrypted_manifest_atomic", failing_save)
    with pytest.raises(OSError) as excinfo:
        ws.set_latest_analytical(canon.artifact_id)
    assert _internal_frame_locals_containing(excinfo.value, (_PASSWORD_SENTINEL_10B4_2,)) == []
