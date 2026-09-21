"""
Неизменяемые модели Workspace (Stage 10B.1).

Чистый слой: никакого ввода-вывода, криптографии, блокировок и обращений
к сторам. Модели валидируют себя при конструировании; при нарушении они
поднимают ValueError с ФИКСИРОВАННЫМ сообщением (имя поля, но никогда —
само значение), поэтому недопустимые данные не могут попасть в текст
ошибки.

Слова о смысле некоторых моделей важны:

    ArtifactSlot / ProvenanceSlot — только «выделено структурно
    допустимое место для потенциального файла». Слот не означает, что файл
    зарегистрирован, проверен, безопасен или разрешён к отправке, и не
    содержит ни одного поля или свойства с таким смыслом.

    WorkspaceVerification — про целостность, согласованность и привязку.
    Она ничего не утверждает о пригодности файлов для внешнего AI.

Manifest не хранит пути (пути выводятся из идентификаторов), результат
inspection (Stage 10A) и любые значения из данных клиентов.
"""

from __future__ import annotations

import dataclasses
import enum
import re
import unicodedata
from datetime import datetime
from pathlib import Path
from typing import Optional

MANIFEST_SCHEMA_VERSION = 1
LABEL_MAX_LENGTH = 64

_HEX32 = re.compile(r"[0-9a-f]{32}")
_HEX64 = re.compile(r"[0-9a-f]{64}")
_PERIOD = re.compile(r"[0-9]{4}-(?:0[1-9]|1[0-2])")
_TIMESTAMP = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z")
_BACKUP_NAME = re.compile(r"[0-9]{8}-[0-9a-f]{8}")


# ----------------------------------------------------------------------
# Проверки форматов (значения наружу никогда не попадают)
# ----------------------------------------------------------------------


def _is_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _is_nonneg_int(value: object) -> bool:
    return _is_int(value) and value >= 0


def _is_hex32(value: object) -> bool:
    return isinstance(value, str) and _HEX32.fullmatch(value) is not None


def _is_hex64(value: object) -> bool:
    return isinstance(value, str) and _HEX64.fullmatch(value) is not None


def _is_period(value: object) -> bool:
    return isinstance(value, str) and _PERIOD.fullmatch(value) is not None


def _is_timestamp(value: object) -> bool:
    if not isinstance(value, str) or _TIMESTAMP.fullmatch(value) is None:
        return False
    try:
        datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ")
    except ValueError:
        return False
    return True


def _is_label(value: object) -> bool:
    if value is None:
        return True
    if not isinstance(value, str) or not 1 <= len(value) <= LABEL_MAX_LENGTH:
        return False
    if value[0].isspace() or value[-1].isspace():
        return False
    if any(unicodedata.category(char) == "Cc" for char in value):
        return False
    # Строка должна кодироваться в UTF-8: одиночные суррогаты сделали бы
    # manifest несериализуемым, а текст UnicodeEncodeError раскрыл бы символ.
    # Значение не нормализуется и не очищается — недопустимое отвергается.
    try:
        value.encode("utf-8", errors="strict")
    except UnicodeEncodeError:
        return False
    return True


def _check(condition: bool, field_name: str) -> None:
    if not condition:
        raise ValueError(f"{field_name}: недопустимое значение")


# ----------------------------------------------------------------------
# Enum'ы
# ----------------------------------------------------------------------


class ArtifactKind(enum.Enum):
    ANONYMIZED_MONTHLY = "anonymized_monthly"
    ANALYTICAL_CANONICAL = "analytical_canonical"
    LOCAL_RESTORED = "local_restored"


class StoreSyncState(enum.Enum):
    CONSISTENT = "consistent"
    AHEAD = "ahead"


# ----------------------------------------------------------------------
# StoreState
# ----------------------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class StoreState:
    entry_count: int
    prefix_digest: str

    def __post_init__(self) -> None:
        _check(_is_nonneg_int(self.entry_count), "StoreState.entry_count")
        _check(_is_hex64(self.prefix_digest), "StoreState.prefix_digest")


