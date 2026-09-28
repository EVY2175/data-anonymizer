"""
Stage 10C.2.2 — Package Policy / Inventory.

DETECT / CLASSIFY / VALIDATE / REJECT — этот модуль НЕ выполняет
scrub/mutation (это будущий Stage10C.2.3) и не создаёт/не изменяет
никаких файлов.

======================================================================
Correction Pass after Independent Security Review — MAJOR-1/MINOR-1
======================================================================

MAJOR-1 (устранено): относительный разбор .rels-файлов больше НЕ строит
list[_RelationshipRecord] для ВСЕХ relationships безусловно. Вместо
этого используется потоковый callback-based обработчик
(_stream_relationships) — каждая relationship-запись обрабатывается
НЕМЕДЛЕННО (validate -> classify -> resolve/count -> elem.clear()) и не
удерживается в памяти дольше одного элемента. Hyperlink-записи вообще
никогда не превращаются в _RelationshipRecord — только инкремент
счётчика. Единственное неизбежное O(N)-состояние — множество уже
встреченных Id (для duplicate-detection, которое информационно-теоретически
не может быть дешевле O(distinct Id) без потери корректности) — см.
docstring _stream_relationships.

MINOR-1 (устранено): inspect_package_policy(path) теперь ПРОГРАММНО (не
только по документированной конвенции) сначала вызывает полный
app.scrub.preflight.preflight_xlsx_package(path) — MAX_NONEMPTY_CELLS
(Stage10C.2.1 Section B) не может быть случайно пропущен при прямом
вызове этой функции. Внутренняя package-policy логика вынесена в
_inspect_package_policy_after_preflight — вызывается ТОЛЬКО после
успешного preflight в рамках того же вызова inspect_package_policy.

======================================================================
Архитектура (Stage10C.2.2 Contract Correction + Final Freeze)
======================================================================

1. [Content_Types].xml разбирается ПОЛНОСТЬЮ, потоково (ET.iterparse,
   не ET.parse) — Default/Override-декларации валидируются НЕЗАВИСИМО
   от reachability: любой Override с запрещённым/неизвестным
   Content-Type отклоняется сразу, даже если ни одна relationship на
   него не ссылается (закрывает orphan-hard-reject-content bypass).
   override_map/default_map естественно ограничены MAX_ZIP_ENTRIES
   (каждая запись требует существования соответствующей физической
   части), поэтому отдельного streaming-callback здесь не требуется.

2. root (_rels/.rels), workbook (xl/_rels/workbook.xml.rels) и каждый
   worksheet .rels разбираются через _stream_relationships с
   role-специфичным callback — ни один не сужен под одну роль заранее
   (в отличие от app.scrub.preflight, который читает workbook.xml.rels
   только в объёме, нужном для разрешения sheet r:id), но и не
   накапливает список всех записей.

3. Для каждой allowed-роли выполняется трёхсторонняя сверка:
   relationship Type + resolved physical Target + effective Content-Type
   должны соответствовать ОДНОЙ и той же роли — несовпадение в любую
   сторону -> PackageScrubReason.INVALID_INPUT_PACKAGE.

4. Capstone-инвариант (§13/§26 контракта): после классификации КАЖДЫЙ
   физический ZIP-член обязан быть учтён ровно одной разрешённой ролью
   (или быть [Content_Types].xml / ожидаемым .rels) — иначе
   UNSUPPORTED_PACKAGE_CONTENT. Это же правило закрывает "unexpected
   .rels" (§24 контракта) как частный случай — незнакомый .rels-файл
   просто никогда не попадёт в accounted-множество.

======================================================================
OD-7
======================================================================

Ни одно исключение не содержит: путь входного файла, имя записи ZIP,
relationship Target/Id, PartName, worksheet name, Content-Type
пользовательского (не константного) значения, raw XML. Дисциплина
идентична app.scrub.preflight (flag-then-raise-outside-except,
_safe_close_suppressing/_close_or_fail для закрытия потоков).

======================================================================
Известный NOTE (не решается в этой фазе)
======================================================================

inspect_package_policy вызывает preflight_xlsx_package(path), а затем
_inspect_package_policy_after_preflight(path) заново открывает тот же
файл по пути (через _cheap_zip_preflight) — между этими двумя
открытиями существует теоретическое TOCTOU-окно (файл на диске мог
измениться). Это не решается здесь — требует более широкого решения
на уровне Stage10C-wide security closure (например, единый open
file-descriptor, передаваемый через весь конвейер), которое затронуло
бы closed Stage10C.2.1 без доказанного blocker для этого прохода.
"""

