"""
Тесты app.anonymizer (Stage 7A — Flat Anonymizer).

FlatTable-фикстуры строятся напрямую через CellRecord/FlatTable (без
openpyxl) — Stage 7A не работает с Excel-форматом вообще.
"""

from __future__ import annotations

import datetime

import pytest

import app.anonymizer as anonymizer_module
from app.anonymizer import (
    AnonymizationError,
    FormulaPseudonymizationError,
    InvalidPseudonymizationValueError,
    InvalidRuleConfigurationError,
    UnsupportedPseudonymizationTargetError,
    anonymize_flat_table,
)
from app.excel.models import CellRecord, FlatTable
from app.mapping.base import MappingConflictError
from app.mapping.memory import InMemoryMappingStore
from app.models.entities import EntityType, MappingEntry
from app.models.rules import Action, FieldRule, FieldType

# ---------------------------------------------------------------------------
# Помощники построения фикстур
# ---------------------------------------------------------------------------


def _table(sheet_name: str, headers: list[object], rows: list[list[object]]) -> FlatTable:
    header_row = tuple(
        CellRecord(row=1, column=i + 1, value=h) for i, h in enumerate(headers)
    )
    data_rows = tuple(
        tuple(CellRecord(row=r_idx + 2, column=c_idx + 1, value=v) for c_idx, v in enumerate(row))
        for r_idx, row in enumerate(rows)
    )
    return FlatTable(sheet_name=sheet_name, header_row=header_row, rows=data_rows)


def _rule(column_name: str, field_type: FieldType, action: Action) -> FieldRule:
    return FieldRule(column_name=column_name, field_type=field_type, action=action)


def _company_rule(column_name: str = "Компания") -> FieldRule:
    return _rule(column_name, FieldType.COMPANY, Action.PSEUDONYMIZE)


def _person_rule(column_name: str = "ФИО") -> FieldRule:
    return _rule(column_name, FieldType.PERSON, Action.PSEUDONYMIZE)


# ---------------------------------------------------------------------------
# RULES (1-9)
# ---------------------------------------------------------------------------


def test_all_columns_covered_succeeds() -> None:
    table = _table("S", ["A", "B"], [["x", "y"]])
    rules = {
        1: _rule("A", FieldType.UNKNOWN, Action.KEEP),
        2: _rule("B", FieldType.UNKNOWN, Action.KEEP),
    }
    result = anonymize_flat_table(table, rules, InMemoryMappingStore())
    assert result.rows == table.rows


def test_missing_rule_raises() -> None:
    table = _table("S", ["A", "B"], [["x", "y"]])
    rules = {1: _rule("A", FieldType.UNKNOWN, Action.KEEP)}
    with pytest.raises(InvalidRuleConfigurationError):
        anonymize_flat_table(table, rules, InMemoryMappingStore())


def test_rule_for_nonexistent_column_raises() -> None:
    table = _table("S", ["A"], [["x"]])
    rules = {
        1: _rule("A", FieldType.UNKNOWN, Action.KEEP),
        2: _rule("B", FieldType.UNKNOWN, Action.KEEP),
    }
    with pytest.raises(InvalidRuleConfigurationError):
        anonymize_flat_table(table, rules, InMemoryMappingStore())


def test_index_zero_raises() -> None:
    table = _table("S", ["A"], [["x"]])
    rules = {0: _rule("A", FieldType.UNKNOWN, Action.KEEP), 1: _rule("A", FieldType.UNKNOWN, Action.KEEP)}
    with pytest.raises(InvalidRuleConfigurationError):
        anonymize_flat_table(table, rules, InMemoryMappingStore())


def test_negative_index_raises() -> None:
    table = _table("S", ["A"], [["x"]])
    rules = {-1: _rule("A", FieldType.UNKNOWN, Action.KEEP), 1: _rule("A", FieldType.UNKNOWN, Action.KEEP)}
    with pytest.raises(InvalidRuleConfigurationError):
        anonymize_flat_table(table, rules, InMemoryMappingStore())


def test_bool_index_raises() -> None:
    table = _table("S", ["A"], [["x"]])
    rules = {True: _rule("A", FieldType.UNKNOWN, Action.KEEP)}
    with pytest.raises(InvalidRuleConfigurationError):
        anonymize_flat_table(table, rules, InMemoryMappingStore())  # type: ignore[dict-item]


def test_non_int_index_raises() -> None:
    table = _table("S", ["A"], [["x"]])
    rules = {"1": _rule("A", FieldType.UNKNOWN, Action.KEEP)}
    with pytest.raises(InvalidRuleConfigurationError):
        anonymize_flat_table(table, rules, InMemoryMappingStore())  # type: ignore[dict-item]


