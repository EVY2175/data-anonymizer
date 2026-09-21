"""
Тесты Stage 10B.1: ошибки, модели, manifest v1 и digest сторов.

Слой чисто вычислительный: файловой системы, криптографии и сторов здесь
нет. Все данные синтетические.
"""

from __future__ import annotations

import ast
import dataclasses
import json
import traceback
from pathlib import Path

import pytest

from app.models.entities import EntityType, MappingEntry
from app.models.identifiers import IdentifierMappingEntry, IdentifierType
from app.workspace import errors as errors_module
from app.workspace import manifest as manifest_module
from app.workspace import models as models_module
from app.workspace.errors import (
    ArtifactIntegrityError,
    ArtifactNotFoundError,
    ArtifactRegistrationError,
    ArtifactRegistrationReason,
    WorkspaceAuthenticationError,
    WorkspaceBindingError,
    WorkspaceBindingReason,
    WorkspaceCorruptedError,
    WorkspaceCorruptedReason,
    WorkspaceError,
    WorkspaceInputError,
    WorkspaceLockedError,
    WorkspaceLockedReason,
    WorkspaceNotFoundError,
)
from app.workspace.manifest import (
    EMPTY_IDENTIFIER_DIGEST,
    EMPTY_IDENTIFIER_STATE,
    EMPTY_MAPPING_DIGEST,
    EMPTY_MAPPING_STATE,
    IDENTIFIER_DIGEST_DOMAIN,
    MANIFEST_KIND,
    MAPPING_DIGEST_DOMAIN,
    alias_prefix_digest,
    identifier_entries_prefix_digest,
    mapping_entries_prefix_digest,
    parse_manifest,
    serialize_manifest,
    token_prefix_digest,
)
from app.workspace.models import (
    ArtifactKind,
    ArtifactRecord,
    ArtifactSlot,
    BackupInfo,
    ProvenanceSlot,
    StoreRecoveryResult,
    StoreState,
    StoreSyncState,
    WorkspaceInfo,
    WorkspaceManifest,
    WorkspaceVerification,
)

M = ArtifactKind.ANONYMIZED_MONTHLY
C = ArtifactKind.ANALYTICAL_CANONICAL
L = ArtifactKind.LOCAL_RESTORED

TS = "2026-09-21T12:00:00Z"
SENTINEL = "SENTINEL_9F2C"
FORBIDDEN_TERMS = ("is_safe", "can_upload", "approved", "authorized", "upload_allowed", "uploadable")
FORBIDDEN_ATTRIBUTE_PARTS = ("safe", "verified", "registered", "approved", "authorized", "upload")


# ---------------------------------------------------------------------------
# Помощники
# ---------------------------------------------------------------------------


def hex32(n: int) -> str:
    return f"{n:032x}"


def hex64(n: int) -> str:
    return f"{n:064x}"


WS_ID = hex32(0xABC)


def monthly(i: int, period: str = "2026-01") -> ArtifactRecord:
    return ArtifactRecord(
        artifact_id=hex32(i),
        kind=M,
        sha256=hex64(i),
        created_at=TS,
        parent_artifact_ids=(),
        period=period,
        source_job_id=hex32(1000 + i),
        provenance_id=hex32(2000 + i),
        provenance_sha256=hex64(3000 + i),
    )


def canonical(i: int, parents: tuple, period: str = "2026-01") -> ArtifactRecord:
    return ArtifactRecord(
        artifact_id=hex32(i),
        kind=C,
        sha256=hex64(i),
        created_at=TS,
        parent_artifact_ids=tuple(hex32(p) for p in parents),
        period=period,
        source_job_id=None,
        provenance_id=None,
        provenance_sha256=None,
    )


def local(i: int, parent: int, period: str = "2026-01") -> ArtifactRecord:
    return ArtifactRecord(
        artifact_id=hex32(i),
        kind=L,
        sha256=hex64(i),
        created_at=TS,
        parent_artifact_ids=(hex32(parent),),
        period=period,
        source_job_id=None,
        provenance_id=None,
        provenance_sha256=None,
    )


def make_manifest(artifacts: tuple = (), latest=None, **overrides) -> WorkspaceManifest:
    fields = dict(
        schema_version=1,
        workspace_id=WS_ID,
        label=None,
        created_at=TS,
        revision=1,
        pending_store_mutation=False,
        mapping_state=EMPTY_MAPPING_STATE,
        identifier_state=EMPTY_IDENTIFIER_STATE,
        artifacts=tuple(artifacts),
        latest_analytical_artifact_id=hex32(latest) if isinstance(latest, int) else latest,
    )
    fields.update(overrides)
    return WorkspaceManifest(**fields)


def payload_of(manifest: WorkspaceManifest) -> dict:
    return json.loads(serialize_manifest(manifest))


def dump(payload) -> bytes:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def rich_manifest() -> WorkspaceManifest:
    return make_manifest(
        artifacts=(
            monthly(1, "2026-01"),
            canonical(2, (1,), "2026-01"),
            monthly(3, "2026-02"),
            canonical(4, (2, 3), "2026-02"),
            local(5, 4, "2026-02"),
        ),
        latest=4,
        label="Проект",
        revision=7,
        mapping_state=StoreState(3, hex64(0x11)),
        identifier_state=StoreState(2, hex64(0x22)),
    )


def assert_payload_invalid(data: bytes, reason=WorkspaceCorruptedReason.MANIFEST_INVALID_PAYLOAD) -> WorkspaceCorruptedError:
    with pytest.raises(WorkspaceCorruptedError) as exc_info:
        parse_manifest(data)
    assert exc_info.value.reason is reason
    return exc_info.value


# ---------------------------------------------------------------------------
# Enum'ы и ошибки
# ---------------------------------------------------------------------------


def test_artifact_kind_exact_members_and_values() -> None:
    assert {(k.name, k.value) for k in ArtifactKind} == {
        ("ANONYMIZED_MONTHLY", "anonymized_monthly"),
        ("ANALYTICAL_CANONICAL", "analytical_canonical"),
        ("LOCAL_RESTORED", "local_restored"),
    }


def test_store_sync_state_exact_members_and_values() -> None:
    assert {(s.name, s.value) for s in StoreSyncState} == {("CONSISTENT", "consistent"), ("AHEAD", "ahead")}


def test_reason_enums_exact_members() -> None:
    assert [r.name for r in WorkspaceCorruptedReason] == [
        "MANIFEST_INVALID_CONTAINER",
        "MANIFEST_INVALID_PAYLOAD",
        "MANIFEST_UNSUPPORTED_VERSION",
        "LAYOUT_INCOMPLETE",
        "UNSAFE_LAYOUT",
        "LOCK_FILE_MISSING",
        "BACKUP_INVALID",
        "BACKUP_FOREIGN",
        "ADOPT_VERIFICATION_FAILED",
    ]
    assert [r.name for r in WorkspaceBindingReason] == [
        "STORE_MISSING",
        "STORE_UNOPENABLE",
        "STORE_REGRESSED",
        "STORE_DIVERGED",
        "STORE_UNEXPECTED_ENTRIES",
        "STORE_AHEAD_UNMARKED",
        "RECOVERY_REQUIRED",
        "APPEND_ONLY_VIOLATION",
    ]
    assert [r.name for r in WorkspaceLockedReason] == ["HELD_BY_OTHER", "NESTED_ACQUISITION", "NOT_HELD"]
    assert [r.name for r in ArtifactRegistrationReason] == [
        "SLOT_INVALID",
        "FILE_INVALID",
        "INVALID_PERIOD",
        "INVALID_LINEAGE",
        "DUPLICATE_SHA256",
        "EVIDENCE_REQUIRED",
        "EVIDENCE_NOT_ALLOWED",
        "EVIDENCE_HASH_MISMATCH",
        "EVIDENCE_BLOCKING_FINDINGS",
        "JOB_ID_INVALID",
        "PROVENANCE_REQUIRED",
        "PROVENANCE_NOT_ALLOWED",
        "PROVENANCE_INVALID",
        "POINTER_KIND_INVALID",
    ]