from __future__ import annotations

import typing
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path
from typing import Callable, Optional, Union

from app.scrub.errors import PackageScrubError, PackageScrubReason
from app.scrub.models import PackageInventory
from app.scrub.preflight import (
    _SUPPORTED_EXTENSION,
    _TAG_RELATIONSHIP,
    _WORKBOOK_PART,
    _WORKBOOK_RELS_PART,
    _canonical_member_name,
    _cheap_zip_preflight,
    _close_or_fail,
    _is_safe_member_name,
    _open_member_stream,
    _read_workbook_sheet_rids,
    _resolve_relationship_target,
    _safe_close_suppressing,
    preflight_xlsx_package,
)

_PathLike = Union[str, Path]

# ----------------------------------------------------------------------
# Frozen OPC/OOXML константы (Stage10C.2.2 Final Freeze)
# ----------------------------------------------------------------------

_CT_NS = "http://schemas.openxmlformats.org/package/2006/content-types"
_TAG_DEFAULT = f"{{{_CT_NS}}}Default"
_TAG_OVERRIDE = f"{{{_CT_NS}}}Override"

_CONTENT_TYPES_PART = "[Content_Types].xml"
_ROOT_RELS_PART = "_rels/.rels"

_ALLOWED_DEFAULTS = {
    "rels": "application/vnd.openxmlformats-package.relationships+xml",
    "xml": "application/xml",
    "vml": "application/vnd.openxmlformats-officedocument.vmlDrawing",
}

_CT_WORKBOOK = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"
_CT_WORKSHEET = "application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"
_CT_STYLES = "application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"
_CT_THEME = "application/vnd.openxmlformats-officedocument.theme+xml"
_CT_CORE_PROPS = "application/vnd.openxmlformats-package.core-properties+xml"
_CT_APP_PROPS = "application/vnd.openxmlformats-officedocument.extended-properties+xml"
_CT_CUSTOM_PROPS = "application/vnd.openxmlformats-officedocument.custom-properties+xml"
_CT_SHARED_STRINGS = "application/vnd.openxmlformats-officedocument.spreadsheetml.sharedStrings+xml"
_CT_CALC_CHAIN = "application/vnd.openxmlformats-officedocument.spreadsheetml.calcChain+xml"
_CT_COMMENTS = "application/vnd.openxmlformats-officedocument.spreadsheetml.comments+xml"
_CT_TABLE = "application/vnd.openxmlformats-officedocument.spreadsheetml.table+xml"
_CT_VML_DRAWING = "application/vnd.openxmlformats-officedocument.vmlDrawing"

_ALLOWED_OVERRIDE_CONTENT_TYPES = frozenset(
    {
        _CT_WORKBOOK,
        _CT_WORKSHEET,
        _CT_STYLES,
        _CT_THEME,
        _CT_CORE_PROPS,
        _CT_APP_PROPS,
        _CT_CUSTOM_PROPS,
        _CT_SHARED_STRINGS,
        _CT_CALC_CHAIN,
        _CT_COMMENTS,
        _CT_TABLE,
    }
)

_R_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
_PKG_REL_NS = "http://schemas.openxmlformats.org/package/2006/relationships"

_REL_OFFICE_DOCUMENT = f"{_R_NS}/officeDocument"
_REL_CORE_PROPERTIES = f"{_PKG_REL_NS}/metadata/core-properties"
_REL_EXTENDED_PROPERTIES = f"{_R_NS}/extended-properties"
_REL_CUSTOM_PROPERTIES = f"{_R_NS}/custom-properties"

_REL_WORKSHEET = f"{_R_NS}/worksheet"
_REL_STYLES = f"{_R_NS}/styles"
_REL_THEME = f"{_R_NS}/theme"
_REL_SHARED_STRINGS = f"{_R_NS}/sharedStrings"
_REL_CALC_CHAIN = f"{_R_NS}/calcChain"

