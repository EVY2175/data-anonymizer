"""
Тесты моделей данных: app.models.entities и app.models.rules.
"""

from __future__ import annotations

import dataclasses

import pytest

from app.models.entities import EntityType, MappingEntry
from app.models.rules import Action, FieldRule, FieldType


# ---------------------------------------------------------------------------
# EntityType
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "entity_type, expected_prefix",
    [
        (EntityType.COMPANY, "C"),
        (EntityType.BRANCH, "B"),
        (EntityType.DEPARTMENT, "D"),
        (EntityType.PERSON, "P"),
    ],
)
def test_entity_type_prefix(entity_type: EntityType, expected_prefix: str) -> None:
    assert entity_type.alias_prefix == expected_prefix


def test_unknown_entity_type_has_safe_distinct_prefix() -> None:
    """
    UNKNOWN не должен молча совпадать с одним из типизированных префиксов
    (это создало бы риск спутать неопознанную сущность с осмысленным
    типом) и должен быть однозначно определён.
    """
    known_prefixes = {
        EntityType.COMPANY.alias_prefix,
        EntityType.BRANCH.alias_prefix,
        EntityType.DEPARTMENT.alias_prefix,
        EntityType.PERSON.alias_prefix,
    }
    assert EntityType.UNKNOWN.alias_prefix
    assert EntityType.UNKNOWN.alias_prefix not in known_prefixes


def test_every_entity_type_has_a_prefix() -> None:
    for entity_type in EntityType:
        assert isinstance(entity_type.alias_prefix, str)
        assert entity_type.alias_prefix


def test_entity_type_prefixes_are_unique() -> None:
    prefixes = [entity_type.alias_prefix for entity_type in EntityType]
    assert len(prefixes) == len(set(prefixes))


# ---------------------------------------------------------------------------
# MappingEntry
# ---------------------------------------------------------------------------

def test_mapping_entry_creation_minimal() -> None:
    entry = MappingEntry(
        alias="C_7GQ2MX9KA", real_value='ООО "Компания"', entity_type=EntityType.COMPANY
    )
    assert entry.alias == "C_7GQ2MX9KA"
    assert entry.real_value == 'ООО "Компания"'
    assert entry.entity_type is EntityType.COMPANY
    assert entry.parent_alias is None


def test_mapping_entry_creation_with_parent() -> None:
    entry = MappingEntry(
        alias="B_4K8M2Q7RX",
        real_value="Московский филиал",
        entity_type=EntityType.BRANCH,
        parent_alias="C_7GQ2MX9KA",
    )
    assert entry.parent_alias == "C_7GQ2MX9KA"


@pytest.mark.parametrize(
    "kwargs",
    [
        dict(alias="", real_value="X", entity_type=EntityType.COMPANY),
        dict(alias="   ", real_value="X", entity_type=EntityType.COMPANY),
        dict(alias="C_X", real_value="", entity_type=EntityType.COMPANY),
        dict(alias="C_X", real_value="X", entity_type="company"),
        dict(alias="C_X", real_value="X", entity_type=EntityType.COMPANY, parent_alias="   "),
        dict(alias="C_X", real_value="X", entity_type=EntityType.COMPANY, parent_alias="C_X"),
    ],
)
def test_mapping_entry_rejects_invalid_data(kwargs) -> None:
    with pytest.raises(ValueError):
        MappingEntry(**kwargs)


def test_mapping_entry_is_immutable() -> None:
    entry = MappingEntry(alias="C_X", real_value="X", entity_type=EntityType.COMPANY)
    with pytest.raises(dataclasses.FrozenInstanceError):
        entry.alias = "C_Y"  # type: ignore[misc]


def test_mapping_entry_round_trip_dict() -> None:
    original = MappingEntry(
        alias="B_4K8M2Q7RX",
        real_value="Московский филиал",
        entity_type=EntityType.BRANCH,
        parent_alias="C_7GQ2MX9KA",
    )
    data = original.to_dict()
    restored = MappingEntry.from_dict(data)
    assert restored == original
    assert restored.to_dict() == data


def test_mapping_entry_round_trip_dict_without_parent() -> None:
    original = MappingEntry(alias="C_X", real_value="X", entity_type=EntityType.COMPANY)
    data = original.to_dict()
    assert data["parent_alias"] is None
    restored = MappingEntry.from_dict(data)
    assert restored == original


def test_mapping_entry_from_dict_missing_fields() -> None:
    with pytest.raises(ValueError):
        MappingEntry.from_dict({"alias": "C_X"})


def test_mapping_entry_from_dict_invalid_entity_type() -> None:
    with pytest.raises(ValueError):
        MappingEntry.from_dict({"alias": "C_X", "real_value": "X", "entity_type": "not-a-type"})


def test_mapping_entry_from_dict_not_a_dict() -> None:
    with pytest.raises(ValueError):
        MappingEntry.from_dict(["not", "a", "dict"])  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# FieldType / Action / FieldRule
# ---------------------------------------------------------------------------

def test_action_has_required_members() -> None:
    assert Action.PSEUDONYMIZE.value == "pseudonymize"
    assert Action.REMOVE.value == "remove"
    assert Action.KEEP.value == "keep"


def test_field_rule_creation() -> None:
    rule = FieldRule(
        column_name="Контрагент", field_type=FieldType.COMPANY, action=Action.PSEUDONYMIZE
    )
    assert rule.column_name == "Контрагент"
    assert rule.field_type is FieldType.COMPANY
    assert rule.action is Action.PSEUDONYMIZE


@pytest.mark.parametrize(
    "kwargs",
    [
        dict(column_name="", field_type=FieldType.COMPANY, action=Action.KEEP),
        dict(column_name="   ", field_type=FieldType.COMPANY, action=Action.KEEP),
        dict(column_name="X", field_type="company", action=Action.KEEP),
        dict(column_name="X", field_type=FieldType.COMPANY, action="keep"),
    ],
)
def test_field_rule_rejects_invalid_data(kwargs) -> None:
    with pytest.raises(ValueError):
        FieldRule(**kwargs)


def test_field_rule_round_trip_dict() -> None:
    original = FieldRule(column_name="ИНН", field_type=FieldType.INN, action=Action.REMOVE)
    data = original.to_dict()
    restored = FieldRule.from_dict(data)
    assert restored == original
    assert restored.to_dict() == data


def test_field_rule_from_dict_missing_fields() -> None:
    with pytest.raises(ValueError):
        FieldRule.from_dict({"column_name": "X"})


def test_field_rule_from_dict_invalid_field_type() -> None:
    with pytest.raises(ValueError):
        FieldRule.from_dict({"column_name": "X", "field_type": "not-a-type", "action": "keep"})


def test_field_rule_from_dict_invalid_action() -> None:
    with pytest.raises(ValueError):
        FieldRule.from_dict({"column_name": "X", "field_type": "company", "action": "not-an-action"})


def test_field_rule_from_dict_not_a_dict() -> None:
    with pytest.raises(ValueError):
        FieldRule.from_dict(["not", "a", "dict"])  # type: ignore[arg-type]


def test_field_rule_is_mutable_for_user_override() -> None:
    """
    В отличие от MappingEntry, FieldRule должен позволять пользователю
    изменить action после автоматического анализа — поэтому не frozen.
    """
    rule = FieldRule(column_name="Оборот", field_type=FieldType.FINANCIAL, action=Action.KEEP)
    rule.action = Action.REMOVE
    assert rule.action is Action.REMOVE
