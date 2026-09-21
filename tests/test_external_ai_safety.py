"""
Тесты External AI Safety inspection primitives (Stage 10A) —
app.safety.external_ai.

Все данные synthetic. Workbook строятся напрямую через openpyxl; stores —
InMemory реализации. Реальных названий компаний, идентификаторов и
паролей здесь нет.
"""

from __future__ import annotations

import dataclasses
import hashlib
import os
import zipfile
from pathlib import Path

import openpyxl
import pytest
from openpyxl.chart import BarChart, Reference
from openpyxl.comments import Comment
from openpyxl.packaging.custom import StringProperty
from openpyxl.workbook.defined_name import DefinedName
from openpyxl.worksheet.formula import ArrayFormula
from openpyxl.worksheet.table import Table

from app.excel import writer as excel_writer
from app.mapping.identifier_memory import InMemoryIdentifierMappingStore
from app.mapping.memory import InMemoryMappingStore
from app.models.entities import EntityType, MappingEntry
from app.models.identifiers import IdentifierMappingEntry, IdentifierType
from app.restore import analytical_writer, writer as restore_writer
from app.safety import external_ai
from app.safety.external_ai import (
    MAX_FINDINGS_PER_KIND,
    SEVERITY_BY_KIND,
    CoreProperty,
    ExternalAiInspectionError,
    ExternalAiInspectionInputError,
    ExternalAiSafetyError,
    FindingKind,
    FindingLocation,
    InspectionCheck,
    InspectionFailureReason,
    NotInspectedArea,
    SafetyFinding,
    Severity,
    inspect_workbook_for_external_ai,
    sha256_file,
)

ENTITY = "ООО Ромашка"
PERSON = "Иванов Иван"
IDENT = "7707083893"
ANALYTICAL_MARKER = "DataAnonymizer.AnalyticallyRestored"
ORIGINAL_MARKER = "DataAnonymizer.RestoredFromJobId"


# ---------------------------------------------------------------------------
# Помощники
# ---------------------------------------------------------------------------


def _make(tmp_path: Path, build=None, name: str = "w.xlsx") -> Path:
    wb = openpyxl.Workbook()
    if build is not None:
        build(wb)
    path = tmp_path / name
    wb.save(path)
    wb.close()
    return path


def _entity_store(*values: str) -> InMemoryMappingStore:
    store = InMemoryMappingStore()
    for index, value in enumerate(values):
        store.add(
            MappingEntry(alias=f"C_ALIAS{index}", real_value=value, entity_type=EntityType.COMPANY)
        )
    return store


def _identifier_store(*values: str) -> InMemoryIdentifierMappingStore:
    store = InMemoryIdentifierMappingStore()
    for index, value in enumerate(values):
        store.add(
            IdentifierMappingEntry(
                token=f"INN_TOKEN{index}", identifier_value=value, identifier_type=IdentifierType.INN
            )
        )
    return store


def _inspect(path: Path, entities=(ENTITY,), identifiers=(IDENT,)):
    identifier_store = None if identifiers is None else _identifier_store(*identifiers)
    return inspect_workbook_for_external_ai(path, _entity_store(*entities), identifier_store)


def _kinds(report) -> dict:
    return {kind: count for kind, count in report.finding_counts}


def _add_parts(path: Path, names: list) -> None:
    with zipfile.ZipFile(path, "a") as archive:
        for name in names:
            archive.writestr(name, b"x")


def _spy_load_workbook(monkeypatch: pytest.MonkeyPatch) -> dict:
    state = {"loaded": [], "closes": 0}
    real_load = openpyxl.load_workbook

    def spying_load(*args, **kwargs):
        loaded = real_load(*args, **kwargs)
        real_close = loaded.close

        def counting_close():
            state["closes"] += 1
            real_close()

        loaded.close = counting_close
        state["loaded"].append(loaded)
        return loaded

    monkeypatch.setattr(openpyxl, "load_workbook", spying_load)
    return state


# ---------------------------------------------------------------------------
# A. SHA-256
# ---------------------------------------------------------------------------


def test_sha256_empty_file(tmp_path: Path) -> None:
    path = tmp_path / "empty.bin"
    path.write_bytes(b"")
    assert sha256_file(path) == "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"


def test_sha256_abc_vector_and_lowercase_hex(tmp_path: Path) -> None:
    path = tmp_path / "abc.bin"
    path.write_bytes(b"abc")
    result = sha256_file(str(path))
    assert result == "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"
    assert result == result.lower() and len(result) == 64


def test_sha256_multi_block_file_matches_hashlib(tmp_path: Path) -> None:
    data = bytes(range(256)) * (3 * 4096 + 5)  # > 3 MiB, не кратно размеру блока
    path = tmp_path / "big.bin"
    path.write_bytes(data)
    assert len(data) > 3 * 1024 * 1024
    assert sha256_file(path) == hashlib.sha256(data).hexdigest()


def test_sha256_reads_in_chunks_not_whole_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    data = b"z" * 10_000
    path = tmp_path / "chunks.bin"
    path.write_bytes(data)
    monkeypatch.setattr(external_ai, "_HASH_CHUNK_SIZE", 1024)

    sizes: list[int] = []
    real_sha256 = hashlib.sha256

    class SpyDigest:
        def __init__(self) -> None:
            self._inner = real_sha256()

        def update(self, chunk: bytes) -> None:
            sizes.append(len(chunk))
            self._inner.update(chunk)

        def hexdigest(self) -> str:
            return self._inner.hexdigest()

    monkeypatch.setattr(external_ai.hashlib, "sha256", SpyDigest)

    assert sha256_file(path) == real_sha256(data).hexdigest()
    assert len(sizes) == 10 and max(sizes) <= 1024


def test_sha256_path_and_extension_not_part_of_hash(tmp_path: Path) -> None:
    first = tmp_path / "one.xlsx"
    second = tmp_path / "two.dat"
    first.write_bytes(b"same")
    second.write_bytes(b"same")
    assert sha256_file(first) == sha256_file(second)


def test_sha256_rejects_wrong_path_type() -> None:
    with pytest.raises(TypeError):
        sha256_file(123)  # type: ignore[arg-type]