_REL_HYPERLINK = f"{_R_NS}/hyperlink"
_REL_COMMENTS = f"{_R_NS}/comments"
_REL_VML_DRAWING = f"{_R_NS}/vmlDrawing"
_REL_TABLE = f"{_R_NS}/table"

# Ожидаемый Content-Type резолвнутой части для каждой relationship-роли,
# для которой применяется трёхсторонняя сверка (hyperlink сознательно
# исключён — его Target никогда не резолвится и не проверяется, см. §16
# контракта: "только count + classification").
_ROLE_CONTENT_TYPE = {
    _REL_OFFICE_DOCUMENT: _CT_WORKBOOK,
    _REL_CORE_PROPERTIES: _CT_CORE_PROPS,
    _REL_EXTENDED_PROPERTIES: _CT_APP_PROPS,
    _REL_CUSTOM_PROPERTIES: _CT_CUSTOM_PROPS,
    _REL_WORKSHEET: _CT_WORKSHEET,
    _REL_STYLES: _CT_STYLES,
    _REL_THEME: _CT_THEME,
    _REL_SHARED_STRINGS: _CT_SHARED_STRINGS,
    _REL_CALC_CHAIN: _CT_CALC_CHAIN,
    _REL_COMMENTS: _CT_COMMENTS,
    _REL_VML_DRAWING: _CT_VML_DRAWING,
    _REL_TABLE: _CT_TABLE,
}


class _RelationshipRecord(typing.NamedTuple):
    id: str
    type: str
    target: str
    mode: str  # "Internal" или "External" — никогда иное


# ----------------------------------------------------------------------
# [Content_Types].xml — потоковый разбор
# ----------------------------------------------------------------------


def _parse_content_types(
    zf: zipfile.ZipFile, canonical_names: set[str]
) -> tuple[dict[str, str], dict[str, str]]:
    """
    Возвращает (override_map, default_map): override_map — canonical
    PartName -> ContentType (только для допустимых, невалидные/запрещённые
    Content-Type отклоняются здесь же, независимо от reachability);
    default_map — canonical Extension -> ContentType (только rels/xml/vml).

    override_map естественно ограничен MAX_ZIP_ENTRIES: каждая запись
    требует существования соответствующей физической части
    (`canonical_part not in canonical_names` -> reject), поэтому здесь
    отдельный streaming-callback не требуется, в отличие от .rels
    (relationships — записи ВНУТРИ одного XML-документа, не отдельные
    ZIP-записи, и потому не ограничены MAX_ZIP_ENTRIES — см.
    _stream_relationships).
    """
    if _canonical_member_name(_CONTENT_TYPES_PART) not in canonical_names:
        raise PackageScrubError(PackageScrubReason.INVALID_INPUT_PACKAGE)

    stream = _open_member_stream(zf, _CONTENT_TYPES_PART)
    default_map: dict[str, str] = {}
    override_map: dict[str, str] = {}
    abort_reason: Optional[PackageScrubReason] = None
    parse_failed = False
    try:
        for _event, elem in ET.iterparse(stream, events=("end",)):
            tag = elem.tag
            if tag == _TAG_DEFAULT:
                ext = elem.get("Extension")
                content_type = elem.get("ContentType")
                if not ext or not content_type:
                    abort_reason = PackageScrubReason.INVALID_INPUT_PACKAGE
                    elem.clear()
                    break
                ext_key = ext.casefold()
                if ext_key in default_map:
                    abort_reason = PackageScrubReason.INVALID_INPUT_PACKAGE
                    elem.clear()
                    break
                if _ALLOWED_DEFAULTS.get(ext_key) != content_type:
                    abort_reason = PackageScrubReason.UNSUPPORTED_PACKAGE_CONTENT
                    elem.clear()
                    break
                default_map[ext_key] = content_type
                elem.clear()
                continue
            if tag == _TAG_OVERRIDE:
                part_name = elem.get("PartName")
                content_type = elem.get("ContentType")
                if not part_name or not content_type:
                    abort_reason = PackageScrubReason.INVALID_INPUT_PACKAGE
                    elem.clear()
                    break
                normalized = part_name[1:] if part_name.startswith("/") else part_name
                if not _is_safe_member_name(normalized):
                    abort_reason = PackageScrubReason.INVALID_INPUT_PACKAGE
                    elem.clear()
                    break
                canonical_part = _canonical_member_name(normalized)
                if canonical_part in override_map:
                    abort_reason = PackageScrubReason.INVALID_INPUT_PACKAGE
                    elem.clear()
                    break
                if canonical_part not in canonical_names:
                    abort_reason = PackageScrubReason.INVALID_INPUT_PACKAGE
                    elem.clear()
                    break
                if content_type not in _ALLOWED_OVERRIDE_CONTENT_TYPES:
                    abort_reason = PackageScrubReason.UNSUPPORTED_PACKAGE_CONTENT
                    elem.clear()
                    break
                override_map[canonical_part] = content_type
                elem.clear()
                continue
            elem.clear()
    except ET.ParseError:
        parse_failed = True

    if parse_failed:
        _safe_close_suppressing(stream)
        raise PackageScrubError(PackageScrubReason.INVALID_INPUT_PACKAGE)
    if abort_reason is not None:
        _safe_close_suppressing(stream)
        raise PackageScrubError(abort_reason)

    _close_or_fail(stream)
    return override_map, default_map


