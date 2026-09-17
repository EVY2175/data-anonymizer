"""
Тесты add_many() — Stage 7B.4.3 (transactional batch insertion).

Покрывает ОБЕ реализации IdentifierMappingStore:
    - InMemoryIdentifierMappingStore (Stage 7B.3);
    - EncryptedFileIdentifierMappingStore (Stage 7B.4.1).

Не покрывается здесь: anonymizer integration (Stage 7B.5), token
generation collision-avoidance (интеграционный вопрос будущего Stage
7B.5, не часть add_many()), provenance, старое N(N+1)/2 rewrite-per-add
поведение (уже измерено в Stage 7B.4.2, не regression-тест).

Группы:
    A. API (add_many присутствует, abstract, обе реализации)
    B. In-memory success
    C. Duplicates
    D. In-memory rollback (conflicts)
    E. Iterable behavior (generator, one-shot, raises during materialization)
    F. Encrypted success
    G. Encrypted no-write (all-duplicate batch)
    H. Single-persist contract (permanent architectural regression test)
    I. Mixed duplicate/new (encrypted, single persist)
    J. Encrypted conflict rollback
    K. Persistence failure (encrypted)
    L. Confidentiality
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path

import pytest

import app.mapping.encrypted_identifier_file as eif
from app.mapping.encrypted_identifier_file import EncryptedFileIdentifierMappingStore
from app.mapping.identifier_base import IdentifierMappingConflictError, IdentifierMappingStore
from app.mapping.identifier_memory import InMemoryIdentifierMappingStore
from app.models.identifiers import IdentifierMappingEntry, IdentifierType

PASSWORD = "test-password-123"


@pytest.fixture()
def memory_store() -> InMemoryIdentifierMappingStore:
    return InMemoryIdentifierMappingStore()


@pytest.fixture()
def target_path(tmp_path: Path) -> Path:
    return tmp_path / "identifiers.enc"


@pytest.fixture()
def encrypted_store(target_path: Path) -> EncryptedFileIdentifierMappingStore:
    return EncryptedFileIdentifierMappingStore(target_path, PASSWORD)


def _entry(
    token: str,
    identifier_value: str,
    identifier_type: IdentifierType = IdentifierType.INN,
) -> IdentifierMappingEntry:
    return IdentifierMappingEntry(
        token=token, identifier_value=identifier_value, identifier_type=identifier_type
    )


# ---------------------------------------------------------------------------
# A. API
# ---------------------------------------------------------------------------


def test_add_many_is_abstract_on_base_class() -> None:
    assert "add_many" in IdentifierMappingStore.__abstractmethods__


def test_in_memory_store_has_add_many(memory_store: InMemoryIdentifierMappingStore) -> None:
    assert hasattr(memory_store, "add_many")
    assert callable(memory_store.add_many)


def test_encrypted_store_has_add_many(
    encrypted_store: EncryptedFileIdentifierMappingStore,
) -> None:
    assert hasattr(encrypted_store, "add_many")
    assert callable(encrypted_store.add_many)


def test_add_many_accepts_list(memory_store: InMemoryIdentifierMappingStore) -> None:
    memory_store.add_many([_entry("INN_AAAAAAAAA", "7701234567")])
    assert memory_store.get_by_token("INN_AAAAAAAAA") is not None


def test_add_many_accepts_tuple(memory_store: InMemoryIdentifierMappingStore) -> None:
    memory_store.add_many((_entry("INN_AAAAAAAAA", "7701234567"),))
    assert memory_store.get_by_token("INN_AAAAAAAAA") is not None


def test_add_many_accepts_generator(memory_store: InMemoryIdentifierMappingStore) -> None:
    def gen() -> Iterator[IdentifierMappingEntry]:
        yield _entry("INN_AAAAAAAAA", "7701234567")

    memory_store.add_many(gen())
    assert memory_store.get_by_token("INN_AAAAAAAAA") is not None


# ---------------------------------------------------------------------------
# B. In-memory success
# ---------------------------------------------------------------------------


def test_add_many_empty_list_is_noop(memory_store: InMemoryIdentifierMappingStore) -> None:
    memory_store.add_many([])
    assert memory_store.entries() == ()
    assert memory_store.all_tokens() == set()


def test_add_many_one_new_entry(memory_store: InMemoryIdentifierMappingStore) -> None:
    entry = _entry("INN_AAAAAAAAA", "7701234567")
    memory_store.add_many([entry])
    assert memory_store.get_by_token("INN_AAAAAAAAA") == entry


def test_add_many_multiple_new_entries(memory_store: InMemoryIdentifierMappingStore) -> None:
    entries = [
        _entry("INN_AAAAAAAAA", "7701234567", IdentifierType.INN),
        _entry("KPP_BBBBBBBBB", "770101001", IdentifierType.KPP),
        _entry("OGRN_CCCCCCCC", "1047709000008", IdentifierType.OGRN),
    ]
    memory_store.add_many(entries)
    assert set(memory_store.entries()) == set(entries)


def test_add_many_entries_reachable_via_get_by_token(
    memory_store: InMemoryIdentifierMappingStore,
) -> None:
    entry = _entry("INN_AAAAAAAAA", "7701234567")
    memory_store.add_many([entry])
    assert memory_store.get_by_token("INN_AAAAAAAAA") == entry


def test_add_many_entries_reachable_via_get_by_identity(
    memory_store: InMemoryIdentifierMappingStore,
) -> None:
    entry = _entry("INN_AAAAAAAAA", "7701234567")
    memory_store.add_many([entry])
    assert memory_store.get_by_identity(IdentifierType.INN, "7701234567") == entry


def test_add_many_all_tokens_correct(memory_store: InMemoryIdentifierMappingStore) -> None:
    memory_store.add_many(
        [_entry("INN_AAAAAAAAA", "7701234567"), _entry("KPP_BBBBBBBBB", "770101001", IdentifierType.KPP)]
    )
    assert memory_store.all_tokens() == {"INN_AAAAAAAAA", "KPP_BBBBBBBBB"}


def test_add_many_entries_snapshot_correct(memory_store: InMemoryIdentifierMappingStore) -> None:
    entry_a = _entry("INN_AAAAAAAAA", "7701234567")
    entry_b = _entry("KPP_BBBBBBBBB", "770101001", IdentifierType.KPP)
    memory_store.add_many([entry_a, entry_b])
    assert set(memory_store.entries()) == {entry_a, entry_b}


# ---------------------------------------------------------------------------
# C. Duplicates
# ---------------------------------------------------------------------------


def test_add_many_duplicate_within_batch_collapses_to_one_entry(
    memory_store: InMemoryIdentifierMappingStore,
) -> None:
    entry = _entry("INN_AAAAAAAAA", "7701234567")
    memory_store.add_many([entry, entry])
    assert memory_store.entries() == (entry,)
    assert memory_store.all_tokens() == {"INN_AAAAAAAAA"}


def test_add_many_existing_entry_batch_is_full_noop(
    memory_store: InMemoryIdentifierMappingStore,
) -> None:
    entry = _entry("INN_AAAAAAAAA", "7701234567")
    memory_store.add(entry)
    memory_store.add_many([entry])
    assert memory_store.entries() == (entry,)


def test_add_many_existing_plus_new_both_present(
    memory_store: InMemoryIdentifierMappingStore,
) -> None:
    existing = _entry("INN_AAAAAAAAA", "7701234567")
    memory_store.add(existing)
    new = _entry("KPP_BBBBBBBBB", "770101001", IdentifierType.KPP)
    memory_store.add_many([existing, new])
    assert set(memory_store.entries()) == {existing, new}


def test_add_many_repeated_duplicate_several_times_no_conflict(
    memory_store: InMemoryIdentifierMappingStore,
) -> None:
    entry = _entry("INN_AAAAAAAAA", "7701234567")
    memory_store.add_many([entry, entry, entry, entry])
    assert memory_store.entries() == (entry,)


# ---------------------------------------------------------------------------
# D. In-memory rollback (conflicts)
# ---------------------------------------------------------------------------


def test_add_many_conflict_at_first_position_rolls_back(
    memory_store: InMemoryIdentifierMappingStore,
) -> None:
    memory_store.add(_entry("INN_AAAAAAAAA", "7701234567"))
    before_tokens = memory_store.all_tokens()
    before_entries = memory_store.entries()

    batch = [
        _entry("INN_AAAAAAAAA", "9999999999"),  # conflict: same token, different identity
        _entry("KPP_BBBBBBBBB", "770101001", IdentifierType.KPP),
        _entry("OGRN_CCCCCCCC", "1047709000008", IdentifierType.OGRN),
    ]
    with pytest.raises(IdentifierMappingConflictError):
        memory_store.add_many(batch)

    assert memory_store.all_tokens() == before_tokens
    assert memory_store.entries() == before_entries
    assert memory_store.get_by_token("KPP_BBBBBBBBB") is None
    assert memory_store.get_by_token("OGRN_CCCCCCCC") is None


def test_add_many_conflict_in_middle_rolls_back(
    memory_store: InMemoryIdentifierMappingStore,
) -> None:
    before_entries = memory_store.entries()
    batch = [
        _entry("INN_AAAAAAAAA", "7701234567"),
        _entry("INN_AAAAAAAAA", "9999999999"),  # conflict against the batch entry above
        _entry("OGRN_CCCCCCCC", "1047709000008", IdentifierType.OGRN),
    ]
    with pytest.raises(IdentifierMappingConflictError):
        memory_store.add_many(batch)

    assert memory_store.entries() == before_entries
    assert memory_store.get_by_token("INN_AAAAAAAAA") is None
    assert memory_store.get_by_token("OGRN_CCCCCCCC") is None


def test_add_many_conflict_at_last_position_rolls_back(
    memory_store: InMemoryIdentifierMappingStore,
) -> None:
    before_entries = memory_store.entries()
    batch = [
        _entry("INN_AAAAAAAAA", "7701234567"),
        _entry("KPP_BBBBBBBBB", "770101001", IdentifierType.KPP),
        _entry("INN_AAAAAAAAA", "9999999999"),  # conflict at the very end
    ]
    with pytest.raises(IdentifierMappingConflictError):
        memory_store.add_many(batch)

    assert memory_store.entries() == before_entries
    assert memory_store.get_by_token("INN_AAAAAAAAA") is None
    assert memory_store.get_by_token("KPP_BBBBBBBBB") is None


def test_add_many_conflict_against_existing_token_rolls_back(
    memory_store: InMemoryIdentifierMappingStore,
) -> None:
    memory_store.add(_entry("INN_AAAAAAAAA", "7701234567"))
    before_entries = memory_store.entries()

    with pytest.raises(IdentifierMappingConflictError):
        memory_store.add_many([_entry("INN_AAAAAAAAA", "9999999999")])

    assert memory_store.entries() == before_entries


def test_add_many_conflict_against_existing_identity_rolls_back(
    memory_store: InMemoryIdentifierMappingStore,
) -> None:
    memory_store.add(_entry("INN_AAAAAAAAA", "7701234567"))
    before_entries = memory_store.entries()

    with pytest.raises(IdentifierMappingConflictError):
        memory_store.add_many([_entry("INN_BBBBBBBBB", "7701234567")])

    assert memory_store.entries() == before_entries
    assert memory_store.get_by_token("INN_BBBBBBBBB") is None


def test_add_many_conflict_between_batch_entries_by_token_rolls_back(
    memory_store: InMemoryIdentifierMappingStore,
) -> None:
    before_entries = memory_store.entries()
    batch = [
        _entry("INN_AAAAAAAAA", "7701234567"),
        _entry("INN_AAAAAAAAA", "8812345678"),  # same token, different identity
    ]
    with pytest.raises(IdentifierMappingConflictError):
        memory_store.add_many(batch)

    assert memory_store.entries() == before_entries


def test_add_many_conflict_between_batch_entries_by_identity_rolls_back(
    memory_store: InMemoryIdentifierMappingStore,
) -> None:
    before_entries = memory_store.entries()
    batch = [
        _entry("INN_AAAAAAAAA", "7701234567"),
        _entry("INN_BBBBBBBBB", "7701234567"),  # same identity, different token
    ]
    with pytest.raises(IdentifierMappingConflictError):
        memory_store.add_many(batch)

    assert memory_store.entries() == before_entries


# ---------------------------------------------------------------------------
# E. Iterable behavior
# ---------------------------------------------------------------------------


def test_add_many_one_shot_generator_consumed_once(
    memory_store: InMemoryIdentifierMappingStore,
) -> None:
    entries = [_entry("INN_AAAAAAAAA", "7701234567"), _entry("KPP_BBBBBBBBB", "770101001", IdentifierType.KPP)]
    gen = (e for e in entries)
    memory_store.add_many(gen)
    assert set(memory_store.entries()) == set(entries)


def test_add_many_generator_raising_before_completion_leaves_store_unchanged(
    memory_store: InMemoryIdentifierMappingStore,
) -> None:
    before_entries = memory_store.entries()

    def failing_gen() -> Iterator[IdentifierMappingEntry]:
        yield _entry("INN_AAAAAAAAA", "7701234567")
        yield _entry("KPP_BBBBBBBBB", "770101001", IdentifierType.KPP)
        raise RuntimeError("synthetic iterator failure")

    with pytest.raises(RuntimeError, match="synthetic iterator failure"):
        memory_store.add_many(failing_gen())

    assert memory_store.entries() == before_entries
    assert memory_store.get_by_token("INN_AAAAAAAAA") is None
    assert memory_store.get_by_token("KPP_BBBBBBBBB") is None


def test_add_many_none_raises_type_error(memory_store: InMemoryIdentifierMappingStore) -> None:
    with pytest.raises(TypeError):
        memory_store.add_many(None)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# F. Encrypted success
# ---------------------------------------------------------------------------


def test_add_many_empty_batch_on_absent_file_does_not_create_file(
    target_path: Path, encrypted_store: EncryptedFileIdentifierMappingStore
) -> None:
    encrypted_store.add_many([])
    assert not target_path.exists()


def test_add_many_multiple_new_entries_creates_encrypted_file(
    target_path: Path, encrypted_store: EncryptedFileIdentifierMappingStore
) -> None:
    encrypted_store.add_many(
        [
            _entry("INN_AAAAAAAAA", "7701234567", IdentifierType.INN),
            _entry("KPP_BBBBBBBBB", "770101001", IdentifierType.KPP),
        ]
    )
    assert target_path.exists()


def test_add_many_reopen_restores_whole_batch(
    target_path: Path, encrypted_store: EncryptedFileIdentifierMappingStore
) -> None:
    entries = [
        _entry("INN_AAAAAAAAA", "7701234567", IdentifierType.INN),
        _entry("KPP_BBBBBBBBB", "770101001", IdentifierType.KPP),
        _entry("OGRN_CCCCCCCC", "1047709000008", IdentifierType.OGRN),
    ]
    encrypted_store.add_many(entries)

    reopened = EncryptedFileIdentifierMappingStore(target_path, PASSWORD)
    assert set(reopened.entries()) == set(entries)


def test_add_many_exact_values_preserved(
    target_path: Path, encrypted_store: EncryptedFileIdentifierMappingStore
) -> None:
    padded = _entry("INN_AAAAAAAAA", " 7701234567 ")
    leading_zero = _entry("INN_BBBBBBBBB", "0770123456")
    encrypted_store.add_many([padded, leading_zero])

    reopened = EncryptedFileIdentifierMappingStore(target_path, PASSWORD)
    assert reopened.get_by_identity(IdentifierType.INN, " 7701234567 ") == padded
    assert reopened.get_by_identity(IdentifierType.INN, "0770123456") == leading_zero
    assert reopened.get_by_identity(IdentifierType.INN, "7701234567") is None
    assert reopened.get_by_identity(IdentifierType.INN, "770123456") is None


# ---------------------------------------------------------------------------
# G. Encrypted no-write (all-duplicate batch)
# ---------------------------------------------------------------------------


def test_add_many_all_duplicates_does_not_rewrite_file(
    target_path: Path, encrypted_store: EncryptedFileIdentifierMappingStore
) -> None:
    entry = _entry("INN_AAAAAAAAA", "7701234567")
    encrypted_store.add(entry)
    content_before = target_path.read_bytes()

    encrypted_store.add_many([entry, entry])

    assert target_path.read_bytes() == content_before
    assert encrypted_store.entries() == (entry,)


def test_add_many_all_duplicates_zero_persist_encrypt_write_calls(
    target_path: Path, encrypted_store: EncryptedFileIdentifierMappingStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    entry = _entry("INN_AAAAAAAAA", "7701234567")
    encrypted_store.add(entry)

    calls = {"persist": 0, "encrypt": 0, "atomic_write": 0}
    real_persist = eif.EncryptedFileIdentifierMappingStore._persist
    real_encrypt = eif.encrypt_bytes
    real_atomic_write = eif.EncryptedFileIdentifierMappingStore._atomic_write

    def counting_persist(self, candidate):
        calls["persist"] += 1
        return real_persist(self, candidate)

    def counting_encrypt(plaintext, password):
        calls["encrypt"] += 1
        return real_encrypt(plaintext, password)

    def counting_atomic_write(self, encrypted):
        calls["atomic_write"] += 1
        return real_atomic_write(self, encrypted)

    monkeypatch.setattr(eif.EncryptedFileIdentifierMappingStore, "_persist", counting_persist)
    monkeypatch.setattr(eif, "encrypt_bytes", counting_encrypt)
    monkeypatch.setattr(eif.EncryptedFileIdentifierMappingStore, "_atomic_write", counting_atomic_write)

    encrypted_store.add_many([entry, entry])

    assert calls == {"persist": 0, "encrypt": 0, "atomic_write": 0}


# ---------------------------------------------------------------------------
# H. Single-persist contract (permanent architectural regression test)
# ---------------------------------------------------------------------------


def test_add_many_batch_of_new_entries_persists_exactly_once(
    target_path: Path, encrypted_store: EncryptedFileIdentifierMappingStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    Архитектурный контракт Stage 7B.4.3: для ОДНОГО add_many(batch) с хотя бы
    одной новой записью — ровно один persist/serialize/encrypt/atomic-write,
    независимо от размера batch. Не timing-тест: считаются только вызовы.
    """
    calls = {"persist": 0, "encrypt": 0, "atomic_write": 0}
    real_persist = eif.EncryptedFileIdentifierMappingStore._persist
    real_encrypt = eif.encrypt_bytes
    real_atomic_write = eif.EncryptedFileIdentifierMappingStore._atomic_write

    def counting_persist(self, candidate):
        calls["persist"] += 1
        return real_persist(self, candidate)

    def counting_encrypt(plaintext, password):
        calls["encrypt"] += 1
        return real_encrypt(plaintext, password)

    def counting_atomic_write(self, encrypted):
        calls["atomic_write"] += 1
        return real_atomic_write(self, encrypted)

    monkeypatch.setattr(eif.EncryptedFileIdentifierMappingStore, "_persist", counting_persist)
    monkeypatch.setattr(eif, "encrypt_bytes", counting_encrypt)
    monkeypatch.setattr(eif.EncryptedFileIdentifierMappingStore, "_atomic_write", counting_atomic_write)

    batch = [_entry(f"INN_{i:06d}", f"VALUE_{i:06d}") for i in range(50)]
    encrypted_store.add_many(batch)

    assert calls == {"persist": 1, "encrypt": 1, "atomic_write": 1}
    assert len(encrypted_store.entries()) == 50