def test_duplicate_headers_different_rules_applied_by_index() -> None:
    table = _table("S", ["ИНН", "ИНН"], [["7707083893", "500100732259"]])
    rules = {
        1: _rule("ИНН", FieldType.INN, Action.KEEP),
        2: _rule("ИНН", FieldType.INN, Action.REMOVE),
    }
    result = anonymize_flat_table(table, rules, InMemoryMappingStore())
    assert result.rows[0][0].value == "7707083893"
    assert result.rows[0][1].value is None


def test_field_rule_column_name_is_not_used_as_binding_key() -> None:
    """column_name — метаданные; связывание идёт строго по индексу."""
    table = _table("S", ["Компания"], [["ООО Ромашка"]])
    # column_name сознательно не совпадает с заголовком колонки.
    rules = {1: _rule("СОВСЕМ ДРУГОЕ ИМЯ", FieldType.COMPANY, Action.PSEUDONYMIZE)}
    result = anonymize_flat_table(table, rules, InMemoryMappingStore())
    assert result.rows[0][0].value.startswith("C_")


# ---------------------------------------------------------------------------
# KEEP (10-15)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "value",
    [
        "текст",
        42,
        3.14,
        True,
        None,
        "=SUM(A2:A5)",
    ],
)
def test_keep_preserves_value_and_coordinates(value: object) -> None:
    table = _table("S", ["X"], [[value]])
    rules = {1: _rule("X", FieldType.UNKNOWN, Action.KEEP)}
    result = anonymize_flat_table(table, rules, InMemoryMappingStore())
    cell = result.rows[0][0]
    assert cell.value is value or cell.value == value
    assert cell.row == 2
    assert cell.column == 1


# ---------------------------------------------------------------------------
# REMOVE (16-20)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("value", ["текст", 42, 3.14, "=SUM(A2:A5)", None])
def test_remove_sets_value_to_none_and_preserves_coordinates(value: object) -> None:
    table = _table("S", ["X"], [[value]])
    rules = {1: _rule("X", FieldType.UNKNOWN, Action.REMOVE)}
    result = anonymize_flat_table(table, rules, InMemoryMappingStore())
    cell = result.rows[0][0]
    assert cell.value is None
    assert cell.row == 2
    assert cell.column == 1


# ---------------------------------------------------------------------------
# PSEUDONYMIZE COMPANY (21-26)
# ---------------------------------------------------------------------------


def test_pseudonymize_company_produces_c_prefixed_alias() -> None:
    table = _table("S", ["Компания"], [["ООО Ромашка"]])
    rules = {1: _company_rule()}
    result = anonymize_flat_table(table, rules, InMemoryMappingStore())
    assert result.rows[0][0].value.startswith("C_")


def test_repeated_exact_company_reuses_same_alias() -> None:
    table = _table("S", ["Компания"], [["ООО Ромашка"], ["ООО Ромашка"], ["ООО Ромашка"]])
    rules = {1: _company_rule()}
    result = anonymize_flat_table(table, rules, InMemoryMappingStore())
    aliases = {row[0].value for row in result.rows}
    assert len(aliases) == 1


def test_different_companies_get_different_aliases() -> None:
    table = _table("S", ["Компания"], [["ООО Ромашка"], ["ООО Лютик"]])
    rules = {1: _company_rule()}
    result = anonymize_flat_table(table, rules, InMemoryMappingStore())
    assert result.rows[0][0].value != result.rows[1][0].value


def test_company_already_in_store_reuses_existing_alias() -> None:
    store = InMemoryMappingStore()
    store.add(
        MappingEntry(
            alias="C_PRESEEDED1",
            real_value="ООО Ромашка",
            entity_type=EntityType.COMPANY,
            parent_alias=None,
        )
    )
    table = _table("S", ["Компания"], [["ООО Ромашка"]])
    rules = {1: _company_rule()}
    result = anonymize_flat_table(table, rules, store)
    assert result.rows[0][0].value == "C_PRESEEDED1"
    assert len(store.entries()) == 1  # новая запись не создана


def test_mapping_entry_holds_exact_original_real_value() -> None:
    store = InMemoryMappingStore()
    table = _table("S", ["Компания"], [["ООО Ромашка"]])
    rules = {1: _company_rule()}
    result = anonymize_flat_table(table, rules, store)
    alias = result.rows[0][0].value
    entry = store.get_by_alias(alias)
    assert entry.real_value == "ООО Ромашка"


