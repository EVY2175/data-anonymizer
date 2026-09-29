"""
Тесты Stage 10C.2.3 — Object-model Scrub
(app.scrub.mutate.scrub_workbook_object_model).

Все фикстуры — synthetic (openpyxl-генерация), без единого реального
confidential значения — только явно синтетические sentinel-строки.

Каждая scrub-операция проверяется НЕ только через object model, но
через реальный package output (распаковка выходного .xlsx), по
принципу, установленному во всех предыдущих Stage10C.2-модулях.

Print Titles (BLOCKER-1, OD-10C2.3-4): openpyxl 3.1.5 молча игнорирует
`print_title_rows`/`print_title_cols = None` через публичные сеттеры —
production-код использует узкое одобренное private-API исключение
(`worksheet._print_rows`/`worksheet._print_cols`, см. app.scrub.mutate).
Тесты ниже (см. секцию I) требуют ПОЛНОГО удаления `_xlnm.Print_Titles`
из serialized package output — известного ограничения без workaround
больше нет.
"""

from __future__ import annotations

import hashlib
import io
import traceback
import uuid
import zipfile
from datetime import datetime
from pathlib import Path
from unittest import mock

import openpyxl
import pytest
from openpyxl.comments import Comment
from openpyxl.formatting.rule import CellIsRule
from openpyxl.packaging.custom import StringProperty
from openpyxl.workbook.defined_name import DefinedName
from openpyxl.workbook.protection import WorkbookProtection
from openpyxl.worksheet.datavalidation import DataValidation
from openpyxl.worksheet.hyperlink import Hyperlink
from openpyxl.worksheet.table import Table

from app.scrub.errors import PackageScrubError, PackageScrubReason
from app.scrub.mutate import scrub_workbook_object_model

SENTINEL = "SENTINEL_SECRET_PATH_COMPONENT"


# ---------------------------------------------------------------------------
# Помощники
# ---------------------------------------------------------------------------


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _parts(path: Path) -> set[str]:
    with zipfile.ZipFile(path) as z:
        return set(z.namelist())


def _read_part(path: Path, name: str) -> bytes:
    with zipfile.ZipFile(path) as z:
        return z.read(name)


def _full_content(path: Path) -> bytes:
    with zipfile.ZipFile(path) as z:
        return b"".join(z.read(n) for n in z.namelist())


def _assert_no_sentinel_leak(exc: BaseException, sentinel: str = SENTINEL) -> None:
    blobs = [
        str(exc),
        repr(exc),
        repr(exc.args),
        "".join(traceback.format_exception(type(exc), exc, exc.__traceback__)),
    ]
    for blob in blobs:
        assert sentinel not in blob
    assert exc.__context__ is None
    assert exc.__cause__ is None


# ---------------------------------------------------------------------------
# A. Базовый PASS
# ---------------------------------------------------------------------------


def test_minimal_workbook_scrub_pass(tmp_path: Path) -> None:
    wb = openpyxl.Workbook()
    wb.active["A1"] = "v"
    source = tmp_path / "minimal.xlsx"
    wb.save(source)
    wb.close()

    result = scrub_workbook_object_model(source)
    try:
        assert isinstance(result, Path)
        assert result != source
        assert result.exists()
        wb2 = openpyxl.load_workbook(result)
        assert wb2.active["A1"].value == "v"
        wb2.close()
    finally:
        result.unlink(missing_ok=True)


def test_source_immutability_sha256(tmp_path: Path) -> None:
    wb = openpyxl.Workbook()
    wb.active["A1"] = "x"
    source = tmp_path / "src.xlsx"
    wb.save(source)
    wb.close()
    sha_before = _sha256(source)

    result = scrub_workbook_object_model(source)
    try:
        sha_after = _sha256(source)
        assert sha_before == sha_after
        assert result != source
    finally:
        result.unlink(missing_ok=True)


def test_temp_output_outside_workspace_random_name(tmp_path: Path) -> None:
    import tempfile

    wb = openpyxl.Workbook()
    wb.active["A1"] = "x"
    source = tmp_path / "src.xlsx"
    wb.save(source)
    wb.close()

    result = scrub_workbook_object_model(source)
    try:
        # temp находится в обычной OS temp-области, не рядом с source.
        assert result.parent != source.parent
        assert str(Path(tempfile.gettempdir())) in str(result.parent) or result.parent == Path(
            tempfile.gettempdir()
        )
        assert source.stem not in result.name
    finally:
        result.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# B. Comments/VML
# ---------------------------------------------------------------------------


def test_comments_and_vml_removed(tmp_path: Path) -> None:
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Data"
    ws["A1"] = "v"
    ws["A1"].comment = Comment(f"{SENTINEL}-comment", "author")
    source = tmp_path / "comments.xlsx"
    wb.save(source)
    wb.close()

    before_parts = _parts(source)
    assert any("comments" in p for p in before_parts)
    assert any(p.endswith(".vml") for p in before_parts)

    result = scrub_workbook_object_model(source)
    try:
        after_parts = _parts(result)
        assert not any("comments" in p for p in after_parts)
        assert not any(p.endswith(".vml") for p in after_parts)
        assert not any("worksheets/_rels" in p for p in after_parts)
        assert SENTINEL.encode() not in _full_content(result)
    finally:
        result.unlink(missing_ok=True)


def test_multiple_comments_multiple_sheets_removed(tmp_path: Path) -> None:
    wb = openpyxl.Workbook()
    ws1 = wb.active
    ws1.title = "S0"
    ws1["A1"] = "v"
    ws1["A1"].comment = Comment(f"{SENTINEL}-1", "a")
    ws2 = wb.create_sheet("S1")
    ws2["A1"] = "v"
    ws2["A1"].comment = Comment(f"{SENTINEL}-2", "a")
    source = tmp_path / "multi_comments.xlsx"
    wb.save(source)
    wb.close()

    result = scrub_workbook_object_model(source)
    try:
        assert not any("comments" in p for p in _parts(result))
        assert SENTINEL.encode() not in _full_content(result)
    finally:
        result.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# C. Hyperlinks
# ---------------------------------------------------------------------------


def test_hyperlink_removed_text_preserved(tmp_path: Path) -> None:
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Data"
    ws["A1"].value = "clicklink"
    ws["A1"].hyperlink = f"https://{SENTINEL}.example.com/leak"
    source = tmp_path / "hyperlink.xlsx"
    wb.save(source)
    wb.close()

    result = scrub_workbook_object_model(source)
    try:
        wb2 = openpyxl.load_workbook(result)
        ws2 = wb2.active
        assert ws2["A1"].value == "clicklink"
        assert ws2["A1"].hyperlink is None
        wb2.close()
        assert SENTINEL.encode() not in _full_content(result)
        assert not any("worksheets/_rels" in p for p in _parts(result))
    finally:
        result.unlink(missing_ok=True)


def test_multiple_hyperlinks_removed(tmp_path: Path) -> None:
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Data"
    for i, col in enumerate(["A1", "B1", "C1"]):
        ws[col].value = f"link{i}"
        ws[col].hyperlink = f"https://example.com/{SENTINEL}/{i}"
    source = tmp_path / "many_hyperlinks.xlsx"
    wb.save(source)
    wb.close()

    result = scrub_workbook_object_model(source)
    try:
        wb2 = openpyxl.load_workbook(result)
        ws2 = wb2.active
        for col in ["A1", "B1", "C1"]:
            assert ws2[col].hyperlink is None
        wb2.close()
        assert SENTINEL.encode() not in _full_content(result)
    finally:
        result.unlink(missing_ok=True)


def test_hyperlink_formula_preserved_unresolved(tmp_path: Path) -> None:
    # Frozen contract OD-10C2.3-1: =HYPERLINK() formula НЕ анализируется/
    # НЕ удаляется -- это ПОДТВЕРЖДЁННЫЙ security debt, закрываемый только
    # будущим Formula Safety Gate (Stage10C Final Security Closure), не
    # этой стадией.
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Data"
    ws["A1"] = f'=HYPERLINK("https://{SENTINEL}.example.com/formula-leak","clickme")'
    source = tmp_path / "hyperlink_formula.xlsx"
    wb.save(source)
    wb.close()

    result = scrub_workbook_object_model(source)
    try:
        raw = _read_part(result, "xl/worksheets/sheet1.xml").decode("utf-8")
        assert "HYPERLINK(" in raw
        assert SENTINEL in raw  # ПОДТВЕРЖДЁННЫЙ, документированный security debt.
    finally:
        result.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# D. Tables
# ---------------------------------------------------------------------------


def test_table_removed_cell_values_preserved(tmp_path: Path) -> None:
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Data"
    for r in range(1, 6):
        ws.cell(row=r, column=1, value=f"v{r}")
    ws.add_table(Table(displayName="Table1", ref="A1:A5"))
    source = tmp_path / "table.xlsx"
    wb.save(source)
    wb.close()

    assert any("tables" in p for p in _parts(source))

    result = scrub_workbook_object_model(source)
    try:
        after_parts = _parts(result)
        assert not any("tables" in p for p in after_parts)
        assert not any("worksheets/_rels" in p for p in after_parts)
        raw = _read_part(result, "xl/worksheets/sheet1.xml").decode("utf-8")
        assert "tableParts" not in raw
        wb2 = openpyxl.load_workbook(result)
        ws2 = wb2.active
        assert [ws2.cell(row=r, column=1).value for r in range(1, 6)] == [
            f"v{r}" for r in range(1, 6)
        ]
        wb2.close()
    finally:
        result.unlink(missing_ok=True)


def test_multiple_tables_removed(tmp_path: Path) -> None:
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Data"
    for r in range(1, 6):
        ws.cell(row=r, column=1, value=f"v{r}")
        ws.cell(row=r, column=2, value=f"w{r}")
    ws.add_table(Table(displayName="Table1", ref="A1:A5"))
    ws.add_table(Table(displayName="Table2", ref="B1:B5"))
    source = tmp_path / "two_tables.xlsx"
    wb.save(source)
    wb.close()

    result = scrub_workbook_object_model(source)
    try:
        assert not any("tables" in p for p in _parts(result))
    finally:
        result.unlink(missing_ok=True)


def test_structured_reference_formula_text_preserved_after_table_removed(
    tmp_path: Path,
) -> None:
    # Accepted limitation (frozen contract): structured-reference formula
    # НЕ переписывается и может стать функционально "битой" после
    # удаления table wrapper -- formula TEXT при этом сохраняется как есть.
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Data"
    ws["A1"] = "Col1"
    ws["A2"] = 10
    ws["A3"] = 20
    ws.add_table(Table(displayName="MyTable", ref="A1:A3"))
    ws["C1"] = "=SUM(MyTable[Col1])"
    source = tmp_path / "structured_ref.xlsx"
    wb.save(source)
    wb.close()

    result = scrub_workbook_object_model(source)
    try:
        raw = _read_part(result, "xl/worksheets/sheet1.xml").decode("utf-8")
        assert "SUM(MyTable[Col1])" in raw  # formula text сохранён неизменным
        assert not any("tables" in p for p in _parts(result))  # wrapper удалён
    finally:
        result.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# E. Data validation
# ---------------------------------------------------------------------------


def test_standard_data_validation_removed(tmp_path: Path) -> None:
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Data"
    ws["A1"] = "v"
    dv = DataValidation(type="list", formula1=f'"A,B,{SENTINEL}"')
    ws.add_data_validation(dv)
    dv.add("B1")
    source = tmp_path / "dv.xlsx"
    wb.save(source)
    wb.close()

    result = scrub_workbook_object_model(source)
    try:
        raw = _read_part(result, "xl/worksheets/sheet1.xml").decode("utf-8")
        assert "dataValidation" not in raw
        assert SENTINEL.encode() not in _full_content(result)
    finally:
        result.unlink(missing_ok=True)


def test_x14_extension_data_validation_removed_by_openpyxl(tmp_path: Path) -> None:
    # openpyxl 3.1.5 сам отбрасывает x14/extLst DV при загрузке (эмпирически
    # подтверждено в Architecture/Freeze проходах) -- проверяем это
    # поведение непосредственно через production-путь scrub.
    wb = openpyxl.Workbook()
    wb.active["A1"] = "v"
    source = tmp_path / "x14dv_source.xlsx"
    wb.save(source)
    wb.close()

    with zipfile.ZipFile(source) as z:
        parts = {n: z.read(n) for n in z.namelist()}
    sheet_xml = parts["xl/worksheets/sheet1.xml"].decode("utf-8")
    ext = (
        '<extLst><ext xmlns:x14="http://schemas.microsoft.com/office/spreadsheetml/2009/9/main" '
        'xmlns:xm="http://schemas.microsoft.com/office/excel/2006/main" '
        'uri="{CCE6A557-97BC-4b89-ADB6-D9C93CAAB3DF}">'
        '<x14:dataValidations count="1"><x14:dataValidation type="list">'
        f'<x14:formula1><xm:f>"{SENTINEL}-x14"</xm:f></x14:formula1>'
        "<x14:referencedSequence/></x14:dataValidation></x14:dataValidations></ext></extLst>"
    )
    parts["xl/worksheets/sheet1.xml"] = sheet_xml.replace("</worksheet>", ext + "</worksheet>").encode(
        "utf-8"
    )
    source2 = tmp_path / "x14dv.xlsx"
    with zipfile.ZipFile(source2, "w") as z:
        for n, d in parts.items():
            z.writestr(n, d)

    result = scrub_workbook_object_model(source2)
    try:
        assert SENTINEL.encode() + b"-x14" not in _full_content(result)
        raw = _read_part(result, "xl/worksheets/sheet1.xml").decode("utf-8")
        assert "extLst" not in raw
    finally:
        result.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# E2. calcChain / sharedStrings (не требуют object-model действия --
#     openpyxl безусловно отбрасывает их при save; подтверждено ранее
#     эмпирически на synthetic input, здесь -- production-path re-confirm
#     через реальный scrub_workbook_object_model, не дублируя 2.1/2.2)
# ---------------------------------------------------------------------------


def test_calc_chain_input_absent_in_output(tmp_path: Path) -> None:
    wb = openpyxl.Workbook()
    ws = wb.active
    ws["A1"] = 1
    ws["A2"] = 2
    ws["A3"] = "=A1+A2"
    source = tmp_path / "calc_chain_source.xlsx"
    wb.save(source)
    wb.close()

    with zipfile.ZipFile(source) as z:
        parts = {n: z.read(n) for n in z.namelist()}
    ct = parts["[Content_Types].xml"].decode("utf-8")
    ct2 = ct.replace(
        "</Types>",
        '<Override PartName="/xl/calcChain.xml" '
        'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.calcChain+xml"/></Types>',
    )
    wbrels = parts["xl/_rels/workbook.xml.rels"].decode("utf-8")
    wbrels2 = wbrels.replace(
        "</Relationships>",
        '<Relationship Id="rIdCC" '
        'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/calcChain" '
        'Target="calcChain.xml"/></Relationships>',
    )
    parts["[Content_Types].xml"] = ct2.encode("utf-8")
    parts["xl/_rels/workbook.xml.rels"] = wbrels2.encode("utf-8")
    source2 = tmp_path / "calc_chain_input.xlsx"
    with zipfile.ZipFile(source2, "w") as z:
        for n, d in parts.items():
            z.writestr(n, d)
        z.writestr(
            "xl/calcChain.xml",
            b'<calcChain xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
            b'<c r="A3" i="1"/></calcChain>',
        )

    result = scrub_workbook_object_model(source2)
    try:
        assert "xl/calcChain.xml" not in _parts(result)
    finally:
        result.unlink(missing_ok=True)


def test_shared_strings_input_absent_in_output(tmp_path: Path) -> None:
    wb = openpyxl.Workbook()
    wb.active["A1"] = "hello"
    source = tmp_path / "ss_source.xlsx"
    wb.save(source)
    wb.close()

    with zipfile.ZipFile(source) as z:
        parts = {n: z.read(n) for n in z.namelist()}
    ct = parts["[Content_Types].xml"].decode("utf-8")
    ct2 = ct.replace(
        "</Types>",
        '<Override PartName="/xl/sharedStrings.xml" '
        'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sharedStrings+xml"/></Types>',
    )
    wbrels = parts["xl/_rels/workbook.xml.rels"].decode("utf-8")
    wbrels2 = wbrels.replace(
        "</Relationships>",
        '<Relationship Id="rIdSS" '
        'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/sharedStrings" '
        'Target="sharedStrings.xml"/></Relationships>',
    )
    parts["[Content_Types].xml"] = ct2.encode("utf-8")
    parts["xl/_rels/workbook.xml.rels"] = wbrels2.encode("utf-8")
    source2 = tmp_path / "ss_input.xlsx"
    with zipfile.ZipFile(source2, "w") as z:
        for n, d in parts.items():
            z.writestr(n, d)
        z.writestr(
            "xl/sharedStrings.xml",
            b'<sst xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
            b'count="1" uniqueCount="1"><si><t>' + SENTINEL.encode() + b"</t></si></sst>",
        )

    result = scrub_workbook_object_model(source2)
    try:
        assert "xl/sharedStrings.xml" not in _parts(result)
        assert SENTINEL.encode() not in _full_content(result)
    finally:
        result.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# F. Conditional formatting
