"""
Stage 10C.2.3 — Object-model Scrub.

MUTATION / SCRUB — этот модуль НЕ выполняет resource/package preflight
(Stage10C.2.1), package policy/inventory (Stage10C.2.2), post-save strict
validation (будущий Stage10C.2.4) или public orchestration/authorization
(будущий Stage10C.2.5). Он ожидает вызов ПОСЛЕ успешных 10C.2.1+10C.2.2 —
но сам по себе НЕ является external-safe API: restored-marker rejection
(Stage9C) — ответственность вызывающего слоя (Stage10C.2.5), выполняемая
ДО вызова этого модуля (см. docstring scrub_workbook_object_model).

======================================================================
Frozen contract (Stage10C.2.3 Final Contract Freeze Pass)
======================================================================

Owner Decisions (не переоткрываются):

- OD-10C2.3-1 = D: формулы НЕ анализируются/НЕ модифицируются/НЕ
  заменяются cached-значениями (`data_only=False`). Formula Safety Gate
  (проверка formula-контента на утечку исходных confidential строк,
  например `=HYPERLINK("secret-url",...)`) — ОБЯЗАТЕЛЬНЫЙ MUST-BLOCK
  layer будущего Stage10C Final Security Closure, НЕ реализуется здесь.
  До его прохождения workbook с формулами НЕ считается доказанно
  безопасным для внешнего ИИ.
- OD-10C2.3-2 = A: DataAnonymizer.JobId удаляется вместе со ВСЕМИ custom
  properties, без исключений.
- OD-10C2.3-3 = A: hidden/veryHidden worksheets и hidden rows/columns
  сохраняются без изменений (presentation state, не confidentiality
  surface при условии, что Stage8 anonymization обрабатывает eligible
  cells независимо от hidden state — эта гарантия вне scope 10C.2.3).
- OD-10C2.3-4 = A (BLOCKER-1 Correction Pass): узкое, осознанно
  одобренное исключение на использование private-атрибутов
  `worksheet._print_rows`/`worksheet._print_cols` для очистки Print
  Titles — единственный способ реально их очистить в openpyxl 3.1.5
  (публичные сеттеры `print_title_rows`/`print_title_cols` молча
  игнорируют `None`). Разрешение НЕ распространяется на другие
  private-атрибуты сверх уже принятого `_cells`.
- OD-10C2.3-5 = D (BLOCKER-2 Correction Pass): style/presentation
  free-text surfaces (number formats, font names, named cell styles,
  differential styles, table styles, codeName) НЕ остаются accepted
  confidentiality risk — детерминированно удаляются/нормализуются
  здесь же, не откладываются на будущий Safety Gate (в отличие от
  formulas, OD-10C2.3-1).
- OD-10C2.3-6 = B (BLOCKER-2 Correction Pass): `Font.name` — НЕ
  unrestricted free-text surface. Разрешены только имена из frozen
  allow-list (`_ALLOWED_FONT_NAMES`); любое иное имя нормализуется к
  `_SAFE_FALLBACK_FONT_NAME`.
- OD-10C2.3-7 = RESET (BLOCKER-3 Correction Pass): `xl/theme/theme1.xml`
  НЕ считается бизнес-данными, требующими сохранения. openpyxl хранит
  его как сырые байты (`workbook.loaded_theme`) и сериализует вербатим
  без разбора — исходная тема НЕ парсится/НЕ пытается быть частично
  очищена (theme name/clrScheme name/font-scheme typeface — arbitrary
  free text). Используется безусловный сброс `loaded_theme = None` —
  визуальная потеря custom theme сознательно принимается ради
  confidentiality.
- OD-10C2.3-8 = B (BLOCKER-4 Correction Pass): Rich Text
  `InlineFont.rFont` подчиняется ТОЙ ЖЕ политике, что `Font.name`
  (OD-10C2.3-6) — та же exact allow-list/fallback, применённая
  отдельно, поскольку `InlineFont.rFont` архитектурно НЕ входит в
  `workbook._fonts` (registry-level sanitizer его не видит).

Последовательность мутации (детерминированный порядок, без глобального
состояния): comments/hyperlinks/rich-text run fonts -> tables -> data
validations -> conditional formatting -> defined names (workbook +
per-sheet) -> autoFilter -> print area/titles -> headers/footers ->
worksheet protection -> worksheet codeName -> workbook defined names ->
workbook protection -> workbook codeName -> custom properties -> core
properties -> style-registry sanitization (fonts -> number formats ->
named styles -> differential styles -> table styles) -> theme reset ->
save в НОВЫЙ temp-файл.

Style-registry sanitization (BLOCKER-2 Correction Pass, OD-10C2.3-5/-6)
выполняется на уровне workbook-реестров (`_fonts`, `_number_formats`,
`_named_styles`, `_differential_styles`, `_table_styles`), а НЕ через
переприсвоение `cell.font`/`cell.number_format` — эмпирически доказано
(Architecture/Freeze Pass), что переприсвоение на уровне ячейки
оставляет orphan-записи со старым confidential-значением в этих
append-only реестрах, которые всё равно сериализуются в `styles.xml`
независимо от того, ссылается ли на них ещё хоть одна ячейка.

Empирически подтверждено (Architecture Pass + Final Contract Freeze
Pass, синтетические workbook вне repository) и НЕ требует отдельного
object-model действия здесь: calcChain.xml, sharedStrings.xml,
external links, x14/extLst-based data validation и conditional
formatting extensions — openpyxl 3.1.5 безусловно отбрасывает их при
load_workbook()+save() независимо от параметров (в т.ч. независимо от
keep_links). Это verified-by-construction инвариант — будущий
Stage10C.2.4 обязан его независимо ассертировать на package-уровне, а
не полагаться молча на наблюдаемое поведение.

======================================================================
OD-7
======================================================================

Ни одно исключение не содержит: путь source-файла, имя листа,
координату ячейки, значение ячейки, текст формулы, URL, текст
комментария, имя defined name, значение metadata, raw XML. Единственный
используемый reason — PackageScrubReason.SCRUB_FAILED (уже
зарезервирован именно для этой стадии Stage10C.2 Final Freeze) с
фиксированным sanitized-сообщением.

======================================================================
Не путать со смежными слоями
======================================================================

Этот модуль НЕ строит SafetyReport/ScrubResult, НЕ выставляет флаги
is_safe/can_upload/authorized, НЕ регистрирует artifact, НЕ трогает
Workspace. Единственный публичный результат — Path к новому temporary
.xlsx вне workspace; исходный source_path никогда не изменяется.
"""