def test_exception_hierarchy() -> None:
    for cls in (
        WorkspaceInputError,
        WorkspaceNotFoundError,
        WorkspaceAuthenticationError,
        WorkspaceCorruptedError,
        WorkspaceBindingError,
        WorkspaceLockedError,
        ArtifactNotFoundError,
        ArtifactIntegrityError,
        ArtifactRegistrationError,
    ):
        assert issubclass(cls, WorkspaceError)
    assert issubclass(WorkspaceInputError, ValueError)
    assert issubclass(WorkspaceError, Exception)


@pytest.mark.parametrize(
    ("cls", "enum_type"),
    [
        (WorkspaceCorruptedError, WorkspaceCorruptedReason),
        (WorkspaceBindingError, WorkspaceBindingReason),
        (WorkspaceLockedError, WorkspaceLockedReason),
        (ArtifactRegistrationError, ArtifactRegistrationReason),
    ],
)
def test_reason_errors_have_fixed_distinct_safe_messages(cls, enum_type) -> None:
    messages = []
    for reason in enum_type:
        error = cls(reason)
        assert error.reason is reason
        assert str(error) and str(error) == error.args[0]
        messages.append(str(error))
    assert len(set(messages)) == len(messages)
    with pytest.raises(TypeError):
        cls("not-a-reason")  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        cls(None)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "cls",
    [WorkspaceNotFoundError, WorkspaceAuthenticationError, ArtifactNotFoundError, ArtifactIntegrityError],
)
def test_reasonless_errors_have_fixed_messages(cls) -> None:
    assert str(cls()) == str(cls()) and str(cls())
    with pytest.raises(TypeError):
        cls("custom message")  # type: ignore[call-arg]


def test_input_error_takes_caller_message_only_as_string() -> None:
    assert str(WorkspaceInputError("root не существует: X")) == "root не существует: X"
    for bad in ("", None, 5):
        with pytest.raises(TypeError):
            WorkspaceInputError(bad)  # type: ignore[arg-type]


def test_error_messages_contain_no_sensitive_vocabulary() -> None:
    lowered = []
    for enum_type, cls in (
        (WorkspaceCorruptedReason, WorkspaceCorruptedError),
        (WorkspaceBindingReason, WorkspaceBindingError),
        (WorkspaceLockedReason, WorkspaceLockedError),
        (ArtifactRegistrationReason, ArtifactRegistrationError),
    ):
        lowered.extend(str(cls(reason)).lower() for reason in enum_type)
    for text in lowered:
        assert "password" not in text and "пароль:" not in text
        for term in FORBIDDEN_TERMS:
            assert term not in text


# ---------------------------------------------------------------------------
# Frozen / схемы моделей
# ---------------------------------------------------------------------------


def field_names(cls) -> list:
    return [f.name for f in dataclasses.fields(cls)]


def test_exact_field_names_and_order() -> None:
    assert field_names(StoreState) == ["entry_count", "prefix_digest"]
    assert field_names(ArtifactRecord) == [
        "artifact_id",
        "kind",
        "sha256",
        "created_at",
        "parent_artifact_ids",
        "period",
        "source_job_id",
        "provenance_id",
        "provenance_sha256",
    ]
    assert field_names(ArtifactSlot) == ["artifact_id", "kind", "path"]
    assert field_names(ProvenanceSlot) == ["provenance_id", "path"]
    assert field_names(WorkspaceInfo) == [
        "workspace_id",
        "label",
        "created_at",
        "revision",
        "artifact_count",
        "latest_analytical_artifact_id",
        "mapping_entry_count",
        "identifier_entry_count",
    ]
    assert field_names(StoreRecoveryResult) == ["mapping_entries_adopted", "identifier_entries_adopted"]
    assert field_names(BackupInfo) == ["name", "revision", "mapping_entry_count", "identifier_entry_count"]
    assert field_names(WorkspaceVerification) == [
        "revision",
        "mapping_sync",
        "identifier_sync",
        "recovery_required",
        "layout_ok",
        "artifact_count",
        "artifacts_missing",
        "artifacts_hash_mismatch",
        "provenance_missing",
        "provenance_hash_mismatch",
        "orphan_file_count",
    ]
    assert field_names(WorkspaceManifest) == [
        "schema_version",
        "workspace_id",
        "label",
        "created_at",
        "revision",
        "pending_store_mutation",
        "mapping_state",
        "identifier_state",
        "artifacts",
        "latest_analytical_artifact_id",
    ]


def sample_objects() -> list:
    return [
        StoreState(1, hex64(1)),
        monthly(1),
        ArtifactSlot(hex32(1), M, Path("x")),
        ProvenanceSlot(hex32(1), Path("x")),
        WorkspaceInfo(WS_ID, None, TS, 1, 0, None, 0, 0),
        StoreRecoveryResult(0, 0),
        BackupInfo("00000001-abcdef01", 1, 0, 0),
        WorkspaceVerification(1, StoreSyncState.CONSISTENT, StoreSyncState.CONSISTENT, False, True, 0, 0, 0, 0, 0, 0),
        make_manifest(),
    ]


def test_all_models_are_frozen() -> None:
    for obj in sample_objects():
        first_field = dataclasses.fields(obj)[0].name
        with pytest.raises(dataclasses.FrozenInstanceError):
            setattr(obj, first_field, getattr(obj, first_field))


def test_no_model_has_password_or_secret_fields() -> None:
    for obj in sample_objects():
        for name in field_names(type(obj)):
            assert "password" not in name and "secret" not in name


def test_workspace_info_repr_hides_label() -> None:
    info = WorkspaceInfo(WS_ID, f"label-{SENTINEL}", TS, 1, 0, None, 0, 0)
    assert SENTINEL not in repr(info) and SENTINEL not in str(info)
    assert info.label == f"label-{SENTINEL}"
    assert WS_ID in repr(info)


def test_workspace_manifest_repr_hides_label() -> None:
    label = f"label-{SENTINEL}"
    for manifest in (make_manifest(label=label), dataclasses.replace(rich_manifest(), label=label)):
        assert SENTINEL not in repr(manifest)
        assert SENTINEL not in str(manifest)
        assert manifest.label == label  # значение доступно, скрыто только из repr
        assert "workspace_id=" in repr(manifest)  # repr остаётся содержательным, скрыт только label
    info = WorkspaceInfo(WS_ID, label, TS, 1, 0, None, 0, 0)
    assert SENTINEL not in repr(info)


def test_only_label_is_hidden_from_repr_in_manifest_and_info() -> None:
    assert [f.name for f in dataclasses.fields(WorkspaceManifest) if not f.repr] == ["label"]
    assert [f.name for f in dataclasses.fields(WorkspaceInfo) if not f.repr] == ["label"]
    # схема и порядок полей manifest не изменились
    assert field_names(WorkspaceManifest) == [
        "schema_version",
        "workspace_id",
        "label",
        "created_at",
        "revision",
        "pending_store_mutation",
        "mapping_state",
        "identifier_state",
        "artifacts",
        "latest_analytical_artifact_id",
    ]


def test_slots_are_neutral_and_carry_no_state_flags() -> None:
    for cls in (ArtifactSlot, ProvenanceSlot):
        attributes = [name for name in dir(cls) if not name.startswith("_")]
        for name in attributes:
            for part in FORBIDDEN_ATTRIBUTE_PARTS:
                assert part not in name.lower()
    assert field_names(ArtifactSlot) == ["artifact_id", "kind", "path"]


def test_artifact_record_has_no_path_field_and_no_free_text() -> None:
    names = field_names(ArtifactRecord)
    assert not any("path" in name for name in names)
    assert not any(name in ("detail", "message", "text", "note", "comment") for name in names)


def test_verification_terminology_is_integrity_only() -> None:
    names = field_names(WorkspaceVerification) + [n for n in dir(WorkspaceVerification) if not n.startswith("_")]
    for name in names:
        for part in FORBIDDEN_ATTRIBUTE_PARTS:
            assert part not in name.lower(), name


@pytest.mark.parametrize(
    "module",
    [errors_module, manifest_module, models_module],
    ids=["errors", "manifest", "models"],
)
def test_production_sources_have_no_authorization_terminology(module) -> None:
    source = Path(module.__file__).read_text(encoding="utf-8").lower()
    for term in FORBIDDEN_TERMS:
        assert term not in source


