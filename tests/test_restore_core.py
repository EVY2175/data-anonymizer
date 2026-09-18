"""
Тесты Restore Core (Stage 9A) — app.restore.core.

Все workbook строятся напрямую через openpyxl.Workbook() в памяти, без
файлового I/O — core.py оперирует уже загруженными объектами, поэтому
unit-тесты не нуждаются в anonymize_workbook/EncryptedFileProvenanceStore.
Все данные synthetic; ИНН — валидный по чек-сумме, но синтетический (тот
же, что уже используется в других test-файлах проекта).
"""

from __future__ import annotations

import openpyxl
import pytest
from openpyxl.packaging.custom import StringProperty

from app.mapping.identifier_memory import InMemoryIdentifierMappingStore
from app.mapping.memory import InMemoryMappingStore
from app.models.entities import EntityType, MappingEntry
from app.models.identifiers import IdentifierMappingEntry, IdentifierType
from app.models.provenance import IdentifierCellProvenance, IdentifierRepresentation
from app.restore.core import (
    ReplacementPlanEntry,
    RestoreValidationError,
    UnresolvedRestoreError,
    apply_replacement_plan,
    check_job_id_binding,
    read_workbook_job_id,
    resolve_entity_replacements,
    resolve_identifier_replacements,
    snapshot_structural_bounds,
)

VALID_INN = "7707083893"
JOB_ID_PROPERTY = "DataAnonymizer.JobId"


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


def _add_job_id_property(wb: openpyxl.Workbook, value: object) -> None:
    """Прямая манипуляция .props для тестов, эмулирующих сторонне
    отредактированный/повреждённый файл — обходит уникальность
    CustomPropertyList.append()."""
    wb.custom_doc_props.props.append(StringProperty(name=JOB_ID_PROPERTY, value=value))


# ---------------------------------------------------------------------------
# read_workbook_job_id
# ---------------------------------------------------------------------------


def test_read_workbook_job_id_missing_raises() -> None:
    wb = _make_workbook({"Data": [["A"]]})
    with pytest.raises(RestoreValidationError):
        read_workbook_job_id(wb)


def test_read_workbook_job_id_duplicate_raises() -> None:
    wb = _make_workbook({"Data": [["A"]]})
    _add_job_id_property(wb, "job-1")
    _add_job_id_property(wb, "job-2")
    with pytest.raises(RestoreValidationError):
        read_workbook_job_id(wb)


def test_read_workbook_job_id_empty_raises() -> None:
    wb = _make_workbook({"Data": [["A"]]})
    _add_job_id_property(wb, "")
    with pytest.raises(RestoreValidationError):
        read_workbook_job_id(wb)


def test_read_workbook_job_id_whitespace_only_raises() -> None:
    wb = _make_workbook({"Data": [["A"]]})
    _add_job_id_property(wb, "   ")
    with pytest.raises(RestoreValidationError):
        read_workbook_job_id(wb)


def test_read_workbook_job_id_correct_value() -> None:
    wb = _make_workbook({"Data": [["A"]]})
    _add_job_id_property(wb, "job-abc123")
    assert read_workbook_job_id(wb) == "job-abc123"


# ---------------------------------------------------------------------------
# check_job_id_binding
# ---------------------------------------------------------------------------


def test_check_job_id_binding_match_ok() -> None:
    check_job_id_binding("job-1", "job-1")  # не должно поднимать исключение


def test_check_job_id_binding_mismatch_raises() -> None:
    with pytest.raises(RestoreValidationError):
        check_job_id_binding("job-1", "job-2")


# ---------------------------------------------------------------------------
# snapshot_structural_bounds
# ---------------------------------------------------------------------------


def test_snapshot_structural_bounds() -> None:
    wb = _make_workbook({"First": [["A", "B"], ["C", "D"]], "Second": [["X"]]})
    bounds = snapshot_structural_bounds(wb)
    assert bounds == {"First": (2, 2), "Second": (1, 1)}


# ---------------------------------------------------------------------------
# resolve_identifier_replacements
# ---------------------------------------------------------------------------


def test_identifier_string_representation_restore() -> None:
    wb = _make_workbook({"Data": [["INN"], ["INN_TOKEN1"]]})
    bounds = snapshot_structural_bounds(wb)
    provenance = (
        IdentifierCellProvenance(
            sheet_name="Data", row=2, column=1, token="INN_TOKEN1",
            representation=IdentifierRepresentation.STRING,
        ),
    )
    store = InMemoryIdentifierMappingStore()
    store.add(IdentifierMappingEntry(token="INN_TOKEN1", identifier_value=VALID_INN, identifier_type=IdentifierType.INN))

    plan = resolve_identifier_replacements(wb, bounds, provenance, store)

    assert plan == (ReplacementPlanEntry(sheet_name="Data", row=2, column=1, restored_value=VALID_INN),)
    assert isinstance(plan[0].restored_value, str)


