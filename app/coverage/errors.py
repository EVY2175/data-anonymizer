"""
Исключения Stage 10C application-layer (начиная с Stage 10C.1).

Та же дисциплина, что и app.workspace.errors (Stage 10B): каждое
исключение несёт reason-enum, а текст сообщения фиксирован по reason в
модульном словаре — вызывающий код не может подставить произвольный текст.
Ни одно сообщение не включает: имя листа, FieldRule.column_name/фактический
заголовок, содержимое ячеек, raw identifier/company/person значение,
пароль, содержимое расшифрованных сторов.
"""

from __future__ import annotations

import enum

# ----------------------------------------------------------------------
# Reason-enum'ы
# ----------------------------------------------------------------------


class CoverageReason(enum.Enum):
    INVALID_POLICY_VALUE = "invalid_policy_value"
    MISSING_WORKSHEET_POLICY = "missing_worksheet_policy"
    POLICY_FOR_UNKNOWN_WORKSHEET = "policy_for_unknown_worksheet"
    NO_PROCESS_WORKSHEET_REMAINS = "no_process_worksheet_remains"
    NO_VISIBLE_PROCESS_WORKSHEET = "no_visible_process_worksheet"
    MISSING_PROCESS_RULES = "missing_process_rules"
    RULES_FOR_UNKNOWN_WORKSHEET = "rules_for_unknown_worksheet"
    RULES_FOR_REMOVE_ALL_WORKSHEET = "rules_for_remove_all_worksheet"
    HEADER_MISMATCH = "header_mismatch"
    FORMULA_REJECTED = "formula_rejected"
    WORKBOOK_MUTATION_FAILED = "workbook_mutation_failed"


class ColumnSafetyReason(enum.Enum):
    KEEP_FORBIDDEN_FOR_TYPE = "keep_forbidden_for_type"
    PSEUDONYMIZE_UNSUPPORTED_FOR_TYPE = "pseudonymize_unsupported_for_type"
    CROSS_TYPE_IDENTIFIER_DETECTED = "cross_type_identifier_detected"


# ----------------------------------------------------------------------
# Фиксированные сообщения
# ----------------------------------------------------------------------

_COVERAGE_MESSAGES = {
    CoverageReason.INVALID_POLICY_VALUE: (
        "Некорректная структура или значение конфигурации worksheet policy/rules"
    ),
    CoverageReason.MISSING_WORKSHEET_POLICY: (
        "Для существующего листа не задана явная worksheet policy"
    ),
    CoverageReason.POLICY_FOR_UNKNOWN_WORKSHEET: (
        "Worksheet policy ссылается на несуществующий лист"
    ),
    CoverageReason.NO_PROCESS_WORKSHEET_REMAINS: (
        "Все листы помечены REMOVE_ALL — ни одного PROCESS-листа не осталось"
    ),
    CoverageReason.NO_VISIBLE_PROCESS_WORKSHEET: (
        "Среди PROCESS-листов нет ни одного видимого — все скрыты (hidden/veryHidden)"
    ),
    CoverageReason.MISSING_PROCESS_RULES: (
        "PROCESS-лист не имеет соответствующей записи column rules"
    ),
    CoverageReason.RULES_FOR_UNKNOWN_WORKSHEET: (
        "Column rules заданы для несуществующего листа"
    ),
    CoverageReason.RULES_FOR_REMOVE_ALL_WORKSHEET: (
        "Column rules заданы для листа с policy REMOVE_ALL"
    ),
    CoverageReason.HEADER_MISMATCH: (
        "Заголовок колонки не совпадает с конфигурацией правила"
    ),
    CoverageReason.FORMULA_REJECTED: (
        "На PROCESS-листе обнаружена формула"
    ),
    CoverageReason.WORKBOOK_MUTATION_FAILED: (
        "Не удалось безопасно подготовить workbook"
    ),
}

_COLUMN_SAFETY_MESSAGES = {
    ColumnSafetyReason.KEEP_FORBIDDEN_FOR_TYPE: (
        "Action.KEEP недопустим для данного FieldType"
    ),
    ColumnSafetyReason.PSEUDONYMIZE_UNSUPPORTED_FOR_TYPE: (
        "Action.PSEUDONYMIZE не поддержан для данного FieldType"
    ),
    ColumnSafetyReason.CROSS_TYPE_IDENTIFIER_DETECTED: (
        "В KEEP-колонке обнаружено значение, структурно являющееся идентификатором"
    ),
}


def _require_reason(reason: object, enum_type: type) -> None:
    if not isinstance(reason, enum_type):
        raise TypeError(f"reason должен быть {enum_type.__name__}")


# ----------------------------------------------------------------------
# Исключения
# ----------------------------------------------------------------------


class Stage10CError(Exception):
    """Базовое исключение Stage 10C application-layer."""


class CoverageError(Stage10CError):
    """
    Нарушение worksheet-level policy, её отношения к column rules, header
    cross-check или formula-политики (Stage 10C.1), либо технический сбой
    безопасной подготовки workbook.
    """

    def __init__(self, reason: CoverageReason) -> None:
        _require_reason(reason, CoverageReason)
        super().__init__(_COVERAGE_MESSAGES[reason])
        self.reason = reason


class ColumnSafetyError(Stage10CError):
    """
    Нарушение column action matrix (Stage 10C.1): запрещённое сочетание
    FieldType/Action, либо KEEP-колонка, в которой обнаружено значение,
    структурно являющееся идентификатором.
    """

    def __init__(self, reason: ColumnSafetyReason) -> None:
        _require_reason(reason, ColumnSafetyReason)
        super().__init__(_COLUMN_SAFETY_MESSAGES[reason])
        self.reason = reason
