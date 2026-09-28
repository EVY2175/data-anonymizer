"""
Stage 10C.1 — worksheet-level policy, coverage validation, column KEEP
safety matrix, cross-type identifier detector, identifier expected-count
evidence (Step A) и REMOVE_ALL application.

======================================================================
Границы ответственности Stage 10C.1
======================================================================

Этот модуль отвечает на вопросы, которые Stage 8 сознательно НЕ решает:

    - должен ли данный лист вообще участвовать в анонимизации, и если да
      — обрабатываться (PROCESS) или быть физически исключён из внешнего
      артефакта (REMOVE_ALL) целиком, вместе со всеми sheet-scoped
      метаданными (comments/hyperlinks/validations/defined names/tables);
    - соответствует ли объявленный FieldRule.column_name фактическому
      заголовку колонки (Stage 8 никогда не сверяет column_name с
      реальным заголовком — это только человекочитаемые метаданные);
    - допустимо ли сочетание FieldType/Action.KEEP (Stage 8 сам по себе
      не запрещает KEEP ни для одного FieldType — решение "какие типы
      могут быть KEEP" целиком принадлежит Stage 10C);
    - не скрывает ли KEEP-колонка (любого допустимого типа) фактическое
      значение, структурно являющееся ИНН/КПП/ОГРН/ОГРНИП;
    - сколько непустых identifier-значений в PSEUDONYMIZE-колонках должно
      быть обработано Stage 8 (Step A identifier-processing-proof; Step B
      — сверка с фактическим output/store — реализуется в Stage 10C.4, не
      здесь).

Column-level coverage ВНУТРИ уже выбранного PROCESS-листа (что rules
обязаны покрывать ровно те колонки, что есть в заголовке — ни одной
пропущенной, ни одной лишней) НЕ дублируется здесь: это исключительная,
уже существующая и уже fail-closed ответственность приватной
app.anonymizer._validate_rules, которая естественно сработает, когда
Stage 10C.4 фактически вызовет anonymize_workbook/anonymize_flat_table —
до какой-либо мутации сторов.

======================================================================
OD-7
======================================================================

Ни одно исключение этого модуля не содержит: имя листа, FieldRule.
column_name или фактическое значение заголовка, содержимое ячеек, raw
identifier/company/person значение, пароль, содержимое расшифрованных
сторов. Вместо имени листа используется worksheet_index (1-based позиция
в workbook.worksheets ПОСЛЕ применения REMOVE_ALL — то есть уже финальный
индекс, который сохранит Stage 8 Writer в своём выводе). Исключения
нижних слоёв (openpyxl/OS при мутации/сохранении workbook) никогда не
пробрасываются как есть — они транслируются в CoverageError с фиксированным
сообщением, тем же приёмом, что уже принят в app.workspace.workspace
(except-блок покидается ДО подъёма нового исключения, чтобы __context__
нового исключения не указывал на перехваченное).

======================================================================
Single-pass сканирование
======================================================================

Для каждого PROCESS-листа формула-детекция, cross-type identifier
detector и identifier expected-count evidence вычисляются за ОДИН проход
по worksheet._cells (тот же приватный API и та же openpyxl-версия
3.1.5, уже запинённая и протестированная Stage 10A, app.safety.
external_ai) — без повторных полных сканирований книги. Header cross-check
выполняется отдельным, дешёвым (O(число объявленных правил колонки))
проходом по column_rules, а не полагается на то, что заголовочная ячейка
обязательно присутствует в разреженном словаре _cells.
"""

from __future__ import annotations

import contextlib
import os
import tempfile
from collections.abc import Mapping
from pathlib import Path
from typing import Iterator, Union

import openpyxl
from openpyxl.cell.cell import MergedCell
from openpyxl.worksheet.worksheet import Worksheet

from app.coverage.errors import ColumnSafetyError, ColumnSafetyReason, CoverageError, CoverageReason
from app.coverage.models import (
    IdentifierColumnEvidence,
    WorksheetPolicy,
    WorksheetPreparationResult,
    WorksheetScanResult,
)
from app.detectors import is_valid_inn, is_valid_kpp, is_valid_ogrn, is_valid_ogrnip
from app.models.identifiers import IdentifierType
from app.models.rules import Action, FieldRule, FieldType

