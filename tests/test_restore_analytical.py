"""
Тесты Derived Analytical Workbook Restore Core (Stage 9C) —
app.restore.analytical.

Все workbook строятся напрямую через openpyxl.Workbook() в памяти, без
файлового I/O — core.py оперирует уже загруженными объектами. Все
данные synthetic; ИНН/КПП/ОГРН — валидные по чек-сумме, но синтетические.
"""

from __future__ import annotations

import openpyxl
import pytest

from app.mapping.identifier_memory import InMemoryIdentifierMappingStore
from app.mapping.memory import InMemoryMappingStore
from app.models.entities import EntityType, MappingEntry
from app.models.identifiers import IdentifierMappingEntry, IdentifierType
from app.restore.analytical import (
    AmbiguousRestoreError,
    AnalyticalCellReplacement,
    apply_replacement_plan,
    build_replacement_plan,
)

VALID_INN = "7707083893"
VALID_KPP = "770701001"
VALID_OGRN = "1027700132195"


def _make_workbook(sheet_data: dict[str, list[list[object]]]) -> openpyxl.Workbook:
    wb = openpyxl.Workbook()
    first = True
    for sheet_name, rows in sheet_data.items():
        ws = wb.active if first else wb.create_sheet(sheet_name)
        ws.title = sheet_name
        first = False
        for row_idx, row in enumerate(rows, start=1):
            for col_idx, value in enumerate(row, start=1):
                ws.cell(row=row_idx, column=col_idx, value=value)
    return wb


def _mapping_store_with_alias(alias: str, real_value: str) -> InMemoryMappingStore:
    store = InMemoryMappingStore()
    store.add(MappingEntry(alias=alias, real_value=real_value, entity_type=EntityType.COMPANY))
    return store


def _identifier_store_with_token(token: str, identifier_value: str, identifier_type: IdentifierType) -> InMemoryIdentifierMappingStore:
    store = InMemoryIdentifierMappingStore()
    store.add(IdentifierMappingEntry(token=token, identifier_value=identifier_value, identifier_type=identifier_type))
    return store


# ---------------------------------------------------------------------------
# Entity exact hit
# ---------------------------------------------------------------------------


def test_entity_exact_hit() -> None:
    wb = _make_workbook({"Data": [["Client"], ["C_ALIAS1"]]})
    mapping_store = _mapping_store_with_alias("C_ALIAS1", "ООО Ромашка")

    entity, identifier = build_replacement_plan(wb, mapping_store, None)

    assert entity == (AnalyticalCellReplacement(worksheet_index=1, row=2, column=1, value="ООО Ромашка"),)
    assert identifier == ()


# ---------------------------------------------------------------------------
# Identifier exact hit — INN/KPP/OGRN
# ---------------------------------------------------------------------------


def test_identifier_exact_hit_inn() -> None:
    wb = _make_workbook({"Data": [["INN"], ["INN_TOKEN1"]]})
    mapping_store = InMemoryMappingStore()
    identifier_store = _identifier_store_with_token("INN_TOKEN1", VALID_INN, IdentifierType.INN)

    entity, identifier = build_replacement_plan(wb, mapping_store, identifier_store)

    assert entity == ()
    assert identifier == (AnalyticalCellReplacement(worksheet_index=1, row=2, column=1, value=VALID_INN),)
    assert isinstance(identifier[0].value, str)


def test_identifier_exact_hit_kpp() -> None:
    wb = _make_workbook({"Data": [["KPP"], ["KPP_TOKEN1"]]})
    mapping_store = InMemoryMappingStore()
    identifier_store = _identifier_store_with_token("KPP_TOKEN1", VALID_KPP, IdentifierType.KPP)

    entity, identifier = build_replacement_plan(wb, mapping_store, identifier_store)

    assert identifier == (AnalyticalCellReplacement(worksheet_index=1, row=2, column=1, value=VALID_KPP),)


def test_identifier_exact_hit_ogrn() -> None:
    wb = _make_workbook({"Data": [["OGRN"], ["OGRN_TOKEN1"]]})
    mapping_store = InMemoryMappingStore()
    identifier_store = _identifier_store_with_token("OGRN_TOKEN1", VALID_OGRN, IdentifierType.OGRN)

    entity, identifier = build_replacement_plan(wb, mapping_store, identifier_store)

    assert identifier == (AnalyticalCellReplacement(worksheet_index=1, row=2, column=1, value=VALID_OGRN),)


# ---------------------------------------------------------------------------
# Miss scenarios
# ---------------------------------------------------------------------------


