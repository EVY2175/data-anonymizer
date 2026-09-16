"""
Тесты app.mapping.base (MappingStore) и app.mapping.memory (InMemoryMappingStore).
"""

from __future__ import annotations

import pytest

from app.mapping.base import AmbiguousMappingError, MappingConflictError, MappingStore
from app.mapping.memory import InMemoryMappingStore
from app.models.entities import EntityType, MappingEntry


@pytest.fixture()
def store() -> InMemoryMappingStore:
    return InMemoryMappingStore()


def _entry(
    alias: str,
    real_value: str,
    entity_type: EntityType = EntityType.COMPANY,
    parent_alias: str | None = None,
) -> MappingEntry:
    return MappingEntry(
        alias=alias, real_value=real_value, entity_type=entity_type, parent_alias=parent_alias
    )


# ---------------------------------------------------------------------------
# Контракт MappingStore
# ---------------------------------------------------------------------------

def test_mapping_store_is_abstract() -> None:
    with pytest.raises(TypeError):
        MappingStore()  # type: ignore[abstract]


def test_in_memory_store_satisfies_mapping_store_contract(store: InMemoryMappingStore) -> None:
    assert isinstance(store, MappingStore)


# ---------------------------------------------------------------------------
# 1. Пустое хранилище
# ---------------------------------------------------------------------------

def test_empty_store(store: InMemoryMappingStore) -> None:
    assert store.get_by_alias("C_ANYTHING") is None
    assert store.get_by_real_value("Что угодно") is None
    assert store.contains_alias("C_ANYTHING") is False
    assert store.all_aliases() == set()
    assert store.entries() == ()


# ---------------------------------------------------------------------------
# 2. add + get_by_alias
# ---------------------------------------------------------------------------

def test_add_and_get_by_alias(store: InMemoryMappingStore) -> None:
    entry = _entry("C_7GQ2MX9KA", 'ООО "Компания"')
    store.add(entry)
    assert store.get_by_alias("C_7GQ2MX9KA") == entry


# ---------------------------------------------------------------------------
# 3. add + get_by_real_value по полной identity
# ---------------------------------------------------------------------------

def test_add_and_get_by_real_value_full_identity(store: InMemoryMappingStore) -> None:
    entry = _entry(
        "D_9T3W6H2KM", "Продажи", entity_type=EntityType.DEPARTMENT, parent_alias="B_111AAAAAA"
    )
    store.add(entry)
    found = store.get_by_real_value(
        "Продажи", entity_type=EntityType.DEPARTMENT, parent_alias="B_111AAAAAA"
    )
    assert found == entry


# ---------------------------------------------------------------------------
# 4. contains_alias
# ---------------------------------------------------------------------------

def test_contains_alias(store: InMemoryMappingStore) -> None:
    assert store.contains_alias("C_X") is False
    store.add(_entry("C_X", "Компания X"))
    assert store.contains_alias("C_X") is True


# ---------------------------------------------------------------------------
# 5. all_aliases
# ---------------------------------------------------------------------------

def test_all_aliases(store: InMemoryMappingStore) -> None:
    store.add(_entry("C_A", "A"))
    store.add(_entry("C_B", "B"))
    assert store.all_aliases() == {"C_A", "C_B"}


# ---------------------------------------------------------------------------
# 6. entries
# ---------------------------------------------------------------------------

def test_entries(store: InMemoryMappingStore) -> None:
    entry_a = _entry("C_A", "A")
    entry_b = _entry("C_B", "B")
    store.add(entry_a)
    store.add(entry_b)
    result = store.entries()
    assert isinstance(result, tuple)
    assert set(result) == {entry_a, entry_b}


# ---------------------------------------------------------------------------
# 7. clear
# ---------------------------------------------------------------------------

def test_clear(store: InMemoryMappingStore) -> None:
    store.add(_entry("C_A", "A"))
    store.clear()
    assert store.all_aliases() == set()
    assert store.entries() == ()
    assert store.get_by_alias("C_A") is None


# ---------------------------------------------------------------------------
# 8. Идемпотентное повторное add той же записи
# ---------------------------------------------------------------------------

