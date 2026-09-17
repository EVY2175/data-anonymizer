"""
Тесты app.mapping.provenance_encrypted (EncryptedFileProvenanceStore).

Stage 7C.3 — correctness/security/lifecycle. Здесь НЕ тестируется:
anonymizer integration (Stage 7C.4), Writer/Restore (Stage 7C.5+).

Группы:
    A. Lifecycle (create/open, immediate empty sidecar, immediate reopen)
    B. Schema validation (strict deserialization)
    C. Load semantics (duplicates/conflicts on load)
    D. Initial creation failure
    E. add_many (success/duplicates/conflicts/invalid/generator)
    F. Persistence counts (reset after construction)
    G. add_many persistence failure
    H. clear()
    I. Confidentiality
    J. Unicode/whitespace preservation
    K. Round trip
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

import app.mapping.provenance_encrypted as provenance_encrypted_module
from app.mapping.provenance_base import ProvenanceConflictError
from app.mapping.provenance_encrypted import (
    PROVENANCE_PAYLOAD_SCHEMA_VERSION,
    EncryptedFileProvenanceStore,
    InvalidProvenancePayloadError,
    UnsupportedProvenancePayloadSchemaVersionError,
)
from app.models.provenance import IdentifierCellProvenance, IdentifierRepresentation
from app.security import crypto

PASSWORD = "test-password-123"
JOB_ID = "job-1"


@pytest.fixture()
def target_path(tmp_path: Path) -> Path:
    return tmp_path / "provenance.enc"


def _entry(
    sheet_name: str,
    row: int,
    column: int,
    token: str,
    representation: IdentifierRepresentation = IdentifierRepresentation.STRING,
) -> IdentifierCellProvenance:
    return IdentifierCellProvenance(
        sheet_name=sheet_name, row=row, column=column, token=token, representation=representation
    )


def _write_encrypted_payload(path: Path, plaintext: bytes, password: str = PASSWORD) -> None:
    """Собирает malformed/valid payload ИСКЛЮЧИТЕЛЬНО в памяти, шифрует и
    только тогда пишет encrypted bytes на диск."""
    encrypted = crypto.encrypt_bytes(plaintext, password)
    path.write_bytes(encrypted)


def _valid_entry_dict(
    sheet_name: str = "Sheet1",
    row: int = 2,
    column: int = 3,
    token: str = "INN_AAAAAAAAA",
    representation: str = "string",
) -> dict:
    return {
        "sheet_name": sheet_name,
        "row": row,
        "column": column,
        "token": token,
        "representation": representation,
    }


# ---------------------------------------------------------------------------
# A. Lifecycle
# ---------------------------------------------------------------------------


def test_absent_path_job_id_none_raises(target_path: Path) -> None:
    with pytest.raises(ValueError):
        EncryptedFileProvenanceStore(target_path, PASSWORD, job_id=None)


def test_absent_path_valid_job_id_succeeds(target_path: Path) -> None:
    store = EncryptedFileProvenanceStore(target_path, PASSWORD, job_id=JOB_ID)
    assert store.job_id == JOB_ID


def test_constructor_immediately_creates_file(target_path: Path) -> None:
    EncryptedFileProvenanceStore(target_path, PASSWORD, job_id=JOB_ID)
    assert target_path.exists()


def test_immediate_decrypt_shows_exact_empty_schema(target_path: Path) -> None:
    EncryptedFileProvenanceStore(target_path, PASSWORD, job_id=JOB_ID)
    raw = target_path.read_bytes()
    plaintext = crypto.decrypt_bytes(raw, PASSWORD)
    payload = json.loads(plaintext.decode("utf-8"))
    assert payload == {
        "schema_version": PROVENANCE_PAYLOAD_SCHEMA_VERSION,
        "job_id": JOB_ID,
        "entries": [],
    }


def test_immediate_reopen_without_job_id_succeeds(target_path: Path) -> None:
    EncryptedFileProvenanceStore(target_path, PASSWORD, job_id=JOB_ID)
    reopened = EncryptedFileProvenanceStore(target_path, PASSWORD)
    assert reopened.job_id == JOB_ID
    assert reopened.entries() == ()


def test_existing_file_with_supplied_job_id_raises(target_path: Path) -> None:
    EncryptedFileProvenanceStore(target_path, PASSWORD, job_id=JOB_ID)
    with pytest.raises(ValueError):
        EncryptedFileProvenanceStore(target_path, PASSWORD, job_id="job-2")


def test_existing_file_with_omitted_job_id_succeeds(target_path: Path) -> None:
    EncryptedFileProvenanceStore(target_path, PASSWORD, job_id=JOB_ID)
    reopened = EncryptedFileProvenanceStore(target_path, PASSWORD)
    assert reopened.job_id == JOB_ID


@pytest.mark.parametrize("bad_job_id", [123, "", "   "])
def test_malformed_job_id_at_create_raises(target_path: Path, bad_job_id: object) -> None:
    with pytest.raises(ValueError):
        EncryptedFileProvenanceStore(target_path, PASSWORD, job_id=bad_job_id)  # type: ignore[arg-type]


@pytest.mark.parametrize("bad_job_id_raw", [None, 123, "", "   "])
def test_malformed_job_id_in_payload_raises(target_path: Path, bad_job_id_raw: object) -> None:
    payload = {
        "schema_version": 1,
        "job_id": bad_job_id_raw,
        "entries": [],
    }
    _write_encrypted_payload(target_path, json.dumps(payload).encode("utf-8"))
    with pytest.raises(InvalidProvenancePayloadError):
        EncryptedFileProvenanceStore(target_path, PASSWORD)


def test_missing_parent_directory_raises(tmp_path: Path) -> None:
    missing = tmp_path / "does_not_exist" / "provenance.enc"
    with pytest.raises(FileNotFoundError):
        EncryptedFileProvenanceStore(missing, PASSWORD, job_id=JOB_ID)


def test_zero_byte_existing_file_is_treated_as_corrupt(target_path: Path) -> None:
    target_path.write_bytes(b"")
    with pytest.raises(crypto.InvalidContainerError):
        EncryptedFileProvenanceStore(target_path, PASSWORD)


def test_wrong_password_raises_decryption_error(target_path: Path) -> None:
    EncryptedFileProvenanceStore(target_path, PASSWORD, job_id=JOB_ID)
    with pytest.raises(crypto.DecryptionError):
        EncryptedFileProvenanceStore(target_path, "wrong-password")


def test_tampered_ciphertext_raises_decryption_error(target_path: Path) -> None:
    EncryptedFileProvenanceStore(target_path, PASSWORD, job_id=JOB_ID)

    raw = bytearray(target_path.read_bytes())
    raw[-1] ^= 0xFF
    target_path.write_bytes(bytes(raw))

    with pytest.raises(crypto.DecryptionError):
        EncryptedFileProvenanceStore(target_path, PASSWORD)


def test_job_id_supplied_on_existing_file_rejected_before_decrypt_attempted(
    target_path: Path,
) -> None:
    """
    Доказывает через наблюдаемое поведение (не через инспекцию реализации),
    что проверка "job_id не должен передаваться при открытии существующего
    sidecar" происходит ДО попытки decrypt/parse: если бы decrypt
    выполнялся первым, неверный пароль дал бы crypto.DecryptionError, а не
    ValueError.
    """
    EncryptedFileProvenanceStore(target_path, PASSWORD, job_id=JOB_ID)

    with pytest.raises(ValueError):
        EncryptedFileProvenanceStore(target_path, "wrong-password", job_id="job-2")


# ---------------------------------------------------------------------------
# B. Schema validation
# ---------------------------------------------------------------------------


def test_payload_root_not_object_raises(target_path: Path) -> None:
    _write_encrypted_payload(target_path, json.dumps([1, 2, 3]).encode("utf-8"))
    with pytest.raises(InvalidProvenancePayloadError):
        EncryptedFileProvenanceStore(target_path, PASSWORD)


def test_payload_missing_top_level_key_raises(target_path: Path) -> None:
    _write_encrypted_payload(
        target_path, json.dumps({"schema_version": 1, "entries": []}).encode("utf-8")
    )
    with pytest.raises(InvalidProvenancePayloadError):
        EncryptedFileProvenanceStore(target_path, PASSWORD)


def test_payload_unknown_top_level_key_raises(target_path: Path) -> None:
    _write_encrypted_payload(
        target_path,
        json.dumps(
            {"schema_version": 1, "job_id": JOB_ID, "entries": [], "extra": "x"}
        ).encode("utf-8"),
    )
    with pytest.raises(InvalidProvenancePayloadError):
        EncryptedFileProvenanceStore(target_path, PASSWORD)


def test_payload_schema_version_wrong_type_raises(target_path: Path) -> None:
    _write_encrypted_payload(
        target_path,
        json.dumps({"schema_version": "1", "job_id": JOB_ID, "entries": []}).encode("utf-8"),
    )
    with pytest.raises(InvalidProvenancePayloadError):
        EncryptedFileProvenanceStore(target_path, PASSWORD)


def test_payload_schema_version_bool_raises(target_path: Path) -> None:
    _write_encrypted_payload(
        target_path,
        json.dumps({"schema_version": True, "job_id": JOB_ID, "entries": []}).encode("utf-8"),
    )
    with pytest.raises(InvalidProvenancePayloadError):
        EncryptedFileProvenanceStore(target_path, PASSWORD)


def test_payload_unsupported_schema_version_raises(target_path: Path) -> None:
    _write_encrypted_payload(
        target_path,
        json.dumps({"schema_version": 999, "job_id": JOB_ID, "entries": []}).encode("utf-8"),
    )
    with pytest.raises(UnsupportedProvenancePayloadSchemaVersionError):
        EncryptedFileProvenanceStore(target_path, PASSWORD)


def test_payload_entries_not_list_raises(target_path: Path) -> None:
    _write_encrypted_payload(
        target_path,
        json.dumps({"schema_version": 1, "job_id": JOB_ID, "entries": {}}).encode("utf-8"),
    )
    with pytest.raises(InvalidProvenancePayloadError):
        EncryptedFileProvenanceStore(target_path, PASSWORD)


def test_payload_entry_not_object_raises(target_path: Path) -> None:
    _write_encrypted_payload(
        target_path,
        json.dumps(
            {"schema_version": 1, "job_id": JOB_ID, "entries": ["not-a-dict"]}
        ).encode("utf-8"),
    )
    with pytest.raises(InvalidProvenancePayloadError):
        EncryptedFileProvenanceStore(target_path, PASSWORD)


def test_payload_entry_missing_key_raises(target_path: Path) -> None:
    bad_entry = _valid_entry_dict()
    del bad_entry["token"]
    _write_encrypted_payload(
        target_path,
        json.dumps({"schema_version": 1, "job_id": JOB_ID, "entries": [bad_entry]}).encode(
            "utf-8"
        ),
    )
    with pytest.raises(InvalidProvenancePayloadError):
        EncryptedFileProvenanceStore(target_path, PASSWORD)


def test_payload_entry_unknown_key_raises(target_path: Path) -> None:
    bad_entry = _valid_entry_dict()
    bad_entry["extra_field"] = "unexpected"
    _write_encrypted_payload(
        target_path,
        json.dumps({"schema_version": 1, "job_id": JOB_ID, "entries": [bad_entry]}).encode(
            "utf-8"
        ),
    )
    with pytest.raises(InvalidProvenancePayloadError):
        EncryptedFileProvenanceStore(target_path, PASSWORD)


@pytest.mark.parametrize("bad_sheet_name", [None, 123, "", "   "])
def test_payload_bad_sheet_name_raises(target_path: Path, bad_sheet_name: object) -> None:
    bad_entry = _valid_entry_dict(sheet_name=bad_sheet_name)  # type: ignore[arg-type]
    _write_encrypted_payload(
        target_path,
        json.dumps({"schema_version": 1, "job_id": JOB_ID, "entries": [bad_entry]}).encode(
            "utf-8"
        ),
    )
    with pytest.raises(InvalidProvenancePayloadError):
        EncryptedFileProvenanceStore(target_path, PASSWORD)


@pytest.mark.parametrize("bad_row", [None, 0, -1, 1.5, "2", True, False])
def test_payload_bad_row_raises(target_path: Path, bad_row: object) -> None:
    bad_entry = _valid_entry_dict(row=bad_row)  # type: ignore[arg-type]
    _write_encrypted_payload(
        target_path,
        json.dumps({"schema_version": 1, "job_id": JOB_ID, "entries": [bad_entry]}).encode(
            "utf-8"
        ),
    )
    with pytest.raises(InvalidProvenancePayloadError):
        EncryptedFileProvenanceStore(target_path, PASSWORD)


@pytest.mark.parametrize("bad_column", [None, 0, -1, 1.5, "3", True, False])
def test_payload_bad_column_raises(target_path: Path, bad_column: object) -> None:
    bad_entry = _valid_entry_dict(column=bad_column)  # type: ignore[arg-type]
    _write_encrypted_payload(
        target_path,
        json.dumps({"schema_version": 1, "job_id": JOB_ID, "entries": [bad_entry]}).encode(
            "utf-8"
        ),
    )
    with pytest.raises(InvalidProvenancePayloadError):
        EncryptedFileProvenanceStore(target_path, PASSWORD)


@pytest.mark.parametrize("bad_token", [None, 123, "", "   "])
def test_payload_bad_token_raises(target_path: Path, bad_token: object) -> None:
    bad_entry = _valid_entry_dict(token=bad_token)  # type: ignore[arg-type]
    _write_encrypted_payload(
        target_path,
        json.dumps({"schema_version": 1, "job_id": JOB_ID, "entries": [bad_entry]}).encode(
            "utf-8"
        ),
    )
    with pytest.raises(InvalidProvenancePayloadError):
        EncryptedFileProvenanceStore(target_path, PASSWORD)


def test_payload_unknown_representation_raises(target_path: Path) -> None:
    bad_entry = _valid_entry_dict(representation="float")
    _write_encrypted_payload(
        target_path,
        json.dumps({"schema_version": 1, "job_id": JOB_ID, "entries": [bad_entry]}).encode(
            "utf-8"
        ),
    )
    with pytest.raises(InvalidProvenancePayloadError):
        EncryptedFileProvenanceStore(target_path, PASSWORD)


def test_unknown_representation_raw_value_not_exposed_in_message(target_path: Path) -> None:
    bad_entry = _valid_entry_dict(representation="SuperSecretRepresentation999")
    _write_encrypted_payload(
        target_path,
        json.dumps({"schema_version": 1, "job_id": JOB_ID, "entries": [bad_entry]}).encode(
            "utf-8"
        ),
    )
    with pytest.raises(InvalidProvenancePayloadError) as exc_info:
        EncryptedFileProvenanceStore(target_path, PASSWORD)
    message = str(exc_info.value)
    assert "SuperSecretRepresentation999" not in message
    assert "string" in message
    assert "integer" in message


# ---------------------------------------------------------------------------
# C. Load semantics — duplicates/conflicts
# ---------------------------------------------------------------------------


def test_exact_duplicate_persisted_entries_collapse(target_path: Path) -> None:
    entry_dict = _valid_entry_dict()
    _write_encrypted_payload(
        target_path,
        json.dumps(
            {"schema_version": 1, "job_id": JOB_ID, "entries": [entry_dict, entry_dict]}
        ).encode("utf-8"),
    )
    store = EncryptedFileProvenanceStore(target_path, PASSWORD)
    assert len(store.entries()) == 1


def test_coordinate_different_token_conflict_on_load_raises(target_path: Path) -> None:
    entry_a = _valid_entry_dict(token="INN_AAAAAAAAA")
    entry_b = _valid_entry_dict(token="INN_DIFFERENT")
    _write_encrypted_payload(
        target_path,
        json.dumps({"schema_version": 1, "job_id": JOB_ID, "entries": [entry_a, entry_b]}).encode(
            "utf-8"
        ),
    )
    with pytest.raises(ProvenanceConflictError):
        EncryptedFileProvenanceStore(target_path, PASSWORD)


def test_coordinate_same_token_different_representation_conflict_on_load_raises(
    target_path: Path,
) -> None:
    entry_a = _valid_entry_dict(representation="string")
    entry_b = _valid_entry_dict(representation="integer")
    _write_encrypted_payload(
        target_path,
        json.dumps({"schema_version": 1, "job_id": JOB_ID, "entries": [entry_a, entry_b]}).encode(
            "utf-8"
        ),
    )
    with pytest.raises(ProvenanceConflictError):
        EncryptedFileProvenanceStore(target_path, PASSWORD)


# ---------------------------------------------------------------------------
# D. Initial creation failure
# ---------------------------------------------------------------------------


def test_initial_serialization_failure_leaves_no_file(
    target_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def failing_serialize(store):
        raise RuntimeError("simulated serialization failure")

    monkeypatch.setattr(
        provenance_encrypted_module.EncryptedFileProvenanceStore,
        "_serialize",
        staticmethod(failing_serialize),
    )

    with pytest.raises(RuntimeError):
        EncryptedFileProvenanceStore(target_path, PASSWORD, job_id=JOB_ID)

    assert not target_path.exists()


def test_initial_encryption_failure_leaves_no_file(
    target_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def failing_encrypt(plaintext, password):
        raise RuntimeError("simulated encryption failure")

    monkeypatch.setattr(provenance_encrypted_module, "encrypt_bytes", failing_encrypt)

    with pytest.raises(RuntimeError):
        EncryptedFileProvenanceStore(target_path, PASSWORD, job_id=JOB_ID)

    assert not target_path.exists()


def test_initial_atomic_write_failure_leaves_no_file_and_cleans_temp(
    target_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def failing_replace(*args: object, **kwargs: object) -> None:
        raise OSError("simulated os.replace failure")

    monkeypatch.setattr(os, "replace", failing_replace)

    with pytest.raises(OSError):
        EncryptedFileProvenanceStore(target_path, PASSWORD, job_id=JOB_ID)

    monkeypatch.undo()

    assert not target_path.exists()
    leftover = list(target_path.parent.iterdir())
    assert leftover == []


# ---------------------------------------------------------------------------
# E. add_many
# ---------------------------------------------------------------------------


def test_add_many_accepts_generator(target_path: Path) -> None:
    store = EncryptedFileProvenanceStore(target_path, PASSWORD, job_id=JOB_ID)
    entries = [_entry("Sheet1", 1, 1, "INN_AAAAAAAAA"), _entry("Sheet1", 2, 1, "KPP_BBBBBBBBB")]
    store.add_many(e for e in entries)
    assert set(store.entries()) == set(entries)


def test_add_many_empty_batch_is_noop(target_path: Path) -> None:
    store = EncryptedFileProvenanceStore(target_path, PASSWORD, job_id=JOB_ID)
    content_before = target_path.read_bytes()
    store.add_many([])
    assert target_path.read_bytes() == content_before


def test_add_many_successful_batch(target_path: Path) -> None:
    store = EncryptedFileProvenanceStore(target_path, PASSWORD, job_id=JOB_ID)
    entry_a = _entry("Sheet1", 1, 1, "INN_AAAAAAAAA")
    entry_b = _entry("Sheet1", 2, 1, "KPP_BBBBBBBBB")
    store.add_many([entry_a, entry_b])
    assert set(store.entries()) == {entry_a, entry_b}

    reopened = EncryptedFileProvenanceStore(target_path, PASSWORD)
    assert set(reopened.entries()) == {entry_a, entry_b}


def test_add_many_duplicate_only_does_not_rewrite_file(target_path: Path) -> None:
    store = EncryptedFileProvenanceStore(target_path, PASSWORD, job_id=JOB_ID)
    entry = _entry("Sheet1", 1, 1, "INN_AAAAAAAAA")
    store.add_many([entry])
    content_before = target_path.read_bytes()

    store.add_many([entry])

    assert target_path.read_bytes() == content_before
    assert store.entries() == (entry,)


def test_add_many_mixed_existing_and_new_duplicates(target_path: Path) -> None:
    store = EncryptedFileProvenanceStore(target_path, PASSWORD, job_id=JOB_ID)
    entry_a = _entry("Sheet1", 1, 1, "INN_AAAAAAAAA")
    store.add_many([entry_a])

    entry_b = _entry("Sheet1", 2, 1, "KPP_BBBBBBBBB")
    store.add_many([entry_a, entry_b, entry_a])

    assert set(store.entries()) == {entry_a, entry_b}


def test_add_many_conflict_preserves_state(target_path: Path) -> None:
    store = EncryptedFileProvenanceStore(target_path, PASSWORD, job_id=JOB_ID)
    entry_a = _entry("Sheet1", 1, 1, "INN_AAAAAAAAA")
    store.add_many([entry_a])
    content_before = target_path.read_bytes()

    conflicting = _entry("Sheet1", 1, 1, "INN_DIFFERENT")
    with pytest.raises(ProvenanceConflictError):
        store.add_many([conflicting])

    assert target_path.read_bytes() == content_before
    assert store.entries() == (entry_a,)


def test_add_many_late_conflict_leaves_store_unchanged(target_path: Path) -> None:
    store = EncryptedFileProvenanceStore(target_path, PASSWORD, job_id=JOB_ID)
    entry_a = _entry("Sheet1", 1, 1, "INN_AAAAAAAAA")
    store.add_many([entry_a])
    initial_snapshot = store.entries()
    content_before = target_path.read_bytes()

    entry_b = _entry("Sheet1", 2, 1, "KPP_BBBBBBBBB")
    entry_c = _entry("Sheet1", 3, 1, "OGRN_CCCCCCCC")
    conflicting_a = _entry("Sheet1", 1, 1, "INN_CONFLICT")

    with pytest.raises(ProvenanceConflictError):
        store.add_many([entry_b, entry_c, conflicting_a])

    assert store.entries() == initial_snapshot
    assert target_path.read_bytes() == content_before


def test_add_many_intra_batch_conflict_leaves_store_unchanged(target_path: Path) -> None:
    store = EncryptedFileProvenanceStore(target_path, PASSWORD, job_id=JOB_ID)

    entry_a = _entry("Sheet1", 1, 1, "INN_AAAAAAAAA")
    entry_b = _entry("Sheet1", 2, 1, "KPP_BBBBBBBBB")
    conflicting_a = _entry("Sheet1", 1, 1, "INN_CONFLICT")

    with pytest.raises(ProvenanceConflictError):
        store.add_many([entry_a, entry_b, conflicting_a])

    assert store.entries() == ()
    leftover = [p for p in target_path.parent.iterdir() if p != target_path]
    assert leftover == []


def test_add_many_invalid_entry_late_in_batch(target_path: Path) -> None:
    store = EncryptedFileProvenanceStore(target_path, PASSWORD, job_id=JOB_ID)
    valid_a = _entry("Sheet1", 1, 1, "INN_AAAAAAAAA")
    valid_b = _entry("Sheet1", 2, 1, "KPP_BBBBBBBBB")

    with pytest.raises(TypeError):
        store.add_many([valid_a, "not-an-entry", valid_b])  # type: ignore[list-item]

    assert store.entries() == ()


def test_add_many_same_token_different_coordinates(target_path: Path) -> None:
    store = EncryptedFileProvenanceStore(target_path, PASSWORD, job_id=JOB_ID)
    entry_string = _entry("Sheet1", 1, 1, "INN_AAAAAAAAA", IdentifierRepresentation.STRING)
    entry_integer = _entry("Sheet1", 2, 1, "INN_AAAAAAAAA", IdentifierRepresentation.INTEGER)
    store.add_many([entry_string, entry_integer])
    assert len(store.entries()) == 2


# ---------------------------------------------------------------------------
# F. Persistence counts (reset AFTER construction)
# ---------------------------------------------------------------------------


def _install_persistence_counters(monkeypatch: pytest.MonkeyPatch) -> dict:
    calls = {"persist": 0, "encrypt": 0, "atomic_write": 0}
    real_persist = provenance_encrypted_module.EncryptedFileProvenanceStore._persist
    real_encrypt = provenance_encrypted_module.encrypt_bytes
    real_atomic_write = provenance_encrypted_module.EncryptedFileProvenanceStore._atomic_write

    def counting_persist(self, candidate):
        calls["persist"] += 1
        return real_persist(self, candidate)

    def counting_encrypt(plaintext, password):
        calls["encrypt"] += 1
        return real_encrypt(plaintext, password)

    def counting_atomic_write(self, encrypted):
        calls["atomic_write"] += 1
        return real_atomic_write(self, encrypted)

    monkeypatch.setattr(
        provenance_encrypted_module.EncryptedFileProvenanceStore, "_persist", counting_persist
    )
    monkeypatch.setattr(provenance_encrypted_module, "encrypt_bytes", counting_encrypt)
    monkeypatch.setattr(
        provenance_encrypted_module.EncryptedFileProvenanceStore,
        "_atomic_write",
        counting_atomic_write,
    )
    return calls


def test_successful_changed_batch_persists_exactly_once_after_construction(
    target_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = EncryptedFileProvenanceStore(target_path, PASSWORD, job_id=JOB_ID)

    calls = _install_persistence_counters(monkeypatch)  # counters reset AFTER construction

    store.add_many(
        [_entry("Sheet1", 1, 1, "INN_AAAAAAAAA"), _entry("Sheet1", 2, 1, "KPP_BBBBBBBBB")]
    )

    assert calls == {"persist": 1, "encrypt": 1, "atomic_write": 1}


def test_duplicate_only_batch_zero_additional_persistence(
    target_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = EncryptedFileProvenanceStore(target_path, PASSWORD, job_id=JOB_ID)
    entry = _entry("Sheet1", 1, 1, "INN_AAAAAAAAA")
    store.add_many([entry])

    calls = _install_persistence_counters(monkeypatch)

    store.add_many([entry])

    assert calls == {"persist": 0, "encrypt": 0, "atomic_write": 0}


def test_conflict_zero_additional_persistence(
    target_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = EncryptedFileProvenanceStore(target_path, PASSWORD, job_id=JOB_ID)
    entry_a = _entry("Sheet1", 1, 1, "INN_AAAAAAAAA")
    store.add_many([entry_a])

    calls = _install_persistence_counters(monkeypatch)

    with pytest.raises(ProvenanceConflictError):
        store.add_many([_entry("Sheet1", 1, 1, "INN_DIFFERENT")])

    assert calls == {"persist": 0, "encrypt": 0, "atomic_write": 0}


def test_invalid_entry_zero_additional_persistence(
    target_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = EncryptedFileProvenanceStore(target_path, PASSWORD, job_id=JOB_ID)

    calls = _install_persistence_counters(monkeypatch)

    with pytest.raises(TypeError):
        store.add_many([_entry("Sheet1", 1, 1, "INN_AAAAAAAAA"), "not-an-entry"])  # type: ignore[list-item]

    assert calls == {"persist": 0, "encrypt": 0, "atomic_write": 0}


def test_empty_batch_zero_additional_persistence(
    target_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = EncryptedFileProvenanceStore(target_path, PASSWORD, job_id=JOB_ID)

    calls = _install_persistence_counters(monkeypatch)

    store.add_many([])

    assert calls == {"persist": 0, "encrypt": 0, "atomic_write": 0}


# ---------------------------------------------------------------------------
# G. add_many persistence failure
# ---------------------------------------------------------------------------


def test_add_many_failed_os_replace_preserves_ram_and_disk(
    target_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = EncryptedFileProvenanceStore(target_path, PASSWORD, job_id=JOB_ID)
    initial_entry = _entry("Sheet1", 1, 1, "INN_AAAAAAAAA")
    store.add_many([initial_entry])
    original_bytes = target_path.read_bytes()

    def failing_replace(*args: object, **kwargs: object) -> None:
        raise OSError("simulated os.replace failure")

    monkeypatch.setattr(os, "replace", failing_replace)

    with pytest.raises(OSError):
        store.add_many([_entry("Sheet1", 2, 1, "KPP_BBBBBBBBB")])

    monkeypatch.undo()

    assert target_path.read_bytes() == original_bytes
    assert store.entries() == (initial_entry,)

    reopened = EncryptedFileProvenanceStore(target_path, PASSWORD)
    assert reopened.entries() == (initial_entry,)
    assert reopened.job_id == JOB_ID


def test_add_many_failed_fsync_preserves_state_and_removes_temp(
    target_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = EncryptedFileProvenanceStore(target_path, PASSWORD, job_id=JOB_ID)
    initial_entry = _entry("Sheet1", 1, 1, "INN_AAAAAAAAA")
    store.add_many([initial_entry])
    original_bytes = target_path.read_bytes()

    def failing_fsync(fd: int) -> None:
        raise OSError("simulated fsync failure")

    monkeypatch.setattr(os, "fsync", failing_fsync)

    with pytest.raises(OSError):
        store.add_many([_entry("Sheet1", 2, 1, "KPP_BBBBBBBBB")])

    monkeypatch.undo()

    assert target_path.read_bytes() == original_bytes
    assert store.entries() == (initial_entry,)

    leftover = [p for p in target_path.parent.iterdir() if p != target_path]
    assert leftover == []


# ---------------------------------------------------------------------------
# H. clear()
# ---------------------------------------------------------------------------


def test_clear_on_immediately_created_empty_store_is_noop(target_path: Path) -> None:
    store = EncryptedFileProvenanceStore(target_path, PASSWORD, job_id=JOB_ID)
    content_before = target_path.read_bytes()

    store.clear()

    assert target_path.read_bytes() == content_before
    assert store.entries() == ()
    assert store.job_id == JOB_ID


def test_clear_on_populated_store_persists_empty_payload(target_path: Path) -> None:
    store = EncryptedFileProvenanceStore(target_path, PASSWORD, job_id=JOB_ID)
    store.add_many([_entry("Sheet1", 1, 1, "INN_AAAAAAAAA")])
    content_before = target_path.read_bytes()

    store.clear()

    assert store.entries() == ()
    assert store.job_id == JOB_ID
    assert target_path.read_bytes() != content_before

    raw = target_path.read_bytes()
    plaintext = crypto.decrypt_bytes(raw, PASSWORD)
    payload = json.loads(plaintext.decode("utf-8"))
    assert payload == {
        "schema_version": PROVENANCE_PAYLOAD_SCHEMA_VERSION,
        "job_id": JOB_ID,
        "entries": [],
    }


def test_reopen_after_clear_shows_same_job_id_and_empty_entries(target_path: Path) -> None:
    store = EncryptedFileProvenanceStore(target_path, PASSWORD, job_id=JOB_ID)
    store.add_many([_entry("Sheet1", 1, 1, "INN_AAAAAAAAA")])
    store.clear()

    reopened = EncryptedFileProvenanceStore(target_path, PASSWORD)
    assert reopened.job_id == JOB_ID
    assert reopened.entries() == ()


def test_clear_failure_preserves_old_recoverable_state(
    target_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = EncryptedFileProvenanceStore(target_path, PASSWORD, job_id=JOB_ID)
    entry = _entry("Sheet1", 1, 1, "INN_AAAAAAAAA")
    store.add_many([entry])
    original_bytes = target_path.read_bytes()

    def failing_replace(*args: object, **kwargs: object) -> None:
        raise OSError("simulated os.replace failure")

    monkeypatch.setattr(os, "replace", failing_replace)

    with pytest.raises(OSError):
        store.clear()

    monkeypatch.undo()

    assert target_path.read_bytes() == original_bytes
    assert store.entries() == (entry,)

    reopened = EncryptedFileProvenanceStore(target_path, PASSWORD)
    assert reopened.entries() == (entry,)


# ---------------------------------------------------------------------------
# I. Confidentiality
# ---------------------------------------------------------------------------


def test_empty_sidecar_bytes_do_not_contain_job_id(target_path: Path) -> None:
    secret_job_id = "SuperSecretSyntheticJobId"
    EncryptedFileProvenanceStore(target_path, PASSWORD, job_id=secret_job_id)
    raw = target_path.read_bytes()
    assert secret_job_id.encode("utf-8") not in raw


def test_populated_sidecar_bytes_do_not_contain_job_id_sheet_name_token(target_path: Path) -> None:
    secret_job_id = "SuperSecretSyntheticJobId"
    secret_sheet_name = "SuperSecretSheetName"
    secret_token = "INN_SUPERSECRETTOKEN"

    store = EncryptedFileProvenanceStore(target_path, PASSWORD, job_id=secret_job_id)
    store.add_many([_entry(secret_sheet_name, 1, 1, secret_token)])

    raw = target_path.read_bytes()
    assert secret_job_id.encode("utf-8") not in raw
    assert secret_sheet_name.encode("utf-8") not in raw
    assert secret_token.encode("utf-8") not in raw


def test_conflict_message_does_not_expose_secrets(target_path: Path) -> None:
    secret_job_id = "SuperSecretSyntheticJobId"
    secret_sheet_name = "SuperSecretSheetName"
    secret_token = "INN_SUPERSECRETTOKEN"

    store = EncryptedFileProvenanceStore(target_path, PASSWORD, job_id=secret_job_id)
    store.add_many([_entry(secret_sheet_name, 1, 1, secret_token)])

    with pytest.raises(ProvenanceConflictError) as exc_info:
        store.add_many([_entry(secret_sheet_name, 1, 1, "INN_DIFFERENTTOKEN")])

    message = str(exc_info.value)
    assert secret_job_id not in message
    assert secret_sheet_name not in message
    assert secret_token not in message
    assert PASSWORD not in message


def test_payload_error_message_does_not_expose_secrets(target_path: Path) -> None:
    secret_sheet_name = "SuperSecretSheetName"
    bad_entry = _valid_entry_dict(sheet_name=secret_sheet_name, representation="not-a-real-kind")
    _write_encrypted_payload(
        target_path,
        json.dumps({"schema_version": 1, "job_id": JOB_ID, "entries": [bad_entry]}).encode(
            "utf-8"
        ),
    )
    with pytest.raises(InvalidProvenancePayloadError) as exc_info:
        EncryptedFileProvenanceStore(target_path, PASSWORD)

    message = str(exc_info.value)
    assert secret_sheet_name not in message
    assert PASSWORD not in message


# ---------------------------------------------------------------------------
# J. Unicode / whitespace preservation
# ---------------------------------------------------------------------------


def test_unicode_sheet_name_round_trip_exact(target_path: Path) -> None:
    sheet_name = "Лист Данных 1"
    store = EncryptedFileProvenanceStore(target_path, PASSWORD, job_id=JOB_ID)
    entry = _entry(sheet_name, 1, 1, "INN_AAAAAAAAA")
    store.add_many([entry])

    reopened = EncryptedFileProvenanceStore(target_path, PASSWORD)
    assert reopened.get_by_coordinate(sheet_name, 1, 1) == entry


def test_outer_whitespace_preserved_exact_job_id_sheet_name_token(target_path: Path) -> None:
    padded_job_id = " job-1 "
    padded_sheet_name = " Sheet1 "
    padded_token = " INN_AAAAAAAAA "

    store = EncryptedFileProvenanceStore(target_path, PASSWORD, job_id=padded_job_id)
    entry = _entry(padded_sheet_name, 1, 1, padded_token)
    store.add_many([entry])

    reopened = EncryptedFileProvenanceStore(target_path, PASSWORD)
    assert reopened.job_id == padded_job_id
    assert reopened.get_by_coordinate(padded_sheet_name, 1, 1) == entry
    assert reopened.entries()[0].token == padded_token


# ---------------------------------------------------------------------------
# K. Round trip
# ---------------------------------------------------------------------------


def test_full_round_trip_create_populate_discard_reopen(target_path: Path) -> None:
    store = EncryptedFileProvenanceStore(target_path, PASSWORD, job_id=JOB_ID)
    entries = [
        _entry("SheetB", 5, 2, "INN_AAAAAAAAA"),
        _entry("SheetA", 10, 1, "KPP_BBBBBBBBB", IdentifierRepresentation.INTEGER),
        _entry("SheetA", 2, 3, "OGRN_CCCCCCCC"),
    ]
    store.add_many(entries)
    del store

    reopened = EncryptedFileProvenanceStore(target_path, PASSWORD)
    assert reopened.job_id == JOB_ID
    assert set(reopened.entries()) == set(entries)
    # Deterministic ordering: (sheet_name, row, column)
    assert [e.sheet_name for e in reopened.entries()] == ["SheetA", "SheetA", "SheetB"]


def test_immediate_reopen_before_any_add_many(target_path: Path) -> None:
    EncryptedFileProvenanceStore(target_path, PASSWORD, job_id=JOB_ID)
    reopened = EncryptedFileProvenanceStore(target_path, PASSWORD)
    assert reopened.job_id == JOB_ID
    assert reopened.entries() == ()
