"""
Тесты Stage 10B.4.1: allocate_artifact_path / stage_analytical_file /
register_artifact / get_artifact / list_artifacts.

Группы:
    A. allocate_artifact_path
    B. stage_analytical_file
    C. register_artifact — успешная регистрация всех 3 kind
    D. register_artifact — slot/file
    E. register_artifact — SHA uniqueness
    F. register_artifact — evidence (SafetyReport)
    G. register_artifact — period
    H. register_artifact — lineage
    I. register_artifact — provenance/job_id (ANONYMIZED_MONTHLY)
    J. register_artifact — recovery_required gate
    K. get_artifact / list_artifacts
    L. Revision semantics
    M. Stale Workspace objects
    N. Concurrency
    O. OD-7: конфиденциальность провenance-ошибок
"""

from __future__ import annotations

import dataclasses
import os
import shutil
import threading

import openpyxl
import pytest

from app.mapping.provenance_encrypted import EncryptedFileProvenanceStore
from app.safety.external_ai import FindingKind, InspectionCheck, SafetyReport, sha256_file
from app.workspace import create_workspace, open_workspace
from app.workspace.errors import (
    ArtifactNotFoundError,
    ArtifactRegistrationError,
    ArtifactRegistrationReason,
    WorkspaceBindingError,
    WorkspaceBindingReason,
    WorkspaceInputError,
)
from app.workspace.models import ArtifactKind, ArtifactRecord, ArtifactSlot

PASSWORD = "workspace-password-10b4-1"

M = ArtifactKind.ANONYMIZED_MONTHLY
C = ArtifactKind.ANALYTICAL_CANONICAL
L = ArtifactKind.LOCAL_RESTORED

_JOB_ID_A = "a" * 32
_PROV_ID_A = "b" * 32


def _make_xlsx(path, marker: str = "x") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    wb = openpyxl.Workbook()
    wb.active["A1"] = marker
    wb.save(str(path))


def _report_for(path, *, blocking: bool = False) -> SafetyReport:
    finding_counts = ((FindingKind.RESTORED_MARKER, 1),) if blocking else ()
    return SafetyReport(
        sha256=sha256_file(path),
        worksheet_count=1,
        indexed_entity_values=0,
        indexed_identifier_values=0,
        checks_performed=(InspectionCheck.CUSTOM_PROPERTIES,),
        not_inspected=(),
        finding_counts=finding_counts,
        findings=(),
        findings_truncated=False,
    )


def _make_provenance(root, provenance_id: str, job_id: str, password: str = PASSWORD) -> None:
    path = root / "provenance" / f"{provenance_id}.enc"
    EncryptedFileProvenanceStore(path, password, job_id=job_id)


def _register_monthly(ws, root, *, period="2026-01", marker="m", job_id=_JOB_ID_A, provenance_id=_PROV_ID_A, blocking=False):
    slot = ws.allocate_artifact_path(M)
    _make_xlsx(slot.path, marker)
    _make_provenance(root, provenance_id, job_id)
    return ws.register_artifact(
        slot,
        period=period,
        source_job_id=job_id,
        provenance_id=provenance_id,
        safety_report=_report_for(slot.path, blocking=blocking),
    )


# ---------------------------------------------------------------------------
# A. allocate_artifact_path
# ---------------------------------------------------------------------------


def test_allocate_artifact_path_each_kind(tmp_path):
    root = tmp_path / "ws"
    ws = create_workspace(root, PASSWORD)
    for kind, expected_dir in ((M, "safe"), (C, "safe"), (L, "local_plaintext")):
        slot = ws.allocate_artifact_path(kind)
        assert isinstance(slot, ArtifactSlot)
        assert slot.kind is kind
        assert slot.path.parent == root / expected_dir
        assert slot.path.suffix == ".xlsx"
        assert not slot.path.exists()
        assert len(slot.artifact_id) == 32


def test_allocate_artifact_path_does_not_mutate_manifest(tmp_path):
    root = tmp_path / "ws"
    ws = create_workspace(root, PASSWORD)
    revision_before = ws.info.revision
    ws.allocate_artifact_path(M)
    assert ws.info.revision == revision_before


def test_allocate_artifact_path_rejects_invalid_kind(tmp_path):
    ws = create_workspace(tmp_path / "ws", PASSWORD)
    with pytest.raises(WorkspaceInputError):
        ws.allocate_artifact_path("anonymized_monthly")  # type: ignore[arg-type]


def test_allocate_artifact_path_retries_on_collision(tmp_path, monkeypatch):
    root = tmp_path / "ws"
    ws = create_workspace(root, PASSWORD)

    import app.workspace.workspace as workspace_module

    real_token_hex = workspace_module.secrets.token_hex
    forced_id = "c" * 32
    colliding_path = root / "safe" / f"{forced_id}.xlsx"
    _make_xlsx(colliding_path)

    call_count = {"n": 0}

    def fake_token_hex(n):
        call_count["n"] += 1
        if call_count["n"] == 1:
            return forced_id  # уже занято -> должен последовать retry
        return real_token_hex(n)

    monkeypatch.setattr(workspace_module.secrets, "token_hex", fake_token_hex)
    slot = ws.allocate_artifact_path(M)
    assert slot.artifact_id != forced_id
    assert call_count["n"] >= 2


def test_allocate_artifact_path_gives_up_after_max_attempts(tmp_path, monkeypatch):
    root = tmp_path / "ws"
    ws = create_workspace(root, PASSWORD)

    import app.workspace.workspace as workspace_module

    fixed_id = "d" * 32
    _make_xlsx(root / "safe" / f"{fixed_id}.xlsx")
    monkeypatch.setattr(workspace_module.secrets, "token_hex", lambda n: fixed_id)

    with pytest.raises(WorkspaceInputError):
        ws.allocate_artifact_path(M)


# ---------------------------------------------------------------------------
# B. stage_analytical_file
# ---------------------------------------------------------------------------


def test_stage_analytical_file_success(tmp_path):
    root = tmp_path / "ws"
    ws = create_workspace(root, PASSWORD)
    external = tmp_path / "external.xlsx"
    _make_xlsx(external, "ext")

    slot = ws.stage_analytical_file(external)
    assert slot.kind is C
    assert slot.path.exists()
    assert sha256_file(slot.path) == sha256_file(external)
    # source не изменён/не удалён
    assert external.exists()


def test_stage_analytical_file_rejects_wrong_extension(tmp_path):
    root = tmp_path / "ws"
    ws = create_workspace(root, PASSWORD)
    external = tmp_path / "external.csv"
    external.write_bytes(b"not xlsx")
    with pytest.raises(WorkspaceInputError):
        ws.stage_analytical_file(external)


def test_stage_analytical_file_rejects_missing_source(tmp_path):
    ws = create_workspace(tmp_path / "ws", PASSWORD)
    with pytest.raises(WorkspaceInputError):
        ws.stage_analytical_file(tmp_path / "nope.xlsx")


def test_stage_analytical_file_rejects_directory_source(tmp_path):
    ws = create_workspace(tmp_path / "ws", PASSWORD)
    directory = tmp_path / "adir.xlsx"
    directory.mkdir()
    with pytest.raises(WorkspaceInputError):
        ws.stage_analytical_file(directory)


def test_stage_analytical_file_rejects_source_inside_workspace(tmp_path):
    root = tmp_path / "ws"
    ws = create_workspace(root, PASSWORD)
    inside = root / "safe" / "already_here.xlsx"
    _make_xlsx(inside)
    with pytest.raises(WorkspaceInputError):
        ws.stage_analytical_file(inside)


def test_stage_analytical_file_reparse_source_rejected(tmp_path, monkeypatch):
    root = tmp_path / "ws"
    ws = create_workspace(root, PASSWORD)
    external = tmp_path / "external.xlsx"
    _make_xlsx(external)

    import app.workspace.workspace as workspace_module

    real_check = workspace_module._is_reparse_like
    target_resolved = external.resolve()

    def fake_check(path):
        if path.resolve() == target_resolved:
            return True
        return real_check(path)

    monkeypatch.setattr(workspace_module, "_is_reparse_like", fake_check)
    with pytest.raises(WorkspaceInputError):
        ws.stage_analytical_file(external)


