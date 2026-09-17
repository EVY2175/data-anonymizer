"""
Тесты app.mapping.encrypted_file (EncryptedFileMappingStore).

Группы:
    A. Basic persistence
    B. Security (raw bytes на диске не содержат plaintext)
    C. Mapping semantics (делегирование в InMemoryMappingStore)
    D. Payload validation (malformed payload, собранный только в памяти)
    E. Atomicity / failure injection
    F. No plaintext temp
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from app.mapping.base import AmbiguousMappingError, MappingConflictError, MappingStore
from app.mapping.encrypted_file import (
    EncryptedFileMappingStore,
    InvalidMappingPayloadError,
    MAPPING_PAYLOAD_SCHEMA_VERSION,
    UnsupportedMappingSchemaVersionError,
)
from app.models.entities import EntityType, MappingEntry
from app.security import crypto

PASSWORD = "test-password-123"


@pytest.fixture()
def target_path(tmp_path: Path) -> Path:
    return tmp_path / "mapping.enc"


def _entry(
    alias: str,
    real_value: str,
    entity_type: EntityType = EntityType.COMPANY,
    parent_alias: str | None = None,
) -> MappingEntry:
    return MappingEntry(
        alias=alias, real_value=real_value, entity_type=entity_type, parent_alias=parent_alias
    )


def _write_encrypted_payload(path: Path, plaintext: bytes, password: str = PASSWORD) -> None:
    """
    Собирает malformed/valid payload ИСКЛЮЧИТЕЛЬНО в памяти, шифрует его и
    только тогда пишет encrypted bytes на диск. Сам plaintext (malformed
    JSON/данные) на диск никогда не попадает.
    """
    encrypted = crypto.encrypt_bytes(plaintext, password)
    path.write_bytes(encrypted)


# ---------------------------------------------------------------------------
# Общий контракт
# ---------------------------------------------------------------------------


def test_encrypted_file_store_satisfies_mapping_store_contract(target_path: Path) -> None:
    store = EncryptedFileMappingStore(target_path, PASSWORD)
    assert isinstance(store, MappingStore)


def test_constructor_rejects_non_str_password(target_path: Path) -> None:
    with pytest.raises(TypeError):
        EncryptedFileMappingStore(target_path, 12345)  # type: ignore[arg-type]


def test_constructor_rejects_non_path_like(tmp_path: Path) -> None:
    with pytest.raises(TypeError):
        EncryptedFileMappingStore(12345, PASSWORD)  # type: ignore[arg-type]


def test_missing_parent_directory_raises_file_not_found(tmp_path: Path) -> None:
    missing = tmp_path / "does_not_exist" / "mapping.enc"
    with pytest.raises(FileNotFoundError):
        EncryptedFileMappingStore(missing, PASSWORD)


# ---------------------------------------------------------------------------
# A. Basic persistence
# ---------------------------------------------------------------------------


def test_nonexistent_file_gives_empty_store(target_path: Path) -> None:
    store = EncryptedFileMappingStore(target_path, PASSWORD)
    assert store.entries() == ()
    assert store.all_aliases() == set()
    assert store.get_by_alias("C_ANYTHING") is None


def test_constructor_does_not_create_missing_file(target_path: Path) -> None:
    EncryptedFileMappingStore(target_path, PASSWORD)
    assert not target_path.exists()


def test_add_creates_encrypted_file(target_path: Path) -> None:
    store = EncryptedFileMappingStore(target_path, PASSWORD)
    store.add(_entry("C_A", "Компания А"))
    assert target_path.exists()


def test_reopen_with_same_password_restores_entry(target_path: Path) -> None:
    entry = _entry("C_A", "Компания А")
    store = EncryptedFileMappingStore(target_path, PASSWORD)
    store.add(entry)

    reopened = EncryptedFileMappingStore(target_path, PASSWORD)
    assert reopened.get_by_alias("C_A") == entry


def test_multiple_entries_roundtrip(target_path: Path) -> None:
    entries = [
        _entry("C_A", "A"),
        _entry("C_B", "B"),
        _entry("P_C", "Иванов", entity_type=EntityType.PERSON),
    ]
    store = EncryptedFileMappingStore(target_path, PASSWORD)
    for entry in entries:
        store.add(entry)

    reopened = EncryptedFileMappingStore(target_path, PASSWORD)
    assert set(reopened.entries()) == set(entries)


def test_unicode_roundtrip(target_path: Path) -> None:
    entry = _entry("C_UNI", 'ООО "Ромашка 🌼" — филиал №1')
    store = EncryptedFileMappingStore(target_path, PASSWORD)
    store.add(entry)

    reopened = EncryptedFileMappingStore(target_path, PASSWORD)
    assert reopened.get_by_alias("C_UNI") == entry


def test_unknown_entity_type_roundtrip(target_path: Path) -> None:
    entry = _entry("X_UNK", "???", entity_type=EntityType.UNKNOWN)
    store = EncryptedFileMappingStore(target_path, PASSWORD)
    store.add(entry)

    reopened = EncryptedFileMappingStore(target_path, PASSWORD)
    assert reopened.get_by_alias("X_UNK") == entry


def test_parent_alias_roundtrip(target_path: Path) -> None:
    entry = _entry(
        "D_A", "Отдел продаж", entity_type=EntityType.DEPARTMENT, parent_alias="B_PARENT001"
    )
    store = EncryptedFileMappingStore(target_path, PASSWORD)
    store.add(entry)

    reopened = EncryptedFileMappingStore(target_path, PASSWORD)
    assert reopened.get_by_alias("D_A") == entry


def test_clear_persists_empty_store(target_path: Path) -> None:
    store = EncryptedFileMappingStore(target_path, PASSWORD)
    store.add(_entry("C_A", "A"))
    store.clear()

    reopened = EncryptedFileMappingStore(target_path, PASSWORD)
    assert reopened.entries() == ()
    assert reopened.all_aliases() == set()


def test_clear_on_already_empty_store_is_noop(target_path: Path) -> None:
    store = EncryptedFileMappingStore(target_path, PASSWORD)
    store.clear()  # уже пусто, файл ещё не создан
    assert not target_path.exists()


def test_exact_duplicate_add_is_idempotent_and_does_not_rewrite_file(target_path: Path) -> None:
    store = EncryptedFileMappingStore(target_path, PASSWORD)
    entry = _entry("C_A", "A")
    store.add(entry)
    content_before = target_path.read_bytes()

    store.add(entry)  # точно та же запись

    assert target_path.read_bytes() == content_before
    assert store.entries() == (entry,)


def test_add_rejects_non_mapping_entry(target_path: Path) -> None:
    store = EncryptedFileMappingStore(target_path, PASSWORD)
    with pytest.raises(TypeError):
        store.add("not-a-mapping-entry")  # type: ignore[arg-type]
    assert not target_path.exists()


# ---------------------------------------------------------------------------
# B. Security: raw bytes на диске не содержат plaintext
# ---------------------------------------------------------------------------


def test_encrypted_file_does_not_contain_plaintext_secrets(target_path: Path) -> None:
    real_value = "СекретноеЗначение12345"
    alias = "C_SECRETALIAS999"
    store = EncryptedFileMappingStore(target_path, PASSWORD)
    store.add(_entry(alias, real_value))

    raw = target_path.read_bytes()
    assert real_value.encode("utf-8") not in raw
    assert alias.encode("utf-8") not in raw
    assert PASSWORD.encode("utf-8") not in raw
    assert b"schema_version" not in raw
    assert b"entries" not in raw
    assert b"real_value" not in raw


def test_encrypted_file_matches_stage3_container_format(target_path: Path) -> None:
    store = EncryptedFileMappingStore(target_path, PASSWORD)
    store.add(_entry("C_A", "A"))

    raw = target_path.read_bytes()
    assert raw[:4] == crypto.MAGIC
    assert len(raw) >= crypto._HEADER_V1_SIZE + crypto.GCM_TAG_SIZE

    plaintext = crypto.decrypt_bytes(raw, PASSWORD)
    payload = json.loads(plaintext.decode("utf-8"))
    assert payload["schema_version"] == MAPPING_PAYLOAD_SCHEMA_VERSION
    assert payload["entries"][0]["alias"] == "C_A"


# ---------------------------------------------------------------------------
# C. Mapping semantics (делегирование в InMemoryMappingStore)
# ---------------------------------------------------------------------------


def test_alias_conflict_raises_and_preserves_state(target_path: Path) -> None:
    store = EncryptedFileMappingStore(target_path, PASSWORD)
    store.add(_entry("C_A", "A"))
    content_before = target_path.read_bytes()

    with pytest.raises(MappingConflictError):
        store.add(_entry("C_A", "Different"))

    assert store.get_by_alias("C_A").real_value == "A"
    assert target_path.read_bytes() == content_before


def test_identity_conflict_raises(target_path: Path) -> None:
    store = EncryptedFileMappingStore(target_path, PASSWORD)
    store.add(_entry("C_A", "Same Value"))
    with pytest.raises(MappingConflictError):
        store.add(_entry("C_B", "Same Value"))
    assert store.contains_alias("C_B") is False


def test_same_real_value_allowed_for_different_parent_alias(target_path: Path) -> None:
    store = EncryptedFileMappingStore(target_path, PASSWORD)
    dept_1 = _entry(
        "D_AAA111111", "Продажи", entity_type=EntityType.DEPARTMENT, parent_alias="B_111AAAAAA"
    )
    dept_2 = _entry(
        "D_BBB222222", "Продажи", entity_type=EntityType.DEPARTMENT, parent_alias="B_222BBBBBB"
    )
    store.add(dept_1)
    store.add(dept_2)

    assert (
        store.get_by_real_value("Продажи", EntityType.DEPARTMENT, parent_alias="B_111AAAAAA")
        == dept_1
    )
    assert (
        store.get_by_real_value("Продажи", EntityType.DEPARTMENT, parent_alias="B_222BBBBBB")
        == dept_2
    )


def test_explicit_none_vs_unspecified_parent_alias(target_path: Path) -> None:
    store = EncryptedFileMappingStore(target_path, PASSWORD)
    top_level = _entry(
        "D_TOPLEVEL1", "Продажи", entity_type=EntityType.DEPARTMENT, parent_alias=None
    )
    child = _entry(
        "D_CHILD0001", "Продажи", entity_type=EntityType.DEPARTMENT, parent_alias="B_PARENT001"
    )
    store.add(top_level)
    store.add(child)

    with pytest.raises(AmbiguousMappingError):
        store.get_by_real_value("Продажи", EntityType.DEPARTMENT)

    assert (
        store.get_by_real_value("Продажи", EntityType.DEPARTMENT, parent_alias=None)
        == top_level
    )


def test_ambiguous_partial_get_by_real_value(target_path: Path) -> None:
    store = EncryptedFileMappingStore(target_path, PASSWORD)
    store.add(_entry("C_A", "Иванов", entity_type=EntityType.COMPANY))
    store.add(_entry("P_B", "Иванов", entity_type=EntityType.PERSON))
    with pytest.raises(AmbiguousMappingError):
        store.get_by_real_value("Иванов")


def test_parent_alias_existence_not_validated(target_path: Path) -> None:
    store = EncryptedFileMappingStore(target_path, PASSWORD)
    entry = _entry(
        "D_UNKNOWNPR", "Продажи", entity_type=EntityType.DEPARTMENT, parent_alias="B_UNKNOWN00"
    )
    store.add(entry)  # не должно бросить, даже если B_UNKNOWN00 не добавлен
    assert store.get_by_alias("D_UNKNOWNPR") == entry


def test_all_aliases_snapshot_is_independent(target_path: Path) -> None:
    store = EncryptedFileMappingStore(target_path, PASSWORD)
    store.add(_entry("C_A", "A"))
    snapshot = store.all_aliases()
    snapshot.add("C_INJECTED")
    snapshot.discard("C_A")

    assert store.all_aliases() == {"C_A"}


def test_entries_snapshot_is_independent(target_path: Path) -> None:
    entry_a = _entry("C_A", "A")
    store = EncryptedFileMappingStore(target_path, PASSWORD)
    store.add(entry_a)
    snapshot = store.entries()

    store.add(_entry("C_B", "B"))

    assert snapshot == (entry_a,)
    assert len(store.entries()) == 2


def test_get_by_alias_rejects_invalid_alias(target_path: Path) -> None:
    store = EncryptedFileMappingStore(target_path, PASSWORD)
    with pytest.raises(ValueError):
        store.get_by_alias("")


# ---------------------------------------------------------------------------
# Crypto-layer errors: неверный пароль / tampering / неподдерживаемая версия
# контейнера должны сохранять семантику Stage 3, а не подменяться mapping-
# layer исключениями.
# ---------------------------------------------------------------------------


def test_wrong_password_raises_decryption_error(target_path: Path) -> None:
    store = EncryptedFileMappingStore(target_path, PASSWORD)
    store.add(_entry("C_A", "A"))

    with pytest.raises(crypto.DecryptionError):
        EncryptedFileMappingStore(target_path, "wrong-password")


def test_tampered_ciphertext_raises_decryption_error(target_path: Path) -> None:
    store = EncryptedFileMappingStore(target_path, PASSWORD)
    store.add(_entry("C_A", "A"))

    raw = bytearray(target_path.read_bytes())
    raw[-1] ^= 0xFF  # повреждаем последний байт GCM-тега
    target_path.write_bytes(bytes(raw))

    with pytest.raises(crypto.DecryptionError):
        EncryptedFileMappingStore(target_path, PASSWORD)


def test_unsupported_container_version_propagates_stage3_exception(target_path: Path) -> None:
    store = EncryptedFileMappingStore(target_path, PASSWORD)
    store.add(_entry("C_A", "A"))

    raw = bytearray(target_path.read_bytes())
    raw[4] = 99  # смещение поля version в header'е
    target_path.write_bytes(bytes(raw))

    with pytest.raises(crypto.UnsupportedContainerVersionError):
        EncryptedFileMappingStore(target_path, PASSWORD)


# ---------------------------------------------------------------------------
# D. Payload validation — malformed payload собирается ТОЛЬКО в памяти
# ---------------------------------------------------------------------------


def test_invalid_utf8_payload_raises(target_path: Path) -> None:
    _write_encrypted_payload(target_path, b"\xff\xfe\xfa not valid utf-8")
    with pytest.raises(InvalidMappingPayloadError):
        EncryptedFileMappingStore(target_path, PASSWORD)


def test_invalid_json_payload_raises(target_path: Path) -> None:
    _write_encrypted_payload(target_path, b"{not valid json")
    with pytest.raises(InvalidMappingPayloadError):
        EncryptedFileMappingStore(target_path, PASSWORD)


def test_payload_root_not_object_raises(target_path: Path) -> None:
    _write_encrypted_payload(target_path, json.dumps([1, 2, 3]).encode("utf-8"))
    with pytest.raises(InvalidMappingPayloadError):
        EncryptedFileMappingStore(target_path, PASSWORD)


def test_payload_missing_schema_version_raises(target_path: Path) -> None:
    _write_encrypted_payload(target_path, json.dumps({"entries": []}).encode("utf-8"))
    with pytest.raises(InvalidMappingPayloadError):
        EncryptedFileMappingStore(target_path, PASSWORD)


def test_payload_unsupported_schema_version_raises(target_path: Path) -> None:
    _write_encrypted_payload(
        target_path, json.dumps({"schema_version": 999, "entries": []}).encode("utf-8")
    )
    with pytest.raises(UnsupportedMappingSchemaVersionError):
        EncryptedFileMappingStore(target_path, PASSWORD)


def test_payload_missing_entries_raises(target_path: Path) -> None:
    _write_encrypted_payload(target_path, json.dumps({"schema_version": 1}).encode("utf-8"))
    with pytest.raises(InvalidMappingPayloadError):
        EncryptedFileMappingStore(target_path, PASSWORD)


def test_payload_entries_not_list_raises(target_path: Path) -> None:
    _write_encrypted_payload(
        target_path, json.dumps({"schema_version": 1, "entries": {}}).encode("utf-8")
    )
    with pytest.raises(InvalidMappingPayloadError):
        EncryptedFileMappingStore(target_path, PASSWORD)


def test_payload_entry_not_object_raises(target_path: Path) -> None:
    _write_encrypted_payload(
        target_path,
        json.dumps({"schema_version": 1, "entries": ["not-a-dict"]}).encode("utf-8"),
    )
    with pytest.raises(InvalidMappingPayloadError):
        EncryptedFileMappingStore(target_path, PASSWORD)


def test_payload_malformed_entry_raises(target_path: Path) -> None:
    bad_entry = {"alias": "C_A"}  # нет real_value/entity_type
    _write_encrypted_payload(
        target_path, json.dumps({"schema_version": 1, "entries": [bad_entry]}).encode("utf-8")
    )
    with pytest.raises(InvalidMappingPayloadError):
        EncryptedFileMappingStore(target_path, PASSWORD)


def test_payload_invalid_entity_type_raises(target_path: Path) -> None:
    bad_entry = {"alias": "C_A", "real_value": "A", "entity_type": "not-a-real-type"}
    _write_encrypted_payload(
        target_path, json.dumps({"schema_version": 1, "entries": [bad_entry]}).encode("utf-8")
    )
    with pytest.raises(InvalidMappingPayloadError):
        EncryptedFileMappingStore(target_path, PASSWORD)


def test_payload_duplicate_alias_conflict_raises(target_path: Path) -> None:
    e1 = {"alias": "C_A", "real_value": "A", "entity_type": "company", "parent_alias": None}
    e2 = {"alias": "C_A", "real_value": "B", "entity_type": "company", "parent_alias": None}
    _write_encrypted_payload(
        target_path, json.dumps({"schema_version": 1, "entries": [e1, e2]}).encode("utf-8")
    )
    with pytest.raises(MappingConflictError):
        EncryptedFileMappingStore(target_path, PASSWORD)


def test_payload_duplicate_identity_conflict_raises(target_path: Path) -> None:
    e1 = {"alias": "C_A", "real_value": "Same", "entity_type": "company", "parent_alias": None}
    e2 = {"alias": "C_B", "real_value": "Same", "entity_type": "company", "parent_alias": None}
    _write_encrypted_payload(
        target_path, json.dumps({"schema_version": 1, "entries": [e1, e2]}).encode("utf-8")
    )
    with pytest.raises(MappingConflictError):
        EncryptedFileMappingStore(target_path, PASSWORD)


# ---------------------------------------------------------------------------
# E. Atomicity / failure injection
# ---------------------------------------------------------------------------


def test_failed_os_replace_preserves_disk_and_ram_state(
    target_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = EncryptedFileMappingStore(target_path, PASSWORD)
    initial_entry = _entry("C_A", "A")
    store.add(initial_entry)
    original_bytes = target_path.read_bytes()

    def failing_replace(*args: object, **kwargs: object) -> None:
        raise OSError("simulated os.replace failure")

    monkeypatch.setattr(os, "replace", failing_replace)

    with pytest.raises(OSError):
        store.add(_entry("C_B", "B"))

    monkeypatch.undo()

    assert target_path.read_bytes() == original_bytes
    assert store.get_by_alias("C_B") is None
    assert store.entries() == (initial_entry,)

    leftover = [p for p in target_path.parent.iterdir() if p != target_path]
    assert leftover == []

    reopened = EncryptedFileMappingStore(target_path, PASSWORD)
    assert reopened.get_by_alias("C_A") == initial_entry
    assert reopened.get_by_alias("C_B") is None


def test_failed_clear_preserves_disk_and_ram_state(
    target_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = EncryptedFileMappingStore(target_path, PASSWORD)
    entry = _entry("C_A", "A")
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
    store = EncryptedFileMappingStore(target_path, PASSWORD)
    entry = _entry("C_A", "A")
    store.add(entry)
    original_bytes = target_path.read_bytes()

    def failing_fsync(fd: int) -> None:
        raise OSError("simulated fsync failure")

    monkeypatch.setattr(os, "fsync", failing_fsync)

    with pytest.raises(OSError):
        store.add(_entry("C_B", "B"))

    monkeypatch.undo()

    assert target_path.read_bytes() == original_bytes
    assert store.entries() == (entry,)

    leftover = [p for p in target_path.parent.iterdir() if p != target_path]
    assert leftover == []


def test_failed_write_preserves_state_and_removes_temp(
    target_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = EncryptedFileMappingStore(target_path, PASSWORD)
    entry = _entry("C_A", "A")
    store.add(entry)
    original_bytes = target_path.read_bytes()

    import io

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
        store.add(_entry("C_B", "B"))

    monkeypatch.undo()

    assert target_path.read_bytes() == original_bytes
    assert store.entries() == (entry,)

    leftover = [p for p in target_path.parent.iterdir() if p != target_path]
    assert leftover == []


# ---------------------------------------------------------------------------
# F. No plaintext temp
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

    store = EncryptedFileMappingStore(target_path, PASSWORD)
    real_value = "SuperSecretPlaintextValue"
    store.add(_entry("C_A", real_value))

    temp_bytes = captured["temp_bytes"]
    assert temp_bytes[:4] == crypto.MAGIC
    assert real_value.encode("utf-8") not in temp_bytes
    assert b"schema_version" not in temp_bytes
    assert b"entries" not in temp_bytes

    decrypted = crypto.decrypt_bytes(temp_bytes, PASSWORD)
    assert real_value in decrypted.decode("utf-8")


# ---------------------------------------------------------------------------
# Password security
# ---------------------------------------------------------------------------


def test_repr_does_not_leak_password(target_path: Path) -> None:
    store = EncryptedFileMappingStore(target_path, "super-secret-pw-value")
    assert "super-secret-pw-value" not in repr(store)


def test_no_public_password_getter(target_path: Path) -> None:
    store = EncryptedFileMappingStore(target_path, PASSWORD)
    assert not hasattr(store, "password")
