"""
Тесты app.mapping.encrypted_identifier_file (EncryptedFileIdentifierMappingStore).

Stage 7B.4.1 — correctness/security. НЕ покрывается здесь: performance
smoke gate / batching (Stage 7B.4.2), anonymizer integration (Stage 7B.5),
provenance (отдельный будущий этап).

Группы:
    A. constructor/basic contract
    B. persistence/load
    C. encryption/no plaintext
    D. identifier mapping semantics
    E. crypto error propagation
    F. strict payload validation
    G. atomicity/failure injection
    H. temp contains encrypted bytes
    I. password confidentiality
    J. identifier confidentiality
    K. exact-value preservation
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from app.mapping.encrypted_identifier_file import (
    EncryptedFileIdentifierMappingStore,
    IDENTIFIER_PAYLOAD_SCHEMA_VERSION,
    InvalidIdentifierPayloadError,
    UnsupportedIdentifierPayloadSchemaVersionError,
)
from app.mapping.identifier_base import IdentifierMappingConflictError, IdentifierMappingStore
from app.models.identifiers import IdentifierMappingEntry, IdentifierType
from app.security import crypto

PASSWORD = "test-password-123"


@pytest.fixture()
def target_path(tmp_path: Path) -> Path:
    return tmp_path / "identifiers.enc"


def _entry(
    token: str,
    identifier_value: str,
    identifier_type: IdentifierType = IdentifierType.INN,
) -> IdentifierMappingEntry:
    return IdentifierMappingEntry(
        token=token, identifier_value=identifier_value, identifier_type=identifier_type
    )


def _write_encrypted_payload(path: Path, plaintext: bytes, password: str = PASSWORD) -> None:
    """
    Собирает malformed/valid payload ИСКЛЮЧИТЕЛЬНО в памяти, шифрует его и
    только тогда пишет encrypted bytes на диск.
    """
    encrypted = crypto.encrypt_bytes(plaintext, password)
    path.write_bytes(encrypted)


def _valid_entry_dict(
    token: str = "INN_7GQ2MX9KA",
    identifier_value: str = "7701234567",
    identifier_type: str = "inn",
) -> dict:
    return {"token": token, "identifier_value": identifier_value, "identifier_type": identifier_type}


# ---------------------------------------------------------------------------
# A. Constructor / basic contract
# ---------------------------------------------------------------------------


def test_store_satisfies_identifier_mapping_store_contract(target_path: Path) -> None:
    store = EncryptedFileIdentifierMappingStore(target_path, PASSWORD)
    assert isinstance(store, IdentifierMappingStore)


def test_constructor_rejects_non_str_password(target_path: Path) -> None:
    with pytest.raises(TypeError):
        EncryptedFileIdentifierMappingStore(target_path, 12345)  # type: ignore[arg-type]


def test_constructor_rejects_non_path_like(tmp_path: Path) -> None:
    with pytest.raises(TypeError):
        EncryptedFileIdentifierMappingStore(12345, PASSWORD)  # type: ignore[arg-type]


def test_missing_parent_directory_raises_file_not_found(tmp_path: Path) -> None:
    missing = tmp_path / "does_not_exist" / "identifiers.enc"
    with pytest.raises(FileNotFoundError):
        EncryptedFileIdentifierMappingStore(missing, PASSWORD)


# ---------------------------------------------------------------------------
# B. Persistence / load
# ---------------------------------------------------------------------------


def test_nonexistent_file_gives_empty_store(target_path: Path) -> None:
    store = EncryptedFileIdentifierMappingStore(target_path, PASSWORD)
    assert store.entries() == ()
    assert store.all_tokens() == set()
    assert store.get_by_token("INN_ANYTHING") is None


def test_constructor_does_not_create_missing_file(target_path: Path) -> None:
    EncryptedFileIdentifierMappingStore(target_path, PASSWORD)
    assert not target_path.exists()


def test_add_creates_encrypted_file(target_path: Path) -> None:
    store = EncryptedFileIdentifierMappingStore(target_path, PASSWORD)
    store.add(_entry("INN_7GQ2MX9KA", "7701234567"))
    assert target_path.exists()


def test_reopen_with_same_password_restores_entry(target_path: Path) -> None:
    entry = _entry("INN_7GQ2MX9KA", "7701234567")
    store = EncryptedFileIdentifierMappingStore(target_path, PASSWORD)
    store.add(entry)

    reopened = EncryptedFileIdentifierMappingStore(target_path, PASSWORD)
    assert reopened.get_by_token("INN_7GQ2MX9KA") == entry


def test_multiple_entries_roundtrip(target_path: Path) -> None:
    entries = [
        _entry("INN_AAAAAAAAA", "7701234567", IdentifierType.INN),
        _entry("KPP_BBBBBBBBB", "770101001", IdentifierType.KPP),
        _entry("OGRN_CCCCCCCC", "1047709000008", IdentifierType.OGRN),
    ]
    store = EncryptedFileIdentifierMappingStore(target_path, PASSWORD)
    for entry in entries:
        store.add(entry)

    reopened = EncryptedFileIdentifierMappingStore(target_path, PASSWORD)
    assert set(reopened.entries()) == set(entries)


def test_clear_persists_empty_store(target_path: Path) -> None:
    store = EncryptedFileIdentifierMappingStore(target_path, PASSWORD)
    store.add(_entry("INN_AAAAAAAAA", "7701234567"))
    store.clear()

    reopened = EncryptedFileIdentifierMappingStore(target_path, PASSWORD)
    assert reopened.entries() == ()
    assert reopened.all_tokens() == set()


def test_clear_on_already_empty_store_is_noop(target_path: Path) -> None:
    store = EncryptedFileIdentifierMappingStore(target_path, PASSWORD)
    store.clear()
    assert not target_path.exists()


def test_exact_duplicate_add_is_idempotent_and_does_not_rewrite_file(target_path: Path) -> None:
    store = EncryptedFileIdentifierMappingStore(target_path, PASSWORD)
    entry = _entry("INN_AAAAAAAAA", "7701234567")
    store.add(entry)
    content_before = target_path.read_bytes()

    store.add(entry)  # точно та же запись

    assert target_path.read_bytes() == content_before
    assert store.entries() == (entry,)


def test_add_rejects_non_identifier_mapping_entry(target_path: Path) -> None:
    store = EncryptedFileIdentifierMappingStore(target_path, PASSWORD)
    with pytest.raises(TypeError):
        store.add("not-an-identifier-mapping-entry")  # type: ignore[arg-type]
    assert not target_path.exists()


# ---------------------------------------------------------------------------
# C. Encryption / no plaintext
# ---------------------------------------------------------------------------


def test_encrypted_file_does_not_contain_plaintext_secrets(target_path: Path) -> None:
    identifier_value = "7701234567"
    token = "INN_SECRETTOKEN1"
    store = EncryptedFileIdentifierMappingStore(target_path, PASSWORD)
    store.add(_entry(token, identifier_value))

    raw = target_path.read_bytes()
    assert identifier_value.encode("utf-8") not in raw
    assert token.encode("utf-8") not in raw
    assert PASSWORD.encode("utf-8") not in raw
    assert b"schema_version" not in raw
    assert b"entries" not in raw
    assert b"identifier_value" not in raw


def test_encrypted_file_matches_stage3_container_format(target_path: Path) -> None:
    store = EncryptedFileIdentifierMappingStore(target_path, PASSWORD)
    store.add(_entry("INN_AAAAAAAAA", "7701234567"))

    raw = target_path.read_bytes()
    assert raw[:4] == crypto.MAGIC
    assert len(raw) >= crypto._HEADER_V1_SIZE + crypto.GCM_TAG_SIZE

    plaintext = crypto.decrypt_bytes(raw, PASSWORD)
    payload = json.loads(plaintext.decode("utf-8"))
    assert payload["schema_version"] == IDENTIFIER_PAYLOAD_SCHEMA_VERSION
    assert payload["entries"][0]["token"] == "INN_AAAAAAAAA"


# ---------------------------------------------------------------------------
# D. Identifier mapping semantics
# ---------------------------------------------------------------------------


def test_token_conflict_raises_and_preserves_state(target_path: Path) -> None:
    store = EncryptedFileIdentifierMappingStore(target_path, PASSWORD)
    store.add(_entry("INN_AAAAAAAAA", "7701234567"))
    content_before = target_path.read_bytes()

    with pytest.raises(IdentifierMappingConflictError):
        store.add(_entry("INN_AAAAAAAAA", "7812345678"))

    assert store.get_by_token("INN_AAAAAAAAA").identifier_value == "7701234567"
    assert target_path.read_bytes() == content_before


def test_identity_conflict_raises(target_path: Path) -> None:
    store = EncryptedFileIdentifierMappingStore(target_path, PASSWORD)
    store.add(_entry("INN_AAAAAAAAA", "7701234567"))
    with pytest.raises(IdentifierMappingConflictError):
        store.add(_entry("INN_BBBBBBBBB", "7701234567"))
    assert store.get_by_token("INN_BBBBBBBBB") is None


def test_same_identifier_value_different_type_allowed(target_path: Path) -> None:
    store = EncryptedFileIdentifierMappingStore(target_path, PASSWORD)
    inn_entry = _entry("INN_AAAAAAAAA", "123456789", IdentifierType.INN)
    kpp_entry = _entry("KPP_BBBBBBBBB", "123456789", IdentifierType.KPP)
    store.add(inn_entry)
    store.add(kpp_entry)  # не конфликт: разный identifier_type => разная identity

    assert store.get_by_identity(IdentifierType.INN, "123456789") == inn_entry
    assert store.get_by_identity(IdentifierType.KPP, "123456789") == kpp_entry


def test_all_tokens_snapshot_is_independent(target_path: Path) -> None:
    store = EncryptedFileIdentifierMappingStore(target_path, PASSWORD)
    store.add(_entry("INN_AAAAAAAAA", "7701234567"))
    tokens = store.all_tokens()
    tokens.add("INN_INJECTED")
    tokens.discard("INN_AAAAAAAAA")

    assert store.all_tokens() == {"INN_AAAAAAAAA"}


def test_entries_snapshot_is_independent(target_path: Path) -> None:
    entry_a = _entry("INN_AAAAAAAAA", "7701234567")
    store = EncryptedFileIdentifierMappingStore(target_path, PASSWORD)
    store.add(entry_a)
    snapshot = store.entries()

    store.add(_entry("KPP_BBBBBBBBB", "770101001", IdentifierType.KPP))

    assert snapshot == (entry_a,)
    assert len(store.entries()) == 2


def test_get_by_token_rejects_invalid_token(target_path: Path) -> None:
    store = EncryptedFileIdentifierMappingStore(target_path, PASSWORD)
    with pytest.raises(ValueError):
        store.get_by_token("")


# ---------------------------------------------------------------------------
# E. Crypto error propagation
# ---------------------------------------------------------------------------


def test_wrong_password_raises_decryption_error(target_path: Path) -> None:
    store = EncryptedFileIdentifierMappingStore(target_path, PASSWORD)
    store.add(_entry("INN_AAAAAAAAA", "7701234567"))

    with pytest.raises(crypto.DecryptionError):
        EncryptedFileIdentifierMappingStore(target_path, "wrong-password")


def test_tampered_ciphertext_raises_decryption_error(target_path: Path) -> None:
    store = EncryptedFileIdentifierMappingStore(target_path, PASSWORD)
    store.add(_entry("INN_AAAAAAAAA", "7701234567"))

    raw = bytearray(target_path.read_bytes())
    raw[-1] ^= 0xFF
    target_path.write_bytes(bytes(raw))

    with pytest.raises(crypto.DecryptionError):
        EncryptedFileIdentifierMappingStore(target_path, PASSWORD)


def test_unsupported_container_version_propagates_stage3_exception(target_path: Path) -> None:
    store = EncryptedFileIdentifierMappingStore(target_path, PASSWORD)
    store.add(_entry("INN_AAAAAAAAA", "7701234567"))

    raw = bytearray(target_path.read_bytes())
    raw[4] = 99  # смещение поля version в header'е
    target_path.write_bytes(bytes(raw))

    with pytest.raises(crypto.UnsupportedContainerVersionError):
        EncryptedFileIdentifierMappingStore(target_path, PASSWORD)


# ---------------------------------------------------------------------------
# F. Strict payload validation — malformed payload собирается ТОЛЬКО в памяти
# ---------------------------------------------------------------------------


def test_invalid_utf8_payload_raises(target_path: Path) -> None:
    _write_encrypted_payload(target_path, b"\xff\xfe\xfa not valid utf-8")
    with pytest.raises(InvalidIdentifierPayloadError):
        EncryptedFileIdentifierMappingStore(target_path, PASSWORD)


def test_invalid_json_payload_raises(target_path: Path) -> None:
    _write_encrypted_payload(target_path, b"{not valid json")
    with pytest.raises(InvalidIdentifierPayloadError):
        EncryptedFileIdentifierMappingStore(target_path, PASSWORD)


def test_payload_root_not_object_raises(target_path: Path) -> None:
    _write_encrypted_payload(target_path, json.dumps([1, 2, 3]).encode("utf-8"))
    with pytest.raises(InvalidIdentifierPayloadError):
        EncryptedFileIdentifierMappingStore(target_path, PASSWORD)


def test_payload_missing_schema_version_raises(target_path: Path) -> None:
    _write_encrypted_payload(target_path, json.dumps({"entries": []}).encode("utf-8"))
    with pytest.raises(InvalidIdentifierPayloadError):
        EncryptedFileIdentifierMappingStore(target_path, PASSWORD)


def test_payload_missing_entries_raises(target_path: Path) -> None:
    _write_encrypted_payload(target_path, json.dumps({"schema_version": 1}).encode("utf-8"))
    with pytest.raises(InvalidIdentifierPayloadError):
        EncryptedFileIdentifierMappingStore(target_path, PASSWORD)


def test_payload_unknown_top_level_key_raises(target_path: Path) -> None:
    _write_encrypted_payload(
        target_path,
        json.dumps({"schema_version": 1, "entries": [], "extra": "field"}).encode("utf-8"),
    )
    with pytest.raises(InvalidIdentifierPayloadError):
        EncryptedFileIdentifierMappingStore(target_path, PASSWORD)


def test_payload_schema_version_bool_raises(target_path: Path) -> None:
    _write_encrypted_payload(
        target_path, json.dumps({"schema_version": True, "entries": []}).encode("utf-8")
    )
    with pytest.raises(InvalidIdentifierPayloadError):
        EncryptedFileIdentifierMappingStore(target_path, PASSWORD)


def test_payload_schema_version_wrong_type_raises(target_path: Path) -> None:
    _write_encrypted_payload(
        target_path, json.dumps({"schema_version": "1", "entries": []}).encode("utf-8")
    )
    with pytest.raises(InvalidIdentifierPayloadError):
        EncryptedFileIdentifierMappingStore(target_path, PASSWORD)


def test_payload_unsupported_schema_version_raises(target_path: Path) -> None:
    _write_encrypted_payload(
        target_path, json.dumps({"schema_version": 999, "entries": []}).encode("utf-8")
    )
    with pytest.raises(UnsupportedIdentifierPayloadSchemaVersionError):
        EncryptedFileIdentifierMappingStore(target_path, PASSWORD)


def test_payload_entries_not_list_raises(target_path: Path) -> None:
    _write_encrypted_payload(
        target_path, json.dumps({"schema_version": 1, "entries": {}}).encode("utf-8")
    )
    with pytest.raises(InvalidIdentifierPayloadError):
        EncryptedFileIdentifierMappingStore(target_path, PASSWORD)


def test_payload_entry_not_object_raises(target_path: Path) -> None:
    _write_encrypted_payload(
        target_path,
        json.dumps({"schema_version": 1, "entries": ["not-a-dict"]}).encode("utf-8"),
    )
    with pytest.raises(InvalidIdentifierPayloadError):
        EncryptedFileIdentifierMappingStore(target_path, PASSWORD)


def test_payload_entry_missing_token_raises(target_path: Path) -> None:
    bad_entry = {"identifier_value": "7701234567", "identifier_type": "inn"}
    _write_encrypted_payload(
        target_path, json.dumps({"schema_version": 1, "entries": [bad_entry]}).encode("utf-8")
    )
    with pytest.raises(InvalidIdentifierPayloadError):
        EncryptedFileIdentifierMappingStore(target_path, PASSWORD)


def test_payload_entry_missing_identifier_value_raises(target_path: Path) -> None:
    bad_entry = {"token": "INN_AAAAAAAAA", "identifier_type": "inn"}
    _write_encrypted_payload(
        target_path, json.dumps({"schema_version": 1, "entries": [bad_entry]}).encode("utf-8")
    )
    with pytest.raises(InvalidIdentifierPayloadError):
        EncryptedFileIdentifierMappingStore(target_path, PASSWORD)


def test_payload_entry_missing_identifier_type_raises(target_path: Path) -> None:
    bad_entry = {"token": "INN_AAAAAAAAA", "identifier_value": "7701234567"}
    _write_encrypted_payload(
        target_path, json.dumps({"schema_version": 1, "entries": [bad_entry]}).encode("utf-8")
    )
    with pytest.raises(InvalidIdentifierPayloadError):
        EncryptedFileIdentifierMappingStore(target_path, PASSWORD)


def test_payload_entry_unknown_key_raises(target_path: Path) -> None:
    bad_entry = _valid_entry_dict()
    bad_entry["extra_field"] = "unexpected"
    _write_encrypted_payload(
        target_path, json.dumps({"schema_version": 1, "entries": [bad_entry]}).encode("utf-8")
    )
    with pytest.raises(InvalidIdentifierPayloadError):
        EncryptedFileIdentifierMappingStore(target_path, PASSWORD)


def test_payload_token_wrong_type_raises(target_path: Path) -> None:
    bad_entry = _valid_entry_dict(token=12345)  # type: ignore[arg-type]
    _write_encrypted_payload(
        target_path, json.dumps({"schema_version": 1, "entries": [bad_entry]}).encode("utf-8")
    )
    with pytest.raises(InvalidIdentifierPayloadError):
        EncryptedFileIdentifierMappingStore(target_path, PASSWORD)


def test_payload_token_empty_raises(target_path: Path) -> None:
    bad_entry = _valid_entry_dict(token="")
    _write_encrypted_payload(
        target_path, json.dumps({"schema_version": 1, "entries": [bad_entry]}).encode("utf-8")
    )
    with pytest.raises(InvalidIdentifierPayloadError):
        EncryptedFileIdentifierMappingStore(target_path, PASSWORD)


def test_payload_token_whitespace_only_raises(target_path: Path) -> None:
    bad_entry = _valid_entry_dict(token="   ")
    _write_encrypted_payload(
        target_path, json.dumps({"schema_version": 1, "entries": [bad_entry]}).encode("utf-8")
    )
    with pytest.raises(InvalidIdentifierPayloadError):
        EncryptedFileIdentifierMappingStore(target_path, PASSWORD)


def test_payload_identifier_value_wrong_type_raises(target_path: Path) -> None:
    bad_entry = _valid_entry_dict(identifier_value=7701234567)  # type: ignore[arg-type]
    _write_encrypted_payload(
        target_path, json.dumps({"schema_version": 1, "entries": [bad_entry]}).encode("utf-8")
    )
    with pytest.raises(InvalidIdentifierPayloadError):
        EncryptedFileIdentifierMappingStore(target_path, PASSWORD)


def test_payload_identifier_value_empty_raises(target_path: Path) -> None:
    bad_entry = _valid_entry_dict(identifier_value="")
    _write_encrypted_payload(
        target_path, json.dumps({"schema_version": 1, "entries": [bad_entry]}).encode("utf-8")
    )
    with pytest.raises(InvalidIdentifierPayloadError):
        EncryptedFileIdentifierMappingStore(target_path, PASSWORD)


def test_payload_identifier_value_whitespace_only_raises(target_path: Path) -> None:
    bad_entry = _valid_entry_dict(identifier_value="   ")
    _write_encrypted_payload(
        target_path, json.dumps({"schema_version": 1, "entries": [bad_entry]}).encode("utf-8")
    )
    with pytest.raises(InvalidIdentifierPayloadError):
        EncryptedFileIdentifierMappingStore(target_path, PASSWORD)


def test_payload_identifier_type_wrong_type_raises(target_path: Path) -> None:
    bad_entry = _valid_entry_dict(identifier_type=123)  # type: ignore[arg-type]
    _write_encrypted_payload(
        target_path, json.dumps({"schema_version": 1, "entries": [bad_entry]}).encode("utf-8")
    )
    with pytest.raises(InvalidIdentifierPayloadError):
        EncryptedFileIdentifierMappingStore(target_path, PASSWORD)


def test_payload_unknown_identifier_type_raises(target_path: Path) -> None:
    bad_entry = _valid_entry_dict(identifier_type="unknown_type")
    _write_encrypted_payload(
        target_path, json.dumps({"schema_version": 1, "entries": [bad_entry]}).encode("utf-8")
    )
    with pytest.raises(InvalidIdentifierPayloadError):
        EncryptedFileIdentifierMappingStore(target_path, PASSWORD)


def test_unknown_identifier_type_raw_value_not_exposed_in_message(target_path: Path) -> None:
    """
    Regression (Correction #1): raw-значение identifier_type НЕ должно
    попадать в текст InvalidIdentifierPayloadError, даже если оно похоже
    на конфиденциальное значение — сообщение может называть только
    известные безопасные категории (inn/kpp/ogrn).
    """
    bad_entry = _valid_entry_dict(identifier_type="SuperSecretRawTypeValue_12345")
    _write_encrypted_payload(
        target_path, json.dumps({"schema_version": 1, "entries": [bad_entry]}).encode("utf-8")
    )
    with pytest.raises(InvalidIdentifierPayloadError) as exc_info:
        EncryptedFileIdentifierMappingStore(target_path, PASSWORD)

    message = str(exc_info.value)
    assert "SuperSecretRawTypeValue_12345" not in message
    assert "identifier_type" in message
    for allowed_category in ("inn", "kpp", "ogrn"):
        assert allowed_category in message


def test_payload_ogrnip_identifier_type_not_accepted(target_path: Path) -> None:
    """ОГРНИП не добавлен как значение IdentifierType — 'ogrnip' должен быть отвергнут."""
    bad_entry = _valid_entry_dict(identifier_type="ogrnip")
    _write_encrypted_payload(
        target_path, json.dumps({"schema_version": 1, "entries": [bad_entry]}).encode("utf-8")
    )
    with pytest.raises(InvalidIdentifierPayloadError):
        EncryptedFileIdentifierMappingStore(target_path, PASSWORD)


def test_payload_duplicate_token_conflict_raises(target_path: Path) -> None:
    e1 = _valid_entry_dict(token="INN_AAAAAAAAA", identifier_value="7701234567")
    e2 = _valid_entry_dict(token="INN_AAAAAAAAA", identifier_value="7812345678")
    _write_encrypted_payload(
        target_path, json.dumps({"schema_version": 1, "entries": [e1, e2]}).encode("utf-8")
    )
    with pytest.raises(IdentifierMappingConflictError):
        EncryptedFileIdentifierMappingStore(target_path, PASSWORD)


def test_payload_duplicate_identity_conflict_raises(target_path: Path) -> None:
    e1 = _valid_entry_dict(token="INN_AAAAAAAAA", identifier_value="7701234567")
    e2 = _valid_entry_dict(token="INN_BBBBBBBBB", identifier_value="7701234567")
    _write_encrypted_payload(
        target_path, json.dumps({"schema_version": 1, "entries": [e1, e2]}).encode("utf-8")
    )
    with pytest.raises(IdentifierMappingConflictError):
        EncryptedFileIdentifierMappingStore(target_path, PASSWORD)


# ---------------------------------------------------------------------------
# G. Atomicity / failure injection
# ---------------------------------------------------------------------------


def test_failed_os_replace_preserves_disk_and_ram_state(
    target_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = EncryptedFileIdentifierMappingStore(target_path, PASSWORD)
    initial_entry = _entry("INN_AAAAAAAAA", "7701234567")
    store.add(initial_entry)
    original_bytes = target_path.read_bytes()

    def failing_replace(*args: object, **kwargs: object) -> None:
        raise OSError("simulated os.replace failure")

    monkeypatch.setattr(os, "replace", failing_replace)

    with pytest.raises(OSError):
        store.add(_entry("KPP_BBBBBBBBB", "770101001", IdentifierType.KPP))

    monkeypatch.undo()

    assert target_path.read_bytes() == original_bytes
    assert store.get_by_token("KPP_BBBBBBBBB") is None
    assert store.entries() == (initial_entry,)

    leftover = [p for p in target_path.parent.iterdir() if p != target_path]
    assert leftover == []

    reopened = EncryptedFileIdentifierMappingStore(target_path, PASSWORD)
    assert reopened.get_by_token("INN_AAAAAAAAA") == initial_entry
    assert reopened.get_by_token("KPP_BBBBBBBBB") is None


def test_failed_clear_preserves_disk_and_ram_state(
    target_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = EncryptedFileIdentifierMappingStore(target_path, PASSWORD)
    entry = _entry("INN_AAAAAAAAA", "7701234567")
    store.add(entry)
    original_bytes = target_path.read_bytes()

    def failing_replace(*args: object, **kwargs: object) -> None:
        raise OSError("simulated os.replace failure")

    monkeypatch.setattr(os, "replace", failing_replace)

    with pytest.raises(OSError):
        store.clear()

    monkeypatch.undo()

    assert target_path.read_bytes() == original_bytes
    assert store.entries() == (entry,)

    leftover = [p for p in target_path.parent.iterdir() if p != target_path]
    assert leftover == []


def test_failed_fsync_preserves_state_and_removes_temp(
    target_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = EncryptedFileIdentifierMappingStore(target_path, PASSWORD)
    entry = _entry("INN_AAAAAAAAA", "7701234567")
    store.add(entry)
    original_bytes = target_path.read_bytes()

    def failing_fsync(fd: int) -> None:
        raise OSError("simulated fsync failure")

    monkeypatch.setattr(os, "fsync", failing_fsync)

    with pytest.raises(OSError):
        store.add(_entry("KPP_BBBBBBBBB", "770101001", IdentifierType.KPP))

    monkeypatch.undo()

    assert target_path.read_bytes() == original_bytes
    assert store.entries() == (entry,)

    leftover = [p for p in target_path.parent.iterdir() if p != target_path]
    assert leftover == []


def test_failed_write_preserves_state_and_removes_temp(
    target_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = EncryptedFileIdentifierMappingStore(target_path, PASSWORD)
    entry = _entry("INN_AAAAAAAAA", "7701234567")
    store.add(entry)
    original_bytes = target_path.read_bytes()

    original_fdopen = os.fdopen

    def failing_fdopen(fd: int, mode: str = "r", *args: object, **kwargs: object):
        raw = original_fdopen(fd, mode, *args, **kwargs)

        class _FailingWriter:
            def __enter__(self) -> "_FailingWriter":
                return self

            def __exit__(self, *exc_info: object) -> None:
                raw.close()

            def write(self, data: bytes) -> int:
                raise OSError("simulated write failure")

            def fileno(self) -> int:
                return raw.fileno()

            def flush(self) -> None:
                raw.flush()

        return _FailingWriter()

    monkeypatch.setattr(os, "fdopen", failing_fdopen)

    with pytest.raises(OSError):
        store.add(_entry("KPP_BBBBBBBBB", "770101001", IdentifierType.KPP))

    monkeypatch.undo()

    assert target_path.read_bytes() == original_bytes
    assert store.entries() == (entry,)

    leftover = [p for p in target_path.parent.iterdir() if p != target_path]
    assert leftover == []


# ---------------------------------------------------------------------------
# H. No plaintext temp
# ---------------------------------------------------------------------------


def test_temp_file_holds_encrypted_bytes_not_plaintext(
    target_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: dict[str, bytes] = {}
    real_replace = os.replace

    def spy_replace(src: str, dst: str) -> None:
        captured["temp_bytes"] = Path(src).read_bytes()
        real_replace(src, dst)

    monkeypatch.setattr(os, "replace", spy_replace)

    store = EncryptedFileIdentifierMappingStore(target_path, PASSWORD)
    identifier_value = "SuperSecretIdentifierValue123"
    store.add(_entry("INN_AAAAAAAAA", identifier_value))

    temp_bytes = captured["temp_bytes"]
    assert temp_bytes[:4] == crypto.MAGIC
    assert identifier_value.encode("utf-8") not in temp_bytes
    assert b"schema_version" not in temp_bytes
    assert b"entries" not in temp_bytes

    decrypted = crypto.decrypt_bytes(temp_bytes, PASSWORD)
    assert identifier_value in decrypted.decode("utf-8")


# ---------------------------------------------------------------------------
# I. Password confidentiality
# ---------------------------------------------------------------------------


def test_repr_does_not_leak_password(target_path: Path) -> None:
    store = EncryptedFileIdentifierMappingStore(target_path, "super-secret-pw-value")
    assert "super-secret-pw-value" not in repr(store)


def test_no_public_password_getter(target_path: Path) -> None:
    store = EncryptedFileIdentifierMappingStore(target_path, PASSWORD)
    assert not hasattr(store, "password")


# ---------------------------------------------------------------------------
# J. Identifier confidentiality
# ---------------------------------------------------------------------------


def test_token_conflict_message_does_not_expose_identifier_value(target_path: Path) -> None:
    store = EncryptedFileIdentifierMappingStore(target_path, PASSWORD)
    store.add(_entry("INN_AAAAAAAAA", "7701234567"))

    with pytest.raises(IdentifierMappingConflictError) as exc_info:
        store.add(_entry("INN_AAAAAAAAA", "7812345678"))

    message = str(exc_info.value)
    assert "7701234567" not in message
    assert "7812345678" not in message


def test_identity_conflict_message_does_not_expose_identifier_value(target_path: Path) -> None:
    store = EncryptedFileIdentifierMappingStore(target_path, PASSWORD)
    store.add(_entry("INN_AAAAAAAAA", "7701234567"))

    with pytest.raises(IdentifierMappingConflictError) as exc_info:
        store.add(_entry("INN_BBBBBBBBB", "7701234567"))

    message = str(exc_info.value)
    assert "7701234567" not in message


@pytest.mark.parametrize(
    "bad_entry_factory",
    [
        lambda: _valid_entry_dict(identifier_value="ConfidentialRawValue999"),
    ],
)
def test_payload_validation_error_does_not_expose_confidential_value_from_other_field(
    target_path: Path, bad_entry_factory
) -> None:
    """
    Даже когда payload содержит валидную (по форме) confidential-подобную
    строку в identifier_value, ошибка о СОВСЕМ ДРУГОМ поле той же записи
    (например, о некорректном identifier_type) не должна случайно
    "прихватить" содержимое identifier_value в свой текст.
    """
    bad_entry = bad_entry_factory()
    bad_entry["identifier_type"] = "not-a-real-type"
    _write_encrypted_payload(
        target_path, json.dumps({"schema_version": 1, "entries": [bad_entry]}).encode("utf-8")
    )
    with pytest.raises(InvalidIdentifierPayloadError) as exc_info:
        EncryptedFileIdentifierMappingStore(target_path, PASSWORD)

    assert "ConfidentialRawValue999" not in str(exc_info.value)


def test_missing_identifier_value_error_does_not_expose_token(target_path: Path) -> None:
    bad_entry = {"token": "INN_SHOULD_NOT_LEAK", "identifier_type": "inn"}
    _write_encrypted_payload(
        target_path, json.dumps({"schema_version": 1, "entries": [bad_entry]}).encode("utf-8")
    )
    with pytest.raises(InvalidIdentifierPayloadError) as exc_info:
        EncryptedFileIdentifierMappingStore(target_path, PASSWORD)

    # token сам по себе псевдоним (не строго обязателен к сокрытию), но
    # сообщение по контракту должно называть только поле/индекс, не
    # конкретные значения записи.
    assert "INN_SHOULD_NOT_LEAK" not in str(exc_info.value)


def test_model_structural_error_is_wrapped_without_leaking_confidential_value(
    target_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    Regression (Correction #2): если IdentifierMappingEntry(...) неожиданно
    поднимет ValueError/TypeError (например, после будущего изменения model
    validation), наружу должен выйти InvalidIdentifierPayloadError, а не
    сырое исключение модельного слоя — и текст исходного исключения не
    должен попасть в сообщение внешнего исключения.

    store.add(entry) (конфликт-путь) НЕ монки-патчится и не проверяется
    здесь — это отдельный контракт (см.
    test_payload_duplicate_token_conflict_raises и соседние тесты).
    """
    import app.mapping.encrypted_identifier_file as encrypted_identifier_file_module

    def failing_constructor(*args: object, **kwargs: object):
        raise ValueError("SuperSecretRawIdentifierValue123")

    monkeypatch.setattr(
        encrypted_identifier_file_module, "IdentifierMappingEntry", failing_constructor
    )

    entry_dict = _valid_entry_dict()
    _write_encrypted_payload(
        target_path, json.dumps({"schema_version": 1, "entries": [entry_dict]}).encode("utf-8")
    )

    with pytest.raises(InvalidIdentifierPayloadError) as exc_info:
        EncryptedFileIdentifierMappingStore(target_path, PASSWORD)

    message = str(exc_info.value)
    assert "SuperSecretRawIdentifierValue123" not in message
    assert isinstance(exc_info.value.__cause__, ValueError)


# ---------------------------------------------------------------------------
# K. Exact-value preservation
# ---------------------------------------------------------------------------


def test_outer_whitespace_roundtrip_preserves_exact_value_and_distinct_identity(
    target_path: Path,
) -> None:
    bare = _entry("INN_AAAAAAAAA", "7701234567")
    padded = _entry("INN_BBBBBBBBB", " 7701234567 ")
    store = EncryptedFileIdentifierMappingStore(target_path, PASSWORD)
    store.add(bare)
    store.add(padded)  # не конфликт: разные строки => разная identity

    reopened = EncryptedFileIdentifierMappingStore(target_path, PASSWORD)
    assert reopened.get_by_identity(IdentifierType.INN, "7701234567") == bare
    assert reopened.get_by_identity(IdentifierType.INN, " 7701234567 ") == padded
    assert reopened.get_by_identity(IdentifierType.INN, "7701234567 ") is None


def test_leading_zero_roundtrip_preserves_exact_value(target_path: Path) -> None:
    with_zero = _entry("INN_AAAAAAAAA", "0770123456")
    without_zero = _entry("INN_BBBBBBBBB", "770123456")
    store = EncryptedFileIdentifierMappingStore(target_path, PASSWORD)
    store.add(with_zero)
    store.add(without_zero)

    reopened = EncryptedFileIdentifierMappingStore(target_path, PASSWORD)
    assert reopened.get_by_identity(IdentifierType.INN, "0770123456") == with_zero
    assert reopened.get_by_identity(IdentifierType.INN, "770123456") == without_zero


def test_no_normalization_applied_anywhere_in_roundtrip(target_path: Path) -> None:
    values = ["7701234567", " 7701234567 ", "0770123456", "  0770123456"]
    store = EncryptedFileIdentifierMappingStore(target_path, PASSWORD)
    for i, value in enumerate(values):
        store.add(_entry(f"INN_TOKEN{i:06d}", value))

    reopened = EncryptedFileIdentifierMappingStore(target_path, PASSWORD)
    reopened_values = {entry.identifier_value for entry in reopened.entries()}
    assert reopened_values == set(values)
    assert len(reopened.entries()) == len(values)  # все 4 — разные identity