def test_identifier_integer_representation_restore() -> None:
    wb = _make_workbook({"Data": [["INN"], ["INN_TOKEN1"]]})
    bounds = snapshot_structural_bounds(wb)
    provenance = (
        IdentifierCellProvenance(
            sheet_name="Data", row=2, column=1, token="INN_TOKEN1",
            representation=IdentifierRepresentation.INTEGER,
        ),
    )
    store = InMemoryIdentifierMappingStore()
    store.add(IdentifierMappingEntry(token="INN_TOKEN1", identifier_value=VALID_INN, identifier_type=IdentifierType.INN))

    plan = resolve_identifier_replacements(wb, bounds, provenance, store)

    assert plan == (ReplacementPlanEntry(sheet_name="Data", row=2, column=1, restored_value=int(VALID_INN)),)
    assert isinstance(plan[0].restored_value, int)


def test_identifier_integer_conversion_failure_raises() -> None:
    wb = _make_workbook({"Data": [["INN"], ["INN_TOKEN1"]]})
    bounds = snapshot_structural_bounds(wb)
    provenance = (
        IdentifierCellProvenance(
            sheet_name="Data", row=2, column=1, token="INN_TOKEN1",
            representation=IdentifierRepresentation.INTEGER,
        ),
    )
    store = InMemoryIdentifierMappingStore()
    # Синтетическое повреждение: identifier_value не является чистым int-string.
    store.add(
        IdentifierMappingEntry(token="INN_TOKEN1", identifier_value="not-a-number", identifier_type=IdentifierType.INN)
    )

    with pytest.raises(UnresolvedRestoreError):
        resolve_identifier_replacements(wb, bounds, provenance, store)


def test_identifier_token_mismatch_raises_and_no_lookup_performed() -> None:
    wb = _make_workbook({"Data": [["INN"], ["EDITED_VALUE"]]})  # ячейка изменена после анонимизации
    bounds = snapshot_structural_bounds(wb)
    provenance = (
        IdentifierCellProvenance(
            sheet_name="Data", row=2, column=1, token="INN_TOKEN1",
            representation=IdentifierRepresentation.STRING,
        ),
    )

    lookup_calls = {"n": 0}
    store = InMemoryIdentifierMappingStore()
    store.add(IdentifierMappingEntry(token="INN_TOKEN1", identifier_value=VALID_INN, identifier_type=IdentifierType.INN))
    real_get_by_token = store.get_by_token
    store.get_by_token = lambda token: (lookup_calls.__setitem__("n", lookup_calls["n"] + 1), real_get_by_token(token))[1]

    with pytest.raises(UnresolvedRestoreError):
        resolve_identifier_replacements(wb, bounds, provenance, store)

    assert lookup_calls["n"] == 0  # get_by_token не должен вызываться до exact token match


def test_identifier_token_lookup_missing_raises() -> None:
    wb = _make_workbook({"Data": [["INN"], ["INN_TOKEN1"]]})
    bounds = snapshot_structural_bounds(wb)
    provenance = (
        IdentifierCellProvenance(
            sheet_name="Data", row=2, column=1, token="INN_TOKEN1",
            representation=IdentifierRepresentation.STRING,
        ),
    )
    empty_store = InMemoryIdentifierMappingStore()  # "не тот" store — token отсутствует

    with pytest.raises(UnresolvedRestoreError):
        resolve_identifier_replacements(wb, bounds, provenance, empty_store)


def test_identifier_missing_sheet_raises() -> None:
    wb = _make_workbook({"Data": [["INN"], ["INN_TOKEN1"]]})
    bounds = snapshot_structural_bounds(wb)
    provenance = (
        IdentifierCellProvenance(
            sheet_name="NoSuchSheet", row=2, column=1, token="INN_TOKEN1",
            representation=IdentifierRepresentation.STRING,
        ),
    )
    store = InMemoryIdentifierMappingStore()

    with pytest.raises(UnresolvedRestoreError):
        resolve_identifier_replacements(wb, bounds, provenance, store)