def _effective_content_type(
    canonical_part_name: str, override_map: dict[str, str], default_map: dict[str, str]
) -> Optional[str]:
    if canonical_part_name in override_map:
        return override_map[canonical_part_name]
    ext = canonical_part_name.rsplit(".", 1)[-1] if "." in canonical_part_name else ""
    return default_map.get(ext)


def _rels_sibling_path(part_name: str) -> str:
    if "/" in part_name:
        dir_part, base = part_name.rsplit("/", 1)
        return f"{dir_part}/_rels/{base}.rels"
    return f"_rels/{part_name}.rels"


# ----------------------------------------------------------------------
# Потоковый generic .rels processor (Correction Pass, MAJOR-1)
# ----------------------------------------------------------------------

_OnRecord = Callable[[str, str, str, str], None]


def _stream_relationships(zf: zipfile.ZipFile, rels_part_name: str, on_record: _OnRecord) -> None:
    """
    Потоково разбирает ОДИН .rels-файл, вызывая on_record(id, type, target,
    mode) для КАЖДОЙ relationship-записи немедленно — сама функция НЕ
    накапливает список записей (Correction Pass, MAJOR-1). Generic-
    валидация (duplicate Id, missing поля, TargetMode) выполняется здесь
    и остаётся неизменной; роль-специфичная классификация/резолюция/
    подсчёт делегируется on_record (вызывающему коду).

    Память: duplicate-Id detection требует множества уже встреченных Id
    (seen_ids) — это НЕИЗБЕЖНОЕ O(число relationship-записей в файле)
    состояние (корректно обнаружить дубликат среди N элементов
    информационно-теоретически невозможно, храня меньше ~N идентификаторов
    — см. Correction Pass §9/§5). В отличие от предыдущей версии, здесь
    НЕ хранятся Type/Target/TargetMode для каждой записи — только Id,
    что даёт кратное (не менее чем в разы) уменьшение per-entry footprint
    по сравнению с полным _RelationshipRecord (см. итоговый отчёт §6-8
    с эмпирическим сравнением).

    on_record может поднимать PackageScrubError (уже sanitized) — он
    перехватывается здесь ТОЛЬКО чтобы безопасно закрыть stream перед
    повторным подъёмом того же объекта исключения (OD-7 flag-then-raise
    паттерн, без создания нового исключения внутри except-блока).
    """
    stream = _open_member_stream(zf, rels_part_name)
    seen_ids: set[str] = set()
    abort_reason: Optional[PackageScrubReason] = None
    propagate: Optional[PackageScrubError] = None
    parse_failed = False
    try:
        for _event, elem in ET.iterparse(stream, events=("end",)):
            tag = elem.tag
            if tag != _TAG_RELATIONSHIP:
                elem.clear()
                continue
            rid = elem.get("Id")
            rtype = elem.get("Type")
            target = elem.get("Target")
            mode_raw = elem.get("TargetMode")
            if not rid or not rtype or target is None:
                abort_reason = PackageScrubReason.INVALID_INPUT_PACKAGE
                elem.clear()
                break
            if rid in seen_ids:
                abort_reason = PackageScrubReason.INVALID_INPUT_PACKAGE
                elem.clear()
                break
            if mode_raw is None:
                mode = "Internal"
            elif mode_raw == "External":
                mode = "External"
            else:
                abort_reason = PackageScrubReason.INVALID_INPUT_PACKAGE
                elem.clear()
                break
            seen_ids.add(rid)
            elem.clear()
            try:
                on_record(rid, rtype, target, mode)
            except PackageScrubError as e:
                propagate = e
                break
    except ET.ParseError:
        parse_failed = True

    if parse_failed:
        _safe_close_suppressing(stream)
        raise PackageScrubError(PackageScrubReason.INVALID_INPUT_PACKAGE)
    if propagate is not None:
        _safe_close_suppressing(stream)
        raise propagate
    if abort_reason is not None:
        _safe_close_suppressing(stream)
        raise PackageScrubError(abort_reason)

    _close_or_fail(stream)