from __future__ import annotations

import os
import tempfile
import unicodedata
from datetime import datetime
from pathlib import Path
from typing import Union

import openpyxl
from openpyxl.cell.cell import MergedCell
from openpyxl.cell.rich_text import CellRichText, TextBlock
from openpyxl.formatting.formatting import ConditionalFormattingList
from openpyxl.packaging.custom import CustomPropertyList
from openpyxl.styles.differential import DifferentialStyleList
from openpyxl.styles.numbers import is_datetime
from openpyxl.workbook.protection import WorkbookProtection
from openpyxl.worksheet.protection import SheetProtection

from app.scrub.errors import PackageScrubError, PackageScrubReason
from app.scrub.preflight import _SUPPORTED_EXTENSION

_PathLike = Union[str, Path]

_HEADER_FOOTER_ATTRIBUTES = (
    "oddHeader",
    "oddFooter",
    "evenHeader",
    "evenFooter",
    "firstHeader",
    "firstFooter",
)

# 13 нетаймстамповых core-properties полей (Final Contract Freeze Pass §20)
# — полный список реально сериализуемых пользовательских/идентифицирующих
# строковых полей DocumentProperties в openpyxl 3.1.5 (подтверждено
# интроспекцией openpyxl.packaging.core.DocumentProperties.__init__).
_CORE_STRING_FIELDS = (
    "creator",
    "title",
    "subject",
    "description",
    "keywords",
    "category",
    "contentStatus",
    "identifier",
    "lastModifiedBy",
    "lastPrinted",
    "revision",
    "version",
    "language",
)

# Фиксированная дата создания (frozen contract) — modified намеренно НЕ
# устанавливается: openpyxl безусловно перезаписывает его текущим
# временем сохранения при save (не поддаётся управлению, уже принято
# ранее в проекте как non-confidential operational timestamp).
_FIXED_CREATED_DATE = datetime(2000, 1, 1)