# ----------------------------------------------------------------------
# ArtifactRecord
# ----------------------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class ArtifactRecord:
    artifact_id: str
    kind: ArtifactKind
    sha256: str
    created_at: str
    parent_artifact_ids: tuple[str, ...]
    period: str
    source_job_id: Optional[str]
    provenance_id: Optional[str]
    provenance_sha256: Optional[str]

    def __post_init__(self) -> None:
        _check(_is_hex32(self.artifact_id), "ArtifactRecord.artifact_id")
        _check(isinstance(self.kind, ArtifactKind), "ArtifactRecord.kind")
        _check(_is_hex64(self.sha256), "ArtifactRecord.sha256")
        _check(_is_timestamp(self.created_at), "ArtifactRecord.created_at")

        parents = self.parent_artifact_ids
        _check(
            isinstance(parents, tuple) and all(_is_hex32(parent) for parent in parents),
            "ArtifactRecord.parent_artifact_ids",
        )
        _check(len(set(parents)) == len(parents), "ArtifactRecord.parent_artifact_ids")
        _check(self.artifact_id not in parents, "ArtifactRecord.parent_artifact_ids")

        _check(_is_period(self.period), "ArtifactRecord.period")

        if self.kind is ArtifactKind.ANONYMIZED_MONTHLY:
            _check(parents == (), "ArtifactRecord.parent_artifact_ids")
            _check(_is_hex32(self.source_job_id), "ArtifactRecord.source_job_id")
            _check(_is_hex32(self.provenance_id), "ArtifactRecord.provenance_id")
            _check(_is_hex64(self.provenance_sha256), "ArtifactRecord.provenance_sha256")
        else:
            if self.kind is ArtifactKind.ANALYTICAL_CANONICAL:
                _check(1 <= len(parents) <= 2, "ArtifactRecord.parent_artifact_ids")
            else:
                _check(len(parents) == 1, "ArtifactRecord.parent_artifact_ids")
            _check(self.source_job_id is None, "ArtifactRecord.source_job_id")
            _check(self.provenance_id is None, "ArtifactRecord.provenance_id")
            _check(self.provenance_sha256 is None, "ArtifactRecord.provenance_sha256")


# ----------------------------------------------------------------------
# Слоты (только выделенное место)
# ----------------------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class ArtifactSlot:
    """Структурно допустимое место для потенциального артефакта — и только."""

    artifact_id: str
    kind: ArtifactKind
    path: Path

    def __post_init__(self) -> None:
        _check(_is_hex32(self.artifact_id), "ArtifactSlot.artifact_id")
        _check(isinstance(self.kind, ArtifactKind), "ArtifactSlot.kind")
        _check(isinstance(self.path, Path), "ArtifactSlot.path")


@dataclasses.dataclass(frozen=True)
class ProvenanceSlot:
    """Структурно допустимое место для потенциального provenance sidecar — и только."""

    provenance_id: str
    path: Path

    def __post_init__(self) -> None:
        _check(_is_hex32(self.provenance_id), "ProvenanceSlot.provenance_id")
        _check(isinstance(self.path, Path), "ProvenanceSlot.path")


# ----------------------------------------------------------------------
# Публичные информационные модели
# ----------------------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class WorkspaceInfo:
    workspace_id: str
    label: Optional[str] = dataclasses.field(repr=False)
    created_at: str
    revision: int
    artifact_count: int
    latest_analytical_artifact_id: Optional[str]
    mapping_entry_count: int
    identifier_entry_count: int

    def __post_init__(self) -> None:
        _check(_is_hex32(self.workspace_id), "WorkspaceInfo.workspace_id")
        _check(_is_label(self.label), "WorkspaceInfo.label")
        _check(_is_timestamp(self.created_at), "WorkspaceInfo.created_at")
        _check(_is_int(self.revision) and self.revision >= 1, "WorkspaceInfo.revision")
        _check(_is_nonneg_int(self.artifact_count), "WorkspaceInfo.artifact_count")
        _check(
            self.latest_analytical_artifact_id is None or _is_hex32(self.latest_analytical_artifact_id),
            "WorkspaceInfo.latest_analytical_artifact_id",
        )
        _check(_is_nonneg_int(self.mapping_entry_count), "WorkspaceInfo.mapping_entry_count")
        _check(_is_nonneg_int(self.identifier_entry_count), "WorkspaceInfo.identifier_entry_count")


