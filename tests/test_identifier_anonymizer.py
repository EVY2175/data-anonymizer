"""
Тесты identifier tokenization integration в app.anonymizer (Stage 7B.5).

FlatTable-фикстуры строятся напрямую через CellRecord/FlatTable (без
openpyxl), как и в tests/test_anonymizer.py. Существующее Stage 7A
entity-поведение (COMPANY/PERSON/KEEP/REMOVE) остаётся полностью в
tests/test_anonymizer.py и НЕ дублируется здесь, кроме минимально
необходимых mixed-table/backward-compatibility сценариев.

Синтетические, но checksum-валидные ИНН/КПП/ОГРН/ОГРНИП (никаких
реальных персональных данных):
    VALID_INN               = "7707083893"   (10 цифр, ИНН юрлица)
    VALID_INN_2              = "7707083886"   (10 цифр, другая identity)
    VALID_INN_3              = "7707083910"   (10 цифр, третья identity)
    VALID_INN_LEADING_ZERO   = "0770107391"   (10 цифр, начинается с 0)
    VALID_INN12              = "500100732259" (12 цифр, ИНН физлица/ИП)
    VALID_KPP                = "770701001"    (9 символов, только цифры)
    VALID_KPP_WITH_LETTER    = "7707AB001"    (9 символов, с буквами)
    VALID_OGRN               = "1047709000008" (13 цифр)
    VALID_OGRNIP              = "304770900000126" (15 цифр)
"""

from __future__ import annotations

import datetime
import os
import secrets as secrets_module
from pathlib import Path

import pytest

import app.anonymizer as anonymizer_module
from app.anonymizer import (
    FormulaPseudonymizationError,
    InvalidIdentifierValueError,
    MissingIdentifierStoreError,
    anonymize_flat_table,
)
from app.excel.models import CellRecord, FlatTable
from app.mapping.encrypted_identifier_file import EncryptedFileIdentifierMappingStore
from app.mapping.identifier_base import IdentifierMappingConflictError
from app.mapping.identifier_memory import InMemoryIdentifierMappingStore
from app.mapping.memory import InMemoryMappingStore
from app.models.identifiers import IdentifierMappingEntry, IdentifierType
from app.models.rules import Action, FieldRule, FieldType
from app.security.alias_generator import PRODUCTION_RANDOM_LENGTH

VALID_INN = "7707083893"
VALID_INN_2 = "7707083886"
VALID_INN_3 = "7707083910"
VALID_INN_LEADING_ZERO = "0770107391"
VALID_INN12 = "500100732259"
VALID_KPP = "770701001"
VALID_KPP_WITH_LETTER = "7707AB001"
VALID_OGRN = "1047709000008"
VALID_OGRNIP = "304770900000126"

PASSWORD = "test-password-123"

# ---------------------------------------------------------------------------
# Фикстуры/помощники
# ---------------------------------------------------------------------------


def _table(sheet_name: str, headers: list[object], rows: list[list[object]]) -> FlatTable:
    header_row = tuple(CellRecord(row=1, column=i + 1, value=h) for i, h in enumerate(headers))
    data_rows = tuple(
        tuple(CellRecord(row=r_idx + 2, column=c_idx + 1, value=v) for c_idx, v in enumerate(row))
        for r_idx, row in enumerate(rows)
    )
    return FlatTable(sheet_name=sheet_name, header_row=header_row, rows=data_rows)


def _rule(column_name: str, field_type: FieldType, action: Action) -> FieldRule:
    return FieldRule(column_name=column_name, field_type=field_type, action=action)


def _identifier_rule(
    column_name: str, field_type: FieldType, action: Action = Action.PSEUDONYMIZE
) -> FieldRule:
    return _rule(column_name, field_type, action)


@pytest.fixture()
def target_path(tmp_path: Path) -> Path:
    return tmp_path / "identifiers.enc"


# ---------------------------------------------------------------------------
# A-B. API backward compatibility / keyword-only identifier_store
# ---------------------------------------------------------------------------


def test_backward_compatible_call_without_identifier_store_still_works() -> None:
    table = _table("S", ["Компания"], [["ООО Ромашка"]])
    rules = {1: _rule("Компания", FieldType.COMPANY, Action.PSEUDONYMIZE)}
    result = anonymize_flat_table(table, rules, InMemoryMappingStore())
    assert result.rows[0][0].value.startswith("C_")


def test_identifier_store_is_keyword_only() -> None:
    table = _table("S", ["A"], [["x"]])
    rules = {1: _rule("A", FieldType.UNKNOWN, Action.KEEP)}
    with pytest.raises(TypeError):
        anonymize_flat_table(table, rules, InMemoryMappingStore(), None)  # type: ignore[misc]


def test_identifier_store_default_is_none_via_keyword() -> None:
    table = _table("S", ["A"], [["x"]])
    rules = {1: _rule("A", FieldType.UNKNOWN, Action.KEEP)}
    result = anonymize_flat_table(table, rules, InMemoryMappingStore(), identifier_store=None)
    assert result.rows[0][0].value == "x"