_PathLike = Union[str, Path]

# ----------------------------------------------------------------------
# Column action matrix (frozen, Stage 10C Final Contract Freeze §4) —
# единственное место в проекте, где эта матрица объявлена.
# ----------------------------------------------------------------------

_KEEP_FORBIDDEN_TYPES = frozenset(
    {
        FieldType.COMPANY,
        FieldType.PERSON,
        FieldType.PHONE,
        FieldType.EMAIL,
        FieldType.ADDRESS,
        FieldType.UNKNOWN,
    }
)

# FieldType, для которых KEEP допустим, но обязан пройти cross-type
# identifier detector (для INN/KPP/OGRN это одновременно и есть
# независимое доказательство пустоты, требуемое OD-10C-2 — см. docstring
# _is_identifier_like ниже).
_KEEP_REQUIRES_DETECTOR_SCAN = frozenset(
    {
        FieldType.INN,
        FieldType.KPP,
        FieldType.OGRN,
        FieldType.BRANCH,
        FieldType.DEPARTMENT,
        FieldType.FINANCIAL,
    }
)

# Ровно то же множество, что anonymizer._PSEUDONYMIZABLE_FIELD_TYPES |
# anonymizer._IDENTIFIER_FIELD_TYPES (обе структуры приватны в app.
# anonymizer и не импортируются напрямую) — объявлено независимо здесь как
# defense-in-depth ранняя Stage10C-специфичная проверка, не подмена
# Stage8 anonymizer._validate_pseudonymization_targets.
_PSEUDONYMIZE_SUPPORTED_TYPES = frozenset(
    {FieldType.COMPANY, FieldType.PERSON, FieldType.INN, FieldType.KPP, FieldType.OGRN}
)

_IDENTIFIER_FIELD_TYPES: dict[FieldType, IdentifierType] = {
    FieldType.INN: IdentifierType.INN,
    FieldType.KPP: IdentifierType.KPP,
    FieldType.OGRN: IdentifierType.OGRN,
}


def _is_identifier_like(value: object) -> bool:
    """
    Cross-type identifier detector (Stage 10C Final Contract Freeze §14):
    любая KEEP-колонка допустимого типа обязана пройти этот скан.
    detect_value_type НЕ используется — он сознательно исключает КПП из
    автоматической детекции по значению (см. app.detectors docstring);
    здесь нужны все четыре Stage 5 public-валидатора отдельно. value
    передаётся как есть, без самодельной конверсии типов — вся
    type-policy (bool/float отвергаются, str/int допускаются) уже
    реализована внутри самих валидаторов.
    """
    return is_valid_inn(value) or is_valid_ogrn(value) or is_valid_ogrnip(value) or is_valid_kpp(value)


def _is_empty_identifier_value(value: object) -> bool:
    """
    Тот же predicate "пусто", что app.anonymizer._pseudonymize_identifier_cell
    использует для решения "пропустить ячейку или обработать" — byte-
    identical, чтобы Step A expected_count совпадал с реальной популяцией
    ячеек, которую увидит Stage 8.
    """
    return value is None or (isinstance(value, str) and value.strip() == "")


# ----------------------------------------------------------------------
# Валидация формы входных аргументов
# ----------------------------------------------------------------------


def _validate_worksheet_policies_type(worksheet_policies: object) -> None:
    if not isinstance(worksheet_policies, Mapping):
        raise CoverageError(CoverageReason.INVALID_POLICY_VALUE)
    for name, policy in worksheet_policies.items():
        if not isinstance(name, str) or not name:
            raise CoverageError(CoverageReason.INVALID_POLICY_VALUE)
        if not isinstance(policy, WorksheetPolicy):
            raise CoverageError(CoverageReason.INVALID_POLICY_VALUE)