@dataclasses.dataclass(frozen=True)
class StoreRecoveryResult:
    mapping_entries_adopted: int
    identifier_entries_adopted: int

    def __post_init__(self) -> None:
        _check(_is_nonneg_int(self.mapping_entries_adopted), "StoreRecoveryResult.mapping_entries_adopted")
        _check(_is_nonneg_int(self.identifier_entries_adopted), "StoreRecoveryResult.identifier_entries_adopted")


@dataclasses.dataclass(frozen=True)
class BackupInfo:
    name: str
    revision: int
    mapping_entry_count: int
    identifier_entry_count: int

    def __post_init__(self) -> None:
        _check(isinstance(self.name, str) and _BACKUP_NAME.fullmatch(self.name) is not None, "BackupInfo.name")
        _check(_is_int(self.revision) and self.revision >= 1, "BackupInfo.revision")
        _check(_is_nonneg_int(self.mapping_entry_count), "BackupInfo.mapping_entry_count")
        _check(_is_nonneg_int(self.identifier_entry_count), "BackupInfo.identifier_entry_count")


@dataclasses.dataclass(frozen=True)
class WorkspaceVerification:
    """
    Результат проверки целостности/согласованности/привязки. Не содержит
    и не выводит суждений о пригодности файлов для внешнего AI.
    """

    revision: int
    mapping_sync: StoreSyncState
    identifier_sync: StoreSyncState
    recovery_required: bool
    layout_ok: bool
    artifact_count: int
    artifacts_missing: int
    artifacts_hash_mismatch: int
    provenance_missing: int
    provenance_hash_mismatch: int
    orphan_file_count: int

    def __post_init__(self) -> None:
        _check(_is_int(self.revision) and self.revision >= 1, "WorkspaceVerification.revision")
        _check(isinstance(self.mapping_sync, StoreSyncState), "WorkspaceVerification.mapping_sync")
        _check(isinstance(self.identifier_sync, StoreSyncState), "WorkspaceVerification.identifier_sync")
        _check(isinstance(self.recovery_required, bool), "WorkspaceVerification.recovery_required")
        _check(isinstance(self.layout_ok, bool), "WorkspaceVerification.layout_ok")
        for name in (
            "artifact_count",
            "artifacts_missing",
            "artifacts_hash_mismatch",
            "provenance_missing",
            "provenance_hash_mismatch",
            "orphan_file_count",
        ):
            _check(_is_nonneg_int(getattr(self, name)), f"WorkspaceVerification.{name}")

    @property
    def has_integrity_problems(self) -> bool:
        # Решение владельца: orphan_file_count — отдельный сигнал и сюда не
        # входит. Свойство описывает только проблемы целостности/привязки;
        # осиротевшие (незарегистрированные, остаточные) файлы сообщаются
        # отдельным счётчиком.
        return (
            not self.layout_ok
            or self.recovery_required
            or self.mapping_sync is not StoreSyncState.CONSISTENT
            or self.identifier_sync is not StoreSyncState.CONSISTENT
            or self.artifacts_missing > 0
            or self.artifacts_hash_mismatch > 0
            or self.provenance_missing > 0
            or self.provenance_hash_mismatch > 0
        )


