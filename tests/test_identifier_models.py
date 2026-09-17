"""
Тесты app.models.identifiers: IdentifierType, IdentifierMappingEntry.

Stage 7B.1 — только модель данных. Здесь НЕ тестируется: lookup/store
semantics (Stage 7B.3), формат/генерация token (Stage 7B.2), domain
validation checksum ИНН/КПП/ОГРН (Stage 7B.5).
"""

from __future__ import annotations

import dataclasses

import pytest

from app.models.identifiers import IdentifierMappingEntry, IdentifierType

# ---------------------------------------------------------------------------
# IdentifierType
# ---------------------------------------------------------------------------


def test_identifier_type_has_exactly_inn_kpp_ogrn() -> None:
    assert {member.name for member in IdentifierType} == {"INN", "KPP", "OGRN"}


def test_identifier_type_exact_enum_values() -> None:
    assert IdentifierType.INN.value == "inn"
    assert IdentifierType.KPP.value == "kpp"
    assert IdentifierType.OGRN.value == "ogrn"


def test_identifier_type_has_no_ogrnip() -> None:
    assert not hasattr(IdentifierType, "OGRNIP")


def test_identifier_type_has_no_phone_email_address_financial() -> None:
    for name in ("PHONE", "EMAIL", "ADDRESS", "FINANCIAL"):
        assert not hasattr(IdentifierType, name)


# ---------------------------------------------------------------------------
# IdentifierMappingEntry — корректное создание
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("identifier_type", list(IdentifierType))
def test_identifier_mapping_entry_creation_for_each_type(
    identifier_type: IdentifierType,
) -> None:
    entry = IdentifierMappingEntry(
        token="INN_7GQ2MX9KA",
        identifier_value="7701234567",
        identifier_type=identifier_type,
    )
    assert entry.token == "INN_7GQ2MX9KA"
    assert entry.identifier_value == "7701234567"
    assert entry.identifier_type is identifier_type


# ---------------------------------------------------------------------------
# Immutability / equality
# ---------------------------------------------------------------------------


def test_identifier_mapping_entry_is_frozen() -> None:
    entry = IdentifierMappingEntry(
        token="INN_7GQ2MX9KA", identifier_value="7701234567", identifier_type=IdentifierType.INN
    )
    with pytest.raises(dataclasses.FrozenInstanceError):
        entry.token = "changed"  # type: ignore[misc]
    with pytest.raises(dataclasses.FrozenInstanceError):
        entry.identifier_value = "changed"  # type: ignore[misc]
    with pytest.raises(dataclasses.FrozenInstanceError):
        entry.identifier_type = IdentifierType.KPP  # type: ignore[misc]


def test_identifier_mapping_entry_equality_is_by_value() -> None:
    entry_a = IdentifierMappingEntry(
        token="INN_7GQ2MX9KA", identifier_value="7701234567", identifier_type=IdentifierType.INN
    )
    entry_b = IdentifierMappingEntry(
        token="INN_7GQ2MX9KA", identifier_value="7701234567", identifier_type=IdentifierType.INN
    )
    entry_c = IdentifierMappingEntry(
        token="INN_DIFFERENT", identifier_value="7701234567", identifier_type=IdentifierType.INN
    )
    assert entry_a == entry_b
    assert entry_a is not entry_b
    assert entry_a != entry_c


# ---------------------------------------------------------------------------
# EXACT value contract: token / identifier_value без нормализации
# ---------------------------------------------------------------------------


def test_token_is_stored_exact() -> None:
    entry = IdentifierMappingEntry(
        token="INN_7GQ2MX9KA", identifier_value="7701234567", identifier_type=IdentifierType.INN
    )
    assert entry.token == "INN_7GQ2MX9KA"


def test_identifier_value_is_stored_exact() -> None:
    entry = IdentifierMappingEntry(
        token="INN_7GQ2MX9KA", identifier_value="7701234567", identifier_type=IdentifierType.INN
    )
    assert entry.identifier_value == "7701234567"


