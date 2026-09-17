"""
Тесты app.mapping.identifier_base (IdentifierMappingStore) и
app.mapping.identifier_memory (InMemoryIdentifierMappingStore).

Stage 7B.3 — строго in-memory storage contract. Здесь НЕ тестируется:
encrypted persistence (Stage 7B.4), anonymizer integration (Stage 7B.5),
token generation (Stage 7B.2), domain-валидация identifier значений
(checksum ИНН/КПП/ОГРН — Stage 5).
"""

from __future__ import annotations

import inspect

import pytest

from app.mapping.identifier_base import IdentifierMappingConflictError, IdentifierMappingStore
from app.mapping.identifier_memory import InMemoryIdentifierMappingStore
from app.models.identifiers import IdentifierMappingEntry, IdentifierType


@pytest.fixture()
def store() -> InMemoryIdentifierMappingStore:
    return InMemoryIdentifierMappingStore()


def _entry(
    token: str,
    identifier_value: str,
    identifier_type: IdentifierType = IdentifierType.INN,
) -> IdentifierMappingEntry:
    return IdentifierMappingEntry(
        token=token, identifier_value=identifier_value, identifier_type=identifier_type
    )


# ---------------------------------------------------------------------------
# A. Контракт IdentifierMappingStore
# ---------------------------------------------------------------------------


def test_abstract_store_cannot_be_instantiated() -> None:
    with pytest.raises(TypeError):
        IdentifierMappingStore()  # type: ignore[abstract]


def test_in_memory_store_satisfies_identifier_mapping_store_contract(
    store: InMemoryIdentifierMappingStore,
) -> None:
    assert isinstance(store, IdentifierMappingStore)


# ---------------------------------------------------------------------------
# B. Пустое хранилище
# ---------------------------------------------------------------------------


def test_empty_store(store: InMemoryIdentifierMappingStore) -> None:
    assert store.get_by_token("INN_ANYTHING") is None
    assert store.get_by_identity(IdentifierType.INN, "7701234567") is None
    assert store.all_tokens() == set()
    assert store.entries() == ()


# ---------------------------------------------------------------------------
# C-E. add + get_by_token + get_by_identity
# ---------------------------------------------------------------------------


def test_add_and_get_by_token(store: InMemoryIdentifierMappingStore) -> None:
    entry = _entry("INN_7GQ2MX9KA", "7701234567")
    store.add(entry)
    assert store.get_by_token("INN_7GQ2MX9KA") == entry


def test_add_and_get_by_identity(store: InMemoryIdentifierMappingStore) -> None:
    entry = _entry("INN_7GQ2MX9KA", "7701234567", IdentifierType.INN)
    store.add(entry)
    assert store.get_by_identity(IdentifierType.INN, "7701234567") == entry


# ---------------------------------------------------------------------------
# F. Идемпотентный точный дубль
# ---------------------------------------------------------------------------


def test_exact_duplicate_add_is_idempotent(store: InMemoryIdentifierMappingStore) -> None:
    entry = _entry("INN_7GQ2MX9KA", "7701234567")
    store.add(entry)
    store.add(entry)  # повтор той же записи — не ошибка
    assert store.entries() == (entry,)
    assert store.all_tokens() == {"INN_7GQ2MX9KA"}


def test_idempotent_add_equal_but_not_identical_entry(
    store: InMemoryIdentifierMappingStore,
) -> None:
    """Идемпотентность должна работать по значению, а не по identity объекта."""
    store.add(_entry("INN_7GQ2MX9KA", "7701234567"))
    store.add(_entry("INN_7GQ2MX9KA", "7701234567"))  # другой объект, те же значения
    assert len(store.entries()) == 1


# ---------------------------------------------------------------------------
# G-J. Конфликты и их атомарность
# ---------------------------------------------------------------------------