def test_company_mapping_entry_has_none_parent_alias() -> None:
    store = InMemoryMappingStore()
    table = _table("S", ["Компания"], [["ООО Ромашка"]])
    rules = {1: _company_rule()}
    result = anonymize_flat_table(table, rules, store)
    entry = store.get_by_alias(result.rows[0][0].value)
    assert entry.parent_alias is None


# ---------------------------------------------------------------------------
# PSEUDONYMIZE PERSON (27-29)
# ---------------------------------------------------------------------------


def test_pseudonymize_person_produces_p_prefixed_alias() -> None:
    table = _table("S", ["ФИО"], [["Иванов Иван Иванович"]])
    rules = {1: _person_rule()}
    result = anonymize_flat_table(table, rules, InMemoryMappingStore())
    assert result.rows[0][0].value.startswith("P_")


def test_repeated_exact_person_reuses_same_alias() -> None:
    table = _table("S", ["ФИО"], [["Иванов Иван Иванович"], ["Иванов Иван Иванович"]])
    rules = {1: _person_rule()}
    result = anonymize_flat_table(table, rules, InMemoryMappingStore())
    assert result.rows[0][0].value == result.rows[1][0].value


def test_same_text_company_vs_person_get_different_aliases() -> None:
    table = _table("S", ["Компания", "ФИО"], [["Ромашка", "Ромашка"]])
    rules = {1: _company_rule(), 2: _person_rule()}
    result = anonymize_flat_table(table, rules, InMemoryMappingStore())
    company_alias, person_alias = result.rows[0][0].value, result.rows[0][1].value
    assert company_alias != person_alias
    assert company_alias.startswith("C_")
    assert person_alias.startswith("P_")


# ---------------------------------------------------------------------------
# EXACT VALUE (30-32) — никакой нормализации
# ---------------------------------------------------------------------------


def test_double_internal_space_is_a_different_identity() -> None:
    table = _table("S", ["Компания"], [["ООО Ромашка"], ["ООО  Ромашка"]])
    rules = {1: _company_rule()}
    result = anonymize_flat_table(table, rules, InMemoryMappingStore())
    assert result.rows[0][0].value != result.rows[1][0].value


def test_outer_whitespace_is_a_different_identity() -> None:
    table = _table("S", ["Компания"], [["ООО Ромашка"], [" ООО Ромашка "]])
    rules = {1: _company_rule()}
    result = anonymize_flat_table(table, rules, InMemoryMappingStore())
    assert result.rows[0][0].value != result.rows[1][0].value


def test_case_variants_are_not_normalized() -> None:
    table = _table("S", ["Компания"], [["Ромашка"], ["ромашка"]])
    rules = {1: _company_rule()}
    result = anonymize_flat_table(table, rules, InMemoryMappingStore())
    assert result.rows[0][0].value != result.rows[1][0].value


# ---------------------------------------------------------------------------
# EMPTY (33-35)
# ---------------------------------------------------------------------------


def test_none_under_pseudonymize_unchanged_no_mapping() -> None:
    store = InMemoryMappingStore()
    table = _table("S", ["Компания"], [[None]])
    rules = {1: _company_rule()}
    result = anonymize_flat_table(table, rules, store)
    assert result.rows[0][0].value is None
    assert store.entries() == ()


def test_empty_string_under_pseudonymize_unchanged_no_mapping() -> None:
    store = InMemoryMappingStore()
    table = _table("S", ["Компания"], [[""]])
    rules = {1: _company_rule()}
    result = anonymize_flat_table(table, rules, store)
    assert result.rows[0][0].value == ""
    assert store.entries() == ()


def test_whitespace_only_string_under_pseudonymize_unchanged_exact_no_mapping() -> None:
    store = InMemoryMappingStore()
    table = _table("S", ["Компания"], [["   "]])
    rules = {1: _company_rule()}
    result = anonymize_flat_table(table, rules, store)
    assert result.rows[0][0].value == "   "
    assert store.entries() == ()


# ---------------------------------------------------------------------------
# INVALID PSEUDONYMIZATION VALUES (36-41)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("value", [42, 3.14, True])
def test_non_str_company_value_raises_invalid_value_error(value: object) -> None:
    store = InMemoryMappingStore()
    table = _table("S", ["Компания"], [[value]])
    rules = {1: _company_rule()}
    with pytest.raises(InvalidPseudonymizationValueError):
        anonymize_flat_table(table, rules, store)
    assert store.entries() == ()