def test_sha256_leaf_io_errors_unchanged(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        sha256_file(tmp_path / "missing.bin")
    with pytest.raises((IsADirectoryError, PermissionError)):
        sha256_file(tmp_path)


# ---------------------------------------------------------------------------
# B. Preflight
# ---------------------------------------------------------------------------


def test_preflight_invalid_path_type() -> None:
    with pytest.raises(TypeError):
        inspect_workbook_for_external_ai(123, _entity_store(ENTITY), None)  # type: ignore[arg-type]


def test_preflight_invalid_mapping_store_type(tmp_path: Path) -> None:
    path = _make(tmp_path)
    with pytest.raises(TypeError):
        inspect_workbook_for_external_ai(path, object(), None)  # type: ignore[arg-type]


def test_preflight_invalid_identifier_store_type(tmp_path: Path) -> None:
    path = _make(tmp_path)
    with pytest.raises(TypeError):
        inspect_workbook_for_external_ai(path, _entity_store(ENTITY), object())  # type: ignore[arg-type]


def test_preflight_wrong_suffix(tmp_path: Path) -> None:
    path = tmp_path / "w.txt"
    path.write_bytes(b"x")
    with pytest.raises(ExternalAiInspectionInputError) as exc_info:
        inspect_workbook_for_external_ai(path, _entity_store(ENTITY), None)
    assert isinstance(exc_info.value, ValueError)
    assert isinstance(exc_info.value, ExternalAiSafetyError)


def test_preflight_missing_file(tmp_path: Path) -> None:
    with pytest.raises(ExternalAiInspectionInputError):
        inspect_workbook_for_external_ai(tmp_path / "missing.xlsx", _entity_store(ENTITY), None)


def test_preflight_directory(tmp_path: Path) -> None:
    directory = tmp_path / "dir.xlsx"
    directory.mkdir()
    with pytest.raises(ExternalAiInspectionInputError):
        inspect_workbook_for_external_ai(directory, _entity_store(ENTITY), None)


def test_uppercase_suffix_accepted(tmp_path: Path) -> None:
    path = _make(tmp_path, name="W.XLSX")
    assert _inspect(path).worksheet_count == 1


# ---------------------------------------------------------------------------
# C. Restore markers
# ---------------------------------------------------------------------------


def _with_props(*props):
    def build(wb):
        for name, value in props:
            wb.custom_doc_props.append(StringProperty(name=name, value=value))

    return build


@pytest.mark.parametrize("name", [ANALYTICAL_MARKER, ORIGINAL_MARKER])
@pytest.mark.parametrize("value", ["true", "false", "", "anything"])
def test_restore_marker_any_value_is_blocking(tmp_path: Path, name: str, value: str) -> None:
    report = _inspect(_make(tmp_path, _with_props((name, value))))

    assert report.count(FindingKind.RESTORED_MARKER) == 1
    assert report.has_blocking_findings
    finding = report.findings[0]
    assert finding.kind is FindingKind.RESTORED_MARKER
    assert finding.location is FindingLocation.WORKBOOK
    assert finding.severity is Severity.BLOCKING
    assert finding.worksheet_index is None and finding.row is None


def test_duplicate_restore_markers_each_produce_finding(tmp_path: Path) -> None:
    def build(wb):
        wb.custom_doc_props.append(StringProperty(name=ANALYTICAL_MARKER, value="true"))
        wb.custom_doc_props.props.append(StringProperty(name=ANALYTICAL_MARKER, value="true"))

    report = _inspect(_make(tmp_path, build))
    assert report.count(FindingKind.RESTORED_MARKER) == 2


def test_both_marker_names_counted_separately(tmp_path: Path) -> None:
    report = _inspect(
        _make(tmp_path, _with_props((ANALYTICAL_MARKER, "true"), (ORIGINAL_MARKER, "abc")))
    )
    assert report.count(FindingKind.RESTORED_MARKER) == 2


def test_job_id_alone_is_not_a_finding(tmp_path: Path) -> None:
    report = _inspect(_make(tmp_path, _with_props(("DataAnonymizer.JobId", "job123"))))
    assert report.finding_counts == ()
    assert not report.has_blocking_findings and not report.has_unverified_exposures


def test_marker_names_match_closed_stage_private_constants() -> None:
    assert analytical_writer._ANALYTICALLY_RESTORED_PROPERTY_NAME == ANALYTICAL_MARKER
    assert restore_writer._RESTORED_FROM_JOB_ID_PROPERTY_NAME == ORIGINAL_MARKER
    assert set(external_ai._RESTORED_MARKER_PROPERTY_NAMES) == {ANALYTICAL_MARKER, ORIGINAL_MARKER}
    assert excel_writer._JOB_ID_PROPERTY_NAME == "DataAnonymizer.JobId"
    assert "DataAnonymizer.JobId" not in external_ai._RESTORED_MARKER_PROPERTY_NAMES


# ---------------------------------------------------------------------------
# D. Entity matching
# ---------------------------------------------------------------------------


def test_entity_exact_hit_with_coordinates(tmp_path: Path) -> None:
    def build(wb):
        wb.active["A1"] = "Клиент"
        wb.active["B2"] = ENTITY

    report = _inspect(_make(tmp_path, build))
    assert report.count(FindingKind.KNOWN_ENTITY_VALUE) == 1
    finding = report.findings[0]
    assert (finding.location, finding.worksheet_index, finding.row, finding.column) == (
        FindingLocation.CELL,
        1,
        2,
        2,
    )


def test_entity_matches_person_entries_too(tmp_path: Path) -> None:
    store = InMemoryMappingStore()
    store.add(MappingEntry(alias="P_ALIAS", real_value=PERSON, entity_type=EntityType.PERSON))

    def build(wb):
        wb.active["A1"] = PERSON

    report = inspect_workbook_for_external_ai(_make(tmp_path, build), store, None)
    assert report.count(FindingKind.KNOWN_ENTITY_VALUE) == 1


@pytest.mark.parametrize(
    "cell_value",
    [
        "Другая компания",
        f"Клиент {ENTITY} вырос",
        ENTITY.lower(),
        ENTITY.upper(),
        f" {ENTITY} ",
        f"{ENTITY}\n",
        f"{ENTITY} ",
    ],
)
def test_entity_non_exact_values_are_not_findings(tmp_path: Path, cell_value: str) -> None:
    def build(wb):
        wb.active["A1"] = cell_value

    report = _inspect(_make(tmp_path, build))
    assert report.count(FindingKind.KNOWN_ENTITY_VALUE) == 0


def test_entity_int_cell_never_matches_entity_values(tmp_path: Path) -> None:
    def build(wb):
        wb.active["A1"] = 12345

    report = _inspect(_make(tmp_path, build), entities=("12345",), identifiers=("999",))
    assert report.finding_counts == ()


# ---------------------------------------------------------------------------
# E. Identifier matching
# ---------------------------------------------------------------------------


def test_identifier_string_exact_hit(tmp_path: Path) -> None:
    def build(wb):
        wb.active["A1"] = IDENT

    report = _inspect(_make(tmp_path, build))
    assert report.count(FindingKind.KNOWN_IDENTIFIER_VALUE) == 1
    assert report.count(FindingKind.KNOWN_ENTITY_VALUE) == 0


def test_identifier_int_matches_via_str(tmp_path: Path) -> None:
    def build(wb):
        wb.active["A1"] = int(IDENT)

    report = _inspect(_make(tmp_path, build))
    assert report.count(FindingKind.KNOWN_IDENTIFIER_VALUE) == 1


def test_identifier_leading_zero_not_reconstructed(tmp_path: Path) -> None:
    def build(wb):
        wb.active["A1"] = 12345678  # int: Excel уже потерял ведущие нули
        wb.active["A2"] = "0012345678"  # str: точное совпадение

    report = _inspect(_make(tmp_path, build), identifiers=("0012345678",))
    assert report.count(FindingKind.KNOWN_IDENTIFIER_VALUE) == 1
    assert report.findings[0].row == 2


def test_identifier_bool_and_float_are_not_matched(tmp_path: Path) -> None:
    def build(wb):
        wb.active["A1"] = True
        wb.active["A2"] = 1.5

    report = _inspect(_make(tmp_path, build), identifiers=("True", "1.5", IDENT))
    assert report.count(FindingKind.KNOWN_IDENTIFIER_VALUE) == 0


def test_identifier_store_none_disables_identifier_check(tmp_path: Path) -> None:
    def build(wb):
        wb.active["A1"] = IDENT

    report = _inspect(_make(tmp_path, build), identifiers=None)
    assert report.count(FindingKind.KNOWN_IDENTIFIER_VALUE) == 0
    assert report.indexed_identifier_values is None
    assert InspectionCheck.KNOWN_IDENTIFIER_EXACT not in report.checks_performed
    assert InspectionCheck.KNOWN_ENTITY_EXACT in report.checks_performed


def test_identifier_store_present_reports_index_and_check(tmp_path: Path) -> None:
    report = _inspect(_make(tmp_path), identifiers=(IDENT, "1234567890"))
    assert report.indexed_identifier_values == 2
    assert report.indexed_entity_values == 1
    assert InspectionCheck.KNOWN_IDENTIFIER_EXACT in report.checks_performed


def test_value_in_both_indexes_yields_two_findings(tmp_path: Path) -> None:
    def build(wb):
        wb.active["A1"] = "SHARED"

    report = _inspect(_make(tmp_path, build), entities=("SHARED",), identifiers=("SHARED",))
    assert report.count(FindingKind.KNOWN_ENTITY_VALUE) == 1
    assert report.count(FindingKind.KNOWN_IDENTIFIER_VALUE) == 1


# ---------------------------------------------------------------------------
# F. Типы ячеек
# ---------------------------------------------------------------------------


def test_formula_string_is_unverified_and_not_looked_up(tmp_path: Path) -> None:
    def build(wb):
        wb.active["A1"] = f'=IF(B1="{ENTITY}",1,0)'

    report = _inspect(_make(tmp_path, build))
    assert report.count(FindingKind.FORMULA) == 1
    assert report.count(FindingKind.KNOWN_ENTITY_VALUE) == 0
    assert report.findings[0].severity is Severity.UNVERIFIED


def test_formula_text_equal_to_known_value_is_not_looked_up(tmp_path: Path) -> None:
    def build(wb):
        wb.active["A1"] = "=B1"

    report = _inspect(_make(tmp_path, build), entities=("=B1",), identifiers=("=B1",))
    assert _kinds(report) == {FindingKind.FORMULA: 1}


def test_array_formula_is_detected(tmp_path: Path) -> None:
    def build(wb):
        wb.active["A1"] = ArrayFormula("A1:A1", "=B1*2")

    assert _inspect(_make(tmp_path, build)).count(FindingKind.FORMULA) == 1


def test_error_cell_is_not_looked_up(tmp_path: Path) -> None:
    def build(wb):
        wb.active["A1"] = "#N/A"

    report = _inspect(_make(tmp_path, build), entities=("#N/A",))
    assert report.finding_counts == ()


def test_empty_and_style_only_cells_produce_no_findings(tmp_path: Path) -> None:
    def build(wb):
        wb.active["A1"].number_format = "0.00"
        wb.active["B1"] = None

    assert _inspect(_make(tmp_path, build)).finding_counts == ()


def test_string_starting_with_equals_stored_as_text_is_ordinary_string(tmp_path: Path) -> None:
    def build(wb):
        cell = wb.active["A1"]
        cell.value = "=Ромашка"
        cell.data_type = "s"

    report = _inspect(_make(tmp_path, build), entities=("=Ромашка",))
    assert report.count(FindingKind.FORMULA) == 0
    assert report.count(FindingKind.KNOWN_ENTITY_VALUE) == 1


def test_merged_cells_top_left_scanned_others_skipped(tmp_path: Path) -> None:
    def build(wb):
        wb.active["A1"] = ENTITY
        wb.active.merge_cells("A1:B2")

    report = _inspect(_make(tmp_path, build))
    assert report.count(FindingKind.KNOWN_ENTITY_VALUE) == 1
    assert report.finding_counts == ((FindingKind.KNOWN_ENTITY_VALUE, 1),)


# ---------------------------------------------------------------------------
# G. Листы
# ---------------------------------------------------------------------------


def test_hidden_and_very_hidden_sheets_are_scanned_and_flagged(tmp_path: Path) -> None:
    def build(wb):
        wb.active["A1"] = "Видимый"
        hidden = wb.create_sheet("Hid")
        hidden.sheet_state = "hidden"
        hidden["A1"] = ENTITY
        very = wb.create_sheet("Very")
        very.sheet_state = "veryHidden"
        very["A1"] = IDENT

    report = _inspect(_make(tmp_path, build))
    assert report.worksheet_count == 3
    assert report.count(FindingKind.HIDDEN_WORKSHEET) == 2
    by_kind = {f.kind: f for f in report.findings if f.kind in (FindingKind.KNOWN_ENTITY_VALUE, FindingKind.KNOWN_IDENTIFIER_VALUE)}
    assert by_kind[FindingKind.KNOWN_ENTITY_VALUE].worksheet_index == 2
    assert by_kind[FindingKind.KNOWN_IDENTIFIER_VALUE].worksheet_index == 3
    hidden_indexes = [f.worksheet_index for f in report.findings if f.kind is FindingKind.HIDDEN_WORKSHEET]
    assert hidden_indexes == [2, 3]
    assert all(f.location is FindingLocation.WORKSHEET for f in report.findings if f.kind is FindingKind.HIDDEN_WORKSHEET)


def test_sheet_title_exact_hit_entity_and_identifier(tmp_path: Path) -> None:
    def build(wb):
        wb.active.title = ENTITY
        wb.create_sheet(IDENT)

    report = _inspect(_make(tmp_path, build))
    hits = {f.kind: f for f in report.findings}
    assert hits[FindingKind.KNOWN_ENTITY_VALUE].location is FindingLocation.SHEET_TITLE
    assert hits[FindingKind.KNOWN_ENTITY_VALUE].worksheet_index == 1
    assert hits[FindingKind.KNOWN_IDENTIFIER_VALUE].location is FindingLocation.SHEET_TITLE
    assert hits[FindingKind.KNOWN_IDENTIFIER_VALUE].worksheet_index == 2


def test_non_matching_sheet_title_is_not_a_finding(tmp_path: Path) -> None:
    def build(wb):
        wb.active.title = "Продажи"

    assert _inspect(_make(tmp_path, build)).finding_counts == ()


# ---------------------------------------------------------------------------
# H. Exposures
# ---------------------------------------------------------------------------


def test_comment_and_hyperlink_are_unverified_with_coordinates(tmp_path: Path) -> None:
    def build(wb):
        ws = wb.active
        ws["A1"] = "text"
        ws["A1"].comment = Comment("комментарий", "автор")
        ws["B3"] = "link"
        ws["B3"].hyperlink = "https://example.invalid/path"

    report = _inspect(_make(tmp_path, build))
    by_kind = {f.kind: f for f in report.findings}
    assert (by_kind[FindingKind.COMMENT].row, by_kind[FindingKind.COMMENT].column) == (1, 1)
    assert (by_kind[FindingKind.HYPERLINK].row, by_kind[FindingKind.HYPERLINK].column) == (3, 2)
    assert report.has_unverified_exposures and not report.has_blocking_findings


def test_core_property_exposure_and_openpyxl_creator_benign(tmp_path: Path) -> None:
    default_report = _inspect(_make(tmp_path, name="default.xlsx"))
    assert default_report.count(FindingKind.CORE_PROPERTY) == 0

    def build(wb):
        wb.properties.creator = "Сидоров Пётр"

    report = _inspect(_make(tmp_path, build, name="creator.xlsx"))
    finding = report.findings[0]
    assert finding.kind is FindingKind.CORE_PROPERTY
    assert finding.location is FindingLocation.CORE_PROPERTY
    assert finding.core_property is CoreProperty.CREATOR
    assert finding.worksheet_index is None


@pytest.mark.parametrize(
    "core_property",
    [prop for prop in CoreProperty if prop is not CoreProperty.CREATOR],
)
def test_each_other_core_property_is_reported(tmp_path: Path, core_property: CoreProperty) -> None:
    def build(wb):
        setattr(wb.properties, core_property.value, "some-value")

    report = _inspect(_make(tmp_path, build))
    reported = [f.core_property for f in report.findings if f.kind is FindingKind.CORE_PROPERTY]
    assert reported == [core_property]


def test_whitespace_only_core_property_is_not_reported(tmp_path: Path) -> None:
    def build(wb):
        wb.properties.title = "   "

    assert _inspect(_make(tmp_path, build)).count(FindingKind.CORE_PROPERTY) == 0


def test_known_value_in_core_property_is_blocking_and_unverified(tmp_path: Path) -> None:
    def build(wb):
        wb.properties.lastModifiedBy = ENTITY
        wb.properties.title = IDENT

    report = _inspect(_make(tmp_path, build))
    known = {(f.kind, f.core_property) for f in report.findings if f.location is FindingLocation.CORE_PROPERTY}
    assert (FindingKind.KNOWN_ENTITY_VALUE, CoreProperty.LAST_MODIFIED_BY) in known
    assert (FindingKind.KNOWN_IDENTIFIER_VALUE, CoreProperty.TITLE) in known
    assert (FindingKind.CORE_PROPERTY, CoreProperty.LAST_MODIFIED_BY) in known
    assert (FindingKind.CORE_PROPERTY, CoreProperty.TITLE) in known


def test_benign_creator_that_equals_known_value_is_still_blocking(tmp_path: Path) -> None:
    report = _inspect(_make(tmp_path), entities=("openpyxl",))
    assert report.count(FindingKind.KNOWN_ENTITY_VALUE) == 1
    assert report.count(FindingKind.CORE_PROPERTY) == 0
    assert report.findings[0].core_property is CoreProperty.CREATOR


def test_custom_properties_other_than_data_anonymizer_are_reported(tmp_path: Path) -> None:
    report = _inspect(
        _make(
            tmp_path,
            _with_props(
                ("Company.Dept", "v"),
                ("DataAnonymizer.JobId", "j"),
                ("DataAnonymizer.Other", "o"),
            ),
        )
    )
    assert _kinds(report) == {FindingKind.CUSTOM_PROPERTY: 1}


def test_workbook_and_sheet_defined_names(tmp_path: Path) -> None:
    def build(wb):
        wb.defined_names["WbName"] = DefinedName("WbName", attr_text="Sheet!$A$1")
        wb.active.defined_names["LocName"] = DefinedName("LocName", attr_text="Sheet!$B$1")

    report = _inspect(_make(tmp_path, build))
    locations = sorted(f.location.value for f in report.findings if f.kind is FindingKind.DEFINED_NAME)
    assert locations == ["workbook", "worksheet"]


def test_header_footer_one_finding_per_sheet_and_blank_ignored(tmp_path: Path) -> None:
    def build(wb):
        wb.active.oddHeader.center.text = "Конфиденциально"
        wb.active.oddFooter.left.text = "Страница"
        blank = wb.create_sheet("Blank")
        blank.oddHeader.center.text = "   "

    report = _inspect(_make(tmp_path, build))
    assert report.count(FindingKind.HEADER_FOOTER) == 1
    assert report.findings[0].worksheet_index == 1


def test_tables_are_reported_per_table(tmp_path: Path) -> None:
    def build(wb):
        ws = wb.active
        ws["A1"], ws["B1"] = "Колонка1", "Колонка2"
        ws["A2"], ws["B2"] = 1, 2
        ws.tables.add(Table(displayName="T1", ref="A1:B2"))

    report = _inspect(_make(tmp_path, build))
    assert report.count(FindingKind.TABLE) == 1
    assert report.findings[0].worksheet_index == 1


def test_real_chart_yields_chart_and_drawing_parts(tmp_path: Path) -> None:
    def build(wb):
        ws = wb.active
        ws["A1"], ws["A2"] = 1, 2
        chart = BarChart()
        chart.add_data(Reference(ws, min_col=1, min_row=1, max_row=2))
        ws.add_chart(chart, "C2")

    report = _inspect(_make(tmp_path, build))
    assert report.count(FindingKind.CHART) == 1
    assert report.count(FindingKind.DRAWING) == 1
    assert all(f.location is FindingLocation.PACKAGE for f in report.findings)


@pytest.mark.parametrize(
    ("part", "kind"),
    [
        ("xl/externalLinks/externalLink1.xml", FindingKind.EXTERNAL_LINK),
        ("xl/drawings/drawing9.xml", FindingKind.DRAWING),
        ("xl/charts/chart7.xml", FindingKind.CHART),
        ("xl/chartsheets/sheet1.xml", FindingKind.CHARTSHEET),
        ("xl/media/image1.png", FindingKind.MEDIA),
        ("xl/embeddings/oleObject1.bin", FindingKind.EMBEDDING),
        ("xl/pivotCache/pivotCacheDefinition1.xml", FindingKind.PIVOT),
        ("xl/pivotTables/pivotTable1.xml", FindingKind.PIVOT),
        ("customXml/item1.xml", FindingKind.CUSTOM_XML),
        ("xl/vbaProject.bin", FindingKind.VBA),
        ("xl/threadedComments/threadedComment1.xml", FindingKind.THREADED_COMMENT),
        ("xl/persons/person.xml", FindingKind.THREADED_COMMENT),
    ],
)
def test_package_part_kinds(tmp_path: Path, part: str, kind: FindingKind) -> None:
    path = _make(tmp_path)
    _add_parts(path, [part])
    report = _inspect(path)
    assert _kinds(report) == {kind: 1}
    assert report.findings[0].location is FindingLocation.PACKAGE


@pytest.mark.parametrize(
    "part",
    [
        "xl/drawings/vmlDrawing1.vml",
        "xl/drawings/commentsDrawing1.vml",
        "xl/drawings/_rels/drawing1.xml.rels",
        "xl/charts/style1.xml",
        "xl/charts/colors1.xml",
        "xl/externalLinks/_rels/externalLink1.xml.rels",
        "xl/comments/comment1.xml",
        "xl/printerSettings/printerSettings1.bin",
    ],
)
def test_package_parts_that_are_not_findings(tmp_path: Path, part: str) -> None:
    path = _make(tmp_path)
    _add_parts(path, [part])
    assert _inspect(path).finding_counts == ()


# ---------------------------------------------------------------------------
# I. Report
# ---------------------------------------------------------------------------


def test_report_is_deterministic_and_ordered(tmp_path: Path) -> None:
    def build(wb):
        ws = wb.active
        ws["A1"] = "=B1"
        ws["B1"] = ENTITY
        ws["A2"] = ENTITY
        second = wb.create_sheet("Second")
        second["A1"] = ENTITY

    path = _make(tmp_path, build)
    first = _inspect(path)
    second = _inspect(path)

    assert first == second
    order = list(FindingKind)
    assert [k for k, _ in first.finding_counts] == sorted((k for k, _ in first.finding_counts), key=order.index)
    entity_positions = [
        (f.worksheet_index, f.row, f.column)
        for f in first.findings
        if f.kind is FindingKind.KNOWN_ENTITY_VALUE
    ]
    assert entity_positions == [(1, 1, 2), (1, 2, 1), (2, 1, 1)]


def test_finding_counts_only_positive_and_exact(tmp_path: Path) -> None:
    def build(wb):
        wb.active["A1"] = ENTITY
        wb.active["A2"] = ENTITY

    report = _inspect(_make(tmp_path, build))
    assert report.finding_counts == ((FindingKind.KNOWN_ENTITY_VALUE, 2),)
    assert report.count(FindingKind.COMMENT) == 0


def test_finding_cap_keeps_exact_counts_and_sets_truncated(tmp_path: Path) -> None:
    total = MAX_FINDINGS_PER_KIND + 50

    def build(wb):
        ws = wb.active
        for row in range(1, total + 1):
            ws.cell(row=row, column=1, value=ENTITY)
            ws.cell(row=row, column=2, value="=A1")

    report = _inspect(_make(tmp_path, build))
    assert report.count(FindingKind.KNOWN_ENTITY_VALUE) == total
    assert report.count(FindingKind.FORMULA) == total
    per_kind = {kind: 0 for kind in FindingKind}
    for finding in report.findings:
        per_kind[finding.kind] += 1
    assert per_kind[FindingKind.KNOWN_ENTITY_VALUE] == MAX_FINDINGS_PER_KIND
    assert per_kind[FindingKind.FORMULA] == MAX_FINDINGS_PER_KIND
    assert report.findings_truncated is True


def test_no_truncation_below_cap(tmp_path: Path) -> None:
    def build(wb):
        wb.active["A1"] = ENTITY

    report = _inspect(_make(tmp_path, build))
    assert report.findings_truncated is False


def test_report_properties_and_fixed_limitations(tmp_path: Path) -> None:
    report = _inspect(_make(tmp_path))
    assert report.finding_counts == ()
    assert report.has_blocking_findings is False and report.has_unverified_exposures is False
    assert report.not_inspected == tuple(NotInspectedArea)
    assert set(report.not_inspected) == {
        NotInspectedArea.DATA_VALIDATION,
        NotInspectedArea.CONDITIONAL_FORMATTING,
        NotInspectedArea.EXTENDED_PROPERTIES,
        NotInspectedArea.SUBSTRING_MATCHES,
        NotInspectedArea.UNKNOWN_VALUES,
        NotInspectedArea.NON_TEXT_NON_INTEGER_CELLS,
    }
    assert not hasattr(report, "is_safe") and not hasattr(report, "can_upload")


def test_report_and_finding_are_immutable(tmp_path: Path) -> None:
    report = _inspect(_make(tmp_path, _with_props((ANALYTICAL_MARKER, "true"))))
    with pytest.raises(dataclasses.FrozenInstanceError):
        report.sha256 = "x"  # type: ignore[misc]
    with pytest.raises(dataclasses.FrozenInstanceError):
        report.findings[0].kind = FindingKind.COMMENT  # type: ignore[misc]


def test_report_sha256_matches_file_hash(tmp_path: Path) -> None:
    path = _make(tmp_path)
    assert _inspect(path).sha256 == sha256_file(path)


def test_severity_mapping_is_centralized_and_complete() -> None:
    assert set(SEVERITY_BY_KIND) == set(FindingKind)
    blocking = {kind for kind, severity in SEVERITY_BY_KIND.items() if severity is Severity.BLOCKING}
    assert blocking == {
        FindingKind.RESTORED_MARKER,
        FindingKind.KNOWN_ENTITY_VALUE,
        FindingKind.KNOWN_IDENTIFIER_VALUE,
    }


@pytest.mark.parametrize(
    "kwargs",
    [
        {"location": FindingLocation.CELL},
        {"location": FindingLocation.CELL, "worksheet_index": 1, "row": 1, "column": 0},
        {"location": FindingLocation.CELL, "worksheet_index": 1, "row": 1, "column": 1, "core_property": CoreProperty.TITLE},
        {"location": FindingLocation.SHEET_TITLE, "worksheet_index": 1, "row": 1},
        {"location": FindingLocation.WORKSHEET},
        {"location": FindingLocation.CORE_PROPERTY},
        {"location": FindingLocation.CORE_PROPERTY, "core_property": CoreProperty.TITLE, "worksheet_index": 1},
        {"location": FindingLocation.WORKBOOK, "worksheet_index": 1},
        {"location": FindingLocation.PACKAGE, "core_property": CoreProperty.TITLE},
        {"location": FindingLocation.CELL, "worksheet_index": True, "row": 1, "column": 1},
    ],
)
def test_finding_invariants_rejected(kwargs: dict) -> None:
    with pytest.raises(ValueError):
        SafetyFinding(kind=FindingKind.COMMENT, **kwargs)


def test_finding_valid_shapes_accepted() -> None:
    SafetyFinding(FindingKind.COMMENT, FindingLocation.CELL, 1, 2, 3)
    SafetyFinding(FindingKind.HIDDEN_WORKSHEET, FindingLocation.WORKSHEET, 2)
    SafetyFinding(FindingKind.CORE_PROPERTY, FindingLocation.CORE_PROPERTY, core_property=CoreProperty.TITLE)
    SafetyFinding(FindingKind.RESTORED_MARKER, FindingLocation.WORKBOOK)
    SafetyFinding(FindingKind.CHART, FindingLocation.PACKAGE)


# ---------------------------------------------------------------------------
# J. Гарантии
# ---------------------------------------------------------------------------


def test_source_bytes_and_directory_unchanged(tmp_path: Path) -> None:
    def build(wb):
        wb.active["A1"] = ENTITY
        wb.active["A2"] = "=A1"

    path = _make(tmp_path, build)
    bytes_before = path.read_bytes()
    listing_before = sorted(os.listdir(tmp_path))

    _inspect(path)

    assert path.read_bytes() == bytes_before
    assert sorted(os.listdir(tmp_path)) == listing_before


def test_stores_unchanged_and_never_mutated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def build(wb):
        wb.active["A1"] = ENTITY

    path = _make(tmp_path, build)
    entities = _entity_store(ENTITY)
    identifiers = _identifier_store(IDENT)
    entity_before = entities.entries()
    identifier_before = identifiers.entries()

    def forbidden(*args, **kwargs):
        raise AssertionError("inspection не должен мутировать store")

    for store in (entities, identifiers):
        for method in ("add", "clear"):
            monkeypatch.setattr(store, method, forbidden)
    monkeypatch.setattr(identifiers, "add_many", forbidden)

    inspect_workbook_for_external_ai(path, entities, identifiers)

    assert entities.entries() == entity_before
    assert identifiers.entries() == identifier_before


def test_store_entries_called_exactly_once_regardless_of_cell_count(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def build(wb):
        ws = wb.active
        for row in range(1, 501):
            ws.cell(row=row, column=1, value=f"value-{row}")

    path = _make(tmp_path, build)
    entities = _entity_store(ENTITY)
    identifiers = _identifier_store(IDENT)
    calls = {"entity": 0, "identifier": 0}
    real_entity_entries = entities.entries
    real_identifier_entries = identifiers.entries

    def counting_entity_entries():
        calls["entity"] += 1
        return real_entity_entries()

    def counting_identifier_entries():
        calls["identifier"] += 1
        return real_identifier_entries()

    monkeypatch.setattr(entities, "entries", counting_entity_entries)
    monkeypatch.setattr(identifiers, "entries", counting_identifier_entries)

    inspect_workbook_for_external_ai(path, entities, identifiers)

    assert calls == {"entity": 1, "identifier": 1}


def test_workbook_closed_on_success(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = _make(tmp_path)
    state = _spy_load_workbook(monkeypatch)
    _inspect(path)
    assert state["closes"] == 1


def test_workbook_closed_and_error_is_safe_when_zip_inventory_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = _make(tmp_path)
    state = _spy_load_workbook(monkeypatch)

    def failing_zipfile(*args, **kwargs):
        raise RuntimeError("CONFIDENTIAL_ZIP_TEXT Ромашка")

    monkeypatch.setattr(external_ai.zipfile, "ZipFile", failing_zipfile)

    with pytest.raises(ExternalAiInspectionError) as exc_info:
        _inspect(path)

    assert exc_info.value.reason is InspectionFailureReason.UNREADABLE_WORKBOOK
    assert state["closes"] == 1
    assert "CONFIDENTIAL_ZIP_TEXT" not in str(exc_info.value) + repr(exc_info.value)
    assert exc_info.value.__cause__ is None and exc_info.value.__suppress_context__ is True


def test_corrupt_workbook_is_wrapped_with_safe_message(tmp_path: Path) -> None:
    path = tmp_path / "secret-name.xlsx"
    path.write_bytes(b"this is not a zip file")

    with pytest.raises(ExternalAiInspectionError) as exc_info:
        _inspect(path)

    assert exc_info.value.reason is InspectionFailureReason.UNREADABLE_WORKBOOK
    assert "secret-name" not in str(exc_info.value)
    assert exc_info.value.__suppress_context__ is True


def test_foreign_load_exception_text_never_leaks(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = _make(tmp_path)

    def failing_load(*args, **kwargs):
        raise ValueError("CONFIDENTIAL_LEAF_TEXT")

    monkeypatch.setattr(openpyxl, "load_workbook", failing_load)

    with pytest.raises(ExternalAiInspectionError) as exc_info:
        _inspect(path)

    assert "CONFIDENTIAL_LEAF_TEXT" not in str(exc_info.value) + repr(exc_info.value)
    assert exc_info.value.__cause__ is None


def test_keyboard_interrupt_is_not_swallowed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = _make(tmp_path)

    def interrupting_load(*args, **kwargs):
        raise KeyboardInterrupt

    monkeypatch.setattr(openpyxl, "load_workbook", interrupting_load)

    with pytest.raises(KeyboardInterrupt):
        _inspect(path)


class _EntriesRaisingMappingStore(InMemoryMappingStore):
    def __init__(self, error: BaseException) -> None:
        super().__init__()
        self._error = error

    def entries(self):
        raise self._error


class _EntriesRaisingIdentifierStore(InMemoryIdentifierMappingStore):
    def __init__(self, error: BaseException) -> None:
        super().__init__()
        self._error = error

    def entries(self):
        raise self._error


def test_mapping_store_entries_error_propagates_as_same_object(tmp_path: Path) -> None:
    path = _make(tmp_path)
    sentinel = RuntimeError("SYNTHETIC_MAPPING_STORE_ERROR")

    with pytest.raises(RuntimeError) as exc_info:
        inspect_workbook_for_external_ai(path, _EntriesRaisingMappingStore(sentinel), None)

    # Именно тот же объект, а не просто тот же тип/сообщение: обёртка в
    # ExternalAiInspectionError (даже с сохранением текста) не допускается.
    assert exc_info.value is sentinel
    assert not isinstance(exc_info.value, ExternalAiSafetyError)


def test_identifier_store_entries_error_propagates_as_same_object(tmp_path: Path) -> None:
    path = _make(tmp_path)
    sentinel = RuntimeError("SYNTHETIC_IDENTIFIER_STORE_ERROR")

    with pytest.raises(RuntimeError) as exc_info:
        inspect_workbook_for_external_ai(
            path, _entity_store(ENTITY), _EntriesRaisingIdentifierStore(sentinel)
        )

    assert exc_info.value is sentinel
    assert not isinstance(exc_info.value, ExternalAiSafetyError)


@pytest.mark.parametrize("failing_call", [1, 2], ids=["hash_before", "hash_after"])
def test_sha256_io_error_through_inspect_propagates_as_same_object(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failing_call: int
) -> None:
    path = _make(tmp_path)
    sentinel = PermissionError("SYNTHETIC_LEAF_IO_ERROR")
    real_sha256_file = external_ai.sha256_file
    calls = {"n": 0}

    def failing_sha256_file(target):
        calls["n"] += 1
        if calls["n"] == failing_call:
            raise sentinel
        return real_sha256_file(target)

    monkeypatch.setattr(external_ai, "sha256_file", failing_sha256_file)

    with pytest.raises(PermissionError) as exc_info:
        _inspect(path)

    assert exc_info.value is sentinel
    assert not isinstance(exc_info.value, ExternalAiSafetyError)
    assert calls["n"] == failing_call


def test_file_changed_between_hashes_raises_and_returns_no_report(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = _make(tmp_path)
    real_sha256_file = external_ai.sha256_file
    calls = {"n": 0}

    def unstable_sha256_file(target):
        calls["n"] += 1
        digest = real_sha256_file(target)
        return digest if calls["n"] == 1 else "0" * 64

    monkeypatch.setattr(external_ai, "sha256_file", unstable_sha256_file)

    with pytest.raises(ExternalAiInspectionError) as exc_info:
        _inspect(path)

    assert exc_info.value.reason is InspectionFailureReason.FILE_CHANGED_DURING_INSPECTION
    assert calls["n"] == 2


def test_sparse_worksheet_is_not_densely_materialized(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def build(wb):
        wb.active["A1"] = "start"
        wb.active.cell(row=2000, column=26, value=ENTITY)  # dense = 52 000 ячеек

    path = _make(tmp_path, build)
    state = _spy_load_workbook(monkeypatch)

    report = _inspect(path)

    worksheet = state["loaded"][0].worksheets[0]
    assert worksheet.max_row == 2000 and worksheet.max_column == 26
    # dense iter_rows материализовал бы 52 000 объектов Cell в _cells.
    assert len(worksheet._cells) == 2
    finding = report.findings[0]
    assert (finding.row, finding.column) == (2000, 26)


def test_private_cells_contract_under_pinned_openpyxl(tmp_path: Path) -> None:
    assert openpyxl.__version__ == "3.1.5"
    workbook = openpyxl.load_workbook(_make(tmp_path))
    cells = workbook.worksheets[0]._cells
    assert isinstance(cells, dict)
    assert all(isinstance(key, tuple) and len(key) == 2 for key in cells)
    workbook.close()


@pytest.mark.parametrize("broken", [None, {"not-a-tuple": object()}])
def test_incompatible_cells_contract_fails_safely_without_dense_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, broken
) -> None:
    path = _make(tmp_path, lambda wb: wb.active.__setitem__("A1", ENTITY))
    real_load = openpyxl.load_workbook
    state = {"closes": 0}

    def load_with_broken_cells(*args, **kwargs):
        loaded = real_load(*args, **kwargs)
        loaded.worksheets[0]._cells = broken
        real_close = loaded.close

        def counting_close():
            state["closes"] += 1
            real_close()

        loaded.close = counting_close
        return loaded

    monkeypatch.setattr(openpyxl, "load_workbook", load_with_broken_cells)

    with pytest.raises(ExternalAiInspectionError) as exc_info:
        _inspect(path)

    assert exc_info.value.reason is InspectionFailureReason.UNREADABLE_WORKBOOK
    assert state["closes"] == 1


# ---------------------------------------------------------------------------
# K. Конфиденциальность
# ---------------------------------------------------------------------------


def test_report_and_findings_contain_no_confidential_values(tmp_path: Path) -> None:
    secrets = [
        "SECRET_FORMULA_TEXT",
        "SECRET_COMMENT_TEXT",
        "SECRET_URL_PATH",
        "SECRET_PROP_NAME",
        "SECRET_PROP_VALUE",
        "SECRET_CREATOR",
        "SECRET_SHEET_TITLE",
        "SECRET_ALIAS_X",
        "SECRET_TOKEN_X",
        "SECRET_HEADER_TEXT",
        "SECRET_NAMED_RANGE",
        ENTITY,
        IDENT,
    ]

    def build(wb):
        ws = wb.active
        ws.title = "SECRET_SHEET_TITLE"
        ws["A1"] = ENTITY
        ws["A2"] = IDENT
        ws["A3"] = "=SECRET_FORMULA_TEXT()"
        ws["A4"] = "x"
        ws["A4"].comment = Comment("SECRET_COMMENT_TEXT", "автор")
        ws["A5"] = "l"
        ws["A5"].hyperlink = "https://example.invalid/SECRET_URL_PATH"
        ws.oddHeader.center.text = "SECRET_HEADER_TEXT"
        wb.defined_names["SECRET_NAMED_RANGE"] = DefinedName("SECRET_NAMED_RANGE", attr_text="SECRET_SHEET_TITLE!$A$1")
        wb.properties.creator = "SECRET_CREATOR"
        wb.custom_doc_props.append(StringProperty(name="SECRET_PROP_NAME", value="SECRET_PROP_VALUE"))
        wb.custom_doc_props.append(StringProperty(name=ANALYTICAL_MARKER, value="SECRET_PROP_VALUE"))

    path = _make(tmp_path, build)
    entities = InMemoryMappingStore()
    entities.add(MappingEntry(alias="SECRET_ALIAS_X", real_value=ENTITY, entity_type=EntityType.COMPANY))
    identifiers = InMemoryIdentifierMappingStore()
    identifiers.add(
        IdentifierMappingEntry(token="SECRET_TOKEN_X", identifier_value=IDENT, identifier_type=IdentifierType.INN)
    )

    report = inspect_workbook_for_external_ai(path, entities, identifiers)

    rendered = repr(report) + " ".join(repr(f) + str(f) for f in report.findings)
    for secret in secrets:
        assert secret not in rendered
    assert report.has_blocking_findings and report.has_unverified_exposures


def test_input_errors_do_not_contain_workbook_or_store_content(tmp_path: Path) -> None:
    path = tmp_path / "w.txt"
    path.write_bytes(b"x")
    entities = _entity_store(ENTITY)
    with pytest.raises(ExternalAiInspectionInputError) as exc_info:
        inspect_workbook_for_external_ai(path, entities, _identifier_store(IDENT))
    message = str(exc_info.value)
    assert ENTITY not in message and IDENT not in message


def test_package_part_names_never_appear_in_report_or_findings(tmp_path: Path) -> None:
    sentinel = "SECRET_PART_NAME_9C71"
    path = _make(tmp_path)
    _add_parts(
        path,
        [
            f"xl/media/{sentinel}.png",
            f"customXml/{sentinel}.xml",
            f"xl/embeddings/{sentinel}.bin",
            f"xl/externalLinks/{sentinel}.xml",
        ],
    )

    report = _inspect(path)

    # Не вакуумно: finding'и по этим частям реально созданы.
    assert _kinds(report) == {
        FindingKind.EXTERNAL_LINK: 1,
        FindingKind.MEDIA: 1,
        FindingKind.EMBEDDING: 1,
        FindingKind.CUSTOM_XML: 1,
    }
    assert len(report.findings) == 4

    rendered = [repr(report), str(report), repr(report.findings), str(report.findings)]
    for finding in report.findings:
        rendered.extend([repr(finding), str(finding)])
    for text in rendered:
        assert sentinel not in text


def test_safety_finding_schema_lock() -> None:
    # Намеренный schema-lock security-границы: у SafetyFinding не должно
    # появляться payload-полей (detail/value/name/text/path/part_name/url/
    # message и т.п.) — только перечисления и координаты.
    assert [field.name for field in dataclasses.fields(SafetyFinding)] == [
        "kind",
        "location",
        "worksheet_index",
        "row",
        "column",
        "core_property",
    ]


def test_production_module_has_no_forbidden_side_effect_calls() -> None:
    source = Path(external_ai.__file__).read_text(encoding="utf-8")
    for forbidden in ("print(", "logging", ".save(", "os.replace", "os.rename", "shutil"):
        assert forbidden not in source