# Font allow-list (BLOCKER-2 Correction Pass, OD-10C2.3-6, Final Contract
# Freeze Pass §4-6) — exact match после unicodedata.normalize("NFC")+strip,
# НЕ case-insensitive/substring/fuzzy. Любое иное имя -> fallback.
_ALLOWED_FONT_NAMES = frozenset(
    {
        "Calibri",
        "Calibri Light",
        "Arial",
        "Times New Roman",
        "Courier New",
        "Segoe UI",
        "Tahoma",
        "Verdana",
        "Aptos",
    }
)
_SAFE_FALLBACK_FONT_NAME = "Calibri"

# Exact custom number-format allow-list (BLOCKER-2 Correction Pass,
# Final Contract Freeze Pass §9-10) — ровно эти 7 строк, exact match, без
# regex/parser. Built-in форматы сюда не попадают вообще: openpyxl
# никогда не добавляет built-in format code в workbook._number_formats
# (подтверждено эмпирически) -- они адресуются fixed numeric id напрямую.
_ALLOWED_CUSTOM_NUMBER_FORMATS = frozenset(
    {
        "yyyy-mm-dd",
        "dd.mm.yyyy",
        "dd.mm.yy",
        "dd.mm.yyyy hh:mm",
        "dd.mm.yyyy hh:mm:ss",
        "hh:mm",
        "hh:mm:ss",
    }
)
_NUMBER_FORMAT_FALLBACK_DATE = "yyyy-mm-dd"
_NUMBER_FORMAT_FALLBACK_TIME = "hh:mm:ss"
_NUMBER_FORMAT_FALLBACK_DATETIME = "dd.mm.yyyy hh:mm:ss"
_NUMBER_FORMAT_FALLBACK_PERCENT = "0.00%"
_NUMBER_FORMAT_FALLBACK_OTHER = "0.00"

# Deterministic neutral rename для custom (не builtinId) NamedStyle
# (Final Contract Freeze Pass §10-11).
_DA_STYLE_NAME_TEMPLATE = "DAStyle{index:04d}"

# BLOCKER Correction Pass (NamedStyle builtinId bypass): точный,
# независимо сверенный с реальным openpyxl.styles.builtins.styles
# (openpyxl==3.1.5) список ВСЕХ 49 доверенных пар (name, builtinId).
# Доверие к built-in статусу стиля требует EXACT PAIR MATCH -- ни
# `builtinId is not None` сам по себе, ни `name` само по себе не
# являются достаточным доказательством: произвольный (name, builtinId)
# со SPOOFED non-None builtinId (не входящим в этот список, либо не
# соответствующим этому конкретному name) ранее позволял confidential
# имени пережить scrub под видом "built-in" стиля.
_TRUSTED_BUILTIN_STYLE_PAIRS = frozenset(
    {
        ("Normal", 0),
        ("Comma", 3),
        ("Currency", 4),
        ("Percent", 5),
        ("Comma [0]", 6),
        ("Currency [0]", 7),
        ("Hyperlink", 8),
        ("Followed Hyperlink", 9),
        ("Note", 10),
        ("Warning Text", 11),
        ("Title", 15),
        ("Headline 1", 16),
        ("Headline 2", 17),
        ("Headline 3", 18),
        ("Headline 4", 19),
        ("Input", 20),
        ("Output", 21),
        ("Calculation", 22),
        ("Check Cell", 23),
        ("Linked Cell", 24),
        ("Total", 25),
        ("Good", 26),
        ("Bad", 27),
        ("Neutral", 28),
        ("Accent1", 29),
        ("20 % - Accent1", 30),
        ("40 % - Accent1", 31),
        ("60 % - Accent1", 32),
        ("Accent2", 33),
        ("20 % - Accent2", 34),
        ("40 % - Accent2", 35),
        ("60 % - Accent2", 36),
        ("Accent3", 37),
        ("20 % - Accent3", 38),
        ("40 % - Accent3", 39),
        ("60 % - Accent3", 40),
        ("Accent4", 41),
        ("20 % - Accent4", 42),
        ("40 % - Accent4", 43),
        ("60 % - Accent4", 44),
        ("Accent5", 45),
        ("20 % - Accent5", 46),
        ("40 % - Accent5", 47),
        ("60 % - Accent5", 48),
        ("Accent6", 49),
        ("20 % - Accent6", 50),
        ("40 % - Accent6", 51),
        ("60 % - Accent6", 52),
        ("Explanatory Text", 53),
    }
)

