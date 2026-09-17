"""
Тесты app.detectors: is_valid_inn/kpp/ogrn/ogrnip, detect_value_type,
detect_column_type.

Тестовые векторы ИНН-10/ИНН-12/ОГРН/ОГРНИП с корректной контрольной суммой
проверены прямым вычислением по формулам ТЗ (см. отчёт), а не взяты
"на веру".
"""

from __future__ import annotations

import pytest

from app.detectors import (
    _normalize_column_name,
    detect_column_type,
    detect_value_type,
    is_valid_inn,
    is_valid_kpp,
    is_valid_ogrn,
    is_valid_ogrnip,
)
from app.models.rules import FieldType

# ---------------------------------------------------------------------------
# Проверенные валидные тестовые векторы (checksum вычислен по формулам ТЗ)
# ---------------------------------------------------------------------------

VALID_INN10 = "7707083893"
VALID_INN12 = "500100732259"
VALID_OGRN13 = "1047709000008"
VALID_OGRNIP15 = "304770900000126"
VALID_KPP = "770701001"

# Полноширинная Unicode-цифра '3' (U+FF13) — визуально похожа на ASCII '3',
# но другая кодовая точка. Используется, чтобы доказать отказ от
# Unicode-цифр там, где допустимы только ASCII 0-9.
_FULLWIDTH_THREE = "３"


# ---------------------------------------------------------------------------
# ИНН
# ---------------------------------------------------------------------------


def test_inn10_known_valid() -> None:
    assert is_valid_inn(VALID_INN10) is True


def test_inn10_wrong_check_digit() -> None:
    corrupted = VALID_INN10[:9] + "4"  # правильная цифра — "3"
    assert is_valid_inn(corrupted) is False


def test_inn12_known_valid() -> None:
    assert is_valid_inn(VALID_INN12) is True


def test_inn12_first_check_digit_wrong() -> None:
    # 11-я цифра должна быть '5'; подменяем только её.
    corrupted = VALID_INN12[:10] + "0" + VALID_INN12[11:]
    assert is_valid_inn(corrupted) is False


def test_inn12_second_check_digit_wrong() -> None:
    # 12-я цифра должна быть '9'; подменяем только её.
    corrupted = VALID_INN12[:11] + "0"
    assert is_valid_inn(corrupted) is False


@pytest.mark.parametrize("bad_length", ["770708389", "77070838933", "1", ""])
def test_inn_rejects_wrong_length(bad_length: str) -> None:
    assert is_valid_inn(bad_length) is False


def test_inn_rejects_letters() -> None:
    assert is_valid_inn("770708389A") is False


def test_inn_rejects_internal_whitespace() -> None:
    assert is_valid_inn("770 708389 3") is False
    assert is_valid_inn("7707 083893") is False


def test_inn_strips_outer_whitespace() -> None:
    assert is_valid_inn(f"  {VALID_INN10}  ") is True


def test_inn_rejects_unicode_digits() -> None:
    corrupted = VALID_INN10[:9] + _FULLWIDTH_THREE
    assert is_valid_inn(corrupted) is False


def test_inn_accepts_int_exact_value() -> None:
    assert is_valid_inn(int(VALID_INN10)) is True
    assert is_valid_inn(int(VALID_INN12)) is True


def test_inn_rejects_float_even_when_numerically_equal() -> None:
    assert is_valid_inn(float(VALID_INN10)) is False
    assert is_valid_inn(7707083893.0) is False


def test_inn_rejects_bool() -> None:
    assert is_valid_inn(True) is False
    assert is_valid_inn(False) is False


def test_inn_rejects_none() -> None:
    assert is_valid_inn(None) is False


def test_inn_rejects_other_types() -> None:
    assert is_valid_inn([7707083893]) is False
    assert is_valid_inn({"inn": VALID_INN10}) is False


# ---------------------------------------------------------------------------
# ОГРН
# ---------------------------------------------------------------------------


def test_ogrn_known_valid() -> None:
    assert is_valid_ogrn(VALID_OGRN13) is True


def test_ogrn_wrong_check_digit() -> None:
    corrupted = VALID_OGRN13[:12] + "9"  # правильная цифра — "8"
    assert is_valid_ogrn(corrupted) is False