# ---------------------------------------------------------------------------
# C. register_artifact — успешная регистрация всех 3 kind
# ---------------------------------------------------------------------------


def test_register_monthly_success(tmp_path):
    root = tmp_path / "ws"
    ws = create_workspace(root, PASSWORD)
    record = _register_monthly(ws, root)
    assert isinstance(record, ArtifactRecord)
    assert record.kind is M
    assert record.parent_artifact_ids == ()
    assert record.source_job_id == _JOB_ID_A
    assert record.provenance_id == _PROV_ID_A
    assert record.provenance_sha256 is not None
    assert ws.info.artifact_count == 1


def test_register_canonical_with_monthly_parent(tmp_path):
    root = tmp_path / "ws"
    ws = create_workspace(root, PASSWORD)
    monthly = _register_monthly(ws, root)

    slot = ws.allocate_artifact_path(C)
    _make_xlsx(slot.path, "canon")
    record = ws.register_artifact(
        slot, period="2026-01", parent_artifact_ids=(monthly.artifact_id,),
        safety_report=_report_for(slot.path),
    )
    assert record.kind is C
    assert record.parent_artifact_ids == (monthly.artifact_id,)
    assert record.source_job_id is None
    assert record.provenance_id is None


def test_register_canonical_with_canonical_prev_only(tmp_path):
    root = tmp_path / "ws"
    ws = create_workspace(root, PASSWORD)
    monthly = _register_monthly(ws, root)
    slot1 = ws.allocate_artifact_path(C)
    _make_xlsx(slot1.path, "c1")
    canon1 = ws.register_artifact(
        slot1, period="2026-01", parent_artifact_ids=(monthly.artifact_id,),
        safety_report=_report_for(slot1.path),
    )

    slot2 = ws.allocate_artifact_path(C)
    _make_xlsx(slot2.path, "c2")
    canon2 = ws.register_artifact(
        slot2, period="2026-01", parent_artifact_ids=(canon1.artifact_id,),
        safety_report=_report_for(slot2.path),
    )
    assert canon2.parent_artifact_ids == (canon1.artifact_id,)


def test_register_canonical_with_both_parents_correct_order(tmp_path):
    root = tmp_path / "ws"
    ws = create_workspace(root, PASSWORD)
    monthly1 = _register_monthly(ws, root, period="2026-01", marker="m1", provenance_id="1" * 32, job_id="2" * 32)
    slot1 = ws.allocate_artifact_path(C)
    _make_xlsx(slot1.path, "c1")
    canon1 = ws.register_artifact(
        slot1, period="2026-01", parent_artifact_ids=(monthly1.artifact_id,),
        safety_report=_report_for(slot1.path),
    )

    monthly2 = _register_monthly(ws, root, period="2026-02", marker="m2", provenance_id="3" * 32, job_id="4" * 32)
    slot2 = ws.allocate_artifact_path(C)
    _make_xlsx(slot2.path, "c2")
    canon2 = ws.register_artifact(
        slot2, period="2026-02", parent_artifact_ids=(canon1.artifact_id, monthly2.artifact_id),
        safety_report=_report_for(slot2.path),
    )
    assert canon2.parent_artifact_ids == (canon1.artifact_id, monthly2.artifact_id)


def test_register_local_restored_from_monthly(tmp_path):
    root = tmp_path / "ws"
    ws = create_workspace(root, PASSWORD)
    monthly = _register_monthly(ws, root)
    slot = ws.allocate_artifact_path(L)
    _make_xlsx(slot.path, "local")
    record = ws.register_artifact(slot, period="2026-01", parent_artifact_ids=(monthly.artifact_id,))
    assert record.kind is L
    assert record.source_job_id is None
    assert record.provenance_id is None


def test_register_local_restored_from_canonical(tmp_path):
    root = tmp_path / "ws"
    ws = create_workspace(root, PASSWORD)
    monthly = _register_monthly(ws, root)
    canon_slot = ws.allocate_artifact_path(C)
    _make_xlsx(canon_slot.path, "canon")
    canon = ws.register_artifact(
        canon_slot, period="2026-01", parent_artifact_ids=(monthly.artifact_id,),
        safety_report=_report_for(canon_slot.path),
    )
    local_slot = ws.allocate_artifact_path(L)
    _make_xlsx(local_slot.path, "local2")
    record = ws.register_artifact(local_slot, period="2026-01", parent_artifact_ids=(canon.artifact_id,))
    assert record.parent_artifact_ids == (canon.artifact_id,)


# ---------------------------------------------------------------------------
# D. register_artifact — slot/file
# ---------------------------------------------------------------------------


def test_register_rejects_wrong_slot_type(tmp_path):
    ws = create_workspace(tmp_path / "ws", PASSWORD)
    with pytest.raises(WorkspaceInputError):
        ws.register_artifact("not-a-slot", period="2026-01")  # type: ignore[arg-type]


def test_register_rejects_mismatched_slot_path(tmp_path):
    root = tmp_path / "ws"
    ws = create_workspace(root, PASSWORD)
    slot = ws.allocate_artifact_path(M)
    _make_xlsx(slot.path)
    tampered = dataclasses.replace(slot, path=root / "safe" / "other-name.xlsx")
    _make_xlsx(tampered.path)
    with pytest.raises(ArtifactRegistrationError) as excinfo:
        ws.register_artifact(
            tampered, period="2026-01", source_job_id=_JOB_ID_A, provenance_id=_PROV_ID_A,
            safety_report=_report_for(tampered.path),
        )
    assert excinfo.value.reason is ArtifactRegistrationReason.SLOT_INVALID


def test_register_rejects_missing_file(tmp_path):
    root = tmp_path / "ws"
    ws = create_workspace(root, PASSWORD)
    slot = ws.allocate_artifact_path(M)
    # Файл никогда не создан.
    with pytest.raises(ArtifactRegistrationError) as excinfo:
        ws.register_artifact(
            slot, period="2026-01", source_job_id=_JOB_ID_A, provenance_id=_PROV_ID_A,
            safety_report=_dummy_report(),
        )
    assert excinfo.value.reason is ArtifactRegistrationReason.FILE_INVALID


def _dummy_report() -> SafetyReport:
    return SafetyReport(
        sha256="a" * 64, worksheet_count=1, indexed_entity_values=0,
        indexed_identifier_values=0, checks_performed=(), not_inspected=(),
        finding_counts=(), findings=(), findings_truncated=False,
    )


def test_register_reparse_file_rejected(tmp_path, monkeypatch):
    root = tmp_path / "ws"
    ws = create_workspace(root, PASSWORD)
    slot = ws.allocate_artifact_path(M)
    _make_xlsx(slot.path)

    import app.workspace.workspace as workspace_module

    real_check = workspace_module._is_reparse_like
    target_resolved = slot.path.resolve()

    def fake_check(path):
        if path.resolve() == target_resolved:
            return True
        return real_check(path)

    monkeypatch.setattr(workspace_module, "_is_reparse_like", fake_check)
    with pytest.raises(ArtifactRegistrationError) as excinfo:
        ws.register_artifact(
            slot, period="2026-01", source_job_id=_JOB_ID_A, provenance_id=_PROV_ID_A,
            safety_report=_report_for(slot.path),
        )
    assert excinfo.value.reason is ArtifactRegistrationReason.FILE_INVALID


# ---------------------------------------------------------------------------
# E. register_artifact — SHA uniqueness
# ---------------------------------------------------------------------------


def test_register_rejects_duplicate_sha256(tmp_path):
    root = tmp_path / "ws"
    ws = create_workspace(root, PASSWORD)
    first = _register_monthly(ws, root, period="2026-01", marker="same")
    first_path = root / "safe" / f"{first.artifact_id}.xlsx"

    slot2 = ws.allocate_artifact_path(M)
    # Байт-в-байт копия (не regenerate через openpyxl -- Workbook
    # встраивает created/modified timestamp, поэтому "тот же marker" не
    # гарантирует тот же SHA между двумя отдельными save()).
    shutil.copy(str(first_path), str(slot2.path))
    _make_provenance(root, "9" * 32, "8" * 32)
    with pytest.raises(ArtifactRegistrationError) as excinfo:
        ws.register_artifact(
            slot2, period="2026-02", source_job_id="8" * 32, provenance_id="9" * 32,
            safety_report=_report_for(slot2.path),
        )
    assert excinfo.value.reason is ArtifactRegistrationReason.DUPLICATE_SHA256