# ----------------------------------------------------------------------
# WorkspaceManifest (внутренняя модель manifest v1)
# ----------------------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class WorkspaceManifest:
    schema_version: int
    workspace_id: str
    label: Optional[str] = dataclasses.field(repr=False)
    created_at: str
    revision: int
    pending_store_mutation: bool
    mapping_state: StoreState
    identifier_state: StoreState
    artifacts: tuple[ArtifactRecord, ...]
    latest_analytical_artifact_id: Optional[str]

    def __post_init__(self) -> None:
        _check(
            _is_int(self.schema_version) and self.schema_version == MANIFEST_SCHEMA_VERSION,
            "WorkspaceManifest.schema_version",
        )
        _check(_is_hex32(self.workspace_id), "WorkspaceManifest.workspace_id")
        _check(_is_label(self.label), "WorkspaceManifest.label")
        _check(_is_timestamp(self.created_at), "WorkspaceManifest.created_at")
        _check(_is_int(self.revision) and self.revision >= 1, "WorkspaceManifest.revision")
        _check(isinstance(self.pending_store_mutation, bool), "WorkspaceManifest.pending_store_mutation")
        _check(isinstance(self.mapping_state, StoreState), "WorkspaceManifest.mapping_state")
        _check(isinstance(self.identifier_state, StoreState), "WorkspaceManifest.identifier_state")
        _check(
            isinstance(self.artifacts, tuple) and all(isinstance(record, ArtifactRecord) for record in self.artifacts),
            "WorkspaceManifest.artifacts",
        )
        _check(
            self.latest_analytical_artifact_id is None or _is_hex32(self.latest_analytical_artifact_id),
            "WorkspaceManifest.latest_analytical_artifact_id",
        )
        _validate_artifact_graph(self.artifacts, self.latest_analytical_artifact_id)


def _validate_artifact_graph(artifacts: tuple[ArtifactRecord, ...], latest: Optional[str]) -> None:
    """Кросс-проверки реестра: уникальность, порядок родителей, lineage, pointer."""
    seen: dict[str, ArtifactRecord] = {}
    used_sha256: set[str] = set()
    used_provenance: set[str] = set()

    for record in artifacts:
        _check(record.artifact_id not in seen, "WorkspaceManifest.artifacts.artifact_id")
        # SHA-256 уникален глобально, независимо от типа артефакта.
        _check(record.sha256 not in used_sha256, "WorkspaceManifest.artifacts.sha256")
        if record.provenance_id is not None:
            _check(record.provenance_id not in used_provenance, "WorkspaceManifest.artifacts.provenance_id")
            used_provenance.add(record.provenance_id)

        # Родители обязаны уже присутствовать выше по списку: это делает граф
        # ацикличным по построению.
        parents = []
        for parent_id in record.parent_artifact_ids:
            _check(parent_id in seen, "WorkspaceManifest.artifacts.parent_artifact_ids")
            parents.append(seen[parent_id])

        _validate_lineage(record, parents)

        seen[record.artifact_id] = record
        used_sha256.add(record.sha256)

    if latest is not None:
        _check(
            latest in seen and seen[latest].kind is ArtifactKind.ANALYTICAL_CANONICAL,
            "WorkspaceManifest.latest_analytical_artifact_id",
        )


def _validate_lineage(record: ArtifactRecord, parents: list[ArtifactRecord]) -> None:
    kinds = tuple(parent.kind for parent in parents)
    monthly = ArtifactKind.ANONYMIZED_MONTHLY
    canonical = ArtifactKind.ANALYTICAL_CANONICAL

    if record.kind is monthly:
        _check(kinds == (), "WorkspaceManifest.artifacts.lineage")
    elif record.kind is canonical:
        # Допустимо ровно: (monthly,), (canonical_prev,), (canonical_prev, monthly).
        _check(kinds in ((monthly,), (canonical,), (canonical, monthly)), "WorkspaceManifest.artifacts.lineage")
        for parent in parents:
            if parent.kind is monthly:
                _check(parent.period == record.period, "WorkspaceManifest.artifacts.period")
            else:
                _check(parent.period <= record.period, "WorkspaceManifest.artifacts.period")
    else:
        _check(len(parents) == 1 and kinds[0] in (canonical, monthly), "WorkspaceManifest.artifacts.lineage")
        _check(parents[0].period == record.period, "WorkspaceManifest.artifacts.period")