def test_token_conflict_raises(store: InMemoryIdentifierMappingStore) -> None:
    store.add(_entry("INN_ABC123456", "7701234567"))
    with pytest.raises(IdentifierMappingConflictError):
        store.add(_entry("INN_ABC123456", "7812345678"))


def test_identity_conflict_raises(store: InMemoryIdentifierMappingStore) -> None:
    store.add(_entry("INN_ABC123456", "7701234567"))
    with pytest.raises(IdentifierMappingConflictError):
        store.add(_entry("INN_XYZ987654", "7701234567"))


def test_token_conflict_leaves_store_unchanged(store: InMemoryIdentifierMappingStore) -> None:
    original = _entry("INN_ABC123456", "7701234567")
    store.add(original)

    with pytest.raises(IdentifierMappingConflictError):
        store.add(_entry("INN_ABC123456", "7812345678"))

    # Состояние не должно было испортиться конфликтной попыткой.
    assert store.get_by_token("INN_ABC123456") == original
    assert store.get_by_identity(IdentifierType.INN, "7701234567") == original
    assert store.get_by_identity(IdentifierType.INN, "7812345678") is None
    assert store.entries() == (original,)
    assert store.all_tokens() == {"INN_ABC123456"}


def test_identity_conflict_leaves_store_unchanged(store: InMemoryIdentifierMappingStore) -> None:
    original = _entry("INN_ABC123456", "7701234567")
    store.add(original)

    with pytest.raises(IdentifierMappingConflictError):
        store.add(_entry("INN_XYZ987654", "7701234567"))

    assert store.get_by_identity(IdentifierType.INN, "7701234567") == original
    assert store.get_by_token("INN_XYZ987654") is None
    assert store.entries() == (original,)
    assert store.all_tokens() == {"INN_ABC123456"}


# ---------------------------------------------------------------------------
# K. Разные IdentifierType с одинаковым identifier_value — разная identity
# ---------------------------------------------------------------------------


def test_same_identifier_value_different_type_is_different_identity(
    store: InMemoryIdentifierMappingStore,
) -> None:
    inn_entry = _entry("INN_AAAAAAAAA", "123456789", IdentifierType.INN)
    kpp_entry = _entry("KPP_BBBBBBBBB", "123456789", IdentifierType.KPP)
    store.add(inn_entry)
    store.add(kpp_entry)  # не конфликт: разные identifier_type => разная identity

    assert store.get_by_identity(IdentifierType.INN, "123456789") == inn_entry
    assert store.get_by_identity(IdentifierType.KPP, "123456789") == kpp_entry
    assert len(store.entries()) == 2


# ---------------------------------------------------------------------------
# L-M. EXACT value: whitespace / leading zeros
# ---------------------------------------------------------------------------


def test_outer_whitespace_is_a_different_identity(store: InMemoryIdentifierMappingStore) -> None:
    bare = _entry("INN_AAAAAAAAA", "7701234567")
    padded = _entry("INN_BBBBBBBBB", " 7701234567 ")
    store.add(bare)
    store.add(padded)  # не конфликт: разные строки => разная identity

    assert store.get_by_identity(IdentifierType.INN, "7701234567") == bare
    assert store.get_by_identity(IdentifierType.INN, " 7701234567 ") == padded
    assert store.get_by_identity(IdentifierType.INN, "7701234567 ") is None
    assert len(store.entries()) == 2


def test_leading_zero_is_a_different_identity(store: InMemoryIdentifierMappingStore) -> None:
    with_zero = _entry("INN_AAAAAAAAA", "0770123456")
    without_zero = _entry("INN_BBBBBBBBB", "770123456")
    store.add(with_zero)
    store.add(without_zero)

    assert store.get_by_identity(IdentifierType.INN, "0770123456") == with_zero
    assert store.get_by_identity(IdentifierType.INN, "770123456") == without_zero
    assert len(store.entries()) == 2


# ---------------------------------------------------------------------------
# N-O. Snapshot semantics
# ---------------------------------------------------------------------------