def test_entity_miss() -> None:
    wb = _make_workbook({"Data": [["Client"], ["не является alias"]]})
    mapping_store = InMemoryMappingStore()

    entity, identifier = build_replacement_plan(wb, mapping_store, None)

    assert entity == ()
    assert identifier == ()


def test_identifier_miss() -> None:
    wb = _make_workbook({"Data": [["INN"], ["INN_UNKNOWN"]]})
    mapping_store = InMemoryMappingStore()
    identifier_store = InMemoryIdentifierMappingStore()  # пустой — token отсутствует

    entity, identifier = build_replacement_plan(wb, mapping_store, identifier_store)

    assert entity == ()
    assert identifier == ()


def test_dual_miss() -> None:
    wb = _make_workbook({"Data": [["A"], ["обычный текст"]]})
    mapping_store = InMemoryMappingStore()
    identifier_store = InMemoryIdentifierMappingStore()

    entity, identifier = build_replacement_plan(wb, mapping_store, identifier_store)

    assert entity == ()
    assert identifier == ()


# ---------------------------------------------------------------------------
# Dual hit -> AmbiguousRestoreError
# ---------------------------------------------------------------------------


def test_dual_hit_raises_ambiguous_restore_error() -> None:
    wb = _make_workbook({"Data": [["Value"], ["COLLIDE"]]})
    mapping_store = _mapping_store_with_alias("COLLIDE", "ООО Тест")
    identifier_store = _identifier_store_with_token("COLLIDE", VALID_INN, IdentifierType.INN)

    with pytest.raises(AmbiguousRestoreError) as exc_info:
        build_replacement_plan(wb, mapping_store, identifier_store)

    message = str(exc_info.value)
    assert "worksheet 1" in message
    assert "row 2" in message
    assert "column 1" in message
    # Никаких confidential-значений в сообщении.
    assert "COLLIDE" not in message
    assert "ООО Тест" not in message
    assert VALID_INN not in message


def test_identifier_store_none_no_ambiguity_possible() -> None:
    wb = _make_workbook({"Data": [["Value"], ["COLLIDE"]]})
    mapping_store = _mapping_store_with_alias("COLLIDE", "ООО Тест")

    entity, identifier = build_replacement_plan(wb, mapping_store, None)

    assert entity == (AnalyticalCellReplacement(worksheet_index=1, row=2, column=1, value="ООО Тест"),)
    assert identifier == ()


# ---------------------------------------------------------------------------
# Eligibility: substring / whitespace / case / formula / non-string / error cell
# ---------------------------------------------------------------------------


def test_substring_unchanged() -> None:
    wb = _make_workbook({"Data": [["Note"], ["Клиент: C_ALIAS1, рост 20%"]]})
    mapping_store = _mapping_store_with_alias("C_ALIAS1", "ООО Ромашка")

    entity, identifier = build_replacement_plan(wb, mapping_store, None)

    assert entity == ()


def test_whitespace_mismatch_unchanged() -> None:
    wb = _make_workbook({"Data": [["Note"], [" C_ALIAS1 "]]})
    mapping_store = _mapping_store_with_alias("C_ALIAS1", "ООО Ромашка")

    entity, identifier = build_replacement_plan(wb, mapping_store, None)

    assert entity == ()


def test_case_mismatch_unchanged() -> None:
    wb = _make_workbook({"Data": [["Note"], ["c_alias1"]]})
    mapping_store = _mapping_store_with_alias("C_ALIAS1", "ООО Ромашка")

    entity, identifier = build_replacement_plan(wb, mapping_store, None)

    assert entity == ()


def test_formula_unchanged() -> None:
    wb = _make_workbook({"Data": [["Value"], ["=A1"]]})
    mapping_store = InMemoryMappingStore()

    entity, identifier = build_replacement_plan(wb, mapping_store, None)

    assert entity == ()


def test_formula_containing_alias_text_unchanged() -> None:
    wb = _make_workbook({"Data": [["Value"], ['=IF(A1="C_ALIAS1",1,0)']]})
    mapping_store = _mapping_store_with_alias("C_ALIAS1", "ООО Ромашка")

    entity, identifier = build_replacement_plan(wb, mapping_store, None)

    assert entity == ()


def test_non_string_unchanged() -> None:
    wb = _make_workbook({"Data": [["Value"], [42]]})
    mapping_store = InMemoryMappingStore()

    entity, identifier = build_replacement_plan(wb, mapping_store, None)

    assert entity == ()
    assert identifier == ()