def _validate_rules_by_sheet_type(rules_by_sheet: object) -> None:
    if not isinstance(rules_by_sheet, Mapping):
        raise CoverageError(CoverageReason.INVALID_POLICY_VALUE)
    for sheet_name, rules in rules_by_sheet.items():
        if not isinstance(sheet_name, str) or not sheet_name:
            raise CoverageError(CoverageReason.INVALID_POLICY_VALUE)
        if not isinstance(rules, Mapping):
            raise CoverageError(CoverageReason.INVALID_POLICY_VALUE)
        for column, rule in rules.items():
            if isinstance(column, bool) or not isinstance(column, int) or column < 1:
                raise CoverageError(CoverageReason.INVALID_POLICY_VALUE)
            if not isinstance(rule, FieldRule):
                raise CoverageError(CoverageReason.INVALID_POLICY_VALUE)


# ----------------------------------------------------------------------
# Public API: worksheet coverage
# ----------------------------------------------------------------------


def validate_worksheet_coverage(
    workbook: openpyxl.Workbook, worksheet_policies: Mapping[str, WorksheetPolicy]
) -> None:
    """
    Каждый реальный лист workbook (workbook.worksheets — chartsheets сюда
    не входят, их классификация/отклонение — frozen scope Stage 10C.2, не
    решается здесь) обязан иметь ровно одну запись в worksheet_policies;
    каждая запись обязана ссылаться на реально существующий лист; хотя бы
    один лист обязан быть PROCESS. Hidden/veryHidden листы подчиняются
    тем же правилам без исключений (sheet_state не влияет на
    обязательность policy как таковой).

    Дополнительно (закрытие MAJOR-1 независимого review): среди итогового
    набора PROCESS-листов — определяемого ЗДЕСЬ, до какой-либо мутации
    workbook, в том числе до _apply_remove_all — обязан быть хотя бы один
    лист с sheet_state == "visible". Если PROCESS-листы существуют, но все
    они hidden/veryHidden, результирующий workbook после REMOVE_ALL не
    может быть сохранён openpyxl в принципе (openpyxl поднимает
    ValueError/IndexError при попытке сохранить workbook без единого
    видимого листа) — эта проверка ловит такую конфигурацию заранее,
    HARD FAIL до мутации, а не постфактум через generic save-failure.
    Ни sheet_state, ни состав листов здесь не меняются — только читаются.
    """
    _validate_worksheet_policies_type(worksheet_policies)

    actual_names = {worksheet.title for worksheet in workbook.worksheets}
    policy_names = set(worksheet_policies.keys())

    if actual_names - policy_names:
        raise CoverageError(CoverageReason.MISSING_WORKSHEET_POLICY)

    if policy_names - actual_names:
        raise CoverageError(CoverageReason.POLICY_FOR_UNKNOWN_WORKSHEET)

    if not any(policy is WorksheetPolicy.PROCESS for policy in worksheet_policies.values()):
        raise CoverageError(CoverageReason.NO_PROCESS_WORKSHEET_REMAINS)

    process_worksheets = [
        worksheet
        for worksheet in workbook.worksheets
        if worksheet_policies[worksheet.title] is WorksheetPolicy.PROCESS
    ]
    if not any(worksheet.sheet_state == "visible" for worksheet in process_worksheets):
        raise CoverageError(CoverageReason.NO_VISIBLE_PROCESS_WORKSHEET)


def validate_process_rules(
    worksheet_policies: Mapping[str, WorksheetPolicy],
    rules_by_sheet: Mapping[str, Mapping[int, FieldRule]],
) -> None:
    """
    Отношение worksheet_policies <-> rules_by_sheet (Stage 10C.1): каждый
    PROCESS-лист обязан иметь запись в rules_by_sheet; ни один REMOVE_ALL-
    лист не должен иметь такой записи (не игнорируется молча — считается
    несогласованной конфигурацией); rules_by_sheet не может ссылаться на
    лист, отсутствующий в worksheet_policies.

    ВАЖНО: эта функция полагается на то, что worksheet_policies уже
    провалидирован против реального workbook через
    validate_worksheet_coverage (вызывается раньше в
    prepare_workbook_for_anonymization) — поэтому она не принимает и не
    требует объект workbook: проверка "ссылается ли rules_by_sheet на
    реально существующий лист" транзитивно следует из уже установленного
    равенства worksheet_policies.keys() == реальные имена листов.
    """
    _validate_rules_by_sheet_type(rules_by_sheet)

    rules_names = set(rules_by_sheet.keys())
    policy_names = set(worksheet_policies.keys())

    if rules_names - policy_names:
        raise CoverageError(CoverageReason.RULES_FOR_UNKNOWN_WORKSHEET)

    for name, policy in worksheet_policies.items():
        if policy is WorksheetPolicy.PROCESS and name not in rules_names:
            raise CoverageError(CoverageReason.MISSING_PROCESS_RULES)
        if policy is WorksheetPolicy.REMOVE_ALL and name in rules_names:
            raise CoverageError(CoverageReason.RULES_FOR_REMOVE_ALL_WORKSHEET)