def test_all_tokens_snapshot_is_independent(store: InMemoryIdentifierMappingStore) -> None:
    store.add(_entry("INN_AAAAAAAAA", "7701234567"))
    tokens = store.all_tokens()
    tokens.clear()

    assert store.all_tokens() == {"INN_AAAAAAAAA"}


def test_entries_snapshot_is_independent(store: InMemoryIdentifierMappingStore) -> None:
    entry_a = _entry("INN_AAAAAAAAA", "7701234567")
    store.add(entry_a)
    snapshot = store.entries()

    store.add(_entry("KPP_BBBBBBBBB", "770101001", IdentifierType.KPP))

    # Ранее полученный snapshot не должен был "увидеть" новую запись.
    assert snapshot == (entry_a,)
    assert len(store.entries()) == 2


# ---------------------------------------------------------------------------
# P. Insertion order
# ---------------------------------------------------------------------------


def test_entries_preserve_insertion_order(store: InMemoryIdentifierMappingStore) -> None:
    entry_a = _entry("INN_AAAAAAAAA", "1")
    entry_b = _entry("INN_BBBBBBBBB", "2")
    entry_c = _entry("INN_CCCCCCCCC", "3")
    store.add(entry_b)
    store.add(entry_a)
    store.add(entry_c)

    assert store.entries() == (entry_b, entry_a, entry_c)


# ---------------------------------------------------------------------------
# Q-R. clear()
# ---------------------------------------------------------------------------


def test_clear_clears_both_indexes(store: InMemoryIdentifierMappingStore) -> None:
    store.add(_entry("INN_AAAAAAAAA", "7701234567"))
    store.clear()

    assert store.get_by_token("INN_AAAAAAAAA") is None
    assert store.get_by_identity(IdentifierType.INN, "7701234567") is None
    assert store.all_tokens() == set()
    assert store.entries() == ()


def test_repeated_clear_is_safe(store: InMemoryIdentifierMappingStore) -> None:
    store.add(_entry("INN_AAAAAAAAA", "7701234567"))
    store.clear()
    store.clear()  # повторный clear не должен падать

    assert store.entries() == ()


def test_reuse_after_clear(store: InMemoryIdentifierMappingStore) -> None:
    entry = _entry("INN_AAAAAAAAA", "7701234567")
    store.add(entry)
    store.clear()

    store.add(entry)  # тот же token и та же identity — ранее уже использовались

    assert store.get_by_token("INN_AAAAAAAAA") == entry


# ---------------------------------------------------------------------------
# S. Конфликтное сообщение не раскрывает identifier_value
# ---------------------------------------------------------------------------


def test_token_conflict_message_does_not_expose_identifier_value(
    store: InMemoryIdentifierMappingStore,
) -> None:
    store.add(_entry("INN_ABC123456", "7701234567"))
    with pytest.raises(IdentifierMappingConflictError) as exc_info:
        store.add(_entry("INN_ABC123456", "7812345678"))

    message = str(exc_info.value)
    assert "7701234567" not in message
    assert "7812345678" not in message
    assert "INN_ABC123456" in message  # token — псевдоним, безопасно раскрывать


def test_identity_conflict_message_does_not_expose_identifier_value(
    store: InMemoryIdentifierMappingStore,
) -> None:
    store.add(_entry("INN_ABC123456", "7701234567"))
    with pytest.raises(IdentifierMappingConflictError) as exc_info:
        store.add(_entry("INN_XYZ987654", "7701234567"))

    message = str(exc_info.value)
    assert "7701234567" not in message
    assert "INN_ABC123456" in message
    assert "INN_XYZ987654" in message


# ---------------------------------------------------------------------------
# T-V. Отсутствие entity/Excel/provenance концепций
# ---------------------------------------------------------------------------