def test_outer_whitespace_inside_nonempty_identifier_value_is_preserved() -> None:
    entry = IdentifierMappingEntry(
        token="INN_7GQ2MX9KA",
        identifier_value=" 7701234567 ",
        identifier_type=IdentifierType.INN,
    )
    assert entry.identifier_value == " 7701234567 "


def test_leading_zero_in_identifier_value_is_preserved() -> None:
    entry = IdentifierMappingEntry(
        token="INN_7GQ2MX9KA",
        identifier_value="0770123456",
        identifier_type=IdentifierType.INN,
    )
    assert entry.identifier_value == "0770123456"


# ---------------------------------------------------------------------------
# Структурная валидация: token
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("bad_token", [123, None, 1.5, True, ["INN_X"]])
def test_token_rejects_non_str(bad_token: object) -> None:
    with pytest.raises(ValueError):
        IdentifierMappingEntry(
            token=bad_token,  # type: ignore[arg-type]
            identifier_value="7701234567",
            identifier_type=IdentifierType.INN,
        )


def test_token_rejects_empty_string() -> None:
    with pytest.raises(ValueError):
        IdentifierMappingEntry(
            token="", identifier_value="7701234567", identifier_type=IdentifierType.INN
        )


@pytest.mark.parametrize("whitespace_token", [" ", "   ", "\t", "\n"])
def test_token_rejects_whitespace_only(whitespace_token: str) -> None:
    with pytest.raises(ValueError):
        IdentifierMappingEntry(
            token=whitespace_token,
            identifier_value="7701234567",
            identifier_type=IdentifierType.INN,
        )


# ---------------------------------------------------------------------------
# Структурная валидация: identifier_value
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("bad_value", [7701234567, None, 1.5, True, ["7701234567"]])
def test_identifier_value_rejects_non_str(bad_value: object) -> None:
    with pytest.raises(ValueError):
        IdentifierMappingEntry(
            token="INN_7GQ2MX9KA",
            identifier_value=bad_value,  # type: ignore[arg-type]
            identifier_type=IdentifierType.INN,
        )


def test_identifier_value_rejects_empty_string() -> None:
    with pytest.raises(ValueError):
        IdentifierMappingEntry(
            token="INN_7GQ2MX9KA", identifier_value="", identifier_type=IdentifierType.INN
        )


@pytest.mark.parametrize("whitespace_value", [" ", "   ", "\t", "\n"])
def test_identifier_value_rejects_whitespace_only(whitespace_value: str) -> None:
    with pytest.raises(ValueError):
        IdentifierMappingEntry(
            token="INN_7GQ2MX9KA",
            identifier_value=whitespace_value,
            identifier_type=IdentifierType.INN,
        )


# ---------------------------------------------------------------------------
# Структурная валидация: identifier_type
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("bad_type", ["inn", 1, None, object()])
def test_identifier_type_field_rejects_wrong_type(bad_type: object) -> None:
    with pytest.raises(ValueError):
        IdentifierMappingEntry(
            token="INN_7GQ2MX9KA",
            identifier_value="7701234567",
            identifier_type=bad_type,  # type: ignore[arg-type]
        )


# ---------------------------------------------------------------------------
# Структурный тест полей dataclass — фиксирует отсутствие лишних полей
# ---------------------------------------------------------------------------


def test_identifier_mapping_entry_has_exactly_expected_fields() -> None:
    field_names = {f.name for f in dataclasses.fields(IdentifierMappingEntry)}
    assert field_names == {"token", "identifier_value", "identifier_type"}
    # Явно фиксируем отсутствие полей, которые сознательно НЕ добавлялись.
    for forbidden in ("origin_python_type", "parent_alias", "sheet", "row", "column", "metadata"):
        assert forbidden not in field_names
