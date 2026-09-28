"""
Тесты Stage 10C.1 — frozen column action matrix
(app.coverage.worksheet_policy.validate_column_action).

Полное покрытие FieldType x Action согласно Stage 10C Final Contract
Freeze §4. validate_column_action проверяет только саму декларацию
(FieldType/Action) — не содержимое ячеек (это cross-type detector,
покрыт test_cross_type_identifier_detector.py).
"""

from __future__ import annotations

import pytest

from app.coverage.errors import ColumnSafetyError, ColumnSafetyReason
from app.coverage.worksheet_policy import validate_column_action
from app.models.rules import Action, FieldRule, FieldType


def _rule(field_type: FieldType, action: Action) -> FieldRule:
    return FieldRule(column_name="Column", field_type=field_type, action=action)


# ---------------------------------------------------------------------------
# A. Допустимые сочетания (не должны поднимать исключение)
# ---------------------------------------------------------------------------

_ALLOWED_CASES = [
    (FieldType.COMPANY, Action.PSEUDONYMIZE),
    (FieldType.COMPANY, Action.REMOVE),
    (FieldType.PERSON, Action.PSEUDONYMIZE),
    (FieldType.PERSON, Action.REMOVE),
    (FieldType.INN, Action.PSEUDONYMIZE),
    (FieldType.INN, Action.REMOVE),
    (FieldType.INN, Action.KEEP),
    (FieldType.KPP, Action.PSEUDONYMIZE),
    (FieldType.KPP, Action.REMOVE),
    (FieldType.KPP, Action.KEEP),
    (FieldType.OGRN, Action.PSEUDONYMIZE),
    (FieldType.OGRN, Action.REMOVE),
    (FieldType.OGRN, Action.KEEP),
    (FieldType.BRANCH, Action.KEEP),
    (FieldType.BRANCH, Action.REMOVE),
    (FieldType.DEPARTMENT, Action.KEEP),
    (FieldType.DEPARTMENT, Action.REMOVE),
    (FieldType.PHONE, Action.REMOVE),
    (FieldType.EMAIL, Action.REMOVE),
    (FieldType.ADDRESS, Action.REMOVE),
    (FieldType.FINANCIAL, Action.KEEP),
    (FieldType.FINANCIAL, Action.REMOVE),
    (FieldType.UNKNOWN, Action.REMOVE),
]


@pytest.mark.parametrize("field_type,action", _ALLOWED_CASES)
def test_allowed_combination_does_not_raise(field_type: FieldType, action: Action) -> None:
    validate_column_action(_rule(field_type, action))


# ---------------------------------------------------------------------------
# B. KEEP_FORBIDDEN_FOR_TYPE
# ---------------------------------------------------------------------------

_KEEP_FORBIDDEN_CASES = [
    FieldType.COMPANY,
    FieldType.PERSON,
    FieldType.PHONE,
    FieldType.EMAIL,
    FieldType.ADDRESS,
    FieldType.UNKNOWN,
]


@pytest.mark.parametrize("field_type", _KEEP_FORBIDDEN_CASES)
def test_keep_forbidden_for_type(field_type: FieldType) -> None:
    with pytest.raises(ColumnSafetyError) as exc_info:
        validate_column_action(_rule(field_type, Action.KEEP))
    assert exc_info.value.reason is ColumnSafetyReason.KEEP_FORBIDDEN_FOR_TYPE


def test_company_keep_rejected() -> None:
    with pytest.raises(ColumnSafetyError) as exc_info:
        validate_column_action(_rule(FieldType.COMPANY, Action.KEEP))
    assert exc_info.value.reason is ColumnSafetyReason.KEEP_FORBIDDEN_FOR_TYPE


def test_person_keep_rejected() -> None:
    with pytest.raises(ColumnSafetyError) as exc_info:
        validate_column_action(_rule(FieldType.PERSON, Action.KEEP))
    assert exc_info.value.reason is ColumnSafetyReason.KEEP_FORBIDDEN_FOR_TYPE


def test_phone_keep_rejected() -> None:
    with pytest.raises(ColumnSafetyError) as exc_info:
        validate_column_action(_rule(FieldType.PHONE, Action.KEEP))
    assert exc_info.value.reason is ColumnSafetyReason.KEEP_FORBIDDEN_FOR_TYPE


def test_email_keep_rejected() -> None:
    with pytest.raises(ColumnSafetyError) as exc_info:
        validate_column_action(_rule(FieldType.EMAIL, Action.KEEP))
    assert exc_info.value.reason is ColumnSafetyReason.KEEP_FORBIDDEN_FOR_TYPE