# ----------------------------------------------------------------------
# Public API: column action matrix
# ----------------------------------------------------------------------


def validate_column_action(rule: FieldRule) -> None:
    """
    Frozen column action matrix (Stage 10C Final Contract Freeze §4):
    Action.KEEP запрещён для COMPANY/PERSON/PHONE/EMAIL/ADDRESS/UNKNOWN;
    Action.PSEUDONYMIZE допустим только для COMPANY/PERSON/INN/KPP/OGRN
    (ранний, Stage10C-специфичный отказ ДО обращения к Stage 8 — не
    дожидается anonymizer.UnsupportedPseudonymizationTargetError).
    Не проверяет содержимое ячеек (это scan_process_worksheet) — только
    саму декларацию FieldType/Action.
    """
    if not isinstance(rule, FieldRule):
        raise TypeError(f"rule должен быть FieldRule, получено: {type(rule)!r}")

    if rule.action is Action.KEEP and rule.field_type in _KEEP_FORBIDDEN_TYPES:
        raise ColumnSafetyError(ColumnSafetyReason.KEEP_FORBIDDEN_FOR_TYPE)

    if rule.action is Action.PSEUDONYMIZE and rule.field_type not in _PSEUDONYMIZE_SUPPORTED_TYPES:
        raise ColumnSafetyError(ColumnSafetyReason.PSEUDONYMIZE_UNSUPPORTED_FOR_TYPE)


# ----------------------------------------------------------------------
# Public API: единый скан одного PROCESS-листа
# ----------------------------------------------------------------------


def scan_process_worksheet(
    worksheet: Worksheet,
    worksheet_index: int,
    column_rules: Mapping[int, FieldRule],
) -> WorksheetScanResult:
    """
    Один контролируемый проход по существующим ячейкам PROCESS-листа,
    вычисляющий одновременно: formula-детекцию (любая ячейка листа,
    независимо от column rule), cross-type identifier detector для
    допустимых KEEP-колонок и identifier expected-count evidence (Step A)
    для PSEUDONYMIZE-identifier-колонок. Не мутирует worksheet. Сам не
    поднимает исключений по найденным нарушениям — решение о HARD FAIL
    принимает вызывающий код (prepare_workbook_for_anonymization),
    агрегирующий результаты по всем PROCESS-листам книги.

    Header cross-check выполняется отдельно, ПЕРЕД основным проходом:
    для каждой реально объявленной column rule явно ищется ячейка
    (row=1, column) в worksheet._cells — а не полагается на то, что
    основной проход обязательно "наткнётся" на неё (worksheet._cells —
    разреженный словарь: полностью пустая/никогда не заполнявшаяся ячейка
    заголовка в нём попросту отсутствует).
    """
    if isinstance(worksheet_index, bool) or not isinstance(worksheet_index, int) or worksheet_index < 1:
        raise ValueError("worksheet_index должен быть int >= 1")
    if not isinstance(column_rules, Mapping):
        raise TypeError(f"column_rules должен быть Mapping, получено: {type(column_rules)!r}")

    cells = getattr(worksheet, "_cells", None)
    if not isinstance(cells, dict):
        raise CoverageError(CoverageReason.WORKBOOK_MUTATION_FAILED)
    for key in cells:
        if not (
            isinstance(key, tuple)
            and len(key) == 2
            and isinstance(key[0], int)
            and isinstance(key[1], int)
        ):
            raise CoverageError(CoverageReason.WORKBOOK_MUTATION_FAILED)

    header_mismatches: list[int] = []
    for column, rule in column_rules.items():
        header_cell = cells.get((1, column))
        if header_cell is None or isinstance(header_cell, MergedCell):
            header_value = None
        else:
            header_value = header_cell.value
        if header_value != rule.column_name:
            header_mismatches.append(column)

    formula_found = False
    keep_violations: list[tuple[int, ColumnSafetyReason]] = []
    expected_counts: dict[tuple[int, IdentifierType], int] = {}

    for (row, column), cell in cells.items():
        if isinstance(cell, MergedCell):
            continue

        if cell.data_type == "f":
            formula_found = True

        if row == 1:
            continue

        rule = column_rules.get(column)
        if rule is None:
            continue

        value = cell.value

        if rule.action is Action.KEEP:
            if rule.field_type in _KEEP_REQUIRES_DETECTOR_SCAN:
                if not _is_empty_identifier_value(value) and _is_identifier_like(value):
                    keep_violations.append((column, ColumnSafetyReason.CROSS_TYPE_IDENTIFIER_DETECTED))
            continue

        if rule.action is Action.PSEUDONYMIZE and rule.field_type in _IDENTIFIER_FIELD_TYPES:
            if not _is_empty_identifier_value(value):
                key = (column, _IDENTIFIER_FIELD_TYPES[rule.field_type])
                expected_counts[key] = expected_counts.get(key, 0) + 1

    identifier_evidence = tuple(
        IdentifierColumnEvidence(
            worksheet_index=worksheet_index,
            column=column,
            identifier_type=identifier_type,
            expected_count=count,
        )
        for (column, identifier_type), count in sorted(expected_counts.items())
    )

    return WorksheetScanResult(
        formula_found=formula_found,
        header_mismatches=tuple(sorted(header_mismatches)),
        keep_violations=tuple(keep_violations),
        identifier_evidence=identifier_evidence,
    )