# Fixed safe значения для TableStyleList.defaultTableStyle/
# defaultPivotStyle (Final Contract Freeze Pass §15) -- эмпирически
# подтверждено, что это НЕ constrained enum, а обычный String(), поэтому
# исходное значение НЕ считается safe по умолчанию и нормализуется
# безусловно к тем же значениям, что openpyxl использует как
# конструкторные defaults.
_SAFE_DEFAULT_TABLE_STYLE = "TableStyleMedium9"
_SAFE_DEFAULT_PIVOT_STYLE = "PivotStyleLight16"


def _existing_cells(worksheet) -> list:
    """
    Только реально существующие ячейки (не dense iter_rows —
    материализация sparse worksheet до миллиардов пустых Cell), по
    прецеденту app.safety.external_ai. MergedCell исключены явно: это не
    полноценная mutable-ячейка (запись comment/hyperlink в неё
    бессмысленна/небезопасна — реальные значения принадлежат anchor-
    ячейке). Несовместимый `_cells` — fail-closed SCRUB_FAILED, не
    молчаливый fallback.
    """
    cells = getattr(worksheet, "_cells", None)
    if not isinstance(cells, dict):
        raise PackageScrubError(PackageScrubReason.SCRUB_FAILED)
    return [cell for cell in cells.values() if not isinstance(cell, MergedCell)]


def _scrub_worksheet(worksheet) -> None:
    for cell in _existing_cells(worksheet):
        if cell.comment is not None:
            cell.comment = None
        if cell.hyperlink is not None:
            cell.hyperlink = None
        if isinstance(cell.value, CellRichText):
            _sanitize_rich_text_fonts(cell.value)

    for name in list(worksheet.tables.keys()):
        del worksheet.tables[name]

    worksheet.data_validations.dataValidation = []
    worksheet.conditional_formatting = ConditionalFormattingList()

    worksheet.defined_names.clear()

    # AutoFilter (Final Contract Freeze Pass §14/§16): ref=None не
    # обязательно очищает stale in-memory filterColumn/sortState, но
    # эмпирически гарантирует, что <autoFilter> и авто-generated
    # _xlnm._FilterDatabase не сериализуются при save — security boundary
    # здесь именно package output, а не in-memory состояние.
    worksheet.auto_filter.ref = None

    worksheet.print_area = None

    # Print Titles (BLOCKER-1 correction, OD-10C2.3-4): публичные сеттеры
    # print_title_rows/print_title_cols в openpyxl 3.1.5 молча
    # игнорируют None (`if value is not None: ...`) и НЕ очищают
    # internal state — в отличие от print_area. Узкое, осознанно
    # одобренное исключение (наравне с уже принятым _cells для
    # existing-cell traversal): прямая запись в _print_rows/_print_cols —
    # единственный способ добиться реальной очистки. Stage10C.2.4
    # обязана независимо проверить отсутствие _xlnm.Print_Titles на
    # package-уровне — эта запись не единственная точка доверия.
    worksheet._print_rows = None
    worksheet._print_cols = None

    for attribute in _HEADER_FOOTER_ATTRIBUTES:
        header_footer = getattr(worksheet, attribute)
        header_footer.left.text = None
        header_footer.center.text = None
        header_footer.right.text = None

    worksheet.protection = SheetProtection()

    # Worksheet codeName (BLOCKER-2 Correction Pass, Final Contract
    # Freeze Pass §12): публичный атрибут, не требует VBA (подтверждено
    # для обычного .xlsx), не несёт визуального/аналитического значения
    # -- безусловно сбрасывается.
    worksheet.sheet_properties.codeName = None


def _normalize_font_name(name):
    if name is None:
        return None
    normalized = unicodedata.normalize("NFC", name).strip()
    if normalized in _ALLOWED_FONT_NAMES:
        return normalized
    return _SAFE_FALLBACK_FONT_NAME


def _sanitize_rich_text_fonts(rich_text: CellRichText) -> None:
    """
    BLOCKER-4 Correction Pass (OD-10C2.3-8): `InlineFont.rFont` живёт
    ВНУТРИ run'а rich-text значения ячейки (`CellRichText`/`TextBlock`),
    архитектурно отдельно от `workbook._fonts` -- registry-level
    `_sanitize_font_registry` его не видит и не может увидеть. Та же
    normalize-политика (OD-10C2.3-6), применённая к каждому run'у.
    `CellRichText` -- обычный `list` (не IndexedList/hash-backed
    реестр), `TextBlock`/`InlineFont` -- обычные mutable-объекты; та же
    in-place мутация безопасна (эмпирически подтверждено, Independent
    Re-review §13). Плоские строковые фрагменты (не TextBlock) и runs
    без явного font пропускаются без изменений.
    """
    for part in rich_text:
        if isinstance(part, TextBlock) and part.font is not None:
            part.font.rFont = _normalize_font_name(part.font.rFont)