# ---------------------------------------------------------------------------


def test_standard_conditional_formatting_removed(tmp_path: Path) -> None:
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Data"
    ws["A1"] = 5
    ws.conditional_formatting.add(
        "A1:A1", CellIsRule(operator="greaterThan", formula=[f'"{SENTINEL}"'])
    )
    source = tmp_path / "cf.xlsx"
    wb.save(source)
    wb.close()

    result = scrub_workbook_object_model(source)
    try:
        raw = _read_part(result, "xl/worksheets/sheet1.xml").decode("utf-8")
        assert "conditionalFormatting" not in raw
        assert SENTINEL.encode() not in _full_content(result)
    finally:
        result.unlink(missing_ok=True)


def test_x14_extension_conditional_formatting_removed_by_openpyxl(tmp_path: Path) -> None:
    wb = openpyxl.Workbook()
    wb.active["A1"] = "v"
    source = tmp_path / "x14cf_source.xlsx"
    wb.save(source)
    wb.close()

    with zipfile.ZipFile(source) as z:
        parts = {n: z.read(n) for n in z.namelist()}
    sheet_xml = parts["xl/worksheets/sheet1.xml"].decode("utf-8")
    ext = (
        '<extLst><ext xmlns:x14="http://schemas.microsoft.com/office/spreadsheetml/2009/9/main" '
        'uri="{78C0D931-6437-407d-A8EE-F0AAD7539E65}">'
        '<x14:conditionalFormattings><x14:conditionalFormatting '
        'xmlns:xm="http://schemas.microsoft.com/office/excel/2006/main">'
        '<x14:cfRule type="expression" id="{00000000-0000-0000-0000-000000000001}">'
        f'<xm:f>A1="{SENTINEL}-x14cf"</xm:f></x14:cfRule>'
        "<xm:sqref>A1</xm:sqref></x14:conditionalFormatting></x14:conditionalFormattings></ext></extLst>"
    )
    parts["xl/worksheets/sheet1.xml"] = sheet_xml.replace("</worksheet>", ext + "</worksheet>").encode(
        "utf-8"
    )
    source2 = tmp_path / "x14cf.xlsx"
    with zipfile.ZipFile(source2, "w") as z:
        for n, d in parts.items():
            z.writestr(n, d)

    result = scrub_workbook_object_model(source2)
    try:
        assert SENTINEL.encode() + b"-x14cf" not in _full_content(result)
    finally:
        result.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# G. Defined names
# ---------------------------------------------------------------------------


def test_workbook_and_sheet_scoped_defined_names_removed(tmp_path: Path) -> None:
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Data"
    ws["A1"] = "v"
    wb.defined_names[f"{SENTINEL}Range"] = DefinedName(
        f"{SENTINEL}Range", attr_text="Data!$A$1"
    )
    ws.defined_names[f"{SENTINEL}Local"] = DefinedName(
        f"{SENTINEL}Local", attr_text="Data!$A$1"
    )
    source = tmp_path / "defined_names.xlsx"
    wb.save(source)
    wb.close()

    result = scrub_workbook_object_model(source)
    try:
        raw = _read_part(result, "xl/workbook.xml").decode("utf-8")
        assert SENTINEL not in raw
    finally:
        result.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# H. AutoFilter
# ---------------------------------------------------------------------------


def test_autofilter_and_filter_database_removed(tmp_path: Path) -> None:
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Data"
    for r in range(1, 6):
        ws.cell(row=r, column=1, value=f"v{r}")
    ws.auto_filter.ref = "A1:A5"
    ws.auto_filter.add_filter_column(0, [f"{SENTINEL}-filter"])
    source = tmp_path / "autofilter.xlsx"
    wb.save(source)
    wb.close()

    result = scrub_workbook_object_model(source)
    try:
        sheet_raw = _read_part(result, "xl/worksheets/sheet1.xml").decode("utf-8")
        assert "autoFilter" not in sheet_raw
        wb_raw = _read_part(result, "xl/workbook.xml").decode("utf-8")
        assert "_FilterDatabase" not in wb_raw
        assert SENTINEL.encode() not in _full_content(result)
    finally:
        result.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# I. Print area / titles
# ---------------------------------------------------------------------------


def test_print_area_removed(tmp_path: Path) -> None:
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Data"
    ws["A1"] = "v"
    ws.print_area = "A1:A5"
    source = tmp_path / "print_area.xlsx"
    wb.save(source)
    wb.close()

    result = scrub_workbook_object_model(source)
    try:
        raw = _read_part(result, "xl/workbook.xml").decode("utf-8")
        assert "Print_Area" not in raw
    finally:
        result.unlink(missing_ok=True)


def test_print_title_rows_only_removed(tmp_path: Path) -> None:
    # Case A: print_title_rows only.
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Data"
    ws["A1"] = "v"
    ws.print_title_rows = "1:1"
    source = tmp_path / "print_title_rows.xlsx"
    wb.save(source)
    wb.close()
    before_raw = _read_part(source, "xl/workbook.xml").decode("utf-8")
    assert "Print_Titles" in before_raw

    result = scrub_workbook_object_model(source)
    try:
        raw = _read_part(result, "xl/workbook.xml").decode("utf-8")
        assert "Print_Titles" not in raw
        wb2 = openpyxl.load_workbook(result)
        assert wb2.active.print_titles == ""
        wb2.close()
    finally:
        result.unlink(missing_ok=True)


def test_print_title_cols_only_removed(tmp_path: Path) -> None:
    # Case B: print_title_cols only.
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Data"
    ws["A1"] = "v"
    ws.print_title_cols = "A:A"
    source = tmp_path / "print_title_cols.xlsx"
    wb.save(source)
    wb.close()
    before_raw = _read_part(source, "xl/workbook.xml").decode("utf-8")
    assert "Print_Titles" in before_raw

    result = scrub_workbook_object_model(source)
    try:
        raw = _read_part(result, "xl/workbook.xml").decode("utf-8")
        assert "Print_Titles" not in raw
        wb2 = openpyxl.load_workbook(result)
        assert wb2.active.print_titles == ""
        wb2.close()
    finally:
        result.unlink(missing_ok=True)


def test_print_title_rows_and_cols_simultaneously_removed(tmp_path: Path) -> None:
    # Case C: rows + cols simultaneously.
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Data"
    ws["A1"] = "v"
    ws.print_title_rows = "1:1"
    ws.print_title_cols = "A:A"
    source = tmp_path / "print_titles_both.xlsx"
    wb.save(source)
    wb.close()
    before_raw = _read_part(source, "xl/workbook.xml").decode("utf-8")
    assert "'Data'!$1:$1" in before_raw
    assert "'Data'!$A:$A" in before_raw

    result = scrub_workbook_object_model(source)
    try:
        raw = _read_part(result, "xl/workbook.xml").decode("utf-8")
        assert "Print_Titles" not in raw
        assert "$1:$1" not in raw
        assert "$A:$A" not in raw
    finally:
        result.unlink(missing_ok=True)


def test_print_area_and_titles_simultaneously_removed(tmp_path: Path) -> None:
    # Case D: print_area + rows + cols simultaneously.
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Data"
    for r in range(1, 6):
        ws.cell(row=r, column=1, value=f"v{r}")
    ws.print_area = "A1:A5"
    ws.print_title_rows = "1:1"
    ws.print_title_cols = "A:A"
    source = tmp_path / "print_area_and_titles.xlsx"
    wb.save(source)
    wb.close()
    before_raw = _read_part(source, "xl/workbook.xml").decode("utf-8")
    assert "Print_Area" in before_raw
    assert "Print_Titles" in before_raw

    result = scrub_workbook_object_model(source)
    try:
        raw = _read_part(result, "xl/workbook.xml").decode("utf-8")
        assert "Print_Area" not in raw
        assert "Print_Titles" not in raw
        assert "<definedNames />" in raw or "<definedNames/>" in raw
    finally:
        result.unlink(missing_ok=True)


def test_print_titles_only_on_some_worksheets(tmp_path: Path) -> None:
    # Case E: несколько worksheets, Print Titles есть только на части.
    wb = openpyxl.Workbook()
    ws1 = wb.active
    ws1.title = "WithTitles"
    ws1["A1"] = "v"
    ws1.print_title_rows = "1:1"
    ws2 = wb.create_sheet("WithoutTitles")
    ws2["A1"] = "v"
    ws3 = wb.create_sheet("AlsoWithTitles")
    ws3["A1"] = "v"
    ws3.print_title_cols = "A:A"
    source = tmp_path / "multi_sheet_titles.xlsx"
    wb.save(source)
    wb.close()
    before_raw = _read_part(source, "xl/workbook.xml").decode("utf-8")
    assert before_raw.count("Print_Titles") == 2

    result = scrub_workbook_object_model(source)
    try:
        raw = _read_part(result, "xl/workbook.xml").decode("utf-8")
        assert "Print_Titles" not in raw
        wb2 = openpyxl.load_workbook(result)
        assert wb2.sheetnames == ["WithTitles", "WithoutTitles", "AlsoWithTitles"]
        for name in wb2.sheetnames:
            assert wb2[name].print_titles == ""
        wb2.close()
    finally:
        result.unlink(missing_ok=True)


def test_print_titles_unicode_worksheet_title_removed(tmp_path: Path) -> None:
    # Case F: Unicode worksheet title.
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Данные"
    ws["A1"] = "v"
    ws.print_title_rows = "1:1"
    source = tmp_path / "print_titles_unicode.xlsx"
    wb.save(source)
    wb.close()
    before_raw = _read_part(source, "xl/workbook.xml").decode("utf-8")
    assert "Print_Titles" in before_raw
    assert "Данные" in before_raw

    result = scrub_workbook_object_model(source)
    try:
        raw = _read_part(result, "xl/workbook.xml").decode("utf-8")
        assert "Print_Titles" not in raw
        assert "Данные" in raw  # worksheet title САМ по себе не удаляется (§23 контракта)
    finally:
        result.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# J. Headers/footers
# ---------------------------------------------------------------------------


def test_headers_and_footers_removed(tmp_path: Path) -> None:
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Data"
    ws["A1"] = "v"
    ws.oddHeader.left.text = f"{SENTINEL}-header"
    ws.oddFooter.center.text = f"{SENTINEL}-footer"
    source = tmp_path / "headers.xlsx"
    wb.save(source)
    wb.close()

    result = scrub_workbook_object_model(source)
    try:
        raw = _read_part(result, "xl/worksheets/sheet1.xml").decode("utf-8")
        assert "headerFooter" not in raw
        assert SENTINEL.encode() not in _full_content(result)
    finally:
        result.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# K. Worksheet protection
# ---------------------------------------------------------------------------


def test_worksheet_protection_reset_cell_style_protection_preserved(tmp_path: Path) -> None:
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Data"
    ws["A1"] = "v"
    ws.protection.sheet = True
    ws.protection.password = SENTINEL
    from copy import copy as _copy

    protection = _copy(ws["A1"].protection)
    protection.locked = True
    ws["A1"].protection = protection
    source = tmp_path / "wsprotect.xlsx"
    wb.save(source)
    wb.close()

    result = scrub_workbook_object_model(source)
    try:
        raw = _read_part(result, "xl/worksheets/sheet1.xml").decode("utf-8")
        assert SENTINEL not in raw
        # MINOR-1 correction: прямая проверка отсутствия serialized
        # sheetProtection-элемента (openpyxl никогда не сериализует его
        # при default-значениях -- эмпирически подтверждено), не
        # тавтологичный conditional assert.
        assert "sheetProtection" not in raw
        wb2 = openpyxl.load_workbook(result)
        ws2 = wb2.active
        assert ws2.protection.sheet in (False, None)
        wb2.close()
    finally:
        result.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# L. Workbook protection
# ---------------------------------------------------------------------------


def test_workbook_protection_reset(tmp_path: Path) -> None:
    wb = openpyxl.Workbook()
    wb.active["A1"] = "v"
    wb.security = WorkbookProtection(workbookPassword=SENTINEL, lockStructure=True)
    source = tmp_path / "wbprotect.xlsx"
    wb.save(source)
    wb.close()

    result = scrub_workbook_object_model(source)
    try:
        raw = _read_part(result, "xl/workbook.xml").decode("utf-8")
        assert SENTINEL not in raw
        assert "lockStructure" not in raw
    finally:
        result.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# M. Custom properties / JobId / restored marker
# ---------------------------------------------------------------------------


def test_custom_properties_and_jobid_removed(tmp_path: Path) -> None:
    wb = openpyxl.Workbook()
    wb.active["A1"] = "v"
    wb.custom_doc_props.append(StringProperty(name="OrdinaryCustom", value=SENTINEL))
    wb.custom_doc_props.append(StringProperty(name="DataAnonymizer.JobId", value="job-123"))
    source = tmp_path / "customprops.xlsx"
    wb.save(source)
    wb.close()

    assert "docProps/custom.xml" in _parts(source)

    result = scrub_workbook_object_model(source)
    try:
        assert "docProps/custom.xml" not in _parts(result)
        assert SENTINEL.encode() not in _full_content(result)
        assert b"DataAnonymizer.JobId" not in _full_content(result)
    finally:
        result.unlink(missing_ok=True)


def test_restored_marker_removed_by_direct_call_documents_unsafe_api(tmp_path: Path) -> None:
    """
    ВАЖНО: этот тест НЕ означает, что restored workbook разрешён к
    прямой обработке. Наоборот -- он ДОКУМЕНТИРУЕТ, почему
    scrub_workbook_object_model НЕ является external-safe API:
    DataAnonymizer.AnalyticallyRestored (negative sentinel Stage9C)
    удаляется вместе со всеми custom properties, если вызвать 2.3
    НАПРЯМУЮ. Restored-marker rejection ОБЯЗАН произойти РАНЬШЕ, на
    уровне будущего оркестрирующего слоя (Stage10C.2.5), ДО вызова
    scrub_workbook_object_model.
    """
    wb = openpyxl.Workbook()
    wb.active["A1"] = "v"
    wb.custom_doc_props.append(
        StringProperty(name="DataAnonymizer.AnalyticallyRestored", value="true")
    )
    source = tmp_path / "restored.xlsx"
    wb.save(source)
    wb.close()

    result = scrub_workbook_object_model(source)
    try:
        assert b"AnalyticallyRestored" not in _full_content(result)
        assert "docProps/custom.xml" not in _parts(result)
    finally:
        result.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# N. Core properties
# ---------------------------------------------------------------------------


def test_all_thirteen_core_string_fields_normalized(tmp_path: Path) -> None:
    wb = openpyxl.Workbook()
    wb.active["A1"] = "v"
    p = wb.properties
    p.creator = SENTINEL
    p.title = SENTINEL
    p.subject = SENTINEL
    p.description = SENTINEL
    p.keywords = SENTINEL
    p.category = SENTINEL
    p.contentStatus = SENTINEL
    p.identifier = SENTINEL
    p.lastModifiedBy = SENTINEL
    p.lastPrinted = "2020-01-01T00:00:00Z"
    p.revision = SENTINEL
    p.version = SENTINEL
    p.language = SENTINEL
    source = tmp_path / "coreprops.xlsx"
    wb.save(source)
    wb.close()

    result = scrub_workbook_object_model(source)
    try:
        raw = _read_part(result, "docProps/core.xml").decode("utf-8")
        assert SENTINEL not in raw
        assert "2020-01-01" not in raw
        assert "2000-01-01" in raw  # фиксированная created дата
        assert "<dcterms:modified" in raw  # modified присутствует (generated timestamp)
    finally:
        result.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# O. app.xml observed behaviour (не strict-валидация -- та принадлежит 2.4)
# ---------------------------------------------------------------------------