def test_address_keep_rejected() -> None:
    with pytest.raises(ColumnSafetyError) as exc_info:
        validate_column_action(_rule(FieldType.ADDRESS, Action.KEEP))
    assert exc_info.value.reason is ColumnSafetyReason.KEEP_FORBIDDEN_FOR_TYPE


def test_unknown_keep_rejected() -> None:
    with pytest.raises(ColumnSafetyError) as exc_info:
        validate_column_action(_rule(FieldType.UNKNOWN, Action.KEEP))
    assert exc_info.value.reason is ColumnSafetyReason.KEEP_FORBIDDEN_FOR_TYPE


# ---------------------------------------------------------------------------
# C. PSEUDONYMIZE_UNSUPPORTED_FOR_TYPE
# ---------------------------------------------------------------------------

_PSEUDONYMIZE_UNSUPPORTED_CASES = [
    FieldType.BRANCH,
    FieldType.DEPARTMENT,
    FieldType.PHONE,
    FieldType.EMAIL,
    FieldType.ADDRESS,
    FieldType.FINANCIAL,
    FieldType.UNKNOWN,
]


@pytest.mark.parametrize("field_type", _PSEUDONYMIZE_UNSUPPORTED_CASES)
def test_pseudonymize_unsupported_for_type(field_type: FieldType) -> None:
    with pytest.raises(ColumnSafetyError) as exc_info:
        validate_column_action(_rule(field_type, Action.PSEUDONYMIZE))
    assert exc_info.value.reason is ColumnSafetyReason.PSEUDONYMIZE_UNSUPPORTED_FOR_TYPE


def test_branch_pseudonymize_rejected_early() -> None:
    with pytest.raises(ColumnSafetyError) as exc_info:
        validate_column_action(_rule(FieldType.BRANCH, Action.PSEUDONYMIZE))
    assert exc_info.value.reason is ColumnSafetyReason.PSEUDONYMIZE_UNSUPPORTED_FOR_TYPE


def test_department_pseudonymize_rejected_early() -> None:
    with pytest.raises(ColumnSafetyError) as exc_info:
        validate_column_action(_rule(FieldType.DEPARTMENT, Action.PSEUDONYMIZE))
    assert exc_info.value.reason is ColumnSafetyReason.PSEUDONYMIZE_UNSUPPORTED_FOR_TYPE


def test_phone_pseudonymize_rejected_early() -> None:
    with pytest.raises(ColumnSafetyError) as exc_info:
        validate_column_action(_rule(FieldType.PHONE, Action.PSEUDONYMIZE))
    assert exc_info.value.reason is ColumnSafetyReason.PSEUDONYMIZE_UNSUPPORTED_FOR_TYPE


def test_email_pseudonymize_rejected_early() -> None:
    with pytest.raises(ColumnSafetyError) as exc_info:
        validate_column_action(_rule(FieldType.EMAIL, Action.PSEUDONYMIZE))
    assert exc_info.value.reason is ColumnSafetyReason.PSEUDONYMIZE_UNSUPPORTED_FOR_TYPE


def test_address_pseudonymize_rejected_early() -> None:
    with pytest.raises(ColumnSafetyError) as exc_info:
        validate_column_action(_rule(FieldType.ADDRESS, Action.PSEUDONYMIZE))
    assert exc_info.value.reason is ColumnSafetyReason.PSEUDONYMIZE_UNSUPPORTED_FOR_TYPE


def test_financial_pseudonymize_rejected_early() -> None:
    with pytest.raises(ColumnSafetyError) as exc_info:
        validate_column_action(_rule(FieldType.FINANCIAL, Action.PSEUDONYMIZE))
    assert exc_info.value.reason is ColumnSafetyReason.PSEUDONYMIZE_UNSUPPORTED_FOR_TYPE


def test_unknown_pseudonymize_rejected_early() -> None:
    with pytest.raises(ColumnSafetyError) as exc_info:
        validate_column_action(_rule(FieldType.UNKNOWN, Action.PSEUDONYMIZE))
    assert exc_info.value.reason is ColumnSafetyReason.PSEUDONYMIZE_UNSUPPORTED_FOR_TYPE


# ---------------------------------------------------------------------------
# D. Некорректный вход
# ---------------------------------------------------------------------------


def test_validate_column_action_rejects_non_field_rule() -> None:
    with pytest.raises(TypeError):
        validate_column_action("not-a-rule")  # type: ignore[arg-type]
