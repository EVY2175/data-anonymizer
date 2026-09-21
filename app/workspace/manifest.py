"""
Manifest v1: схема, строгая сериализация/разбор и digest сторов (Stage 10B.1).

Чистый слой: только байты, JSON и хэши. Никакого ввода-вывода,
криптографии, блокировок и обращений к сторам — шифрованное чтение/запись
manifest появится отдельным slice'ом.

======================================================================
Сериализация
======================================================================

    json.dumps(payload, sort_keys=True, separators=(",", ":"),
               ensure_ascii=False).encode("utf-8")

Без завершающего перевода строки; порядок artifacts — порядок
регистрации. Разбор строгий: дубликаты ключей, неизвестные и
отсутствующие ключи, неверные типы (в том числе bool вместо int),
неверные значения enum, форматы ID/хэшей/времени/периода и нарушения
кросс-проверок отвергаются. Ошибки разбора никогда не содержат
проблемное значение: наружу выходит только WorkspaceCorruptedError с
фиксированной причиной (исходное исключение подавляется).

======================================================================
Digest сторов
======================================================================

Для привязки стора к workspace хэшируются ТОЛЬКО alias (mapping) либо
token (identifier) — не конфиденциальные значения. Алгоритм с
длина-префиксами исключает неоднозначность разделителей; домены mapping
и identifier разделены. Prefix-digest первых N значений совпадает с
digest списка из первых N значений.
"""

from __future__ import annotations

import hashlib
import json
from typing import Optional, Sequence

from app.workspace.errors import WorkspaceCorruptedError, WorkspaceCorruptedReason
from app.workspace.models import (
    MANIFEST_SCHEMA_VERSION,
    ArtifactKind,
    ArtifactRecord,
    StoreState,
    WorkspaceManifest,
)

MANIFEST_KIND = "workspace_manifest"

MAPPING_DIGEST_DOMAIN = b"DataAnonymizer/StoreDigest/v1/mapping"
IDENTIFIER_DIGEST_DOMAIN = b"DataAnonymizer/StoreDigest/v1/identifier"

# Digest нуля записей (значения заморожены контрактом Stage 10B).
EMPTY_MAPPING_DIGEST = "63151f4849137be80e60edbe7a99638f282a7d8fe28603f961af496ab55b2be1"
EMPTY_IDENTIFIER_DIGEST = "283b6b24088aab15b5ddec1d8abbd82be0e69c2e5c7832a64d2e5f2cbe3fc7ed"

EMPTY_MAPPING_STATE = StoreState(entry_count=0, prefix_digest=EMPTY_MAPPING_DIGEST)
EMPTY_IDENTIFIER_STATE = StoreState(entry_count=0, prefix_digest=EMPTY_IDENTIFIER_DIGEST)