# ---------------------------------------------------------------------------
# C-D. MissingIdentifierStoreError
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("field_type", [FieldType.INN, FieldType.KPP, FieldType.OGRN])
def test_missing_identifier_store_raises(field_type: FieldType) -> None:
    table = _table("S", ["X"], [[VALID_INN]])
    rules = {1: _identifier_rule("X", field_type)}
    with pytest.raises(MissingIdentifierStoreError):
        anonymize_flat_table(table, rules, InMemoryMappingStore())


def test_missing_identifier_store_fails_before_row_processing_and_entity_mutation() -> None:
    mapping_store = InMemoryMappingStore()
    table = _table(
        "S",
        ["Компания", "ИНН"],
        [["ООО Ромашка", VALID_INN]],
    )
    rules = {
        1: _rule("Компания", FieldType.COMPANY, Action.PSEUDONYMIZE),
        2: _identifier_rule("ИНН", FieldType.INN),
    }
    with pytest.raises(MissingIdentifierStoreError):
        anonymize_flat_table(table, rules, mapping_store)
    assert mapping_store.entries() == ()


# ---------------------------------------------------------------------------
# E-I. Valid INN/KPP/OGRN/OGRNIP success + correct prefixes
# ---------------------------------------------------------------------------


def test_valid_inn_produces_inn_prefixed_token() -> None:
    table = _table("S", ["ИНН"], [[VALID_INN]])
    rules = {1: _identifier_rule("ИНН", FieldType.INN)}
    result = anonymize_flat_table(
        table, rules, InMemoryMappingStore(), identifier_store=InMemoryIdentifierMappingStore()
    )
    assert result.rows[0][0].value.startswith("INN_")


def test_valid_kpp_produces_kpp_prefixed_token() -> None:
    table = _table("S", ["КПП"], [[VALID_KPP]])
    rules = {1: _identifier_rule("КПП", FieldType.KPP)}
    result = anonymize_flat_table(
        table, rules, InMemoryMappingStore(), identifier_store=InMemoryIdentifierMappingStore()
    )
    assert result.rows[0][0].value.startswith("KPP_")


def test_valid_ogrn13_produces_ogrn_prefixed_token() -> None:
    table = _table("S", ["ОГРН"], [[VALID_OGRN]])
    rules = {1: _identifier_rule("ОГРН", FieldType.OGRN)}
    result = anonymize_flat_table(
        table, rules, InMemoryMappingStore(), identifier_store=InMemoryIdentifierMappingStore()
    )
    assert result.rows[0][0].value.startswith("OGRN_")


def test_valid_ogrnip15_through_ogrn_field_type_produces_ogrn_prefixed_token() -> None:
    table = _table("S", ["ОГРН"], [[VALID_OGRNIP]])
    rules = {1: _identifier_rule("ОГРН", FieldType.OGRN)}
    result = anonymize_flat_table(
        table, rules, InMemoryMappingStore(), identifier_store=InMemoryIdentifierMappingStore()
    )
    assert result.rows[0][0].value.startswith("OGRN_")


def test_valid_kpp_with_letters_accepted() -> None:
    table = _table("S", ["КПП"], [[VALID_KPP_WITH_LETTER]])
    rules = {1: _identifier_rule("КПП", FieldType.KPP)}
    result = anonymize_flat_table(
        table, rules, InMemoryMappingStore(), identifier_store=InMemoryIdentifierMappingStore()
    )
    assert result.rows[0][0].value.startswith("KPP_")


# ---------------------------------------------------------------------------
# J-L. Token reuse / repeated identity / pending batch
# ---------------------------------------------------------------------------


def test_existing_token_reused_no_new_pending_entry() -> None:
    identifier_store = InMemoryIdentifierMappingStore()
    existing = IdentifierMappingEntry(
        token="INN_PRESEEDED1", identifier_value=VALID_INN, identifier_type=IdentifierType.INN
    )
    identifier_store.add(existing)

    table = _table("S", ["ИНН"], [[VALID_INN]])
    rules = {1: _identifier_rule("ИНН", FieldType.INN)}
    result = anonymize_flat_table(
        table, rules, InMemoryMappingStore(), identifier_store=identifier_store
    )
    assert result.rows[0][0].value == "INN_PRESEEDED1"
    assert len(identifier_store.entries()) == 1


def test_repeated_same_identity_within_table_gets_one_token() -> None:
    identifier_store = InMemoryIdentifierMappingStore()
    table = _table("S", ["ИНН"], [[VALID_INN], [VALID_INN], [VALID_INN]])
    rules = {1: _identifier_rule("ИНН", FieldType.INN)}
    result = anonymize_flat_table(
        table, rules, InMemoryMappingStore(), identifier_store=identifier_store
    )
    tokens = {row[0].value for row in result.rows}
    assert len(tokens) == 1
    assert len(identifier_store.entries()) == 1