def base_verification(**overrides) -> WorkspaceVerification:
    fields = dict(
        revision=1,
        mapping_sync=StoreSyncState.CONSISTENT,
        identifier_sync=StoreSyncState.CONSISTENT,
        recovery_required=False,
        layout_ok=True,
        artifact_count=3,
        artifacts_missing=0,
        artifacts_hash_mismatch=0,
        provenance_missing=0,
        provenance_hash_mismatch=0,
        orphan_file_count=0,
    )
    fields.update(overrides)
    return WorkspaceVerification(**fields)


@pytest.mark.parametrize(
    ("overrides", "expected"),
    [
        ({}, False),
        ({"orphan_file_count": 4}, False),
        ({"layout_ok": False}, True),
        ({"recovery_required": True}, True),
        ({"mapping_sync": StoreSyncState.AHEAD}, True),
        ({"identifier_sync": StoreSyncState.AHEAD}, True),
        ({"artifacts_missing": 1}, True),
        ({"artifacts_hash_mismatch": 1}, True),
        ({"provenance_missing": 1}, True),
        ({"provenance_hash_mismatch": 1}, True),
    ],
)
def test_has_integrity_problems(overrides: dict, expected: bool) -> None:
    assert base_verification(**overrides).has_integrity_problems is expected


def test_orphan_file_count_is_a_separate_signal_by_owner_decision() -> None:
    # Решение владельца: has_integrity_problems описывает только целостность/привязку;
    # orphan_file_count — отдельный сигнал и в это свойство не входит.
    with_orphans = base_verification(orphan_file_count=7)
    assert with_orphans.orphan_file_count == 7
    assert with_orphans.has_integrity_problems is False
    # ...но настоящая проблема целостности по-прежнему видна при наличии orphan'ов
    assert base_verification(orphan_file_count=7, artifacts_missing=1).has_integrity_problems is True
    # отдельного свойства для orphan'ов в 10B.1 намеренно нет
    assert not hasattr(WorkspaceVerification, "has_orphan_files")
    assert not any("orphan" in name for name in dir(WorkspaceVerification) if name.startswith("has_"))


@pytest.mark.parametrize(
    "overrides",
    [
        {"revision": 0},
        {"revision": True},
        {"mapping_sync": "consistent"},
        {"recovery_required": 0},
        {"layout_ok": None},
        {"artifact_count": -1},
        {"orphan_file_count": True},
    ],
)
def test_verification_field_validation(overrides: dict) -> None:
    with pytest.raises(ValueError):
        base_verification(**overrides)


def test_small_model_validation() -> None:
    for bad in (-1, True, 1.5, "1"):
        with pytest.raises(ValueError):
            StoreRecoveryResult(bad, 0)  # type: ignore[arg-type]
    for name in ("", "1-abc", "00000001-ABCDEF01", "00000001-abcdef0", "../00000001-abcdef01", "00000001-abcdef01\n"):
        with pytest.raises(ValueError):
            BackupInfo(name, 1, 0, 0)
    with pytest.raises(ValueError):
        BackupInfo("00000001-abcdef01", 0, 0, 0)
    with pytest.raises(ValueError):
        WorkspaceInfo(WS_ID, " bad", TS, 1, 0, None, 0, 0)
    with pytest.raises(ValueError):
        WorkspaceInfo(WS_ID, None, TS, 1, 0, "NOT-HEX", 0, 0)


def test_slot_validation() -> None:
    with pytest.raises(ValueError):
        ArtifactSlot(hex32(1), M, "not-a-path")  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        ArtifactSlot("A" * 32, M, Path("x"))
    with pytest.raises(ValueError):
        ArtifactSlot(hex32(1), "anonymized_monthly", Path("x"))  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        ProvenanceSlot(hex32(1), "x")  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        ProvenanceSlot("short", Path("x"))


# ---------------------------------------------------------------------------
# StoreState и форматы
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("count", [0, 1, 10**9])
def test_store_state_accepts_valid(count: int) -> None:
    assert StoreState(count, hex64(5)).entry_count == count


@pytest.mark.parametrize("count", [-1, True, False, 1.0, "1", None, [1]])
def test_store_state_rejects_bad_count(count) -> None:
    with pytest.raises(ValueError):
        StoreState(count, hex64(5))


@pytest.mark.parametrize(
    "digest",
    ["A" * 64, "a" * 63, "a" * 65, "g" * 64, "", None, 5, ("a" * 64) + "\n", " " + "a" * 63, b"a" * 64],
)
def test_store_state_rejects_bad_digest(digest) -> None:
    with pytest.raises(ValueError):
        StoreState(0, digest)


BAD_HEX32 = ["", "A" * 32, "a" * 31, "a" * 33, "g" * 32, " " + "a" * 31, ("a" * 32) + "\n", None, 5, b"a" * 32, ["a" * 32]]
BAD_HEX64 = ["", "A" * 64, "a" * 63, "a" * 65, "g" * 64, ("a" * 64) + "\n", None, 5, b"a" * 64]


@pytest.mark.parametrize("value", BAD_HEX32)
def test_record_rejects_bad_artifact_id(value) -> None:
    with pytest.raises(ValueError):
        dataclasses.replace(monthly(1), artifact_id=value)


@pytest.mark.parametrize("value", BAD_HEX32)
def test_record_rejects_bad_source_job_id_and_provenance_id(value) -> None:
    with pytest.raises(ValueError):
        dataclasses.replace(monthly(1), source_job_id=value)
    with pytest.raises(ValueError):
        dataclasses.replace(monthly(1), provenance_id=value)


@pytest.mark.parametrize("value", BAD_HEX64)
def test_record_rejects_bad_sha256_and_provenance_sha256(value) -> None:
    with pytest.raises(ValueError):
        dataclasses.replace(monthly(1), sha256=value)
    with pytest.raises(ValueError):
        dataclasses.replace(monthly(1), provenance_sha256=value)


@pytest.mark.parametrize("value", BAD_HEX32)
def test_manifest_rejects_bad_workspace_id(value) -> None:
    with pytest.raises(ValueError):
        make_manifest(workspace_id=value)


@pytest.mark.parametrize("period", ["2026-01", "2026-12", "0001-06", "9999-09"])
def test_period_accepts_valid(period: str) -> None:
    assert monthly(1, period).period == period


@pytest.mark.parametrize(
    "period",
    [
        "2026-13",
        "2026-00",
        "2026-1",
        "26-01",
        "2026-01 ",
        " 2026-01",
        "2026/01",
        "2026-01-01",
        "2026-01\n",
        "٢٠٢٦-٠١",  # арабо-индийские цифры: \d принял бы их, [0-9] — нет
        "٢٠٢٦-01",  # только год не-ASCII: отличает [0-9] от \d в части года
        "",
        None,
        202601,
    ],
)
def test_period_rejects_invalid(period) -> None:
    with pytest.raises(ValueError):
        dataclasses.replace(monthly(1), period=period)


@pytest.mark.parametrize("value", ["2026-09-21T12:00:00Z", "2000-02-29T00:00:00Z", "1999-12-31T23:59:59Z"])
def test_created_at_accepts_valid(value: str) -> None:
    assert make_manifest(created_at=value).created_at == value


@pytest.mark.parametrize(
    "value",
    [
        "2026-09-21T12:00:00",
        "2026-09-21T12:00:00+00:00",
        "2026-09-21T12:00:00.000Z",
        "2026-09-21t12:00:00Z",
        "2026-09-21 12:00:00Z",
        "2026-13-21T12:00:00Z",
        "2026-09-31T12:00:00Z",
        "2026-02-30T12:00:00Z",
        "2026-09-21T24:00:00Z",
        "2026-09-21T12:60:00Z",
        "2026-09-21T12:00:60Z",
        "2026-09-21T12:00:00Z\n",
        " 2026-09-21T12:00:00Z",
        "2026-9-1T1:0:0Z",
        "٢٠٢٦-09-21T12:00:00Z",  # не-ASCII цифры: strptime их принял бы, regex [0-9] — нет
        "2026-09-21T١٢:00:00Z",
        "2026-09-21",
        "",
        None,
        20260921,
    ],
)
def test_created_at_rejects_invalid(value) -> None:
    with pytest.raises(ValueError):
        make_manifest(created_at=value)
    with pytest.raises(ValueError):
        dataclasses.replace(monthly(1), created_at=value)


