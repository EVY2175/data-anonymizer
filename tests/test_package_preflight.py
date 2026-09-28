"""
Тесты Stage 10C.2.1 — Resource + ZIP/package structural preflight
(app.scrub.preflight.preflight_xlsx_package).

Все фикстуры — synthetic (сборка через openpyxl/zipfile), без единого
реального confidential значения.

Дорогостоящие лимиты (MAX_ZIP_ENTRIES/MAX_TOTAL_UNCOMPRESSED_BYTES/
MAX_COMPRESSION_RATIO/MAX_WORKSHEETS/MAX_NONEMPTY_CELLS) тестируются через
monkeypatch этих же модульных констант на маленькие значения + маленькие
реальные фикстуры, реально их превышающие — это тестирует настоящую
production-ветку сравнения (не mock самой проверки), не требуя создавать
физически огромные файлы.

Два имени записи ZIP (backslash, NUL-байт) не могут быть добросовестно
воспроизведены через zipfile.ZipFile.writestr() — сам модуль zipfile
нормализует "\\" в "/" и обрезает имя по первому NUL-байту при записи
(проверено эмпирически при разработке этого набора). Для этих двух
случаев pure-функция _is_safe_member_name проверяется напрямую — то же
производственное решение, что видит preflight при чтении ZIP,
сформированного каким-либо иным (не Python zipfile) инструментом.
Аналогично encryption-бит: zipfile.writestr пересчитывает flag_bits при
записи, поэтому проверяется напрямую через zipfile.ZipInfo с
установленным флагом, переданный в реальную production-функцию
_validate_zip_structure_and_collect_names.
"""

from __future__ import annotations

import io
import os
import re
import traceback
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path
from unittest import mock

import openpyxl
import pytest

from app.scrub.errors import PackageScrubError, PackageScrubReason
from app.scrub.models import PackagePreflightResult
from app.scrub.preflight import (
    MAX_COMPRESSION_RATIO,
    MAX_NONEMPTY_CELLS,
    MAX_TOTAL_UNCOMPRESSED_BYTES,
    MAX_WORKSHEETS,
    MAX_XLSX_SIZE_BYTES,
    MAX_ZIP_ENTRIES,
    _count_nonempty_cells_in_part,
    _is_nonempty_cell_element,
    _is_safe_member_name,
    _validate_zip_structure_and_collect_names,
    preflight_xlsx_package,
)

_SML_NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"

PASSWORD_SENTINEL = "SENTINEL_SECRET_PATH_COMPONENT"


# ---------------------------------------------------------------------------
# Фикстуры/помощники
# ---------------------------------------------------------------------------


def _minimal_workbook_parts() -> dict[str, bytes]:
    """Байты частей минимального, реально валидного .xlsx (через openpyxl)."""
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Data"
    ws["A1"] = "v"
    buf = io.BytesIO()
    wb.save(buf)
    wb.close()
    buf.seek(0)
    with zipfile.ZipFile(buf) as z:
        return {name: z.read(name) for name in z.namelist()}


def _write_xlsx(path: Path, parts: dict[str, bytes], extra: list[tuple[str, bytes]] | None = None) -> None:
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        for name, data in parts.items():
            z.writestr(name, data)
        for name, data in extra or []:
            z.writestr(name, data)


def _multi_sheet_workbook(path: Path, sheet_count: int, cells_per_sheet: int) -> None:
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Sheet0"
    for r in range(1, cells_per_sheet + 1):
        ws.cell(row=r, column=1, value=f"v{r}")
    for i in range(1, sheet_count):
        extra_ws = wb.create_sheet(f"Sheet{i}")
        for r in range(1, cells_per_sheet + 1):
            extra_ws.cell(row=r, column=1, value=f"v{r}")
    wb.save(path)
    wb.close()


@pytest.fixture()
def base_parts() -> dict[str, bytes]:
    return _minimal_workbook_parts()


# ---------------------------------------------------------------------------
# A. Basic file constraints
# ---------------------------------------------------------------------------


def test_valid_minimal_xlsx_passes(tmp_path: Path, base_parts) -> None:
    p = tmp_path / "valid.xlsx"
    _write_xlsx(p, base_parts)
    result = preflight_xlsx_package(p)
    assert isinstance(result, PackagePreflightResult)
    assert result.sheet_count == 1
    assert result.nonempty_cell_count == 1


def test_uppercase_extension_accepted(tmp_path: Path, base_parts) -> None:
    p = tmp_path / "valid.XLSX"
    _write_xlsx(p, base_parts)
    result = preflight_xlsx_package(p)
    assert result.sheet_count == 1


def test_wrong_extension_rejected(tmp_path: Path, base_parts) -> None:
    p = tmp_path / "valid.txt"
    _write_xlsx(p, base_parts)
    with pytest.raises(PackageScrubError) as exc_info:
        preflight_xlsx_package(p)
    assert exc_info.value.reason is PackageScrubReason.INVALID_INPUT_PACKAGE


@pytest.mark.parametrize("bad_extension", [".xls", ".xlsm", ".xlsb", ".xltx", ".xltm", ".csv"])
def test_unsupported_office_extensions_rejected(tmp_path: Path, base_parts, bad_extension: str) -> None:
    p = tmp_path / f"valid{bad_extension}"
    _write_xlsx(p, base_parts)
    with pytest.raises(PackageScrubError) as exc_info:
        preflight_xlsx_package(p)
    assert exc_info.value.reason is PackageScrubReason.INVALID_INPUT_PACKAGE


def test_missing_path_rejected(tmp_path: Path) -> None:
    with pytest.raises(PackageScrubError) as exc_info:
        preflight_xlsx_package(tmp_path / "does_not_exist.xlsx")
    assert exc_info.value.reason is PackageScrubReason.INVALID_INPUT_PACKAGE