# ---------------------------------------------------------------------------
# I. Mixed duplicate/new (encrypted, single persist)
# ---------------------------------------------------------------------------


def test_add_many_mixed_duplicate_and_new_persists_exactly_once(
    target_path: Path, encrypted_store: EncryptedFileIdentifierMappingStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    entry_a = _entry("INN_AAAAAAAAA", "7701234567")
    encrypted_store.add(entry_a)

    calls = {"persist": 0}
    real_persist = eif.EncryptedFileIdentifierMappingStore._persist

    def counting_persist(self, candidate):
        calls["persist"] += 1
        return real_persist(self, candidate)

    monkeypatch.setattr(eif.EncryptedFileIdentifierMappingStore, "_persist", counting_persist)

    entry_b = _entry("KPP_BBBBBBBBB", "770101001", IdentifierType.KPP)
    entry_c = _entry("OGRN_CCCCCCCC", "1047709000008", IdentifierType.OGRN)
    encrypted_store.add_many([entry_a, entry_b, entry_a, entry_c])

    assert calls["persist"] == 1
    assert set(encrypted_store.entries()) == {entry_a, entry_b, entry_c}

    reopened = EncryptedFileIdentifierMappingStore(target_path, PASSWORD)
    assert set(reopened.entries()) == {entry_a, entry_b, entry_c}


# ---------------------------------------------------------------------------
# J. Encrypted conflict rollback
# ---------------------------------------------------------------------------


def test_add_many_conflict_in_batch_preserves_disk_and_ram(
    target_path: Path, encrypted_store: EncryptedFileIdentifierMappingStore
) -> None:
    assert not target_path.exists()

    entry_a = _entry("INN_AAAAAAAAA", "7701234567")
    entry_b = _entry("KPP_BBBBBBBBB", "770101001", IdentifierType.KPP)
    conflicting_c = _entry("INN_AAAAAAAAA", "9999999999")  # conflicts with entry_a's token

    with pytest.raises(IdentifierMappingConflictError):
        encrypted_store.add_many([entry_a, entry_b, conflicting_c])

    assert not target_path.exists()
    assert encrypted_store.entries() == ()
    assert encrypted_store.get_by_token("INN_AAAAAAAAA") is None
    assert encrypted_store.get_by_token("KPP_BBBBBBBBB") is None


def test_add_many_conflict_against_pre_existing_state_preserves_disk_and_ram(
    target_path: Path, encrypted_store: EncryptedFileIdentifierMappingStore
) -> None:
    existing = _entry("INN_AAAAAAAAA", "7701234567")
    encrypted_store.add(existing)
    content_before = target_path.read_bytes()

    with pytest.raises(IdentifierMappingConflictError):
        encrypted_store.add_many(
            [_entry("KPP_BBBBBBBBB", "770101001", IdentifierType.KPP), _entry("INN_AAAAAAAAA", "9999999999")]
        )

    assert target_path.read_bytes() == content_before
    assert encrypted_store.entries() == (existing,)
    assert encrypted_store.get_by_token("KPP_BBBBBBBBB") is None


def test_add_many_conflict_between_two_batch_entries_preserves_disk_and_ram(
    target_path: Path, encrypted_store: EncryptedFileIdentifierMappingStore
) -> None:
    with pytest.raises(IdentifierMappingConflictError):
        encrypted_store.add_many(
            [
                _entry("INN_AAAAAAAAA", "7701234567"),
                _entry("INN_BBBBBBBBB", "7701234567"),  # same identity, different token
            ]
        )

    assert not target_path.exists()
    assert encrypted_store.entries() == ()


# ---------------------------------------------------------------------------
# K. Persistence failure (encrypted)
# ---------------------------------------------------------------------------


def test_add_many_failed_os_replace_preserves_disk_and_ram_state(
    target_path: Path, encrypted_store: EncryptedFileIdentifierMappingStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    initial_entry = _entry("INN_AAAAAAAAA", "7701234567")
    encrypted_store.add(initial_entry)
    original_bytes = target_path.read_bytes()

    def failing_replace(*args: object, **kwargs: object) -> None:
        raise OSError("simulated os.replace failure")

    monkeypatch.setattr(os, "replace", failing_replace)

    batch = [_entry("KPP_BBBBBBBBB", "770101001", IdentifierType.KPP), _entry("OGRN_CCCCCCCC", "1047709000008", IdentifierType.OGRN)]
    with pytest.raises(OSError):
        encrypted_store.add_many(batch)

    monkeypatch.undo()

    assert target_path.read_bytes() == original_bytes
    assert encrypted_store.entries() == (initial_entry,)
    assert encrypted_store.get_by_token("KPP_BBBBBBBBB") is None

    leftover = [p for p in target_path.parent.iterdir() if p != target_path]
    assert leftover == []

    reopened = EncryptedFileIdentifierMappingStore(target_path, PASSWORD)
    assert reopened.entries() == (initial_entry,)


def test_add_many_failed_fsync_preserves_state_and_removes_temp(
    target_path: Path, encrypted_store: EncryptedFileIdentifierMappingStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    initial_entry = _entry("INN_AAAAAAAAA", "7701234567")
    encrypted_store.add(initial_entry)
    original_bytes = target_path.read_bytes()

    def failing_fsync(fd: int) -> None:
        raise OSError("simulated fsync failure")

    monkeypatch.setattr(os, "fsync", failing_fsync)

    batch = [_entry("KPP_BBBBBBBBB", "770101001", IdentifierType.KPP)]
    with pytest.raises(OSError):
        encrypted_store.add_many(batch)

    monkeypatch.undo()

    assert target_path.read_bytes() == original_bytes
    assert encrypted_store.entries() == (initial_entry,)

    leftover = [p for p in target_path.parent.iterdir() if p != target_path]
    assert leftover == []


# ---------------------------------------------------------------------------
# L. Confidentiality
# ---------------------------------------------------------------------------


def test_add_many_conflict_message_does_not_expose_identifier_value_memory(
    memory_store: InMemoryIdentifierMappingStore,
) -> None:
    secret_value = "SuperSecretRawIdentifierValue123"
    memory_store.add(_entry("INN_AAAAAAAAA", secret_value))

    with pytest.raises(IdentifierMappingConflictError) as exc_info:
        memory_store.add_many([_entry("INN_AAAAAAAAA", "8812345678")])

    message = str(exc_info.value)
    assert secret_value not in message
    assert "8812345678" not in message


def test_add_many_conflict_message_does_not_expose_identifier_value_encrypted(
    target_path: Path, encrypted_store: EncryptedFileIdentifierMappingStore
) -> None:
    secret_value = "SuperSecretRawIdentifierValue123"
    encrypted_store.add(_entry("INN_AAAAAAAAA", secret_value))

    with pytest.raises(IdentifierMappingConflictError) as exc_info:
        encrypted_store.add_many([_entry("INN_AAAAAAAAA", "8812345678")])

    message = str(exc_info.value)
    assert secret_value not in message
    assert "8812345678" not in message
