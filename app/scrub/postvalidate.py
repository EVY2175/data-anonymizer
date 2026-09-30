"""
Stage 10C.2.4 — Strict Post-Save Package Validation.

Выполняется ПОСЛЕ app.scrub.mutate.scrub_workbook_object_model — принимает
путь к уже сохранённому scrubbed-кандидату (реальный файл на диске,
результат openpyxl Workbook.save()) и проверяет, что его OPC/OOXML
package СТРОГО соответствует замороженной, закрытой грамматике: ровно
ожидаемый набор физических частей, ровно ожидаемые Content-Type/
relationship-декларации, ровно ожидаемые XML-элементы/атрибуты в каждой
control-части и в каждом worksheet, ровно ожидаемые byte-for-byte
theme1.xml/app.xml (frozen SHA-256), ровно ожидаемая грамматика
docProps/core.xml.

Это НЕ второй preflight и НЕ повторная package policy inspection —
задача этого модуля УЖЕ предполагает, что вход прошёл Stage10C.2.1
(app.scrub.preflight) и Stage10C.2.2 (app.scrub.inventory) на ВХОДНОМ
файле; этот модуль применяется к ВЫХОДНОМУ файлу scrub-а и умышленно
использует СВОЮ, более узкую (не переиспользованную из
app.scrub.inventory) карту допустимых Content-Type/relationship ролей —
выходная грамматика Stage10C.2.4 строго уже входной грамматики
Stage10C.2.2 (например, sharedStrings/calcChain/comments/table/custom
properties, разрешённые на входе, на выходе запрещены безусловно).

======================================================================
Единственная точка входа
======================================================================

validate_scrubbed_workbook_package(path) -> PackageValidationResult

======================================================================
Переиспользование (не дублирование чисел/примитивов)
======================================================================

Из app.scrub.preflight переиспользуются БЕЗ ИЗМЕНЕНИЙ: все шесть frozen
resource-лимитов (MAX_XLSX_SIZE_BYTES, MAX_ZIP_ENTRIES,
MAX_TOTAL_UNCOMPRESSED_BYTES, MAX_COMPRESSION_RATIO, MAX_WORKSHEETS,
MAX_NONEMPTY_CELLS), а также низкоуровневые примитивы ZIP/relationship-
безопасности (_cheap_zip_preflight, _read_workbook_sheet_rids,
_resolve_relationship_target, _is_safe_member_name,
_canonical_member_name, _open_member_stream, _safe_close_suppressing,
_close_or_fail, _is_nonempty_cell_element, _WORKBOOK_PART,
_WORKBOOK_RELS_PART, _SUPPORTED_EXTENSION). Из app.scrub.inventory
переиспользуется ТОЛЬКО generic-примитив _stream_relationships (роль-
агностичный потоковый .rels reader) — role/content-type карта у этого
модуля СВОЯ, не импортируется. Из app.scrub.mutate переиспользуются БЕЗ
ИЗМЕНЕНИЙ (импорт, без дублирования) frozen allow-листы, УЖЕ
являющиеся единственным источником истины для соответствующих решений
scrub-а: _TRUSTED_BUILTIN_STYLE_PAIRS, _ALLOWED_FONT_NAMES,
_ALLOWED_CUSTOM_NUMBER_FORMATS, _DA_STYLE_NAME_TEMPLATE,
_SAFE_DEFAULT_TABLE_STYLE, _SAFE_DEFAULT_PIVOT_STYLE. Циклического
импорта нет: app.scrub.mutate и app.scrub.inventory не импортируют
app.scrub.postvalidate.

======================================================================
Single ZipFile handle (НЕ security snapshot guarantee)
======================================================================

Весь пакет открывается ОДИН раз через _cheap_zip_preflight — одна и та
же ZipFile-дескриптор переиспользуется для всех проверок ниже. Это
сделано для производительности и внутренней согласованности проверки
(не открывать файл по пути повторно несколько раз), а НЕ как
доказанная TOCTOU-защита: единственный открытый дескриптор
zipfile.ZipFile на Windows/CPython 3.13 эмпирически НЕ блокирует
конкурентную запись поверх того же файла путём или другим процессом
(см. Stage10C.2.4 Contract Freeze Pass, эксперимент на уровне ОС) — при
конкурентной подмене чтение из уже открытого дескриптора либо получает
старые (уже провалидированные) байты, либо падает с BadZipFile/CRC-
ошибкой (обнаруживаемый сбой, не тихая подмена), но формальной гарантии
неизменности пакета между двумя вызовами этого модуля НЕТ — это
известный, не закрытый здесь TOCTOU-долг всего Stage10C (см.
app.scrub.inventory, тот же NOTE).

======================================================================
Модель ошибок (OD-7)
======================================================================

Все НОВЫЕ (специфичные для этого модуля) семантические/grammar-
нарушения поднимают PackageScrubError(PackageScrubReason.
POST_VALIDATION_FAILED) — единое фиксированное сообщение, никогда не
включающее часть пакета, XML-содержимое, имя листа, значение ячейки,
formula-текст, provenance/alias/token. Численные resource-нарушения
(все шесть лимитов, перепроверяемые на OUTPUT так же строго, как на
INPUT) поднимают PackageScrubReason.RESOURCE_LIMIT_EXCEEDED — тем же
переиспользованным кодом preflight, без дублирования порогов. Ни одно
исключение нижнего уровня (xml.etree.ElementTree, zipfile, os) не
пробрасывается как есть: flag-then-raise-outside-except, та же
дисциплина, что и в app.scrub.preflight/app.scrub.inventory.

======================================================================
XML safety
======================================================================

Только stdlib xml.etree.ElementTree, без сети. Все части пакета —
включая control-части (workbook.xml, styles.xml, [Content_Types].xml,
*.rels, docProps/core.xml) — читаются ТОЛЬКО потоково (ET.iterparse с
elem.clear() на каждом закрывающем событии), НЕ ET.parse: Stage10C.2.1
Independent Security Review (MAJOR-1) эмпирически показал, что
non-streaming ET.parse на компактных control-частях допускает
~150x memory amplification в рамках уже замороженных лимитов — тот же
риск применим к любой части пакета, не только к workbook.xml/
workbook.xml.rels, поэтому здесь эта дисциплина применяется
универсально, ко ВСЕМ XML-частям без исключения, включая worksheet.xml
(явно потоково, ET.iterparse) и styles.xml. mc:AlternateContent,
mc:Ignorable, extLst и любой неизвестный namespace отклоняются
универсально самим механизмом grammar-валидации (allow-list дочерних
элементов на каждом уровне; тег вне allow-list -> POST_VALIDATION_FAILED),
без специального кода для каждого конкретного запрещённого имени.

======================================================================
Явно НЕ входит в эту фазу (см. Stage10C.2.4 Architecture/Freeze Pass)
======================================================================

- Семантический скан raw-значений ячеек/formula-текста/имён листов на
  предмет confidential-контента (это Stage8 anonymization, уже
  выполненный ДО scrub-а, и отдельный будущий Formula Safety Gate) —
  здесь проверяется только СТРУКТУРА (какие теги/атрибуты допустимы),
  никогда содержимое текстовых узлов.
- Полное закрытие TOCTOU на уровне Stage10C в целом (см. выше).
- Восстановление/защита от отредактированного вручную XLSX (это
  RESTORED_MARKER_PRESENT/KNOWN_RAW_WORKSHEET_TITLE — другие
  подстадии).

======================================================================
NOTE (новые находки этого implementation pass, не зафиксированные ранее)
======================================================================

Часть точных attribute-value-доменов "generic layout" элементов
(sheetView/pane/selection/col/row/calcPr/bookView/font-registry/border/
fill/alignment/protection и т.п. — все НЕ упомянутые дословно в
Stage10C.2.4 Contract Freeze тексте, доступном в этом проходе) была
НЕЗАВИСИМО ВЫВЕДЕНА в рамках этой Implementation Pass напрямую из
установленного openpyxl==3.1.5 (inspect.getsource / __attrs__ /
__elements__ / descriptor.values) и из реального scrubbed-output ряда
проб — той же методологией, что использовалась во всём проекте для
верификации поведения openpyxl. В ходе этого вывода обнаружен НОВЫЙ,
ранее не зафиксированный потенциальный канал: WorkbookProperties.codeName,
WorksheetProperties.codeName и WorksheetProperties.syncRef — все три
объявлены в openpyxl как ничем не ограниченный String()-дескриптор (не
enum, не regex), то есть потенциальный произвольный free-text канал,
если когда-либо будут установлены во входном workbook и не будут
стёрты Stage10C.2.3. Ни один из них не входит в замороженный allow-list
атрибутов workbookPr/sheetPr этого модуля (значит присутствие любого из
них в выходном пакете -> POST_VALIDATION_FAILED) — см. финальный отчёт
Implementation Pass, раздел "Security self-review", п. про codeName/syncRef.
"""

from __future__ import annotations

import dataclasses
import hashlib
import re
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path
from typing import Callable, Optional, Union

from app.scrub.errors import PackageScrubError, PackageScrubReason
from app.scrub.inventory import _stream_relationships
from app.scrub.mutate import (
    _ALLOWED_CUSTOM_NUMBER_FORMATS,
    _ALLOWED_FONT_NAMES,
    _DA_STYLE_NAME_TEMPLATE,
    _SAFE_DEFAULT_PIVOT_STYLE,
    _SAFE_DEFAULT_TABLE_STYLE,
    _TRUSTED_BUILTIN_STYLE_PAIRS,
)
from app.scrub.preflight import (
    MAX_NONEMPTY_CELLS,
    _SUPPORTED_EXTENSION,
    _WORKBOOK_PART,
    _WORKBOOK_RELS_PART,
    _canonical_member_name,
    _cheap_zip_preflight,
    _close_or_fail,
    _is_nonempty_cell_element,
    _is_safe_member_name,
    _open_member_stream,
    _read_workbook_sheet_rids,
    _resolve_relationship_target,
    _safe_close_suppressing,
)

_PathLike = Union[str, Path]

# ----------------------------------------------------------------------
# Frozen package-control constants (Stage10C.2.4 Contract Freeze)
# ----------------------------------------------------------------------

_CONTENT_TYPES_PART = "[Content_Types].xml"
_ROOT_RELS_PART = "_rels/.rels"
_STYLES_PART = "xl/styles.xml"
_THEME_PART = "xl/theme/theme1.xml"
_CORE_PART = "docProps/core.xml"
_APP_PART = "docProps/app.xml"

_MIN_PACKAGE_ENTRIES = 9

# Frozen SHA-256 (Stage10C.2.4 Contract Freeze, эмпирически воспроизведено
# 3x на разных input-вариантах — openpyxl.loaded_theme=None и docProps/app.xml
# детерминированы независимо от содержимого workbook, см. модульный docstring
# app.scrub.mutate, BLOCKER-3 Correction Pass).
_FROZEN_THEME_SHA256 = "d15e8ebf78ef7b9720839d7ae8fdc81a7df5bc24706d8e137df61a5683c358d9"
_FROZEN_APP_XML_SHA256 = "209fca6b00afe72a5029754b94be5953d8f16d96f67130325566b9366ad4ccc5"

_FIXED_CORE_CREATED = "2000-01-01T00:00:00Z"