def test_excel_error_cell_skipped() -> None:
    wb = _make_workbook({"Data": [["Value"], ["#N/A"]]})
    # Гипотетический alias "#N/A" в MappingStore — не должен быть найден,
    # так как error-ячейка исключается до lookup.
    mapping_store = _mapping_store_with_alias("#N/A", "ООО Ромашка")

    # Подтверждаем, что openpyxl действительно распознал "#N/A" как error cell.
    assert wb["Data"]["A2"].data_type == "e"

    entity, identifier = build_replacement_plan(wb, mapping_store, None)

    assert entity == ()


# ---------------------------------------------------------------------------
# Merged cells
# ---------------------------------------------------------------------------


def test_merged_top_left_restored() -> None:
    wb = _make_workbook({"Data": [["A", "B"]]})
    wb["Data"]["A1"] = "C_ALIAS1"
    wb["Data"].merge_cells("A1:B1")
    mapping_store = _mapping_store_with_alias("C_ALIAS1", "ООО Ромашка")

    entity, identifier = build_replacement_plan(wb, mapping_store, None)

    assert entity == (AnalyticalCellReplacement(worksheet_index=1, row=1, column=1, value="ООО Ромашка"),)


def test_merged_non_top_left_skipped() -> None:
    wb = _make_workbook({"Data": [["A", "B"]]})
    wb["Data"].merge_cells("A1:B1")
    mapping_store = InMemoryMappingStore()

    # Не должно падать и не должно пытаться резолвить non-top-left cell.
    entity, identifier = build_replacement_plan(wb, mapping_store, None)
    assert entity == ()
    assert identifier == ()


# ---------------------------------------------------------------------------
# Duplicate occurrences / counts by cells
# ---------------------------------------------------------------------------


def test_duplicate_occurrences_counted_by_cells() -> None:
    wb = _make_workbook(
        {"Data": [["Client"], ["C_ALIAS1"], ["C_ALIAS1"], ["другое"], ["C_ALIAS1"]]}
    )
    mapping_store = _mapping_store_with_alias("C_ALIAS1", "ООО Ромашка")

    entity, identifier = build_replacement_plan(wb, mapping_store, None)

    assert len(entity) == 3
    assert all(r.value == "ООО Ромашка" for r in entity)


# ---------------------------------------------------------------------------
# Multiple worksheets / hidden / veryHidden
# ---------------------------------------------------------------------------


def test_multiple_worksheets_scanned() -> None:
    wb = _make_workbook({"First": [["Client"], ["C_ALIAS1"]], "Second": [["INN"], ["INN_TOKEN1"]]})
    mapping_store = _mapping_store_with_alias("C_ALIAS1", "ООО Ромашка")
    identifier_store = _identifier_store_with_token("INN_TOKEN1", VALID_INN, IdentifierType.INN)

    entity, identifier = build_replacement_plan(wb, mapping_store, identifier_store)

    assert entity == (AnalyticalCellReplacement(worksheet_index=1, row=2, column=1, value="ООО Ромашка"),)
    assert identifier == (AnalyticalCellReplacement(worksheet_index=2, row=2, column=1, value=VALID_INN),)


def test_hidden_worksheet_scanned() -> None:
    wb = _make_workbook({"Visible": [["A"]], "HiddenSheet": [["Client"], ["C_ALIAS1"]]})
    wb["HiddenSheet"].sheet_state = "hidden"
    mapping_store = _mapping_store_with_alias("C_ALIAS1", "ООО Ромашка")

    entity, identifier = build_replacement_plan(wb, mapping_store, None)

    assert entity == (AnalyticalCellReplacement(worksheet_index=2, row=2, column=1, value="ООО Ромашка"),)


def test_very_hidden_worksheet_scanned() -> None:
    wb = _make_workbook({"Visible": [["A"]], "VeryHiddenSheet": [["Client"], ["C_ALIAS1"]]})
    wb["VeryHiddenSheet"].sheet_state = "veryHidden"
    mapping_store = _mapping_store_with_alias("C_ALIAS1", "ООО Ромашка")

    entity, identifier = build_replacement_plan(wb, mapping_store, None)

    assert entity == (AnalyticalCellReplacement(worksheet_index=2, row=2, column=1, value="ООО Ромашка"),)


# ---------------------------------------------------------------------------
# No normalization / no guessing
# ---------------------------------------------------------------------------