def test_no_parent_alias_concept_anywhere() -> None:
    import dataclasses

    field_names = {f.name for f in dataclasses.fields(IdentifierMappingEntry)}
    assert "parent_alias" not in field_names

    for method_name in ("add", "get_by_token", "get_by_identity", "all_tokens", "entries", "clear"):
        sig = inspect.signature(getattr(IdentifierMappingStore, method_name))
        assert "parent_alias" not in sig.parameters
        assert "parent" not in sig.parameters
        assert "entity_alias" not in sig.parameters
        assert "company_alias" not in sig.parameters


def test_no_python_origin_type_concept() -> None:
    import dataclasses

    field_names = {f.name for f in dataclasses.fields(IdentifierMappingEntry)}
    for forbidden in ("origin_python_type", "python_type", "source_type", "value_type", "was_integer"):
        assert forbidden not in field_names


def test_no_excel_coordinate_concept() -> None:
    import dataclasses

    field_names = {f.name for f in dataclasses.fields(IdentifierMappingEntry)}
    for forbidden in ("sheet", "sheet_name", "row", "column", "cell", "coordinate"):
        assert forbidden not in field_names

    for method_name in ("add", "get_by_token", "get_by_identity", "all_tokens", "entries", "clear"):
        sig = inspect.signature(getattr(IdentifierMappingStore, method_name))
        for forbidden in ("sheet", "sheet_name", "row", "column", "cell", "coordinate"):
            assert forbidden not in sig.parameters


# ---------------------------------------------------------------------------
# W-X. Минимальный шестиметодный API, без лишнего
# ---------------------------------------------------------------------------


def test_abstract_api_has_exactly_six_methods() -> None:
    assert IdentifierMappingStore.__abstractmethods__ == {
        "add",
        "get_by_token",
        "get_by_identity",
        "all_tokens",
        "entries",
        "clear",
    }


def test_no_forbidden_extra_methods_on_store() -> None:
    forbidden_names = (
        "contains_token",
        "remove",
        "update",
        "save",
        "load",
        "flush",
        "commit",
        "rollback",
        "begin",
        "transaction",
        "get_by_value",
        "get_by_identifier_value",
    )
    for name in forbidden_names:
        assert not hasattr(IdentifierMappingStore, name)
        assert not hasattr(InMemoryIdentifierMappingStore, name)


def test_get_by_identity_requires_both_identifier_type_and_value() -> None:
    """Partial lookup (только по identifier_value, без identifier_type) не поддержан."""
    sig = inspect.signature(InMemoryIdentifierMappingStore.get_by_identity)
    params = list(sig.parameters)
    assert "identifier_type" in params
    assert "identifier_value" in params
    # Оба параметра обязательны — без значений по умолчанию.
    assert sig.parameters["identifier_type"].default is inspect.Parameter.empty
    assert sig.parameters["identifier_value"].default is inspect.Parameter.empty


# ---------------------------------------------------------------------------
# Валидация входных параметров lookup-методов
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("bad_token", ["", "   ", None, 123])
def test_get_by_token_rejects_invalid_token(
    store: InMemoryIdentifierMappingStore, bad_token: object
) -> None:
    with pytest.raises(ValueError):
        store.get_by_token(bad_token)  # type: ignore[arg-type]


@pytest.mark.parametrize("bad_value", ["", "   ", None, 123])
def test_get_by_identity_rejects_invalid_identifier_value(
    store: InMemoryIdentifierMappingStore, bad_value: object
) -> None:
    with pytest.raises(ValueError):
        store.get_by_identity(IdentifierType.INN, bad_value)  # type: ignore[arg-type]


def test_get_by_identity_rejects_invalid_identifier_type(
    store: InMemoryIdentifierMappingStore,
) -> None:
    with pytest.raises(ValueError):
        store.get_by_identity("inn", "7701234567")  # type: ignore[arg-type]


def test_add_rejects_non_identifier_mapping_entry(store: InMemoryIdentifierMappingStore) -> None:
    with pytest.raises(TypeError):
        store.add("not-an-identifier-mapping-entry")  # type: ignore[arg-type]