# ----------------------------------------------------------------------
# Role resolution + cross-check
# ----------------------------------------------------------------------


def _resolve_and_account(
    record: _RelationshipRecord,
    role_type: str,
    canonical_names: set[str],
    override_map: dict[str, str],
    default_map: dict[str, str],
    seen: set[str],
    base_dir: str,
) -> str:
    """
    Резолвит Target одного relationship-record для заданной роли:
    External -> reject; escape/unsafe -> reject; несуществующая физическая
    часть -> reject; повторное использование ФИЗИЧЕСКОЙ части другой
    ролью/relationship (глобально по всему пакету, между worksheet
    включительно) -> reject; role/content-type mismatch -> reject.
    Возвращает canonical target и добавляет его в seen (учтён).
    """
    if record.mode == "External":
        raise PackageScrubError(PackageScrubReason.UNSUPPORTED_PACKAGE_CONTENT)

    canonical_target = _resolve_relationship_target(record.target, base_dir)
    if canonical_target is None:
        raise PackageScrubError(PackageScrubReason.UNSUPPORTED_PACKAGE_CONTENT)

    canonical_key = _canonical_member_name(canonical_target)
    if canonical_key not in canonical_names:
        raise PackageScrubError(PackageScrubReason.INVALID_INPUT_PACKAGE)
    if canonical_key in seen:
        raise PackageScrubError(PackageScrubReason.INVALID_INPUT_PACKAGE)

    expected_content_type = _ROLE_CONTENT_TYPE[role_type]
    effective_content_type = _effective_content_type(canonical_key, override_map, default_map)
    if effective_content_type != expected_content_type:
        raise PackageScrubError(PackageScrubReason.INVALID_INPUT_PACKAGE)

    seen.add(canonical_key)
    return canonical_key


# ----------------------------------------------------------------------
# Root relationships (потоково, bounded per-type counters)
# ----------------------------------------------------------------------


def _classify_root(
    zf: zipfile.ZipFile,
    canonical_names: set[str],
    override_map: dict[str, str],
    default_map: dict[str, str],
    seen: set[str],
) -> tuple[bool, bool, bool]:
    counts = {
        _REL_OFFICE_DOCUMENT: 0,
        _REL_CORE_PROPERTIES: 0,
        _REL_EXTENDED_PROPERTIES: 0,
        _REL_CUSTOM_PROPERTIES: 0,
    }

    def on_record(rid: str, rtype: str, target: str, mode: str) -> None:
        if rtype not in counts:
            raise PackageScrubError(PackageScrubReason.UNSUPPORTED_PACKAGE_CONTENT)
        counts[rtype] += 1
        if counts[rtype] > 1:
            raise PackageScrubError(PackageScrubReason.INVALID_INPUT_PACKAGE)
        record = _RelationshipRecord(id=rid, type=rtype, target=target, mode=mode)
        if rtype == _REL_OFFICE_DOCUMENT:
            workbook_target = _resolve_and_account(
                record, _REL_OFFICE_DOCUMENT, canonical_names, override_map, default_map, seen, base_dir=""
            )
            if workbook_target != _canonical_member_name(_WORKBOOK_PART):
                raise PackageScrubError(PackageScrubReason.INVALID_INPUT_PACKAGE)
        else:
            _resolve_and_account(record, rtype, canonical_names, override_map, default_map, seen, base_dir="")

    if _canonical_member_name(_ROOT_RELS_PART) not in canonical_names:
        raise PackageScrubError(PackageScrubReason.INVALID_INPUT_PACKAGE)
    _stream_relationships(zf, _ROOT_RELS_PART, on_record)

    if counts[_REL_OFFICE_DOCUMENT] != 1:
        raise PackageScrubError(PackageScrubReason.INVALID_INPUT_PACKAGE)

    return counts[_REL_CORE_PROPERTIES] == 1, counts[_REL_EXTENDED_PROPERTIES] == 1, counts[_REL_CUSTOM_PROPERTIES] == 1