def test_register_duplicate_sha256_across_kinds_rejected(tmp_path):
    root = tmp_path / "ws"
    ws = create_workspace(root, PASSWORD)
    monthly = _register_monthly(ws, root, marker="dup")
    monthly_path = root / "safe" / f"{monthly.artifact_id}.xlsx"

    canon_slot = ws.allocate_artifact_path(C)
    shutil.copy(str(monthly_path), str(canon_slot.path))  # тот же байты, ДРУГОЙ kind
    with pytest.raises(ArtifactRegistrationError) as excinfo:
        ws.register_artifact(
            canon_slot, period="2026-01", parent_artifact_ids=(monthly.artifact_id,),
            safety_report=_report_for(canon_slot.path),
        )
    assert excinfo.value.reason is ArtifactRegistrationReason.DUPLICATE_SHA256


# ---------------------------------------------------------------------------
# F. register_artifact — evidence (SafetyReport)
# ---------------------------------------------------------------------------


def test_register_monthly_requires_evidence(tmp_path):
    root = tmp_path / "ws"
    ws = create_workspace(root, PASSWORD)
    slot = ws.allocate_artifact_path(M)
    _make_xlsx(slot.path)
    _make_provenance(root, _PROV_ID_A, _JOB_ID_A)
    with pytest.raises(ArtifactRegistrationError) as excinfo:
        ws.register_artifact(slot, period="2026-01", source_job_id=_JOB_ID_A, provenance_id=_PROV_ID_A)
    assert excinfo.value.reason is ArtifactRegistrationReason.EVIDENCE_REQUIRED


def test_register_evidence_wrong_type_raises_type_error(tmp_path):
    root = tmp_path / "ws"
    ws = create_workspace(root, PASSWORD)
    slot = ws.allocate_artifact_path(M)
    _make_xlsx(slot.path)
    _make_provenance(root, _PROV_ID_A, _JOB_ID_A)
    with pytest.raises(TypeError):
        ws.register_artifact(
            slot, period="2026-01", source_job_id=_JOB_ID_A, provenance_id=_PROV_ID_A,
            safety_report="not-a-safety-report",  # type: ignore[arg-type]
        )


def test_register_evidence_hash_mismatch(tmp_path):
    root = tmp_path / "ws"
    ws = create_workspace(root, PASSWORD)
    slot = ws.allocate_artifact_path(M)
    _make_xlsx(slot.path)
    _make_provenance(root, _PROV_ID_A, _JOB_ID_A)
    wrong_report = _dummy_report()  # sha256 не совпадает с реальным файлом
    with pytest.raises(ArtifactRegistrationError) as excinfo:
        ws.register_artifact(
            slot, period="2026-01", source_job_id=_JOB_ID_A, provenance_id=_PROV_ID_A,
            safety_report=wrong_report,
        )
    assert excinfo.value.reason is ArtifactRegistrationReason.EVIDENCE_HASH_MISMATCH


def test_register_evidence_blocking_findings(tmp_path):
    root = tmp_path / "ws"
    ws = create_workspace(root, PASSWORD)
    slot = ws.allocate_artifact_path(M)
    _make_xlsx(slot.path)
    _make_provenance(root, _PROV_ID_A, _JOB_ID_A)
    with pytest.raises(ArtifactRegistrationError) as excinfo:
        ws.register_artifact(
            slot, period="2026-01", source_job_id=_JOB_ID_A, provenance_id=_PROV_ID_A,
            safety_report=_report_for(slot.path, blocking=True),
        )
    assert excinfo.value.reason is ArtifactRegistrationReason.EVIDENCE_BLOCKING_FINDINGS


def test_register_local_restored_rejects_evidence(tmp_path):
    root = tmp_path / "ws"
    ws = create_workspace(root, PASSWORD)
    monthly = _register_monthly(ws, root)
    slot = ws.allocate_artifact_path(L)
    _make_xlsx(slot.path, "local")
    with pytest.raises(ArtifactRegistrationError) as excinfo:
        ws.register_artifact(
            slot, period="2026-01", parent_artifact_ids=(monthly.artifact_id,),
            safety_report=_report_for(slot.path),
        )
    assert excinfo.value.reason is ArtifactRegistrationReason.EVIDENCE_NOT_ALLOWED


# ---------------------------------------------------------------------------
# G. register_artifact — period
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("bad_period", ["2026-13", "2026-00", "26-01", "2026/01", "2026-1", ""])
def test_register_rejects_invalid_period(tmp_path, bad_period):
    root = tmp_path / "ws"
    ws = create_workspace(root, PASSWORD)
    slot = ws.allocate_artifact_path(M)
    _make_xlsx(slot.path)
    _make_provenance(root, _PROV_ID_A, _JOB_ID_A)
    with pytest.raises(ArtifactRegistrationError) as excinfo:
        ws.register_artifact(
            slot, period=bad_period, source_job_id=_JOB_ID_A, provenance_id=_PROV_ID_A,
            safety_report=_report_for(slot.path),
        )
    assert excinfo.value.reason is ArtifactRegistrationReason.INVALID_PERIOD


# ---------------------------------------------------------------------------
# H. register_artifact — lineage
# ---------------------------------------------------------------------------


def test_register_monthly_rejects_parents(tmp_path):
    root = tmp_path / "ws"
    ws = create_workspace(root, PASSWORD)
    other = _register_monthly(ws, root, period="2026-01", marker="other", provenance_id="1" * 32, job_id="2" * 32)
    slot = ws.allocate_artifact_path(M)
    _make_xlsx(slot.path, "m2")
    _make_provenance(root, "3" * 32, "4" * 32)
    with pytest.raises(ArtifactRegistrationError) as excinfo:
        ws.register_artifact(
            slot, period="2026-02", parent_artifact_ids=(other.artifact_id,),
            source_job_id="4" * 32, provenance_id="3" * 32,
            safety_report=_report_for(slot.path),
        )
    assert excinfo.value.reason is ArtifactRegistrationReason.INVALID_LINEAGE


def test_register_canonical_rejects_missing_parent(tmp_path):
    root = tmp_path / "ws"
    ws = create_workspace(root, PASSWORD)
    slot = ws.allocate_artifact_path(C)
    _make_xlsx(slot.path)
    with pytest.raises(ArtifactRegistrationError) as excinfo:
        ws.register_artifact(
            slot, period="2026-01", parent_artifact_ids=("f" * 32,),
            safety_report=_report_for(slot.path),
        )
    assert excinfo.value.reason is ArtifactRegistrationReason.INVALID_LINEAGE


def test_register_canonical_rejects_self_parent(tmp_path):
    root = tmp_path / "ws"
    ws = create_workspace(root, PASSWORD)
    slot = ws.allocate_artifact_path(C)
    _make_xlsx(slot.path)
    with pytest.raises(ArtifactRegistrationError) as excinfo:
        ws.register_artifact(
            slot, period="2026-01", parent_artifact_ids=(slot.artifact_id,),
            safety_report=_report_for(slot.path),
        )
    assert excinfo.value.reason is ArtifactRegistrationReason.INVALID_LINEAGE


def test_register_canonical_rejects_duplicate_parent_ids(tmp_path):
    root = tmp_path / "ws"
    ws = create_workspace(root, PASSWORD)
    monthly = _register_monthly(ws, root)
    slot = ws.allocate_artifact_path(C)
    _make_xlsx(slot.path)
    with pytest.raises(ArtifactRegistrationError) as excinfo:
        ws.register_artifact(
            slot, period="2026-01",
            parent_artifact_ids=(monthly.artifact_id, monthly.artifact_id),
            safety_report=_report_for(slot.path),
        )
    assert excinfo.value.reason is ArtifactRegistrationReason.INVALID_LINEAGE