@pytest.mark.parametrize("label", [None, "A", "Проект «Альфа»", "x" * 64, "a b c", "a-b_c.d"])
def test_label_accepts_valid(label) -> None:
    assert make_manifest(label=label).label == label


@pytest.mark.parametrize(
    "label",
    ["", "x" * 65, " lead", "trail ", "\tlead", "a\nb", "a\tb", "a\x00b", "a\x1fb", "a\x7fb", 5, b"abc", ["x"]],
)
def test_label_rejects_invalid(label) -> None:
    with pytest.raises(ValueError):
        make_manifest(label=label)


SURROGATE_LABELS = [
    "\ud800",
    "\udfff",
    "ab\udc80cd",
    # Два ОТДЕЛЬНЫХ суррогатных code point (литерал "􏿿" Python склеил бы
    # в один допустимый символ U+10FFFF, поэтому строка строится через chr()).
    chr(0xDBFF) + chr(0xDFFF),
    SENTINEL + "\ud800",
]

# Для JSON-разбора годятся только одиночные суррогаты: пара «старший+младший»
# при чтении JSON склеивается в один допустимый символ.
PARSE_SURROGATE_LABELS = [
    "\ud800",
    "\udfff",
    "ab\udc80cd",
    chr(0xDC00) + chr(0xD800),  # младший перед старшим — пары не образуют
    SENTINEL + "\ud800",
]


def dump_ascii(payload) -> bytes:
    """JSON с экранированием (\\ud800 и т.п.): единственный способ передать суррогат в байтах."""
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("ascii")


@pytest.mark.parametrize("label", SURROGATE_LABELS)
def test_label_with_lone_surrogate_rejected_by_manifest_and_info(label: str) -> None:
    with pytest.raises(ValueError) as manifest_error:
        make_manifest(label=label)
    with pytest.raises(ValueError) as info_error:
        WorkspaceInfo(WS_ID, label, TS, 1, 0, None, 0, 0)
    for error in (manifest_error, info_error):
        text = str(error.value) + repr(error.value)
        assert SENTINEL not in text
        assert "\ud800" not in text and "\udc80" not in text


def test_escaped_valid_surrogate_pair_in_json_is_a_valid_label() -> None:
    # Валидная пара в JSON-экранировании читается как ОДИН допустимый символ.
    payload = payload_of(make_manifest())
    payload["label"] = "😀"
    data = dump_ascii(payload)
    assert b"\\ud83d\\ude00" in data.lower()
    parsed = parse_manifest(data)
    assert parsed.label == "😀"
    assert parse_manifest(serialize_manifest(parsed)) == parsed


@pytest.mark.parametrize("label", PARSE_SURROGATE_LABELS)
def test_parse_rejects_escaped_lone_surrogate_label(label: str) -> None:
    payload = payload_of(make_manifest())
    payload["label"] = label
    data = dump_ascii(payload)
    assert b"\\ud" in data.lower() or b"\\udc" in data.lower()  # суррогат действительно передан

    error = assert_payload_invalid(data)

    rendered = str(error) + repr(error) + "".join(traceback.format_exception(error))
    assert SENTINEL not in rendered
    assert error.__cause__ is None and error.__suppress_context__ is True


def test_label_encodability_error_does_not_leak_through_serialization_path() -> None:
    # Раньше такой label принимался и serialize_manifest падал с UnicodeEncodeError,
    # чей текст содержал сам символ. Теперь до сериализации дело не доходит.
    payload = payload_of(make_manifest())
    payload["label"] = "\ud800"
    with pytest.raises(WorkspaceCorruptedError):
        parse_manifest(dump_ascii(payload))


VALID_NON_ASCII_LABELS = [
    "Проект «Альфа»",
    "日本語のラベル",
    "עברית",
    "🙂 smoke",
    "😀" * 64,
    "é",  # комбинирующий символ
    "Ünïcödé",
]


@pytest.mark.parametrize("label", VALID_NON_ASCII_LABELS)
def test_valid_non_ascii_labels_are_accepted_and_round_trip(label: str) -> None:
    manifest = make_manifest(label=label)
    data = serialize_manifest(manifest)
    assert label.encode("utf-8") in data
    parsed = parse_manifest(data)
    assert parsed.label == label
    assert parsed == manifest
    info = WorkspaceInfo(WS_ID, label, TS, 1, 0, None, 0, 0)
    assert info.label == label


def test_accepted_label_implies_manifest_is_serializable() -> None:
    # Инвариант без внешних зависимостей: перебор всех BMP code point'ов и выборки
    # астральных. Если label принят моделью — manifest обязан сериализоваться в
    # UTF-8 и разбираться обратно; любой суррогат обязан быть отвергнут.
    base = make_manifest()
    code_points = list(range(0x20, 0x10000)) + list(range(0x10000, 0x110000, 97))
    accepted = 0
    for code_point in code_points:
        label = "a" + chr(code_point) + "b"
        try:
            manifest = dataclasses.replace(base, label=label)
        except ValueError:
            continue  # отклонение допустимо (например, управляющие символы)
        assert not (0xD800 <= code_point <= 0xDFFF), hex(code_point)
        accepted += 1
        data = serialize_manifest(manifest)  # не должен падать
        assert parse_manifest(data) == manifest
    assert accepted > 60000  # проверка, что цикл действительно что-то принял


def test_every_surrogate_code_point_is_rejected() -> None:
    base = make_manifest()
    for code_point in range(0xD800, 0xE000):
        with pytest.raises(ValueError):
            dataclasses.replace(base, label="a" + chr(code_point))


def test_control_character_policy_unchanged_by_encodability_check() -> None:
    # Политика Cc не расширялась и не сужалась: \n, \t, NUL по-прежнему отвергаются.
    for label in ("a\nb", "a\tb", "a\x00b", "a\x7fb"):
        with pytest.raises(ValueError):
            make_manifest(label=label)


def test_model_errors_never_contain_offending_values() -> None:
    attempts = [
        lambda: dataclasses.replace(monthly(1), artifact_id=SENTINEL),
        lambda: dataclasses.replace(monthly(1), sha256=SENTINEL),
        lambda: dataclasses.replace(monthly(1), period=SENTINEL),
        lambda: dataclasses.replace(monthly(1), created_at=SENTINEL),
        lambda: make_manifest(label=SENTINEL + "\n"),
        lambda: StoreState(0, SENTINEL),
        lambda: BackupInfo(SENTINEL, 1, 0, 0),
        lambda: WorkspaceInfo(SENTINEL, None, TS, 1, 0, None, 0, 0),
    ]
    for attempt in attempts:
        with pytest.raises(ValueError) as exc_info:
            attempt()
        assert SENTINEL not in str(exc_info.value) and SENTINEL not in repr(exc_info.value)


# ---------------------------------------------------------------------------
# WorkspaceManifest: базовая валидация
# ---------------------------------------------------------------------------


def test_manifest_default_is_valid() -> None:
    manifest = make_manifest()
    assert manifest.artifacts == () and manifest.latest_analytical_artifact_id is None


@pytest.mark.parametrize("revision", [0, -1, True, False, 1.0, "1", None])
def test_manifest_rejects_bad_revision(revision) -> None:
    with pytest.raises(ValueError):
        make_manifest(revision=revision)


@pytest.mark.parametrize("flag", [0, 1, "false", None, 1.0])
def test_manifest_rejects_non_bool_pending_flag(flag) -> None:
    with pytest.raises(ValueError):
        make_manifest(pending_store_mutation=flag)


@pytest.mark.parametrize("version", [0, 2, True, 1.0, "1", None])
def test_manifest_rejects_bad_schema_version(version) -> None:
    with pytest.raises(ValueError):
        make_manifest(schema_version=version)