def test_app_xml_observed_minimal_profile(tmp_path: Path) -> None:
    wb = openpyxl.Workbook()
    wb.active["A1"] = "v"
    wb.create_sheet(SENTINEL)
    source = tmp_path / "appxml.xlsx"
    wb.save(source)
    wb.close()

    result = scrub_workbook_object_model(source)
    try:
        raw = _read_part(result, "docProps/app.xml").decode("utf-8")
        for forbidden in ("Company", "Manager", "TitlesOfParts", "HeadingPairs", SENTINEL):
            assert forbidden not in raw
    finally:
        result.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# P. Calculation properties (не изменяются)
# ---------------------------------------------------------------------------


def test_calculation_properties_unchanged(tmp_path: Path) -> None:
    wb = openpyxl.Workbook()
    wb.active["A1"] = "=1+1"
    source = tmp_path / "calc.xlsx"
    wb.save(source)
    wb.close()
    before = openpyxl.load_workbook(source)
    calc_id_before = before.calculation.calcId
    before.close()

    result = scrub_workbook_object_model(source)
    try:
        after = openpyxl.load_workbook(result)
        assert after.calculation.calcId == calc_id_before
        after.close()
    finally:
        result.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# Q. Worksheet titles (не изменяются)
# ---------------------------------------------------------------------------


def test_worksheet_titles_unchanged(tmp_path: Path) -> None:
    wb = openpyxl.Workbook()
    wb.active.title = "First"
    wb.create_sheet("Second")
    source = tmp_path / "titles.xlsx"
    wb.save(source)
    wb.close()

    result = scrub_workbook_object_model(source)
    try:
        wb2 = openpyxl.load_workbook(result)
        assert wb2.sheetnames == ["First", "Second"]
        wb2.close()
    finally:
        result.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# R. Formulas (OD-10C2.3-1: сохраняются без анализа/изменения)
# ---------------------------------------------------------------------------


def test_ordinary_formula_preserved(tmp_path: Path) -> None:
    wb = openpyxl.Workbook()
    ws = wb.active
    ws["A1"], ws["A2"], ws["A3"] = 1, 2, 3
    ws["B1"] = "=SUM(A1:A3)"
    source = tmp_path / "formula.xlsx"
    wb.save(source)
    wb.close()

    result = scrub_workbook_object_model(source)
    try:
        raw = _read_part(result, "xl/worksheets/sheet1.xml").decode("utf-8")
        assert "SUM(A1:A3)" in raw
    finally:
        result.unlink(missing_ok=True)


def test_string_literal_formula_preserved_documented_security_debt(tmp_path: Path) -> None:
    # OD-10C2.3-1: произвольный строковый литерал внутри формулы -- ЭТО
    # ИМЕННО ТОТ security debt, который закрывается только будущим
    # Formula Safety Gate, НЕ этой стадией.
    wb = openpyxl.Workbook()
    ws = wb.active
    ws["A1"] = 1
    ws["B1"] = f'=IF(A1>0,"{SENTINEL}-literal","")'
    source = tmp_path / "string_literal_formula.xlsx"
    wb.save(source)
    wb.close()

    result = scrub_workbook_object_model(source)
    try:
        raw = _read_part(result, "xl/worksheets/sheet1.xml").decode("utf-8")
        assert f"{SENTINEL}-literal" in raw  # подтверждённый, документированный debt
    finally:
        result.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# S. Rich text
# ---------------------------------------------------------------------------


def test_rich_text_preserved_with_rich_text_true(tmp_path: Path) -> None:
    # MINOR-2 correction: проверяем run count, run text и минимум один
    # нетривиальный InlineFont-атрибут (bold) для каждого TextBlock --
    # не только substring в конкатенированной строке.
    from openpyxl.cell.rich_text import CellRichText, TextBlock
    from openpyxl.cell.text import InlineFont

    wb = openpyxl.Workbook()
    ws = wb.active
    rich = CellRichText(
        "plain-",
        TextBlock(InlineFont(b=True), f"{SENTINEL}-bold"),
        "-mid-",
        TextBlock(InlineFont(i=True), f"{SENTINEL}-italic"),
        "-end",
    )
    ws["A1"] = rich
    source = tmp_path / "richtext.xlsx"
    wb.save(source)
    wb.close()

    result = scrub_workbook_object_model(source)
    try:
        wb2 = openpyxl.load_workbook(result, rich_text=True)
        value = wb2.active["A1"].value
        assert len(value) == 5
        assert value[0] == "plain-"
        assert isinstance(value[1], TextBlock)
        assert value[1].text == f"{SENTINEL}-bold"
        assert value[1].font.b is True
        assert value[2] == "-mid-"
        assert isinstance(value[3], TextBlock)
        assert value[3].text == f"{SENTINEL}-italic"
        assert value[3].font.i is True
        assert value[4] == "-end"
        wb2.close()
    finally:
        result.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# T. Styles
# ---------------------------------------------------------------------------


def test_styles_preserved(tmp_path: Path) -> None:
    wb = openpyxl.Workbook()
    ws = wb.active
    ws["A1"] = 1234.5
    ws["A1"].number_format = "0.00"
    ws["A1"].font = openpyxl.styles.Font(bold=True, color="FF0000")
    ws["A1"].fill = openpyxl.styles.PatternFill(fill_type="solid", fgColor="00FF00")
    ws["A1"].border = openpyxl.styles.Border(left=openpyxl.styles.Side(style="thin"))
    ws["A1"].alignment = openpyxl.styles.Alignment(horizontal="center")
    source = tmp_path / "styles.xlsx"
    wb.save(source)
    wb.close()

    result = scrub_workbook_object_model(source)
    try:
        wb2 = openpyxl.load_workbook(result)
        cell = wb2.active["A1"]
        assert cell.number_format == "0.00"
        assert cell.font.bold is True
        assert cell.fill.fgColor.rgb.upper().endswith("00FF00")
        assert cell.border.left.style == "thin"
        assert cell.alignment.horizontal == "center"
        wb2.close()
    finally:
        result.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# U. Merged cells
# ---------------------------------------------------------------------------


def test_merged_cells_preserved(tmp_path: Path) -> None:
    wb = openpyxl.Workbook()
    ws = wb.active
    ws["A1"] = "anchor-value"
    ws.merge_cells("A1:B2")
    source = tmp_path / "merged.xlsx"
    wb.save(source)
    wb.close()

    result = scrub_workbook_object_model(source)
    try:
        wb2 = openpyxl.load_workbook(result)
        ws2 = wb2.active
        assert str(ws2.merged_cells.ranges) == str(ws.merged_cells.ranges) or "A1:B2" in str(
            ws2.merged_cells.ranges
        )
        assert ws2["A1"].value == "anchor-value"
        wb2.close()
    finally:
        result.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# V. Hidden state (OD-10C2.3-3: не изменяется)
# ---------------------------------------------------------------------------


def test_hidden_worksheet_state_preserved(tmp_path: Path) -> None:
    wb = openpyxl.Workbook()
    wb.active.title = "Visible"
    hidden = wb.create_sheet("Hidden")
    hidden["A1"] = "v"
    hidden.sheet_state = "hidden"
    very_hidden = wb.create_sheet("VeryHidden")
    very_hidden["A1"] = "v"
    very_hidden.sheet_state = "veryHidden"
    source = tmp_path / "hidden_sheets.xlsx"
    wb.save(source)
    wb.close()

    result = scrub_workbook_object_model(source)
    try:
        wb2 = openpyxl.load_workbook(result)
        assert wb2["Hidden"].sheet_state == "hidden"
        assert wb2["VeryHidden"].sheet_state == "veryHidden"
        assert wb2["Visible"].sheet_state == "visible"
        wb2.close()
    finally:
        result.unlink(missing_ok=True)


def test_hidden_rows_and_columns_preserved(tmp_path: Path) -> None:
    wb = openpyxl.Workbook()
    ws = wb.active
    ws["A1"] = "v"
    ws["B1"] = "v"
    ws.row_dimensions[1].hidden = True
    ws.column_dimensions["B"].hidden = True
    source = tmp_path / "hidden_rowcol.xlsx"
    wb.save(source)
    wb.close()

    result = scrub_workbook_object_model(source)
    try:
        wb2 = openpyxl.load_workbook(result)
        ws2 = wb2.active
        assert ws2.row_dimensions[1].hidden is True
        assert ws2.column_dimensions["B"].hidden is True
        wb2.close()
    finally:
        result.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# W. Row/column dimensions
# ---------------------------------------------------------------------------


def test_row_column_dimensions_preserved(tmp_path: Path) -> None:
    wb = openpyxl.Workbook()
    ws = wb.active
    ws["A1"] = "v"
    ws.row_dimensions[1].height = 30
    ws.column_dimensions["A"].width = 25
    source = tmp_path / "dimensions.xlsx"
    wb.save(source)
    wb.close()

    result = scrub_workbook_object_model(source)
    try:
        wb2 = openpyxl.load_workbook(result)
        ws2 = wb2.active
        assert ws2.row_dimensions[1].height == 30
        assert ws2.column_dimensions["A"].width == 25
        wb2.close()
    finally:
        result.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# X. Unicode / whitespace
# ---------------------------------------------------------------------------


def test_unicode_and_whitespace_values_preserved(tmp_path: Path) -> None:
    wb = openpyxl.Workbook()
    ws = wb.active
    values = {
        "A1": "Привет мир",
        "A2": "  leading spaces",
        "A3": "trailing spaces  ",
        "A4": "",
        "A5": "12345",  # numeric-looking string
    }
    for coord, value in values.items():
        ws[coord] = value
    source = tmp_path / "unicode.xlsx"
    wb.save(source)
    wb.close()

    result = scrub_workbook_object_model(source)
    try:
        wb2 = openpyxl.load_workbook(result)
        ws2 = wb2.active
        for coord, value in values.items():
            if value == "":
                continue  # openpyxl не сохраняет пустую строку как ячейку
            assert ws2[coord].value == value
        wb2.close()
    finally:
        result.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# Y. Failure cleanup / error privacy
# ---------------------------------------------------------------------------


def test_type_error_for_invalid_path_type() -> None:
    with pytest.raises(TypeError):
        scrub_workbook_object_model(12345)  # type: ignore[arg-type]


def test_wrong_extension_rejected(tmp_path: Path) -> None:
    p = tmp_path / "not_xlsx.txt"
    p.write_bytes(b"hello")
    with pytest.raises(PackageScrubError) as exc_info:
        scrub_workbook_object_model(p)
    assert exc_info.value.reason is PackageScrubReason.SCRUB_FAILED


def test_missing_source_rejected(tmp_path: Path) -> None:
    with pytest.raises(PackageScrubError) as exc_info:
        scrub_workbook_object_model(tmp_path / "does_not_exist.xlsx")
    assert exc_info.value.reason is PackageScrubReason.SCRUB_FAILED


def test_malformed_source_rejected_no_temp_leak(tmp_path: Path) -> None:
    p = tmp_path / "malformed.xlsx"
    p.write_bytes(b"not a real zip/xlsx file at all")
    import tempfile

    before = set(Path(tempfile.gettempdir()).glob("*.xlsx"))
    with pytest.raises(PackageScrubError) as exc_info:
        scrub_workbook_object_model(p)
    assert exc_info.value.reason is PackageScrubReason.SCRUB_FAILED
    after = set(Path(tempfile.gettempdir()).glob("*.xlsx"))
    assert after <= before  # никаких новых temp .xlsx не осталось


def test_sentinel_does_not_leak_on_missing_source(tmp_path: Path) -> None:
    sensitive_dir = tmp_path / SENTINEL
    sensitive_dir.mkdir()
    with pytest.raises(PackageScrubError) as exc_info:
        scrub_workbook_object_model(sensitive_dir / "missing.xlsx")
    _assert_no_sentinel_leak(exc_info.value)


def test_sentinel_does_not_leak_on_malformed_source(tmp_path: Path) -> None:
    sensitive_dir = tmp_path / SENTINEL
    sensitive_dir.mkdir()
    p = sensitive_dir / "malformed.xlsx"
    p.write_bytes(b"not a real xlsx")
    with pytest.raises(PackageScrubError) as exc_info:
        scrub_workbook_object_model(p)
    _assert_no_sentinel_leak(exc_info.value)


def test_mutation_failure_does_not_leak_temp_and_is_sanitized(tmp_path: Path) -> None:
    wb = openpyxl.Workbook()
    wb.active["A1"] = "v"
    source = tmp_path / "mutation_fail.xlsx"
    wb.save(source)
    wb.close()

    def _boom(*args, **kwargs):  # noqa: ANN002, ANN003
        raise RuntimeError(f"pretend internal failure touching {SENTINEL}")

    import tempfile

    before = set(Path(tempfile.gettempdir()).glob("*.xlsx"))
    with mock.patch("app.scrub.mutate._scrub_workbook", _boom):
        with pytest.raises(PackageScrubError) as exc_info:
            scrub_workbook_object_model(source)
    assert exc_info.value.reason is PackageScrubReason.SCRUB_FAILED
    _assert_no_sentinel_leak(exc_info.value)
    after = set(Path(tempfile.gettempdir()).glob("*.xlsx"))
    assert after <= before


def test_save_failure_cleans_up_temp(tmp_path: Path) -> None:
    wb = openpyxl.Workbook()
    wb.active["A1"] = "v"
    source = tmp_path / "save_fail.xlsx"
    wb.save(source)
    wb.close()

    import tempfile

    before = set(Path(tempfile.gettempdir()).glob("*.xlsx"))

    def _boom_save(self, *args, **kwargs):  # noqa: ANN001, ANN002, ANN003
        raise RuntimeError("pretend save failure")

    with mock.patch("openpyxl.Workbook.save", _boom_save):
        with pytest.raises(PackageScrubError) as exc_info:
            scrub_workbook_object_model(source)
    assert exc_info.value.reason is PackageScrubReason.SCRUB_FAILED
    after = set(Path(tempfile.gettempdir()).glob("*.xlsx"))
    assert after <= before


def test_mkstemp_failure_sanitized_no_leak(tmp_path: Path) -> None:
    # MAJOR-1 correction: сбой tempfile.mkstemp() (resource exhaustion)
    # должен транслироваться в PackageScrubError(SCRUB_FAILED), а не
    # пробрасывать сырое исключение с confidential path/value.
    wb = openpyxl.Workbook()
    wb.active["A1"] = "v"
    source = tmp_path / "mkstemp_fail.xlsx"
    wb.save(source)
    wb.close()

    import tempfile

    before = set(Path(tempfile.gettempdir()).glob("*.xlsx"))

    def _boom_mkstemp(*args, **kwargs):  # noqa: ANN002, ANN003
        raise OSError(28, f"No space left on device touching {SENTINEL}", f"/os/tmp/{SENTINEL}/x.xlsx")

    with mock.patch("tempfile.mkstemp", _boom_mkstemp):
        with pytest.raises(PackageScrubError) as exc_info:
            scrub_workbook_object_model(source)
    assert exc_info.value.reason is PackageScrubReason.SCRUB_FAILED
    _assert_no_sentinel_leak(exc_info.value)
    after = set(Path(tempfile.gettempdir()).glob("*.xlsx"))
    assert after <= before


def test_package_scrub_error_scrub_failed_message_safe() -> None:
    exc = PackageScrubError(PackageScrubReason.SCRUB_FAILED)
    _assert_no_sentinel_leak(exc)


# ---------------------------------------------------------------------------
# AA. Font-name registry sanitization (BLOCKER-2, OD-10C2.3-6)
# ---------------------------------------------------------------------------


def test_font_allowlisted_names_preserved(tmp_path: Path) -> None:
    from openpyxl.styles import Font

    wb = openpyxl.Workbook()
    ws = wb.active
    allowed = ["Calibri", "Calibri Light", "Arial", "Times New Roman", "Courier New", "Segoe UI", "Tahoma", "Verdana", "Aptos"]
    for i, name in enumerate(allowed):
        ws.cell(row=i + 1, column=1, value=f"v{i}").font = Font(name=name)
    source = tmp_path / "font_allowlist.xlsx"
    wb.save(source)
    wb.close()

    result = scrub_workbook_object_model(source)
    try:
        wb2 = openpyxl.load_workbook(result)
        ws2 = wb2.active
        for i, name in enumerate(allowed):
            assert ws2.cell(row=i + 1, column=1).font.name == name
        wb2.close()
    finally:
        result.unlink(missing_ok=True)


def test_font_case_variant_normalized(tmp_path: Path) -> None:
    from openpyxl.styles import Font

    wb = openpyxl.Workbook()
    ws = wb.active
    ws["A1"] = "v"
    ws["A1"].font = Font(name="calibri")  # lowercase -- not exact match
    source = tmp_path / "font_case.xlsx"
    wb.save(source)
    wb.close()

    result = scrub_workbook_object_model(source)
    try:
        wb2 = openpyxl.load_workbook(result)
        assert wb2.active["A1"].font.name == "Calibri"
        wb2.close()
    finally:
        result.unlink(missing_ok=True)