def test_pending_batch_contains_one_entry_for_repeated_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    identifier_store = InMemoryIdentifierMappingStore()
    captured: dict[str, tuple] = {}
    real_add_many = InMemoryIdentifierMappingStore.add_many

    def counting_add_many(self, entries):
        captured["entries"] = tuple(entries)
        return real_add_many(self, captured["entries"])

    monkeypatch.setattr(InMemoryIdentifierMappingStore, "add_many", counting_add_many)

    table = _table("S", ["ИНН"], [[VALID_INN], [VALID_INN]])
    rules = {1: _identifier_rule("ИНН", FieldType.INN)}
    anonymize_flat_table(table, rules, InMemoryMappingStore(), identifier_store=identifier_store)

    assert len(captured["entries"]) == 1
    assert captured["entries"][0].identifier_value == VALID_INN


# ---------------------------------------------------------------------------
# M. Same raw value under different IdentifierType stays independent
# ---------------------------------------------------------------------------


def test_same_raw_value_different_identifier_type_remains_independent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    ИНН/КПП/ОГРН имеют попарно разные допустимые длины, поэтому реальное
    checksum-валидное значение никогда не совпадает буквально между двумя
    типами. Чтобы проверить сам identity-механизм (identifier_type — часть
    identity), валидаторы временно подменяются на "всегда True" — это
    тестовый приём для проверки архитектуры, а не production-сценарий.
    """
    monkeypatch.setattr(
        anonymizer_module,
        "_IDENTIFIER_VALIDATORS",
        {
            IdentifierType.INN: lambda value: True,
            IdentifierType.KPP: lambda value: True,
            IdentifierType.OGRN: lambda value: True,
        },
    )
    identifier_store = InMemoryIdentifierMappingStore()
    table = _table("S", ["ИНН", "КПП"], [["999999999", "999999999"]])
    rules = {
        1: _identifier_rule("ИНН", FieldType.INN),
        2: _identifier_rule("КПП", FieldType.KPP),
    }
    result = anonymize_flat_table(
        table, rules, InMemoryMappingStore(), identifier_store=identifier_store
    )
    inn_token = result.rows[0][0].value
    kpp_token = result.rows[0][1].value
    assert inn_token != kpp_token
    assert inn_token.startswith("INN_")
    assert kpp_token.startswith("KPP_")
    assert len(identifier_store.entries()) == 2


# ---------------------------------------------------------------------------
# N-Q. Exact-value semantics: leading zero, int/str convergence, whitespace
# ---------------------------------------------------------------------------


def test_leading_zero_string_preserved_exact() -> None:
    identifier_store = InMemoryIdentifierMappingStore()
    table = _table("S", ["ИНН"], [[VALID_INN_LEADING_ZERO]])
    rules = {1: _identifier_rule("ИНН", FieldType.INN)}
    anonymize_flat_table(table, rules, InMemoryMappingStore(), identifier_store=identifier_store)
    entry = identifier_store.entries()[0]
    assert entry.identifier_value == VALID_INN_LEADING_ZERO


def test_int_value_uses_str_no_padding() -> None:
    identifier_store = InMemoryIdentifierMappingStore()
    table = _table("S", ["ИНН"], [[int(VALID_INN)]])
    rules = {1: _identifier_rule("ИНН", FieldType.INN)}
    anonymize_flat_table(table, rules, InMemoryMappingStore(), identifier_store=identifier_store)
    entry = identifier_store.entries()[0]
    assert entry.identifier_value == str(int(VALID_INN))
    assert entry.identifier_value == VALID_INN


def test_int_and_string_same_value_converge_to_one_token() -> None:
    identifier_store = InMemoryIdentifierMappingStore()
    table = _table("S", ["ИНН"], [[VALID_INN], [int(VALID_INN)]])
    rules = {1: _identifier_rule("ИНН", FieldType.INN)}
    result = anonymize_flat_table(
        table, rules, InMemoryMappingStore(), identifier_store=identifier_store
    )
    assert result.rows[0][0].value == result.rows[1][0].value
    assert len(identifier_store.entries()) == 1


def test_outer_whitespace_preserved_as_distinct_identity() -> None:
    identifier_store = InMemoryIdentifierMappingStore()
    padded = f" {VALID_INN} "
    table = _table("S", ["ИНН"], [[VALID_INN], [padded]])
    rules = {1: _identifier_rule("ИНН", FieldType.INN)}
    result = anonymize_flat_table(
        table, rules, InMemoryMappingStore(), identifier_store=identifier_store
    )
    assert result.rows[0][0].value != result.rows[1][0].value
    assert len(identifier_store.entries()) == 2
    values = {e.identifier_value for e in identifier_store.entries()}
    assert values == {VALID_INN, padded}


# ---------------------------------------------------------------------------
# R-T. Empty semantics
# ---------------------------------------------------------------------------


def test_whitespace_only_passthrough_exact() -> None:
    identifier_store = InMemoryIdentifierMappingStore()
    table = _table("S", ["ИНН"], [["   "]])
    rules = {1: _identifier_rule("ИНН", FieldType.INN)}
    result = anonymize_flat_table(
        table, rules, InMemoryMappingStore(), identifier_store=identifier_store
    )
    assert result.rows[0][0].value == "   "
    assert identifier_store.entries() == ()


def test_none_passthrough() -> None:
    identifier_store = InMemoryIdentifierMappingStore()
    table = _table("S", ["ИНН"], [[None]])
    rules = {1: _identifier_rule("ИНН", FieldType.INN)}
    result = anonymize_flat_table(
        table, rules, InMemoryMappingStore(), identifier_store=identifier_store
    )
    assert result.rows[0][0].value is None
    assert identifier_store.entries() == ()


def test_empty_string_passthrough() -> None:
    identifier_store = InMemoryIdentifierMappingStore()
    table = _table("S", ["ИНН"], [[""]])
    rules = {1: _identifier_rule("ИНН", FieldType.INN)}
    result = anonymize_flat_table(
        table, rules, InMemoryMappingStore(), identifier_store=identifier_store
    )
    assert result.rows[0][0].value == ""
    assert identifier_store.entries() == ()


# ---------------------------------------------------------------------------
# U-W. Type rejection
# ---------------------------------------------------------------------------


def test_bool_rejected() -> None:
    identifier_store = InMemoryIdentifierMappingStore()
    table = _table("S", ["ИНН"], [[True]])
    rules = {1: _identifier_rule("ИНН", FieldType.INN)}
    with pytest.raises(InvalidIdentifierValueError):
        anonymize_flat_table(table, rules, InMemoryMappingStore(), identifier_store=identifier_store)
    assert identifier_store.entries() == ()


def test_float_rejected() -> None:
    identifier_store = InMemoryIdentifierMappingStore()
    table = _table("S", ["ИНН"], [[float(VALID_INN)]])
    rules = {1: _identifier_rule("ИНН", FieldType.INN)}
    with pytest.raises(InvalidIdentifierValueError):
        anonymize_flat_table(table, rules, InMemoryMappingStore(), identifier_store=identifier_store)
    assert identifier_store.entries() == ()


def test_datetime_rejected() -> None:
    identifier_store = InMemoryIdentifierMappingStore()
    table = _table("S", ["ИНН"], [[datetime.datetime(2024, 5, 1)]])
    rules = {1: _identifier_rule("ИНН", FieldType.INN)}
    with pytest.raises(InvalidIdentifierValueError):
        anonymize_flat_table(table, rules, InMemoryMappingStore(), identifier_store=identifier_store)
    assert identifier_store.entries() == ()


# ---------------------------------------------------------------------------
# X-Y. Invalid checksum / structure
# ---------------------------------------------------------------------------


def test_invalid_checksum_rejected() -> None:
    identifier_store = InMemoryIdentifierMappingStore()
    corrupted = VALID_INN[:9] + str((int(VALID_INN[9]) + 1) % 10)
    table = _table("S", ["ИНН"], [[corrupted]])
    rules = {1: _identifier_rule("ИНН", FieldType.INN)}
    with pytest.raises(InvalidIdentifierValueError):
        anonymize_flat_table(table, rules, InMemoryMappingStore(), identifier_store=identifier_store)
    assert identifier_store.entries() == ()


def test_invalid_structure_rejected() -> None:
    identifier_store = InMemoryIdentifierMappingStore()
    table = _table("S", ["ИНН"], [["12345"]])  # неверная длина
    rules = {1: _identifier_rule("ИНН", FieldType.INN)}
    with pytest.raises(InvalidIdentifierValueError):
        anonymize_flat_table(table, rules, InMemoryMappingStore(), identifier_store=identifier_store)
    assert identifier_store.entries() == ()


def test_kpp_invalid_structure_rejected() -> None:
    identifier_store = InMemoryIdentifierMappingStore()
    table = _table("S", ["КПП"], [["abcdefghi"]])  # не соответствует формату
    rules = {1: _identifier_rule("КПП", FieldType.KPP)}
    with pytest.raises(InvalidIdentifierValueError):
        anonymize_flat_table(table, rules, InMemoryMappingStore(), identifier_store=identifier_store)
    assert identifier_store.entries() == ()


# ---------------------------------------------------------------------------
# Z. Formula
# ---------------------------------------------------------------------------


def test_formula_raises_formula_error_not_identifier_error() -> None:
    identifier_store = InMemoryIdentifierMappingStore()
    table = _table("S", ["ИНН"], [["=SUM(A1:A2)"]])
    rules = {1: _identifier_rule("ИНН", FieldType.INN)}
    with pytest.raises(FormulaPseudonymizationError):
        anonymize_flat_table(table, rules, InMemoryMappingStore(), identifier_store=identifier_store)
    assert identifier_store.entries() == ()


# ---------------------------------------------------------------------------
# AA-AC. REMOVE/KEEP identifiers, identifier_store not read when unneeded
# ---------------------------------------------------------------------------


def test_remove_identifier_works_without_identifier_store() -> None:
    table = _table("S", ["ИНН"], [[VALID_INN]])
    rules = {1: _identifier_rule("ИНН", FieldType.INN, Action.REMOVE)}
    result = anonymize_flat_table(table, rules, InMemoryMappingStore())
    assert result.rows[0][0].value is None


def test_keep_identifier_works_without_identifier_store() -> None:
    table = _table("S", ["ИНН"], [[VALID_INN]])
    rules = {1: _identifier_rule("ИНН", FieldType.INN, Action.KEEP)}
    result = anonymize_flat_table(table, rules, InMemoryMappingStore())
    assert result.rows[0][0].value == VALID_INN


class _ExplodingIdentifierStore(InMemoryIdentifierMappingStore):
    """Тестовый double: любое обращение к all_tokens()/add_many() — ошибка."""

    def all_tokens(self):
        raise AssertionError("identifier_store.all_tokens() не должен вызываться")

    def add_many(self, entries):
        raise AssertionError("identifier_store.add_many() не должен вызываться")


def test_identifier_store_not_read_when_no_identifier_pseudonymize_rule() -> None:
    table = _table("S", ["Компания", "ИНН"], [["ООО Ромашка", VALID_INN]])
    rules = {
        1: _rule("Компания", FieldType.COMPANY, Action.PSEUDONYMIZE),
        2: _identifier_rule("ИНН", FieldType.INN, Action.KEEP),
    }
    result = anonymize_flat_table(
        table, rules, InMemoryMappingStore(), identifier_store=_ExplodingIdentifierStore()
    )
    assert result.rows[0][0].value.startswith("C_")
    assert result.rows[0][1].value == VALID_INN


# ---------------------------------------------------------------------------
# AD-AG. Call-count contracts (all_tokens/add_many)
# ---------------------------------------------------------------------------


def test_all_tokens_called_exactly_once(monkeypatch: pytest.MonkeyPatch) -> None:
    identifier_store = InMemoryIdentifierMappingStore()
    call_count = {"n": 0}
    original_all_tokens = identifier_store.all_tokens

    def counting_all_tokens():
        call_count["n"] += 1
        return original_all_tokens()

    monkeypatch.setattr(identifier_store, "all_tokens", counting_all_tokens)

    table = _table("S", ["ИНН"], [[VALID_INN], [VALID_INN_2], [VALID_INN]])
    rules = {1: _identifier_rule("ИНН", FieldType.INN)}
    anonymize_flat_table(table, rules, InMemoryMappingStore(), identifier_store=identifier_store)

    assert call_count["n"] == 1


def test_no_entries_scan_per_cell(monkeypatch: pytest.MonkeyPatch) -> None:
    identifier_store = InMemoryIdentifierMappingStore()

    def forbidden_entries():
        raise AssertionError("entries() не должен вызываться для lookup")

    monkeypatch.setattr(identifier_store, "entries", forbidden_entries)

    table = _table("S", ["ИНН"], [[VALID_INN], [VALID_INN_2]])
    rules = {1: _identifier_rule("ИНН", FieldType.INN)}
    # entries() используется ТОЛЬКО внутри add_many()/_clone_store() у
    # EncryptedFileIdentifierMappingStore — у InMemory add_many НЕ вызывает
    # entries() вообще (см. app/mapping/identifier_memory.py), поэтому
    # forbidden_entries не должен сработать здесь.
    anonymize_flat_table(table, rules, InMemoryMappingStore(), identifier_store=identifier_store)


def test_add_many_called_exactly_once_for_multiple_new_identities(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    identifier_store = InMemoryIdentifierMappingStore()
    call_count = {"n": 0}
    real_add_many = InMemoryIdentifierMappingStore.add_many

    def counting_add_many(self, entries):
        call_count["n"] += 1
        return real_add_many(self, entries)

    monkeypatch.setattr(InMemoryIdentifierMappingStore, "add_many", counting_add_many)

    table = _table("S", ["ИНН"], [[VALID_INN], [VALID_INN_2], [VALID_INN_3]])
    rules = {1: _identifier_rule("ИНН", FieldType.INN)}
    anonymize_flat_table(table, rules, InMemoryMappingStore(), identifier_store=identifier_store)

    assert call_count["n"] == 1
    assert len(identifier_store.entries()) == 3


def test_all_existing_identities_result_in_zero_add_many_calls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    identifier_store = InMemoryIdentifierMappingStore()
    identifier_store.add(
        IdentifierMappingEntry(
            token="INN_PRESEEDED1", identifier_value=VALID_INN, identifier_type=IdentifierType.INN
        )
    )
    call_count = {"n": 0}
    real_add_many = InMemoryIdentifierMappingStore.add_many

    def counting_add_many(self, entries):
        call_count["n"] += 1
        return real_add_many(self, entries)

    monkeypatch.setattr(InMemoryIdentifierMappingStore, "add_many", counting_add_many)

    table = _table("S", ["ИНН"], [[VALID_INN], [VALID_INN]])
    rules = {1: _identifier_rule("ИНН", FieldType.INN)}
    anonymize_flat_table(table, rules, InMemoryMappingStore(), identifier_store=identifier_store)

    assert call_count["n"] == 0


# ---------------------------------------------------------------------------
# AH. Pending collision prevention (used_tokens grows locally)
# ---------------------------------------------------------------------------


def test_used_tokens_grows_locally_across_new_identifiers(monkeypatch: pytest.MonkeyPatch) -> None:
    identifier_store = InMemoryIdentifierMappingStore()
    seen_snapshots: list[set] = []
    minted = iter(["INN_ONE", "INN_TWO", "INN_THREE"])

    def fake_generate_identifier_token(identifier_type, existing_tokens):
        seen_snapshots.append(set(existing_tokens))
        return next(minted)

    monkeypatch.setattr(anonymizer_module, "generate_identifier_token", fake_generate_identifier_token)

    table = _table("S", ["ИНН"], [[VALID_INN], [VALID_INN_2], [VALID_INN_3]])
    rules = {1: _identifier_rule("ИНН", FieldType.INN)}
    anonymize_flat_table(table, rules, InMemoryMappingStore(), identifier_store=identifier_store)

    assert seen_snapshots[0] == set()
    assert seen_snapshots[1] == {"INN_ONE"}
    assert seen_snapshots[2] == {"INN_ONE", "INN_TWO"}


def test_pending_token_collision_is_detected_and_resolved_via_real_generator(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    Усиленная версия AH: доказывает РЕАЛЬНЫЙ collision-handling contract
    generate_identifier_token (Stage 7B.2, production-код, НЕ подменённый
    monkeypatch) при работе с pending-состоянием Stage 7B.5.

    Две разные НОВЫЕ identity (обе отсутствуют в persisted identifier_store)
    обрабатываются последовательно в одной таблице. Подменяется только
    источник случайности (secrets.choice) на детерминированную
    последовательность символов — сам generate_identifier_token/
    _generate_with_prefix (retry-цикл, сравнение с existing) остаётся
    НЕТРОНУТЫМ production-кодом:

        1) 1-я identity: первая попытка генерации сразу успешна
           ("A"*9 -> token "INN_AAAAAAAAA"); anonymizer.py немедленно
           добавляет его в used_tokens (см. app/anonymizer.py:646).
        2) 2-я identity: ПЕРВАЯ попытка генерации намеренно производит ТОТ
           ЖЕ случайный "A"*9 -> кандидат "INN_AAAAAAAAA" — но он уже есть
           в used_tokens (пришёл из pending первой identity, ещё не
           персистентной!) -> production retry-цикл внутри
           _generate_with_prefix обнаруживает совпадение и делает вторую
           попытку -> "B"*9 -> token "INN_BBBBBBBBB", отсутствующий в
           existing -> возвращается.

    Это доказывает, что used_tokens, видимый второй генерацией, реально
    содержит token первой ЕЩЁ НЕ ПЕРСИСТЕНТНОЙ (pending) identity — если бы
    anonymizer.py не обновлял used_tokens сразу после первой генерации
    (или не передавал его тот же объект), коллизия осталась бы
    незамеченной и оба cell получили бы ОДИН И ТОТ ЖЕ token "INN_AAAAAAAAA".
    """
    consumed_random_chars = iter(
        ["A"] * PRODUCTION_RANDOM_LENGTH  # 1-я identity: попытка №1 — сразу успех
        + ["A"] * PRODUCTION_RANDOM_LENGTH  # 2-я identity: попытка №1 — коллизия с pending token
        + ["B"] * PRODUCTION_RANDOM_LENGTH  # 2-я identity: попытка №2 — успех после retry
    )

    def fake_choice(alphabet: str) -> str:
        return next(consumed_random_chars)

    monkeypatch.setattr(secrets_module, "choice", fake_choice)

    identifier_store = InMemoryIdentifierMappingStore()
    assert identifier_store.get_by_identity(IdentifierType.INN, VALID_INN) is None
    assert identifier_store.get_by_identity(IdentifierType.INN, VALID_INN_2) is None

    call_count = {"add_many": 0}
    real_add_many = InMemoryIdentifierMappingStore.add_many

    def counting_add_many(self, entries):
        call_count["add_many"] += 1
        return real_add_many(self, entries)

    monkeypatch.setattr(InMemoryIdentifierMappingStore, "add_many", counting_add_many)

    table = _table("S", ["ИНН"], [[VALID_INN], [VALID_INN_2]])
    rules = {1: _identifier_rule("ИНН", FieldType.INN)}

    result = anonymize_flat_table(
        table, rules, InMemoryMappingStore(), identifier_store=identifier_store
    )

    first_token = result.rows[0][0].value
    second_token = result.rows[1][0].value

    assert first_token == "INN_AAAAAAAAA"
    # Ключевая проверка: коллизия обнаружена и разрешена реальным retry —
    # НЕ "INN_AAAAAAAAA" повторно, несмотря на идентичный первый random draw.
    assert second_token == "INN_BBBBBBBBB"
    assert first_token != second_token

    entries_by_value = {e.identifier_value: e.token for e in identifier_store.entries()}
    assert entries_by_value[VALID_INN] == first_token
    assert entries_by_value[VALID_INN_2] == second_token
    assert len(identifier_store.entries()) == 2

    assert call_count["add_many"] == 1