def test_no_normalization_no_guessing() -> None:
    # Строка, "похожая" на identifier token по формату, но реально
    # отсутствующая в store — не восстанавливается по догадке/формату.
    wb = _make_workbook({"Data": [["INN"], ["INN_LOOKS_LIKE_TOKEN"]]})
    mapping_store = InMemoryMappingStore()
    identifier_store = InMemoryIdentifierMappingStore()

    entity, identifier = build_replacement_plan(wb, mapping_store, identifier_store)

    assert entity == ()
    assert identifier == ()


# ---------------------------------------------------------------------------
# Store immutability
# ---------------------------------------------------------------------------


def test_stores_unchanged_after_scan() -> None:
    wb = _make_workbook({"Data": [["Client", "INN"], ["C_ALIAS1", "INN_TOKEN1"]]})
    mapping_store = _mapping_store_with_alias("C_ALIAS1", "ООО Ромашка")
    identifier_store = _identifier_store_with_token("INN_TOKEN1", VALID_INN, IdentifierType.INN)

    mapping_entries_before = mapping_store.entries()
    identifier_entries_before = identifier_store.entries()

    build_replacement_plan(wb, mapping_store, identifier_store)

    assert mapping_store.entries() == mapping_entries_before
    assert identifier_store.entries() == identifier_entries_before


# ---------------------------------------------------------------------------
# apply_replacement_plan
# ---------------------------------------------------------------------------


def test_apply_replacement_plan_mutates_only_planned_cells() -> None:
    wb = _make_workbook({"Data": [["A1", "B1"], ["A2", "B2"]]})
    entity = (AnalyticalCellReplacement(worksheet_index=1, row=1, column=1, value="restored-A1"),)
    identifier = (AnalyticalCellReplacement(worksheet_index=1, row=2, column=2, value="restored-B2"),)

    apply_replacement_plan(wb, entity, identifier)

    ws = wb["Data"]
    assert ws.cell(row=1, column=1).value == "restored-A1"
    assert ws.cell(row=2, column=2).value == "restored-B2"
    assert ws.cell(row=1, column=2).value == "B1"
    assert ws.cell(row=2, column=1).value == "A2"


# ---------------------------------------------------------------------------
# All-or-nothing на уровне core: late ambiguity не мутирует workbook
# ---------------------------------------------------------------------------


def test_late_ambiguity_does_not_mutate_any_cell() -> None:
    wb = _make_workbook(
        {
            "Sheet1": [["C_ALIAS1", "обычный текст"], ["INN_TOKEN1", "C_ALIAS1"]],
            "Sheet2": [["C_ALIAS2", "COLLIDE"], ["не alias", "INN_TOKEN1"]],
        }
    )
    mapping_store = InMemoryMappingStore()
    for alias, real_value in (("C_ALIAS1", "ООО Ромашка"), ("C_ALIAS2", "ООО Лютик"), ("COLLIDE", "ООО Тест")):
        mapping_store.add(MappingEntry(alias=alias, real_value=real_value, entity_type=EntityType.COMPANY))
    identifier_store = InMemoryIdentifierMappingStore()
    identifier_store.add(
        IdentifierMappingEntry(token="INN_TOKEN1", identifier_value=VALID_INN, identifier_type=IdentifierType.INN)
    )
    identifier_store.add(
        IdentifierMappingEntry(token="COLLIDE", identifier_value="1234567890", identifier_type=IdentifierType.INN)
    )

    def snapshot() -> dict[tuple[str, int, int], object]:
        return {
            (ws.title, cell.row, cell.column): cell.value
            for ws in wb.worksheets
            for row in ws.iter_rows()
            for cell in row
        }

    before = snapshot()
    # Ambiguity (Sheet2!B1) возникает ПОСЛЕ нескольких успешно найденных кандидатов
    # (Sheet1!A1, Sheet1!A2, Sheet1!B2, Sheet2!A1) — ни один из них не должен быть изменён.
    assert before[("Sheet1", 1, 1)] == "C_ALIAS1"
    assert before[("Sheet2", 1, 1)] == "C_ALIAS2"

    with pytest.raises(AmbiguousRestoreError):
        build_replacement_plan(wb, mapping_store, identifier_store)

    assert snapshot() == before
    assert wb["Sheet1"]["A1"].value == "C_ALIAS1"
    assert wb["Sheet1"]["A2"].value == "INN_TOKEN1"
    assert wb["Sheet1"]["B2"].value == "C_ALIAS1"
    assert wb["Sheet2"]["A1"].value == "C_ALIAS2"
    assert wb["Sheet2"]["B1"].value == "COLLIDE"
    assert wb["Sheet2"]["B2"].value == "INN_TOKEN1"