def test_datetime_person_value_raises_invalid_value_error() -> None:
    store = InMemoryMappingStore()
    table = _table("S", ["ФИО"], [[datetime.datetime(2024, 5, 1)]])
    rules = {1: _person_rule()}
    with pytest.raises(InvalidPseudonymizationValueError):
        anonymize_flat_table(table, rules, store)
    assert store.entries() == ()


def test_formula_company_value_raises_formula_error() -> None:
    store = InMemoryMappingStore()
    table = _table("S", ["Компания"], [["=CONCAT(A1,B1)"]])
    rules = {1: _company_rule()}
    with pytest.raises(FormulaPseudonymizationError):
        anonymize_flat_table(table, rules, store)
    assert store.entries() == ()


def test_formula_person_value_raises_formula_error() -> None:
    store = InMemoryMappingStore()
    table = _table("S", ["ФИО"], [["=CONCAT(A1,B1)"]])
    rules = {1: _person_rule()}
    with pytest.raises(FormulaPseudonymizationError):
        anonymize_flat_table(table, rules, store)
    assert store.entries() == ()


def test_only_failing_cell_creates_no_entry_earlier_cells_stay() -> None:
    """
    Атомарности на Stage 7A нет: успешно обработанные ранее ячейки
    остаются в store, но сама сбойная ячейка не создаёт запись.
    """
    store = InMemoryMappingStore()
    table = _table(
        "S",
        ["Компания"],
        [["ООО Ромашка"], [42]],  # первая строка валидна, вторая — нет
    )
    rules = {1: _company_rule()}
    with pytest.raises(InvalidPseudonymizationValueError):
        anonymize_flat_table(table, rules, store)
    assert len(store.entries()) == 1
    assert store.entries()[0].real_value == "ООО Ромашка"


# ---------------------------------------------------------------------------
# UNSUPPORTED TARGETS (42-51)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "field_type",
    [
        FieldType.BRANCH,
        FieldType.DEPARTMENT,
        FieldType.INN,
        FieldType.KPP,
        FieldType.OGRN,
        FieldType.PHONE,
        FieldType.EMAIL,
        FieldType.ADDRESS,
        FieldType.FINANCIAL,
        FieldType.UNKNOWN,
    ],
)
def test_unsupported_pseudonymization_targets_raise(field_type: FieldType) -> None:
    table = _table("S", ["X"], [["value"]])
    rules = {1: _rule("X", field_type, Action.PSEUDONYMIZE)}
    with pytest.raises(UnsupportedPseudonymizationTargetError):
        anonymize_flat_table(table, rules, InMemoryMappingStore())


def test_unsupported_target_is_rejected_before_any_row_processing() -> None:
    """
    Конфигурация проверяется целиком до обработки строк — ошибка должна
    сработать, даже если "плохая" колонка никогда не встречает данных,
    отличных от KEEP-совместимых.
    """
    store = InMemoryMappingStore()
    table = _table("S", ["Компания", "Телефон"], [["ООО Ромашка", "+7900..."]])
    rules = {
        1: _company_rule(),
        2: _rule("Телефон", FieldType.PHONE, Action.PSEUDONYMIZE),
    }
    with pytest.raises(UnsupportedPseudonymizationTargetError):
        anonymize_flat_table(table, rules, store)
    assert store.entries() == ()  # до строк дело не дошло


# ---------------------------------------------------------------------------
# IMMUTABILITY / STRUCTURE (52-58)
# ---------------------------------------------------------------------------


def test_input_flat_table_is_not_mutated() -> None:
    table = _table("S", ["Компания"], [["ООО Ромашка"], ["ООО Лютик"]])
    original_rows = table.rows
    original_header = table.header_row
    rules = {1: _company_rule()}
    anonymize_flat_table(table, rules, InMemoryMappingStore())
    assert table.rows == original_rows
    assert table.header_row == original_header


def test_result_is_a_new_flat_table_object() -> None:
    table = _table("S", ["Компания"], [["ООО Ромашка"]])
    rules = {1: _company_rule()}
    result = anonymize_flat_table(table, rules, InMemoryMappingStore())
    assert result is not table


def test_sheet_name_preserved() -> None:
    table = _table("МойЛист", ["A"], [["x"]])
    rules = {1: _rule("A", FieldType.UNKNOWN, Action.KEEP)}
    result = anonymize_flat_table(table, rules, InMemoryMappingStore())
    assert result.sheet_name == "МойЛист"


def test_header_row_unchanged() -> None:
    table = _table("S", ["Компания"], [["ООО Ромашка"]])
    rules = {1: _company_rule()}
    result = anonymize_flat_table(table, rules, InMemoryMappingStore())
    assert result.header_row == table.header_row