def test_register_canonical_rejects_two_monthly_parents(tmp_path):
    root = tmp_path / "ws"
    ws = create_workspace(root, PASSWORD)
    monthly1 = _register_monthly(ws, root, period="2026-01", marker="m1", provenance_id="1" * 32, job_id="2" * 32)
    monthly2 = _register_monthly(ws, root, period="2026-01", marker="m2", provenance_id="3" * 32, job_id="4" * 32)
    slot = ws.allocate_artifact_path(C)
    _make_xlsx(slot.path, "c")
    with pytest.raises(ArtifactRegistrationError) as excinfo:
        ws.register_artifact(
            slot, period="2026-01", parent_artifact_ids=(monthly1.artifact_id, monthly2.artifact_id),
            safety_report=_report_for(slot.path),
        )
    assert excinfo.value.reason is ArtifactRegistrationReason.INVALID_LINEAGE


def test_register_canonical_rejects_reversed_parent_order(tmp_path):
    root = tmp_path / "ws"
    ws = create_workspace(root, PASSWORD)
    monthly = _register_monthly(ws, root, period="2026-01", marker="m1")
    slot1 = ws.allocate_artifact_path(C)
    _make_xlsx(slot1.path, "c1")
    canon1 = ws.register_artifact(
        slot1, period="2026-01", parent_artifact_ids=(monthly.artifact_id,),
        safety_report=_report_for(slot1.path),
    )
    monthly2 = _register_monthly(ws, root, period="2026-02", marker="m2", provenance_id="5" * 32, job_id="6" * 32)
    slot2 = ws.allocate_artifact_path(C)
    _make_xlsx(slot2.path, "c2")
    # Неверный порядок: monthly ПЕРВЫМ, canonical ВТОРЫМ.
    with pytest.raises(ArtifactRegistrationError) as excinfo:
        ws.register_artifact(
            slot2, period="2026-02", parent_artifact_ids=(monthly2.artifact_id, canon1.artifact_id),
            safety_report=_report_for(slot2.path),
        )
    assert excinfo.value.reason is ArtifactRegistrationReason.INVALID_LINEAGE


def test_register_canonical_rejects_monthly_parent_wrong_period(tmp_path):
    root = tmp_path / "ws"
    ws = create_workspace(root, PASSWORD)
    monthly = _register_monthly(ws, root, period="2026-01", marker="m")
    slot = ws.allocate_artifact_path(C)
    _make_xlsx(slot.path)
    with pytest.raises(ArtifactRegistrationError) as excinfo:
        ws.register_artifact(
            slot, period="2026-02", parent_artifact_ids=(monthly.artifact_id,),
            safety_report=_report_for(slot.path),
        )
    assert excinfo.value.reason is ArtifactRegistrationReason.INVALID_LINEAGE


def test_register_canonical_rejects_future_period_canonical_parent(tmp_path):
    root = tmp_path / "ws"
    ws = create_workspace(root, PASSWORD)
    monthly = _register_monthly(ws, root, period="2026-05", marker="m")
    slot1 = ws.allocate_artifact_path(C)
    _make_xlsx(slot1.path, "c1")
    canon_future = ws.register_artifact(
        slot1, period="2026-05", parent_artifact_ids=(monthly.artifact_id,),
        safety_report=_report_for(slot1.path),
    )
    slot2 = ws.allocate_artifact_path(C)
    _make_xlsx(slot2.path, "c2")
    with pytest.raises(ArtifactRegistrationError) as excinfo:
        ws.register_artifact(
            slot2, period="2026-01", parent_artifact_ids=(canon_future.artifact_id,),
            safety_report=_report_for(slot2.path),
        )
    assert excinfo.value.reason is ArtifactRegistrationReason.INVALID_LINEAGE


def test_register_canonical_rejects_local_restored_parent(tmp_path):
    root = tmp_path / "ws"
    ws = create_workspace(root, PASSWORD)
    monthly = _register_monthly(ws, root)
    local_slot = ws.allocate_artifact_path(L)
    _make_xlsx(local_slot.path, "local")
    local = ws.register_artifact(local_slot, period="2026-01", parent_artifact_ids=(monthly.artifact_id,))

    slot = ws.allocate_artifact_path(C)
    _make_xlsx(slot.path)
    with pytest.raises(ArtifactRegistrationError) as excinfo:
        ws.register_artifact(
            slot, period="2026-01", parent_artifact_ids=(local.artifact_id,),
            safety_report=_report_for(slot.path),
        )
    assert excinfo.value.reason is ArtifactRegistrationReason.INVALID_LINEAGE


def test_register_local_restored_rejects_two_parents(tmp_path):
    root = tmp_path / "ws"
    ws = create_workspace(root, PASSWORD)
    monthly1 = _register_monthly(ws, root, period="2026-01", marker="m1")
    monthly2 = _register_monthly(ws, root, period="2026-02", marker="m2", provenance_id="7" * 32, job_id="8" * 32)
    slot = ws.allocate_artifact_path(L)
    _make_xlsx(slot.path)
    with pytest.raises(ArtifactRegistrationError) as excinfo:
        ws.register_artifact(
            slot, period="2026-01", parent_artifact_ids=(monthly1.artifact_id, monthly2.artifact_id),
        )
    assert excinfo.value.reason is ArtifactRegistrationReason.INVALID_LINEAGE


def test_register_local_restored_rejects_period_mismatch(tmp_path):
    root = tmp_path / "ws"
    ws = create_workspace(root, PASSWORD)
    monthly = _register_monthly(ws, root, period="2026-01")
    slot = ws.allocate_artifact_path(L)
    _make_xlsx(slot.path)
    with pytest.raises(ArtifactRegistrationError) as excinfo:
        ws.register_artifact(slot, period="2026-02", parent_artifact_ids=(monthly.artifact_id,))
    assert excinfo.value.reason is ArtifactRegistrationReason.INVALID_LINEAGE


def test_register_local_restored_rejects_zero_parents(tmp_path):
    ws = create_workspace(tmp_path / "ws", PASSWORD)
    slot = ws.allocate_artifact_path(L)
    _make_xlsx(slot.path)
    with pytest.raises(ArtifactRegistrationError) as excinfo:
        ws.register_artifact(slot, period="2026-01")
    assert excinfo.value.reason is ArtifactRegistrationReason.INVALID_LINEAGE


# ---------------------------------------------------------------------------
# I. register_artifact — provenance/job_id
# ---------------------------------------------------------------------------


def test_register_monthly_requires_provenance(tmp_path):
    root = tmp_path / "ws"
    ws = create_workspace(root, PASSWORD)
    slot = ws.allocate_artifact_path(M)
    _make_xlsx(slot.path)
    with pytest.raises(ArtifactRegistrationError) as excinfo:
        ws.register_artifact(
            slot, period="2026-01", source_job_id=_JOB_ID_A, safety_report=_report_for(slot.path),
        )
    assert excinfo.value.reason is ArtifactRegistrationReason.PROVENANCE_REQUIRED


def test_register_monthly_requires_job_id(tmp_path):
    root = tmp_path / "ws"
    ws = create_workspace(root, PASSWORD)
    slot = ws.allocate_artifact_path(M)
    _make_xlsx(slot.path)
    _make_provenance(root, _PROV_ID_A, _JOB_ID_A)
    with pytest.raises(ArtifactRegistrationError) as excinfo:
        ws.register_artifact(
            slot, period="2026-01", provenance_id=_PROV_ID_A, safety_report=_report_for(slot.path),
        )
    assert excinfo.value.reason is ArtifactRegistrationReason.JOB_ID_INVALID


def test_register_monthly_rejects_malformed_provenance_id(tmp_path):
    root = tmp_path / "ws"
    ws = create_workspace(root, PASSWORD)
    slot = ws.allocate_artifact_path(M)
    _make_xlsx(slot.path)
    with pytest.raises(ArtifactRegistrationError) as excinfo:
        ws.register_artifact(
            slot, period="2026-01", source_job_id=_JOB_ID_A, provenance_id="short",
            safety_report=_report_for(slot.path),
        )
    assert excinfo.value.reason is ArtifactRegistrationReason.PROVENANCE_INVALID