def test_idempotent_add_same_entry(store: InMemoryMappingStore) -> None:
    entry = _entry("C_A", "A")
    store.add(entry)
    store.add(entry)  # повтор той же записи — не ошибка
    assert store.entries() == (entry,)
    assert store.all_aliases() == {"C_A"}


def test_idempotent_add_equal_but_not_identical_entry(store: InMemoryMappingStore) -> None:
    """Идемпотентность должна работать по значению, а не по identity объекта."""
    store.add(_entry("C_A", "A"))
    store.add(_entry("C_A", "A"))  # другой объект Python, те же значения полей
    assert len(store.entries()) == 1


# ---------------------------------------------------------------------------
# 9. Конфликт: одинаковый alias, разные записи
# ---------------------------------------------------------------------------

def test_conflict_same_alias_different_entries(store: InMemoryMappingStore) -> None:
    store.add(_entry("P_ABC123456", "Иванов", entity_type=EntityType.PERSON))
    with pytest.raises(MappingConflictError):
        store.add(_entry("P_ABC123456", "Петров", entity_type=EntityType.PERSON))
    # Состояние не должно было испортиться конфликтной попыткой.
    assert store.get_by_alias("P_ABC123456").real_value == "Иванов"


# ---------------------------------------------------------------------------
# 10. Конфликт: одинаковая полная identity, разные alias
# ---------------------------------------------------------------------------

def test_conflict_same_identity_different_alias(store: InMemoryMappingStore) -> None:
    store.add(_entry("C_A", "ООО Ромашка"))
    with pytest.raises(MappingConflictError):
        store.add(_entry("C_B", "ООО Ромашка"))
    assert store.contains_alias("C_B") is False
    assert store.get_by_alias("C_A").alias == "C_A"
    assert len(store.entries()) == 1


# ---------------------------------------------------------------------------
# 11. Одинаковый real_value разрешён для разных EntityType
# ---------------------------------------------------------------------------

def test_same_real_value_allowed_for_different_entity_type(store: InMemoryMappingStore) -> None:
    company_entry = _entry("C_IVANOV11A", "Иванов", entity_type=EntityType.COMPANY)
    person_entry = _entry("P_IVANOV22B", "Иванов", entity_type=EntityType.PERSON)
    store.add(company_entry)
    store.add(person_entry)  # не конфликт: разные entity_type => разная identity

    assert store.get_by_real_value("Иванов", entity_type=EntityType.COMPANY) == company_entry
    assert store.get_by_real_value("Иванов", entity_type=EntityType.PERSON) == person_entry
    with pytest.raises(AmbiguousMappingError):
        store.get_by_real_value("Иванов")


# ---------------------------------------------------------------------------
# 12. Одинаковый real_value разрешён для разных parent_alias
# ---------------------------------------------------------------------------

def test_same_real_value_allowed_for_different_parent_alias(store: InMemoryMappingStore) -> None:
    dept_1 = _entry(
        "D_AAA111111", "Продажи", entity_type=EntityType.DEPARTMENT, parent_alias="B_111AAAAAA"
    )
    dept_2 = _entry(
        "D_BBB222222", "Продажи", entity_type=EntityType.DEPARTMENT, parent_alias="B_222BBBBBB"
    )
    store.add(dept_1)
    store.add(dept_2)  # не конфликт: разные parent_alias => разная identity

    assert (
        store.get_by_real_value(
            "Продажи", entity_type=EntityType.DEPARTMENT, parent_alias="B_111AAAAAA"
        )
        == dept_1
    )
    assert (
        store.get_by_real_value(
            "Продажи", entity_type=EntityType.DEPARTMENT, parent_alias="B_222BBBBBB"
        )
        == dept_2
    )


# ---------------------------------------------------------------------------
# 13. Неоднозначный get_by_real_value без достаточного контекста
# ---------------------------------------------------------------------------

def test_ambiguous_lookup_without_sufficient_context(store: InMemoryMappingStore) -> None:
    dept_1 = _entry(
        "D_AAA111111", "Продажи", entity_type=EntityType.DEPARTMENT, parent_alias="B_111AAAAAA"
    )
    dept_2 = _entry(
        "D_BBB222222", "Продажи", entity_type=EntityType.DEPARTMENT, parent_alias="B_222BBBBBB"
    )
    store.add(dept_1)
    store.add(dept_2)

    with pytest.raises(AmbiguousMappingError):
        store.get_by_real_value("Продажи")

    with pytest.raises(AmbiguousMappingError):
        store.get_by_real_value("Продажи", entity_type=EntityType.DEPARTMENT)

    # Полностью заданная identity разрешает неоднозначность.
    assert (
        store.get_by_real_value(
            "Продажи", entity_type=EntityType.DEPARTMENT, parent_alias="B_111AAAAAA"
        )
        == dept_1
    )