def test_manifest_rejects_wrong_container_types() -> None:
    with pytest.raises(ValueError):
        make_manifest(mapping_state=(0, hex64(1)))
    with pytest.raises(ValueError):
        make_manifest(identifier_state=None)
    valid = make_manifest()
    with pytest.raises(ValueError):
        dataclasses.replace(valid, artifacts=[monthly(1)])  # список вместо tuple
    with pytest.raises(ValueError):
        dataclasses.replace(valid, artifacts=("not-a-record",))


# ---------------------------------------------------------------------------
# Матрица полей artifact
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "overrides",
    [
        {"parent_artifact_ids": (hex32(9),)},
        {"source_job_id": None},
        {"provenance_id": None},
        {"provenance_sha256": None},
    ],
)
def test_monthly_matrix_violations(overrides: dict) -> None:
    with pytest.raises(ValueError):
        dataclasses.replace(monthly(1), **overrides)


@pytest.mark.parametrize(
    "overrides",
    [
        {"parent_artifact_ids": ()},
        {"parent_artifact_ids": (hex32(2), hex32(3), hex32(4))},
        {"parent_artifact_ids": (hex32(2), hex32(2))},
        {"parent_artifact_ids": [hex32(2)]},
        {"parent_artifact_ids": (hex32(1),)},  # родитель равен самому артефакту
        {"source_job_id": hex32(9)},
        {"provenance_id": hex32(9)},
        {"provenance_sha256": hex64(9)},
    ],
)
def test_canonical_matrix_violations(overrides: dict) -> None:
    with pytest.raises(ValueError):
        dataclasses.replace(canonical(1, (2,)), **overrides)


@pytest.mark.parametrize(
    "overrides",
    [
        {"parent_artifact_ids": ()},
        {"parent_artifact_ids": (hex32(2), hex32(3))},
        {"source_job_id": hex32(9)},
        {"provenance_id": hex32(9)},
        {"provenance_sha256": hex64(9)},
    ],
)
def test_local_matrix_violations(overrides: dict) -> None:
    with pytest.raises(ValueError):
        dataclasses.replace(local(1, 2), **overrides)


def test_record_kind_must_be_enum_not_string() -> None:
    with pytest.raises(ValueError):
        dataclasses.replace(monthly(1), kind="anonymized_monthly")


# ---------------------------------------------------------------------------
# Lineage
# ---------------------------------------------------------------------------


def test_lineage_allowed_forms() -> None:
    make_manifest(artifacts=(monthly(1), canonical(2, (1,))))  # (monthly,)
    make_manifest(artifacts=(monthly(1), canonical(2, (1,)), canonical(3, (2,))))  # (canonical_prev,)
    make_manifest(
        artifacts=(monthly(1, "2026-01"), canonical(2, (1,), "2026-01"), monthly(3, "2026-02"), canonical(4, (2, 3), "2026-02"))
    )  # (canonical_prev, monthly)
    make_manifest(artifacts=(monthly(1), local(2, 1)))
    make_manifest(artifacts=(monthly(1), canonical(2, (1,)), local(3, 2)))


def test_canonical_parent_period_ordering() -> None:
    older = (monthly(1, "2026-01"), canonical(2, (1,), "2026-01"))
    make_manifest(artifacts=older + (canonical(3, (2,), "2026-01"),))  # равный период допустим
    make_manifest(artifacts=older + (canonical(3, (2,), "2026-05"),))  # более поздний допустим
    later = (monthly(1, "2026-03"), canonical(2, (1,), "2026-03"))
    with pytest.raises(ValueError):
        make_manifest(artifacts=later + (canonical(3, (2,), "2026-02"),))  # родитель новее артефакта


def test_canonical_with_reversed_parent_order_rejected() -> None:
    with pytest.raises(ValueError):
        make_manifest(
            artifacts=(monthly(1), canonical(2, (1,)), monthly(3), canonical(4, (3, 2)))  # monthly, затем canonical
        )


def test_canonical_two_monthly_or_two_canonical_parents_rejected() -> None:
    with pytest.raises(ValueError):
        make_manifest(artifacts=(monthly(1), monthly(2), canonical(3, (1, 2))))
    with pytest.raises(ValueError):
        make_manifest(artifacts=(monthly(1), canonical(2, (1,)), canonical(3, (1,)), canonical(4, (2, 3))))


def test_canonical_monthly_parent_period_must_match() -> None:
    with pytest.raises(ValueError):
        make_manifest(artifacts=(monthly(1, "2026-01"), canonical(2, (1,), "2026-02")))


def test_canonical_cannot_have_local_parent() -> None:
    with pytest.raises(ValueError):
        make_manifest(artifacts=(monthly(1), local(2, 1), canonical(3, (2,))))


def test_local_lineage_rules() -> None:
    with pytest.raises(ValueError):
        make_manifest(artifacts=(monthly(1), local(2, 1), local(3, 2)))  # родитель LOCAL
    with pytest.raises(ValueError):
        make_manifest(artifacts=(monthly(1, "2026-01"), local(2, 1, "2026-02")))  # период не равен родительскому


def test_parents_must_appear_earlier() -> None:
    with pytest.raises(ValueError):
        make_manifest(artifacts=(canonical(2, (1,)), monthly(1)))
    with pytest.raises(ValueError):
        make_manifest(artifacts=(local(2, 1), monthly(1)))
    with pytest.raises(ValueError):
        make_manifest(artifacts=(canonical(2, (99,)),))  # неизвестный родитель


# ---------------------------------------------------------------------------
# Глобальная уникальность
# ---------------------------------------------------------------------------


def test_duplicate_artifact_id_rejected() -> None:
    with pytest.raises(ValueError):
        make_manifest(artifacts=(monthly(1), dataclasses.replace(monthly(2), artifact_id=hex32(1))))


@pytest.mark.parametrize(
    "second",
    [
        lambda: dataclasses.replace(monthly(2), sha256=hex64(1)),
        lambda: dataclasses.replace(canonical(2, (1,)), sha256=hex64(1)),
        lambda: dataclasses.replace(local(2, 1), sha256=hex64(1)),
    ],
    ids=["same-kind", "canonical-vs-monthly", "local-vs-monthly"],
)
def test_sha256_unique_across_all_kinds(second) -> None:
    with pytest.raises(ValueError):
        make_manifest(artifacts=(monthly(1), second()))


def test_provenance_id_unique_among_non_none() -> None:
    with pytest.raises(ValueError):
        make_manifest(artifacts=(monthly(1), dataclasses.replace(monthly(2), provenance_id=hex32(2001))))
    # None у нескольких записей допустим
    make_manifest(artifacts=(monthly(1), canonical(2, (1,)), canonical(3, (2,))))


# ---------------------------------------------------------------------------
# Pointer
# ---------------------------------------------------------------------------


def test_pointer_none_and_canonical_allowed() -> None:
    assert make_manifest().latest_analytical_artifact_id is None
    manifest = make_manifest(artifacts=(monthly(1), canonical(2, (1,))), latest=2)
    assert manifest.latest_analytical_artifact_id == hex32(2)


def test_pointer_rejects_monthly_and_local() -> None:
    with pytest.raises(ValueError):
        make_manifest(artifacts=(monthly(1),), latest=1)
    with pytest.raises(ValueError):
        make_manifest(artifacts=(monthly(1), local(2, 1)), latest=2)


def test_pointer_rejects_unknown_and_malformed() -> None:
    with pytest.raises(ValueError):
        make_manifest(artifacts=(monthly(1), canonical(2, (1,))), latest=99)
    with pytest.raises(ValueError):
        make_manifest(latest=5)  # реестр пуст
    with pytest.raises(ValueError):
        make_manifest(latest="NOT-HEX")


# ---------------------------------------------------------------------------
# Каноническая сериализация и round-trip
# ---------------------------------------------------------------------------