_SML_NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
_R_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
_PKG_REL_NS = "http://schemas.openxmlformats.org/package/2006/relationships"
_CT_NS = "http://schemas.openxmlformats.org/package/2006/content-types"
_CP_NS = "http://schemas.openxmlformats.org/package/2006/metadata/core-properties"
_DCTERMS_NS = "http://purl.org/dc/terms/"
_XSI_NS = "http://www.w3.org/2001/XMLSchema-instance"
_XML_NS = "http://www.w3.org/XML/1998/namespace"

_ATTR_R_ID = f"{{{_R_NS}}}id"
_ATTR_XML_SPACE = f"{{{_XML_NS}}}space"
_ATTR_XSI_TYPE = f"{{{_XSI_NS}}}type"

_CT_WORKBOOK = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"
_CT_WORKSHEET = "application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"
_CT_STYLES = "application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"
_CT_THEME = "application/vnd.openxmlformats-officedocument.theme+xml"
_CT_CORE_PROPS = "application/vnd.openxmlformats-package.core-properties+xml"
_CT_APP_PROPS = "application/vnd.openxmlformats-officedocument.extended-properties+xml"

_ALLOWED_DEFAULTS = {
    "rels": "application/vnd.openxmlformats-package.relationships+xml",
    "xml": "application/xml",
}

_REL_OFFICE_DOCUMENT = f"{_R_NS}/officeDocument"
_REL_CORE_PROPERTIES = f"{_PKG_REL_NS}/metadata/core-properties"
_REL_EXTENDED_PROPERTIES = f"{_R_NS}/extended-properties"
_REL_WORKSHEET = f"{_R_NS}/worksheet"
_REL_STYLES = f"{_R_NS}/styles"
_REL_THEME = f"{_R_NS}/theme"


def _tag(local: str) -> str:
    return f"{{{_SML_NS}}}{local}"


# ----------------------------------------------------------------------
# PackageValidationResult
# ----------------------------------------------------------------------


def _is_strict_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


@dataclasses.dataclass(frozen=True)
class PackageValidationResult:
    """
    Результат validate_scrubbed_workbook_package: неконфиденциальные
    счётчики package-уровня, ничего не содержащие из пути/имени части/
    worksheet name/SHA/значения ячейки/formula-текста/metadata-строки.
    """

    zip_entry_count: int
    package_size_bytes: int
    worksheet_count: int
    formula_cell_count: int

    def __post_init__(self) -> None:
        for field_name in (
            "zip_entry_count",
            "package_size_bytes",
            "worksheet_count",
            "formula_cell_count",
        ):
            if not _is_strict_int(getattr(self, field_name)):
                raise ValueError(f"PackageValidationResult.{field_name} должен быть int")
        if self.zip_entry_count < _MIN_PACKAGE_ENTRIES:
            raise ValueError("PackageValidationResult.zip_entry_count должен быть >= 9")
        if self.package_size_bytes <= 0:
            raise ValueError("PackageValidationResult.package_size_bytes должен быть > 0")
        if self.worksheet_count < 1:
            raise ValueError("PackageValidationResult.worksheet_count должен быть >= 1")
        if self.formula_cell_count < 0:
            raise ValueError("PackageValidationResult.formula_cell_count должен быть >= 0")


# ----------------------------------------------------------------------
# Generic value-shape validators (замкнутые, безопасные домены для
# атрибутов, у которых нет конфиденциального free-text канала)
# ----------------------------------------------------------------------

_RE_BOOL01 = re.compile(r"^[01]$")
_RE_INT = re.compile(r"^-?[0-9]+$")
_RE_FLOAT = re.compile(r"^-?[0-9]+(\.[0-9]+)?$")
_RE_CELL_REF = re.compile(r"^\$?[A-Z]{1,3}\$?[0-9]+$")
_RE_CELL_RANGE = re.compile(r"^\$?[A-Z]{1,3}\$?[0-9]+(:\$?[A-Z]{1,3}\$?[0-9]+)?$")
_RE_RGB_COLOR = re.compile(r"^[0-9A-Fa-f]{6}([0-9A-Fa-f]{2})?$")
_RE_UNIVERSAL_MEASURE = re.compile(r"^-?[0-9]+(\.[0-9]+)?(mm|cm|in|pt|pc|pi)$")
_RE_W3CDTF = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")

# GradientFill Grammar v1 (Contract Freeze Pass) — единственный источник
# истины: openpyxl.compat.safe_string() форматирует degree/left/right/top/
# bottom/position ИСКЛЮЧИТЕЛЬНО через "%.16g" % value (NaN/Inf -> ""),
# НИКОГДА через простой decimal (существующий _float_shape этого не
# покрывает — легитимный degree="1e-07" им бы отклонялся, что эмпирически
# подтверждено как реальный false-positive риск, не гипотетический). Этот
# regex — ТОЧНОЕ соответствие лексической форме "%.16g", не обобщение и
# не float()+isfinite() (тот принял бы "1_000"/" 1"/"nan"/"Infinity",
# которые openpyxl никогда не производит).
_RE_GRADIENT_NUMERIC = re.compile(r"^-?[0-9]+(\.[0-9]+)?([eE][+-][0-9]+)?$")

# DAStyle-regex ВЫВЕДЕН из _DA_STYLE_NAME_TEMPLATE (app.scrub.mutate), а не
# продублирован как отдельный литерал "DAStyle\d{4}" — единственный
# источник истины для формата custom-имени остаётся mutate.py.
_DASTYLE_PREFIX, _DASTYLE_SUFFIX = _DA_STYLE_NAME_TEMPLATE.split("{index:04d}")
_RE_DASTYLE_NAME = re.compile(rf"^{re.escape(_DASTYLE_PREFIX)}\d{{4}}{re.escape(_DASTYLE_SUFFIX)}$")

_Validator = Callable[[str], bool]


def _bool01(value: str) -> bool:
    return bool(_RE_BOOL01.match(value))


def _int_shape(value: str) -> bool:
    return bool(_RE_INT.match(value))


def _float_shape(value: str) -> bool:
    return bool(_RE_FLOAT.match(value))


def _cellref_shape(value: str) -> bool:
    return bool(_RE_CELL_REF.match(value))


def _cellrange_shape(value: str) -> bool:
    return bool(_RE_CELL_RANGE.match(value))


def _sqref_shape(value: str) -> bool:
    tokens = value.split(" ")
    return len(tokens) > 0 and all(_cellrange_shape(tok) for tok in tokens if tok)


def _rgb_color_shape(value: str) -> bool:
    return bool(_RE_RGB_COLOR.match(value))


def _universal_measure_shape(value: str) -> bool:
    return bool(_RE_UNIVERSAL_MEASURE.match(value))


def _tint_shape(value: str) -> bool:
    if not _RE_FLOAT.match(value):
        return False
    try:
        return -1.0 <= float(value) <= 1.0
    except ValueError:
        return False


def _gradient_numeric_shape(value: str) -> bool:
    """
    GradientFill Grammar v1: degree/left/right/top/bottom — тот же
    "%.16g"-лексический домен, БЕЗ дополнительного numeric range (frozen
    contract §8: для этих пяти полей range сознательно не введён — у
    openpyxl.styles.fills.GradientFill.degree/left/right/top/bottom нет
    собственных границ, и придумывать их не на чем).
    """
    return bool(_RE_GRADIENT_NUMERIC.match(value))


def _gradient_position_shape(value: str) -> bool:
    """
    GradientFill Grammar v1: Stop.position — тот же лексический домен,
    ПЛЮС обязательный численный диапазон 0..1 (frozen contract §8/§9) —
    именно то бы, что openpyxl.styles.fills.Stop.position = MinMax(0, 1)
    обеспечивает для конечных чисел (NaN обходит MinMax внутри самого
    openpyxl, но лексический regex выше уже отклоняет "nan" как строку,
    поэтому это не является брешью для validator-а).
    """
    if not _RE_GRADIENT_NUMERIC.match(value):
        return False
    return 0.0 <= float(value) <= 1.0


def _enum(values: frozenset) -> _Validator:
    def _check(value: str) -> bool:
        return value in values

    return _check


# ----------------------------------------------------------------------
# Color attribute grammar (общий для fonts/fills/borders — тот же набор
# полей, что и InlineFont.color, Stage10C.2.3 BLOCKER-4 Correction Pass)
#
# MINOR-2 Correction Pass: до этого прохода допускались одновременно
# противоречивые комбинации (rgb+theme+type и т.п.), поскольку каждый
# атрибут проверялся независимо, без учёта совместной семантики. Disposable-
# эксперимент (реальный scrub-вывод, openpyxl==3.1.5, 8 независимых
# комбинаций: rgb-only, indexed-only, theme-only, theme+tint, auto-only,
# rgb+tint, indexed+tint, auto+tint, плюс default Color() -> rgb="00000000")
# ЭМПИРИЧЕСКИ ПОКАЗАЛ, что:
#   1) "type" НИ РАЗУ не появляется в реальном выводе ни в одной комбинации
#      (openpyxl.styles.colors.Color сериализует его крайне редко и не в
#      обычном object-model пути scrub-а) — предложенная в задании модель
#      "color mode определяется type" НЕ СООТВЕТСТВУЕТ реальному выводу;
#   2) ровно ОДИН из {rgb, indexed, theme, auto} присутствует ВСЕГДА (никогда
#      ноль, никогда два и более) — Color() без аргументов сериализуется как
#      rgb="00000000", а не как элемент без ни одного из четырёх полей;
#   3) "tint" — универсальный модификатор, эмпирически подтверждён
#      совместно со ВСЕМИ четырьмя режимами (rgb+tint, indexed+tint,
#      theme+tint, auto+tint), а не только с theme, как можно было бы
#      предположить.
# Поскольку наблюдаемая реальность прямо противоречит предложенной в
# задании type-based модели, "type" полностью исключён из allow-list
# (его присутствие -> POST_VALIDATION_FAILED, а не разрешённая по типу
# комбинация) — это НЕ архитектурный выбор, а прямое, безальтернативное
# следствие эксперимента (задание, требующее type-семантику, само по себе
# не реализуемо на реальных данных). Экспериментально НЕ подтверждённой
# осталась только связка type-ассоциированных ограничений из задания —
# она снята целиком, а не заменена собственным предположением.
# ----------------------------------------------------------------------

_COLOR_MODE_ATTRS: dict[str, _Validator] = {
    "rgb": _rgb_color_shape,
    "indexed": _int_shape,
    "auto": _bool01,
    "theme": _int_shape,
}

_COLOR_ATTRS: dict[str, _Validator] = {
    **_COLOR_MODE_ATTRS,
    "tint": _tint_shape,
}

_COLOR_MODE_KEYS = frozenset(_COLOR_MODE_ATTRS)


def _validate_color_attrs(elem: ET.Element) -> bool:
    """
    Строгая grammar-проверка ОДНОГО <color>/<fgColor>/<bgColor> элемента:
    каждый атрибут — из замкнутого _COLOR_ATTRS-домена (значение проходит
    свой validator), И ровно один из {rgb, indexed, theme, auto} обязан
    присутствовать (не ноль, не два и более) — см. MINOR-2 Correction Pass
    выше. Универсальный validator (_validate_attrs) этого не выражает,
    поскольку проверяет каждый атрибут независимо, без cross-attribute
    инварианта.
    """
    for key, value in elem.attrib.items():
        validator = _COLOR_ATTRS.get(key)
        if validator is None or not validator(value):
            return False
    mode_count = sum(1 for key in elem.attrib if key in _COLOR_MODE_KEYS)
    return mode_count == 1