@pytest.mark.parametrize("bad_length", ["104770900000", "10477090000081", ""])
def test_ogrn_rejects_wrong_length(bad_length: str) -> None:
    assert is_valid_ogrn(bad_length) is False


def test_ogrn_rejects_unicode_digits() -> None:
    corrupted = VALID_OGRN13[:12] + _FULLWIDTH_THREE
    assert is_valid_ogrn(corrupted) is False


def test_ogrn_accepts_int_exact_value() -> None:
    assert is_valid_ogrn(int(VALID_OGRN13)) is True


def test_ogrn_rejects_float() -> None:
    assert is_valid_ogrn(float(VALID_OGRN13)) is False


def test_ogrn_rejects_bool_and_none() -> None:
    assert is_valid_ogrn(True) is False
    assert is_valid_ogrn(None) is False


def test_ogrn_does_not_accept_15_digit_ogrnip() -> None:
    """is_valid_ogrn проверяет ТОЛЬКО 13-значный ОГРН."""
    assert is_valid_ogrn(VALID_OGRNIP15) is False


# ---------------------------------------------------------------------------
# ОГРНИП
# ---------------------------------------------------------------------------


def test_ogrnip_known_valid() -> None:
    assert is_valid_ogrnip(VALID_OGRNIP15) is True


def test_ogrnip_wrong_check_digit() -> None:
    corrupted = VALID_OGRNIP15[:14] + "5"  # правильная цифра — "6"
    assert is_valid_ogrnip(corrupted) is False


@pytest.mark.parametrize("bad_length", ["30477090000012", "3047709000001266", ""])
def test_ogrnip_rejects_wrong_length(bad_length: str) -> None:
    assert is_valid_ogrnip(bad_length) is False


def test_ogrnip_accepts_int_exact_value() -> None:
    assert is_valid_ogrnip(int(VALID_OGRNIP15)) is True


def test_ogrnip_rejects_float() -> None:
    assert is_valid_ogrnip(float(VALID_OGRNIP15)) is False


def test_ogrnip_rejects_bool_and_none() -> None:
    assert is_valid_ogrnip(True) is False
    assert is_valid_ogrnip(None) is False


def test_ogrnip_does_not_accept_13_digit_ogrn() -> None:
    """is_valid_ogrnip проверяет ТОЛЬКО 15-значный ОГРНИП."""
    assert is_valid_ogrnip(VALID_OGRN13) is False


# ---------------------------------------------------------------------------
# КПП
# ---------------------------------------------------------------------------


def test_kpp_all_numeric_valid() -> None:
    assert is_valid_kpp(VALID_KPP) is True


def test_kpp_allows_uppercase_letters_in_positions_5_6() -> None:
    assert is_valid_kpp("7707AB001") is True


def test_kpp_rejects_lowercase_letters() -> None:
    assert is_valid_kpp("7707ab001") is False


def test_kpp_rejects_cyrillic_lookalikes() -> None:
    # "А" и "В" здесь кириллические (U+0410, U+0412), не латинские.
    assert is_valid_kpp("7707АВ001") is False


@pytest.mark.parametrize("bad_length", ["77070100", "7707010011", ""])
def test_kpp_rejects_wrong_length(bad_length: str) -> None:
    assert is_valid_kpp(bad_length) is False


def test_kpp_rejects_letter_in_digit_only_position() -> None:
    assert is_valid_kpp("77A701001") is False  # буква в первых 4 позициях
    assert is_valid_kpp("770701A01") is False  # буква в последних 3 позициях


def test_kpp_rejects_unicode_digits() -> None:
    corrupted = VALID_KPP[:8] + _FULLWIDTH_THREE
    assert is_valid_kpp(corrupted) is False


def test_kpp_strips_outer_whitespace() -> None:
    assert is_valid_kpp(f"  {VALID_KPP}  ") is True


def test_kpp_rejects_internal_whitespace() -> None:
    assert is_valid_kpp("7707 01001") is False


def test_kpp_accepts_int_for_fully_numeric_value() -> None:
    assert is_valid_kpp(int(VALID_KPP)) is True


def test_kpp_rejects_float_bool_none() -> None:
    assert is_valid_kpp(float(VALID_KPP)) is False
    assert is_valid_kpp(770701001.0) is False
    assert is_valid_kpp(True) is False
    assert is_valid_kpp(None) is False