MINIMAL_CANONICAL_BYTES = (
    b'{"artifacts":[],"created_at":"2026-09-21T12:00:00Z","kind":"workspace_manifest","label":null,'
    b'"latest_analytical_artifact_id":null,"pending_store_mutation":false,"revision":1,"schema_version":1,'
    b'"stores":{"identifier":{"entry_count":0,"prefix_digest":"283b6b24088aab15b5ddec1d8abbd82be0e69c2e5c7832a64d2e5f2cbe3fc7ed"},'
    b'"mapping":{"entry_count":0,"prefix_digest":"63151f4849137be80e60edbe7a99638f282a7d8fe28603f961af496ab55b2be1"}},'
    b'"workspace_id":"00000000000000000000000000000abc"}'
)


def test_canonical_bytes_are_exact_for_minimal_manifest() -> None:
    assert serialize_manifest(make_manifest()) == MINIMAL_CANONICAL_BYTES
    assert not MINIMAL_CANONICAL_BYTES.endswith(b"\n")


def test_serialization_is_deterministic_and_keeps_utf8() -> None:
    manifest = make_manifest(label="Проект «Альфа»")
    first = serialize_manifest(manifest)
    assert first == serialize_manifest(manifest)
    assert "Проект «Альфа»".encode("utf-8") in first
    assert b"\\u" not in first


def test_round_trip_full_manifest() -> None:
    manifest = rich_manifest()
    data = serialize_manifest(manifest)
    parsed = parse_manifest(data)
    assert parsed == manifest
    assert serialize_manifest(parsed) == data


def test_artifact_order_is_registration_order() -> None:
    manifest = rich_manifest()
    parsed = parse_manifest(serialize_manifest(manifest))
    assert [r.artifact_id for r in parsed.artifacts] == [hex32(i) for i in (1, 2, 3, 4, 5)]
    assert parsed.artifacts[3].parent_artifact_ids == (hex32(2), hex32(3))


def test_json_shape_has_exact_keys_and_no_paths() -> None:
    payload = payload_of(rich_manifest())
    assert set(payload) == {
        "kind",
        "schema_version",
        "workspace_id",
        "label",
        "created_at",
        "revision",
        "pending_store_mutation",
        "stores",
        "artifacts",
        "latest_analytical_artifact_id",
    }
    assert payload["kind"] == MANIFEST_KIND == "workspace_manifest"
    assert set(payload["stores"]) == {"mapping", "identifier"}
    for state in payload["stores"].values():
        assert set(state) == {"entry_count", "prefix_digest"}
    for artifact in payload["artifacts"]:
        assert set(artifact) == {
            "artifact_id",
            "kind",
            "sha256",
            "created_at",
            "parent_artifact_ids",
            "period",
            "source_job_id",
            "provenance_id",
            "provenance_sha256",
        }
    text = json.dumps(payload).lower()
    for forbidden in ("relative_path", "path", "report", "evidence", "password", "safety"):
        assert forbidden not in text
    assert "\\" not in text.replace("\\u", "")


def test_manifest_argument_types() -> None:
    with pytest.raises(TypeError):
        serialize_manifest({"kind": "workspace_manifest"})  # type: ignore[arg-type]
    for bad in ("text", None, bytearray(b"{}"), 5):
        with pytest.raises(TypeError):
            parse_manifest(bad)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Строгий разбор: верхний уровень
# ---------------------------------------------------------------------------

TOP_KEYS = [
    "kind",
    "schema_version",
    "workspace_id",
    "label",
    "created_at",
    "revision",
    "pending_store_mutation",
    "stores",
    "artifacts",
    "latest_analytical_artifact_id",
]


@pytest.mark.parametrize("key", TOP_KEYS)
def test_missing_top_level_key_rejected(key: str) -> None:
    payload = payload_of(rich_manifest())
    del payload[key]
    assert_payload_invalid(dump(payload))


def test_unknown_top_level_key_rejected() -> None:
    payload = payload_of(rich_manifest())
    payload["extra"] = 1
    assert_payload_invalid(dump(payload))


BAD_TOP_VALUES = {
    "kind": ["other", 5, None, True],
    "workspace_id": ["", "A" * 32, "a" * 31, 5, None, ["a" * 32]],
    "label": ["", " x", "x ", "a\nb", "a" * 65, 5, ["x"], True],
    "created_at": ["2026-13-01T00:00:00Z", "2026-09-21T12:00:00", 5, None, ""],
    "revision": [0, -1, True, False, 1.5, "1", None],
    "pending_store_mutation": [0, 1, "false", None],
    "latest_analytical_artifact_id": [5, "A" * 32, "xyz", ["a"]],
    "stores": [None, [], "x", 5],
    "artifacts": [None, {}, "x", 5],
}


@pytest.mark.parametrize(
    ("key", "value"),
    [(key, value) for key, values in BAD_TOP_VALUES.items() for value in values],
)
def test_bad_top_level_value_rejected(key: str, value) -> None:
    payload = payload_of(rich_manifest())
    payload[key] = value
    assert_payload_invalid(dump(payload))


@pytest.mark.parametrize("version", [0, -1, 2, 99])
def test_other_integer_schema_versions_are_unsupported(version: int) -> None:
    payload = payload_of(rich_manifest())
    payload["schema_version"] = version
    assert_payload_invalid(dump(payload), WorkspaceCorruptedReason.MANIFEST_UNSUPPORTED_VERSION)


def test_unsupported_version_is_reported_even_when_other_keys_differ() -> None:
    error = assert_payload_invalid(dump({"schema_version": 2, "brand": "new"}), WorkspaceCorruptedReason.MANIFEST_UNSUPPORTED_VERSION)
    assert error.reason is WorkspaceCorruptedReason.MANIFEST_UNSUPPORTED_VERSION


@pytest.mark.parametrize("version", ["1", True, False, 1.0, None, [1]])
def test_non_integer_schema_versions_are_invalid_payload(version) -> None:
    payload = payload_of(rich_manifest())
    payload["schema_version"] = version
    assert_payload_invalid(dump(payload))


# ---------------------------------------------------------------------------
# Строгий разбор: вложенные структуры
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("store", ["mapping", "identifier"])
def test_store_object_shape_strict(store: str) -> None:
    base = payload_of(rich_manifest())

    unknown = json.loads(json.dumps(base))
    unknown["stores"][store]["extra"] = 1
    assert_payload_invalid(dump(unknown))

    for key in ("entry_count", "prefix_digest"):
        missing = json.loads(json.dumps(base))
        del missing["stores"][store][key]
        assert_payload_invalid(dump(missing))

    for value in (True, False, -1, 1.5, "1", None):
        bad_count = json.loads(json.dumps(base))
        bad_count["stores"][store]["entry_count"] = value
        assert_payload_invalid(dump(bad_count))

    for value in ("A" * 64, "a" * 63, "g" * 64, None, 5):
        bad_digest = json.loads(json.dumps(base))
        bad_digest["stores"][store]["prefix_digest"] = value
        assert_payload_invalid(dump(bad_digest))


def test_stores_object_requires_both_keys_and_no_others() -> None:
    base = payload_of(rich_manifest())
    for key in ("mapping", "identifier"):
        broken = json.loads(json.dumps(base))
        del broken["stores"][key]
        assert_payload_invalid(dump(broken))
    broken = json.loads(json.dumps(base))
    broken["stores"]["provenance"] = broken["stores"]["mapping"]
    assert_payload_invalid(dump(broken))


ARTIFACT_KEYS = [
    "artifact_id",
    "kind",
    "sha256",
    "created_at",
    "parent_artifact_ids",
    "period",
    "source_job_id",
    "provenance_id",
    "provenance_sha256",
]


@pytest.mark.parametrize("key", ARTIFACT_KEYS)
def test_missing_artifact_key_rejected(key: str) -> None:
    payload = payload_of(rich_manifest())
    del payload["artifacts"][0][key]
    assert_payload_invalid(dump(payload))


@pytest.mark.parametrize("key", ["relative_path", "path", "detail", "extra"])
def test_unknown_artifact_key_rejected(key: str) -> None:
    payload = payload_of(rich_manifest())
    payload["artifacts"][0][key] = "x"
    assert_payload_invalid(dump(payload))