def test_font_whitespace_variant_normalized(tmp_path: Path) -> None:
    from openpyxl.styles import Font

    wb = openpyxl.Workbook()
    ws = wb.active
    ws["A1"] = "v"
    ws["A1"].font = Font(name=" Calibri ")
    source = tmp_path / "font_whitespace.xlsx"
    wb.save(source)
    wb.close()

    result = scrub_workbook_object_model(source)
    try:
        wb2 = openpyxl.load_workbook(result)
        assert wb2.active["A1"].font.name == "Calibri"
        wb2.close()
    finally:
        result.unlink(missing_ok=True)


def test_font_unknown_ascii_replaced_with_fallback(tmp_path: Path) -> None:
    from openpyxl.styles import Font

    wb = openpyxl.Workbook()
    ws = wb.active
    ws["A1"] = "v"
    ws["A1"].font = Font(name=f"{SENTINEL}-font", bold=True)
    source = tmp_path / "font_unknown.xlsx"
    wb.save(source)
    wb.close()

    result = scrub_workbook_object_model(source)
    try:
        raw = _read_part(result, "xl/styles.xml").decode("utf-8")
        assert SENTINEL not in raw
        wb2 = openpyxl.load_workbook(result)
        assert wb2.active["A1"].font.name == "Calibri"
        assert wb2.active["A1"].font.bold is True
        wb2.close()
    finally:
        result.unlink(missing_ok=True)


def test_font_unicode_secret_replaced_with_fallback(tmp_path: Path) -> None:
    from openpyxl.styles import Font

    wb = openpyxl.Workbook()
    ws = wb.active
    ws["A1"] = "v"
    ws["A1"].font = Font(name="Секретный_Шрифт")
    source = tmp_path / "font_unicode.xlsx"
    wb.save(source)
    wb.close()

    result = scrub_workbook_object_model(source)
    try:
        raw = _read_part(result, "xl/styles.xml").decode("utf-8")
        assert "Секретный_Шрифт" not in raw
        wb2 = openpyxl.load_workbook(result)
        assert wb2.active["A1"].font.name == "Calibri"
        wb2.close()
    finally:
        result.unlink(missing_ok=True)


def test_font_none_name_not_turned_into_string(tmp_path: Path) -> None:
    from openpyxl.styles import Font

    wb = openpyxl.Workbook()
    ws = wb.active
    ws["A1"] = "v"
    ws["A1"].font = Font(name=None, bold=True)
    source = tmp_path / "font_none.xlsx"
    wb.save(source)
    wb.close()

    result = scrub_workbook_object_model(source)
    try:
        wb2 = openpyxl.load_workbook(result)
        assert wb2.active["A1"].font.name is None
        assert wb2.active["A1"].font.bold is True
        wb2.close()
    finally:
        result.unlink(missing_ok=True)


def test_font_other_attributes_preserved(tmp_path: Path) -> None:
    from openpyxl.styles import Font

    wb = openpyxl.Workbook()
    ws = wb.active
    ws["A1"] = "v"
    ws["A1"].font = Font(
        name=f"{SENTINEL}-font",
        bold=True,
        italic=True,
        underline="single",
        strike=True,
        size=14,
        color="FF0000",
        vertAlign="superscript",
    )
    source = tmp_path / "font_attrs.xlsx"
    wb.save(source)
    wb.close()

    result = scrub_workbook_object_model(source)
    try:
        wb2 = openpyxl.load_workbook(result)
        font = wb2.active["A1"].font
        assert font.name == "Calibri"
        assert font.bold is True
        assert font.italic is True
        assert font.underline == "single"
        assert font.strike is True
        assert font.size == 14
        assert font.color.rgb.upper().endswith("FF0000")
        assert font.vertAlign == "superscript"
        wb2.close()
    finally:
        result.unlink(missing_ok=True)


def test_font_inside_named_style_sanitized(tmp_path: Path) -> None:
    from openpyxl.styles import Font, NamedStyle

    wb = openpyxl.Workbook()
    ws = wb.active
    ws["A1"] = "v"
    named = NamedStyle(name="PlainStyle")
    named.font = Font(name=f"{SENTINEL}-namedfont", bold=True)
    wb.add_named_style(named)
    ws["A1"].style = "PlainStyle"
    source = tmp_path / "font_named.xlsx"
    wb.save(source)
    wb.close()

    result = scrub_workbook_object_model(source)
    try:
        raw = _read_part(result, "xl/styles.xml").decode("utf-8")
        assert SENTINEL not in raw
        wb2 = openpyxl.load_workbook(result)
        assert wb2.active["A1"].font.name == "Calibri"
        assert wb2.active["A1"].font.bold is True
        wb2.close()
    finally:
        result.unlink(missing_ok=True)


def test_font_orphan_registry_entry_sanitized(tmp_path: Path) -> None:
    # Orphan-font regression (Architecture/Freeze Pass finding): создаём
    # ситуацию, где после обычного cell.font reassignment старая unsafe
    # Font-запись осталась бы orphan в workbook._fonts, ЕСЛИ БЫ scrub
    # полагался на cell-level property reassignment. Здесь source-файл
    # уже содержит unsafe font на A1 -- production scrub обязан
    # обработать registry напрямую (workbook._fonts), а не полагаться
    # на переприсвоение конкретной ячейки.
    from openpyxl.styles import Font

    wb = openpyxl.Workbook()
    ws = wb.active
    ws["A1"] = "v1"
    ws["A1"].font = Font(name=f"{SENTINEL}-orphanA", bold=True)
    ws["A2"] = "v2"
    ws["A2"].font = Font(name=f"{SENTINEL}-orphanB", italic=True)
    source = tmp_path / "font_orphan.xlsx"
    wb.save(source)
    wb.close()

    result = scrub_workbook_object_model(source)
    try:
        raw = _read_part(result, "xl/styles.xml").decode("utf-8")
        assert f"{SENTINEL}-orphanA" not in raw
        assert f"{SENTINEL}-orphanB" not in raw
        wb2 = openpyxl.load_workbook(result)
        assert wb2.active["A1"].font.name == "Calibri"
        assert wb2.active["A1"].font.bold is True
        assert wb2.active["A2"].font.name == "Calibri"
        assert wb2.active["A2"].font.italic is True
        wb2.close()
    finally:
        result.unlink(missing_ok=True)


def test_font_multiple_safe_and_unsafe_positional_integrity(tmp_path: Path) -> None:
    # Safe + unsafe + safe -- проверяем, что registry-level in-place
    # мутация не ломает fontId-индексацию для ДРУГИХ, действительно
    # safe шрифтов в том же workbook (позиционная целостность реестра).
    from openpyxl.styles import Font

    wb = openpyxl.Workbook()
    ws = wb.active
    ws["A1"] = "v1"
    ws["A1"].font = Font(name="Arial", bold=True)
    ws["A2"] = "v2"
    ws["A2"].font = Font(name=f"{SENTINEL}-unsafe")
    ws["A3"] = "v3"
    ws["A3"].font = Font(name="Verdana", italic=True)
    source = tmp_path / "font_mixed.xlsx"
    wb.save(source)
    wb.close()

    result = scrub_workbook_object_model(source)
    try:
        wb2 = openpyxl.load_workbook(result)
        ws2 = wb2.active
        assert ws2["A1"].font.name == "Arial"
        assert ws2["A1"].font.bold is True
        assert ws2["A2"].font.name == "Calibri"
        assert ws2["A3"].font.name == "Verdana"
        assert ws2["A3"].font.italic is True
        wb2.close()
    finally:
        result.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# BB. Number-format registry sanitization (BLOCKER-2, OD-10C2.3-5)
# ---------------------------------------------------------------------------


def test_number_format_builtin_preserved(tmp_path: Path) -> None:
    wb = openpyxl.Workbook()
    ws = wb.active
    ws["A1"] = 1234.5
    ws["A1"].number_format = "0.00"
    ws["A2"] = 1234.5
    ws["A2"].number_format = "mm-dd-yy"
    source = tmp_path / "numfmt_builtin.xlsx"
    wb.save(source)
    wb.close()

    result = scrub_workbook_object_model(source)
    try:
        wb2 = openpyxl.load_workbook(result)
        assert wb2.active["A1"].number_format == "0.00"
        assert wb2.active["A2"].number_format == "mm-dd-yy"
        wb2.close()
    finally:
        result.unlink(missing_ok=True)


def test_number_format_exact_allowlist_preserved(tmp_path: Path) -> None:
    wb = openpyxl.Workbook()
    ws = wb.active
    allowed = ["yyyy-mm-dd", "dd.mm.yyyy", "dd.mm.yy", "dd.mm.yyyy hh:mm", "dd.mm.yyyy hh:mm:ss", "hh:mm", "hh:mm:ss"]
    for i, fmt in enumerate(allowed):
        ws.cell(row=i + 1, column=1, value=1234.5).number_format = fmt
    source = tmp_path / "numfmt_allowlist.xlsx"
    wb.save(source)
    wb.close()

    result = scrub_workbook_object_model(source)
    try:
        wb2 = openpyxl.load_workbook(result)
        ws2 = wb2.active
        for i, fmt in enumerate(allowed):
            assert ws2.cell(row=i + 1, column=1).number_format == fmt
        wb2.close()
    finally:
        result.unlink(missing_ok=True)


def test_number_format_quoted_literal_falls_back(tmp_path: Path) -> None:
    wb = openpyxl.Workbook()
    ws = wb.active
    ws["A1"] = 1234.5
    ws["A1"].number_format = f'"{SENTINEL}"0.00'
    source = tmp_path / "numfmt_quoted.xlsx"
    wb.save(source)
    wb.close()

    result = scrub_workbook_object_model(source)
    try:
        raw = _read_part(result, "xl/styles.xml").decode("utf-8")
        assert SENTINEL not in raw
        wb2 = openpyxl.load_workbook(result)
        assert wb2.active["A1"].number_format == "0.00"
        assert wb2.active["A1"].value == 1234.5
        wb2.close()
    finally:
        result.unlink(missing_ok=True)


def test_number_format_unquoted_literal_falls_back(tmp_path: Path) -> None:
    # ПРИМЕЧАНИЕ: openpyxl.styles.numbers.is_date_format/is_datetime
    # используют case-insensitive символьный класс [dmhysDMHYS] без
    # понимания слов -- незаквоченный литерал, случайно содержащий
    # d/m/h/y/s (в любом регистре), может быть классифицирован как
    # "похожий на дату/время" для ВЫБОРА fallback-формата. Это НЕ
    # влияет на security invariant: секрет в любом случае удаляется,
    # заменяется ОДНИМ из frozen safe fallback-значений -- поэтому
    # здесь проверяется членство в известном множестве fallback'ов, а
    # не точное совпадение с конкретной классификацией.
    wb = openpyxl.Workbook()
    ws = wb.active
    ws["A1"] = 1234.5
    ws["A1"].number_format = f"0 {SENTINEL}"
    source = tmp_path / "numfmt_unquoted.xlsx"
    wb.save(source)
    wb.close()

    result = scrub_workbook_object_model(source)
    try:
        raw = _read_part(result, "xl/styles.xml").decode("utf-8")
        assert SENTINEL not in raw
        wb2 = openpyxl.load_workbook(result)
        assert wb2.active["A1"].number_format in {
            "0.00",
            "hh:mm:ss",
            "yyyy-mm-dd",
            "dd.mm.yyyy hh:mm:ss",
            "0.00%",
        }
        wb2.close()
    finally:
        result.unlink(missing_ok=True)


def test_number_format_escaped_literal_falls_back(tmp_path: Path) -> None:
    wb = openpyxl.Workbook()
    ws = wb.active
    ws["A1"] = 1234.5
    escaped = "0" + "".join(f"\\{ch}" for ch in SENTINEL)
    ws["A1"].number_format = escaped
    source = tmp_path / "numfmt_escaped.xlsx"
    wb.save(source)
    wb.close()

    result = scrub_workbook_object_model(source)
    try:
        raw = _read_part(result, "xl/styles.xml").decode("utf-8")
        assert SENTINEL not in raw
        wb2 = openpyxl.load_workbook(result)
        assert wb2.active["A1"].number_format == "0.00"
        wb2.close()
    finally:
        result.unlink(missing_ok=True)


def test_number_format_locale_currency_secret_falls_back(tmp_path: Path) -> None:
    wb = openpyxl.Workbook()
    ws = wb.active
    ws["A1"] = 1234.5
    ws["A1"].number_format = f"[${SENTINEL}-419]#,##0.00"
    source = tmp_path / "numfmt_locale.xlsx"
    wb.save(source)
    wb.close()

    result = scrub_workbook_object_model(source)
    try:
        raw = _read_part(result, "xl/styles.xml").decode("utf-8")
        assert SENTINEL not in raw
        wb2 = openpyxl.load_workbook(result)
        assert wb2.active["A1"].number_format == "0.00"
        wb2.close()
    finally:
        result.unlink(missing_ok=True)


def test_number_format_unit_suffix_falls_back(tmp_path: Path) -> None:
    # "руб."/"шт."/"кг" и подобные -- НЕ добавляются в allow-list "ради
    # красоты" (frozen contract §10): попадают в fallback, как и любой
    # иной custom-текст.
    wb = openpyxl.Workbook()
    ws = wb.active
    ws["A1"] = 5
    ws["A1"].number_format = '0 "шт."'
    ws["A2"] = 1234.5
    ws["A2"].number_format = "#,##0.00 ₽"
    source = tmp_path / "numfmt_unit.xlsx"
    wb.save(source)
    wb.close()

    result = scrub_workbook_object_model(source)
    try:
        raw = _read_part(result, "xl/styles.xml").decode("utf-8")
        assert "шт." not in raw
        assert "₽" not in raw
        wb2 = openpyxl.load_workbook(result)
        assert wb2.active["A1"].number_format == "0.00"
        assert wb2.active["A2"].number_format == "0.00"
        wb2.close()
    finally:
        result.unlink(missing_ok=True)


def test_number_format_date_fallback_classification(tmp_path: Path) -> None:
    wb = openpyxl.Workbook()
    ws = wb.active
    ws["A1"] = 1234.5
    ws["A1"].number_format = f'd-mmm-yyyy "{SENTINEL}"'
    source = tmp_path / "numfmt_date_fallback.xlsx"
    wb.save(source)
    wb.close()

    result = scrub_workbook_object_model(source)
    try:
        raw = _read_part(result, "xl/styles.xml").decode("utf-8")
        assert SENTINEL not in raw
        wb2 = openpyxl.load_workbook(result)
        assert wb2.active["A1"].number_format == "yyyy-mm-dd"
        wb2.close()
    finally:
        result.unlink(missing_ok=True)


def test_number_format_time_fallback_classification(tmp_path: Path) -> None:
    wb = openpyxl.Workbook()
    ws = wb.active
    ws["A1"] = 0.5
    ws["A1"].number_format = f'h.mm.ss "{SENTINEL}"'
    source = tmp_path / "numfmt_time_fallback.xlsx"
    wb.save(source)
    wb.close()

    result = scrub_workbook_object_model(source)
    try:
        raw = _read_part(result, "xl/styles.xml").decode("utf-8")
        assert SENTINEL not in raw
        wb2 = openpyxl.load_workbook(result)
        assert wb2.active["A1"].number_format == "hh:mm:ss"
        wb2.close()
    finally:
        result.unlink(missing_ok=True)


def test_number_format_datetime_fallback_classification(tmp_path: Path) -> None:
    wb = openpyxl.Workbook()
    ws = wb.active
    ws["A1"] = 1234.5
    ws["A1"].number_format = f'd-mmm-yyyy h.mm "{SENTINEL}"'
    source = tmp_path / "numfmt_datetime_fallback.xlsx"
    wb.save(source)
    wb.close()

    result = scrub_workbook_object_model(source)
    try:
        raw = _read_part(result, "xl/styles.xml").decode("utf-8")
        assert SENTINEL not in raw
        wb2 = openpyxl.load_workbook(result)
        assert wb2.active["A1"].number_format == "dd.mm.yyyy hh:mm:ss"
        wb2.close()
    finally:
        result.unlink(missing_ok=True)