def test_row_count_preserved() -> None:
    table = _table("S", ["A"], [["1"], ["2"], ["3"]])
    rules = {1: _rule("A", FieldType.UNKNOWN, Action.KEEP)}
    result = anonymize_flat_table(table, rules, InMemoryMappingStore())
    assert len(result.rows) == len(table.rows)


def test_column_count_and_order_preserved() -> None:
    table = _table("S", ["A", "B", "C"], [["1", "2", "3"]])
    rules = {
        1: _rule("A", FieldType.UNKNOWN, Action.KEEP),
        2: _rule("B", FieldType.UNKNOWN, Action.REMOVE),
        3: _rule("C", FieldType.UNKNOWN, Action.KEEP),
    }
    result = anonymize_flat_table(table, rules, InMemoryMappingStore())
    row = result.rows[0]
    assert len(row) == 3
    assert [c.column for c in row] == [1, 2, 3]
    assert row[0].value == "1"
    assert row[1].value is None
    assert row[2].value == "3"


def test_coordinates_preserved_across_all_actions() -> None:
    table = _table("S", ["Компания", "Телефон"], [["ООО Ромашка", "123"]])
    rules = {1: _company_rule(), 2: _rule("Телефон", FieldType.PHONE, Action.REMOVE)}
    result = anonymize_flat_table(table, rules, InMemoryMappingStore())
    for original, produced in zip(table.rows[0], result.rows[0]):
        assert original.row == produced.row
        assert original.column == produced.column


# ---------------------------------------------------------------------------
# STORE / ALIAS (59-62)
# ---------------------------------------------------------------------------


def test_mapping_conflict_error_propagates(monkeypatch: pytest.MonkeyPatch) -> None:
    store = InMemoryMappingStore()
    store.add(
        MappingEntry(
            alias="C_FIXEDALIAS",
            real_value="Другая компания",
            entity_type=EntityType.COMPANY,
            parent_alias=None,
        )
    )

    def fake_generate_alias(entity_type, existing_aliases):
        return "C_FIXEDALIAS"

    monkeypatch.setattr(anonymizer_module, "generate_alias", fake_generate_alias)

    table = _table("S", ["Компания"], [["ООО Ромашка"]])
    rules = {1: _company_rule()}

    with pytest.raises(MappingConflictError):
        anonymize_flat_table(table, rules, store)


def test_all_aliases_called_exactly_once(monkeypatch: pytest.MonkeyPatch) -> None:
    store = InMemoryMappingStore()
    call_count = {"n": 0}
    original_all_aliases = store.all_aliases

    def counting_all_aliases():
        call_count["n"] += 1
        return original_all_aliases()

    monkeypatch.setattr(store, "all_aliases", counting_all_aliases)

    table = _table(
        "S",
        ["Компания"],
        [["A"], ["B"], ["A"], ["C"], ["B"], ["D"]],
    )
    rules = {1: _company_rule()}
    anonymize_flat_table(table, rules, store)

    assert call_count["n"] == 1


def test_existing_aliases_set_grows_locally_across_new_entities(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    Белый ящик: подтверждает, что рабочее множество занятых alias
    пополняется локально между генерациями внутри одного вызова, а не
    перезапрашивается у store на каждую новую сущность.
    """
    store = InMemoryMappingStore()
    seen_snapshots: list[set[str]] = []
    minted = iter(["C_ONE", "C_TWO", "C_THREE"])

    def fake_generate_alias(entity_type, existing_aliases):
        seen_snapshots.append(set(existing_aliases))
        return next(minted)

    monkeypatch.setattr(anonymizer_module, "generate_alias", fake_generate_alias)

    table = _table("S", ["Компания"], [["A"], ["B"], ["C"]])
    rules = {1: _company_rule()}
    anonymize_flat_table(table, rules, store)

    assert seen_snapshots[0] == set()
    assert seen_snapshots[1] == {"C_ONE"}
    assert seen_snapshots[2] == {"C_ONE", "C_TWO"}


def test_second_run_with_same_store_reuses_aliases() -> None:
    store = InMemoryMappingStore()
    rules = {1: _company_rule()}

    table1 = _table("S", ["Компания"], [["ООО Ромашка"]])
    result1 = anonymize_flat_table(table1, rules, store)
    alias1 = result1.rows[0][0].value

    table2 = _table("S", ["Компания"], [["ООО Ромашка"], ["ООО Лютик"]])
    result2 = anonymize_flat_table(table2, rules, store)

    assert result2.rows[0][0].value == alias1
    assert result2.rows[1][0].value != alias1
