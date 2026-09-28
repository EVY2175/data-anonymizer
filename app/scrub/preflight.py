"""
Stage 10C.2.1 — Resource + ZIP/package structural preflight.

Выполняется ДО какого-либо обращения к openpyxl. Задача этого модуля —
установить, что XLSX-пакет (уже прошедший Stage10C.1 + Stage8
anonymization) безопасен по объёму и структуре ZIP-контейнера, и
сосчитать нужные Stage10C.2.2 неконфиденциальные величины (число листов,
число непустых ячеек), не строя ни ZIP-дерево на диске, ни openpyxl
object model, ни DOM целого XML-документа (ни worksheet, ни workbook.xml,
ни workbook.xml.rels — см. Post-Review Correction Pass, MAJOR-1).

======================================================================
Два раздела (Stage 10C.2 Final Contract Freeze §4-6)
======================================================================

Раздел A — дешёвый ZIP preflight: работает исключительно с central-
directory метаданными (ZipInfo.file_size/compress_size/flag_bits и
именами записей) — ни один байт содержимого записи не распаковывается.

Раздел B — ограниченные семантические лимиты: выполняется только после
успешного прохождения раздела A. И xl/workbook.xml, и
xl/_rels/workbook.xml.rels читаются ТОЛЬКО потоково (ET.iterparse с
elem.clear() на каждом событии) — независимая security review (Stage
10C.2.1 Independent Security Review, MAJOR-1) эмпирически доказала, что
non-streaming ET.parse() на этих частях допускает ~150x memory
amplification в рамках уже замороженных лимитов (компактный файл на
диске мог давать сотни MiB DOM). Число непустых ячеек — потоковый разбор
(ET.iterparse) с ранним прерыванием сразу после превышения лимита.

======================================================================
OD-7
======================================================================

Ни одно исключение этого модуля не содержит: путь входного файла, имя
записи ZIP, содержимое XML, worksheet name, relationship target,
значение ячейки. Исключения нижних слоёв (os/zipfile/xml.etree) никогда
не пробрасываются как есть — они транслируются в PackageScrubError с
фиксированным сообщением, тем же приёмом (except-блок покидается ДО
подъёма нового исключения), что уже принят в app.workspace.workspace и
app.coverage.worksheet_policy.

Отдельно (Post-Review Correction Pass, MINOR-1): закрытие ZipFile/потока
само по себе никогда не может протечь raw-исключение и не может ни
заменить уже решённую первичную ошибку, ни выдать "успешный" результат,
если закрытие неожиданно провалилось после успешной обработки — см.
_safe_close_suppressing/_close_or_fail.

======================================================================
XML safety
======================================================================

Используется только стандартный xml.etree.ElementTree (pinned Python
3.13 — встроенный expat не разрешает внешние сущности/DTD по умолчанию;
billion-laughs и XXE эмпирически проверены и блокируются на уровне
парсера, см. Independent Security Review §8). Сторонний XML-парсер не
вводится. Raw XML никогда не логируется и не включается в исключения.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path
from typing import IO, Optional, Union

from app.scrub.errors import PackageScrubError, PackageScrubReason
from app.scrub.models import PackagePreflightResult

_PathLike = Union[str, Path]

# ----------------------------------------------------------------------
# Frozen limits (Stage10C Final Contract Freeze, OD-10C-7) — единственное
# место в проекте, где эти константы объявлены для Stage10C.2.
# ----------------------------------------------------------------------

MAX_XLSX_SIZE_BYTES = 50 * 1024 * 1024
MAX_WORKSHEETS = 200
MAX_NONEMPTY_CELLS = 2_000_000
MAX_ZIP_ENTRIES = 10_000
MAX_TOTAL_UNCOMPRESSED_BYTES = 500 * 1024 * 1024
MAX_COMPRESSION_RATIO = 100

_SUPPORTED_EXTENSION = ".xlsx"

# OOXML namespaces, используемые для потокового/ограниченного XML-разбора.
_SML_NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
_R_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
_PKG_REL_NS = "http://schemas.openxmlformats.org/package/2006/relationships"

_TAG_SHEET = f"{{{_SML_NS}}}sheet"
_TAG_SHEETS = f"{{{_SML_NS}}}sheets"
_TAG_ROW = f"{{{_SML_NS}}}row"
_TAG_CELL = f"{{{_SML_NS}}}c"
_TAG_CELL_VALUE = f"{{{_SML_NS}}}v"
_TAG_CELL_FORMULA = f"{{{_SML_NS}}}f"
_TAG_CELL_INLINE_STR = f"{{{_SML_NS}}}is"
_ATTR_R_ID = f"{{{_R_NS}}}id"
_TAG_RELATIONSHIP = f"{{{_PKG_REL_NS}}}Relationship"

_WORKBOOK_PART = "xl/workbook.xml"
_WORKBOOK_RELS_PART = "xl/_rels/workbook.xml.rels"

# Зарезервированные Windows device-имена (case-insensitive, сравниваются с
# basename сегмента ДО первой точки) — Stage10C.2.1 Post-Review Correction
# Pass, MINOR-3 (hardening на будущее: сам этот substage ничего не
# извлекает на файловую систему, но это единственный member-name safety
# gate, который может быть унаследован последующими substages).
_WINDOWS_RESERVED_BASENAMES = frozenset(
    {
        "con",
        "prn",
        "aux",
        "nul",
        "com1", "com2", "com3", "com4", "com5", "com6", "com7", "com8", "com9",
        "lpt1", "lpt2", "lpt3", "lpt4", "lpt5", "lpt6", "lpt7", "lpt8", "lpt9",
    }
)


# ----------------------------------------------------------------------
# Раздел A: package name safety (pure helpers, без файлового I/O)
# ----------------------------------------------------------------------


def _is_safe_member_name(name: str) -> bool:
    """
    Проверяет ОДНО имя записи ZIP на безопасность: без NUL, без обратного
    слэша, без двоеточия (закрывает "C:/...", "C:...", NTFS ADS-подобный
    синтаксис), без ведущего "/", без сегментов ".."/"."/пустых
    (внутренний "//"). Trailing "/" (маркер директории) сознательно не
    считается пустым внутренним сегментом.

    Дополнительно (MINOR-3 hardening) каждый сегмент проверяется на:
    - зарезервированное Windows device-имя (CON/PRN/AUX/NUL/COM1-9/LPT1-9,
      case-insensitive, сравнение по basename до первой точки);
    - завершение пробелом или точкой (Windows path normalization
      aliasing — "name." и "name " на Windows фактически совпадают с
      "name").
    """
    if not name:
        return False
    if "\x00" in name:
        return False
    if "\\" in name:
        return False
    if ":" in name:
        return False
    if name.startswith("/"):
        return False
    body = name[:-1] if name.endswith("/") else name
    if not body:
        return False
    for segment in body.split("/"):
        if segment in ("", ".", ".."):
            return False
        if segment.endswith(" ") or segment.endswith("."):
            return False
        basename = segment.split(".", 1)[0].casefold()
        if basename in _WINDOWS_RESERVED_BASENAMES:
            return False
    return True


def _canonical_member_name(name: str) -> str:
    """
    Каноническая форма для collision-детекции: разделитель "/", регистро-
    независимое сравнение (casefold). Вызывается ТОЛЬКО после
    _is_safe_member_name(name) is True.
    """
    return name.casefold()


def _resolve_relationship_target(target: str, base_dir: str) -> Optional[str]:
    """
    Разрешает OPC-relationship Target в canonical package-путь (без
    ведущего "/"), с учётом абсолютных ("/...") и относительных targets
    (относительно base_dir — директории источника relationship-файла).
    Возвращает None, если target пытается выйти за пределы пакета через
    ".." (небезопасный relationship target).
    """
    if target.startswith("/"):
        candidate = target[1:]
    else:
        combined = f"{base_dir}/{target}" if base_dir else target
        parts: list[str] = []
        for segment in combined.split("/"):
            if segment in ("", "."):
                continue
            if segment == "..":
                if not parts:
                    return None
                parts.pop()
            else:
                parts.append(segment)
        candidate = "/".join(parts)
    if not _is_safe_member_name(candidate):
        return None
    return candidate


# ----------------------------------------------------------------------
# Безопасное закрытие потоков/ZipFile (Post-Review Correction Pass,
# MINOR-1). Два разных сценария намеренно разделены:
#
# _safe_close_suppressing — используется, когда УЖЕ есть решённая
# первичная ошибка: сбой close() не должен её заменить или зачейнить —
# подавляется полностью.
#
# _close_or_fail — используется на УСПЕШНОМ пути: если close()
# неожиданно проваливается, результат не может считаться безопасно
# завершённым без анализа — поднимается новый sanitized
# PackageScrubError вместо молчаливого возврата "успеха".
# ----------------------------------------------------------------------


def _safe_close_suppressing(closeable) -> None:
    try:
        closeable.close()
    except Exception:
        pass


def _close_or_fail(closeable) -> None:
    close_failed = False
    try:
        closeable.close()
    except Exception:
        close_failed = True
    if close_failed:
        raise PackageScrubError(PackageScrubReason.INVALID_INPUT_PACKAGE)


# ----------------------------------------------------------------------
# Раздел A: дешёвый ZIP preflight
# ----------------------------------------------------------------------


def _validate_basic_file(path: Path) -> None:
    if not path.exists() or not path.is_file():
        raise PackageScrubError(PackageScrubReason.INVALID_INPUT_PACKAGE)

    stat_failed = False
    size = -1
    try:
        size = path.stat().st_size
    except OSError:
        stat_failed = True
    if stat_failed:
        raise PackageScrubError(PackageScrubReason.INVALID_INPUT_PACKAGE)

    if size > MAX_XLSX_SIZE_BYTES:
        raise PackageScrubError(PackageScrubReason.RESOURCE_LIMIT_EXCEEDED)


def _open_zip(path: Path) -> zipfile.ZipFile:
    open_failed = False
    zf: Optional[zipfile.ZipFile] = None
    try:
        zf = zipfile.ZipFile(path, "r")
    except Exception:
        open_failed = True
    if open_failed:
        raise PackageScrubError(PackageScrubReason.INVALID_INPUT_PACKAGE)
    return zf  # type: ignore[return-value]


def _validate_zip_structure_and_collect_names(
    infos: list[zipfile.ZipInfo],
) -> tuple[set[str], int]:
    """
    Раздел A, шаги 7-12: работает исключительно с уже полученным
    infolist() (central directory) — ни один байт содержимого записи не
    читается. Возвращает (canonical-имена, суммарный заявленный
    uncompressed-размер) — вычисляется один раз, переиспользуется
    вызывающим кодом без повторного обхода infolist().
    """
    if len(infos) > MAX_ZIP_ENTRIES:
        raise PackageScrubError(PackageScrubReason.RESOURCE_LIMIT_EXCEEDED)

    total_uncompressed = 0
    seen_canonical_names: set[str] = set()

    for info in infos:
        total_uncompressed += info.file_size

        ratio = info.file_size / max(info.compress_size, 1)
        if ratio > MAX_COMPRESSION_RATIO:
            raise PackageScrubError(PackageScrubReason.RESOURCE_LIMIT_EXCEEDED)

        if info.flag_bits & 0x1:
            raise PackageScrubError(PackageScrubReason.INVALID_INPUT_PACKAGE)

        if not _is_safe_member_name(info.filename):
            raise PackageScrubError(PackageScrubReason.UNSUPPORTED_PACKAGE_CONTENT)

        canonical = _canonical_member_name(info.filename)
        if canonical in seen_canonical_names:
            raise PackageScrubError(PackageScrubReason.UNSUPPORTED_PACKAGE_CONTENT)
        seen_canonical_names.add(canonical)

    if total_uncompressed > MAX_TOTAL_UNCOMPRESSED_BYTES:
        raise PackageScrubError(PackageScrubReason.RESOURCE_LIMIT_EXCEEDED)

    return seen_canonical_names, total_uncompressed


def _cheap_zip_preflight(path: Path) -> tuple[zipfile.ZipFile, int, set[str], int]:
    """
    Выполняет весь раздел A по порядку и возвращает открытый ZipFile
    (владение переходит вызывающему коду — обязан закрыть), число
    записей, множество canonical-имён и суммарный uncompressed-размер —
    infolist() вызывается ровно один раз за весь preflight.
    """
    _validate_basic_file(path)
    zf = _open_zip(path)

    infolist_failed = False
    infos: list[zipfile.ZipInfo] = []
    try:
        infos = zf.infolist()
    except Exception:
        infolist_failed = True
    if infolist_failed:
        _safe_close_suppressing(zf)
        raise PackageScrubError(PackageScrubReason.INVALID_INPUT_PACKAGE)

    try:
        canonical_names, total_uncompressed = _validate_zip_structure_and_collect_names(infos)
    except PackageScrubError:
        _safe_close_suppressing(zf)
        raise

    return zf, len(infos), canonical_names, total_uncompressed


# ----------------------------------------------------------------------
# Раздел B: bounded/streaming XML-чтение
# ----------------------------------------------------------------------


def _open_member_stream(zf: zipfile.ZipFile, name: str) -> IO[bytes]:
    open_failed = False
    stream: Optional[IO[bytes]] = None
    try:
        stream = zf.open(name, "r")
    except Exception:
        open_failed = True
    if open_failed:
        raise PackageScrubError(PackageScrubReason.INVALID_INPUT_PACKAGE)
    return stream  # type: ignore[return-value]


def _read_workbook_sheet_rids(zf: zipfile.ZipFile, canonical_names: set[str]) -> list[str]:
    """
    Потоково читает xl/workbook.xml через ET.iterparse — полный DOM НЕ
    строится (Post-Review Correction Pass, MAJOR-1). Каждый элемент
    очищается (elem.clear()) сразу на своём "end"-событии, независимо от
    тега — иначе накопление сработало бы даже без полного ET.parse,
    если бы «мусорные» элементы ДО <sheets> не освобождались.

    Возвращает список r:id — по одному на каждый <sheet> внутри
    <sheets>. После закрывающего </sheets> сбор r:id прекращается, но
    поток ПРОДОЛЖАЕТ дочитываться до EOF (Post-Correction Independent
    Re-Review, финальная MINOR-находка) — иначе malformed XML строго
    ПОСЛЕ корректно закрытого </sheets> (defined names, calcPr и т.п.)
    остался бы незамеченным, и структурно невалидный документ прошёл бы
    preflight как валидный. elem.clear() применяется универсально на
    каждом "end"-событии независимо от тега — память остаётся
    ограниченной, дочитывание стоит только CPU-времени, не памяти.

    Early-abort (сохранён, авторизованное resource rejection): как
    только число уже принятых <sheet> достигает MAX_WORKSHEETS,
    СЛЕДУЮЩИЙ <sheet> немедленно прерывает разбор с
    RESOURCE_LIMIT_EXCEEDED — документ с миллионами <sheet> не
    дочитывается (в отличие от нормального случая, где decision уже
    принят и дочитывать XML до EOF не требуется).

    Fail-closed (MINOR-2): <sheet> без r:id или с r:id, УЖЕ встреченным
    ранее среди <sheet> — структурная неоднозначность workbook, не
    resource-лимит; поднимается INVALID_INPUT_PACKAGE немедленно (решение
    уже принято, дочитывать остаток не требуется), никакого молчаливого
    dedupe.
    """
    if _canonical_member_name(_WORKBOOK_PART) not in canonical_names:
        raise PackageScrubError(PackageScrubReason.INVALID_INPUT_PACKAGE)

    stream = _open_member_stream(zf, _WORKBOOK_PART)
    rids: list[str] = []
    seen_rids: set[str] = set()
    in_sheets = False
    found_sheets_container = False
    abort_reason: Optional[PackageScrubReason] = None
    parse_failed = False
    try:
        for event, elem in ET.iterparse(stream, events=("start", "end")):
            if event == "start":
                if elem.tag == _TAG_SHEETS:
                    in_sheets = True
                    found_sheets_container = True
                continue

            tag = elem.tag
            if tag == _TAG_SHEETS:
                # <sheets> закрылся корректно — сбор r:id завершён, но
                # поток НЕ прерывается: остаток workbook.xml (defined
                # names, calcPr и т.п.) обязан быть дочитан до EOF, чтобы
                # malformed XML в хвосте не остался незамеченным.
                in_sheets = False
                elem.clear()
                continue
            if tag == _TAG_SHEET and in_sheets:
                if len(rids) >= MAX_WORKSHEETS:
                    abort_reason = PackageScrubReason.RESOURCE_LIMIT_EXCEEDED
                    elem.clear()
                    break
                rid = elem.get(_ATTR_R_ID)
                if not rid or rid in seen_rids:
                    abort_reason = PackageScrubReason.INVALID_INPUT_PACKAGE
                    elem.clear()
                    break
                seen_rids.add(rid)
                rids.append(rid)
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
    if not found_sheets_container:
        _safe_close_suppressing(stream)
        raise PackageScrubError(PackageScrubReason.INVALID_INPUT_PACKAGE)

    _close_or_fail(stream)
    return rids


def _read_workbook_relationships(
    zf: zipfile.ZipFile, canonical_names: set[str], needed_rids: set[str]
) -> dict[str, tuple[str, Optional[str]]]:
    """
    Потоково читает xl/_rels/workbook.xml.rels через ET.iterparse.
    Сохраняет ТОЛЬКО relationship-записи, чей Id входит в needed_rids
    (полученный из уже ограниченного и дедуплицированного списка sheet
    r:id — не более MAX_WORKSHEETS штук) — остальные relationship-записи
    (docProps/styles/theme/...) немедленно отбрасываются (elem.clear()),
    без накопления в памяти независимо от их числа или размера.

    Документ дочитывается ПОЛНОСТЬЮ, без досрочного выхода даже после
    того, как все needed_rids уже найдены — иначе повторное определение
    уже нужного Id, встречающееся ПОЗЖЕ в файле, было бы пропущено.
    Security correctness важнее микрооптимизации (Post-Review Correction
    Pass §6). Полный DOM при этом не строится — CPU-время линейно
    зависит от размера файла, память — нет.

    Fail-closed: повторное определение УЖЕ нужного Id → INVALID_INPUT_PACKAGE
    немедленно (остальной документ можно не дочитывать — решение уже
    принято).
    """
    if _canonical_member_name(_WORKBOOK_RELS_PART) not in canonical_names:
        raise PackageScrubError(PackageScrubReason.INVALID_INPUT_PACKAGE)

    stream = _open_member_stream(zf, _WORKBOOK_RELS_PART)
    relationships: dict[str, tuple[str, Optional[str]]] = {}
    abort_reason: Optional[PackageScrubReason] = None
    parse_failed = False
    try:
        for _event, elem in ET.iterparse(stream, events=("end",)):
            tag = elem.tag
            if tag != _TAG_RELATIONSHIP:
                elem.clear()
                continue

            rid = elem.get("Id")
            if rid not in needed_rids:
                elem.clear()
                continue

            target = elem.get("Target")
            mode = elem.get("TargetMode")
            if target is None:
                abort_reason = PackageScrubReason.INVALID_INPUT_PACKAGE
                elem.clear()
                break
            if rid in relationships:
                abort_reason = PackageScrubReason.INVALID_INPUT_PACKAGE
                elem.clear()
                break
            relationships[rid] = (target, mode)
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
    return relationships


def _resolve_sheet_part_names(
    rids: list[str],
    relationships: dict[str, tuple[str, Optional[str]]],
    canonical_names: set[str],
) -> list[str]:
    """
    Для каждого r:id из <sheets> разрешает физическое имя части.
    Relationship отсутствует / TargetMode="External" / target выходит за
    пределы пакета / разрешённая часть физически отсутствует в пакете —
    во всех случаях структурная невалидность/недопустимая политика.

    MINOR-2 (fail-closed, не dedupe): если ДВА разных r:id (rids уже
    гарантированно уникальны — см. _read_workbook_sheet_rids) разрешаются
    в ОДНУ и ту же каноническую физическую часть — это структурная
    неоднозначность workbook (какой лист «настоящий»?), а не законный
    повторный подсчёт — INVALID_INPUT_PACKAGE, часть не считается дважды.
    """
    resolved: list[str] = []
    seen_canonical_targets: set[str] = set()
    for rid in rids:
        entry = relationships.get(rid)
        if entry is None:
            raise PackageScrubError(PackageScrubReason.INVALID_INPUT_PACKAGE)
        target, mode = entry
        if mode == "External":
            raise PackageScrubError(PackageScrubReason.UNSUPPORTED_PACKAGE_CONTENT)

        base_dir = _WORKBOOK_PART.rsplit("/", 1)[0] if "/" in _WORKBOOK_PART else ""
        canonical_target = _resolve_relationship_target(target, base_dir)
        if canonical_target is None:
            raise PackageScrubError(PackageScrubReason.UNSUPPORTED_PACKAGE_CONTENT)

        canonical_key = _canonical_member_name(canonical_target)
        if canonical_key not in canonical_names:
            raise PackageScrubError(PackageScrubReason.INVALID_INPUT_PACKAGE)

        if canonical_key in seen_canonical_targets:
            raise PackageScrubError(PackageScrubReason.INVALID_INPUT_PACKAGE)
        seen_canonical_targets.add(canonical_key)

        resolved.append(canonical_target)
    return resolved


def _is_nonempty_cell_element(cell_elem: ET.Element) -> bool:
    """
    Ячейка считается непустой, если у неё есть дочерний <v> (значение,
    включая ссылку на sharedStrings по индексу), <f> (формула) или <is>
    (inline string) — покрывает все OOXML-варианты (числовое/булево/
    ошибочное значение, shared-string reference, inlineStr, formula
    cell). Styled-but-empty <c s="N"/> и полностью пустой <c/> не имеют
    ни одного из этих детей и корректно считаются пустыми.
    """
    return (
        cell_elem.find(_TAG_CELL_VALUE) is not None
        or cell_elem.find(_TAG_CELL_FORMULA) is not None
        or cell_elem.find(_TAG_CELL_INLINE_STR) is not None
    )


def _count_nonempty_cells_in_part(
    zf: zipfile.ZipFile, part_name: str, limit: int
) -> int:
    """
    Потоковый подсчёт непустых ячеек одной worksheet-части. Использует
    ET.iterparse поверх ZipFile.open(...) (не ZipFile.read — во избежание
    загрузки всего содержимого части в память сразу). DOM целиком не
    строится: обработанные <c> и <row> элементы очищаются сразу после
    использования. Прерывается немедленно, как только счётчик превышает
    limit — остаток части не дочитывается.
    """
    stream = _open_member_stream(zf, part_name)
    count = 0
    parse_failed = False
    try:
        for _event, elem in ET.iterparse(stream, events=("end",)):
            tag = elem.tag
            if tag == _TAG_CELL:
                if _is_nonempty_cell_element(elem):
                    count += 1
                elem.clear()
                if count > limit:
                    break
            elif tag == _TAG_ROW:
                elem.clear()
    except ET.ParseError:
        parse_failed = True

    if parse_failed:
        _safe_close_suppressing(stream)
        raise PackageScrubError(PackageScrubReason.INVALID_INPUT_PACKAGE)

    _close_or_fail(stream)
    return count


def _semantic_preflight(
    zf: zipfile.ZipFile, canonical_names: set[str]
) -> tuple[int, int]:
    """
    Раздел B целиком: возвращает (sheet_count, nonempty_cell_count).
    Лимит MAX_WORKSHEETS уже принудительно обеспечен внутри
    _read_workbook_sheet_rids (early-abort) — при успешном возврате
    len(rids) <= MAX_WORKSHEETS гарантировано, повторная проверка здесь
    не требуется.
    """
    rids = _read_workbook_sheet_rids(zf, canonical_names)
    sheet_count = len(rids)

    relationships = _read_workbook_relationships(zf, canonical_names, set(rids))
    sheet_parts = _resolve_sheet_part_names(rids, relationships, canonical_names)

    total_nonempty = 0
    for part_name in sheet_parts:
        remaining_budget = MAX_NONEMPTY_CELLS - total_nonempty
        # remaining_budget уже <= 0 только если total_nonempty > MAX (что
        # само по себе уже должно было прервать цикл на предыдущей
        # итерации) — оставлено как defensive lower bound, не 0.
        part_count = _count_nonempty_cells_in_part(zf, part_name, max(remaining_budget, 0))
        total_nonempty += part_count
        if total_nonempty > MAX_NONEMPTY_CELLS:
            raise PackageScrubError(PackageScrubReason.RESOURCE_LIMIT_EXCEEDED)

    return sheet_count, total_nonempty


# ----------------------------------------------------------------------
# Public API
# ----------------------------------------------------------------------


def preflight_xlsx_package(path: _PathLike) -> PackagePreflightResult:
    """
    Resource + ZIP/package structural preflight (Stage 10C.2.1) —
    единственная точка входа. Не вызывает openpyxl.load_workbook. Не
    извлекает содержимое ZIP на файловую систему. Не создаёт временных
    файлов. Результат не содержит confidential/структурных данных — см.
    PackagePreflightResult.

    :raises TypeError: path не str/Path.
    :raises PackageScrubError: см. PackageScrubReason.INVALID_INPUT_PACKAGE
        (неверное расширение, не ZIP, повреждённый/зашифрованный пакет,
        небезопасные имена записей, структурно неразрешимые relationships,
        дублирующийся/неоднозначный r:id или Target, неожиданный сбой
        закрытия потока после успешной обработки),
        PackageScrubReason.RESOURCE_LIMIT_EXCEEDED (любой из лимитов
        OD-10C-7 превышен), PackageScrubReason.UNSUPPORTED_PACKAGE_CONTENT
        (дублирующиеся/коллизирующие имена записей, relationship с
        TargetMode="External" в роли источника листа, relationship target,
        выходящий за пределы пакета).
    """
    if not isinstance(path, (str, Path)):
        raise TypeError(f"path должен быть str или Path, получено: {type(path)!r}")

    candidate = Path(path)
    if candidate.suffix.lower() != _SUPPORTED_EXTENSION:
        raise PackageScrubError(PackageScrubReason.INVALID_INPUT_PACKAGE)

    zf, zip_entry_count, canonical_names, total_uncompressed = _cheap_zip_preflight(candidate)
    try:
        sheet_count, nonempty_cell_count = _semantic_preflight(zf, canonical_names)
    except PackageScrubError:
        _safe_close_suppressing(zf)
        raise
    _close_or_fail(zf)

    return PackagePreflightResult(
        zip_entry_count=zip_entry_count,
        total_uncompressed_bytes=total_uncompressed,
        sheet_count=sheet_count,
        nonempty_cell_count=nonempty_cell_count,
    )