_TOP_LEVEL_KEYS = frozenset(
    {
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
)
_STORES_KEYS = frozenset({"mapping", "identifier"})
_STORE_KEYS = frozenset({"entry_count", "prefix_digest"})
_ARTIFACT_KEYS = frozenset(
    {
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
)


# ----------------------------------------------------------------------
# Digest
# ----------------------------------------------------------------------


def _prefix_digest(domain: bytes, values: object, n: Optional[int]) -> str:
    if not isinstance(values, (list, tuple)):
        raise TypeError("values должен быть list или tuple строк")
    for value in values:
        if not isinstance(value, str):
            raise TypeError("каждое значение должно быть строкой")

    if n is None:
        count = len(values)
    else:
        if isinstance(n, bool) or not isinstance(n, int):
            raise TypeError("n должен быть int")
        if n < 0 or n > len(values):
            raise ValueError("n вне допустимого диапазона")
        count = n

    digest = hashlib.sha256()
    digest.update(len(domain).to_bytes(8, "big"))
    digest.update(domain)
    digest.update(count.to_bytes(8, "big"))
    for value in values[:count]:
        try:
            encoded = value.encode("utf-8", errors="strict")
        except UnicodeEncodeError:
            # Текст UnicodeEncodeError содержит сам символ — наружу не отдаём.
            raise ValueError("значение не кодируется в UTF-8") from None
        digest.update(len(encoded).to_bytes(8, "big"))
        digest.update(encoded)
    return digest.hexdigest()


def alias_prefix_digest(aliases: Sequence[str], n: Optional[int] = None) -> str:
    """Digest первых n alias (по умолчанию всех) в домене mapping."""
    return _prefix_digest(MAPPING_DIGEST_DOMAIN, aliases, n)


def token_prefix_digest(tokens: Sequence[str], n: Optional[int] = None) -> str:
    """Digest первых n token (по умолчанию всех) в домене identifier."""
    return _prefix_digest(IDENTIFIER_DIGEST_DOMAIN, tokens, n)


def _attribute_values(entries: object, attribute: str) -> list:
    if not isinstance(entries, (list, tuple)):
        raise TypeError("entries должен быть list или tuple записей")
    values = []
    for entry in entries:
        try:
            values.append(getattr(entry, attribute))
        except AttributeError:
            raise TypeError("запись не содержит ожидаемого поля") from None
    return values


def mapping_entries_prefix_digest(entries: Sequence[object], n: Optional[int] = None) -> str:
    """Digest по полю alias записей mapping (real_value не читается)."""
    return alias_prefix_digest(_attribute_values(entries, "alias"), n)


def identifier_entries_prefix_digest(entries: Sequence[object], n: Optional[int] = None) -> str:
    """Digest по полю token записей identifier (identifier_value не читается)."""
    return token_prefix_digest(_attribute_values(entries, "token"), n)


# ----------------------------------------------------------------------
# Сериализация
# ----------------------------------------------------------------------


def _store_state_to_dict(state: StoreState) -> dict:
    return {"entry_count": state.entry_count, "prefix_digest": state.prefix_digest}


def _record_to_dict(record: ArtifactRecord) -> dict:
    return {
        "artifact_id": record.artifact_id,
        "kind": record.kind.value,
        "sha256": record.sha256,
        "created_at": record.created_at,
        "parent_artifact_ids": list(record.parent_artifact_ids),
        "period": record.period,
        "source_job_id": record.source_job_id,
        "provenance_id": record.provenance_id,
        "provenance_sha256": record.provenance_sha256,
    }


def serialize_manifest(manifest: WorkspaceManifest) -> bytes:
    """Канонические UTF-8 байты manifest v1 (без завершающего перевода строки)."""
    if not isinstance(manifest, WorkspaceManifest):
        raise TypeError("manifest должен быть WorkspaceManifest")
    payload = {
        "kind": MANIFEST_KIND,
        "schema_version": manifest.schema_version,
        "workspace_id": manifest.workspace_id,
        "label": manifest.label,
        "created_at": manifest.created_at,
        "revision": manifest.revision,
        "pending_store_mutation": manifest.pending_store_mutation,
        "stores": {
            "mapping": _store_state_to_dict(manifest.mapping_state),
            "identifier": _store_state_to_dict(manifest.identifier_state),
        },
        "artifacts": [_record_to_dict(record) for record in manifest.artifacts],
        "latest_analytical_artifact_id": manifest.latest_analytical_artifact_id,
    }
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


# ----------------------------------------------------------------------
# Строгий разбор
# ----------------------------------------------------------------------


class _UnsupportedVersion(Exception):
    """Внутренний сигнал: schema_version — целое число, но не 1."""


def _reject_duplicate_keys(pairs: list) -> dict:
    keys = [key for key, _ in pairs]
    if len(set(keys)) != len(keys):
        raise ValueError("duplicate keys")
    return dict(pairs)


def _reject_constant(_name: str) -> None:
    raise ValueError("non-finite constants are not allowed")


def _exact_keys(value: object, keys: frozenset) -> dict:
    if not isinstance(value, dict) or set(value) != keys:
        raise ValueError("unexpected object shape")
    return value


def _build_store_state(value: object) -> StoreState:
    obj = _exact_keys(value, _STORE_KEYS)
    return StoreState(entry_count=obj["entry_count"], prefix_digest=obj["prefix_digest"])


def _build_record(value: object) -> ArtifactRecord:
    obj = _exact_keys(value, _ARTIFACT_KEYS)
    kind_value = obj["kind"]
    if not isinstance(kind_value, str):
        raise ValueError("kind must be a string")
    parents = obj["parent_artifact_ids"]
    if not isinstance(parents, list):
        raise ValueError("parents must be a list")
    return ArtifactRecord(
        artifact_id=obj["artifact_id"],
        kind=ArtifactKind(kind_value),
        sha256=obj["sha256"],
        created_at=obj["created_at"],
        parent_artifact_ids=tuple(parents),
        period=obj["period"],
        source_job_id=obj["source_job_id"],
        provenance_id=obj["provenance_id"],
        provenance_sha256=obj["provenance_sha256"],
    )


def _build_manifest(payload: object) -> WorkspaceManifest:
    if not isinstance(payload, dict):
        raise ValueError("top level must be an object")

    version = payload.get("schema_version")
    if type(version) is int and version != MANIFEST_SCHEMA_VERSION:
        raise _UnsupportedVersion

    obj = _exact_keys(payload, _TOP_LEVEL_KEYS)
    if obj["kind"] != MANIFEST_KIND:
        raise ValueError("unexpected manifest kind")

    stores = _exact_keys(obj["stores"], _STORES_KEYS)
    artifacts = obj["artifacts"]
    if not isinstance(artifacts, list):
        raise ValueError("artifacts must be a list")

    return WorkspaceManifest(
        schema_version=obj["schema_version"],
        workspace_id=obj["workspace_id"],
        label=obj["label"],
        created_at=obj["created_at"],
        revision=obj["revision"],
        pending_store_mutation=obj["pending_store_mutation"],
        mapping_state=_build_store_state(stores["mapping"]),
        identifier_state=_build_store_state(stores["identifier"]),
        artifacts=tuple(_build_record(item) for item in artifacts),
        latest_analytical_artifact_id=obj["latest_analytical_artifact_id"],
    )


def parse_manifest(data: bytes) -> WorkspaceManifest:
    """
    Строго разбирает байты manifest v1.

    :raises TypeError: data не bytes.
    :raises WorkspaceCorruptedError: MANIFEST_UNSUPPORTED_VERSION либо
        MANIFEST_INVALID_PAYLOAD; сообщение фиксированное, исходное
        исключение подавляется (значения в текст не попадают).
    """
    if not isinstance(data, bytes):
        raise TypeError("data должен быть bytes")

    try:
        text = data.decode("utf-8", errors="strict")
        payload = json.loads(
            text,
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_constant,
        )
        return _build_manifest(payload)
    except _UnsupportedVersion:
        raise WorkspaceCorruptedError(WorkspaceCorruptedReason.MANIFEST_UNSUPPORTED_VERSION) from None
    except (ValueError, TypeError, KeyError, RecursionError):
        raise WorkspaceCorruptedError(WorkspaceCorruptedReason.MANIFEST_INVALID_PAYLOAD) from None