# ----------------------------------------------------------------------
# InlineFont / Rich Text grammar (Stage10C.2.3 BLOCKER-4 Correction Pass
# — точные value-домены, переиспользованы как СПЕЦИФИКАЦИЯ, не как
# импортированный код: mutate.py не экспортирует эти домены как
# отдельные символы).
# ----------------------------------------------------------------------

_ALLOWED_RFONT_NAMES = _ALLOWED_FONT_NAMES

_INLINE_FONT_CHILD_ATTRS: dict[str, dict[str, _Validator]] = {
    "rFont": {"val": _enum(frozenset(_ALLOWED_RFONT_NAMES))},
    "b": {"val": _bool01},
    "i": {"val": _bool01},
    "u": {
        "val": _enum(
            frozenset({"single", "double", "singleAccounting", "doubleAccounting"})
        )
    },
    "strike": {"val": _bool01},
    "outline": {"val": _bool01},
    "shadow": {"val": _bool01},
    "condense": {"val": _bool01},
    "extend": {"val": _bool01},
    "sz": {"val": _float_shape},
    "color": _COLOR_ATTRS,
    "vertAlign": {"val": _enum(frozenset({"superscript", "subscript", "baseline"}))},
    "family": {"val": _int_shape},
    "charset": {"val": _int_shape},
    "scheme": {"val": _enum(frozenset({"major", "minor"}))},
}

# Font-registry <font> — те же поля, что InlineFont, плюс "name" вместо
# "rFont" (единственное отличие тега; значение-домен идентичен).
_FONT_CHILD_ATTRS: dict[str, dict[str, _Validator]] = dict(_INLINE_FONT_CHILD_ATTRS)
_FONT_CHILD_ATTRS["name"] = {"val": _enum(frozenset(_ALLOWED_FONT_NAMES))}
del _FONT_CHILD_ATTRS["rFont"]


# ----------------------------------------------------------------------
# Общий движок потоковой grammar-валидации (stack-based, ET.iterparse)
# ----------------------------------------------------------------------
#
# grammar: dict[full_tag, _ElementGrammar] — full_tag включает namespace
# (f"{{{ns}}}local"). Для каждого встреченного элемента:
#   - тег должен входить в allowed children родителя (или быть root_tag
#     на верхнем уровне);
#   - все атрибуты должны входить в allowed attrs этого тега, значение —
#     проходить validator;
#   - namespace, отсутствующий в grammar в принципе (тег не найден ни в
#     одном allowed-children множестве и не является root) -> reject.
#
# Память: как и в preflight.py/inventory.py, elem.clear() на каждом
# "end", независимо от тега; полный DOM никогда не строится.


@dataclasses.dataclass
class _ElementGrammar:
    attrs: dict[str, _Validator]
    children: frozenset


def _validate_attrs(elem: ET.Element, grammar_entry: _ElementGrammar) -> bool:
    for key, value in elem.attrib.items():
        validator = grammar_entry.attrs.get(key)
        if validator is None:
            return False
        if not validator(value):
            return False
    return True


def _is_whitespace_only(value: Optional[str]) -> bool:
    """
    BLOCKER-1 Correction Pass: единственная определяющая функция для
    "structural text/tail" политики — None, пустая строка или строка,
    состоящая исключительно из XML-форматирующих пробельных символов
    (пробел/таб/CR/LF — ровно то, что даёт str.strip()), допустимы;
    любой иной символ — недопустим. НЕ используется для text-bearing
    элементов (<v>/<f>/<t>/dcterms:created/dcterms:modified) — у них
    своя, уже существующая семантическая проверка значения.
    """
    return value is None or value.strip() == ""


def _stream_grammar(
    stream,
    grammar: dict[str, _ElementGrammar],
    root_tag: str,
    text_bearing_tags: frozenset = frozenset(),
) -> None:
    """
    Потоково проверяет один XML-документ целиком против замкнутой
    grammar-таблицы. Поднимает PackageScrubError(POST_VALIDATION_FAILED)
    при первом нарушении (неизвестный тег/атрибут/namespace, недопустимое
    значение атрибута, любой XML comment/processing instruction, любой
    non-whitespace elem.text вне text_bearing_tags, любой non-whitespace
    elem.tail на ЛЮБОМ элементе — BLOCKER-1 Correction Pass). Ничего не
    возвращает — вызывающий код, которому нужны данные из элементов
    (например, счётчики), обязан подписаться отдельно (см.
    worksheet-specific проход, который использует свой собственный
    потоковый цикл вместо этого generic engine).

    Tail-политика ОБЯЗАНА быть отложенной на один шаг вперёд: эмпирически
    подтверждено (BLOCKER-1 Correction Pass, disposable-эксперимент с
    ET.iterparse и tail-текстом > 16 КБ, что больше внутреннего read-
    буфера ElementTree.iterparse), что elem.tail НЕ гарантированно
    заполнен в момент "end"-события ЭТОГО ЖЕ элемента — он гарантированно
    полностью заполнен только к моменту СЛЕДУЮЩЕГО события потока (start
    следующего sibling, или end родителя). Наивная проверка elem.tail
    сразу на "end" с немедленным elem.clear() была бы тем же классом
    бага под видом исправления (для tail длиннее ~16 КБ проверка увидела
    бы ПУСТУЮ строку вместо реальной инъекции). Поэтому elem.clear()
    для КАЖДОГO элемента откладывается на один шаг: очищается и
    проверяется на tail только после того, как обработано СЛЕДУЮЩЕЕ
    событие потока (pending_tail_elem). elem.text, напротив, гарантированно
    полностью заполнен уже на "end" СВОЕГО элемента (подтверждено тем же
    экспериментом с text > 16 КБ) — отдельной отсрочки не требует.
    """
    stack: list[str] = []
    violation = False
    parse_failed = False
    pending_tail_elem: Optional[ET.Element] = None
    try:
        for event, elem in ET.iterparse(stream, events=("start", "end", "comment", "pi")):
            if event in ("comment", "pi"):
                violation = True
                break

            if pending_tail_elem is not None:
                if not _is_whitespace_only(pending_tail_elem.tail):
                    violation = True
                    break
                pending_tail_elem.clear()
                pending_tail_elem = None

            if event == "start":
                tag = elem.tag
                if not stack:
                    if tag != root_tag:
                        violation = True
                        break
                else:
                    parent_grammar = grammar.get(stack[-1])
                    if parent_grammar is None or tag not in parent_grammar.children:
                        violation = True
                        break
                grammar_entry = grammar.get(tag)
                if grammar_entry is None:
                    violation = True
                    break
                if not _validate_attrs(elem, grammar_entry):
                    violation = True
                    break
                stack.append(tag)
                continue
            # event == "end"
            tag = elem.tag
            if tag not in text_bearing_tags and not _is_whitespace_only(elem.text):
                violation = True
                break
            if stack and stack[-1] == tag:
                stack.pop()
            pending_tail_elem = elem
    except ET.ParseError:
        parse_failed = True

    if not violation and not parse_failed and pending_tail_elem is not None:
        if not _is_whitespace_only(pending_tail_elem.tail):
            violation = True
        else:
            pending_tail_elem.clear()

    if parse_failed:
        _safe_close_suppressing(stream)
        raise PackageScrubError(PackageScrubReason.POST_VALIDATION_FAILED)
    if violation:
        _safe_close_suppressing(stream)
        raise PackageScrubError(PackageScrubReason.POST_VALIDATION_FAILED)

    _close_or_fail(stream)


def _leaf(attrs: dict[str, _Validator]) -> _ElementGrammar:
    return _ElementGrammar(attrs=attrs, children=frozenset())


# ----------------------------------------------------------------------
# [Content_Types].xml
# ----------------------------------------------------------------------


def _validate_content_types(
    zf: zipfile.ZipFile, canonical_names: set[str]
) -> tuple[dict[str, str], dict[str, str]]:
    """
    Возвращает (override_map, default_map) по успешной проверке: только
    допустимые Default (rels/xml) и Override (workbook/worksheet/styles/
    theme/core/app) — любой иной Extension/ContentType/дубликат/orphan-
    override (не существующая физическая часть) -> POST_VALIDATION_FAILED.
    """
    tag_types = f"{{{_CT_NS}}}Types"
    tag_default = f"{{{_CT_NS}}}Default"
    tag_override = f"{{{_CT_NS}}}Override"

    if _canonical_member_name(_CONTENT_TYPES_PART) not in canonical_names:
        raise PackageScrubError(PackageScrubReason.POST_VALIDATION_FAILED)

    stream = _open_member_stream(zf, _CONTENT_TYPES_PART)
    default_map: dict[str, str] = {}
    override_map: dict[str, str] = {}
    violation = False
    parse_failed = False
    pending_tail_elem: Optional[ET.Element] = None
    try:
        for event, elem in ET.iterparse(stream, events=("start", "end", "comment", "pi")):
            if event in ("comment", "pi"):
                violation = True
                break

            if pending_tail_elem is not None:
                if not _is_whitespace_only(pending_tail_elem.tail):
                    violation = True
                    break
                pending_tail_elem.clear()
                pending_tail_elem = None

            if event == "end":
                if not _is_whitespace_only(elem.text):
                    violation = True
                    break
                pending_tail_elem = elem
                continue
            tag = elem.tag
            if tag == tag_types:
                if elem.attrib:
                    violation = True
                    break
                continue
            if tag == tag_default:
                ext = elem.get("Extension")
                content_type = elem.get("ContentType")
                if not ext or not content_type or len(elem.attrib) != 2:
                    violation = True
                    break
                ext_key = ext.casefold()
                if ext_key in default_map or _ALLOWED_DEFAULTS.get(ext_key) != content_type:
                    violation = True
                    break
                default_map[ext_key] = content_type
                continue
            if tag == tag_override:
                part_name = elem.get("PartName")
                content_type = elem.get("ContentType")
                if not part_name or not content_type or len(elem.attrib) != 2:
                    violation = True
                    break
                normalized = part_name[1:] if part_name.startswith("/") else part_name
                if not _is_safe_member_name(normalized):
                    violation = True
                    break
                canonical_part = _canonical_member_name(normalized)
                if canonical_part in override_map or canonical_part not in canonical_names:
                    violation = True
                    break
                expected = _EXPECTED_OVERRIDE_CONTENT_TYPE.get(canonical_part)
                is_worksheet_part = canonical_part.startswith("xl/worksheets/") and canonical_part.endswith(
                    ".xml"
                )
                if expected is None and not is_worksheet_part:
                    violation = True
                    break
                if expected is not None and content_type != expected:
                    violation = True
                    break
                if expected is None and content_type != _CT_WORKSHEET:
                    violation = True
                    break
                override_map[canonical_part] = content_type
                continue
            violation = True
            break
    except ET.ParseError:
        parse_failed = True

    if not violation and not parse_failed and pending_tail_elem is not None:
        if not _is_whitespace_only(pending_tail_elem.tail):
            violation = True
        else:
            pending_tail_elem.clear()

    if parse_failed or violation:
        _safe_close_suppressing(stream)
        raise PackageScrubError(PackageScrubReason.POST_VALIDATION_FAILED)

    _close_or_fail(stream)
    return override_map, default_map