def test_register_monthly_rejects_missing_provenance_file(tmp_path):
    root = tmp_path / "ws"
    ws = create_workspace(root, PASSWORD)
    slot = ws.allocate_artifact_path(M)
    _make_xlsx(slot.path)
    with pytest.raises(ArtifactRegistrationError) as excinfo:
        ws.register_artifact(
            slot, period="2026-01", source_job_id=_JOB_ID_A, provenance_id=_PROV_ID_A,
            safety_report=_report_for(slot.path),
        )
    assert excinfo.value.reason is ArtifactRegistrationReason.PROVENANCE_INVALID


def test_register_monthly_rejects_corrupted_provenance_file(tmp_path):
    root = tmp_path / "ws"
    ws = create_workspace(root, PASSWORD)
    slot = ws.allocate_artifact_path(M)
    _make_xlsx(slot.path)
    prov_path = root / "provenance" / f"{_PROV_ID_A}.enc"
    prov_path.parent.mkdir(parents=True, exist_ok=True)
    prov_path.write_bytes(b"corrupted, not a valid encrypted container")
    with pytest.raises(ArtifactRegistrationError) as excinfo:
        ws.register_artifact(
            slot, period="2026-01", source_job_id=_JOB_ID_A, provenance_id=_PROV_ID_A,
            safety_report=_report_for(slot.path),
        )
    assert excinfo.value.reason is ArtifactRegistrationReason.PROVENANCE_INVALID


def test_register_monthly_rejects_wrong_job_id_binding(tmp_path):
    root = tmp_path / "ws"
    ws = create_workspace(root, PASSWORD)
    slot = ws.allocate_artifact_path(M)
    _make_xlsx(slot.path)
    _make_provenance(root, _PROV_ID_A, _JOB_ID_A)
    wrong_job_id = "9" * 32
    with pytest.raises(ArtifactRegistrationError) as excinfo:
        ws.register_artifact(
            slot, period="2026-01", source_job_id=wrong_job_id, provenance_id=_PROV_ID_A,
            safety_report=_report_for(slot.path),
        )
    assert excinfo.value.reason is ArtifactRegistrationReason.JOB_ID_INVALID


def test_register_monthly_provenance_sha256_computed_not_trusted(tmp_path):
    """
    register_artifact не принимает provenance_sha256 от вызывающего кода
    вообще (такого параметра нет) -- Workspace обязана вычислить его
    САМА из фактических байт sidecar на диске.
    """
    root = tmp_path / "ws"
    ws = create_workspace(root, PASSWORD)
    record = _register_monthly(ws, root)
    prov_path = root / "provenance" / f"{_PROV_ID_A}.enc"
    assert record.provenance_sha256 == sha256_file(prov_path)


# ---------------------------------------------------------------------------
# Correction Pass #1: атомарная privenance-привязка (закрытие MINOR-1
# Independent Adversarial Review) -- job_id и provenance_sha256 обязаны
# относиться к ОДНОМУ И ТОМУ ЖЕ снимку зашифрованных байт, а не к двум
# независимым чтениям потенциально мутирующего оригинала.
# ---------------------------------------------------------------------------


def test_provenance_source_mutation_cannot_create_mixed_ab_binding(tmp_path, monkeypatch):
    """
    Структурно-независимый (не завязанный на конкретные имена внутренних
    функций) детерминированный пробник: перехватывает Path.read_bytes на
    уровне файловой системы и, СРАЗУ ПОСЛЕ первого чтения provenance-пути
    (кем бы оно ни было вызвано -- job_id-валидацией или хэшированием),
    подменяет файл на диске совершенно другой версией (другой job_id,
    другое содержимое). Если реализация читает provenance ДВАЖДЫ
    независимо (одно чтение для job_id, другое -- для sha256), она
    получит job_id версии A, но sha256 версии B -- ровно MINOR-1.
    Корректная (после Correction Pass #1) реализация обязана записать
    provenance_sha256 версии A (снимка, из которого был провалидирован
    job_id), а НЕ версии B, появившейся на диске уже после захвата снимка.
    """
    import hashlib

    root = tmp_path / "ws"
    ws = create_workspace(root, PASSWORD)
    slot = ws.allocate_artifact_path(M)
    _make_xlsx(slot.path)

    prov_id = "9" * 32
    prov_path = root / "provenance" / f"{prov_id}.enc"
    job_id_a = "1" * 32
    job_id_b = "2" * 32
    EncryptedFileProvenanceStore(prov_path, PASSWORD, job_id=job_id_a)

    version_a_bytes = prov_path.read_bytes()
    version_a_sha = hashlib.sha256(version_a_bytes).hexdigest()

    from pathlib import Path as _PathType

    real_read_bytes = _PathType.read_bytes
    state = {"mutated": False}

    def mutating_read_bytes(self):
        result = real_read_bytes(self)
        if not state["mutated"] and self == prov_path:
            state["mutated"] = True
            # Симулируем гонку: сразу ПОСЛЕ первого чтения оригинального
            # provenance-пути (независимо от того, кто его инициировал)
            # файл на диске заменяется другой версией -- job_id_b.
            self.unlink()
            EncryptedFileProvenanceStore(self, PASSWORD, job_id=job_id_b)
        return result

    monkeypatch.setattr(_PathType, "read_bytes", mutating_read_bytes)

    # source_job_id указывает ИМЕННО версию A (ту, что реально была на
    # диске в момент allocate/старта регистрации) -- ожидаем успешную
    # регистрацию, связанную ИСКЛЮЧИТЕЛЬНО с версией A.
    record = ws.register_artifact(
        slot, period="2026-01", source_job_id=job_id_a, provenance_id=prov_id,
        safety_report=_report_for(slot.path),
    )

    assert state["mutated"] is True  # подтверждаем, что мутация реально произошла
    assert record.source_job_id == job_id_a
    assert record.provenance_sha256 == version_a_sha, (
        "provenance_sha256 обязан соответствовать снимку версии A (той, чей "
        "job_id был провалидирован), а не версии B, появившейся после захвата снимка"
    )
    # Текущий диск теперь версия B -- её SHA заведомо отличается от
    # записанного provenance_sha256 (что и требуется: запись жёстко
    # привязана к снимку на момент регистрации, а не к "текущему" файлу).
    assert sha256_file(prov_path) != record.provenance_sha256


def test_provenance_snapshot_temp_removed_after_success(tmp_path):
    root = tmp_path / "ws"
    ws = create_workspace(root, PASSWORD)
    prov_dir = root / "provenance"
    before = set(prov_dir.iterdir()) if prov_dir.exists() else set()
    _register_monthly(ws, root)
    after = set(prov_dir.iterdir())
    # После успешной регистрации в provenance/ остаётся ТОЛЬКО легитимный
    # sidecar -- никаких .provenance-snapshot-*.tmp остатков.
    tmp_leftovers = [p for p in after if ".provenance-snapshot-" in p.name]
    assert tmp_leftovers == []


def test_provenance_snapshot_temp_removed_after_validation_failure(tmp_path):
    root = tmp_path / "ws"
    ws = create_workspace(root, PASSWORD)
    slot = ws.allocate_artifact_path(M)
    _make_xlsx(slot.path)
    prov_id = "8" * 32
    prov_path = root / "provenance" / f"{prov_id}.enc"
    prov_path.parent.mkdir(parents=True, exist_ok=True)
    prov_path.write_bytes(b"corrupted, not a valid container")

    with pytest.raises(ArtifactRegistrationError):
        ws.register_artifact(
            slot, period="2026-01", source_job_id="7" * 32, provenance_id=prov_id,
            safety_report=_report_for(slot.path),
        )

    tmp_leftovers = [p for p in (root / "provenance").iterdir() if ".provenance-snapshot-" in p.name]
    assert tmp_leftovers == []