# ---------------------------------------------------------------------------
# 14. snapshot all_aliases нельзя использовать для изменения store
# ---------------------------------------------------------------------------

def test_all_aliases_snapshot_is_independent(store: InMemoryMappingStore) -> None:
    store.add(_entry("C_A", "A"))
    snapshot = store.all_aliases()
    snapshot.add("C_INJECTED")
    snapshot.discard("C_A")

    assert store.all_aliases() == {"C_A"}
    assert store.contains_alias("C_INJECTED") is False


# ---------------------------------------------------------------------------
# 15. entries не раскрывает изменяемую внутреннюю коллекцию
# ---------------------------------------------------------------------------

def test_entries_snapshot_is_independent(store: InMemoryMappingStore) -> None:
    entry_a = _entry("C_A", "A")
    store.add(entry_a)
    snapshot = store.entries()

    store.add(_entry("C_B", "B"))

    # Ранее полученный snapshot не должен был "увидеть" новую запись.
    assert snapshot == (entry_a,)
    assert len(store.entries()) == 2


# ---------------------------------------------------------------------------
# 16. parent_alias не обязан существовать в store
# ---------------------------------------------------------------------------

def test_parent_alias_existence_not_validated(store: InMemoryMappingStore) -> None:
    entry = _entry(
        "D_UNKNOWNPR", "Продажи", entity_type=EntityType.DEPARTMENT, parent_alias="B_UNKNOWN00"
    )
    store.add(entry)  # не должно бросить исключение, даже если B_UNKNOWN00 не добавлен
    assert store.get_by_alias("D_UNKNOWNPR") == entry
    assert store.contains_alias("B_UNKNOWN00") is False


# ---------------------------------------------------------------------------
# 17. Некорректные входные данные публичных методов
# ---------------------------------------------------------------------------

def test_add_rejects_non_mapping_entry(store: InMemoryMappingStore) -> None:
    with pytest.raises(TypeError):
        store.add("not-a-mapping-entry")  # type: ignore[arg-type]


@pytest.mark.parametrize("bad_alias", ["", "   ", None, 123])
def test_get_by_alias_rejects_invalid_alias(store: InMemoryMappingStore, bad_alias) -> None:
    with pytest.raises(ValueError):
        store.get_by_alias(bad_alias)  # type: ignore[arg-type]


@pytest.mark.parametrize("bad_alias", ["", "   ", None, 123])
def test_contains_alias_rejects_invalid_alias(store: InMemoryMappingStore, bad_alias) -> None:
    with pytest.raises(ValueError):
        store.contains_alias(bad_alias)  # type: ignore[arg-type]


@pytest.mark.parametrize("bad_real_value", ["", "   ", None, 123])
def test_get_by_real_value_rejects_invalid_real_value(
    store: InMemoryMappingStore, bad_real_value
) -> None:
    with pytest.raises(ValueError):
        store.get_by_real_value(bad_real_value)  # type: ignore[arg-type]


def test_get_by_real_value_rejects_invalid_entity_type(store: InMemoryMappingStore) -> None:
    with pytest.raises(ValueError):
        store.get_by_real_value("X", entity_type="company")  # type: ignore[arg-type]


@pytest.mark.parametrize("bad_parent_alias", ["", "   ", 123])
def test_get_by_real_value_rejects_invalid_parent_alias(
    store: InMemoryMappingStore, bad_parent_alias
) -> None:
    with pytest.raises(ValueError):
        store.get_by_real_value("X", parent_alias=bad_parent_alias)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# 18. После clear можно повторно добавить ранее использованные значения/alias
# ---------------------------------------------------------------------------