# ---------------------------------------------------------------------------
# AI. IdentifierMappingConflictError propagation
# ---------------------------------------------------------------------------


def test_identifier_mapping_conflict_error_propagates(monkeypatch: pytest.MonkeyPatch) -> None:
    identifier_store = InMemoryIdentifierMappingStore()
    identifier_store.add(
        IdentifierMappingEntry(
            token="INN_FIXEDTOKEN", identifier_value=VALID_INN_2, identifier_type=IdentifierType.INN
        )
    )

    def fake_generate_identifier_token(identifier_type, existing_tokens):
        return "INN_FIXEDTOKEN"

    monkeypatch.setattr(anonymizer_module, "generate_identifier_token", fake_generate_identifier_token)

    table = _table("S", ["ИНН"], [[VALID_INN]])  # новая identity, но token уже занят другой
    rules = {1: _identifier_rule("ИНН", FieldType.INN)}

    with pytest.raises(IdentifierMappingConflictError):
        anonymize_flat_table(table, rules, InMemoryMappingStore(), identifier_store=identifier_store)

    assert len(identifier_store.entries()) == 1  # исходная запись не пострадала


# ---------------------------------------------------------------------------
# AJ. Encrypted persistence failure propagation
# ---------------------------------------------------------------------------


def test_encrypted_persistence_failure_propagates(
    target_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    identifier_store = EncryptedFileIdentifierMappingStore(target_path, PASSWORD)

    def failing_replace(*args: object, **kwargs: object) -> None:
        raise OSError("simulated os.replace failure")

    monkeypatch.setattr(os, "replace", failing_replace)

    table = _table("S", ["ИНН"], [[VALID_INN]])
    rules = {1: _identifier_rule("ИНН", FieldType.INN)}

    with pytest.raises(OSError):
        anonymize_flat_table(table, rules, InMemoryMappingStore(), identifier_store=identifier_store)

    monkeypatch.undo()
    assert not target_path.exists()
    assert identifier_store.entries() == ()


# ---------------------------------------------------------------------------
# AK-AM. Late failure rollback / entity-identifier asymmetry
# ---------------------------------------------------------------------------


def test_late_identifier_validation_failure_leaves_identifier_store_unchanged() -> None:
    identifier_store = InMemoryIdentifierMappingStore()
    table = _table("S", ["ИНН"], [[VALID_INN], ["not-a-valid-inn"]])
    rules = {1: _identifier_rule("ИНН", FieldType.INN)}

    with pytest.raises(InvalidIdentifierValueError):
        anonymize_flat_table(table, rules, InMemoryMappingStore(), identifier_store=identifier_store)

    assert identifier_store.entries() == ()


def test_late_entity_failure_after_earlier_pending_identifier_leaves_identifier_store_unchanged() -> None:
    identifier_store = InMemoryIdentifierMappingStore()
    mapping_store = InMemoryMappingStore()
    table = _table(
        "S",
        ["ИНН", "Компания"],
        [
            [VALID_INN, "ООО Ромашка"],
            [VALID_INN_2, 42],  # невалидное значение под COMPANY
        ],
    )
    rules = {
        1: _identifier_rule("ИНН", FieldType.INN),
        2: _rule("Компания", FieldType.COMPANY, Action.PSEUDONYMIZE),
    }

    with pytest.raises(anonymizer_module.InvalidPseudonymizationValueError):
        anonymize_flat_table(table, rules, mapping_store, identifier_store=identifier_store)

    assert identifier_store.entries() == ()


def test_entity_identifier_asymmetry_early_entity_survives_pending_identifier_does_not() -> None:
    """
    Явная проверка задокументированной асимметрии: ранняя entity-запись
    переживает позднюю ошибку, а pending identifier-запись — нет.
    """
    identifier_store = InMemoryIdentifierMappingStore()
    mapping_store = InMemoryMappingStore()
    table = _table(
        "S",
        ["Компания", "ИНН"],
        [
            ["ООО Ромашка", VALID_INN],  # первая строка полностью валидна
            [42, VALID_INN_2],  # вторая строка: невалидная COMPANY
        ],
    )
    rules = {
        1: _rule("Компания", FieldType.COMPANY, Action.PSEUDONYMIZE),
        2: _identifier_rule("ИНН", FieldType.INN),
    }

    with pytest.raises(anonymizer_module.InvalidPseudonymizationValueError):
        anonymize_flat_table(table, rules, mapping_store, identifier_store=identifier_store)

    assert len(mapping_store.entries()) == 1
    assert mapping_store.entries()[0].real_value == "ООО Ромашка"
    assert identifier_store.entries() == ()


# ---------------------------------------------------------------------------
# AN-AP. Confidentiality
# ---------------------------------------------------------------------------


def test_invalid_identifier_value_error_does_not_expose_raw_value() -> None:
    identifier_store = InMemoryIdentifierMappingStore()
    secret_looking_value = "SuperSecretRawIdentifierValue999"
    table = _table("S", ["ИНН"], [[secret_looking_value]])
    rules = {1: _identifier_rule("ИНН", FieldType.INN)}

    with pytest.raises(InvalidIdentifierValueError) as exc_info:
        anonymize_flat_table(table, rules, InMemoryMappingStore(), identifier_store=identifier_store)

    assert secret_looking_value not in str(exc_info.value)


def test_invalid_identifier_value_error_does_not_expose_sheet_name() -> None:
    identifier_store = InMemoryIdentifierMappingStore()
    distinctive_sheet_name = "КлиентКонфиденциальныйЛист"
    table = _table(distinctive_sheet_name, ["ИНН"], [["not-a-valid-inn"]])
    rules = {1: _identifier_rule("ИНН", FieldType.INN)}

    with pytest.raises(InvalidIdentifierValueError) as exc_info:
        anonymize_flat_table(table, rules, InMemoryMappingStore(), identifier_store=identifier_store)

    assert distinctive_sheet_name not in str(exc_info.value)


def test_missing_identifier_store_error_contains_no_cell_data() -> None:
    secret_looking_value = "SuperSecretRawIdentifierValue999"
    table = _table("S", ["ИНН"], [[secret_looking_value]])
    rules = {1: _identifier_rule("ИНН", FieldType.INN)}

    with pytest.raises(MissingIdentifierStoreError) as exc_info:
        anonymize_flat_table(table, rules, InMemoryMappingStore())

    message = str(exc_info.value)
    assert secret_looking_value not in message
    assert "S" != message  # sanity: сообщение — не просто sheet_name


# ---------------------------------------------------------------------------
# AQ-AS. Structure / mixed-table / existing behavior parity
# ---------------------------------------------------------------------------


def test_input_flat_table_is_not_mutated_with_identifier_column() -> None:
    table = _table("S", ["ИНН"], [[VALID_INN], [VALID_INN_2]])
    original_rows = table.rows
    rules = {1: _identifier_rule("ИНН", FieldType.INN)}
    anonymize_flat_table(
        table, rules, InMemoryMappingStore(), identifier_store=InMemoryIdentifierMappingStore()
    )
    assert table.rows == original_rows


def test_mixed_entity_identifier_remove_keep_table() -> None:
    mapping_store = InMemoryMappingStore()
    identifier_store = InMemoryIdentifierMappingStore()
    table = _table(
        "S",
        ["Компания", "ФИО", "ИНН", "КПП", "ОГРН", "Телефон", "Заметка"],
        [["ООО Ромашка", "Иванов Иван Иванович", VALID_INN, VALID_KPP, VALID_OGRN, "+7900", "оставить"]],
    )
    rules = {
        1: _rule("Компания", FieldType.COMPANY, Action.PSEUDONYMIZE),
        2: _rule("ФИО", FieldType.PERSON, Action.PSEUDONYMIZE),
        3: _identifier_rule("ИНН", FieldType.INN),
        4: _identifier_rule("КПП", FieldType.KPP),
        5: _identifier_rule("ОГРН", FieldType.OGRN),
        6: _rule("Телефон", FieldType.PHONE, Action.REMOVE),
        7: _rule("Заметка", FieldType.UNKNOWN, Action.KEEP),
    }
    result = anonymize_flat_table(table, rules, mapping_store, identifier_store=identifier_store)
    row = result.rows[0]

    assert row[0].value.startswith("C_")
    assert row[1].value.startswith("P_")
    assert row[2].value.startswith("INN_")
    assert row[3].value.startswith("KPP_")
    assert row[4].value.startswith("OGRN_")
    assert row[5].value is None
    assert row[6].value == "оставить"

    assert len(mapping_store.entries()) == 2
    assert len(identifier_store.entries()) == 3


def test_existing_stage_7a_company_behavior_remains_intact() -> None:
    """Смоук-проверка: базовый Stage 7A сценарий не задет интеграцией."""
    table = _table("S", ["Компания"], [["ООО Ромашка"], ["ООО Ромашка"], ["ООО Лютик"]])
    rules = {1: _rule("Компания", FieldType.COMPANY, Action.PSEUDONYMIZE)}
    result = anonymize_flat_table(table, rules, InMemoryMappingStore())
    assert result.rows[0][0].value == result.rows[1][0].value
    assert result.rows[0][0].value != result.rows[2][0].value