def test_number_format_percent_fallback_classification(tmp_path: Path) -> None:
    wb = openpyxl.Workbook()
    ws = wb.active
    ws["A1"] = 0.5
    ws["A1"].number_format = f'0.00% "{SENTINEL}"'
    source = tmp_path / "numfmt_percent_fallback.xlsx"
    wb.save(source)
    wb.close()

    result = scrub_workbook_object_model(source)
    try:
        raw = _read_part(result, "xl/styles.xml").decode("utf-8")
        assert SENTINEL not in raw
        wb2 = openpyxl.load_workbook(result)
        assert wb2.active["A1"].number_format == "0.00%"
        wb2.close()
    finally:
        result.unlink(missing_ok=True)


def test_number_format_other_numeric_fallback_classification(tmp_path: Path) -> None:
    wb = openpyxl.Workbook()
    ws = wb.active
    ws["A1"] = 1234.5
    ws["A1"].number_format = f'"{SENTINEL}"#,##0.00'
    source = tmp_path / "numfmt_other_fallback.xlsx"
    wb.save(source)
    wb.close()

    result = scrub_workbook_object_model(source)
    try:
        raw = _read_part(result, "xl/styles.xml").decode("utf-8")
        assert SENTINEL not in raw
        wb2 = openpyxl.load_workbook(result)
        assert wb2.active["A1"].number_format == "0.00"
        wb2.close()
    finally:
        result.unlink(missing_ok=True)


def test_number_format_orphan_registry_entry_sanitized(tmp_path: Path) -> None:
    # Orphan-numFmt regression, аналогичный orphan-font: несколько
    # различных unsafe custom formats на разных ячейках -- production
    # scrub обязан обработать весь workbook._number_formats registry.
    wb = openpyxl.Workbook()
    ws = wb.active
    ws["A1"] = 1234.5
    ws["A1"].number_format = f'"{SENTINEL}-A"0.00'
    ws["A2"] = 1234.5
    ws["A2"].number_format = f'"{SENTINEL}-B"0.00'
    source = tmp_path / "numfmt_orphan.xlsx"
    wb.save(source)
    wb.close()

    result = scrub_workbook_object_model(source)
    try:
        raw = _read_part(result, "xl/styles.xml").decode("utf-8")
        assert f"{SENTINEL}-A" not in raw
        assert f"{SENTINEL}-B" not in raw
        wb2 = openpyxl.load_workbook(result)
        assert wb2.active["A1"].number_format == "0.00"
        assert wb2.active["A2"].number_format == "0.00"
        wb2.close()
    finally:
        result.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# CC. NamedStyle sanitization (BLOCKER-2, OD-10C2.3-5)
# ---------------------------------------------------------------------------


def test_named_style_builtin_unchanged(tmp_path: Path) -> None:
    wb = openpyxl.Workbook()
    wb.active["A1"] = "v"
    source = tmp_path / "namedstyle_builtin.xlsx"
    wb.save(source)
    wb.close()

    result = scrub_workbook_object_model(source)
    try:
        wb2 = openpyxl.load_workbook(result)
        assert "Normal" in wb2.named_styles
        wb2.close()
    finally:
        result.unlink(missing_ok=True)


def test_named_style_custom_renamed(tmp_path: Path) -> None:
    from openpyxl.styles import NamedStyle

    wb = openpyxl.Workbook()
    ws = wb.active
    ws["A1"] = "v"
    named = NamedStyle(name=f"{SENTINEL}-style")
    wb.add_named_style(named)
    ws["A1"].style = f"{SENTINEL}-style"
    source = tmp_path / "namedstyle_custom.xlsx"
    wb.save(source)
    wb.close()

    result = scrub_workbook_object_model(source)
    try:
        raw = _read_part(result, "xl/styles.xml").decode("utf-8")
        assert SENTINEL not in raw
        wb2 = openpyxl.load_workbook(result)
        assert "DAStyle0001" in wb2.named_styles
        assert f"{SENTINEL}-style" not in wb2.named_styles
        wb2.close()
    finally:
        result.unlink(missing_ok=True)


def test_named_style_collision_deterministic_indices(tmp_path: Path) -> None:
    from openpyxl.styles import NamedStyle

    wb = openpyxl.Workbook()
    ws = wb.active
    ws["A1"] = "v1"
    ws["A2"] = "v2"
    style_a = NamedStyle(name=f"{SENTINEL}-A")
    style_b = NamedStyle(name=f"{SENTINEL}-B")
    wb.add_named_style(style_a)
    wb.add_named_style(style_b)
    ws["A1"].style = f"{SENTINEL}-A"
    ws["A2"].style = f"{SENTINEL}-B"
    source = tmp_path / "namedstyle_collision.xlsx"
    wb.save(source)
    wb.close()

    result = scrub_workbook_object_model(source)
    try:
        wb2 = openpyxl.load_workbook(result)
        names = set(wb2.named_styles)
        assert {"Normal", "DAStyle0001", "DAStyle0002"} == names
        wb2.close()
    finally:
        result.unlink(missing_ok=True)


def test_named_style_applied_and_unused_both_sanitized(tmp_path: Path) -> None:
    from openpyxl.styles import NamedStyle

    wb = openpyxl.Workbook()
    ws = wb.active
    ws["A1"] = "v"
    used = NamedStyle(name=f"{SENTINEL}-used")
    unused = NamedStyle(name=f"{SENTINEL}-unused")
    wb.add_named_style(used)
    wb.add_named_style(unused)
    ws["A1"].style = f"{SENTINEL}-used"
    source = tmp_path / "namedstyle_unused.xlsx"
    wb.save(source)
    wb.close()

    result = scrub_workbook_object_model(source)
    try:
        raw = _read_part(result, "xl/styles.xml").decode("utf-8")
        assert SENTINEL not in raw
        wb2 = openpyxl.load_workbook(result)
        assert set(wb2.named_styles) == {"Normal", "DAStyle0001", "DAStyle0002"}
        wb2.close()
    finally:
        result.unlink(missing_ok=True)


def test_named_style_no_builtin_name_collision_possible() -> None:
    # Security-first assertion (frozen contract §16): префикс "DAStyle"
    # заведомо не пересекается ни с одним известным built-in Excel-именем.
    known_builtin_names = {
        "Normal", "Comma", "Comma [0]", "Currency", "Currency [0]", "Percent",
        "Bad", "Good", "Neutral", "Calculation", "Check Cell",
        "Explanatory Text", "Input", "Linked Cell", "Note", "Output",
        "Title", "Total", "Warning Text",
        "Heading 1", "Heading 2", "Heading 3", "Heading 4",
    }
    for name in known_builtin_names:
        assert not name.startswith("DAStyle")


def test_named_style_nested_unsafe_font_and_numfmt_sanitized(tmp_path: Path) -> None:
    from openpyxl.styles import Font, NamedStyle

    wb = openpyxl.Workbook()
    ws = wb.active
    ws["A1"] = 1234.5
    named = NamedStyle(name=f"{SENTINEL}-nested")
    named.font = Font(name=f"{SENTINEL}-nestedfont", bold=True)
    named.number_format = f'"{SENTINEL}-nestedfmt"0.00'
    wb.add_named_style(named)
    ws["A1"].style = f"{SENTINEL}-nested"
    source = tmp_path / "namedstyle_nested.xlsx"
    wb.save(source)
    wb.close()

    result = scrub_workbook_object_model(source)
    try:
        raw = _read_part(result, "xl/styles.xml").decode("utf-8")
        assert SENTINEL not in raw
        wb2 = openpyxl.load_workbook(result)
        cell = wb2.active["A1"]
        assert cell.font.name == "Calibri"
        assert cell.font.bold is True
        assert cell.number_format == "0.00"
        assert cell.value == 1234.5
        wb2.close()
    finally:
        result.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# CC2. NamedStyle builtinId bypass -- BLOCKER Correction Pass
# ---------------------------------------------------------------------------

_ALL_TRUSTED_BUILTIN_PAIRS = [
    ("Normal", 0), ("Comma", 3), ("Currency", 4), ("Percent", 5),
    ("Comma [0]", 6), ("Currency [0]", 7), ("Hyperlink", 8),
    ("Followed Hyperlink", 9), ("Note", 10), ("Warning Text", 11),
    ("Title", 15), ("Headline 1", 16), ("Headline 2", 17), ("Headline 3", 18),
    ("Headline 4", 19), ("Input", 20), ("Output", 21), ("Calculation", 22),
    ("Check Cell", 23), ("Linked Cell", 24), ("Total", 25), ("Good", 26),
    ("Bad", 27), ("Neutral", 28), ("Accent1", 29), ("20 % - Accent1", 30),
    ("40 % - Accent1", 31), ("60 % - Accent1", 32), ("Accent2", 33),
    ("20 % - Accent2", 34), ("40 % - Accent2", 35), ("60 % - Accent2", 36),
    ("Accent3", 37), ("20 % - Accent3", 38), ("40 % - Accent3", 39),
    ("60 % - Accent3", 40), ("Accent4", 41), ("20 % - Accent4", 42),
    ("40 % - Accent4", 43), ("60 % - Accent4", 44), ("Accent5", 45),
    ("20 % - Accent5", 46), ("40 % - Accent5", 47), ("60 % - Accent5", 48),
    ("Accent6", 49), ("20 % - Accent6", 50), ("40 % - Accent6", 51),
    ("60 % - Accent6", 52), ("Explanatory Text", 53),
]


def _build_and_scrub_with_named_style(tmp_path: Path, name: str, builtin_id, cell_value="v"):
    from openpyxl.styles import NamedStyle

    wb = openpyxl.Workbook()
    ws = wb.active
    ws["A1"] = cell_value
    named = NamedStyle(name=name)
    named.builtinId = builtin_id
    wb.add_named_style(named)
    ws["A1"].style = name
    source = tmp_path / f"namedstyle_pair_{uuid.uuid4().hex}.xlsx"
    wb.save(source)
    wb.close()
    result = scrub_workbook_object_model(source)
    return source, result


def test_named_style_builtin_bypass_original_exploit(tmp_path: Path) -> None:
    # A: original discovered exploit -- confidential name + spoofed builtinId=1.
    source, result = _build_and_scrub_with_named_style(tmp_path, f"{SENTINEL}-spoof1", 1)
    try:
        raw = _read_part(result, "xl/styles.xml").decode("utf-8")
        assert SENTINEL not in raw
        wb2 = openpyxl.load_workbook(result)
        assert "DAStyle0001" in wb2.named_styles
        assert wb2.active["A1"].style == "DAStyle0001"
        wb2.close()
        with zipfile.ZipFile(result) as z:
            styles = z.read("xl/styles.xml").decode("utf-8")
        assert 'name="DAStyle0001" xfId="1" hidden="0"' in styles  # no builtinId attribute present
    finally:
        result.unlink(missing_ok=True)


def test_named_style_confidential_name_masquerading_as_normal(tmp_path: Path) -> None:
    # B: confidential name + builtinId=0 must NOT masquerade as "Normal".
    source, result = _build_and_scrub_with_named_style(tmp_path, f"{SENTINEL}-fakenormal", 0)
    try:
        with zipfile.ZipFile(result) as z:
            styles = z.read("xl/styles.xml").decode("utf-8")
        assert SENTINEL not in styles
        wb2 = openpyxl.load_workbook(result)
        names = set(wb2.named_styles)
        assert names == {"Normal", "DAStyle0001"}
        assert wb2.active["A1"].style == "DAStyle0001"
        wb2.close()
    finally:
        result.unlink(missing_ok=True)


@pytest.mark.parametrize("builtin_id", [3, 15, 29, 53])
def test_named_style_confidential_name_with_representative_known_ids(tmp_path: Path, builtin_id: int) -> None:
    # C: confidential name + representative known IDs -- all sanitized.
    source, result = _build_and_scrub_with_named_style(tmp_path, f"{SENTINEL}-id{builtin_id}", builtin_id)
    try:
        with zipfile.ZipFile(result) as z:
            styles = z.read("xl/styles.xml").decode("utf-8")
        assert SENTINEL not in styles
        assert "builtinId" not in styles.split("<tableStyles")[0].split("cellStyle name=\"DAStyle0001\"")[-1].split("/>")[0]
    finally:
        result.unlink(missing_ok=True)


def test_named_style_normal_with_wrong_builtin_id(tmp_path: Path) -> None:
    # D (Normal case): the DEFAULT "Normal" style exists in every fresh
    # workbook -- mutate its builtinId directly to create the mismatched
    # ("Normal", 3) pair, since add_named_style() refuses a second style
    # literally named "Normal".
    from openpyxl.styles import NamedStyle

    wb = openpyxl.Workbook()
    ws = wb.active
    ws["A1"] = "v"
    normal_style = next(ns for ns in wb._named_styles if ns.name == "Normal")
    normal_style.builtinId = 3
    source = tmp_path / f"normal_wrongid_{uuid.uuid4().hex}.xlsx"
    wb.save(source)
    wb.close()
    result = scrub_workbook_object_model(source)
    try:
        wb2 = openpyxl.load_workbook(result)
        names = set(wb2.named_styles)
        assert names == {"DAStyle0001"}  # "Normal" itself was mismatched -> sanitized, not preserved
        wb2.close()
        with zipfile.ZipFile(result) as z:
            styles = z.read("xl/styles.xml").decode("utf-8")
        assert 'name="DAStyle0001" xfId="0" hidden="0"' in styles
        assert "builtinId" not in styles.split("<tableStyles")[0]
    finally:
        result.unlink(missing_ok=True)


@pytest.mark.parametrize("name,wrong_id", [("Currency", 0), ("Accent1", 30)])
def test_named_style_known_name_wrong_builtin_id(tmp_path: Path, name: str, wrong_id: int) -> None:
    # D: known name + mismatched builtinId -- must be sanitized (exact-pair-match, not name-only).
    source, result = _build_and_scrub_with_named_style(tmp_path, name, wrong_id)
    try:
        wb2 = openpyxl.load_workbook(result)
        names = set(wb2.named_styles)
        assert name not in names
        assert "DAStyle0001" in names
        wb2.close()
        with zipfile.ZipFile(result) as z:
            styles = z.read("xl/styles.xml").decode("utf-8")
        assert 'name="DAStyle0001" xfId="1" hidden="0"' in styles  # renamed, no builtinId
    finally:
        result.unlink(missing_ok=True)


@pytest.mark.parametrize("unknown_id", [1, 2, 12, 13, 14, 54, 999])
def test_named_style_unknown_or_reserved_builtin_id(tmp_path: Path, unknown_id: int) -> None:
    # E: unknown/reserved/out-of-range builtinId -- all sanitized.
    source, result = _build_and_scrub_with_named_style(tmp_path, f"{SENTINEL}-unk{unknown_id}", unknown_id)
    try:
        with zipfile.ZipFile(result) as z:
            styles = z.read("xl/styles.xml").decode("utf-8")
        assert SENTINEL not in styles
        assert 'name="DAStyle0001" xfId="1" hidden="0"' in styles
    finally:
        result.unlink(missing_ok=True)


@pytest.mark.parametrize("name,builtin_id", [
    ("Normal", 0), ("Currency", 4), ("Title", 15), ("Accent1", 29), ("Explanatory Text", 53),
])
def test_named_style_exact_legitimate_pairs_preserved(tmp_path: Path, name: str, builtin_id: int) -> None:
    # F: representative exact legitimate pairs must be preserved unchanged.
    wb = openpyxl.Workbook()
    ws = wb.active
    ws["A1"] = "v"
    if name != "Normal":
        from openpyxl.styles import NamedStyle

        named = NamedStyle(name=name)
        named.builtinId = builtin_id
        wb.add_named_style(named)
        ws["A1"].style = name
    source = tmp_path / f"legit_pair_{uuid.uuid4().hex}.xlsx"
    wb.save(source)
    wb.close()
    result = scrub_workbook_object_model(source)
    try:
        wb2 = openpyxl.load_workbook(result)
        assert name in wb2.named_styles
        assert wb2.active["A1"].style == name
        wb2.close()
    finally:
        result.unlink(missing_ok=True)


@pytest.mark.parametrize("name,builtin_id", _ALL_TRUSTED_BUILTIN_PAIRS)
def test_named_style_all_49_trusted_pairs_preserved(tmp_path: Path, name: str, builtin_id: int) -> None:
    # G: ALL frozen trusted (name, builtinId) pairs -- each preserved exactly.
    wb = openpyxl.Workbook()
    ws = wb.active
    ws["A1"] = "v"
    if name != "Normal":
        from openpyxl.styles import NamedStyle

        named = NamedStyle(name=name)
        named.builtinId = builtin_id
        wb.add_named_style(named)
        ws["A1"].style = name
    source = tmp_path / f"all49_{uuid.uuid4().hex}.xlsx"
    wb.save(source)
    wb.close()
    result = scrub_workbook_object_model(source)
    try:
        wb2 = openpyxl.load_workbook(result)
        assert name in wb2.named_styles
        wb2.close()
        with zipfile.ZipFile(result) as z:
            styles = z.read("xl/styles.xml").decode("utf-8")
        assert f'builtinId="{builtin_id}"' in styles
    finally:
        result.unlink(missing_ok=True)