def _classify_number_format_fallback(fmt: str) -> str:
    # is_datetime/is_date_format используются ТОЛЬКО для выбора fallback
    # (Final Contract Freeze Pass §13), НЕ как security-проверка
    # исходного формата -- к этому моменту формат уже признан unsafe
    # (не built-in и не в exact allow-list).
    kind = is_datetime(fmt)
    if kind == "datetime":
        return _NUMBER_FORMAT_FALLBACK_DATETIME
    if kind == "date":
        return _NUMBER_FORMAT_FALLBACK_DATE
    if kind == "time":
        return _NUMBER_FORMAT_FALLBACK_TIME
    if "%" in fmt:
        return _NUMBER_FORMAT_FALLBACK_PERCENT
    return _NUMBER_FORMAT_FALLBACK_OTHER


def _sanitize_font_registry(workbook) -> None:
    """
    `workbook._fonts` (private): registry-level in-place мутация --
    единственный надёжный способ (Architecture/Freeze Pass, эмпирически
    доказано). Переприсвоение `cell.font = ...` не удаляет старую Font-
    запись из этого append-only реестра -- она остаётся orphan и всё
    равно сериализуется в styles.xml. Мутируется только `.name` на
    существующей позиции -- без удаления/вставки/reorder, поэтому
    fontId-индексация в cellXfs остаётся валидной. NamedStyle.font --
    та же самая object identity, что и соответствующая запись в этом
    реестре (подтверждено эмпирически), поэтому отдельная обработка
    вложенных NamedStyle-шрифтов не требуется.
    """
    for font in workbook._fonts:
        font.name = _normalize_font_name(font.name)


def _sanitize_number_format_registry(workbook) -> None:
    """
    `workbook._number_formats` (private): та же registry-level in-place
    логика, что и для fonts -- по той же эмпирически доказанной причине
    (orphan-записи). Мутация по индексу, без удаления/вставки/reorder --
    numFmtId-индексация остаётся валидной.
    """
    for index, fmt in enumerate(workbook._number_formats):
        if fmt in _ALLOWED_CUSTOM_NUMBER_FORMATS:
            continue
        workbook._number_formats[index] = _classify_number_format_fallback(fmt)


def _sanitize_named_styles(workbook) -> None:
    """
    `workbook._named_styles` (private): нет public API для итерации+
    переименования зарегистрированных NamedStyle-объектов.

    BLOCKER Correction Pass (NamedStyle builtinId bypass): доверие к
    built-in статусу стиля требует EXACT PAIR MATCH (`name`, `builtinId`)
    против `_TRUSTED_BUILTIN_STYLE_PAIRS` -- эмпирически доказано, что
    проверка ТОЛЬКО `builtinId is not None` (прежняя логика) позволяла
    произвольному confidential `name` пережить scrub, если ему был
    присвоен ЛЮБОЙ non-None `builtinId` (spoofed/unknown/reserved
    значение, либо known `builtinId` с несоответствующим `name`).

    Любой NamedStyle БЕЗ exact pair match считается untrusted и
    получает: (а) детерминированное нейтральное имя (исходное имя
    НИКОГДА не используется как основа/часть нового); (б)
    `builtinId = None` -- иначе результат остался бы семантически
    "built-in" со spoofed маркером даже после переименования. Оба шага
    обязательны и неразделимы.
    """
    custom_index = 0
    for named_style in workbook._named_styles:
        if (named_style.name, named_style.builtinId) in _TRUSTED_BUILTIN_STYLE_PAIRS:
            continue
        custom_index += 1
        named_style.name = _DA_STYLE_NAME_TEMPLATE.format(index=custom_index)
        named_style.builtinId = None


def _clear_differential_styles(workbook) -> None:
    """
    `workbook._differential_styles` (private): нет public API очистки.
    После удаления CF/Tables (per-worksheet, выше) весь контейнер --
    гарантированно orphan (Final Contract Freeze Pass §13,17-18) --
    заменяется целиком, той же по духу практикой, что уже применена к
    ConditionalFormattingList/CustomPropertyList.
    """
    workbook._differential_styles = DifferentialStyleList()