def test_identifier_out_of_bounds_coordinate_raises() -> None:
    wb = _make_workbook({"Data": [["INN"], ["INN_TOKEN1"]]})  # max_row=2, max_column=1
    bounds = snapshot_structural_bounds(wb)
    provenance = (
        IdentifierCellProvenance(
            sheet_name="Data", row=99, column=1, token="INN_TOKEN1",
            representation=IdentifierRepresentation.STRING,
        ),
    )
    store = InMemoryIdentifierMappingStore()

    with pytest.raises(UnresolvedRestoreError):
        resolve_identifier_replacements(wb, bounds, provenance, store)


def test_identifier_non_top_left_merged_coordinate_raises() -> None:
    wb = _make_workbook({"Data": [["A", "B"], ["C", "D"]]})
    wb["Data"].merge_cells("A1:B1")  # B1 становится non-top-left MergedCell
    bounds = snapshot_structural_bounds(wb)
    provenance = (
        IdentifierCellProvenance(
            sheet_name="Data", row=1, column=2, token="INN_TOKEN1",
            representation=IdentifierRepresentation.STRING,
        ),
    )
    store = InMemoryIdentifierMappingStore()

    with pytest.raises(UnresolvedRestoreError):
        resolve_identifier_replacements(wb, bounds, provenance, store)


def test_identifier_formula_at_coordinate_is_token_mismatch() -> None:
    wb = _make_workbook({"Data": [["INN"], ["=SUM(A1:A1)"]]})
    bounds = snapshot_structural_bounds(wb)
    provenance = (
        IdentifierCellProvenance(
            sheet_name="Data", row=2, column=1, token="INN_TOKEN1",
            representation=IdentifierRepresentation.STRING,
        ),
    )
    store = InMemoryIdentifierMappingStore()

    with pytest.raises(UnresolvedRestoreError):
        resolve_identifier_replacements(wb, bounds, provenance, store)


def test_identifier_multiple_sheets() -> None:
    wb = _make_workbook({"First": [["INN"], ["INN_TOKEN1"]], "Second": [["INN"], ["INN_TOKEN2"]]})
    bounds = snapshot_structural_bounds(wb)
    provenance = (
        IdentifierCellProvenance(
            sheet_name="First", row=2, column=1, token="INN_TOKEN1",
            representation=IdentifierRepresentation.STRING,
        ),
        IdentifierCellProvenance(
            sheet_name="Second", row=2, column=1, token="INN_TOKEN2",
            representation=IdentifierRepresentation.STRING,
        ),
    )
    store = InMemoryIdentifierMappingStore()
    store.add(IdentifierMappingEntry(token="INN_TOKEN1", identifier_value=VALID_INN, identifier_type=IdentifierType.INN))
    store.add(IdentifierMappingEntry(token="INN_TOKEN2", identifier_value="7707083886", identifier_type=IdentifierType.INN))

    plan = resolve_identifier_replacements(wb, bounds, provenance, store)

    assert set(plan) == {
        ReplacementPlanEntry(sheet_name="First", row=2, column=1, restored_value=VALID_INN),
        ReplacementPlanEntry(sheet_name="Second", row=2, column=1, restored_value="7707083886"),
    }


def test_identifier_empty_provenance_allows_none_store() -> None:
    wb = _make_workbook({"Data": [["Company"], ["ООО Ромашка"]]})
    bounds = snapshot_structural_bounds(wb)
    plan = resolve_identifier_replacements(wb, bounds, (), None)
    assert plan == ()


# ---------------------------------------------------------------------------
# resolve_entity_replacements
# ---------------------------------------------------------------------------


def _mapping_store_with_alias(alias: str, real_value: str) -> InMemoryMappingStore:
    store = InMemoryMappingStore()
    store.add(MappingEntry(alias=alias, real_value=real_value, entity_type=EntityType.COMPANY))
    return store


def test_entity_exact_alias_restored() -> None:
    wb = _make_workbook({"Data": [["Company"], ["C_ALIAS1"]]})
    bounds = snapshot_structural_bounds(wb)
    store = _mapping_store_with_alias("C_ALIAS1", "ООО Ромашка")

    plan = resolve_entity_replacements(wb, bounds, store, set())

    assert plan == (ReplacementPlanEntry(sheet_name="Data", row=2, column=1, restored_value="ООО Ромашка"),)


def test_entity_unknown_string_unchanged() -> None:
    wb = _make_workbook({"Data": [["Company"], ["не является alias"]]})
    bounds = snapshot_structural_bounds(wb)
    store = InMemoryMappingStore()  # пустой — alias отсутствует

    plan = resolve_entity_replacements(wb, bounds, store, set())

    assert plan == ()