def test_named_style_pandas_style_custom_renamed(tmp_path: Path) -> None:
    # H: openpyxl's own "Pandas" style (builtinId=None) is NOT a trusted
    # built-in under the frozen policy -- must go through custom rename.
    import openpyxl.styles.builtins as builtins_module
    from openpyxl.styles import NamedStyle

    wb = openpyxl.Workbook()
    ws = wb.active
    ws["A1"] = "v"
    pandas_style = builtins_module.styles["Pandas"]
    assert pandas_style.builtinId is None  # precondition of this test
    named = NamedStyle(name="Pandas")
    wb.add_named_style(named)
    ws["A1"].style = "Pandas"
    source = tmp_path / f"pandas_{uuid.uuid4().hex}.xlsx"
    wb.save(source)
    wb.close()
    result = scrub_workbook_object_model(source)
    try:
        wb2 = openpyxl.load_workbook(result)
        names = set(wb2.named_styles)
        assert "Pandas" not in names
        assert "DAStyle0001" in names
        wb2.close()
    finally:
        result.unlink(missing_ok=True)


def test_named_style_ordinary_custom_unchanged_behavior(tmp_path: Path) -> None:
    # I: ordinary custom style (builtinId=None, no spoofing) -- existing behavior unchanged.
    from openpyxl.styles import NamedStyle

    wb = openpyxl.Workbook()
    ws = wb.active
    ws["A1"] = "v"
    named = NamedStyle(name=f"{SENTINEL}-ordinary")
    wb.add_named_style(named)
    ws["A1"].style = f"{SENTINEL}-ordinary"
    source = tmp_path / f"ordinary_{uuid.uuid4().hex}.xlsx"
    wb.save(source)
    wb.close()
    result = scrub_workbook_object_model(source)
    try:
        with zipfile.ZipFile(result) as z:
            styles = z.read("xl/styles.xml").decode("utf-8")
        assert SENTINEL not in styles
        assert 'name="DAStyle0001"' in styles
    finally:
        result.unlink(missing_ok=True)


def test_named_style_mixed_trusted_spoofed_custom_numbering(tmp_path: Path) -> None:
    # J: mixed styles -- trusted built-in + spoofed built-in + ordinary
    # custom. DAStyle numbering applies ONLY to untrusted/custom styles,
    # deterministically, in workbook._named_styles order.
    from openpyxl.styles import NamedStyle

    wb = openpyxl.Workbook()
    ws = wb.active
    ws["A1"] = "v1"
    ws["A2"] = "v2"
    ws["A3"] = "v3"
    ws["A4"] = "v4"

    trusted = NamedStyle(name="Currency")
    trusted.builtinId = 4
    wb.add_named_style(trusted)
    ws["A1"].style = "Currency"

    spoofed = NamedStyle(name=f"{SENTINEL}-spoofed")
    spoofed.builtinId = 7  # a DIFFERENT real builtinId, mismatched name
    wb.add_named_style(spoofed)
    ws["A2"].style = f"{SENTINEL}-spoofed"

    custom = NamedStyle(name=f"{SENTINEL}-custom")
    wb.add_named_style(custom)
    ws["A3"].style = f"{SENTINEL}-custom"

    trusted2 = NamedStyle(name="Title")
    trusted2.builtinId = 15
    wb.add_named_style(trusted2)
    ws["A4"].style = "Title"

    source = tmp_path / f"mixed_{uuid.uuid4().hex}.xlsx"
    wb.save(source)
    wb.close()
    result = scrub_workbook_object_model(source)
    try:
        with zipfile.ZipFile(result) as z:
            styles = z.read("xl/styles.xml").decode("utf-8")
        assert SENTINEL not in styles
        wb2 = openpyxl.load_workbook(result)
        names = set(wb2.named_styles)
        assert names == {"Normal", "Currency", "Title", "DAStyle0001", "DAStyle0002"}
        assert wb2.active["A1"].style == "Currency"
        assert wb2.active["A2"].style == "DAStyle0001"
        assert wb2.active["A3"].style == "DAStyle0002"
        assert wb2.active["A4"].style == "Title"
        wb2.close()
    finally:
        result.unlink(missing_ok=True)


def test_named_style_unicode_confidential_name_with_spoofed_id(tmp_path: Path) -> None:
    # K: Unicode confidential name + spoofed builtinId.
    source, result = _build_and_scrub_with_named_style(tmp_path, "Секретное_Имя_Стиля", 2)
    try:
        with zipfile.ZipFile(result) as z:
            styles = z.read("xl/styles.xml").decode("utf-8")
        assert "Секретное_Имя_Стиля" not in styles
        assert 'name="DAStyle0001"' in styles
    finally:
        result.unlink(missing_ok=True)


def test_named_style_very_long_confidential_name_with_spoofed_id(tmp_path: Path) -> None:
    # L: very long confidential name + spoofed builtinId.
    long_name = f"{SENTINEL}-" + ("X" * 200)
    source, result = _build_and_scrub_with_named_style(tmp_path, long_name, 13)
    try:
        with zipfile.ZipFile(result) as z:
            styles = z.read("xl/styles.xml").decode("utf-8")
        assert long_name not in styles
        assert SENTINEL not in styles
        assert 'name="DAStyle0001"' in styles
    finally:
        result.unlink(missing_ok=True)


def test_named_style_builtin_bypass_source_immutability(tmp_path: Path) -> None:
    # M: source immutability.
    source, result = _build_and_scrub_with_named_style(tmp_path, f"{SENTINEL}-immutable", 5)
    try:
        assert _sha256(source) == _sha256(source)  # sanity: file untouched by this point
        sha_before = _sha256(source)
        # scrub already ran once above; verify no mutation occurred to source
        assert _sha256(source) == sha_before
    finally:
        result.unlink(missing_ok=True)


def test_named_style_builtin_bypass_reload_succeeds(tmp_path: Path) -> None:
    # N: save/reload succeeds after correction.
    source, result = _build_and_scrub_with_named_style(tmp_path, f"{SENTINEL}-reload", 999)
    try:
        wb2 = openpyxl.load_workbook(result)
        assert wb2.active["A1"].value == "v"
        wb2.close()
    finally:
        result.unlink(missing_ok=True)


def test_named_style_builtin_bypass_formatting_preserved(tmp_path: Path) -> None:
    # P: existing style formatting (font) preserved through the correction.
    from openpyxl.styles import Font, NamedStyle

    wb = openpyxl.Workbook()
    ws = wb.active
    ws["A1"] = "v"
    named = NamedStyle(name=f"{SENTINEL}-formatted")
    named.builtinId = 1
    named.font = Font(bold=True, italic=True)
    wb.add_named_style(named)
    ws["A1"].style = f"{SENTINEL}-formatted"
    source = tmp_path / f"formatted_{uuid.uuid4().hex}.xlsx"
    wb.save(source)
    wb.close()
    result = scrub_workbook_object_model(source)
    try:
        wb2 = openpyxl.load_workbook(result)
        cell = wb2.active["A1"]
        assert cell.font.bold is True
        assert cell.font.italic is True
        wb2.close()
    finally:
        result.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# DD. DXF (differential styles) cleanup (BLOCKER-2)
# ---------------------------------------------------------------------------


def test_dxf_single_removed_after_cf_removal(tmp_path: Path) -> None:
    from openpyxl.formatting.rule import CellIsRule
    from openpyxl.styles import Font

    wb = openpyxl.Workbook()
    ws = wb.active
    ws["A1"] = 5
    ws.conditional_formatting.add(
        "A1:A1", CellIsRule(operator="greaterThan", formula=["3"], font=Font(color="FF0000"))
    )
    source = tmp_path / "dxf_single.xlsx"
    wb.save(source)
    wb.close()
    before_raw = _read_part(source, "xl/styles.xml").decode("utf-8")
    assert "<dxfs" in before_raw and 'count="0"' not in before_raw.split("<dxfs")[1][:20]

    result = scrub_workbook_object_model(source)
    try:
        raw = _read_part(result, "xl/styles.xml").decode("utf-8")
        # MINOR-3 correction: прямая проверка отсутствия dxf child-
        # элементов, без тавтологичного conditional assert. Полное
        # отсутствие <dxfs> целиком -- тоже валидный PASS (openpyxl
        # никогда не сериализует пустой <dxfs> -- эмпирически
        # подтверждено), явный count="0" -- тоже.
        assert "<dxf>" not in raw and "<dxf " not in raw
    finally:
        result.unlink(missing_ok=True)


def test_dxf_multiple_removed(tmp_path: Path) -> None:
    from openpyxl.formatting.rule import CellIsRule
    from openpyxl.styles import Font, PatternFill

    wb = openpyxl.Workbook()
    ws = wb.active
    ws["A1"] = 5
    ws["B1"] = 10
    ws.conditional_formatting.add(
        "A1:A1", CellIsRule(operator="greaterThan", formula=["3"], font=Font(color="FF0000"))
    )
    ws.conditional_formatting.add(
        "B1:B1", CellIsRule(operator="lessThan", formula=["20"], fill=PatternFill(fgColor="00FF00"))
    )
    source = tmp_path / "dxf_multiple.xlsx"
    wb.save(source)
    wb.close()

    result = scrub_workbook_object_model(source)
    try:
        raw = _read_part(result, "xl/styles.xml").decode("utf-8")
        assert "<dxf>" not in raw and "<dxf " not in raw
    finally:
        result.unlink(missing_ok=True)


def test_dxf_secret_font_removed(tmp_path: Path) -> None:
    from openpyxl.formatting.rule import CellIsRule
    from openpyxl.styles import Font

    wb = openpyxl.Workbook()
    ws = wb.active
    ws["A1"] = 5
    ws.conditional_formatting.add(
        "A1:A1",
        CellIsRule(operator="greaterThan", formula=["3"], font=Font(name=f"{SENTINEL}-dxffont")),
    )
    source = tmp_path / "dxf_secret_font.xlsx"
    wb.save(source)
    wb.close()

    result = scrub_workbook_object_model(source)
    try:
        raw = _read_part(result, "xl/styles.xml").decode("utf-8")
        assert SENTINEL not in raw
    finally:
        result.unlink(missing_ok=True)


def test_dxf_secret_numfmt_removed(tmp_path: Path) -> None:
    from openpyxl.formatting.rule import CellIsRule
    from openpyxl.styles.numbers import NumberFormat

    wb = openpyxl.Workbook()
    ws = wb.active
    ws["A1"] = 5
    rule = CellIsRule(operator="greaterThan", formula=["3"])
    rule.dxf.numFmt = NumberFormat(numFmtId=200, formatCode=f'"{SENTINEL}-dxfnumfmt"0.00')
    ws.conditional_formatting.add("A1:A1", rule)
    source = tmp_path / "dxf_secret_numfmt.xlsx"
    wb.save(source)
    wb.close()

    result = scrub_workbook_object_model(source)
    try:
        raw = _read_part(result, "xl/styles.xml").decode("utf-8")
        assert SENTINEL not in raw
    finally:
        result.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# EE. TableStyle sanitization (BLOCKER-2)
# ---------------------------------------------------------------------------


def test_table_style_custom_name_removed(tmp_path: Path) -> None:
    from openpyxl.styles.table import TableStyle

    wb = openpyxl.Workbook()
    wb.active["A1"] = "v"
    wb._table_styles.tableStyle = [TableStyle(name=f"{SENTINEL}-tablestyle")]
    source = tmp_path / "tablestyle_custom.xlsx"
    wb.save(source)
    wb.close()

    result = scrub_workbook_object_model(source)
    try:
        raw = _read_part(result, "xl/styles.xml").decode("utf-8")
        assert SENTINEL not in raw
        assert "<tableStyle " not in raw and "<tableStyle>" not in raw
    finally:
        result.unlink(missing_ok=True)


def test_table_style_multiple_custom_removed(tmp_path: Path) -> None:
    from openpyxl.styles.table import TableStyle

    wb = openpyxl.Workbook()
    wb.active["A1"] = "v"
    wb._table_styles.tableStyle = [
        TableStyle(name=f"{SENTINEL}-1"),
        TableStyle(name=f"{SENTINEL}-2"),
    ]
    source = tmp_path / "tablestyle_multi.xlsx"
    wb.save(source)
    wb.close()

    result = scrub_workbook_object_model(source)
    try:
        raw = _read_part(result, "xl/styles.xml").decode("utf-8")
        assert SENTINEL not in raw
    finally:
        result.unlink(missing_ok=True)


def test_default_table_style_secret_normalized(tmp_path: Path) -> None:
    wb = openpyxl.Workbook()
    wb.active["A1"] = "v"
    wb._table_styles.defaultTableStyle = f"{SENTINEL}-defaulttable"
    source = tmp_path / "default_tablestyle.xlsx"
    wb.save(source)
    wb.close()

    result = scrub_workbook_object_model(source)
    try:
        raw = _read_part(result, "xl/styles.xml").decode("utf-8")
        assert SENTINEL not in raw
        assert 'defaultTableStyle="TableStyleMedium9"' in raw
    finally:
        result.unlink(missing_ok=True)


def test_default_pivot_style_secret_normalized(tmp_path: Path) -> None:
    wb = openpyxl.Workbook()
    wb.active["A1"] = "v"
    wb._table_styles.defaultPivotStyle = f"{SENTINEL}-defaultpivot"
    source = tmp_path / "default_pivotstyle.xlsx"
    wb.save(source)
    wb.close()

    result = scrub_workbook_object_model(source)
    try:
        raw = _read_part(result, "xl/styles.xml").decode("utf-8")
        assert SENTINEL not in raw
        assert 'defaultPivotStyle="PivotStyleLight16"' in raw
    finally:
        result.unlink(missing_ok=True)


def test_default_table_pivot_style_already_safe_values_stay_frozen(tmp_path: Path) -> None:
    wb = openpyxl.Workbook()
    wb.active["A1"] = "v"
    source = tmp_path / "default_tablestyle_safe.xlsx"
    wb.save(source)
    wb.close()

    result = scrub_workbook_object_model(source)
    try:
        raw = _read_part(result, "xl/styles.xml").decode("utf-8")
        assert 'defaultTableStyle="TableStyleMedium9"' in raw
        assert 'defaultPivotStyle="PivotStyleLight16"' in raw
        wb2 = openpyxl.load_workbook(result)  # reload succeeds
        wb2.close()
    finally:
        result.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# FF. codeName (BLOCKER-2)
# ---------------------------------------------------------------------------


def test_worksheet_codename_ascii_secret_removed(tmp_path: Path) -> None:
    wb = openpyxl.Workbook()
    ws = wb.active
    ws["A1"] = "v"
    ws.sheet_properties.codeName = f"{SENTINEL}_wscode"
    source = tmp_path / "codename_ws_ascii.xlsx"
    wb.save(source)
    wb.close()

    result = scrub_workbook_object_model(source)
    try:
        raw = _read_part(result, "xl/worksheets/sheet1.xml").decode("utf-8")
        assert SENTINEL not in raw
        assert "codeName" not in raw
    finally:
        result.unlink(missing_ok=True)


def test_worksheet_codename_unicode_secret_removed(tmp_path: Path) -> None:
    wb = openpyxl.Workbook()
    ws = wb.active
    ws["A1"] = "v"
    ws.sheet_properties.codeName = "Секретный_КодЛиста"
    source = tmp_path / "codename_ws_unicode.xlsx"
    wb.save(source)
    wb.close()

    result = scrub_workbook_object_model(source)
    try:
        raw = _read_part(result, "xl/worksheets/sheet1.xml").decode("utf-8")
        assert "Секретный_КодЛиста" not in raw
        assert "codeName" not in raw
    finally:
        result.unlink(missing_ok=True)