def test_reuse_after_clear(store: InMemoryMappingStore) -> None:
    entry = _entry("C_A", "A")
    store.add(entry)
    store.clear()

    store.add(entry)  # тот же alias и та же identity — ранее уже использовались

    assert store.get_by_alias("C_A") == entry
    assert store.get_by_real_value("A", entity_type=EntityType.COMPANY) == entry


# ---------------------------------------------------------------------------
# Различение "parent_alias не задан" и "parent_alias явно None"
#
# Сценарий: один и тот же (real_value, entity_type), но одна запись —
# top-level (parent_alias=None), другая — дочерняя (parent_alias="B_...").
# ---------------------------------------------------------------------------

@pytest.fixture()
def store_with_mixed_parent_aliases(store: InMemoryMappingStore):
    top_level = _entry(
        "D_TOPLEVEL1", "Продажи", entity_type=EntityType.DEPARTMENT, parent_alias=None
    )
    child = _entry(
        "D_CHILD0001", "Продажи", entity_type=EntityType.DEPARTMENT, parent_alias="B_PARENT001"
    )
    store.add(top_level)
    store.add(child)
    return store, top_level, child


# A. Обе записи добавляются без конфликта и сосуществуют в хранилище.
def test_mixed_parent_alias_entries_coexist(store_with_mixed_parent_aliases) -> None:
    store, top_level, child = store_with_mixed_parent_aliases
    assert store.contains_alias(top_level.alias)
    assert store.contains_alias(child.alias)
    assert set(store.entries()) == {top_level, child}


# B. get_by_real_value(real_value, entity_type) без parent_alias -> AmbiguousMappingError.
def test_lookup_without_parent_alias_is_ambiguous_when_mixed(
    store_with_mixed_parent_aliases,
) -> None:
    store, _, _ = store_with_mixed_parent_aliases
    with pytest.raises(AmbiguousMappingError):
        store.get_by_real_value("Продажи", EntityType.DEPARTMENT)


# C. get_by_real_value(real_value, entity_type, parent_alias=None) -> именно top-level запись.
def test_lookup_with_explicit_none_parent_alias_returns_top_level(
    store_with_mixed_parent_aliases,
) -> None:
    store, top_level, _ = store_with_mixed_parent_aliases
    found = store.get_by_real_value("Продажи", EntityType.DEPARTMENT, parent_alias=None)
    assert found == top_level


# D. get_by_real_value(real_value, entity_type, parent_alias="B_...") -> именно дочерняя запись.
def test_lookup_with_explicit_parent_alias_returns_child(
    store_with_mixed_parent_aliases,
) -> None:
    store, _, child = store_with_mixed_parent_aliases
    found = store.get_by_real_value(
        "Продажи", EntityType.DEPARTMENT, parent_alias="B_PARENT001"
    )
    assert found == child


def test_omitted_vs_explicit_none_parent_alias_differ(store_with_mixed_parent_aliases) -> None:
    """
    Ключевой контраст, ради которого введён sentinel: один и тот же вызов
    с опущенным parent_alias и с parent_alias=None даёт РАЗНЫЙ результат.
    """
    store, top_level, _ = store_with_mixed_parent_aliases

    with pytest.raises(AmbiguousMappingError):
        store.get_by_real_value("Продажи", EntityType.DEPARTMENT)  # опущен -> неоднозначно

    assert (
        store.get_by_real_value("Продажи", EntityType.DEPARTMENT, parent_alias=None)
        == top_level
    )  # явный None -> однозначно top-level


def test_explicit_none_parent_alias_filters_even_without_entity_type(
    store_with_mixed_parent_aliases,
) -> None:
    """parent_alias=None должен фильтровать даже если entity_type не указан."""
    store, top_level, _child = store_with_mixed_parent_aliases
    found = store.get_by_real_value("Продажи", parent_alias=None)
    assert found == top_level


def test_sentinel_is_not_part_of_public_api(store: InMemoryMappingStore) -> None:
    """
    Sentinel — деталь реализации: вызывающему коду никогда не нужно (и не
    должно быть нужно) явно на него ссылаться, вызовы работают только
    через сигнатуру по умолчанию.
    """
    import inspect

    from app.mapping.base import _PARENT_UNSET

    sig = inspect.signature(InMemoryMappingStore.get_by_real_value)
    assert sig.parameters["parent_alias"].default is _PARENT_UNSET