# ---------------------------------------------------------------------------
# Перекрёстная проверка: форматы не пересекаются по длине
# ---------------------------------------------------------------------------


def test_valid_identifiers_do_not_cross_validate() -> None:
    """
    Длины форматов попарно различны (КПП=9, ИНН=10/12, ОГРН=13,
    ОГРНИП=15), поэтому ни одно валидное значение одного типа не может
    случайно пройти валидатор другого типа.
    """
    assert is_valid_kpp(VALID_INN10) is False
    assert is_valid_inn(VALID_KPP) is False
    assert is_valid_inn(VALID_OGRN13) is False
    assert is_valid_ogrn(VALID_INN12) is False
    assert is_valid_ogrn(VALID_OGRNIP15) is False
    assert is_valid_ogrnip(VALID_OGRN13) is False


# ---------------------------------------------------------------------------
# detect_value_type
# ---------------------------------------------------------------------------


def test_detect_value_type_inn() -> None:
    assert detect_value_type(VALID_INN10) is FieldType.INN
    assert detect_value_type(VALID_INN12) is FieldType.INN


def test_detect_value_type_ogrn() -> None:
    assert detect_value_type(VALID_OGRN13) is FieldType.OGRN


def test_detect_value_type_ogrnip_maps_to_ogrn() -> None:
    assert detect_value_type(VALID_OGRNIP15) is FieldType.OGRN


def test_detect_value_type_does_not_auto_detect_kpp_by_value() -> None:
    """
    КПП не имеет checksum, поэтому detect_value_type() сознательно НЕ
    классифицирует структурно похожие на КПП значения как FieldType.KPP —
    это может быть произвольный внутренний ID/номер договора и т.п.
    Автоматическая детекция КПП возможна только по контексту названия
    столбца (см. detect_column_type). is_valid_kpp() как структурный
    валидатор при этом продолжает работать без изменений.
    """
    assert detect_value_type(VALID_KPP) is FieldType.UNKNOWN


@pytest.mark.parametrize(
    "value",
    ["770701001", 770701001, "123456789", "111111111", "7707AB001"],
)
def test_detect_value_type_kpp_like_values_are_unknown(value: object) -> None:
    assert detect_value_type(value) is FieldType.UNKNOWN


def test_is_valid_kpp_contract_unchanged_after_detect_value_type_fix() -> None:
    """
    Регрессия: изменение detect_value_type() не должно затрагивать
    is_valid_kpp() как отдельный структурный валидатор, и
    detect_column_type("КПП") должен по-прежнему давать FieldType.KPP.
    """
    assert is_valid_kpp("770701001") is True
    assert is_valid_kpp(770701001) is True
    assert is_valid_kpp("7707AB001") is True
    assert detect_column_type("КПП") is FieldType.KPP


@pytest.mark.parametrize(
    "text",
    ['ООО "Ромашка"', "Иванов Иван Иванович", "Москва", "не идентификатор"],
)
def test_detect_value_type_plain_text_is_unknown(text: str) -> None:
    assert detect_value_type(text) is FieldType.UNKNOWN


def test_detect_value_type_float_is_unknown() -> None:
    assert detect_value_type(7707083893.0) is FieldType.UNKNOWN


def test_detect_value_type_none_is_unknown() -> None:
    assert detect_value_type(None) is FieldType.UNKNOWN


def test_detect_value_type_never_returns_none() -> None:
    for value in (VALID_INN10, VALID_OGRN13, VALID_OGRNIP15, VALID_KPP, "text", None, 1.5):
        assert detect_value_type(value) is not None
        assert isinstance(detect_value_type(value), FieldType)


# ---------------------------------------------------------------------------
# detect_column_type
# ---------------------------------------------------------------------------