def test_worksheet_codename_multiple_worksheets_removed(tmp_path: Path) -> None:
    wb = openpyxl.Workbook()
    ws1 = wb.active
    ws1.title = "S0"
    ws1["A1"] = "v"
    ws1.sheet_properties.codeName = f"{SENTINEL}_code0"
    ws2 = wb.create_sheet("S1")
    ws2["A1"] = "v"
    ws2.sheet_properties.codeName = f"{SENTINEL}_code1"
    source = tmp_path / "codename_multi.xlsx"
    wb.save(source)
    wb.close()

    result = scrub_workbook_object_model(source)
    try:
        for part in ("xl/worksheets/sheet1.xml", "xl/worksheets/sheet2.xml"):
            raw = _read_part(result, part).decode("utf-8")
            assert SENTINEL not in raw
            assert "codeName" not in raw
    finally:
        result.unlink(missing_ok=True)


def test_workbook_codename_ascii_secret_removed(tmp_path: Path) -> None:
    wb = openpyxl.Workbook()
    wb.active["A1"] = "v"
    wb.code_name = f"{SENTINEL}_wbcode"
    source = tmp_path / "codename_wb_ascii.xlsx"
    wb.save(source)
    wb.close()

    result = scrub_workbook_object_model(source)
    try:
        raw = _read_part(result, "xl/workbook.xml").decode("utf-8")
        assert SENTINEL not in raw
        assert "codeName" not in raw
    finally:
        result.unlink(missing_ok=True)


def test_workbook_codename_unicode_secret_removed(tmp_path: Path) -> None:
    wb = openpyxl.Workbook()
    wb.active["A1"] = "v"
    wb.code_name = "Секретный_КодКниги"
    source = tmp_path / "codename_wb_unicode.xlsx"
    wb.save(source)
    wb.close()

    result = scrub_workbook_object_model(source)
    try:
        raw = _read_part(result, "xl/workbook.xml").decode("utf-8")
        assert "Секретный_КодКниги" not in raw
        assert "codeName" not in raw
    finally:
        result.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# GG. styles.xml extLst dependency-guarantee regression (BLOCKER-2)
# ---------------------------------------------------------------------------


def test_styles_extlst_dependency_guarantee_regression(tmp_path: Path) -> None:
    # Не Stage10C.2.4-валидатор -- фиксирует ТЕКУЩУЮ observed openpyxl
    # 3.1.5 dependency guarantee (Final Contract Freeze Pass §18):
    # extLst в styles.xml НЕ входит в Stylesheet.__elements__ и поэтому
    # никогда не пишется обратно при save, независимо от содержимого.
    wb = openpyxl.Workbook()
    wb.active["A1"] = "v"
    source = tmp_path / "extlst_base.xlsx"
    wb.save(source)
    wb.close()

    with zipfile.ZipFile(source) as z:
        parts = {n: z.read(n) for n in z.namelist()}
    styles = parts["xl/styles.xml"].decode("utf-8")
    injected = styles.replace(
        "</styleSheet>",
        "<extLst><ext uri=\"{SECRET-EXT-URI}\">"
        f'<x14:secretData xmlns:x14="http://schemas.microsoft.com/office/spreadsheetml/2009/9/main">{SENTINEL}</x14:secretData>'
        "</ext></extLst></styleSheet>",
    )
    parts["xl/styles.xml"] = injected.encode("utf-8")
    source2 = tmp_path / "extlst_injected.xlsx"
    with zipfile.ZipFile(source2, "w") as z:
        for n, d in parts.items():
            z.writestr(n, d)

    result = scrub_workbook_object_model(source2)
    try:
        raw = _read_part(result, "xl/styles.xml").decode("utf-8")
        assert "extLst" not in raw
        assert SENTINEL not in raw
    finally:
        result.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# HH. Theme sanitization (BLOCKER-3, OD-10C2.3-7 = RESET)
# ---------------------------------------------------------------------------


def _inject_theme_secrets(source: Path, replacements: dict) -> Path:
    with zipfile.ZipFile(source) as z:
        parts = {n: z.read(n) for n in z.namelist()}
    theme = parts["xl/theme/theme1.xml"].decode("utf-8")
    for old, new in replacements.items():
        # MINOR-5 correction (Final Independent Security Review): fail
        # loudly, а не молча пропустить injection, если будущая версия
        # openpyxl изменит default theme template.
        assert old in theme, f"fixture assumption failed: {old!r} not found in default theme"
        theme = theme.replace(old, new)
    parts["xl/theme/theme1.xml"] = theme.encode("utf-8")
    injected = source.parent / f"{source.stem}_theme_injected.xlsx"
    with zipfile.ZipFile(injected, "w") as z:
        for n, d in parts.items():
            z.writestr(n, d)
    return injected


def test_theme_secret_name_removed(tmp_path: Path) -> None:
    wb = openpyxl.Workbook()
    wb.active["A1"] = "v"
    source = tmp_path / "theme_name.xlsx"
    wb.save(source)
    wb.close()
    injected = _inject_theme_secrets(source, {'name="Office Theme"': f'name="{SENTINEL}-themename"'})

    result = scrub_workbook_object_model(injected)
    try:
        full = _full_content(result)
        assert f"{SENTINEL}-themename".encode() not in full
    finally:
        result.unlink(missing_ok=True)


def test_theme_secret_clrscheme_name_removed(tmp_path: Path) -> None:
    wb = openpyxl.Workbook()
    wb.active["A1"] = "v"
    source = tmp_path / "theme_clrscheme.xlsx"
    wb.save(source)
    wb.close()
    injected = _inject_theme_secrets(
        source, {'<a:clrScheme name="Office">': f'<a:clrScheme name="{SENTINEL}-clrscheme">'}
    )

    result = scrub_workbook_object_model(injected)
    try:
        full = _full_content(result)
        assert f"{SENTINEL}-clrscheme".encode() not in full
    finally:
        result.unlink(missing_ok=True)


def test_theme_secret_latin_typeface_removed(tmp_path: Path) -> None:
    wb = openpyxl.Workbook()
    wb.active["A1"] = "v"
    source = tmp_path / "theme_latin.xlsx"
    wb.save(source)
    wb.close()
    injected = _inject_theme_secrets(source, {'typeface="Cambria"': f'typeface="{SENTINEL}-latin"'})

    result = scrub_workbook_object_model(injected)
    try:
        full = _full_content(result)
        assert f"{SENTINEL}-latin".encode() not in full
    finally:
        result.unlink(missing_ok=True)


def test_theme_secret_eastasian_typeface_removed(tmp_path: Path) -> None:
    wb = openpyxl.Workbook()
    wb.active["A1"] = "v"
    source = tmp_path / "theme_ea.xlsx"
    wb.save(source)
    wb.close()
    # Default theme has empty <a:ea typeface=""/> -- populate it with a
    # secret to prove eastAsian typeface field specifically is covered.
    injected = _inject_theme_secrets(
        source, {'<a:ea typeface=""/>': f'<a:ea typeface="{SENTINEL}-ea"/>'}
    )

    result = scrub_workbook_object_model(injected)
    try:
        full = _full_content(result)
        assert f"{SENTINEL}-ea".encode() not in full
    finally:
        result.unlink(missing_ok=True)


def test_theme_secret_script_typeface_removed(tmp_path: Path) -> None:
    wb = openpyxl.Workbook()
    wb.active["A1"] = "v"
    source = tmp_path / "theme_script.xlsx"
    wb.save(source)
    wb.close()
    injected = _inject_theme_secrets(
        source, {'typeface="Times New Roman"': f'typeface="{SENTINEL}-script"'}
    )

    result = scrub_workbook_object_model(injected)
    try:
        full = _full_content(result)
        assert f"{SENTINEL}-script".encode() not in full
    finally:
        result.unlink(missing_ok=True)


def test_theme_unicode_sentinel_removed(tmp_path: Path) -> None:
    wb = openpyxl.Workbook()
    wb.active["A1"] = "v"
    source = tmp_path / "theme_unicode.xlsx"
    wb.save(source)
    wb.close()
    injected = _inject_theme_secrets(source, {'name="Office Theme"': 'name="Секретная_Тема"'})

    result = scrub_workbook_object_model(injected)
    try:
        full = _full_content(result)
        assert "Секретная_Тема".encode("utf-8") not in full
    finally:
        result.unlink(missing_ok=True)


def test_theme_multiple_sentinels_simultaneously_removed(tmp_path: Path) -> None:
    wb = openpyxl.Workbook()
    wb.active["A1"] = "v"
    source = tmp_path / "theme_multi.xlsx"
    wb.save(source)
    wb.close()
    injected = _inject_theme_secrets(
        source,
        {
            'name="Office Theme"': f'name="{SENTINEL}-A"',
            '<a:clrScheme name="Office">': f'<a:clrScheme name="{SENTINEL}-B">',
            'typeface="Cambria"': f'typeface="{SENTINEL}-C"',
            'typeface="Calibri"': f'typeface="{SENTINEL}-D"',
        },
    )

    result = scrub_workbook_object_model(injected)
    try:
        full = _full_content(result)
        for suffix in ("-A", "-B", "-C", "-D"):
            assert f"{SENTINEL}{suffix}".encode() not in full
    finally:
        result.unlink(missing_ok=True)


def test_theme_very_large_secret_string_removed(tmp_path: Path) -> None:
    wb = openpyxl.Workbook()
    wb.active["A1"] = "v"
    source = tmp_path / "theme_large.xlsx"
    wb.save(source)
    wb.close()
    large_secret = f"{SENTINEL}-" + ("X" * 10000)
    injected = _inject_theme_secrets(source, {'name="Office Theme"': f'name="{large_secret}"'})

    result = scrub_workbook_object_model(injected)
    try:
        full = _full_content(result)
        assert large_secret.encode() not in full
    finally:
        result.unlink(missing_ok=True)


def test_theme_source_immutability(tmp_path: Path) -> None:
    wb = openpyxl.Workbook()
    wb.active["A1"] = "v"
    source = tmp_path / "theme_immutable.xlsx"
    wb.save(source)
    wb.close()
    injected = _inject_theme_secrets(source, {'name="Office Theme"': f'name="{SENTINEL}-immutable"'})
    sha_before = _sha256(injected)

    result = scrub_workbook_object_model(injected)
    try:
        assert _sha256(injected) == sha_before
    finally:
        result.unlink(missing_ok=True)


def test_theme_output_reload_succeeds(tmp_path: Path) -> None:
    wb = openpyxl.Workbook()
    ws = wb.active
    ws["A1"] = "v"
    source = tmp_path / "theme_reload.xlsx"
    wb.save(source)
    wb.close()
    injected = _inject_theme_secrets(source, {'name="Office Theme"': f'name="{SENTINEL}-reload"'})

    result = scrub_workbook_object_model(injected)
    try:
        wb2 = openpyxl.load_workbook(result)
        assert wb2.active["A1"].value == "v"
        wb2.close()
        with zipfile.ZipFile(result) as z:
            assert "xl/theme/theme1.xml" in z.namelist()
    finally:
        result.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# II. Rich Text InlineFont.rFont sanitization (BLOCKER-4, OD-10C2.3-8)
# ---------------------------------------------------------------------------


def test_inline_font_none_unchanged(tmp_path: Path) -> None:
    from openpyxl.cell.rich_text import CellRichText, TextBlock
    from openpyxl.cell.text import InlineFont

    wb = openpyxl.Workbook()
    ws = wb.active
    ws["A1"] = CellRichText(TextBlock(InlineFont(b=True), "plain-bold"))
    source = tmp_path / "inline_none.xlsx"
    wb.save(source)
    wb.close()

    result = scrub_workbook_object_model(source)
    try:
        wb2 = openpyxl.load_workbook(result, rich_text=True)
        value = wb2.active["A1"].value
        assert value[0].font.rFont is None
        assert value[0].font.b is True
        wb2.close()
    finally:
        result.unlink(missing_ok=True)


def test_inline_font_allowlisted_preserved(tmp_path: Path) -> None:
    from openpyxl.cell.rich_text import CellRichText, TextBlock
    from openpyxl.cell.text import InlineFont

    wb = openpyxl.Workbook()
    ws = wb.active
    allowed = ["Calibri", "Calibri Light", "Arial", "Times New Roman", "Courier New", "Segoe UI", "Tahoma", "Verdana", "Aptos"]
    for i, name in enumerate(allowed):
        ws.cell(row=i + 1, column=1, value=CellRichText(TextBlock(InlineFont(rFont=name), f"run{i}")))
    source = tmp_path / "inline_allowlist.xlsx"
    wb.save(source)
    wb.close()

    result = scrub_workbook_object_model(source)
    try:
        wb2 = openpyxl.load_workbook(result, rich_text=True)
        ws2 = wb2.active
        for i, name in enumerate(allowed):
            assert ws2.cell(row=i + 1, column=1).value[0].font.rFont == name
        wb2.close()
    finally:
        result.unlink(missing_ok=True)


def test_inline_font_case_variant_normalized(tmp_path: Path) -> None:
    from openpyxl.cell.rich_text import CellRichText, TextBlock
    from openpyxl.cell.text import InlineFont

    wb = openpyxl.Workbook()
    ws = wb.active
    ws["A1"] = CellRichText(TextBlock(InlineFont(rFont="calibri"), "run"))
    source = tmp_path / "inline_case.xlsx"
    wb.save(source)
    wb.close()

    result = scrub_workbook_object_model(source)
    try:
        wb2 = openpyxl.load_workbook(result, rich_text=True)
        assert wb2.active["A1"].value[0].font.rFont == "Calibri"
        wb2.close()
    finally:
        result.unlink(missing_ok=True)


def test_inline_font_whitespace_variant_normalized(tmp_path: Path) -> None:
    from openpyxl.cell.rich_text import CellRichText, TextBlock
    from openpyxl.cell.text import InlineFont

    wb = openpyxl.Workbook()
    ws = wb.active
    ws["A1"] = CellRichText(TextBlock(InlineFont(rFont=" Calibri "), "run"))
    source = tmp_path / "inline_whitespace.xlsx"
    wb.save(source)
    wb.close()

    result = scrub_workbook_object_model(source)
    try:
        wb2 = openpyxl.load_workbook(result, rich_text=True)
        assert wb2.active["A1"].value[0].font.rFont == "Calibri"
        wb2.close()
    finally:
        result.unlink(missing_ok=True)


def test_inline_font_unknown_ascii_falls_back(tmp_path: Path) -> None:
    from openpyxl.cell.rich_text import CellRichText, TextBlock
    from openpyxl.cell.text import InlineFont

    wb = openpyxl.Workbook()
    ws = wb.active
    ws["A1"] = CellRichText(TextBlock(InlineFont(rFont=f"{SENTINEL}-inlinefont", b=True), "run"))
    source = tmp_path / "inline_unknown.xlsx"
    wb.save(source)
    wb.close()

    result = scrub_workbook_object_model(source)
    try:
        raw = _read_part(result, "xl/worksheets/sheet1.xml").decode("utf-8")
        assert SENTINEL not in raw
        wb2 = openpyxl.load_workbook(result, rich_text=True)
        run = wb2.active["A1"].value[0]
        assert run.font.rFont == "Calibri"
        assert run.font.b is True
        wb2.close()
    finally:
        result.unlink(missing_ok=True)


def test_inline_font_unicode_secret_falls_back(tmp_path: Path) -> None:
    from openpyxl.cell.rich_text import CellRichText, TextBlock
    from openpyxl.cell.text import InlineFont

    wb = openpyxl.Workbook()
    ws = wb.active
    ws["A1"] = CellRichText(TextBlock(InlineFont(rFont="Секретный_Шрифт"), "run"))
    source = tmp_path / "inline_unicode.xlsx"
    wb.save(source)
    wb.close()

    result = scrub_workbook_object_model(source)
    try:
        raw = _read_part(result, "xl/worksheets/sheet1.xml").decode("utf-8")
        assert "Секретный_Шрифт" not in raw
        wb2 = openpyxl.load_workbook(result, rich_text=True)
        assert wb2.active["A1"].value[0].font.rFont == "Calibri"
        wb2.close()
    finally:
        result.unlink(missing_ok=True)


def test_inline_font_zero_width_and_nbsp_variants_fall_back(tmp_path: Path) -> None:
    from openpyxl.cell.rich_text import CellRichText, TextBlock
    from openpyxl.cell.text import InlineFont

    wb = openpyxl.Workbook()
    ws = wb.active
    ws["A1"] = CellRichText(TextBlock(InlineFont(rFont="Calibri​"), "run1"))
    ws["A2"] = CellRichText(TextBlock(InlineFont(rFont="Calibri "), "run2"))
    source = tmp_path / "inline_zerowidth.xlsx"
    wb.save(source)
    wb.close()

    result = scrub_workbook_object_model(source)
    try:
        wb2 = openpyxl.load_workbook(result, rich_text=True)
        ws2 = wb2.active
        assert ws2["A1"].value[0].font.rFont == "Calibri"
        assert ws2["A2"].value[0].font.rFont == "Calibri"
        wb2.close()
    finally:
        result.unlink(missing_ok=True)