def _sanitize_table_styles(workbook) -> None:
    """
    `workbook._table_styles` (private): нет public API для полной
    очистки custom table-style definitions или нормализации default-
    имён. Custom tableStyle-записи удаляются целиком (orphan после
    удаления Tables). defaultTableStyle/defaultPivotStyle -- ОБЫЧНЫЙ
    String(), не constrained enum (эмпирически подтверждено, Final
    Contract Freeze Pass §15) -- нормализуются безусловно к fixed safe
    значениям, независимо от исходного содержимого.
    """
    workbook._table_styles.tableStyle = ()
    workbook._table_styles.defaultTableStyle = _SAFE_DEFAULT_TABLE_STYLE
    workbook._table_styles.defaultPivotStyle = _SAFE_DEFAULT_PIVOT_STYLE


def _scrub_core_properties(properties) -> None:
    for field_name in _CORE_STRING_FIELDS:
        setattr(properties, field_name, None)
    properties.created = _FIXED_CREATED_DATE


def _scrub_workbook(workbook) -> None:
    for worksheet in workbook.worksheets:
        _scrub_worksheet(worksheet)

    workbook.defined_names.clear()
    workbook.security = WorkbookProtection()

    # Workbook codeName (BLOCKER-2 Correction Pass, Final Contract
    # Freeze Pass §12): та же логика, что и worksheet-level codeName --
    # публичный атрибут, не требует VBA, безусловно сбрасывается.
    workbook.code_name = None

    # Custom properties (OD-10C2.3-2): удаляются ВСЕ, без исключения для
    # DataAnonymizer.JobId. Restored-marker (DataAnonymizer.AnalyticallyRestored/
    # RestoredFromJobId) НЕ является исключением в обратную сторону тоже —
    # его удаление здесь НЕ делает restored workbook безопасным; отклонение
    # restored workbook обязано произойти РАНЬШЕ, на уровне вызывающего
    # оркестрирующего слоя (Stage10C.2.5), а не полагаться на этот scrub.
    workbook.custom_doc_props = CustomPropertyList()

    _scrub_core_properties(workbook.properties)

    # Style-registry sanitization (BLOCKER-2 Correction Pass,
    # OD-10C2.3-5/-6) — выполняется ПОСЛЕ per-worksheet CF/Tables
    # removal (выше), чтобы DXF/TableStyle orphan-очистка была
    # гарантированно корректной (см. docstring модуля).
    _sanitize_font_registry(workbook)
    _sanitize_number_format_registry(workbook)
    _sanitize_named_styles(workbook)
    _clear_differential_styles(workbook)
    _sanitize_table_styles(workbook)

    # Theme (BLOCKER-3 Correction Pass, OD-10C2.3-7 = RESET): openpyxl
    # хранит xl/theme/theme1.xml как СЫРЫЕ БАЙТЫ (`loaded_theme`) и
    # сериализует их вербатим -- theme name/clrScheme name/font-scheme
    # typeface (десятки полей на разные script) являются произвольным
    # user-controlled free text, который НЕ парсится и НЕ пытается быть
    # частично очищен (сознательное решение владельца: визуальная
    # потеря custom theme принимается ради confidentiality). Публичный
    # атрибут -- новый private API не требуется.
    workbook.loaded_theme = None


def _load_workbook_for_scrub(candidate: Path):
    if not candidate.exists() or not candidate.is_file():
        raise PackageScrubError(PackageScrubReason.SCRUB_FAILED)

    load_failed = False
    workbook = None
    try:
        workbook = openpyxl.load_workbook(
            candidate,
            read_only=False,
            data_only=False,
            keep_vba=False,
            keep_links=True,
            rich_text=True,
        )
    except Exception:
        load_failed = True
    if load_failed:
        raise PackageScrubError(PackageScrubReason.SCRUB_FAILED)
    return workbook


def _safe_close_workbook(workbook) -> None:
    try:
        workbook.close()
    except Exception:
        pass


def _safe_unlink(path: Path) -> None:
    try:
        path.unlink(missing_ok=True)
    except Exception:
        pass