# ----------------------------------------------------------------------
# Workbook relationships (потоково, bounded per-type counters +
# bounded worksheet-target dict, <= MAX_WORKSHEETS записей)
# ----------------------------------------------------------------------


def _classify_workbook(
    zf: zipfile.ZipFile,
    canonical_names: set[str],
    override_map: dict[str, str],
    default_map: dict[str, str],
    seen: set[str],
    needed_worksheet_rids: set[str],
) -> tuple[dict[str, str], bool, bool]:
    """
    Возвращает (worksheet_targets_by_rid, has_shared_strings, has_calc_chain).
    worksheet_targets_by_rid ограничен len(needed_worksheet_rids)
    (<= MAX_WORKSHEETS) записями — worksheet-relationships, чей Id НЕ
    входит в needed_worksheet_rids, немедленно отбрасываются, не
    накапливаясь (тот же принцип, что уже принят в
    app.scrub.preflight._read_workbook_relationships для needed_rids).
    """
    counts = {_REL_STYLES: 0, _REL_THEME: 0, _REL_SHARED_STRINGS: 0, _REL_CALC_CHAIN: 0}
    worksheet_targets: dict[str, str] = {}
    allowed_types = {_REL_WORKSHEET, _REL_STYLES, _REL_THEME, _REL_SHARED_STRINGS, _REL_CALC_CHAIN}

    def on_record(rid: str, rtype: str, target: str, mode: str) -> None:
        if rtype not in allowed_types:
            raise PackageScrubError(PackageScrubReason.UNSUPPORTED_PACKAGE_CONTENT)
        record = _RelationshipRecord(id=rid, type=rtype, target=target, mode=mode)
        if rtype == _REL_WORKSHEET:
            if rid not in needed_worksheet_rids:
                return
            target_resolved = _resolve_and_account(
                record, _REL_WORKSHEET, canonical_names, override_map, default_map, seen, base_dir="xl"
            )
            worksheet_targets[rid] = target_resolved
            return
        counts[rtype] += 1
        if counts[rtype] > 1:
            raise PackageScrubError(PackageScrubReason.INVALID_INPUT_PACKAGE)
        _resolve_and_account(record, rtype, canonical_names, override_map, default_map, seen, base_dir="xl")

    if _canonical_member_name(_WORKBOOK_RELS_PART) not in canonical_names:
        raise PackageScrubError(PackageScrubReason.INVALID_INPUT_PACKAGE)
    _stream_relationships(zf, _WORKBOOK_RELS_PART, on_record)

    if counts[_REL_STYLES] != 1:
        raise PackageScrubError(PackageScrubReason.INVALID_INPUT_PACKAGE)
    if counts[_REL_THEME] != 1:
        raise PackageScrubError(PackageScrubReason.INVALID_INPUT_PACKAGE)

    return worksheet_targets, counts[_REL_SHARED_STRINGS] == 1, counts[_REL_CALC_CHAIN] == 1


# ----------------------------------------------------------------------
# Worksheet relationships (потоково: hyperlink -- только счётчик, без
# _RelationshipRecord; comments/vmlDrawing -- максимум 1 резолюция
# каждая; table -- резолюция по одной штуке немедленно)
# ----------------------------------------------------------------------