_EXPECTED_OVERRIDE_CONTENT_TYPE = {
    _canonical_member_name(_WORKBOOK_PART): _CT_WORKBOOK,
    _canonical_member_name(_STYLES_PART): _CT_STYLES,
    _canonical_member_name(_THEME_PART): _CT_THEME,
    _canonical_member_name(_CORE_PART): _CT_CORE_PROPS,
    _canonical_member_name(_APP_PART): _CT_APP_PROPS,
}


def _effective_content_type(
    canonical_part_name: str, override_map: dict[str, str], default_map: dict[str, str]
) -> Optional[str]:
    if canonical_part_name in override_map:
        return override_map[canonical_part_name]
    ext = canonical_part_name.rsplit(".", 1)[-1] if "." in canonical_part_name else ""
    return default_map.get(ext)


# ----------------------------------------------------------------------
# Strict structural text/tail/comment/PI pre-scan для .rels-файлов
# ----------------------------------------------------------------------
#
# BLOCKER-1 Correction Pass, §9: _stream_relationships (app.scrub.inventory)
# — переиспользуемый generic-примитив, который читает ТОЛЬКО Id/Type/
# Target/TargetMode через elem.get(...) и НЕ проверяет elem.text/elem.tail
# и НЕ подписан на comment/pi-события ET.iterparse — он не даёт (и не
# был спроектирован давать) гарантию отсутствия arbitrary free-text
# carrier-а в .rels-документе. Модифицировать inventory.py в рамках этого
# исправления не требуется и не производится (shared helper не меняется).
# Вместо этого .rels-часть предварительно (отдельным, независимым
# проходом по СВЕЖЕ открытому потоку той же части) прогоняется через
# строгий lexical/structural сканер ниже — ни один XML comment/processing
# instruction/non-whitespace text/tail не допускается нигде в
# Relationships/Relationship (у обоих элементов нет ни одного легитимного
# text-bearing поля — только атрибуты). Только после успешного прохождения
# этого скана часть передаётся в _stream_relationships для семантической
# (Id/Type/Target/TargetMode) проверки.


def _reject_comment_pi_and_structural_text_tail(zf: zipfile.ZipFile, part_name: str) -> None:
    stream = _open_member_stream(zf, part_name)
    violation = False
    parse_failed = False
    pending_tail_elem: Optional[ET.Element] = None
    try:
        for event, elem in ET.iterparse(stream, events=("start", "end", "comment", "pi")):
            if event in ("comment", "pi"):
                violation = True
                break

            if pending_tail_elem is not None:
                if not _is_whitespace_only(pending_tail_elem.tail):
                    violation = True
                    break
                pending_tail_elem.clear()
                pending_tail_elem = None

            if event != "end":
                continue
            if not _is_whitespace_only(elem.text):
                violation = True
                break
            pending_tail_elem = elem
    except ET.ParseError:
        parse_failed = True

    if not violation and not parse_failed and pending_tail_elem is not None:
        if not _is_whitespace_only(pending_tail_elem.tail):
            violation = True
        else:
            pending_tail_elem.clear()

    if parse_failed or violation:
        _safe_close_suppressing(stream)
        raise PackageScrubError(PackageScrubReason.POST_VALIDATION_FAILED)

    _close_or_fail(stream)


# ----------------------------------------------------------------------
# _rels/.rels (root relationships)
# ----------------------------------------------------------------------


def _validate_root_rels(
    zf: zipfile.ZipFile,
    canonical_names: set[str],
    override_map: dict[str, str],
    default_map: dict[str, str],
) -> None:
    if _canonical_member_name(_ROOT_RELS_PART) not in canonical_names:
        raise PackageScrubError(PackageScrubReason.POST_VALIDATION_FAILED)

    _reject_comment_pi_and_structural_text_tail(zf, _ROOT_RELS_PART)

    counts = {_REL_OFFICE_DOCUMENT: 0, _REL_CORE_PROPERTIES: 0, _REL_EXTENDED_PROPERTIES: 0}
    allowed_types = set(counts)

    def on_record(_rid: str, rtype: str, target: str, mode: str) -> None:
        if rtype not in allowed_types or mode != "Internal":
            raise PackageScrubError(PackageScrubReason.POST_VALIDATION_FAILED)
        counts[rtype] += 1
        if counts[rtype] > 1:
            raise PackageScrubError(PackageScrubReason.POST_VALIDATION_FAILED)
        canonical_target = _resolve_relationship_target(target, "")
        if canonical_target is None:
            raise PackageScrubError(PackageScrubReason.POST_VALIDATION_FAILED)
        canonical_target = _canonical_member_name(canonical_target)
        if canonical_target not in canonical_names:
            raise PackageScrubError(PackageScrubReason.POST_VALIDATION_FAILED)
        expected_part = {
            _REL_OFFICE_DOCUMENT: _WORKBOOK_PART,
            _REL_CORE_PROPERTIES: _CORE_PART,
            _REL_EXTENDED_PROPERTIES: _APP_PART,
        }[rtype]
        if canonical_target != _canonical_member_name(expected_part):
            raise PackageScrubError(PackageScrubReason.POST_VALIDATION_FAILED)
        expected_ct = {
            _REL_OFFICE_DOCUMENT: _CT_WORKBOOK,
            _REL_CORE_PROPERTIES: _CT_CORE_PROPS,
            _REL_EXTENDED_PROPERTIES: _CT_APP_PROPS,
        }[rtype]
        if _effective_content_type(canonical_target, override_map, default_map) != expected_ct:
            raise PackageScrubError(PackageScrubReason.POST_VALIDATION_FAILED)

    _stream_relationships(zf, _ROOT_RELS_PART, on_record)

    if any(count != 1 for count in counts.values()):
        raise PackageScrubError(PackageScrubReason.POST_VALIDATION_FAILED)


# ----------------------------------------------------------------------
# xl/_rels/workbook.xml.rels
# ----------------------------------------------------------------------


def _validate_workbook_rels(
    zf: zipfile.ZipFile,
    canonical_names: set[str],
    override_map: dict[str, str],
    default_map: dict[str, str],
    sheet_rids: list[str],
) -> dict[str, str]:
    """
    Возвращает {rid: canonical_worksheet_target} для каждого sheet r:id
    из <sheets>. Требует ровно N worksheet-relationships (N == len(sheet_rids)),
    ровно 1 styles, ровно 1 theme, никаких иных Type (в частности
    sharedStrings/calcChain запрещены безусловно на выходе).
    """
    _reject_comment_pi_and_structural_text_tail(zf, _WORKBOOK_RELS_PART)

    needed = set(sheet_rids)
    allowed_types = {_REL_WORKSHEET, _REL_STYLES, _REL_THEME}
    worksheet_targets: dict[str, str] = {}
    counts = {_REL_STYLES: 0, _REL_THEME: 0}

    def on_record(rid: str, rtype: str, target: str, mode: str) -> None:
        if rtype not in allowed_types or mode != "Internal":
            raise PackageScrubError(PackageScrubReason.POST_VALIDATION_FAILED)
        if rtype == _REL_WORKSHEET:
            if rid not in needed:
                raise PackageScrubError(PackageScrubReason.POST_VALIDATION_FAILED)
            if rid in worksheet_targets:
                raise PackageScrubError(PackageScrubReason.POST_VALIDATION_FAILED)
            canonical_target = _resolve_relationship_target(target, "xl")
            if canonical_target is None:
                raise PackageScrubError(PackageScrubReason.POST_VALIDATION_FAILED)
            canonical_target = _canonical_member_name(canonical_target)
            if canonical_target not in canonical_names:
                raise PackageScrubError(PackageScrubReason.POST_VALIDATION_FAILED)
            expected_ct = _effective_content_type(canonical_target, override_map, default_map)
            if expected_ct != _CT_WORKSHEET:
                raise PackageScrubError(PackageScrubReason.POST_VALIDATION_FAILED)
            worksheet_targets[rid] = canonical_target
            return
        counts[rtype] += 1
        if counts[rtype] > 1:
            raise PackageScrubError(PackageScrubReason.POST_VALIDATION_FAILED)
        canonical_target = _resolve_relationship_target(target, "xl")
        expected_part = _STYLES_PART if rtype == _REL_STYLES else _THEME_PART
        if canonical_target is None or _canonical_member_name(canonical_target) != _canonical_member_name(
            expected_part
        ):
            raise PackageScrubError(PackageScrubReason.POST_VALIDATION_FAILED)
        canonical_target = _canonical_member_name(canonical_target)
        expected_ct = _CT_STYLES if rtype == _REL_STYLES else _CT_THEME
        if _effective_content_type(canonical_target, override_map, default_map) != expected_ct:
            raise PackageScrubError(PackageScrubReason.POST_VALIDATION_FAILED)

    _stream_relationships(zf, _WORKBOOK_RELS_PART, on_record)

    if len(worksheet_targets) != len(sheet_rids) or counts[_REL_STYLES] != 1 or counts[_REL_THEME] != 1:
        raise PackageScrubError(PackageScrubReason.POST_VALIDATION_FAILED)
    return worksheet_targets


# ----------------------------------------------------------------------
# xl/workbook.xml grammar
# ----------------------------------------------------------------------

_WORKBOOK_PR_ATTRS: dict[str, _Validator] = {
    "date1904": _bool01,
    "dateCompatibility": _bool01,
    "showObjects": _enum(frozenset({"all", "placeholders", "none"})),
    "showBorderUnselectedTables": _bool01,
    "filterPrivacy": _bool01,
    "promptedSolutions": _bool01,
    "showInkAnnotation": _bool01,
    "backupFile": _bool01,
    "saveExternalLinkValues": _bool01,
    "updateLinks": _enum(frozenset({"never", "userSet", "always"})),
    "hidePivotFieldList": _bool01,
    "showPivotChartFilter": _bool01,
    "allowRefreshQuery": _bool01,
    "publishItems": _bool01,
    "checkCompatibility": _bool01,
    "autoCompressPictures": _bool01,
    "refreshAllConnections": _bool01,
    "defaultThemeVersion": _int_shape,
}

_BOOK_VIEW_ATTRS: dict[str, _Validator] = {
    "visibility": _enum(frozenset({"hidden", "veryHidden", "visible"})),
    "minimized": _bool01,
    "showHorizontalScroll": _bool01,
    "showVerticalScroll": _bool01,
    "showSheetTabs": _bool01,
    "xWindow": _int_shape,
    "yWindow": _int_shape,
    "windowWidth": _int_shape,
    "windowHeight": _int_shape,
    "tabRatio": _int_shape,
    "firstSheet": _int_shape,
    "activeTab": _int_shape,
    "autoFilterDateGrouping": _bool01,
}