def _save_scrubbed_workbook(workbook) -> Path:
    """
    Сохраняет workbook в НОВЫЙ временный .xlsx вне workspace (обычная OS
    temp-область, случайное имя — не производное от source-имени).
    Закрытие file descriptor от mkstemp происходит ДО save (Windows-safe
    паттерн, прецедент app.coverage.worksheet_policy) — повторная
    попытка os.close при сбое намеренно НЕ предпринимается (POSIX не
    специфицирует состояние дескриптора после неудачного close, повторное
    закрытие того же номера рискует закрыть чужой, повторно выделенный
    дескриптор). При любом сбое temp удаляется best-effort.

    MAJOR-1 correction: сбой самого `tempfile.mkstemp()` (resource
    exhaustion — диск переполнен, нет прав на temp-директорию) ранее
    пробрасывался как сырое исключение (нарушение документированного
    `:raises:`-контракта); теперь оборачивается тем же flag-then-raise
    паттерном, что и `os.close`/`workbook.save` ниже.
    """
    mkstemp_failed = False
    fd = None
    tmp_path = None
    try:
        fd, tmp_name = tempfile.mkstemp(suffix=_SUPPORTED_EXTENSION)
        tmp_path = Path(tmp_name)
    except Exception:
        mkstemp_failed = True
    if mkstemp_failed:
        raise PackageScrubError(PackageScrubReason.SCRUB_FAILED)

    fd_close_failed = False
    try:
        os.close(fd)
    except Exception:
        fd_close_failed = True
    if fd_close_failed:
        _safe_unlink(tmp_path)
        raise PackageScrubError(PackageScrubReason.SCRUB_FAILED)

    save_failed = False
    try:
        workbook.save(tmp_path)
    except Exception:
        save_failed = True
    if save_failed:
        _safe_unlink(tmp_path)
        raise PackageScrubError(PackageScrubReason.SCRUB_FAILED)

    return tmp_path


def scrub_workbook_object_model(source_path: _PathLike) -> Path:
    """
    Object-model scrub (Stage 10C.2.3) — единственная точка входа этого
    модуля. Загружает source_path, выполняет frozen-последовательность
    мутаций (см. docstring модуля), сохраняет результат в НОВЫЙ
    temporary .xlsx вне workspace и возвращает его Path. source_path
    НИКОГДА не изменяется и не перезаписывается — ни при успехе, ни при
    ошибке.

    Это INTERNAL API. Он НЕ является safety inspection, package
    validation, external authorization или artifact registration.
    Возвращаемый Path — internal temporary путь, не доказательство
    безопасности. В частности: прямой вызов этой функции на workbook с
    маркером DataAnonymizer.AnalyticallyRestored/RestoredFromJobId
    УДАЛИТ этот маркер вместе со всеми custom properties — восстановленный
    (restored) workbook ОБЯЗАН быть отклонён ДО вызова этой функции,
    на уровне вызывающего оркестрирующего слоя (Stage10C.2.5). Формулы
    (включая потенциально содержащие confidential-строки, например
    =HYPERLINK("...")) сохраняются без анализа — см. OD-10C2.3-1;
    итоговый temp-файл НЕ считается доказанно безопасным для внешнего ИИ
    до прохождения будущего Formula Safety Gate (Stage10C Final Security
    Closure).

    :raises TypeError: source_path не str/Path.
    :raises PackageScrubError: PackageScrubReason.SCRUB_FAILED —
        source_path не существует/не файл/не .xlsx, workbook не удалось
        безопасно загрузить, мутация или сохранение завершились ошибкой,
        сбой очистки временного файла. Сообщение всегда фиксированное и
        безопасное (без пути/имени листа/значения/формулы/URL/текста
        комментария/имени defined name/значения metadata/raw XML).
    """
    if not isinstance(source_path, (str, Path)):
        raise TypeError(f"source_path должен быть str или Path, получено: {type(source_path)!r}")

    candidate = Path(source_path)
    if candidate.suffix.lower() != _SUPPORTED_EXTENSION:
        raise PackageScrubError(PackageScrubReason.SCRUB_FAILED)

    workbook = _load_workbook_for_scrub(candidate)
    try:
        mutation_failed = False
        try:
            _scrub_workbook(workbook)
        except Exception:
            mutation_failed = True
        if mutation_failed:
            raise PackageScrubError(PackageScrubReason.SCRUB_FAILED)
        return _save_scrubbed_workbook(workbook)
    finally:
        _safe_close_workbook(workbook)