def test_provenance_snapshot_temp_never_contains_plaintext(tmp_path, monkeypatch):
    """
    Перехватывает запись временного снимка и проверяет, что байты,
    попадающие в temp-файл, БАЙТ-В-БАЙТ совпадают с зашифрованным
    содержимым исходного provenance-файла (т.е. это именно зашифрованный
    снимок, а не расшифрованный payload).
    """
    root = tmp_path / "ws"
    ws = create_workspace(root, PASSWORD)
    slot = ws.allocate_artifact_path(M)
    _make_xlsx(slot.path)
    prov_path = root / "provenance" / f"{_PROV_ID_A}.enc"
    EncryptedFileProvenanceStore(prov_path, PASSWORD, job_id=_JOB_ID_A)
    original_encrypted_bytes = prov_path.read_bytes()

    import app.workspace.workspace as workspace_module

    captured = {}
    real_capture = workspace_module._capture_provenance_snapshot

    def spying_capture(path):
        snapshot, digest = real_capture(path)
        captured["snapshot"] = snapshot
        return snapshot, digest

    monkeypatch.setattr(workspace_module, "_capture_provenance_snapshot", spying_capture)

    ws.register_artifact(
        slot, period="2026-01", source_job_id=_JOB_ID_A, provenance_id=_PROV_ID_A,
        safety_report=_report_for(slot.path),
    )
    assert captured["snapshot"] == original_encrypted_bytes
    # Зашифрованные байты не являются валидным UTF-8 JSON payload -- грубая
    # проверка, что это не расшифрованный plaintext.
    assert b'"schema_version"' not in captured["snapshot"]
    assert b'"job_id"' not in captured["snapshot"]


# ---------------------------------------------------------------------------
# Correction Pass #2: OD-7 hardening пути подготовки temp-снимка
# (закрытие MAJOR-1/MINOR-1 Focused Re-Review) -- сбои mkstemp/fdopen/
# write/flush/fsync обязаны транслироваться в PROVENANCE_INVALID без
# утечки пароля workspace и без осиротевшего temp-файла.
# ---------------------------------------------------------------------------


def test_provenance_snapshot_mkstemp_failure_maps_to_provenance_invalid(tmp_path, monkeypatch):
    """
    Correction Pass #2 (MAJOR-1): ДО коррекции tempfile.mkstemp
    вызывался ДО входа в try/finally, зачищающий password -- при сбое
    mkstemp пароль оставался достижим через frame locals
    _read_job_id_from_snapshot, а наружу вылетал сырой OSError вместо
    PROVENANCE_INVALID.
    """
    import tempfile as tempfile_module

    root = tmp_path / "ws"
    ws = create_workspace(root, _PASSWORD_SENTINEL)
    slot = ws.allocate_artifact_path(M)
    _make_xlsx(slot.path)
    _make_provenance(root, _PROV_ID_A, _JOB_ID_A, password=_PASSWORD_SENTINEL)

    def failing_mkstemp(*args, **kwargs):
        raise OSError("simulated disk full at mkstemp")

    monkeypatch.setattr(tempfile_module, "mkstemp", failing_mkstemp)

    with pytest.raises(ArtifactRegistrationError) as excinfo:
        ws.register_artifact(
            slot, period="2026-01", source_job_id=_JOB_ID_A, provenance_id=_PROV_ID_A,
            safety_report=_report_for(slot.path),
        )
    assert excinfo.value.reason is ArtifactRegistrationReason.PROVENANCE_INVALID
    assert _internal_frame_locals_containing(excinfo.value, (_PASSWORD_SENTINEL,)) == []
    assert ws.list_artifacts() == ()


def test_provenance_snapshot_fsync_failure_maps_to_provenance_invalid(tmp_path, monkeypatch):
    """Закрытие MINOR-1 Focused Re-Review: сбой fsync -- уже внутри
    защищённого блока (пароль не утекал и до коррекции), но давал сырой
    OSError вместо PROVENANCE_INVALID."""
    import os as os_module

    root = tmp_path / "ws"
    ws = create_workspace(root, _PASSWORD_SENTINEL)
    slot = ws.allocate_artifact_path(M)
    _make_xlsx(slot.path)
    _make_provenance(root, _PROV_ID_A, _JOB_ID_A, password=_PASSWORD_SENTINEL)

    def failing_fsync(fd):
        raise OSError("simulated fsync failure")

    monkeypatch.setattr(os_module, "fsync", failing_fsync)

    with pytest.raises(ArtifactRegistrationError) as excinfo:
        ws.register_artifact(
            slot, period="2026-01", source_job_id=_JOB_ID_A, provenance_id=_PROV_ID_A,
            safety_report=_report_for(slot.path),
        )
    assert excinfo.value.reason is ArtifactRegistrationReason.PROVENANCE_INVALID
    assert _internal_frame_locals_containing(excinfo.value, (_PASSWORD_SENTINEL,)) == []
    assert ws.list_artifacts() == ()

    leftovers = [p for p in (root / "provenance").iterdir() if ".provenance-snapshot-" in p.name]
    assert leftovers == []


def test_provenance_snapshot_write_failure_maps_to_provenance_invalid(tmp_path, monkeypatch):
    """
    Дополнительное покрытие write/flush-пути (не только fsync), без
    привязки к деталям реализации CPython: подменяет os.fdopen обёрткой,
    чей write() поднимает OSError, делегируя всё остальное (close/
    fileno) реальному файловому объекту.
    """
    import os as os_module

    root = tmp_path / "ws"
    ws = create_workspace(root, _PASSWORD_SENTINEL)
    slot = ws.allocate_artifact_path(M)
    _make_xlsx(slot.path)
    _make_provenance(root, _PROV_ID_A, _JOB_ID_A, password=_PASSWORD_SENTINEL)

    real_fdopen = os_module.fdopen

    class _FailingWriteFile:
        def __init__(self, real_file):
            self._real_file = real_file

        def write(self, data):
            raise OSError("simulated write failure")

        def fileno(self):
            return self._real_file.fileno()

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            self._real_file.close()
            return False

    def wrapping_fdopen(fd, *args, **kwargs):
        # os.fdopen также используется WorkspaceFileLock._open (режим
        # "r+b") для захвата workspace.lock -- перехватываем ТОЛЬКО
        # запись temp-снимка provenance (режим "wb"), не трогая лок.
        mode = args[0] if args else kwargs.get("mode")
        if mode == "wb":
            return _FailingWriteFile(real_fdopen(fd, *args, **kwargs))
        return real_fdopen(fd, *args, **kwargs)

    monkeypatch.setattr(os_module, "fdopen", wrapping_fdopen)

    with pytest.raises(ArtifactRegistrationError) as excinfo:
        ws.register_artifact(
            slot, period="2026-01", source_job_id=_JOB_ID_A, provenance_id=_PROV_ID_A,
            safety_report=_report_for(slot.path),
        )
    assert excinfo.value.reason is ArtifactRegistrationReason.PROVENANCE_INVALID
    assert _internal_frame_locals_containing(excinfo.value, (_PASSWORD_SENTINEL,)) == []
    assert ws.list_artifacts() == ()

    leftovers = [p for p in (root / "provenance").iterdir() if ".provenance-snapshot-" in p.name]
    assert leftovers == []


def test_provenance_snapshot_fdopen_failure_closes_fd_and_maps_to_provenance_invalid(tmp_path, monkeypatch):
    """§8: сбой самого os.fdopen (до появления обёртки файла) -- production
    обязан best-effort закрыть сырой fd и всё равно корректно
    транслировать в PROVENANCE_INVALID, без утечки пароля."""
    import os as os_module

    root = tmp_path / "ws"
    ws = create_workspace(root, _PASSWORD_SENTINEL)
    slot = ws.allocate_artifact_path(M)
    _make_xlsx(slot.path)
    _make_provenance(root, _PROV_ID_A, _JOB_ID_A, password=_PASSWORD_SENTINEL)

    real_fdopen = os_module.fdopen

    def failing_fdopen(fd, *args, **kwargs):
        # Перехватываем ТОЛЬКО режим "wb" (temp-снимок provenance), не
        # трогая WorkspaceFileLock._open (режим "r+b").
        mode = args[0] if args else kwargs.get("mode")
        if mode == "wb":
            raise OSError("simulated fdopen failure")
        return real_fdopen(fd, *args, **kwargs)

    monkeypatch.setattr(os_module, "fdopen", failing_fdopen)

    with pytest.raises(ArtifactRegistrationError) as excinfo:
        ws.register_artifact(
            slot, period="2026-01", source_job_id=_JOB_ID_A, provenance_id=_PROV_ID_A,
            safety_report=_report_for(slot.path),
        )
    assert excinfo.value.reason is ArtifactRegistrationReason.PROVENANCE_INVALID
    assert _internal_frame_locals_containing(excinfo.value, (_PASSWORD_SENTINEL,)) == []
    assert ws.list_artifacts() == ()