# MINOR-3 Correction Pass: структурная (НЕ known-value) грамматика имени
# листа. Независимо эмпирически проверено на установленном openpyxl==3.1.5
# (Workbook.title setter + полный scrub_workbook_object_model pipeline):
#   - forbidden chars [ ] : * ? / \ — openpyxl САМ поднимает ValueError при
#     попытке присвоить такое имя листу -> легитимный 2.3-вывод НИКОГДА их
#     не содержит; безопасно запрещать;
#   - control chars (U+0000-U+001F, U+007F) — присвоение имени с control-
#     char'ом openpyxl НЕ отклоняет на этапе .title=..., НО последующий
#     full scrub_workbook_object_model(source) НАДЁЖНО завершается
#     SCRUB_FAILED (эмпирически подтверждено: openpyxl.load_workbook не
#     может корректно перечитать порождённый control-char XML) — легитимный
#     2.3-вывод НИКОГДА их не содержит; безопасно запрещать как defense-in-
#     depth против hand-crafted ZIP, минующего pipeline;
#   - длина >31 символа — openpyxl ТОЛЬКО выдаёт UserWarning, не отклоняет;
#     ЭМПИРИЧЕСКИ ПОДТВЕРЖДЕНО (полный pipeline, 40-символьное имя),
#     что такое имя РЕАЛЬНО переживает scrub_workbook_object_model и
#     является ЛЕГИТИМНЫМ достижимым выводом Stage10C.2.3 — задание
#     предлагало ограничить 1..31, но это ПРОТИВОРЕЧИТ наблюдаемой
#     реальности (см. задание §21: "НЕ вводить новый запрет молча", если
#     openpyxl 3.1.5 допускает и frozen contract ранее не запрещал) —
#     максимум в 31 символ сознательно НЕ введён;
#   - ведущий/конечный апостроф — openpyxl принимает БЕЗ ограничений
#     (эмпирически подтверждено) — новый запрет не введён.
_FORBIDDEN_SHEET_NAME_CHARS = frozenset("[]:*?/\\")


def _sheet_name_shape(value: str) -> bool:
    if len(value) < 1:
        return False
    for ch in value:
        if ch in _FORBIDDEN_SHEET_NAME_CHARS:
            return False
        code_point = ord(ch)
        if code_point <= 0x1F or code_point == 0x7F:
            return False
    return True


_SHEET_ATTRS: dict[str, _Validator] = {
    "name": _sheet_name_shape,
    "sheetId": _int_shape,
    "state": _enum(frozenset({"visible", "hidden", "veryHidden"})),
    _ATTR_R_ID: lambda v: len(v) > 0,
}

_CALC_PR_ATTRS: dict[str, _Validator] = {
    "calcId": _int_shape,
    "calcMode": _enum(frozenset({"auto", "manual", "autoNoTable"})),
    "fullCalcOnLoad": _bool01,
    "refMode": _enum(frozenset({"A1", "R1C1"})),
    "iterate": _bool01,
    "iterateCount": _int_shape,
    "iterateDelta": _float_shape,
    "fullPrecision": _bool01,
    "calcCompleted": _bool01,
    "calcOnSave": _bool01,
    "concurrentCalc": _bool01,
    "concurrentManualCount": _int_shape,
    "forceFullCalc": _bool01,
}

_WORKBOOK_GRAMMAR: dict[str, _ElementGrammar] = {
    _tag("workbook"): _ElementGrammar(
        attrs={},
        children=frozenset(
            {
                _tag("workbookPr"),
                _tag("workbookProtection"),
                _tag("bookViews"),
                _tag("sheets"),
                _tag("definedNames"),
                _tag("calcPr"),
            }
        ),
    ),
    _tag("workbookPr"): _leaf(_WORKBOOK_PR_ATTRS),
    _tag("workbookProtection"): _leaf({}),
    _tag("bookViews"): _ElementGrammar(attrs={}, children=frozenset({_tag("workbookView")})),
    _tag("workbookView"): _leaf(_BOOK_VIEW_ATTRS),
    _tag("sheets"): _ElementGrammar(attrs={}, children=frozenset({_tag("sheet")})),
    _tag("sheet"): _leaf(_SHEET_ATTRS),
    _tag("definedNames"): _leaf({}),
    _tag("calcPr"): _leaf(_CALC_PR_ATTRS),
}


def _validate_workbook_xml_grammar(zf: zipfile.ZipFile) -> None:
    stream = _open_member_stream(zf, _WORKBOOK_PART)
    _stream_grammar(stream, _WORKBOOK_GRAMMAR, _tag("workbook"))


# ----------------------------------------------------------------------
# xl/styles.xml grammar
# ----------------------------------------------------------------------

_ALIGNMENT_ATTRS: dict[str, _Validator] = {
    "horizontal": _enum(
        frozenset(
            {"general", "left", "center", "right", "fill", "justify", "centerContinuous", "distributed"}
        )
    ),
    "vertical": _enum(frozenset({"top", "center", "bottom", "justify", "distributed"})),
    "textRotation": lambda v: _int_shape(v) and (0 <= int(v) <= 180 or int(v) == 255),
    "wrapText": _bool01,
    "shrinkToFit": _bool01,
    "indent": lambda v: _int_shape(v) and int(v) >= 0,
    "relativeIndent": _int_shape,
    "justifyLastLine": _bool01,
    "readingOrder": lambda v: _int_shape(v) and int(v) >= 0,
}

_PROTECTION_ATTRS: dict[str, _Validator] = {"locked": _bool01, "hidden": _bool01}

_XF_ATTRS: dict[str, _Validator] = {
    "numFmtId": _int_shape,
    "fontId": _int_shape,
    "fillId": _int_shape,
    "borderId": _int_shape,
    "applyAlignment": _bool01,
    "applyProtection": _bool01,
    "applyFont": _bool01,
    "applyFill": _bool01,
    "applyBorder": _bool01,
    "applyNumberFormat": _bool01,
    "pivotButton": _bool01,
    "quotePrefix": _bool01,
    "xfId": _int_shape,
}

_SIDE_STYLE_ENUM = _enum(
    frozenset(
        {
            "thin",
            "medium",
            "dashed",
            "dotted",
            "thick",
            "double",
            "hair",
            "mediumDashed",
            "dashDot",
            "mediumDashDot",
            "dashDotDot",
            "mediumDashDotDot",
            "slantDashDot",
        }
    )
)

_PATTERN_TYPE_ENUM = _enum(
    frozenset(
        {
            "solid",
            "mediumGray",
            "darkGray",
            "lightGray",
            "darkHorizontal",
            "darkVertical",
            "darkDown",
            "darkUp",
            "darkGrid",
            "darkTrellis",
            "lightHorizontal",
            "lightVertical",
            "lightDown",
            "lightUp",
            "lightGrid",
            "lightTrellis",
            "gray125",
            "gray0625",
        }
    )
)

# GradientFill Grammar v1 (OD-10C2.4-R1, Contract Freeze Pass): "type"
# REQUIRED, только linear/path (openpyxl.styles.fills.GradientFill.type =
# Set(values=('linear','path')) — закрытый enum, подтверждено эмпирически:
# иное значение поднимает ValueError уже на уровне object model).
# degree/left/right/top/bottom — OPTIONAL, БЕЗ type-dependent restrictions
# (эмпирически подтверждено: type="linear" с одновременно заданными
# degree И left/right/top/bottom реально переживает Stage10C.2.3
# unchanged — вводить cross-attribute запрет было бы неподтверждённым
# реальностью ограничением).
_GRADIENT_FILL_ATTRS: dict[str, _Validator] = {
    "type": _enum(frozenset({"linear", "path"})),
    "degree": _gradient_numeric_shape,
    "left": _gradient_numeric_shape,
    "right": _gradient_numeric_shape,
    "top": _gradient_numeric_shape,
    "bottom": _gradient_numeric_shape,
}

# GradientFill Grammar v1: <stop position="0..1"> — REQUIRED, тот же
# лексический regex + диапазон 0..1 (openpyxl.styles.fills.Stop.position =
# MinMax(min=0, max=1)).
_STOP_ATTRS: dict[str, _Validator] = {"position": _gradient_position_shape}


_STYLES_GRAMMAR: dict[str, _ElementGrammar] = {
    _tag("styleSheet"): _ElementGrammar(
        attrs={},
        children=frozenset(
            {
                _tag("numFmts"),
                _tag("fonts"),
                _tag("fills"),
                _tag("borders"),
                _tag("cellStyleXfs"),
                _tag("cellXfs"),
                _tag("cellStyles"),
                _tag("dxfs"),
                _tag("tableStyles"),
                _tag("colors"),
            }
        ),
    ),
    _tag("numFmts"): _ElementGrammar(attrs={"count": _int_shape}, children=frozenset({_tag("numFmt")})),
    _tag("numFmt"): _leaf({"numFmtId": _int_shape, "formatCode": _enum(_ALLOWED_CUSTOM_NUMBER_FORMATS)}),
    _tag("fonts"): _ElementGrammar(attrs={"count": _int_shape}, children=frozenset({_tag("font")})),
    _tag("font"): _ElementGrammar(attrs={}, children=frozenset(_tag(n) for n in _FONT_CHILD_ATTRS)),
    **{_tag(name): _leaf(attrs) for name, attrs in _FONT_CHILD_ATTRS.items()},
    _tag("fills"): _ElementGrammar(attrs={"count": _int_shape}, children=frozenset({_tag("fill")})),
    _tag("fill"): _ElementGrammar(
        attrs={}, children=frozenset({_tag("patternFill"), _tag("gradientFill")})
    ),
    _tag("patternFill"): _ElementGrammar(
        attrs={"patternType": _PATTERN_TYPE_ENUM},
        children=frozenset({_tag("fgColor"), _tag("bgColor")}),
    ),
    _tag("fgColor"): _leaf(_COLOR_ATTRS),
    _tag("bgColor"): _leaf(_COLOR_ATTRS),
    _tag("gradientFill"): _ElementGrammar(attrs=_GRADIENT_FILL_ATTRS, children=frozenset({_tag("stop")})),
    _tag("stop"): _ElementGrammar(attrs=_STOP_ATTRS, children=frozenset({_tag("color")})),
    _tag("borders"): _ElementGrammar(attrs={"count": _int_shape}, children=frozenset({_tag("border")})),
    _tag("border"): _ElementGrammar(
        attrs={"outline": _bool01, "diagonalUp": _bool01, "diagonalDown": _bool01},
        children=frozenset(
            {
                _tag("start"),
                _tag("end"),
                _tag("left"),
                _tag("right"),
                _tag("top"),
                _tag("bottom"),
                _tag("diagonal"),
                _tag("vertical"),
                _tag("horizontal"),
            }
        ),
    ),
    **{
        _tag(n): _ElementGrammar(attrs={"style": _SIDE_STYLE_ENUM}, children=frozenset({_tag("color")}))
        for n in ("start", "end", "left", "right", "top", "bottom", "diagonal", "vertical", "horizontal")
    },
    _tag("color"): _leaf(_COLOR_ATTRS),
    _tag("cellStyleXfs"): _ElementGrammar(attrs={"count": _int_shape}, children=frozenset({_tag("xf")})),
    _tag("cellXfs"): _ElementGrammar(attrs={"count": _int_shape}, children=frozenset({_tag("xf")})),
    _tag("xf"): _ElementGrammar(
        attrs=_XF_ATTRS, children=frozenset({_tag("alignment"), _tag("protection")})
    ),
    _tag("alignment"): _leaf(_ALIGNMENT_ATTRS),
    _tag("protection"): _leaf(_PROTECTION_ATTRS),
    _tag("cellStyles"): _ElementGrammar(attrs={"count": _int_shape}, children=frozenset({_tag("cellStyle")})),
    _tag("cellStyle"): _leaf(
        {
            "name": lambda v: True,
            "xfId": _int_shape,
            "builtinId": _int_shape,
            "iLevel": _int_shape,
            "hidden": _bool01,
            "customBuiltin": _bool01,
        }
    ),
    _tag("dxfs"): _leaf({"count": lambda v: v == "0"}),
    _tag("tableStyles"): _leaf(
        {
            "count": lambda v: v == "0",
            "defaultTableStyle": lambda v: v == _SAFE_DEFAULT_TABLE_STYLE,
            "defaultPivotStyle": lambda v: v == _SAFE_DEFAULT_PIVOT_STYLE,
        }
    ),
    _tag("colors"): _ElementGrammar(
        attrs={}, children=frozenset({_tag("indexedColors"), _tag("mruColors")})
    ),
    _tag("indexedColors"): _ElementGrammar(attrs={}, children=frozenset({_tag("rgbColor")})),
    _tag("mruColors"): _ElementGrammar(attrs={}, children=frozenset({_tag("color")})),
    _tag("rgbColor"): _leaf({"rgb": _rgb_color_shape}),
}