def _classify_worksheet(
    ws_target: str,
    zf: zipfile.ZipFile,
    canonical_names: set[str],
    override_map: dict[str, str],
    default_map: dict[str, str],
    seen: set[str],
) -> tuple[int, int, int]:
    """
    Возвращает (comment_part_count_delta, table_part_count_delta,
    hyperlink_relationship_count_delta) для ОДНОГО worksheet. Если у
    worksheet нет собственного .rels — это валидный случай (нет
    hyperlinks/comments/tables/VML), возвращается (0, 0, 0).

    Память: hyperlink -- O(1) (только инкремент счётчика, Target
    никогда не читается/не резолвится, _RelationshipRecord для
    hyperlink никогда не создаётся); comments/vmlDrawing -- не более
    одной резолюции каждая (мгновенно, не накапливается); table --
    резолюция по одной записи сразу по получении, без промежуточного
    списка.
    """
    ws_rels_part = _rels_sibling_path(ws_target)
    ws_rels_canonical = _canonical_member_name(ws_rels_part)
    if ws_rels_canonical not in canonical_names:
        return 0, 0, 0
    seen.add(ws_rels_canonical)

    base_dir = ws_target.rsplit("/", 1)[0] if "/" in ws_target else ""
    allowed_types = {_REL_HYPERLINK, _REL_COMMENTS, _REL_VML_DRAWING, _REL_TABLE}
    state = {"hyperlink_count": 0, "comment_count": 0, "table_count": 0, "has_comments": False, "has_vml": False}

    def on_record(rid: str, rtype: str, target: str, mode: str) -> None:
        if rtype not in allowed_types:
            raise PackageScrubError(PackageScrubReason.UNSUPPORTED_PACKAGE_CONTENT)

        if rtype == _REL_HYPERLINK:
            # Frozen hyperlink contract: только count + classification,
            # Target никогда не резолвится/не читается (Independent
            # Security Review §19 — подтверждено, что игнорирование
            # Target не создаёт structural bypass благодаря
            # completeness-gate; изменять эту семантику нет причины).
            state["hyperlink_count"] += 1
            return

        record = _RelationshipRecord(id=rid, type=rtype, target=target, mode=mode)
        if rtype == _REL_COMMENTS:
            if state["has_comments"]:
                raise PackageScrubError(PackageScrubReason.INVALID_INPUT_PACKAGE)
            state["has_comments"] = True
            _resolve_and_account(record, _REL_COMMENTS, canonical_names, override_map, default_map, seen, base_dir)
            state["comment_count"] = 1
            return
        if rtype == _REL_VML_DRAWING:
            if state["has_vml"]:
                raise PackageScrubError(PackageScrubReason.INVALID_INPUT_PACKAGE)
            state["has_vml"] = True
            _resolve_and_account(
                record, _REL_VML_DRAWING, canonical_names, override_map, default_map, seen, base_dir
            )
            return
        if rtype == _REL_TABLE:
            _resolve_and_account(record, _REL_TABLE, canonical_names, override_map, default_map, seen, base_dir)
            state["table_count"] += 1
            return

    _stream_relationships(zf, ws_rels_part, on_record)

    # Structural discriminator (НЕ semantic proof VML payload — см.
    # Stage10C.2.2 Contract Correction §11): vmlDrawing допускается
    # ТОЛЬКО при co-occurrence с comments на ТОМ ЖЕ worksheet. Порядок
    # записей в файле не важен — проверка выполняется ПОСЛЕ полного
    # потокового прохода (drain-to-EOF уже гарантирован
    # _stream_relationships).
    if state["has_vml"] and not state["has_comments"]:
        raise PackageScrubError(PackageScrubReason.UNSUPPORTED_PACKAGE_CONTENT)

    return state["comment_count"], state["table_count"], state["hyperlink_count"]


def _verify_completeness(canonical_names: set[str], seen: set[str]) -> None:
    """
    Capstone-инвариант (§13/§26 контракта): каждый физический ZIP-член
    обязан быть учтён. Directory-маркеры (trailing "/") исключены — это
    не содержательные части. Что-либо оставшееся неучтённым — включая
    unexpected .rels-файлы в неожиданных местах — UNSUPPORTED_PACKAGE_CONTENT.
    """
    for name in canonical_names:
        if name in seen:
            continue
        if name.endswith("/"):
            continue
        raise PackageScrubError(PackageScrubReason.UNSUPPORTED_PACKAGE_CONTENT)


# ----------------------------------------------------------------------
# Public API
# ----------------------------------------------------------------------