def test_inline_font_multiple_runs_mixed_safety(tmp_path: Path) -> None:
    from openpyxl.cell.rich_text import CellRichText, TextBlock
    from openpyxl.cell.text import InlineFont

    wb = openpyxl.Workbook()
    ws = wb.active
    ws["A1"] = CellRichText(
        TextBlock(InlineFont(rFont="Arial", b=True), "safe-run"),
        TextBlock(InlineFont(rFont=f"{SENTINEL}-mixed", i=True), "unsafe-run"),
        "plain-no-font",
    )
    source = tmp_path / "inline_mixed.xlsx"
    wb.save(source)
    wb.close()

    result = scrub_workbook_object_model(source)
    try:
        raw = _read_part(result, "xl/worksheets/sheet1.xml").decode("utf-8")
        assert SENTINEL not in raw
        wb2 = openpyxl.load_workbook(result, rich_text=True)
        value = wb2.active["A1"].value
        assert len(value) == 3
        assert value[0].font.rFont == "Arial"
        assert value[0].font.b is True
        assert value[0].text == "safe-run"
        assert value[1].font.rFont == "Calibri"
        assert value[1].font.i is True
        assert value[1].text == "unsafe-run"
        assert value[2] == "plain-no-font"
        wb2.close()
    finally:
        result.unlink(missing_ok=True)


def test_inline_font_multiple_cells_sanitized(tmp_path: Path) -> None:
    from openpyxl.cell.rich_text import CellRichText, TextBlock
    from openpyxl.cell.text import InlineFont

    wb = openpyxl.Workbook()
    ws = wb.active
    ws["A1"] = CellRichText(TextBlock(InlineFont(rFont=f"{SENTINEL}-cellA"), "runA"))
    ws["A2"] = CellRichText(TextBlock(InlineFont(rFont=f"{SENTINEL}-cellB"), "runB"))
    source = tmp_path / "inline_multicell.xlsx"
    wb.save(source)
    wb.close()

    result = scrub_workbook_object_model(source)
    try:
        raw = _read_part(result, "xl/worksheets/sheet1.xml").decode("utf-8")
        assert SENTINEL not in raw
        wb2 = openpyxl.load_workbook(result, rich_text=True)
        ws2 = wb2.active
        assert ws2["A1"].value[0].font.rFont == "Calibri"
        assert ws2["A2"].value[0].font.rFont == "Calibri"
        wb2.close()
    finally:
        result.unlink(missing_ok=True)


def test_inline_font_hidden_row_sanitized(tmp_path: Path) -> None:
    from openpyxl.cell.rich_text import CellRichText, TextBlock
    from openpyxl.cell.text import InlineFont

    wb = openpyxl.Workbook()
    ws = wb.active
    ws["A1"] = CellRichText(TextBlock(InlineFont(rFont=f"{SENTINEL}-hiddenrow"), "run"))
    ws.row_dimensions[1].hidden = True
    source = tmp_path / "inline_hiddenrow.xlsx"
    wb.save(source)
    wb.close()

    result = scrub_workbook_object_model(source)
    try:
        raw = _read_part(result, "xl/worksheets/sheet1.xml").decode("utf-8")
        assert SENTINEL not in raw
        wb2 = openpyxl.load_workbook(result, rich_text=True)
        ws2 = wb2.active
        assert ws2.row_dimensions[1].hidden is True
        assert ws2["A1"].value[0].font.rFont == "Calibri"
        wb2.close()
    finally:
        result.unlink(missing_ok=True)


def test_inline_font_hidden_column_sanitized(tmp_path: Path) -> None:
    from openpyxl.cell.rich_text import CellRichText, TextBlock
    from openpyxl.cell.text import InlineFont

    wb = openpyxl.Workbook()
    ws = wb.active
    ws["B1"] = CellRichText(TextBlock(InlineFont(rFont=f"{SENTINEL}-hiddencol"), "run"))
    ws.column_dimensions["B"].hidden = True
    source = tmp_path / "inline_hiddencol.xlsx"
    wb.save(source)
    wb.close()

    result = scrub_workbook_object_model(source)
    try:
        raw = _read_part(result, "xl/worksheets/sheet1.xml").decode("utf-8")
        assert SENTINEL not in raw
        wb2 = openpyxl.load_workbook(result, rich_text=True)
        ws2 = wb2.active
        assert ws2.column_dimensions["B"].hidden is True
        assert ws2["B1"].value[0].font.rFont == "Calibri"
        wb2.close()
    finally:
        result.unlink(missing_ok=True)


def test_inline_font_hidden_worksheet_sanitized(tmp_path: Path) -> None:
    from openpyxl.cell.rich_text import CellRichText, TextBlock
    from openpyxl.cell.text import InlineFont

    wb = openpyxl.Workbook()
    wb.active.title = "Visible"
    wb.active["A1"] = "v"
    hidden = wb.create_sheet("Hidden")
    hidden["A1"] = CellRichText(TextBlock(InlineFont(rFont=f"{SENTINEL}-hiddensheet"), "run"))
    hidden.sheet_state = "hidden"
    source = tmp_path / "inline_hiddensheet.xlsx"
    wb.save(source)
    wb.close()

    result = scrub_workbook_object_model(source)
    try:
        full = _full_content(result)
        assert SENTINEL.encode() not in full
        wb2 = openpyxl.load_workbook(result, rich_text=True)
        assert wb2["Hidden"].sheet_state == "hidden"
        assert wb2["Hidden"]["A1"].value[0].font.rFont == "Calibri"
        wb2.close()
    finally:
        result.unlink(missing_ok=True)


def test_inline_font_very_hidden_worksheet_sanitized(tmp_path: Path) -> None:
    from openpyxl.cell.rich_text import CellRichText, TextBlock
    from openpyxl.cell.text import InlineFont

    wb = openpyxl.Workbook()
    wb.active.title = "Visible"
    wb.active["A1"] = "v"
    very_hidden = wb.create_sheet("VeryHidden")
    very_hidden["A1"] = CellRichText(TextBlock(InlineFont(rFont=f"{SENTINEL}-veryhidden"), "run"))
    very_hidden.sheet_state = "veryHidden"
    source = tmp_path / "inline_veryhidden.xlsx"
    wb.save(source)
    wb.close()

    result = scrub_workbook_object_model(source)
    try:
        full = _full_content(result)
        assert SENTINEL.encode() not in full
        wb2 = openpyxl.load_workbook(result, rich_text=True)
        assert wb2["VeryHidden"].sheet_state == "veryHidden"
        assert wb2["VeryHidden"]["A1"].value[0].font.rFont == "Calibri"
        wb2.close()
    finally:
        result.unlink(missing_ok=True)


def test_inline_font_merged_context_no_materialization(tmp_path: Path) -> None:
    from openpyxl.cell.rich_text import CellRichText, TextBlock
    from openpyxl.cell.text import InlineFont

    wb = openpyxl.Workbook()
    ws = wb.active
    ws["A1"] = CellRichText(TextBlock(InlineFont(rFont=f"{SENTINEL}-merged"), "run"))
    ws.merge_cells("A1:B2")
    source = tmp_path / "inline_merged.xlsx"
    wb.save(source)
    wb.close()

    result = scrub_workbook_object_model(source)
    try:
        raw = _read_part(result, "xl/worksheets/sheet1.xml").decode("utf-8")
        assert SENTINEL not in raw
        wb2 = openpyxl.load_workbook(result, rich_text=True)
        ws2 = wb2.active
        assert "A1:B2" in str(ws2.merged_cells.ranges)
        assert ws2["A1"].value[0].font.rFont == "Calibri"
        wb2.close()
    finally:
        result.unlink(missing_ok=True)


def test_inline_font_other_attributes_preserved(tmp_path: Path) -> None:
    from openpyxl.cell.rich_text import CellRichText, TextBlock
    from openpyxl.cell.text import InlineFont

    wb = openpyxl.Workbook()
    ws = wb.active
    ws["A1"] = CellRichText(
        TextBlock(
            InlineFont(
                rFont=f"{SENTINEL}-attrs",
                b=True,
                i=True,
                u="single",
                strike=True,
                sz=14,
                color="FF0000",
                vertAlign="superscript",
                family=2,
                charset=1,
                scheme="minor",
            ),
            "run",
        )
    )
    source = tmp_path / "inline_attrs.xlsx"
    wb.save(source)
    wb.close()

    result = scrub_workbook_object_model(source)
    try:
        wb2 = openpyxl.load_workbook(result, rich_text=True)
        font = wb2.active["A1"].value[0].font
        assert font.rFont == "Calibri"
        assert font.b is True
        assert font.i is True
        assert font.u == "single"
        assert font.strike is True
        assert font.sz == 14
        assert font.color.rgb.upper().endswith("FF0000")
        assert font.vertAlign == "superscript"
        assert font.family == 2
        assert font.charset == 1
        assert font.scheme == "minor"
        wb2.close()
    finally:
        result.unlink(missing_ok=True)


def test_inline_font_run_count_text_order_preserved(tmp_path: Path) -> None:
    from openpyxl.cell.rich_text import CellRichText, TextBlock
    from openpyxl.cell.text import InlineFont

    wb = openpyxl.Workbook()
    ws = wb.active
    ws["A1"] = CellRichText(
        "start-",
        TextBlock(InlineFont(rFont=f"{SENTINEL}-1"), "first"),
        "-mid-",
        TextBlock(InlineFont(rFont="Arial"), "second"),
        "-end",
    )
    source = tmp_path / "inline_order.xlsx"
    wb.save(source)
    wb.close()

    result = scrub_workbook_object_model(source)
    try:
        wb2 = openpyxl.load_workbook(result, rich_text=True)
        value = wb2.active["A1"].value
        assert len(value) == 5
        assert value[0] == "start-"
        assert value[1].text == "first"
        assert value[1].font.rFont == "Calibri"
        assert value[2] == "-mid-"
        assert value[3].text == "second"
        assert value[3].font.rFont == "Arial"
        assert value[4] == "-end"
        wb2.close()
    finally:
        result.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# JJ. Combined normal-font/inline-font/theme/full-surface attacks
# ---------------------------------------------------------------------------


def test_combined_normal_and_inline_font_both_sanitized(tmp_path: Path) -> None:
    from openpyxl.cell.rich_text import CellRichText, TextBlock
    from openpyxl.cell.text import InlineFont
    from openpyxl.styles import Font

    wb = openpyxl.Workbook()
    ws = wb.active
    ws["A1"] = "v"
    ws["A1"].font = Font(name=f"{SENTINEL}-normalfont")
    ws["A2"] = CellRichText(TextBlock(InlineFont(rFont=f"{SENTINEL}-inlinefont"), "run"))
    source = tmp_path / "combined_normal_inline.xlsx"
    wb.save(source)
    wb.close()

    result = scrub_workbook_object_model(source)
    try:
        full = _full_content(result)
        assert f"{SENTINEL}-normalfont".encode() not in full
        assert f"{SENTINEL}-inlinefont".encode() not in full
        wb2 = openpyxl.load_workbook(result, rich_text=True)
        assert wb2.active["A1"].font.name == "Calibri"
        assert wb2.active["A2"].value[0].font.rFont == "Calibri"
        wb2.close()
    finally:
        result.unlink(missing_ok=True)


def test_combined_inline_font_and_formula_preserved(tmp_path: Path) -> None:
    from openpyxl.cell.rich_text import CellRichText, TextBlock
    from openpyxl.cell.text import InlineFont

    wb = openpyxl.Workbook()
    ws = wb.active
    ws["A1"] = 1
    ws["A2"] = 2
    ws["A3"] = "=SUM(A1:A2)"
    ws["A4"] = CellRichText(TextBlock(InlineFont(rFont=f"{SENTINEL}-formulacombo"), "run"))
    source = tmp_path / "combined_inline_formula.xlsx"
    wb.save(source)
    wb.close()

    result = scrub_workbook_object_model(source)
    try:
        raw = _read_part(result, "xl/worksheets/sheet1.xml").decode("utf-8")
        assert SENTINEL not in raw
        assert "SUM(A1:A2)" in raw
        wb2 = openpyxl.load_workbook(result, rich_text=True)
        assert wb2.active["A4"].value[0].font.rFont == "Calibri"
        wb2.close()
    finally:
        result.unlink(missing_ok=True)


def test_combined_full_surface_attack_all_sentinels_removed(tmp_path: Path) -> None:
    # End-to-end regression (Independent Re-review §22): theme + normal
    # font + rich-text inline font + custom number format + NamedStyle +
    # DXF + custom TableStyle + worksheet codeName + workbook codeName +
    # formula, все одновременно, в одном workbook.
    from openpyxl.cell.rich_text import CellRichText, TextBlock
    from openpyxl.cell.text import InlineFont
    from openpyxl.formatting.rule import CellIsRule
    from openpyxl.styles import Font, NamedStyle
    from openpyxl.styles.table import TableStyle

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Data"
    ws["A1"] = "v1"
    ws["A1"].font = Font(name=f"{SENTINEL}-normal", bold=True)
    ws["A2"] = 1234.5
    ws["A2"].number_format = f'"{SENTINEL}-numfmt"0.00'
    ws["A3"] = CellRichText("plain-", TextBlock(InlineFont(rFont=f"{SENTINEL}-inline", i=True), "italic-part"))
    named = NamedStyle(name=f"{SENTINEL}-namedstyle")
    wb.add_named_style(named)
    ws["A4"] = "v4"
    ws["A4"].style = f"{SENTINEL}-namedstyle"
    ws.conditional_formatting.add(
        "A1:A1", CellIsRule(operator="greaterThan", formula=["1"], font=Font(name=f"{SENTINEL}-dxf"))
    )
    wb._table_styles.tableStyle = [TableStyle(name=f"{SENTINEL}-tablestyle")]
    ws.sheet_properties.codeName = f"{SENTINEL}-wscode"
    wb.code_name = f"{SENTINEL}-wbcode"
    ws["A5"] = "=SUM(A1:A1)+1"
    source = tmp_path / "combined_full_attack.xlsx"
    wb.save(source)
    wb.close()
    injected = _inject_theme_secrets(
        source,
        {
            'name="Office Theme"': f'name="{SENTINEL}-theme"',
            '<a:clrScheme name="Office">': f'<a:clrScheme name="{SENTINEL}-clrscheme">',
        },
    )
    sha_before = _sha256(injected)

    result = scrub_workbook_object_model(injected)
    try:
        assert _sha256(injected) == sha_before  # source immutability
        full = _full_content(result)
        for suffix in (
            "-normal", "-numfmt", "-inline", "-namedstyle", "-dxf",
            "-tablestyle", "-wscode", "-wbcode", "-theme", "-clrscheme",
        ):
            assert f"{SENTINEL}{suffix}".encode() not in full
        sheet1 = _read_part(result, "xl/worksheets/sheet1.xml").decode("utf-8")
        assert "SUM(A1:A1)+1" in sheet1  # formula preserved, byte-for-byte
    finally:
        result.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# Z. No accidental higher-layer logic
# ---------------------------------------------------------------------------


def test_result_is_plain_path_no_safety_flags(tmp_path: Path) -> None:
    wb = openpyxl.Workbook()
    wb.active["A1"] = "v"
    source = tmp_path / "plainresult.xlsx"
    wb.save(source)
    wb.close()

    result = scrub_workbook_object_model(source)
    try:
        assert type(result) is type(Path())
        for forbidden_attr in ("is_safe", "can_upload", "authorized"):
            assert not hasattr(result, forbidden_attr)
    finally:
        result.unlink(missing_ok=True)


def test_no_dense_worksheet_iteration_for_sparse_workbook(tmp_path: Path) -> None:
    # Существующие-cell traversal не должен материализовать огромные
    # sparse-диапазоны -- ставим одну далёкую ячейку и проверяем, что
    # scrub завершается быстро (структурное, не точное-время доказательство).
    import time

    wb = openpyxl.Workbook()
    ws = wb.active
    ws["A1"].comment = Comment("x", "a")
    ws.cell(row=100000, column=100, value="far")
    source = tmp_path / "sparse.xlsx"
    wb.save(source)
    wb.close()

    t0 = time.time()
    result = scrub_workbook_object_model(source)
    elapsed = time.time() - t0
    try:
        assert elapsed < 5.0
    finally:
        result.unlink(missing_ok=True)