# MINOR-1 Correction Pass: контейнеры styles.xml, чей "count" атрибут (при
# наличии) обязан ТОЧНО совпадать с фактическим числом прямых детей.
# dxfs/tableStyles сюда не входят — их "count" уже принудительно
# зафиксирован к литералу "0" самой grammar-таблицей (_STYLES_GRAMMAR),
# а дети у них запрещены полностью (frozenset()), поэтому рассинхрон
# структурно невозможен без отдельной проверки.
_COUNTED_CONTAINERS = frozenset(
    {
        _tag("numFmts"),
        _tag("fonts"),
        _tag("fills"),
        _tag("borders"),
        _tag("cellStyleXfs"),
        _tag("cellXfs"),
        _tag("cellStyles"),
    }
)


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1] if tag.startswith("{") else tag


def _validate_styles_xml(zf: zipfile.ZipFile) -> tuple[int, int, int, int, set[int]]:
    """
    Полная потоковая grammar-проверка xl/styles.xml через движок
    _stream_grammar, ПЛЮС дополнительный (не выразимый в generic-таблице)
    проход: точный exact-pair trust для <cellStyle> (переиспользует
    _TRUSTED_BUILTIN_STYLE_PAIRS/_DA_STYLE_NAME_TEMPLATE из
    app.scrub.mutate) и сбор границ font/fill/border/numFmtId для
    bounds-check ссылок из worksheet <c s="...">.

    Возвращает (font_count, fill_count, border_count, cell_xfs_count,
    custom_numfmt_ids).
    """
    stream = _open_member_stream(zf, _STYLES_PART)

    counters = {"font": 0, "fill": 0, "border": 0, "cell_xfs": 0}
    custom_numfmt_ids: set[int] = set()
    cell_style_names_seen: set[str] = set()
    violation = False
    parse_failed = False
    stack: list[str] = []
    in_cell_xfs = False
    pending_tail_elem: Optional[ET.Element] = None

    # GradientFill Grammar v1: <fill> не нумерует детей через "count" (в
    # отличие от _COUNTED_CONTAINERS) — cross-child multiplicity "ровно
    # один из {patternFill, gradientFill}" проверяется отдельным scalar-
    # счётчиком, сбрасываемым на каждом новом <fill> (fill/stop не
    # вложены друг в друга, поэтому одного scalar достаточно — тот же
    # принцип, что уже применён к in_cell_xfs).
    fill_child_count = 0
    stop_color_count = 0

    # MINOR-1 Correction Pass: "count" атрибут (numFmts/fonts/fills/
    # borders/cellStyleXfs/cellXfs/cellStyles), когда присутствует,
    # обязан ТОЧНО совпадать с фактическим числом прямых детей — иначе
    # POST_VALIDATION_FAILED. Это НЕ security-bounds-логика (та по-
    # прежнему строится исключительно на counters/actual-числах выше,
    # никогда на декларированном count) — это отдельная consistency-
    # проверка недостоверного hint'а, добавленная по MINOR-1.
    container_declared: dict[str, Optional[int]] = {}
    container_actual: dict[str, int] = {}

    try:
        for event, elem in ET.iterparse(stream, events=("start", "end", "comment", "pi")):
            if event in ("comment", "pi"):
                violation = True
                break

            if pending_tail_elem is not None:
                if not _is_whitespace_only(pending_tail_elem.tail):
                    violation = True
                    break
                pending_tail_elem.clear()
                pending_tail_elem = None

            tag = elem.tag
            if event == "start":
                if not stack:
                    if tag != _tag("styleSheet"):
                        violation = True
                        break
                else:
                    parent_grammar = _STYLES_GRAMMAR.get(stack[-1])
                    if parent_grammar is None or tag not in parent_grammar.children:
                        violation = True
                        break
                grammar_entry = _STYLES_GRAMMAR.get(tag)
                if grammar_entry is None:
                    violation = True
                    break

                if tag == _tag("cellStyle"):
                    name = elem.get("name")
                    builtin_id_raw = elem.get("builtinId")
                    if name is None or name in cell_style_names_seen:
                        violation = True
                        break
                    cell_style_names_seen.add(name)
                    if builtin_id_raw is not None:
                        if not _int_shape(builtin_id_raw):
                            violation = True
                            break
                        pair = (name, int(builtin_id_raw))
                        if pair not in _TRUSTED_BUILTIN_STYLE_PAIRS:
                            violation = True
                            break
                    else:
                        if not _RE_DASTYLE_NAME.match(name):
                            violation = True
                            break
                    if not _validate_attrs(elem, grammar_entry):
                        violation = True
                        break
                    if stack and stack[-1] == _tag("cellStyles"):
                        container_actual[_tag("cellStyles")] = (
                            container_actual.get(_tag("cellStyles"), 0) + 1
                        )
                    stack.append(tag)
                    continue

                if not _validate_attrs(elem, grammar_entry):
                    violation = True
                    break

                if tag in (_tag("color"), _tag("fgColor"), _tag("bgColor")):
                    if not _validate_color_attrs(elem):
                        violation = True
                        break
                    if tag == _tag("color") and stack and stack[-1] == _tag("stop"):
                        stop_color_count += 1

                if tag == _tag("gradientFill"):
                    if elem.get("type") is None:
                        violation = True
                        break
                elif tag == _tag("stop"):
                    if elem.get("position") is None:
                        violation = True
                        break
                    stop_color_count = 0

                if tag in (_tag("patternFill"), _tag("gradientFill")) and stack and stack[-1] == _tag("fill"):
                    fill_child_count += 1

                if tag in _COUNTED_CONTAINERS:
                    count_raw = elem.get("count")
                    container_declared[tag] = int(count_raw) if count_raw is not None else None
                    container_actual[tag] = 0

                if tag == _tag("numFmt"):
                    if stack and stack[-1] == _tag("numFmts"):
                        container_actual[_tag("numFmts")] = container_actual.get(_tag("numFmts"), 0) + 1
                    numfmt_id_raw = elem.get("numFmtId")
                    if numfmt_id_raw is not None and _int_shape(numfmt_id_raw):
                        custom_numfmt_ids.add(int(numfmt_id_raw))
                elif tag == _tag("xf"):
                    if stack and stack[-1] in (_tag("cellStyleXfs"), _tag("cellXfs")):
                        container_actual[stack[-1]] = container_actual.get(stack[-1], 0) + 1
                    if in_cell_xfs:
                        counters["cell_xfs"] += 1
                        font_id_raw = elem.get("fontId")
                        fill_id_raw = elem.get("fillId")
                        border_id_raw = elem.get("borderId")
                        numfmt_id_raw = elem.get("numFmtId")
                        # MINOR-A Final Pre-Commit Pass: _int_shape (уже
                        # применённый выше через _validate_attrs) допускает
                        # знак "-", поэтому bounds-check обязан явно
                        # отклонять отрицательные значения — верхняя
                        # граница (">= count") сама по себе НЕ отклоняет
                        # отрицательные индексы (Python-сравнение "-1 >= N"
                        # для N>0 всегда False). Independent Re-Review
                        # эмпирически подтвердил обход именно здесь.
                        if font_id_raw is not None:
                            font_id_val = int(font_id_raw)
                            if font_id_val < 0 or font_id_val >= counters["font"]:
                                violation = True
                                break
                        if fill_id_raw is not None:
                            fill_id_val = int(fill_id_raw)
                            if fill_id_val < 0 or fill_id_val >= counters["fill"]:
                                violation = True
                                break
                        if border_id_raw is not None:
                            border_id_val = int(border_id_raw)
                            if border_id_val < 0 or border_id_val >= counters["border"]:
                                violation = True
                                break
                        if numfmt_id_raw is not None:
                            numfmt_id_val = int(numfmt_id_raw)
                            if numfmt_id_val < 0 or (
                                numfmt_id_val >= 164 and numfmt_id_val not in custom_numfmt_ids
                            ):
                                violation = True
                                break
                elif tag == _tag("cellXfs"):
                    in_cell_xfs = True
                elif tag == _tag("font"):
                    if stack and stack[-1] == _tag("fonts"):
                        container_actual[_tag("fonts")] = container_actual.get(_tag("fonts"), 0) + 1
                    counters["font"] += 1
                elif tag == _tag("fill"):
                    if stack and stack[-1] == _tag("fills"):
                        container_actual[_tag("fills")] = container_actual.get(_tag("fills"), 0) + 1
                    counters["fill"] += 1
                    fill_child_count = 0
                elif tag == _tag("border"):
                    if stack and stack[-1] == _tag("borders"):
                        container_actual[_tag("borders")] = container_actual.get(_tag("borders"), 0) + 1
                    counters["border"] += 1

                stack.append(tag)
                continue

            # event == "end"
            if not _is_whitespace_only(elem.text):
                violation = True
                break
            if tag in _COUNTED_CONTAINERS:
                declared = container_declared.get(tag)
                actual = container_actual.get(tag, 0)
                if declared is not None and declared != actual:
                    violation = True
                    break
            if tag == _tag("fill") and fill_child_count != 1:
                violation = True
                break
            if tag == _tag("stop") and stop_color_count != 1:
                violation = True
                break
            if tag == _tag("cellXfs"):
                in_cell_xfs = False
            if stack and stack[-1] == tag:
                stack.pop()
            pending_tail_elem = elem
    except ET.ParseError:
        parse_failed = True

    if not violation and not parse_failed and pending_tail_elem is not None:
        if not _is_whitespace_only(pending_tail_elem.tail):
            violation = True
        else:
            pending_tail_elem.clear()

    if parse_failed or violation:
        _safe_close_suppressing(stream)
        raise PackageScrubError(PackageScrubReason.POST_VALIDATION_FAILED)
    if "Normal" not in cell_style_names_seen:
        _safe_close_suppressing(stream)
        raise PackageScrubError(PackageScrubReason.POST_VALIDATION_FAILED)

    _close_or_fail(stream)
    return counters["font"], counters["fill"], counters["border"], counters["cell_xfs"], custom_numfmt_ids


# ----------------------------------------------------------------------
# xl/worksheets/sheetN.xml grammar (потоково, streaming, с подсчётом
# nonempty cells / formula cells в ОДНОМ проходе)
# ----------------------------------------------------------------------

_DIMENSION_ATTRS: dict[str, _Validator] = {"ref": _cellrange_shape}