def test_register_canonical_rejects_provenance_id(tmp_path):
    root = tmp_path / "ws"
    ws = create_workspace(root, PASSWORD)
    monthly = _register_monthly(ws, root)
    slot = ws.allocate_artifact_path(C)
    _make_xlsx(slot.path)
    with pytest.raises(ArtifactRegistrationError) as excinfo:
        ws.register_artifact(
            slot, period="2026-01", parent_artifact_ids=(monthly.artifact_id,),
            provenance_id=_PROV_ID_A, safety_report=_report_for(slot.path),
        )
    assert excinfo.value.reason is ArtifactRegistrationReason.PROVENANCE_NOT_ALLOWED


def test_register_canonical_rejects_source_job_id(tmp_path):
    root = tmp_path / "ws"
    ws = create_workspace(root, PASSWORD)
    monthly = _register_monthly(ws, root)
    slot = ws.allocate_artifact_path(C)
    _make_xlsx(slot.path)
    with pytest.raises(ArtifactRegistrationError) as excinfo:
        ws.register_artifact(
            slot, period="2026-01", parent_artifact_ids=(monthly.artifact_id,),
            source_job_id=_JOB_ID_A, safety_report=_report_for(slot.path),
        )
    assert excinfo.value.reason is ArtifactRegistrationReason.JOB_ID_INVALID


# ---------------------------------------------------------------------------
# J. register_artifact — recovery_required gate
# ---------------------------------------------------------------------------


def test_register_refuses_when_recovery_required(tmp_path):
    from app.models.entities import EntityType, MappingEntry

    root = tmp_path / "ws"
    ws = create_workspace(root, PASSWORD)
    try:
        with ws.store_mutation() as (mf, idf):
            mf.add(MappingEntry(alias="C_AAAAAAAAA", real_value="R1", entity_type=EntityType.COMPANY, parent_alias=None))
            raise RuntimeError("simulated crash")
    except RuntimeError:
        pass
    assert ws.recovery_required is True

    slot = ws.allocate_artifact_path(M)
    _make_xlsx(slot.path)
    _make_provenance(root, _PROV_ID_A, _JOB_ID_A)
    with pytest.raises(WorkspaceBindingError) as excinfo:
        ws.register_artifact(
            slot, period="2026-01", source_job_id=_JOB_ID_A, provenance_id=_PROV_ID_A,
            safety_report=_report_for(slot.path),
        )
    assert excinfo.value.reason is WorkspaceBindingReason.RECOVERY_REQUIRED


# ---------------------------------------------------------------------------
# K. get_artifact / list_artifacts
# ---------------------------------------------------------------------------


def test_get_artifact_found_and_not_found(tmp_path):
    root = tmp_path / "ws"
    ws = create_workspace(root, PASSWORD)
    record = _register_monthly(ws, root)
    assert ws.get_artifact(record.artifact_id) == record
    with pytest.raises(ArtifactNotFoundError):
        ws.get_artifact("f" * 32)


def test_get_artifact_rejects_non_str_id(tmp_path):
    ws = create_workspace(tmp_path / "ws", PASSWORD)
    with pytest.raises(WorkspaceInputError):
        ws.get_artifact(12345)  # type: ignore[arg-type]


def test_list_artifacts_empty_registry(tmp_path):
    ws = create_workspace(tmp_path / "ws", PASSWORD)
    assert ws.list_artifacts() == ()


def test_list_artifacts_filters_by_kind_and_period(tmp_path):
    root = tmp_path / "ws"
    ws = create_workspace(root, PASSWORD)
    monthly1 = _register_monthly(ws, root, period="2026-01", marker="m1")
    monthly2 = _register_monthly(ws, root, period="2026-02", marker="m2", provenance_id="5" * 32, job_id="6" * 32)
    canon_slot = ws.allocate_artifact_path(C)
    _make_xlsx(canon_slot.path, "c")
    canon = ws.register_artifact(
        canon_slot, period="2026-01", parent_artifact_ids=(monthly1.artifact_id,),
        safety_report=_report_for(canon_slot.path),
    )

    assert set(r.artifact_id for r in ws.list_artifacts()) == {monthly1.artifact_id, monthly2.artifact_id, canon.artifact_id}
    assert ws.list_artifacts(kind=M) == (monthly1, monthly2)
    assert ws.list_artifacts(period="2026-01") == (monthly1, canon)
    assert ws.list_artifacts(kind=C, period="2026-01") == (canon,)
    assert ws.list_artifacts(kind=L) == ()


# ---------------------------------------------------------------------------
# L. Revision semantics
# ---------------------------------------------------------------------------


def test_register_artifact_increments_revision_by_one(tmp_path):
    root = tmp_path / "ws"
    ws = create_workspace(root, PASSWORD)
    revision_before = ws.info.revision
    _register_monthly(ws, root)
    assert ws.info.revision == revision_before + 1


def test_multiple_registrations_increment_revision_cumulatively(tmp_path):
    root = tmp_path / "ws"
    ws = create_workspace(root, PASSWORD)
    start = ws.info.revision
    _register_monthly(ws, root, period="2026-01", marker="a")
    _register_monthly(ws, root, period="2026-02", marker="b", provenance_id="5" * 32, job_id="6" * 32)
    assert ws.info.revision == start + 2


# ---------------------------------------------------------------------------
# M. Stale Workspace objects
# ---------------------------------------------------------------------------


def test_stale_workspace_object_does_not_lose_other_registration(tmp_path):
    root = tmp_path / "ws"
    create_workspace(root, PASSWORD)
    w1 = open_workspace(root, PASSWORD)
    w2 = open_workspace(root, PASSWORD)

    record1 = _register_monthly(w1, root, period="2026-01", marker="w1")
    # w2 всё ещё держит устаревший (до регистрации record1) manifest в
    # кэше, но register_artifact() ОБЯЗАН перечитать актуальный manifest
    # под локом перед мутацией.
    slot2 = w2.allocate_artifact_path(M)
    _make_xlsx(slot2.path, "w2")
    _make_provenance(root, "5" * 32, "6" * 32)
    record2 = w2.register_artifact(
        slot2, period="2026-02", source_job_id="6" * 32, provenance_id="5" * 32,
        safety_report=_report_for(slot2.path),
    )

    final = open_workspace(root, PASSWORD)
    ids = {r.artifact_id for r in final.list_artifacts()}
    assert ids == {record1.artifact_id, record2.artifact_id}
    assert final.info.artifact_count == 2


# ---------------------------------------------------------------------------
# N. Concurrency
# ---------------------------------------------------------------------------


def test_register_artifact_concurrent_attempt_rejected_not_corrupted(tmp_path):
    """
    WorkspaceFileLock — эксклюзивный, НЕБЛОКИРУЮЩИЙ (Stage 10B.3,
    frozen: "Таймаута и retry нет"); второй объект того же процесса на
    тот же канонический путь неотличим от nested-случая и корректно
    даёт NESTED_ACQUISITION немедленно, а не ждёт своей очереди (см.
    tests/test_workspace_lifecycle.py::
    test_lock_second_workspace_object_same_process_is_nested).
    register_artifact() обязан подчиняться ТОЙ ЖЕ дисциплине: детерминированно
    (через w1.lock(), без реального threading.Thread/race) проверяем, что
    попытка w2.register_artifact() ПОКА w1 держит лок немедленно отказывает
    NESTED_ACQUISITION, ничего не портит и не теряет, и что w2 успешно
    регистрирует СВОЙ артефакт сразу после освобождения лока w1.
    """
    root = tmp_path / "ws"
    create_workspace(root, PASSWORD)
    w1 = open_workspace(root, PASSWORD)
    w2 = open_workspace(root, PASSWORD)

    slot1 = w1.allocate_artifact_path(M)
    _make_xlsx(slot1.path, "t1")
    _make_provenance(root, "1" * 32, "2" * 32)

    slot2 = w2.allocate_artifact_path(M)
    _make_xlsx(slot2.path, "t2")
    _make_provenance(root, "3" * 32, "4" * 32)

    from app.workspace.errors import WorkspaceLockedError, WorkspaceLockedReason

    with w1.lock():
        with pytest.raises(WorkspaceLockedError) as excinfo:
            w2.register_artifact(
                slot2, period="2026-02", source_job_id="4" * 32, provenance_id="3" * 32,
                safety_report=_report_for(slot2.path),
            )
        assert excinfo.value.reason is WorkspaceLockedReason.NESTED_ACQUISITION

    # После освобождения лока w1 -- w2 регистрирует СВОЙ артефакт штатно,
    # ничего не потеряно/не испорчено отказавшей попыткой.
    record2 = w2.register_artifact(
        slot2, period="2026-02", source_job_id="4" * 32, provenance_id="3" * 32,
        safety_report=_report_for(slot2.path),
    )
    record1 = w1.register_artifact(
        slot1, period="2026-01", source_job_id="2" * 32, provenance_id="1" * 32,
        safety_report=_report_for(slot1.path),
    )

    final = open_workspace(root, PASSWORD)
    ids = {r.artifact_id for r in final.list_artifacts()}
    assert ids == {record1.artifact_id, record2.artifact_id}
    assert final.info.artifact_count == 2


