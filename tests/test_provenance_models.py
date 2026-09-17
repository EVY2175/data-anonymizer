"""
Тесты app.models.provenance: IdentifierRepresentation, IdentifierCellProvenance.

Stage 7C.1 — только модель данных. Здесь НЕ тестируется: lookup/store
semantics (Stage 7C.2), encrypted persistence (Stage 7C.3), anonymizer
integration (Stage 7C.4), job_id (store-level, Stage 7C.2), парсинг
формата token (Stage 7B.2).
"""

from __future__ import annotations

import dataclasses

import pytest

from app.models.provenance import IdentifierCellProvenance, IdentifierRepresentation

# ---------------------------------------------------------------------------
# IdentifierRepresentation
# ---------------------------------------------------------------------------


def test_identifier_representation_has_exactly_string_and_integer() -> None:
    assert {member.name for member in IdentifierRepresentation} == {"STRING", "INTEGER"}


def test_identifier_representation_exact_enum_values() -> None:
    assert IdentifierRepresentation.STRING.value == "string"
    assert IdentifierRepresentation.INTEGER.value == "integer"


# ---------------------------------------------------------------------------
# IdentifierCellProvenance — корректное создание
# ---------------------------------------------------------------------------


def test_valid_string_provenance_entry() -> None:
    entry = IdentifierCellProvenance(
        sheet_name="Sheet1",
        row=2,
        column=3,
        token="INN_AAAAAAAAA",
        representation=IdentifierRepresentation.STRING,
    )
    assert entry.sheet_name == "Sheet1"
    assert entry.row == 2
    assert entry.column == 3
    assert entry.token == "INN_AAAAAAAAA"
    assert entry.representation is IdentifierRepresentation.STRING


def test_valid_integer_provenance_entry() -> None:
    entry = IdentifierCellProvenance(
        sheet_name="Sheet1",
        row=5,
        column=1,
        token="KPP_BBBBBBBBB",
        representation=IdentifierRepresentation.INTEGER,
    )
    assert entry.sheet_name == "Sheet1"
    assert entry.row == 5
    assert entry.column == 1
    assert entry.token == "KPP_BBBBBBBBB"
    assert entry.representation is IdentifierRepresentation.INTEGER


# ---------------------------------------------------------------------------
# Immutability / equality / hashability
# ---------------------------------------------------------------------------


def test_identifier_cell_provenance_is_frozen() -> None:
    entry = IdentifierCellProvenance(
        sheet_name="Sheet1",
        row=1,
        column=1,
        token="INN_AAAAAAAAA",
        representation=IdentifierRepresentation.STRING,
    )
    with pytest.raises(dataclasses.FrozenInstanceError):
        entry.sheet_name = "changed"  # type: ignore[misc]
    with pytest.raises(dataclasses.FrozenInstanceError):
        entry.row = 2  # type: ignore[misc]
    with pytest.raises(dataclasses.FrozenInstanceError):
        entry.column = 2  # type: ignore[misc]
    with pytest.raises(dataclasses.FrozenInstanceError):
        entry.token = "changed"  # type: ignore[misc]
    with pytest.raises(dataclasses.FrozenInstanceError):
        entry.representation = IdentifierRepresentation.INTEGER  # type: ignore[misc]


def test_identifier_cell_provenance_equality_is_by_value() -> None:
    entry_a = IdentifierCellProvenance(
        sheet_name="Sheet1",
        row=1,
        column=1,
        token="INN_AAAAAAAAA",
        representation=IdentifierRepresentation.STRING,
    )
    entry_b = IdentifierCellProvenance(
        sheet_name="Sheet1",
        row=1,
        column=1,
        token="INN_AAAAAAAAA",
        representation=IdentifierRepresentation.STRING,
    )
    entry_c = IdentifierCellProvenance(
        sheet_name="Sheet1",
        row=1,
        column=1,
        token="INN_DIFFERENT",
        representation=IdentifierRepresentation.STRING,
    )
    assert entry_a == entry_b
    assert entry_a is not entry_b
    assert entry_a != entry_c


def test_identifier_cell_provenance_is_hashable() -> None:
    entry_a = IdentifierCellProvenance(
        sheet_name="Sheet1",
        row=1,
        column=1,
        token="INN_AAAAAAAAA",
        representation=IdentifierRepresentation.STRING,
    )
    entry_b = IdentifierCellProvenance(
        sheet_name="Sheet1",
        row=1,
        column=1,
        token="INN_AAAAAAAAA",
        representation=IdentifierRepresentation.STRING,
    )
    assert hash(entry_a) == hash(entry_b)
    assert {entry_a, entry_b} == {entry_a}


# ---------------------------------------------------------------------------
# EXACT value contract: sheet_name / token без нормализации
# ---------------------------------------------------------------------------


def test_sheet_name_outer_whitespace_is_preserved_exact() -> None:
    entry = IdentifierCellProvenance(
        sheet_name=" Sheet 1 ",
        row=1,
        column=1,
        token="INN_AAAAAAAAA",
        representation=IdentifierRepresentation.STRING,
    )
    assert entry.sheet_name == " Sheet 1 "


def test_token_outer_whitespace_is_preserved_exact() -> None:
    entry = IdentifierCellProvenance(
        sheet_name="Sheet1",
        row=1,
        column=1,
        token=" KPP_BBBBBBBBB ",
        representation=IdentifierRepresentation.STRING,
    )
    assert entry.token == " KPP_BBBBBBBBB "