BAD_ARTIFACT_VALUES = {
    "artifact_id": ["", "A" * 32, 5, None],
    "kind": ["other", "ANONYMIZED_MONTHLY", 5, None, ["anonymized_monthly"]],
    "sha256": ["", "A" * 64, 5, None],
    "created_at": ["x", "2026-13-01T00:00:00Z", 5, None],
    "parent_artifact_ids": ["x", None, {}, 5, [5], ["A" * 32], [hex32(9), hex32(9)]],
    "period": ["2026-13", "2026-1", 5, None, ""],
    "source_job_id": ["x", 5, None],
    "provenance_id": ["x", 5, None],
    "provenance_sha256": ["x", 5, None],
}


@pytest.mark.parametrize(
    ("key", "value"),
    [(key, value) for key, values in BAD_ARTIFACT_VALUES.items() for value in values],
)
def test_bad_monthly_artifact_value_rejected(key: str, value) -> None:
    payload = payload_of(rich_manifest())
    payload["artifacts"][0][key] = value  # индекс 0 — monthly
    assert_payload_invalid(dump(payload))


def test_bad_list_of_parents_type_rejected_for_canonical() -> None:
    payload = payload_of(rich_manifest())
    payload["artifacts"][1]["parent_artifact_ids"] = hex32(1)  # строка вместо списка
    assert_payload_invalid(dump(payload))


def test_non_monthly_artifact_with_provenance_fields_rejected_in_json() -> None:
    payload = payload_of(rich_manifest())
    payload["artifacts"][1]["source_job_id"] = hex32(77)
    assert_payload_invalid(dump(payload))


def test_semantic_violations_in_json_map_to_invalid_payload() -> None:
    payload = payload_of(rich_manifest())
    payload["artifacts"][1]["sha256"] = payload["artifacts"][0]["sha256"]  # глобальный дубликат sha
    assert_payload_invalid(dump(payload))

    payload = payload_of(rich_manifest())
    payload["artifacts"][0], payload["artifacts"][1] = payload["artifacts"][1], payload["artifacts"][0]  # родитель позже
    assert_payload_invalid(dump(payload))

    payload = payload_of(rich_manifest())
    payload["latest_analytical_artifact_id"] = hex32(5)  # LOCAL как pointer
    assert_payload_invalid(dump(payload))


# ---------------------------------------------------------------------------
# Строгий разбор: синтаксис JSON
# ---------------------------------------------------------------------------


def test_duplicate_json_keys_rejected_at_every_level() -> None:
    canonical_text = serialize_manifest(rich_manifest()).decode("utf-8")

    top = canonical_text.replace('"revision":7', '"revision":7,"revision":7', 1)
    assert top != canonical_text
    assert_payload_invalid(top.encode("utf-8"))

    nested = canonical_text.replace('"entry_count":3', '"entry_count":3,"entry_count":3', 1)
    assert nested != canonical_text
    assert_payload_invalid(nested.encode("utf-8"))

    artifact = canonical_text.replace('"period":"2026-01"', '"period":"2026-01","period":"2026-01"', 1)
    assert artifact != canonical_text
    assert_payload_invalid(artifact.encode("utf-8"))

    stores = canonical_text.replace('"stores":{', '"stores":{"mapping":{},', 1)
    assert stores != canonical_text
    assert_payload_invalid(stores.encode("utf-8"))


@pytest.mark.parametrize(
    "data",
    [
        b"",
        b"{",
        b"\xff\xfe",
        b"null",
        b"[]",
        b"5",
        b'"text"',
        b'{"revision": NaN}',
        b'{"revision": Infinity}',
        b"[" * 100000,
        MINIMAL_CANONICAL_BYTES + b"x",
        MINIMAL_CANONICAL_BYTES + b" {}",
        b"\xef\xbb\xbf" + MINIMAL_CANONICAL_BYTES,
    ],
    ids=[
        "empty",
        "unclosed-brace",
        "invalid-utf8",
        "null",
        "empty-list",
        "number",
        "string",
        "nan",
        "infinity",
        "deep-nesting",
        "trailing-garbage",
        "second-document",
        "utf8-bom",
    ],
)
def test_malformed_json_rejected(data: bytes) -> None:
    assert_payload_invalid(data)


def test_whitespace_around_valid_json_is_tolerated_but_not_required() -> None:
    manifest = make_manifest()
    assert parse_manifest(b"\n" + MINIMAL_CANONICAL_BYTES + b"\n") == manifest


# ---------------------------------------------------------------------------
# Конфиденциальность ошибок разбора
# ---------------------------------------------------------------------------


def sentinel_payloads() -> list:
    base = payload_of(rich_manifest())

    def variant(mutator) -> bytes:
        payload = json.loads(json.dumps(base))
        mutator(payload)
        return dump(payload)

    return [
        f'{{"x": {SENTINEL}}}'.encode(),
        f'"{SENTINEL}"'.encode(),
        f'{{"{SENTINEL}": 1}}'.encode(),
        f'{{"a":1,"{SENTINEL}":1,"{SENTINEL}":2}}'.encode(),
        variant(lambda p: p.__setitem__(SENTINEL, 1)),
        variant(lambda p: p.__setitem__("label", SENTINEL + "\n")),
        variant(lambda p: p.__setitem__("workspace_id", SENTINEL)),
        variant(lambda p: p.__setitem__("kind", SENTINEL)),
        variant(lambda p: p["artifacts"][0].__setitem__("artifact_id", SENTINEL)),
        variant(lambda p: p["artifacts"][0].__setitem__("kind", SENTINEL)),
        variant(lambda p: p["artifacts"][0].__setitem__("period", SENTINEL)),
        variant(lambda p: p["artifacts"][0].__setitem__("sha256", SENTINEL)),
        variant(lambda p: p["stores"]["mapping"].__setitem__("prefix_digest", SENTINEL)),
        variant(lambda p: p["stores"].__setitem__(SENTINEL, {})),
        variant(lambda p: p.__setitem__("schema_version", 2)),
        (SENTINEL + "\xff").encode("latin-1"),
    ]


@pytest.mark.parametrize("data", sentinel_payloads())
def test_parse_errors_do_not_leak_offending_values(data: bytes) -> None:
    with pytest.raises(WorkspaceCorruptedError) as exc_info:
        parse_manifest(data)
    error = exc_info.value
    rendered = str(error) + repr(error) + "".join(traceback.format_exception(error))
    assert SENTINEL not in rendered
    assert error.__cause__ is None and error.__suppress_context__ is True


# ---------------------------------------------------------------------------
# Digest сторов
# ---------------------------------------------------------------------------

MAPPING_VECTOR = ["C_AAAAAAAAA", "C_BBBBBBBBB"]
MAPPING_VECTOR_DIGEST = "e6cf12faef8b78b6986170233d8aaeff6e97498429e52ce1aa318e9abaa7088b"
IDENTIFIER_VECTOR = ["INN_A0000000", "INN_B0000000"]
IDENTIFIER_VECTOR_DIGEST = "7ae85accf70f439afecbdb60868271e52d0ffccbb9d2c8cec435e449bb9ae4bb"


def test_zero_digest_vectors() -> None:
    assert alias_prefix_digest([]) == "63151f4849137be80e60edbe7a99638f282a7d8fe28603f961af496ab55b2be1"
    assert token_prefix_digest([]) == "283b6b24088aab15b5ddec1d8abbd82be0e69c2e5c7832a64d2e5f2cbe3fc7ed"
    assert alias_prefix_digest(()) == EMPTY_MAPPING_DIGEST
    assert token_prefix_digest(()) == EMPTY_IDENTIFIER_DIGEST
    assert EMPTY_MAPPING_STATE == StoreState(0, EMPTY_MAPPING_DIGEST)
    assert EMPTY_IDENTIFIER_STATE == StoreState(0, EMPTY_IDENTIFIER_DIGEST)


def test_fixed_non_zero_digest_vectors() -> None:
    assert alias_prefix_digest(MAPPING_VECTOR) == MAPPING_VECTOR_DIGEST
    assert token_prefix_digest(IDENTIFIER_VECTOR) == IDENTIFIER_VECTOR_DIGEST
    assert alias_prefix_digest(tuple(MAPPING_VECTOR)) == MAPPING_VECTOR_DIGEST