_SHEET_PR_ATTRS: dict[str, _Validator] = {}
_OUTLINE_PR_ATTRS: dict[str, _Validator] = {
    "applyStyles": _bool01,
    "summaryBelow": _bool01,
    "summaryRight": _bool01,
    "showOutlineSymbols": _bool01,
}
_PAGE_SETUP_PR_ATTRS: dict[str, _Validator] = {"autoPageBreaks": _bool01, "fitToPage": _bool01}

_SHEET_VIEW_ATTRS: dict[str, _Validator] = {
    "windowProtection": _bool01,
    "showFormulas": _bool01,
    "showGridLines": _bool01,
    "showRowColHeaders": _bool01,
    "showZeros": _bool01,
    "rightToLeft": _bool01,
    "tabSelected": _bool01,
    "showRuler": _bool01,
    "showOutlineSymbols": _bool01,
    "defaultGridColor": _bool01,
    "showWhiteSpace": _bool01,
    "view": _enum(frozenset({"normal", "pageBreakPreview", "pageLayout"})),
    "topLeftCell": _cellref_shape,
    "colorId": _int_shape,
    "zoomScale": _int_shape,
    "zoomScaleNormal": _int_shape,
    "zoomScaleSheetLayoutView": _int_shape,
    "zoomScalePageLayoutView": _int_shape,
    "zoomToFit": _bool01,
    "workbookViewId": _int_shape,
}

_PANE_ATTRS: dict[str, _Validator] = {
    "xSplit": _float_shape,
    "ySplit": _float_shape,
    "topLeftCell": _cellref_shape,
    "activePane": _enum(frozenset({"topLeft", "topRight", "bottomLeft", "bottomRight"})),
    "state": _enum(frozenset({"split", "frozen", "frozenSplit"})),
}

_SELECTION_ATTRS: dict[str, _Validator] = {
    "pane": _enum(frozenset({"topLeft", "topRight", "bottomLeft", "bottomRight"})),
    "activeCell": _cellref_shape,
    "activeCellId": _int_shape,
    "sqref": _sqref_shape,
}

_SHEET_FORMAT_PR_ATTRS: dict[str, _Validator] = {
    "baseColWidth": _int_shape,
    "defaultColWidth": _float_shape,
    "defaultRowHeight": _float_shape,
    "customHeight": _bool01,
    "zeroHeight": _bool01,
    "thickTop": _bool01,
    "thickBottom": _bool01,
    "outlineLevelRow": _int_shape,
    "outlineLevelCol": _int_shape,
}

_COL_ATTRS: dict[str, _Validator] = {
    "min": _int_shape,
    "max": _int_shape,
    "width": _float_shape,
    "hidden": _bool01,
    "outlineLevel": _int_shape,
    "collapsed": _bool01,
    "bestFit": _bool01,
    "customWidth": _bool01,
    "style": _int_shape,
}

_ROW_ATTRS: dict[str, _Validator] = {
    "r": _int_shape,
    "hidden": _bool01,
    "outlineLevel": _int_shape,
    "collapsed": _bool01,
    "ht": _float_shape,
    "customFormat": _bool01,
    "customHeight": _bool01,
    "s": _int_shape,
    "thickBot": _bool01,
    "thickTop": _bool01,
}

_CELL_ALLOWED_T = frozenset({"n", "str", "b", "e", "inlineStr"})
_CELL_ATTRS: dict[str, _Validator] = {
    "r": _cellref_shape,
    "s": _int_shape,
    "t": _enum(_CELL_ALLOWED_T),
}

_FORMULA_ATTRS: dict[str, _Validator] = {
    "t": _enum(frozenset({"array", "dataTable"})),
    "ref": _cellrange_shape,
    "r1": _cellref_shape,
    "r2": _cellref_shape,
    "dt2D": _bool01,
    "dtr": _bool01,
    "del1": _bool01,
    "del2": _bool01,
    "ca": _bool01,
}

_MERGE_CELL_ATTRS: dict[str, _Validator] = {"ref": _cellrange_shape}

_PRINT_OPTIONS_ATTRS: dict[str, _Validator] = {
    "horizontalCentered": _bool01,
    "verticalCentered": _bool01,
    "headings": _bool01,
    "gridLines": _bool01,
    "gridLinesSet": _bool01,
}

_PAGE_MARGINS_ATTRS: dict[str, _Validator] = {
    "left": _float_shape,
    "right": _float_shape,
    "top": _float_shape,
    "bottom": _float_shape,
    "header": _float_shape,
    "footer": _float_shape,
}

_PAGE_SETUP_ATTRS: dict[str, _Validator] = {
    "orientation": _enum(frozenset({"default", "portrait", "landscape"})),
    "paperSize": _int_shape,
    "scale": _int_shape,
    "fitToHeight": _int_shape,
    "fitToWidth": _int_shape,
    "firstPageNumber": _int_shape,
    "horizontalDpi": _int_shape,
    "verticalDpi": _int_shape,
    "copies": _int_shape,
    "useFirstPageNumber": _bool01,
    "usePrinterDefaults": _bool01,
    "blackAndWhite": _bool01,
    "draft": _bool01,
    "paperHeight": _universal_measure_shape,
    "paperWidth": _universal_measure_shape,
    "pageOrder": _enum(frozenset({"downThenOver", "overThenDown"})),
    "cellComments": _enum(frozenset({"asDisplayed", "atEnd"})),
    "errors": _enum(frozenset({"displayed", "blank", "dash", "NA"})),
}

_RPR_CHILDREN = frozenset(_tag(n) for n in _INLINE_FONT_CHILD_ATTRS)

# BLOCKER-1 Correction Pass: единственные легитимные text-bearing элементы
# worksheet.xml — <v> (cached formula value / cell value), <f> (formula
# текст — известный debt до Formula Safety Gate), <t> (plain inline string
# ИЛИ rich text run text, оба используют один и тот же тег). Любой другой
# элемент worksheet.xml обязан иметь elem.text is None/whitespace-only.
_WORKSHEET_TEXT_BEARING_TAGS = frozenset({_tag("v"), _tag("f"), _tag("t")})

_WORKSHEET_GRAMMAR: dict[str, _ElementGrammar] = {
    _tag("worksheet"): _ElementGrammar(
        attrs={},
        children=frozenset(
            {
                _tag("sheetPr"),
                _tag("dimension"),
                _tag("sheetViews"),
                _tag("sheetFormatPr"),
                _tag("cols"),
                _tag("sheetData"),
                _tag("mergeCells"),
                _tag("printOptions"),
                _tag("pageMargins"),
                _tag("pageSetup"),
            }
        ),
    ),
    _tag("sheetPr"): _ElementGrammar(
        attrs=_SHEET_PR_ATTRS, children=frozenset({_tag("outlinePr"), _tag("pageSetUpPr")})
    ),
    _tag("outlinePr"): _leaf(_OUTLINE_PR_ATTRS),
    _tag("pageSetUpPr"): _leaf(_PAGE_SETUP_PR_ATTRS),
    _tag("dimension"): _leaf(_DIMENSION_ATTRS),
    _tag("sheetViews"): _ElementGrammar(attrs={}, children=frozenset({_tag("sheetView")})),
    _tag("sheetView"): _ElementGrammar(
        attrs=_SHEET_VIEW_ATTRS, children=frozenset({_tag("pane"), _tag("selection")})
    ),
    _tag("pane"): _leaf(_PANE_ATTRS),
    _tag("selection"): _leaf(_SELECTION_ATTRS),
    _tag("sheetFormatPr"): _leaf(_SHEET_FORMAT_PR_ATTRS),
    _tag("cols"): _ElementGrammar(attrs={}, children=frozenset({_tag("col")})),
    _tag("col"): _leaf(_COL_ATTRS),
    _tag("sheetData"): _ElementGrammar(attrs={}, children=frozenset({_tag("row")})),
    _tag("row"): _ElementGrammar(attrs=_ROW_ATTRS, children=frozenset({_tag("c")})),
    _tag("c"): _ElementGrammar(
        attrs=_CELL_ATTRS, children=frozenset({_tag("v"), _tag("f"), _tag("is")})
    ),
    _tag("v"): _leaf({}),
    _tag("f"): _leaf(_FORMULA_ATTRS),
    _tag("is"): _ElementGrammar(attrs={}, children=frozenset({_tag("t"), _tag("r")})),
    _tag("r"): _ElementGrammar(attrs={}, children=frozenset({_tag("rPr"), _tag("t")})),
    _tag("rPr"): _ElementGrammar(attrs={}, children=_RPR_CHILDREN),
    **{_tag(n): _leaf(attrs) for n, attrs in _INLINE_FONT_CHILD_ATTRS.items()},
    _tag("t"): _leaf({_ATTR_XML_SPACE: lambda v: v == "preserve"}),
    _tag("mergeCells"): _ElementGrammar(attrs={"count": _int_shape}, children=frozenset({_tag("mergeCell")})),
    _tag("mergeCell"): _leaf(_MERGE_CELL_ATTRS),
    _tag("printOptions"): _leaf(_PRINT_OPTIONS_ATTRS),
    _tag("pageMargins"): _leaf(_PAGE_MARGINS_ATTRS),
    _tag("pageSetup"): _leaf(_PAGE_SETUP_ATTRS),
}


def _validate_worksheet_part(
    zf: zipfile.ZipFile,
    part_name: str,
    style_bounds: tuple[int, int, int, int, set[int]],
    nonempty_budget: int,
) -> tuple[int, int]:
    """
    Один потоковый проход по одной worksheet-части: grammar (через тот
    же stack-механизм, что _stream_grammar, но инлайново — нужно попутно
    считать nonempty/formula cells и bounds-check <c s="">/<row s="">/
    <col style="">, не создавая второй проход по тем же данным) +
    подсчёт nonempty cells (переиспользует ТОЧНУЮ семантику
    _is_nonempty_cell_element) с ранним прерыванием по MAX_NONEMPTY_CELLS
    + подсчёт formula cells (наличие дочернего <f> у <c>).

    Возвращает (nonempty_count, formula_count) для ЭТОЙ ОДНОЙ части.
    """
    font_count, fill_count, border_count, cell_xfs_count, custom_numfmt_ids = style_bounds
    stream = _open_member_stream(zf, part_name)

    stack: list[str] = []
    nonempty = 0
    formula_count = 0
    violation = False
    parse_failed = False
    resource_exceeded = False
    pending_tail_elem: Optional[ET.Element] = None

    try:
        for event, elem in ET.iterparse(stream, events=("start", "end", "comment", "pi")):
            if event in ("comment", "pi"):
                violation = True
                break

            if pending_tail_elem is not None:
                if not _is_whitespace_only(pending_tail_elem.tail):
                    violation = True
                    break
                pending_tail_elem.clear()
                pending_tail_elem = None

            tag = elem.tag
            if event == "start":
                if not stack:
                    if tag != _tag("worksheet"):
                        violation = True
                        break
                else:
                    parent_grammar = _WORKSHEET_GRAMMAR.get(stack[-1])
                    if parent_grammar is None or tag not in parent_grammar.children:
                        violation = True
                        break
                grammar_entry = _WORKSHEET_GRAMMAR.get(tag)
                if grammar_entry is None:
                    violation = True
                    break
                if not _validate_attrs(elem, grammar_entry):
                    violation = True
                    break

                if tag == _tag("color") and not _validate_color_attrs(elem):
                    violation = True
                    break

                if tag == _tag("c"):
                    s_raw = elem.get("s")
                    if s_raw is not None and int(s_raw) >= cell_xfs_count:
                        violation = True
                        break
                elif tag == _tag("row"):
                    s_raw = elem.get("s")
                    if s_raw is not None and int(s_raw) >= cell_xfs_count:
                        violation = True
                        break
                elif tag == _tag("col"):
                    style_raw = elem.get("style")
                    if style_raw is not None and int(style_raw) >= cell_xfs_count:
                        violation = True
                        break

                stack.append(tag)
                continue

            # event == "end"
            if tag not in _WORKSHEET_TEXT_BEARING_TAGS and not _is_whitespace_only(elem.text):
                violation = True
                break
            if tag == _tag("c"):
                if _is_nonempty_cell_element(elem):
                    nonempty += 1
                    if nonempty > nonempty_budget:
                        resource_exceeded = True
                        break
                if elem.find(_tag("f")) is not None:
                    formula_count += 1
            if stack and stack[-1] == tag:
                stack.pop()
            pending_tail_elem = elem
    except ET.ParseError:
        parse_failed = True

    if not violation and not parse_failed and not resource_exceeded and pending_tail_elem is not None:
        if not _is_whitespace_only(pending_tail_elem.tail):
            violation = True
        else:
            pending_tail_elem.clear()

    if parse_failed or violation:
        _safe_close_suppressing(stream)
        raise PackageScrubError(PackageScrubReason.POST_VALIDATION_FAILED)
    if resource_exceeded:
        _safe_close_suppressing(stream)
        raise PackageScrubError(PackageScrubReason.RESOURCE_LIMIT_EXCEEDED)

    _close_or_fail(stream)
    return nonempty, formula_count