# ----------------------------------------------------------------------
# REMOVE_ALL — приватная мутация (не публичный API, Stage 10C.1
# Implementation Contract §20)
# ----------------------------------------------------------------------


def _apply_remove_all(workbook: openpyxl.Workbook, worksheet_policies: Mapping[str, WorksheetPolicy]) -> None:
    for name, policy in worksheet_policies.items():
        if policy is WorksheetPolicy.REMOVE_ALL:
            workbook.remove(workbook[name])


# ----------------------------------------------------------------------
# Public API: единственная точка входа для оркестрации
# ----------------------------------------------------------------------


@contextlib.contextmanager
def prepare_workbook_for_anonymization(
    source_path: _PathLike,
    worksheet_policies: Mapping[str, WorksheetPolicy],
    rules_by_sheet: Mapping[str, Mapping[int, FieldRule]],
) -> Iterator[WorksheetPreparationResult]:
    """
    Единственная точка входа Stage 10C.1: валидирует worksheet coverage,
    отношение policy<->rules, column action matrix, header cross-check,
    formula-политику и column KEEP safety (включая cross-type identifier
    detector), затем физически удаляет REMOVE_ALL-листы и сохраняет
    результат во ВРЕМЕННЫЙ .xlsx (вне Workspace, случайное имя,
    suffix=".xlsx"), пригодный быть переданным как source_path в Stage 8
    anonymize_workbook. Возвращает (через yield) WorksheetPreparationResult
    с identifier expected-count evidence (Step A).

    Side effects: создаёт временный файл вне Workspace; удаляет его в
    finally (после выхода из with-блока, независимо от исхода). Mutability:
    source_path открывается РОВНО ОДИН РАЗ, только на чтение
    (openpyxl.load_workbook) — никогда не перезаписывается; вся мутация
    (REMOVE_ALL) применяется к уже загруженному в память объекту и
    сохраняется исключительно в новый временный путь. Ownership: временный
    файл целиком принадлежит этому context manager'у — вызывающий код не
    должен ни удалять его сам, ни использовать prepared_path за пределами
    with-блока.

    :raises TypeError: source_path не str/Path, worksheet_policies/
        rules_by_sheet не Mapping ожидаемой формы.
    :raises CoverageError: нарушение worksheet-level policy/coverage/
        header/formula-политики, либо технический сбой при мутации/
        сохранении workbook.
    :raises ColumnSafetyError: запрещённое сочетание FieldType/Action,
        либо KEEP-колонка с обнаруженным identifier-подобным значением.
    """
    if not isinstance(source_path, (str, Path)):
        raise TypeError(f"source_path должен быть str или Path, получено: {type(source_path)!r}")

    _validate_worksheet_policies_type(worksheet_policies)
    _validate_rules_by_sheet_type(rules_by_sheet)

    source = Path(source_path)

    workbook = openpyxl.load_workbook(str(source), data_only=False, read_only=False)
    try:
        validate_worksheet_coverage(workbook, worksheet_policies)
        validate_process_rules(worksheet_policies, rules_by_sheet)

        for rules in rules_by_sheet.values():
            for rule in rules.values():
                validate_column_action(rule)

        remove_all_failed = False
        try:
            _apply_remove_all(workbook, worksheet_policies)
        except Exception:
            remove_all_failed = True
        if remove_all_failed:
            raise CoverageError(CoverageReason.WORKBOOK_MUTATION_FAILED)

        evidence: list[IdentifierColumnEvidence] = []
        for worksheet_index, worksheet in enumerate(workbook.worksheets, start=1):
            policy = worksheet_policies[worksheet.title]
            if policy is not WorksheetPolicy.PROCESS:
                continue

            column_rules = rules_by_sheet[worksheet.title]
            scan_result = scan_process_worksheet(worksheet, worksheet_index, column_rules)

            if scan_result.header_mismatches:
                raise CoverageError(CoverageReason.HEADER_MISMATCH)
            if scan_result.formula_found:
                raise CoverageError(CoverageReason.FORMULA_REJECTED)
            if scan_result.keep_violations:
                raise ColumnSafetyError(ColumnSafetyReason.CROSS_TYPE_IDENTIFIER_DETECTED)

            evidence.extend(scan_result.identifier_evidence)

        identifier_evidence = tuple(sorted(evidence, key=lambda item: (item.worksheet_index, item.column)))

        fd, tmp_name = tempfile.mkstemp(suffix=".xlsx")
        tmp_path = Path(tmp_name)

        # Санитизация mkstemp fd close (закрытие MINOR-1 независимого
        # review): в этот момент tmp_path гарантированно пуст (workbook.save
        # ещё не вызывался) — при сбое здесь нет confidential-содержимого,
        # которое могло бы утечь, но раздетый OSError всё равно не должен
        # покидать эту функцию как есть, и temp обязан быть убран
        # best-effort. Повторная попытка os.close на том же fd намеренно НЕ
        # предпринимается: после неудачного close() состояние дескриптора
        # не специфицировано POSIX, и повторное закрытие того же номера
        # может закрыть чужой, повторно выделенный ОС дескриптор — это не
        # "безопасно/разумно", а дополнительный источник риска.
        fd_close_failed = False
        try:
            os.close(fd)
        except Exception:
            fd_close_failed = True
        if fd_close_failed:
            try:
                tmp_path.unlink(missing_ok=True)
            except OSError:
                pass
            raise CoverageError(CoverageReason.WORKBOOK_MUTATION_FAILED)

        save_failed = False
        try:
            workbook.save(str(tmp_path))

            # fsync требует дескриптор, открытый для записи (O_RDWR) — на
            # Windows os.fsync на read-only дескрипторе поднимает OSError
            # (тот же приём, что app.excel.writer._atomic_save).
            fsync_fd = os.open(str(tmp_path), os.O_RDWR)
            try:
                os.fsync(fsync_fd)
            finally:
                os.close(fsync_fd)
        except Exception:
            save_failed = True
        if save_failed:
            try:
                tmp_path.unlink(missing_ok=True)
            except OSError:
                pass
            raise CoverageError(CoverageReason.WORKBOOK_MUTATION_FAILED)
    finally:
        workbook.close()

    try:
        yield WorksheetPreparationResult(prepared_path=tmp_path, identifier_evidence=identifier_evidence)
    finally:
        try:
            tmp_path.unlink(missing_ok=True)
        except OSError:
            pass