def test_register_artifact_real_threads_exactly_one_wins_nested_acquisition(tmp_path):
    """
    Дополнительное реальное (не через w1.lock()) двухпоточное
    подтверждение той же дисциплины: два реальных потока одновременно
    вызывают register_artifact на РАЗНЫХ Workspace-объектах того же
    процесса/root. Поскольку лок неблокирующий, ровно один поток либо
    успевает захватить лок первым (и успешно регистрирует), либо оба
    могут попытаться захватить квази-одновременно -- в любом случае
    итог детерминированно проверяем: НИ ОДНОЙ порчи/потери, каждый
    успешный результат -- корректный ArtifactRecord, каждый неудачный --
    WorkspaceLockedError(NESTED_ACQUISITION), финальный реестр содержит
    ровно успешные записи.
    """
    from app.workspace.errors import WorkspaceLockedError, WorkspaceLockedReason

    root = tmp_path / "ws"
    create_workspace(root, PASSWORD)
    w1 = open_workspace(root, PASSWORD)
    w2 = open_workspace(root, PASSWORD)

    slot1 = w1.allocate_artifact_path(M)
    _make_xlsx(slot1.path, "t1")
    _make_provenance(root, "1" * 32, "2" * 32)

    slot2 = w2.allocate_artifact_path(M)
    _make_xlsx(slot2.path, "t2")
    _make_provenance(root, "3" * 32, "4" * 32)

    results = {}

    def run(ws, slot, period, prov_id, job_id, key):
        try:
            results[key] = ws.register_artifact(
                slot, period=period, source_job_id=job_id, provenance_id=prov_id,
                safety_report=_report_for(slot.path),
            )
        except Exception as exc:  # noqa: BLE001
            results[key] = exc

    t1 = threading.Thread(target=run, args=(w1, slot1, "2026-01", "1" * 32, "2" * 32, "r1"))
    t2 = threading.Thread(target=run, args=(w2, slot2, "2026-02", "3" * 32, "4" * 32, "r2"))
    t1.start()
    t2.start()
    t1.join(timeout=15)
    t2.join(timeout=15)
    assert not t1.is_alive() and not t2.is_alive()

    outcomes = [results["r1"], results["r2"]]
    successes = [o for o in outcomes if isinstance(o, ArtifactRecord)]
    failures = [o for o in outcomes if isinstance(o, WorkspaceLockedError)]
    assert len(successes) + len(failures) == 2
    assert len(successes) >= 1  # хотя бы один обязан успешно зарегистрироваться
    for failure in failures:
        assert failure.reason is WorkspaceLockedReason.NESTED_ACQUISITION

    final = open_workspace(root, PASSWORD)
    final_ids = {r.artifact_id for r in final.list_artifacts()}
    assert final_ids == {r.artifact_id for r in successes}
    assert final.info.artifact_count == len(successes)

    # Ретраим проигравших (если есть) -- показываем, что ничего не
    # заблокировано навсегда, отказ был именно из-за конкуренции за лок.
    if results["r1"] in failures:
        results["r1"] = w1.register_artifact(
            slot1, period="2026-01", source_job_id="2" * 32, provenance_id="1" * 32,
            safety_report=_report_for(slot1.path),
        )
    if results["r2"] in failures:
        results["r2"] = w2.register_artifact(
            slot2, period="2026-02", source_job_id="4" * 32, provenance_id="3" * 32,
            safety_report=_report_for(slot2.path),
        )
    final2 = open_workspace(root, PASSWORD)
    assert final2.info.artifact_count == 2


# ---------------------------------------------------------------------------
# O. OD-7: конфиденциальность provenance-ошибок
# ---------------------------------------------------------------------------

_INTERNAL_FILENAME_SUFFIXES = (os.path.join("app", "workspace", "workspace.py"),)
_PASSWORD_SENTINEL = "SENTINEL_10B4_PASSWORD_5F9K"


def _internal_frame_locals_containing(exc: BaseException, sentinels: tuple) -> list:
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
        _frame_with_uncleared_secret(_PASSWORD_SENTINEL)
    except ValueError as synthetic_exc:
        global _INTERNAL_FILENAME_SUFFIXES
        original = _INTERNAL_FILENAME_SUFFIXES
        _INTERNAL_FILENAME_SUFFIXES = original + (os.path.basename(__file__),)
        try:
            findings = _internal_frame_locals_containing(synthetic_exc, (_PASSWORD_SENTINEL,))
        finally:
            _INTERNAL_FILENAME_SUFFIXES = original
        assert findings != []


def test_od7_corrupted_provenance_no_password_leak(tmp_path):
    root = tmp_path / "ws"
    ws = create_workspace(root, _PASSWORD_SENTINEL)
    slot = ws.allocate_artifact_path(M)
    _make_xlsx(slot.path)
    prov_path = root / "provenance" / f"{_PROV_ID_A}.enc"
    prov_path.parent.mkdir(parents=True, exist_ok=True)
    prov_path.write_bytes(b"not a valid encrypted container")

    with pytest.raises(ArtifactRegistrationError) as excinfo:
        ws.register_artifact(
            slot, period="2026-01", source_job_id=_JOB_ID_A, provenance_id=_PROV_ID_A,
            safety_report=_report_for(slot.path),
        )
    assert excinfo.value.reason is ArtifactRegistrationReason.PROVENANCE_INVALID
    assert _internal_frame_locals_containing(excinfo.value, (_PASSWORD_SENTINEL,)) == []


def test_od7_missing_provenance_file_no_password_leak(tmp_path):
    root = tmp_path / "ws"
    ws = create_workspace(root, _PASSWORD_SENTINEL)
    slot = ws.allocate_artifact_path(M)
    _make_xlsx(slot.path)

    with pytest.raises(ArtifactRegistrationError) as excinfo:
        ws.register_artifact(
            slot, period="2026-01", source_job_id=_JOB_ID_A, provenance_id=_PROV_ID_A,
            safety_report=_report_for(slot.path),
        )
    assert excinfo.value.reason is ArtifactRegistrationReason.PROVENANCE_INVALID
    assert _internal_frame_locals_containing(excinfo.value, (_PASSWORD_SENTINEL,)) == []


def test_od7_wrong_job_id_no_password_leak(tmp_path):
    root = tmp_path / "ws"
    ws = create_workspace(root, _PASSWORD_SENTINEL)
    slot = ws.allocate_artifact_path(M)
    _make_xlsx(slot.path)
    _make_provenance(root, _PROV_ID_A, _JOB_ID_A, password=_PASSWORD_SENTINEL)

    with pytest.raises(ArtifactRegistrationError) as excinfo:
        ws.register_artifact(
            slot, period="2026-01", source_job_id="9" * 32, provenance_id=_PROV_ID_A,
            safety_report=_report_for(slot.path),
        )
    assert excinfo.value.reason is ArtifactRegistrationReason.JOB_ID_INVALID
    assert _internal_frame_locals_containing(excinfo.value, (_PASSWORD_SENTINEL,)) == []