def test_directory_path_rejected(tmp_path: Path) -> None:
    d = tmp_path / "looks_like_a_file.xlsx"
    d.mkdir()
    with pytest.raises(PackageScrubError) as exc_info:
        preflight_xlsx_package(d)
    assert exc_info.value.reason is PackageScrubReason.INVALID_INPUT_PACKAGE


def test_path_type_validation() -> None:
    with pytest.raises(TypeError):
        preflight_xlsx_package(12345)  # type: ignore[arg-type]


def test_physical_size_over_limit_rejected(tmp_path: Path, base_parts, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("app.scrub.preflight.MAX_XLSX_SIZE_BYTES", 100)
    p = tmp_path / "toolarge.xlsx"
    _write_xlsx(p, base_parts)
    assert p.stat().st_size > 100
    with pytest.raises(PackageScrubError) as exc_info:
        preflight_xlsx_package(p)
    assert exc_info.value.reason is PackageScrubReason.RESOURCE_LIMIT_EXCEEDED


def test_physical_size_within_limit_allowed(tmp_path: Path, base_parts) -> None:
    p = tmp_path / "normal.xlsx"
    _write_xlsx(p, base_parts)
    assert p.stat().st_size <= MAX_XLSX_SIZE_BYTES
    preflight_xlsx_package(p)  # не должно поднимать исключение


# ---------------------------------------------------------------------------
# B. ZIP validity / resource limits (central-directory metadata only)
# ---------------------------------------------------------------------------


def test_malformed_zip_rejected(tmp_path: Path) -> None:
    p = tmp_path / "malformed.xlsx"
    p.write_bytes(b"this is not a zip file at all")
    with pytest.raises(PackageScrubError) as exc_info:
        preflight_xlsx_package(p)
    assert exc_info.value.reason is PackageScrubReason.INVALID_INPUT_PACKAGE


def test_encryption_bit_rejected_direct() -> None:
    # zipfile.writestr пересчитывает flag_bits самостоятельно на запись —
    # реальный encryption-бит воспроизведён напрямую на zipfile.ZipInfo,
    # ровно то, что видит production-функция при чтении central directory
    # файла, сформированного не через Python zipfile.
    zi = zipfile.ZipInfo("xl/workbook.xml")
    zi.flag_bits = 1
    zi.file_size = 10
    zi.compress_size = 10
    with pytest.raises(PackageScrubError) as exc_info:
        _validate_zip_structure_and_collect_names([zi])
    assert exc_info.value.reason is PackageScrubReason.INVALID_INPUT_PACKAGE


def test_too_many_zip_entries_rejected(tmp_path: Path, base_parts, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("app.scrub.preflight.MAX_ZIP_ENTRIES", 5)
    p = tmp_path / "manyentries.xlsx"
    extra = [(f"xl/extra/part{i}.xml", b"<x/>") for i in range(10)]
    _write_xlsx(p, base_parts, extra)
    with pytest.raises(PackageScrubError) as exc_info:
        preflight_xlsx_package(p)
    assert exc_info.value.reason is PackageScrubReason.RESOURCE_LIMIT_EXCEEDED


def test_zip_entry_count_within_limit_allowed(tmp_path: Path, base_parts, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("app.scrub.preflight.MAX_ZIP_ENTRIES", 100)
    p = tmp_path / "fewentries.xlsx"
    _write_xlsx(p, base_parts)
    preflight_xlsx_package(p)


def test_total_uncompressed_size_over_limit_rejected(
    tmp_path: Path, base_parts, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("app.scrub.preflight.MAX_TOTAL_UNCOMPRESSED_BYTES", 100)
    monkeypatch.setattr("app.scrub.preflight.MAX_COMPRESSION_RATIO", 10 ** 9)
    p = tmp_path / "bigtotal.xlsx"
    _write_xlsx(p, base_parts, [("xl/extra/big.bin", b"\x00" * 1000)])
    with pytest.raises(PackageScrubError) as exc_info:
        preflight_xlsx_package(p)
    assert exc_info.value.reason is PackageScrubReason.RESOURCE_LIMIT_EXCEEDED


def test_compression_ratio_over_limit_rejected(tmp_path: Path, base_parts, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("app.scrub.preflight.MAX_COMPRESSION_RATIO", 2)
    monkeypatch.setattr("app.scrub.preflight.MAX_TOTAL_UNCOMPRESSED_BYTES", 10 ** 9)
    p = tmp_path / "ratio.xlsx"
    # 10000 нулевых байт сжимаются DEFLATE в считанные десятки байт -> ratio >> 2.
    _write_xlsx(p, base_parts, [("xl/extra/repeat.bin", b"\x00" * 10_000)])
    with pytest.raises(PackageScrubError) as exc_info:
        preflight_xlsx_package(p)
    assert exc_info.value.reason is PackageScrubReason.RESOURCE_LIMIT_EXCEEDED


def test_compression_ratio_empty_entry_no_false_reject(monkeypatch: pytest.MonkeyPatch) -> None:
    # file_size == 0 и compress_size == 0 не должны давать деление на ноль
    # или ложный reject (ratio трактуется как 0, не как "бесконечность").
    # Проверяется напрямую на pure-функции (как в test_encryption_bit_rejected_direct),
    # чтобы не зависеть от естественного коэффициента сжатия реальных XML-частей.
    monkeypatch.setattr("app.scrub.preflight.MAX_COMPRESSION_RATIO", 1)
    zi = zipfile.ZipInfo("xl/extra/empty.bin")
    zi.file_size = 0
    zi.compress_size = 0
    _validate_zip_structure_and_collect_names([zi])  # не должно поднимать исключение


# ---------------------------------------------------------------------------
# C. Package name safety
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "name",
    [
        "xl\\evil.xml",          # backslash
        "xl/ev\x00il.xml",       # NUL byte
        "../evil.xml",           # traversal segment
        "/evil.xml",             # leading slash (absolute)
        "xl//evil.xml",          # internal empty segment
        "xl/./evil.xml",         # "." segment
        "",                      # empty name
    ],
)
def test_is_safe_member_name_rejects_unsafe(name: str) -> None:
    assert _is_safe_member_name(name) is False


@pytest.mark.parametrize(
    "name",
    [
        "xl/workbook.xml",
        "xl/worksheets/sheet1.xml",
        "xl/extra/",  # directory entry, trailing slash
        "docProps/core.xml",
    ],
)
def test_is_safe_member_name_accepts_safe(name: str) -> None:
    assert _is_safe_member_name(name) is True


def test_traversal_member_rejected_end_to_end(tmp_path: Path, base_parts) -> None:
    p = tmp_path / "traversal.xlsx"
    _write_xlsx(p, base_parts, [("../evil.xml", b"<x/>")])
    with pytest.raises(PackageScrubError) as exc_info:
        preflight_xlsx_package(p)
    assert exc_info.value.reason is PackageScrubReason.UNSUPPORTED_PACKAGE_CONTENT


def test_leading_slash_member_rejected_end_to_end(tmp_path: Path, base_parts) -> None:
    p = tmp_path / "leadingslash.xlsx"
    zi = zipfile.ZipInfo("/evil.xml")
    with zipfile.ZipFile(p, "w") as z:
        for name, data in base_parts.items():
            z.writestr(name, data)
        z.writestr(zi, b"<x/>")
    with pytest.raises(PackageScrubError) as exc_info:
        preflight_xlsx_package(p)
    assert exc_info.value.reason is PackageScrubReason.UNSUPPORTED_PACKAGE_CONTENT


def test_internal_double_slash_rejected_end_to_end(tmp_path: Path, base_parts) -> None:
    p = tmp_path / "doubleslash.xlsx"
    _write_xlsx(p, base_parts, [("xl//evil.xml", b"<x/>")])
    with pytest.raises(PackageScrubError) as exc_info:
        preflight_xlsx_package(p)
    assert exc_info.value.reason is PackageScrubReason.UNSUPPORTED_PACKAGE_CONTENT


def test_directory_entry_with_trailing_slash_allowed(tmp_path: Path, base_parts) -> None:
    p = tmp_path / "dirtrail.xlsx"
    _write_xlsx(p, base_parts, [("xl/extra/", b"")])
    preflight_xlsx_package(p)  # не должно поднимать исключение


# ---------------------------------------------------------------------------
# D. Duplicate / collision handling
# ---------------------------------------------------------------------------


def test_duplicate_exact_name_rejected(tmp_path: Path, base_parts) -> None:
    p = tmp_path / "dup.xlsx"
    with zipfile.ZipFile(p, "w") as z:
        for name, data in base_parts.items():
            z.writestr(name, data)
        z.writestr("xl/workbook.xml", base_parts["xl/workbook.xml"])
    with pytest.raises(PackageScrubError) as exc_info:
        preflight_xlsx_package(p)
    assert exc_info.value.reason is PackageScrubReason.UNSUPPORTED_PACKAGE_CONTENT


def test_case_collision_name_rejected(tmp_path: Path, base_parts) -> None:
    p = tmp_path / "casecollision.xlsx"
    with zipfile.ZipFile(p, "w") as z:
        for name, data in base_parts.items():
            z.writestr(name, data)
        z.writestr("XL/WORKBOOK.XML", base_parts["xl/workbook.xml"])
    with pytest.raises(PackageScrubError) as exc_info:
        preflight_xlsx_package(p)
    assert exc_info.value.reason is PackageScrubReason.UNSUPPORTED_PACKAGE_CONTENT


# ---------------------------------------------------------------------------
# E. workbook.xml / relationship resolution
# ---------------------------------------------------------------------------


def test_missing_workbook_xml_rejected(tmp_path: Path, base_parts) -> None:
    parts = {k: v for k, v in base_parts.items() if k != "xl/workbook.xml"}
    p = tmp_path / "nowb.xlsx"
    _write_xlsx(p, parts)
    with pytest.raises(PackageScrubError) as exc_info:
        preflight_xlsx_package(p)
    assert exc_info.value.reason is PackageScrubReason.INVALID_INPUT_PACKAGE


def test_malformed_workbook_xml_rejected(tmp_path: Path, base_parts) -> None:
    parts = dict(base_parts)
    parts["xl/workbook.xml"] = b"<not-valid-xml"
    p = tmp_path / "badwb.xlsx"
    _write_xlsx(p, parts)
    with pytest.raises(PackageScrubError) as exc_info:
        preflight_xlsx_package(p)
    assert exc_info.value.reason is PackageScrubReason.INVALID_INPUT_PACKAGE


def test_missing_workbook_rels_rejected(tmp_path: Path, base_parts) -> None:
    parts = {k: v for k, v in base_parts.items() if k != "xl/_rels/workbook.xml.rels"}
    p = tmp_path / "norels.xlsx"
    _write_xlsx(p, parts)
    with pytest.raises(PackageScrubError) as exc_info:
        preflight_xlsx_package(p)
    assert exc_info.value.reason is PackageScrubReason.INVALID_INPUT_PACKAGE


def test_external_worksheet_relationship_rejected(tmp_path: Path, base_parts) -> None:
    rels = base_parts["xl/_rels/workbook.xml.rels"].decode("utf-8")
    assert 'Target="/xl/worksheets/sheet1.xml"' in rels
    rels2 = rels.replace(
        'Target="/xl/worksheets/sheet1.xml"',
        'Target="file:///C:/evil.xlsx" TargetMode="External"',
    )
    parts = dict(base_parts)
    parts["xl/_rels/workbook.xml.rels"] = rels2.encode("utf-8")
    p = tmp_path / "external.xlsx"
    _write_xlsx(p, parts)
    with pytest.raises(PackageScrubError) as exc_info:
        preflight_xlsx_package(p)
    assert exc_info.value.reason is PackageScrubReason.UNSUPPORTED_PACKAGE_CONTENT


def test_malformed_worksheet_xml_rejected(tmp_path: Path, base_parts) -> None:
    parts = dict(base_parts)
    parts["xl/worksheets/sheet1.xml"] = b"<not-valid"
    p = tmp_path / "badsheet.xlsx"
    _write_xlsx(p, parts)
    with pytest.raises(PackageScrubError) as exc_info:
        preflight_xlsx_package(p)
    assert exc_info.value.reason is PackageScrubReason.INVALID_INPUT_PACKAGE


def test_valid_relationship_resolution_end_to_end(tmp_path: Path, base_parts) -> None:
    p = tmp_path / "resolved.xlsx"
    _write_xlsx(p, base_parts)
    result = preflight_xlsx_package(p)
    assert result.sheet_count == 1
    assert result.nonempty_cell_count == 1


# ---------------------------------------------------------------------------
# F. Worksheet count semantics
# ---------------------------------------------------------------------------


def test_worksheet_count_over_limit_rejected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("app.scrub.preflight.MAX_WORKSHEETS", 3)
    p = tmp_path / "manysheets.xlsx"
    _multi_sheet_workbook(p, sheet_count=5, cells_per_sheet=1)
    with pytest.raises(PackageScrubError) as exc_info:
        preflight_xlsx_package(p)
    assert exc_info.value.reason is PackageScrubReason.RESOURCE_LIMIT_EXCEEDED


def test_worksheet_count_within_limit_allowed(tmp_path: Path) -> None:
    p = tmp_path / "fewsheets.xlsx"
    _multi_sheet_workbook(p, sheet_count=3, cells_per_sheet=2)
    result = preflight_xlsx_package(p)
    assert result.sheet_count == 3


# ---------------------------------------------------------------------------
# G. Nonempty-cell semantics (pure helper)
# ---------------------------------------------------------------------------


def _cell(xml_fragment: str) -> ET.Element:
    return ET.fromstring(f'<c xmlns="{_SML_NS}" r="A1"{xml_fragment}')


def test_styled_but_empty_cell_is_not_nonempty() -> None:
    assert _is_nonempty_cell_element(_cell(' s="3"/>')) is False


def test_completely_empty_cell_is_not_nonempty() -> None:
    assert _is_nonempty_cell_element(_cell("/>")) is False


def test_inline_string_cell_is_nonempty() -> None:
    elem = ET.fromstring(f'<c xmlns="{_SML_NS}" r="A1" t="inlineStr"><is><t>x</t></is></c>')
    assert _is_nonempty_cell_element(elem) is True


def test_shared_string_reference_cell_is_nonempty() -> None:
    elem = ET.fromstring(f'<c xmlns="{_SML_NS}" r="A1" t="s"><v>0</v></c>')
    assert _is_nonempty_cell_element(elem) is True


def test_formula_cell_is_nonempty() -> None:
    elem = ET.fromstring(f'<c xmlns="{_SML_NS}" r="A1"><f>A2+A3</f><v>8</v></c>')
    assert _is_nonempty_cell_element(elem) is True


def test_numeric_cell_is_nonempty() -> None:
    elem = ET.fromstring(f'<c xmlns="{_SML_NS}" r="A1"><v>5</v></c>')
    assert _is_nonempty_cell_element(elem) is True


def test_boolean_cell_is_nonempty() -> None:
    elem = ET.fromstring(f'<c xmlns="{_SML_NS}" r="A1" t="b"><v>1</v></c>')
    assert _is_nonempty_cell_element(elem) is True


def test_error_cell_is_nonempty() -> None:
    elem = ET.fromstring(f'<c xmlns="{_SML_NS}" r="A1" t="e"><v>#DIV/0!</v></c>')
    assert _is_nonempty_cell_element(elem) is True


# ---------------------------------------------------------------------------
# H. Streaming / early-abort
# ---------------------------------------------------------------------------


def _sheet_xml_with_rows(count: int) -> bytes:
    rows = "".join(f'<row r="{r}"><c r="A{r}"><v>{r}</v></c></row>' for r in range(1, count + 1))
    return f'<worksheet xmlns="{_SML_NS}"><sheetData>{rows}</sheetData></worksheet>'.encode("utf-8")


def test_nonempty_cell_count_exact(tmp_path: Path) -> None:
    p = tmp_path / "cells.xlsx"
    with zipfile.ZipFile(p, "w") as z:
        z.writestr("sheet.xml", _sheet_xml_with_rows(100))
    with zipfile.ZipFile(p) as z:
        assert _count_nonempty_cells_in_part(z, "sheet.xml", limit=1000) == 100


def test_nonempty_cell_count_early_abort(tmp_path: Path) -> None:
    p = tmp_path / "cells_abort.xlsx"
    with zipfile.ZipFile(p, "w") as z:
        z.writestr("sheet.xml", _sheet_xml_with_rows(100))
    with zipfile.ZipFile(p) as z:
        # limit=50 -> обрыв сразу после превышения, НЕ дочитывая все 100 строк.
        count = _count_nonempty_cells_in_part(z, "sheet.xml", limit=50)
    assert 50 < count < 100


def test_nonempty_cell_count_over_limit_rejected_end_to_end(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("app.scrub.preflight.MAX_NONEMPTY_CELLS", 10)
    p = tmp_path / "toomanycells.xlsx"
    _multi_sheet_workbook(p, sheet_count=1, cells_per_sheet=25)
    with pytest.raises(PackageScrubError) as exc_info:
        preflight_xlsx_package(p)
    assert exc_info.value.reason is PackageScrubReason.RESOURCE_LIMIT_EXCEEDED


def test_nonempty_cell_count_aggregated_across_sheets(tmp_path: Path) -> None:
    p = tmp_path / "multisheetcells.xlsx"
    _multi_sheet_workbook(p, sheet_count=3, cells_per_sheet=4)
    result = preflight_xlsx_package(p)
    assert result.nonempty_cell_count == 12


# ---------------------------------------------------------------------------
# I. OD-7 security
# ---------------------------------------------------------------------------


def _assert_no_sentinel_leak(exc: BaseException, sentinel: str = PASSWORD_SENTINEL) -> None:
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


def test_missing_path_does_not_leak_filesystem_path(tmp_path: Path) -> None:
    sensitive_dir = tmp_path / PASSWORD_SENTINEL
    with pytest.raises(PackageScrubError) as exc_info:
        preflight_xlsx_package(sensitive_dir / "missing.xlsx")
    _assert_no_sentinel_leak(exc_info.value)


def test_malformed_zip_does_not_leak_filesystem_path(tmp_path: Path) -> None:
    sensitive_dir = tmp_path / PASSWORD_SENTINEL
    sensitive_dir.mkdir()
    p = sensitive_dir / "malformed.xlsx"
    p.write_bytes(b"not a zip")
    with pytest.raises(PackageScrubError) as exc_info:
        preflight_xlsx_package(p)
    _assert_no_sentinel_leak(exc_info.value)


@pytest.mark.parametrize("reason", list(PackageScrubReason))
def test_package_scrub_error_fixed_messages_never_contain_sentinel(reason: PackageScrubReason) -> None:
    exc = PackageScrubError(reason)
    _assert_no_sentinel_leak(exc)


def test_no_confidential_fields_in_preflight_result(tmp_path: Path, base_parts) -> None:
    p = tmp_path / "valid.xlsx"
    _write_xlsx(p, base_parts)
    result = preflight_xlsx_package(p)
    field_names = {f for f in result.__dataclass_fields__}
    assert field_names == {
        "zip_entry_count",
        "total_uncompressed_bytes",
        "sheet_count",
        "nonempty_cell_count",
    }


# ---------------------------------------------------------------------------
# J. openpyxl никогда не вызывается
# ---------------------------------------------------------------------------


def test_openpyxl_load_workbook_never_called(tmp_path: Path, base_parts) -> None:
    p = tmp_path / "valid.xlsx"
    _write_xlsx(p, base_parts)

    def _boom(*args, **kwargs):  # noqa: ANN002, ANN003
        raise AssertionError("preflight_xlsx_package не должен вызывать openpyxl.load_workbook")

    with mock.patch("openpyxl.load_workbook", _boom):
        result = preflight_xlsx_package(p)
    assert result.sheet_count == 1


# ---------------------------------------------------------------------------
# K. PackagePreflightResult model validation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("field_name", ["zip_entry_count", "total_uncompressed_bytes", "sheet_count", "nonempty_cell_count"])
@pytest.mark.parametrize("bad_value", [-1, True])
def test_preflight_result_rejects_invalid_counts(field_name: str, bad_value: object) -> None:
    kwargs = dict(zip_entry_count=1, total_uncompressed_bytes=1, sheet_count=1, nonempty_cell_count=1)
    kwargs[field_name] = bad_value
    with pytest.raises(ValueError):
        PackagePreflightResult(**kwargs)


def test_preflight_result_accepts_zero_counts() -> None:
    result = PackagePreflightResult(
        zip_entry_count=0, total_uncompressed_bytes=0, sheet_count=0, nonempty_cell_count=0
    )
    assert result.sheet_count == 0


# ---------------------------------------------------------------------------
# L. Post-Review Correction Pass — MAJOR-1: workbook.xml/.rels bounded parsing
# ---------------------------------------------------------------------------


def test_workbook_xml_and_rels_never_use_full_dom_parse(tmp_path: Path, base_parts) -> None:
    # Прямое доказательство, что ET.parse (non-streaming, full-DOM) больше
    # НЕ вызывается ни для workbook.xml, ни для workbook.xml.rels — только
    # ET.iterparse. preflight_xlsx_package трогает оба файла, поэтому
    # один этот тест доказывает bounded-parsing свойство для обоих сразу.
    p = tmp_path / "valid.xlsx"
    _write_xlsx(p, base_parts)

    def _boom(*args, **kwargs):  # noqa: ANN002, ANN003
        raise AssertionError("workbook.xml/.rels не должны парситься через ET.parse (full DOM)")

    with mock.patch("app.scrub.preflight.ET.parse", _boom):
        result = preflight_xlsx_package(p)
    assert result.sheet_count == 1


def test_workbook_sheet_count_early_abort_before_malformed_tail(
    tmp_path: Path, base_parts, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Доказательство early-abort БЕЗ гигантской фикстуры: после MAX_WORKSHEETS
    # искусственно занижен до 3, а после 5-го <sheet> в документ вставлен
    # заведомо незакрытый (malformed) XML-хвост. Если бы парсинг НЕ прервался
    # на превышении лимита сразу на 4-м <sheet>, он бы в итоге наткнулся на
    # malformed-хвост и поднял бы INVALID_INPUT_PACKAGE (parse error), а не
    # RESOURCE_LIMIT_EXCEEDED. Получение именно RESOURCE_LIMIT_EXCEEDED
    # доказывает, что документ не дочитывается до конца.
    monkeypatch.setattr("app.scrub.preflight.MAX_WORKSHEETS", 3)
    sheets_xml = "".join(
        f'<sheet name="S{i}" sheetId="{i + 1}" r:id="rId{i + 1}"/>' for i in range(5)
    )
    malformed_tail = "<this-is-not-closed"
    workbook_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
        'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
        f"<sheets>{sheets_xml}</sheets>{malformed_tail}"
    ).encode("utf-8")

    parts = dict(base_parts)
    parts["xl/workbook.xml"] = workbook_xml
    p = tmp_path / "manysheets_corrupt_tail.xlsx"
    _write_xlsx(p, parts)

    with pytest.raises(PackageScrubError) as exc_info:
        preflight_xlsx_package(p)
    assert exc_info.value.reason is PackageScrubReason.RESOURCE_LIMIT_EXCEEDED


def test_workbook_xml_malformed_tail_after_sheets_rejected(
    tmp_path: Path, base_parts
) -> None:
    # Final Minor Correction: malformed XML СТРОГО ПОСЛЕ корректно закрытого
    # </sheets> должен обнаруживаться -- до этого исправления
    # _read_workbook_sheet_rids прерывался сразу на </sheets> и не видел
    # хвост вовсе, что позволяло структурно невалидному документу пройти
    # preflight как валидному (Post-Correction Independent Re-Review).
    # Это production-path тест (через preflight_xlsx_package), а не прямой
    # тест helper'а.
    wb_xml = base_parts["xl/workbook.xml"].decode("utf-8")
    assert "</sheets>" in wb_xml
    idx = wb_xml.index("</sheets>") + len("</sheets>")
    malformed_tail = "<<<not xml at all>>>&garbage;"
    wb_xml2 = wb_xml[:idx] + malformed_tail

    parts = dict(base_parts)
    parts["xl/workbook.xml"] = wb_xml2.encode("utf-8")
    p = tmp_path / "malformed_tail_after_sheets.xlsx"
    _write_xlsx(p, parts)

    with pytest.raises(PackageScrubError) as exc_info:
        preflight_xlsx_package(p)
    assert exc_info.value.reason is PackageScrubReason.INVALID_INPUT_PACKAGE


def test_workbook_xml_malformed_tail_after_sheets_no_sentinel_leak(
    tmp_path: Path, base_parts
) -> None:
    sensitive_dir = tmp_path / PASSWORD_SENTINEL
    sensitive_dir.mkdir()
    wb_xml = base_parts["xl/workbook.xml"].decode("utf-8")
    idx = wb_xml.index("</sheets>") + len("</sheets>")
    # Хвост содержит sentinel -- он не должен просочиться ни в какую часть
    # публичного исключения (raw XML/ParseError-сообщение никогда не
    # прокидывается как есть).
    malformed_tail = f"<<<not xml, contains {PASSWORD_SENTINEL}>>>&garbage;"
    wb_xml2 = wb_xml[:idx] + malformed_tail

    parts = dict(base_parts)
    parts["xl/workbook.xml"] = wb_xml2.encode("utf-8")
    p = sensitive_dir / "malformed_tail_sentinel.xlsx"
    _write_xlsx(p, parts)

    with pytest.raises(PackageScrubError) as exc_info:
        preflight_xlsx_package(p)
    assert exc_info.value.reason is PackageScrubReason.INVALID_INPUT_PACKAGE
    _assert_no_sentinel_leak(exc_info.value)


def test_workbook_xml_valid_tail_after_sheets_accepted(
    tmp_path: Path, base_parts
) -> None:
    # Исправление не должно ошибочно требовать EOF сразу после </sheets> --
    # нормальные валидные элементы ПОСЛЕ <sheets> (definedNames, calcPr и
    # т.п., как их реально генерирует openpyxl) обязаны по-прежнему
    # приводить к успеху.
    wb_xml = base_parts["xl/workbook.xml"].decode("utf-8")
    assert "<definedNames" in wb_xml or "<calcPr" in wb_xml
    p = tmp_path / "valid_normal_tail.xlsx"
    _write_xlsx(p, base_parts)
    result = preflight_xlsx_package(p)
    assert result.sheet_count == 1
    assert result.nonempty_cell_count == 1


def test_workbook_xml_read_to_eof_never_uses_full_dom(
    tmp_path: Path, base_parts
) -> None:
    # Дочитывание хвоста до EOF по-прежнему обязано идти через ET.iterparse,
    # а не через full-DOM ET.parse/ET.fromstring или zf.read().
    p = tmp_path / "valid.xlsx"
    _write_xlsx(p, base_parts)

    def _boom(*args, **kwargs):  # noqa: ANN002, ANN003
        raise AssertionError("дочитывание хвоста workbook.xml не должно использовать full-DOM parse")

    with mock.patch("app.scrub.preflight.ET.parse", _boom), mock.patch(
        "app.scrub.preflight.ET.fromstring", _boom
    ):
        result = preflight_xlsx_package(p)
    assert result.sheet_count == 1


def test_workbook_rels_huge_irrelevant_relationship_not_retained(
    tmp_path: Path, base_parts
) -> None:
    # Огромная (несколько MiB), но НЕНУЖНАЯ (Id не входит в needed_rids)
    # relationship-запись в .rels не должна ни попасть в результат, ни
    # нарушить обработку легитимного (маленького) sheet-relationship.
    rels_xml = base_parts["xl/_rels/workbook.xml.rels"].decode("utf-8")
    # Псевдослучайный (не сжимаемый) hex, чтобы 5 MiB заявленного/реального
    # размера не срезался проверкой compression ratio раздела A раньше,
    # чем мы вообще дойдём до этой (неиспользуемой) relationship-записи.
    huge_target = os.urandom(2 * 1024 * 1024).hex()
    injected = (
        f'<Relationship Id="rIdIrrelevantHuge" '
        f'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/irrelevant" '
        f'Target="{huge_target}"/>'
    )
    rels_xml2 = rels_xml.replace("</Relationships>", injected + "</Relationships>")
    assert rels_xml2 != rels_xml

    parts = dict(base_parts)
    parts["xl/_rels/workbook.xml.rels"] = rels_xml2.encode("utf-8")
    p = tmp_path / "hugeirrelevantrel.xlsx"
    _write_xlsx(p, parts)

    result = preflight_xlsx_package(p)
    assert result.sheet_count == 1
    assert result.nonempty_cell_count == 1


def test_malformed_workbook_xml_streaming_rejected(tmp_path: Path, base_parts) -> None:
    # Malformed XML, встречающийся ПОСЛЕ валидного пролога — должен быть
    # пойман потоковым iterparse так же надёжно, как non-streaming ET.parse.
    parts = dict(base_parts)
    parts["xl/workbook.xml"] = (
        '<?xml version="1.0"?>'
        '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
        'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
        '<sheets><sheet name="S0" sheetId="1" r:id="rId1"/>'
        "<this-is-not-closed"
    ).encode("utf-8")
    p = tmp_path / "streamedbadwb.xlsx"
    _write_xlsx(p, parts)
    with pytest.raises(PackageScrubError) as exc_info:
        preflight_xlsx_package(p)
    assert exc_info.value.reason is PackageScrubReason.INVALID_INPUT_PACKAGE


def test_malformed_workbook_rels_streaming_rejected(tmp_path: Path, base_parts) -> None:
    parts = dict(base_parts)
    parts["xl/_rels/workbook.xml.rels"] = b"<Relationships><Relationship Id=" b"<not-closed"
    p = tmp_path / "streamedbadrels.xlsx"
    _write_xlsx(p, parts)
    with pytest.raises(PackageScrubError) as exc_info:
        preflight_xlsx_package(p)
    assert exc_info.value.reason is PackageScrubReason.INVALID_INPUT_PACKAGE


def test_malformed_workbook_xml_streaming_no_sentinel_leak(tmp_path: Path, base_parts) -> None:
    sensitive_dir = tmp_path / PASSWORD_SENTINEL
    sensitive_dir.mkdir()
    parts = dict(base_parts)
    parts["xl/workbook.xml"] = b"<not-valid-xml"
    p = sensitive_dir / "badwb.xlsx"
    _write_xlsx(p, parts)
    with pytest.raises(PackageScrubError) as exc_info:
        preflight_xlsx_package(p)
    _assert_no_sentinel_leak(exc_info.value)


def test_malformed_workbook_rels_streaming_no_sentinel_leak(tmp_path: Path, base_parts) -> None:
    sensitive_dir = tmp_path / PASSWORD_SENTINEL
    sensitive_dir.mkdir()
    parts = dict(base_parts)
    parts["xl/_rels/workbook.xml.rels"] = b"<not-valid-xml"
    p = sensitive_dir / "badrels.xlsx"
    _write_xlsx(p, parts)
    with pytest.raises(PackageScrubError) as exc_info:
        preflight_xlsx_package(p)
    _assert_no_sentinel_leak(exc_info.value)


# ---------------------------------------------------------------------------
# M. Post-Review Correction Pass — MINOR-1: safe close on all paths
# ---------------------------------------------------------------------------


def test_close_failure_on_success_path_is_sanitized(
    tmp_path: Path, base_parts, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Сценарий B (§10 Correction Pass): обработка была УСПЕШНОЙ, но
    # финальный zf.close() неожиданно проваливается -- результат не
    # должен считаться успешным без анализа: должен подняться новый
    # sanitized PackageScrubError, а не "тихий" успех и не raw-исключение.
    p = tmp_path / PASSWORD_SENTINEL / "valid.xlsx"
    p.parent.mkdir(parents=True)
    _write_xlsx(p, base_parts)

    def _boom_close(self):  # noqa: ANN001
        raise OSError(f"pretend close failure touching {p}")

    monkeypatch.setattr(zipfile.ZipFile, "close", _boom_close)

    with pytest.raises(PackageScrubError) as exc_info:
        preflight_xlsx_package(p)
    assert exc_info.value.reason is PackageScrubReason.INVALID_INPUT_PACKAGE
    _assert_no_sentinel_leak(exc_info.value)


def test_close_failure_does_not_mask_primary_error(
    tmp_path: Path, base_parts, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Сценарий A (§10 Correction Pass): УЖЕ есть решённая первичная
    # ошибка (malformed workbook.xml) -- сбой close() потока НЕ должен её
    # заменить/зачейнить. Ожидается ИМЕННО исходная INVALID_INPUT_PACKAGE
    # (parse error), а не какая-либо другая ошибка от самого close().
    sensitive_dir = tmp_path / PASSWORD_SENTINEL
    sensitive_dir.mkdir()
    parts = dict(base_parts)
    parts["xl/workbook.xml"] = b"<not-valid-xml"
    p = sensitive_dir / "badwb.xlsx"
    _write_xlsx(p, parts)

    def _boom_close(self):  # noqa: ANN001
        raise OSError(f"pretend close failure touching member in {p}")

    monkeypatch.setattr(zipfile.ZipExtFile, "close", _boom_close)

    with pytest.raises(PackageScrubError) as exc_info:
        preflight_xlsx_package(p)
    assert exc_info.value.reason is PackageScrubReason.INVALID_INPUT_PACKAGE
    _assert_no_sentinel_leak(exc_info.value)


def test_close_failure_on_any_member_stream_success_path_is_sanitized(
    tmp_path: Path, base_parts, monkeypatch: pytest.MonkeyPatch
) -> None:
    # _close_or_fail — один и тот же helper, используемый на успешном пути
    # закрытия ЛЮБОГО потока части (workbook.xml/.rels/worksheet). Глобальный
    # monkeypatch ZipExtFile.close триггерится на ПЕРВОМ же успешно
    # обработанном потоке (workbook.xml) -- этого достаточно, чтобы доказать
    # общее свойство: сбой close() на успешном пути для member-потока не
    # даёт молчаливый "успех" и не протекает raw-исключением.
    p = tmp_path / PASSWORD_SENTINEL / "valid.xlsx"
    p.parent.mkdir(parents=True)
    _write_xlsx(p, base_parts)

    def _boom_close(self):  # noqa: ANN001
        raise OSError(f"pretend close failure touching {p}")

    monkeypatch.setattr(zipfile.ZipExtFile, "close", _boom_close)

    with pytest.raises(PackageScrubError) as exc_info:
        preflight_xlsx_package(p)
    assert exc_info.value.reason is PackageScrubReason.INVALID_INPUT_PACKAGE
    _assert_no_sentinel_leak(exc_info.value)


# ---------------------------------------------------------------------------
# N. Post-Review Correction Pass — MINOR-2: duplicate sheet references
# ---------------------------------------------------------------------------


def test_duplicate_sheet_rid_rejected(tmp_path: Path) -> None:
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Data"
    for r in range(1, 6):
        ws.cell(row=r, column=1, value=f"v{r}")
    buf = io.BytesIO()
    wb.save(buf)
    wb.close()
    buf.seek(0)
    with zipfile.ZipFile(buf) as z:
        parts = {n: z.read(n) for n in z.namelist()}

    wb_xml = parts["xl/workbook.xml"].decode("utf-8")
    m = re.search(r"(<sheet [^/]*/>)", wb_xml)
    assert m is not None
    sheet_tag = m.group(1)
    wb_xml2 = wb_xml.replace(sheet_tag, sheet_tag + sheet_tag, 1)
    parts["xl/workbook.xml"] = wb_xml2.encode("utf-8")

    p = tmp_path / "duprid.xlsx"
    _write_xlsx(p, parts)
    with pytest.raises(PackageScrubError) as exc_info:
        preflight_xlsx_package(p)
    assert exc_info.value.reason is PackageScrubReason.INVALID_INPUT_PACKAGE


def test_different_rid_same_target_rejected(tmp_path: Path) -> None:
    wb = openpyxl.Workbook()
    wb.active.title = "S0"
    wb.create_sheet("S1")
    buf = io.BytesIO()
    wb.save(buf)
    wb.close()
    buf.seek(0)
    with zipfile.ZipFile(buf) as z:
        parts = {n: z.read(n) for n in z.namelist()}

    rels_xml = parts["xl/_rels/workbook.xml.rels"].decode("utf-8")
    sheet_targets = re.findall(r'Target="(/xl/worksheets/sheet\d+\.xml)"', rels_xml)
    assert len(sheet_targets) >= 2, sheet_targets
    first_target, second_target = sheet_targets[0], sheet_targets[1]
    rels_xml2 = rels_xml.replace(f'Target="{second_target}"', f'Target="{first_target}"', 1)
    assert rels_xml2 != rels_xml

    parts["xl/_rels/workbook.xml.rels"] = rels_xml2.encode("utf-8")
    p = tmp_path / "sametarget.xlsx"
    _write_xlsx(p, parts)
    with pytest.raises(PackageScrubError) as exc_info:
        preflight_xlsx_package(p)
    assert exc_info.value.reason is PackageScrubReason.INVALID_INPUT_PACKAGE


def test_distinct_sheets_no_double_count(tmp_path: Path) -> None:
    p = tmp_path / "distinct.xlsx"
    _multi_sheet_workbook(p, sheet_count=3, cells_per_sheet=2)
    result = preflight_xlsx_package(p)
    assert result.sheet_count == 3
    assert result.nonempty_cell_count == 6


# ---------------------------------------------------------------------------
# O. Post-Review Correction Pass — MINOR-3: Windows member-name hardening
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "name",
    [
        "C:/evil.xml",
        "C:evil.xml",
        "folder/a:b.xml",
        "CON",
        "con.xml",
        "PRN",
        "AUX",
        "NUL",
        "COM1",
        "COM9.xml",
        "LPT1",
        "LPT9.txt",
        "AUX/data.xml",
        "folder/COM1.xml",
        "folder/name.",
        "folder/name ",
    ],
)
def test_is_safe_member_name_rejects_windows_unsafe(name: str) -> None:
    assert _is_safe_member_name(name) is False


@pytest.mark.parametrize(
    "name",
    [
        "xl/workbook.xml",
        "xl/worksheets/sheet1.xml",
        "xl/extra/",
        "docProps/core.xml",
        "xl/media/image1.png",
        "component.xlsx",
    ],
)
def test_is_safe_member_name_accepts_normal_names_after_hardening(name: str) -> None:
    assert _is_safe_member_name(name) is True


def test_colon_member_name_rejected_end_to_end(tmp_path: Path, base_parts) -> None:
    p = tmp_path / "coloninzip.xlsx"
    _write_xlsx(p, base_parts, [("xl/extra/a:b.xml", b"<x/>")])
    with pytest.raises(PackageScrubError) as exc_info:
        preflight_xlsx_package(p)
    assert exc_info.value.reason is PackageScrubReason.UNSUPPORTED_PACKAGE_CONTENT


def test_windows_reserved_device_name_rejected_end_to_end(tmp_path: Path, base_parts) -> None:
    p = tmp_path / "devicename.xlsx"
    _write_xlsx(p, base_parts, [("xl/extra/CON.xml", b"<x/>")])
    with pytest.raises(PackageScrubError) as exc_info:
        preflight_xlsx_package(p)
    assert exc_info.value.reason is PackageScrubReason.UNSUPPORTED_PACKAGE_CONTENT