def test_domains_are_exact() -> None:
    assert MAPPING_DIGEST_DOMAIN == b"DataAnonymizer/StoreDigest/v1/mapping"
    assert IDENTIFIER_DIGEST_DOMAIN == b"DataAnonymizer/StoreDigest/v1/identifier"


def test_digest_resists_delimiter_collisions() -> None:
    assert alias_prefix_digest(["ab", "c"]) != alias_prefix_digest(["a", "bc"])
    assert alias_prefix_digest(["a\nb"]) != alias_prefix_digest(["a", "b"])
    assert alias_prefix_digest(["a", "b"]) != alias_prefix_digest(["ab"])
    assert alias_prefix_digest([""]) != alias_prefix_digest([])
    assert alias_prefix_digest(["", ""]) != alias_prefix_digest([""])


def test_mapping_and_identifier_domains_are_separated() -> None:
    for values in ([], ["x"], MAPPING_VECTOR):
        assert alias_prefix_digest(values) != token_prefix_digest(values)


def test_order_matters() -> None:
    assert alias_prefix_digest(["a", "b"]) != alias_prefix_digest(["b", "a"])


def test_prefix_semantics() -> None:
    values = ["a1", "a2", "a3", "a4"]
    assert alias_prefix_digest(values, 3) == alias_prefix_digest(values[:3])
    assert alias_prefix_digest(values, 0) == EMPTY_MAPPING_DIGEST
    assert alias_prefix_digest(values, 4) == alias_prefix_digest(values)
    assert alias_prefix_digest(values, None) == alias_prefix_digest(values)
    assert token_prefix_digest(values, 2) == token_prefix_digest(values[:2])
    # Prefix из N не совпадает с digest другого числа записей.
    assert alias_prefix_digest(values, 1) != alias_prefix_digest(values, 2)
    # Дозапись в конец не меняет prefix-digest первых N.
    assert alias_prefix_digest(values + ["a5"], 4) == alias_prefix_digest(values)


@pytest.mark.parametrize("n", [-1, 5, 100])
def test_prefix_n_out_of_range_is_rejected_not_truncated(n: int) -> None:
    with pytest.raises(ValueError):
        alias_prefix_digest(["a", "b", "c", "d"], n)
    with pytest.raises(ValueError):
        token_prefix_digest(["a", "b", "c", "d"], n)


@pytest.mark.parametrize("n", [True, False, 1.0, "1", [1]])
def test_prefix_n_wrong_type_rejected(n) -> None:
    with pytest.raises(TypeError):
        alias_prefix_digest(["a", "b"], n)


def test_digest_input_types() -> None:
    for bad in ("abc", None, 5, {"a"}, iter(["a"]), b"ab"):
        with pytest.raises(TypeError):
            alias_prefix_digest(bad)  # type: ignore[arg-type]
    for bad_item in (5, None, b"a", ["a"]):
        with pytest.raises(TypeError):
            alias_prefix_digest(["ok", bad_item])  # type: ignore[list-item]


def test_digest_unencodable_value_is_rejected_without_leaking() -> None:
    with pytest.raises(ValueError) as exc_info:
        alias_prefix_digest(["\ud800" + SENTINEL])
    assert SENTINEL not in str(exc_info.value) + repr(exc_info.value)
    assert "\ud800" not in str(exc_info.value)
    assert exc_info.value.__suppress_context__ is True


def test_digest_supports_non_ascii_values() -> None:
    assert alias_prefix_digest(["Ж"]) != alias_prefix_digest(["Z"])
    assert alias_prefix_digest(["Ж"]) == alias_prefix_digest(["Ж"])


def make_mapping_entries(aliases: list, real_prefix: str) -> tuple:
    return tuple(
        MappingEntry(alias=alias, real_value=f"{real_prefix}-{index}", entity_type=EntityType.COMPANY)
        for index, alias in enumerate(aliases)
    )


def test_mapping_entries_digest_uses_alias_only() -> None:
    first = make_mapping_entries(MAPPING_VECTOR, "ООО Первая")
    second = make_mapping_entries(MAPPING_VECTOR, "ООО Совершенно Другая")
    assert mapping_entries_prefix_digest(first) == MAPPING_VECTOR_DIGEST
    assert mapping_entries_prefix_digest(second) == MAPPING_VECTOR_DIGEST  # real_value не влияет
    assert mapping_entries_prefix_digest(first, 1) == alias_prefix_digest(MAPPING_VECTOR, 1)
    assert mapping_entries_prefix_digest([]) == EMPTY_MAPPING_DIGEST


def test_identifier_entries_digest_uses_token_only() -> None:
    def entries(prefix: str) -> tuple:
        return tuple(
            IdentifierMappingEntry(
                token=token, identifier_value=f"{prefix}{index}", identifier_type=IdentifierType.INN
            )
            for index, token in enumerate(IDENTIFIER_VECTOR)
        )

    assert identifier_entries_prefix_digest(entries("7707")) == IDENTIFIER_VECTOR_DIGEST
    assert identifier_entries_prefix_digest(entries("5000")) == IDENTIFIER_VECTOR_DIGEST  # identifier_value не влияет
    assert identifier_entries_prefix_digest(entries("7707"), 1) == token_prefix_digest(IDENTIFIER_VECTOR, 1)


def test_entries_digest_input_validation() -> None:
    with pytest.raises(TypeError):
        mapping_entries_prefix_digest("abc")  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        mapping_entries_prefix_digest([object()])
    with pytest.raises(TypeError):
        identifier_entries_prefix_digest([make_mapping_entries(["C_X"], "r")[0]])  # у mapping-записи нет token
    with pytest.raises(ValueError):
        mapping_entries_prefix_digest(make_mapping_entries(MAPPING_VECTOR, "r"), 3)


def test_digest_does_not_depend_on_confidential_payload_in_any_form() -> None:
    entries = make_mapping_entries(["C_ONLY"], f"real-{SENTINEL}")
    assert SENTINEL not in mapping_entries_prefix_digest(entries)
    assert mapping_entries_prefix_digest(entries) == alias_prefix_digest(["C_ONLY"])


# ---------------------------------------------------------------------------
# Граница «нет ввода-вывода» и зависимостей
# ---------------------------------------------------------------------------

PRODUCTION_MODULES = [errors_module, models_module, manifest_module]
FORBIDDEN_SOURCE_TOKENS = (
    "print(",
    "logging",
    "open(",
    ".write",
    "mkdir",
    "unlink",
    "remove(",
    "rename",
    ".replace(",
    "encrypt",
    "decrypt",
    "mappingstore",
    "openpyxl",
    "socket",
    "urllib",
    "subprocess",
    "shutil",
    "tempfile",
    "os.",
    "msvcrt",
    "fcntl",
    ".entries(",
)
ALLOWED_IMPORT_ROOTS = {
    "__future__",
    "dataclasses",
    "enum",
    "hashlib",
    "json",
    "re",
    "unicodedata",
    "datetime",
    "pathlib",
    "typing",
    "app.workspace.errors",
    "app.workspace.models",
}


@pytest.mark.parametrize("module", PRODUCTION_MODULES, ids=["errors", "models", "manifest"])
def test_production_has_no_io_or_runtime_side_effects(module) -> None:
    source = Path(module.__file__).read_text(encoding="utf-8").lower()
    for token in FORBIDDEN_SOURCE_TOKENS:
        assert token not in source, token


@pytest.mark.parametrize("module", PRODUCTION_MODULES, ids=["errors", "models", "manifest"])
def test_production_imports_are_restricted(module) -> None:
    tree = ast.parse(Path(module.__file__).read_text(encoding="utf-8"))
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            imported.add(node.module)
    assert imported <= ALLOWED_IMPORT_ROOTS, imported - ALLOWED_IMPORT_ROOTS
    assert not any(name.startswith(("app.safety", "app.mapping", "app.security", "openpyxl")) for name in imported)


def test_package_init_is_docstring_only() -> None:
    import app.workspace as package

    tree = ast.parse(Path(package.__file__).read_text(encoding="utf-8"))
    assert len(tree.body) == 1
    assert isinstance(tree.body[0], ast.Expr) and isinstance(tree.body[0].value, ast.Constant)
    assert isinstance(tree.body[0].value.value, str)