def _inspect_package_policy(
    zf: zipfile.ZipFile, canonical_names: set[str]
) -> PackageInventory:
    override_map, default_map = _parse_content_types(zf, canonical_names)
    seen: set[str] = {_canonical_member_name(_CONTENT_TYPES_PART)}

    seen.add(_canonical_member_name(_ROOT_RELS_PART))
    has_core, has_app, has_custom = _classify_root(zf, canonical_names, override_map, default_map, seen)

    seen.add(_canonical_member_name(_WORKBOOK_PART))
    seen.add(_canonical_member_name(_WORKBOOK_RELS_PART))
    rids = _read_workbook_sheet_rids(zf, canonical_names)
    worksheet_targets_by_rid, has_shared_strings, has_calc_chain = _classify_workbook(
        zf, canonical_names, override_map, default_map, seen, set(rids)
    )

    worksheet_targets: list[str] = []
    for rid in rids:
        target = worksheet_targets_by_rid.get(rid)
        if target is None:
            raise PackageScrubError(PackageScrubReason.INVALID_INPUT_PACKAGE)
        worksheet_targets.append(target)

    comment_part_count = 0
    table_part_count = 0
    hyperlink_relationship_count = 0
    for ws_target in worksheet_targets:
        c, t, h = _classify_worksheet(ws_target, zf, canonical_names, override_map, default_map, seen)
        comment_part_count += c
        table_part_count += t
        hyperlink_relationship_count += h

    _verify_completeness(canonical_names, seen)

    return PackageInventory(
        has_shared_strings=has_shared_strings,
        has_calc_chain=has_calc_chain,
        comment_part_count=comment_part_count,
        table_part_count=table_part_count,
        hyperlink_relationship_count=hyperlink_relationship_count,
        has_core_properties=has_core,
        has_app_properties=has_app,
        has_custom_properties=has_custom,
    )


def _inspect_package_policy_after_preflight(path: _PathLike) -> PackageInventory:
    """
    Внутренний helper — предполагает, что preflight_xlsx_package(path)
    для ЭТОГО ЖЕ path уже был успешно вызван в рамках текущего вызова
    inspect_package_policy (см. её реализацию). Не вызывать отдельно.
    """
    candidate = Path(path)
    zf, _entry_count, canonical_names, _total_uncompressed = _cheap_zip_preflight(candidate)
    try:
        inventory = _inspect_package_policy(zf, canonical_names)
    except PackageScrubError:
        _safe_close_suppressing(zf)
        raise
    _close_or_fail(zf)
    return inventory


def inspect_package_policy(path: _PathLike) -> PackageInventory:
    """
    Package Policy / Inventory (Stage 10C.2.2) — единственная точка входа.

    Correction Pass (MINOR-1): эта функция ПРОГРАММНО (не только по
    docstring-конвенции) сначала вызывает полный
    app.scrub.preflight.preflight_xlsx_package(path) — весь Stage10C.2.1
    (Section A и Section B, включая MAX_NONEMPTY_CELLS) обязательно
    проверяется ПЕРВЫМ; package-policy инспекция выполняется только
    после его успешного завершения. Прямой вызов этой функции НЕ может
    случайно пропустить какой-либо resource-лимит Stage10C.2.1.

    Не вызывает openpyxl. Не извлекает содержимое ZIP на файловую
    систему. Не создаёт временных файлов. Не выполняет scrub/mutation.
    Результат не содержит confidential/структурных данных — см.
    PackageInventory.

    :raises TypeError: path не str/Path (поднимается preflight_xlsx_package).
    :raises PackageScrubError: см. PackageScrubReason.INVALID_INPUT_PACKAGE
        (malformed XML, дублирующиеся декларации, malformed relationship,
        отсутствующие обязательные поля/relationship, invalid TargetMode,
        orphan разрешённой роли, role/content-type mismatch, повторное
        использование физической части), PackageScrubReason.RESOURCE_LIMIT_EXCEEDED
        (любой из лимитов Stage10C.2.1, включая MAX_NONEMPTY_CELLS),
        PackageScrubReason.UNSUPPORTED_PACKAGE_CONTENT (запрещённая
        фича, неизвестная часть/Content-Type/relationship Type,
        non-hyperlink External, macro-enabled workbook, unexpected
        .rels, неучтённый физический ZIP-член).
    """
    preflight_xlsx_package(path)
    return _inspect_package_policy_after_preflight(path)