_COLUMN_HEADER_EXAMPLES: list[tuple[str, FieldType]] = [
    ("компания", FieldType.COMPANY),
    ("организация", FieldType.COMPANY),
    ("наименование организации", FieldType.COMPANY),
    ("наименование компании", FieldType.COMPANY),
    ("контрагент", FieldType.COMPANY),
    ("филиал", FieldType.BRANCH),
    ("наименование филиала", FieldType.BRANCH),
    ("подразделение", FieldType.DEPARTMENT),
    ("отдел", FieldType.DEPARTMENT),
    ("департамент", FieldType.DEPARTMENT),
    ("структурное подразделение", FieldType.DEPARTMENT),
    ("фио", FieldType.PERSON),
    ("ф.и.о.", FieldType.PERSON),
    ("сотрудник", FieldType.PERSON),
    ("физическое лицо", FieldType.PERSON),
    ("инн", FieldType.INN),
    ("инн организации", FieldType.INN),
    ("инн контрагента", FieldType.INN),
    ("кпп", FieldType.KPP),
    ("кпп организации", FieldType.KPP),
    ("кпп контрагента", FieldType.KPP),
    ("огрн", FieldType.OGRN),
    ("огрнип", FieldType.OGRN),
    ("огрн организации", FieldType.OGRN),
    ("телефон", FieldType.PHONE),
    ("номер телефона", FieldType.PHONE),
    ("мобильный телефон", FieldType.PHONE),
    ("email", FieldType.EMAIL),
    ("e-mail", FieldType.EMAIL),
    ("электронная почта", FieldType.EMAIL),
    ("адрес электронной почты", FieldType.EMAIL),
    ("адрес", FieldType.ADDRESS),
    ("юридический адрес", FieldType.ADDRESS),
    ("фактический адрес", FieldType.ADDRESS),
    ("почтовый адрес", FieldType.ADDRESS),
    ("выручка", FieldType.FINANCIAL),
    ("оборот", FieldType.FINANCIAL),
    ("доход", FieldType.FINANCIAL),
    ("расходы", FieldType.FINANCIAL),
    ("маржинальность", FieldType.FINANCIAL),
    ("прибыль", FieldType.FINANCIAL),
]


@pytest.mark.parametrize("header,expected", _COLUMN_HEADER_EXAMPLES)
def test_detect_column_type_supported_headers(header: str, expected: FieldType) -> None:
    assert detect_column_type(header) is expected


def test_detect_column_type_is_case_insensitive() -> None:
    assert detect_column_type("ИНН") is FieldType.INN
    assert detect_column_type("Организация") is FieldType.COMPANY
    assert detect_column_type("E-MAIL") is FieldType.EMAIL


def test_detect_column_type_strips_outer_whitespace() -> None:
    assert detect_column_type("   инн   ") is FieldType.INN


def test_detect_column_type_collapses_internal_whitespace() -> None:
    assert detect_column_type("структурное   подразделение") is FieldType.DEPARTMENT
    assert detect_column_type("наименование\tорганизации") is FieldType.COMPANY


def test_normalize_column_name_applies_nfc() -> None:
    composed = "й"  # "й" в предкомпонованной форме
    decomposed = "й"  # "и" + комбинирующая краткая — та же буква
    assert _normalize_column_name(composed) == _normalize_column_name(decomposed)


def test_detect_column_type_unknown_header() -> None:
    assert detect_column_type("Комментарий") is FieldType.UNKNOWN
    assert detect_column_type("Примечание") is FieldType.UNKNOWN


def test_detect_column_type_rejects_non_str() -> None:
    assert detect_column_type(None) is FieldType.UNKNOWN
    assert detect_column_type(123) is FieldType.UNKNOWN
    assert detect_column_type(["инн"]) is FieldType.UNKNOWN


def test_detect_column_type_no_substring_false_positives() -> None:
    assert detect_column_type("Комментарий про ИНН") is FieldType.UNKNOWN
    assert detect_column_type("Полное наименование организации-партнёра") is FieldType.UNKNOWN
    assert detect_column_type("ИНН/КПП") is FieldType.UNKNOWN


def test_detect_column_type_never_returns_none() -> None:
    for header in ("инн", "неизвестно", None, 42):
        result = detect_column_type(header)
        assert result is not None
        assert isinstance(result, FieldType)


# ---------------------------------------------------------------------------
# Guard: архитектурное решение Stage 5 (FieldType.OGRNIP не добавлен)
# ---------------------------------------------------------------------------


def test_field_type_has_no_separate_ogrnip_member() -> None:
    """
    Явное решение ТЗ Stage 5: ОГРНИП сознательно не получает отдельного
    FieldType и классифицируется как FieldType.OGRN (см. docstring
    app.detectors). Этот тест документирует и защищает предположение,
    от которого зависит detect_value_type/detect_column_type.
    """
    assert not hasattr(FieldType, "OGRNIP")