# ---------------------------------------------------------------------------
# Структурная валидация: sheet_name
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("bad_sheet_name", [None, 123])
def test_sheet_name_rejects_non_str(bad_sheet_name: object) -> None:
    with pytest.raises(ValueError):
        IdentifierCellProvenance(
            sheet_name=bad_sheet_name,  # type: ignore[arg-type]
            row=1,
            column=1,
            token="INN_AAAAAAAAA",
            representation=IdentifierRepresentation.STRING,
        )


def test_sheet_name_rejects_empty_string() -> None:
    with pytest.raises(ValueError):
        IdentifierCellProvenance(
            sheet_name="",
            row=1,
            column=1,
            token="INN_AAAAAAAAA",
            representation=IdentifierRepresentation.STRING,
        )


def test_sheet_name_rejects_whitespace_only() -> None:
    with pytest.raises(ValueError):
        IdentifierCellProvenance(
            sheet_name="   ",
            row=1,
            column=1,
            token="INN_AAAAAAAAA",
            representation=IdentifierRepresentation.STRING,
        )


# ---------------------------------------------------------------------------
# Структурная валидация: row
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("bad_row", [None, 0, -1, 1.0, "1", True, False])
def test_row_rejects_invalid(bad_row: object) -> None:
    with pytest.raises(ValueError):
        IdentifierCellProvenance(
            sheet_name="Sheet1",
            row=bad_row,  # type: ignore[arg-type]
            column=1,
            token="INN_AAAAAAAAA",
            representation=IdentifierRepresentation.STRING,
        )


@pytest.mark.parametrize("good_row", [1, 2, 100000])
def test_row_accepts_valid(good_row: int) -> None:
    entry = IdentifierCellProvenance(
        sheet_name="Sheet1",
        row=good_row,
        column=1,
        token="INN_AAAAAAAAA",
        representation=IdentifierRepresentation.STRING,
    )
    assert entry.row == good_row


# ---------------------------------------------------------------------------
# Структурная валидация: column
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("bad_column", [None, 0, -1, 1.0, "1", True, False])
def test_column_rejects_invalid(bad_column: object) -> None:
    with pytest.raises(ValueError):
        IdentifierCellProvenance(
            sheet_name="Sheet1",
            row=1,
            column=bad_column,  # type: ignore[arg-type]
            token="INN_AAAAAAAAA",
            representation=IdentifierRepresentation.STRING,
        )


@pytest.mark.parametrize("good_column", [1, 2, 100000])
def test_column_accepts_valid(good_column: int) -> None:
    entry = IdentifierCellProvenance(
        sheet_name="Sheet1",
        row=1,
        column=good_column,
        token="INN_AAAAAAAAA",
        representation=IdentifierRepresentation.STRING,
    )
    assert entry.column == good_column


# ---------------------------------------------------------------------------
# Структурная валидация: token
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("bad_token", [None, 123])
def test_token_rejects_non_str(bad_token: object) -> None:
    with pytest.raises(ValueError):
        IdentifierCellProvenance(
            sheet_name="Sheet1",
            row=1,
            column=1,
            token=bad_token,  # type: ignore[arg-type]
            representation=IdentifierRepresentation.STRING,
        )


def test_token_rejects_empty_string() -> None:
    with pytest.raises(ValueError):
        IdentifierCellProvenance(
            sheet_name="Sheet1",
            row=1,
            column=1,
            token="",
            representation=IdentifierRepresentation.STRING,
        )


def test_token_rejects_whitespace_only() -> None:
    with pytest.raises(ValueError):
        IdentifierCellProvenance(
            sheet_name="Sheet1",
            row=1,
            column=1,
            token="   ",
            representation=IdentifierRepresentation.STRING,
        )


@pytest.mark.parametrize("good_token", ["INN_AAAAAAAAA", " KPP_BBBBBBBBB "])
def test_token_accepts_valid_exact(good_token: str) -> None:
    entry = IdentifierCellProvenance(
        sheet_name="Sheet1",
        row=1,
        column=1,
        token=good_token,
        representation=IdentifierRepresentation.STRING,
    )
    assert entry.token == good_token


# ---------------------------------------------------------------------------
# Структурная валидация: representation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("bad_representation", [None, "string", "integer", 1, True])
def test_representation_rejects_invalid(bad_representation: object) -> None:
    with pytest.raises(ValueError):
        IdentifierCellProvenance(
            sheet_name="Sheet1",
            row=1,
            column=1,
            token="INN_AAAAAAAAA",
            representation=bad_representation,  # type: ignore[arg-type]
        )


@pytest.mark.parametrize(
    "good_representation", [IdentifierRepresentation.STRING, IdentifierRepresentation.INTEGER]
)
def test_representation_accepts_valid(good_representation: IdentifierRepresentation) -> None:
    entry = IdentifierCellProvenance(
        sheet_name="Sheet1",
        row=1,
        column=1,
        token="INN_AAAAAAAAA",
        representation=good_representation,
    )
    assert entry.representation is good_representation


# ---------------------------------------------------------------------------
# Структурный тест полей dataclass — фиксирует отсутствие лишних полей
# ---------------------------------------------------------------------------


def test_identifier_cell_provenance_has_exactly_expected_fields() -> None:
    field_names = {f.name for f in dataclasses.fields(IdentifierCellProvenance)}
    assert field_names == {"sheet_name", "row", "column", "token", "representation"}
    # Явно фиксируем отсутствие полей, которые сознательно НЕ добавлялись
    # (см. docstring app/models/provenance.py и Stage 7C design review).
    for forbidden in (
        "identifier_value",
        "identifier_type",
        "original_value",
        "original_type",
        "workbook_id",
        "job_id",
        "filename",
        "path",
        "metadata",
        "timestamp",
        "hash",
        "uuid",
    ):
        assert forbidden not in field_names