def test_entity_substring_unchanged() -> None:
    wb = _make_workbook({"Data": [["Company"], ["префикс C_ALIAS1 суффикс"]]})
    bounds = snapshot_structural_bounds(wb)
    store = _mapping_store_with_alias("C_ALIAS1", "ООО Ромашка")

    plan = resolve_entity_replacements(wb, bounds, store, set())

    assert plan == ()  # только whole-cell exact match, не substring


def test_entity_formula_unchanged() -> None:
    wb = _make_workbook({"Data": [["Company"], ["=A1"]]})
    bounds = snapshot_structural_bounds(wb)
    store = _mapping_store_with_alias("=A1", "ООО Ромашка")  # гипотетически совпадающий alias

    plan = resolve_entity_replacements(wb, bounds, store, set())

    assert plan == ()  # формулы никогда не рассматриваются


def test_entity_non_string_unchanged() -> None:
    wb = _make_workbook({"Data": [["Value"], [42]]})
    bounds = snapshot_structural_bounds(wb)
    store = InMemoryMappingStore()

    plan = resolve_entity_replacements(wb, bounds, store, set())

    assert plan == ()


def test_entity_empty_and_whitespace_only_string_unchanged() -> None:
    wb = _make_workbook({"Data": [["A", "B"], ["", "   "]]})
    bounds = snapshot_structural_bounds(wb)
    store = InMemoryMappingStore()

    plan = resolve_entity_replacements(wb, bounds, store, set())

    assert plan == ()


def test_entity_alias_on_sheet_without_provenance_restored() -> None:
    # Лист "Second" вообще не упоминается ни в каком provenance —
    # entity restore всё равно должен обработать его (обходит ВСЕ sheets).
    wb = _make_workbook({"First": [["INN"], ["INN_TOKEN1"]], "Second": [["Company"], ["C_ALIAS1"]]})
    bounds = snapshot_structural_bounds(wb)
    store = _mapping_store_with_alias("C_ALIAS1", "ООО Ромашка")

    plan = resolve_entity_replacements(wb, bounds, store, excluded_coordinates=set())

    assert plan == (ReplacementPlanEntry(sheet_name="Second", row=2, column=1, restored_value="ООО Ромашка"),)


def test_entity_excludes_provenance_coordinates() -> None:
    # Ячейка (Data, 2, 1) содержит token, который СЛУЧАЙНО совпадает с
    # известным entity alias в MappingStore — но это identifier-координата,
    # и она должна быть полностью исключена из entity lookup.
    wb = _make_workbook({"Data": [["INN"], ["C_ALIAS1"]]})
    bounds = snapshot_structural_bounds(wb)
    store = _mapping_store_with_alias("C_ALIAS1", "ООО Ромашка")

    plan = resolve_entity_replacements(wb, bounds, store, excluded_coordinates={("Data", 2, 1)})

    assert plan == ()


def test_entity_non_top_left_merged_cell_skipped() -> None:
    wb = _make_workbook({"Data": [["A", "B"]]})
    wb["Data"].merge_cells("A1:B1")
    bounds = snapshot_structural_bounds(wb)
    store = InMemoryMappingStore()

    # Не должно падать и не должно пытаться резолвить non-top-left cell.
    plan = resolve_entity_replacements(wb, bounds, store, set())
    assert plan == ()


def test_entity_wrong_mapping_store_limitation_no_error() -> None:
    """
    Frozen limitation: alias, отсутствующий в "чужом" MappingStore, НЕ
    считается ошибкой — ячейка просто остаётся unchanged.
    """
    wb = _make_workbook({"Data": [["Company"], ["C_REAL_ALIAS"]]})
    bounds = snapshot_structural_bounds(wb)
    wrong_store = InMemoryMappingStore()  # не содержит C_REAL_ALIAS вообще

    plan = resolve_entity_replacements(wb, bounds, wrong_store, set())

    assert plan == ()  # не исключение, а тихий passthrough


# ---------------------------------------------------------------------------
# apply_replacement_plan
# ---------------------------------------------------------------------------


def test_apply_replacement_plan_mutates_only_planned_cells() -> None:
    wb = _make_workbook({"Data": [["A1", "B1"], ["A2", "B2"]]})
    plan = (
        ReplacementPlanEntry(sheet_name="Data", row=1, column=1, restored_value="restored-A1"),
        ReplacementPlanEntry(sheet_name="Data", row=2, column=2, restored_value=42),
    )

    apply_replacement_plan(wb, plan)

    ws = wb["Data"]
    assert ws.cell(row=1, column=1).value == "restored-A1"
    assert ws.cell(row=2, column=2).value == 42
    assert ws.cell(row=1, column=2).value == "B1"  # не запланированная ячейка не тронута
    assert ws.cell(row=2, column=1).value == "A2"