# ----------------------------------------------------------------------
# docProps/core.xml
# ----------------------------------------------------------------------

_TAG_CORE_PROPERTIES = f"{{{_CP_NS}}}coreProperties"
_TAG_DC_CREATED = f"{{{_DCTERMS_NS}}}created"
_TAG_DC_MODIFIED = f"{{{_DCTERMS_NS}}}modified"


def _validate_core_xml(zf: zipfile.ZipFile) -> None:
    stream = _open_member_stream(zf, _CORE_PART)
    seen_created = False
    seen_modified = False
    violation = False
    parse_failed = False
    stack_depth = 0
    pending_tail_elem: Optional[ET.Element] = None
    try:
        for event, elem in ET.iterparse(stream, events=("start", "end", "comment", "pi")):
            if event in ("comment", "pi"):
                violation = True
                break

            if pending_tail_elem is not None:
                if not _is_whitespace_only(pending_tail_elem.tail):
                    violation = True
                    break
                pending_tail_elem.clear()
                pending_tail_elem = None

            tag = elem.tag
            if event == "start":
                stack_depth += 1
                if stack_depth == 1:
                    if tag != _TAG_CORE_PROPERTIES or elem.attrib:
                        violation = True
                        break
                    continue
                if stack_depth != 2 or tag not in (_TAG_DC_CREATED, _TAG_DC_MODIFIED):
                    violation = True
                    break
                if set(elem.attrib) != {_ATTR_XSI_TYPE} or elem.attrib.get(_ATTR_XSI_TYPE) != "dcterms:W3CDTF":
                    violation = True
                    break
                continue
            # end
            stack_depth -= 1
            if tag == _TAG_DC_CREATED:
                if seen_created or (elem.text or "") != _FIXED_CORE_CREATED:
                    violation = True
                    break
                seen_created = True
            elif tag == _TAG_DC_MODIFIED:
                if seen_modified or not _RE_W3CDTF.match(elem.text or ""):
                    violation = True
                    break
                seen_modified = True
            elif tag == _TAG_CORE_PROPERTIES:
                if not _is_whitespace_only(elem.text):
                    violation = True
                    break
            pending_tail_elem = elem
    except ET.ParseError:
        parse_failed = True

    if not violation and not parse_failed and pending_tail_elem is not None:
        if not _is_whitespace_only(pending_tail_elem.tail):
            violation = True
        else:
            pending_tail_elem.clear()

    if parse_failed or violation or not seen_created or not seen_modified:
        _safe_close_suppressing(stream)
        raise PackageScrubError(PackageScrubReason.POST_VALIDATION_FAILED)

    _close_or_fail(stream)


# ----------------------------------------------------------------------
# theme1.xml / app.xml — frozen SHA-256 (не парсятся)
# ----------------------------------------------------------------------


def _validate_frozen_hash(zf: zipfile.ZipFile, part_name: str, expected_sha256: str) -> None:
    stream = _open_member_stream(zf, part_name)
    hasher = hashlib.sha256()
    read_failed = False
    try:
        while True:
            chunk = stream.read(65536)
            if not chunk:
                break
            hasher.update(chunk)
    except Exception:
        read_failed = True

    if read_failed:
        _safe_close_suppressing(stream)
        raise PackageScrubError(PackageScrubReason.POST_VALIDATION_FAILED)
    _close_or_fail(stream)

    if hasher.hexdigest() != expected_sha256:
        raise PackageScrubError(PackageScrubReason.POST_VALIDATION_FAILED)


# ----------------------------------------------------------------------
# Public API
# ----------------------------------------------------------------------


def _expected_physical_parts(worksheet_targets: list[str]) -> set[str]:
    fixed = {
        _canonical_member_name(_CONTENT_TYPES_PART),
        _canonical_member_name(_ROOT_RELS_PART),
        _canonical_member_name(_APP_PART),
        _canonical_member_name(_CORE_PART),
        _canonical_member_name(_WORKBOOK_PART),
        _canonical_member_name(_WORKBOOK_RELS_PART),
        _canonical_member_name(_STYLES_PART),
        _canonical_member_name(_THEME_PART),
    }
    fixed.update(_canonical_member_name(target) for target in worksheet_targets)
    return fixed


def _validate_after_preflight(
    zf: zipfile.ZipFile,
    canonical_names: set[str],
    zip_entry_count: int,
    package_size_bytes: int,
) -> PackageValidationResult:
    if zip_entry_count < _MIN_PACKAGE_ENTRIES:
        raise PackageScrubError(PackageScrubReason.POST_VALIDATION_FAILED)

    for name in canonical_names:
        if name.endswith("/"):
            raise PackageScrubError(PackageScrubReason.POST_VALIDATION_FAILED)

    override_map, default_map = _validate_content_types(zf, canonical_names)
    _validate_root_rels(zf, canonical_names, override_map, default_map)

    sheet_rids = _read_workbook_sheet_rids(zf, canonical_names)
    worksheet_count = len(sheet_rids)
    if worksheet_count < 1:
        raise PackageScrubError(PackageScrubReason.POST_VALIDATION_FAILED)

    worksheet_targets_by_rid = _validate_workbook_rels(
        zf, canonical_names, override_map, default_map, sheet_rids
    )
    worksheet_targets = [worksheet_targets_by_rid[rid] for rid in sheet_rids]

    if len(set(worksheet_targets)) != len(worksheet_targets):
        raise PackageScrubError(PackageScrubReason.POST_VALIDATION_FAILED)

    sheet_names_casefold: set[str] = set()
    stream = _open_member_stream(zf, _WORKBOOK_PART)
    try:
        for _event, elem in ET.iterparse(stream, events=("end",)):
            if elem.tag == _tag("sheet"):
                name = elem.get("name")
                if name is None:
                    raise PackageScrubError(PackageScrubReason.POST_VALIDATION_FAILED)
                key = name.casefold()
                if key in sheet_names_casefold:
                    raise PackageScrubError(PackageScrubReason.POST_VALIDATION_FAILED)
                sheet_names_casefold.add(key)
            elem.clear()
    except ET.ParseError:
        _safe_close_suppressing(stream)
        raise PackageScrubError(PackageScrubReason.POST_VALIDATION_FAILED)
    except PackageScrubError:
        _safe_close_suppressing(stream)
        raise
    _close_or_fail(stream)

    _validate_workbook_xml_grammar(zf)

    expected_parts = _expected_physical_parts(worksheet_targets)
    if canonical_names != expected_parts:
        raise PackageScrubError(PackageScrubReason.POST_VALIDATION_FAILED)

    style_bounds = _validate_styles_xml(zf)

    total_nonempty = 0
    total_formula = 0
    for part_name in worksheet_targets:
        remaining_budget = MAX_NONEMPTY_CELLS - total_nonempty
        nonempty_delta, formula_delta = _validate_worksheet_part(
            zf, part_name, style_bounds, max(remaining_budget, 0)
        )
        total_nonempty += nonempty_delta
        total_formula += formula_delta
        if total_nonempty > MAX_NONEMPTY_CELLS:
            raise PackageScrubError(PackageScrubReason.RESOURCE_LIMIT_EXCEEDED)

    _validate_core_xml(zf)
    _validate_frozen_hash(zf, _THEME_PART, _FROZEN_THEME_SHA256)
    _validate_frozen_hash(zf, _APP_PART, _FROZEN_APP_XML_SHA256)

    return PackageValidationResult(
        zip_entry_count=zip_entry_count,
        package_size_bytes=package_size_bytes,
        worksheet_count=worksheet_count,
        formula_cell_count=total_formula,
    )


def validate_scrubbed_workbook_package(path: _PathLike) -> PackageValidationResult:
    """
    Strict Post-Save Package Validation (Stage 10C.2.4) — единственная
    точка входа. Принимает путь к уже сохранённому scrub-кандидату.

    :raises TypeError: path не str/Path.
    :raises PackageScrubError: см. PackageScrubReason.INVALID_INPUT_PACKAGE /
        UNSUPPORTED_PACKAGE_CONTENT (поднимается переиспользованными
        низкоуровневыми примитивами app.scrub.preflight при базовой ZIP/
        relationship-невалидности), PackageScrubReason.RESOURCE_LIMIT_EXCEEDED
        (любой из шести frozen-лимитов, перепроверенных на output),
        PackageScrubReason.POST_VALIDATION_FAILED (любое несоответствие
        замороженной output-грамматике — лишняя/отсутствующая физическая
        часть, неизвестный/запрещённый XML-элемент или атрибут, недопустимое
        значение, несовпадение frozen SHA-256 theme1.xml/app.xml, нарушение
        стилевой политики, style-index вне границ, дублирующееся/не-
        уникальное имя листа).
    """
    if not isinstance(path, (str, Path)):
        raise TypeError(f"path должен быть str или Path, получено: {type(path)!r}")

    candidate = Path(path)
    if candidate.suffix.lower() != _SUPPORTED_EXTENSION:
        raise PackageScrubError(PackageScrubReason.INVALID_INPUT_PACKAGE)

    zf, zip_entry_count, canonical_names, _total_uncompressed = _cheap_zip_preflight(candidate)

    stat_failed = False
    package_size_bytes = -1
    try:
        package_size_bytes = candidate.stat().st_size
    except OSError:
        stat_failed = True
    if stat_failed:
        _safe_close_suppressing(zf)
        raise PackageScrubError(PackageScrubReason.INVALID_INPUT_PACKAGE)

    try:
        result = _validate_after_preflight(zf, canonical_names, zip_entry_count, package_size_bytes)
    except PackageScrubError:
        _safe_close_suppressing(zf)
        raise
    _close_or_fail(zf)
    return result
