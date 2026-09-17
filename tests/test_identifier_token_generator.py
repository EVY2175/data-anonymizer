"""
Тесты генератора identifier token (app.security.alias_generator,
Stage 7B.2): generate_identifier_token.

Не тестируется здесь: IdentifierMappingStore/lookup-семантика (Stage 7B.3),
domain-валидация значений идентификаторов (Stage 7B.5) — этот генератор
вообще не принимает и не видит реальное значение идентификатора.

Случайность не тестируется статистически (никаких "сгенерировать 100 и
проверить, что все разные") — коллизии/exhaustion проверяются через
monkeypatch и маленький alphabet, в стиле tests/test_alias_generator.py.
"""

from __future__ import annotations

import inspect
import re
import secrets

import pytest

from app.detectors import is_valid_inn, is_valid_kpp, is_valid_ogrn, is_valid_ogrnip
from app.models.entities import EntityType
from app.models.identifiers import IdentifierType
from app.security.alias_generator import (
    PRODUCTION_ALPHABET,
    PRODUCTION_RANDOM_LENGTH,
    IdentifierTokenSpaceExhaustedError,
    generate_alias,
    generate_identifier_token,
)

_EXPECTED_PREFIXES = {
    IdentifierType.INN: "INN",
    IdentifierType.KPP: "KPP",
    IdentifierType.OGRN: "OGRN",
}


# ---------------------------------------------------------------------------
# Формат token: prefix + alphabet + length
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("identifier_type", list(IdentifierType))
def test_token_matches_prefix_and_alphabet(identifier_type: IdentifierType) -> None:
    token = generate_identifier_token(identifier_type, existing_tokens=set())
    prefix = _EXPECTED_PREFIXES[identifier_type]
    pattern = re.compile(
        rf"^{re.escape(prefix)}_[{re.escape(PRODUCTION_ALPHABET)}]{{{PRODUCTION_RANDOM_LENGTH}}}$"
    )
    assert pattern.match(token), f"token {token!r} не соответствует формату для {identifier_type}"


def test_inn_prefix_is_exactly_inn() -> None:
    token = generate_identifier_token(IdentifierType.INN, existing_tokens=set())
    assert token.startswith("INN_")


def test_kpp_prefix_is_exactly_kpp() -> None:
    token = generate_identifier_token(IdentifierType.KPP, existing_tokens=set())
    assert token.startswith("KPP_")


def test_ogrn_prefix_is_exactly_ogrn() -> None:
    token = generate_identifier_token(IdentifierType.OGRN, existing_tokens=set())
    assert token.startswith("OGRN_")


def test_no_ogrnip_prefix_namespace() -> None:
    assert not hasattr(IdentifierType, "OGRNIP")
    # ОГРН и ОГРНИП оба относятся к IdentifierType.OGRN — отдельного
    # префикса "OGRNIP_" не существует и не может быть сгенерирован.
    token = generate_identifier_token(IdentifierType.OGRN, existing_tokens=set())
    assert not token.startswith("OGRNIP_")


def test_production_random_part_length_is_nine() -> None:
    token = generate_identifier_token(IdentifierType.INN, existing_tokens=set())
    random_part = token.split("_", 1)[1]
    assert len(random_part) == 9 == PRODUCTION_RANDOM_LENGTH


def test_production_alphabet_used() -> None:
    token = generate_identifier_token(IdentifierType.INN, existing_tokens=set())
    random_part = token.split("_", 1)[1]
    assert all(char in PRODUCTION_ALPHABET for char in random_part)


def test_module_does_not_use_random_module() -> None:
    import app.security.alias_generator as module

    source = inspect.getsource(module)
    assert "import random" not in source
    assert "\nrandom." not in source
    assert " random." not in source


# ---------------------------------------------------------------------------
# Collision retry / exhaustion
# ---------------------------------------------------------------------------


def test_collision_triggers_retry_and_does_not_return_existing_token(monkeypatch) -> None:
    alphabet = "AB"
    random_length = 1
    existing = {"INN_A"}  # единственное занятое значение в keyspace {A, B}

    sequence = iter(["A", "B"])  # первый выбор коллизирует, второй свободен
    calls = []

    def fake_choice(seq):
        calls.append(seq)
        return next(sequence)

    monkeypatch.setattr(secrets, "choice", fake_choice)

    token = generate_identifier_token(
        IdentifierType.INN,
        existing_tokens=existing,
        alphabet=alphabet,
        random_length=random_length,
    )

    assert token == "INN_B"
    assert token not in existing
    assert len(calls) == 2  # подтверждает retry


def test_exhaustion_raises_explicit_exception_not_infinite_loop() -> None:
    alphabet = "A"
    random_length = 1
    existing = {"INN_A"}  # единственное возможное значение уже занято

    with pytest.raises(IdentifierTokenSpaceExhaustedError):
        generate_identifier_token(
            IdentifierType.INN,
            existing_tokens=existing,
            alphabet=alphabet,
            random_length=random_length,
        )


def test_exhaustion_after_filling_entire_small_keyspace() -> None:
    alphabet = "AB"
    random_length = 1
    existing: set[str] = set()

    for _ in range(2):
        token = generate_identifier_token(
            IdentifierType.KPP,
            existing_tokens=existing,
            alphabet=alphabet,
            random_length=random_length,
        )
        existing.add(token)

    assert existing == {"KPP_A", "KPP_B"}

    with pytest.raises(IdentifierTokenSpaceExhaustedError):
        generate_identifier_token(
            IdentifierType.KPP,
            existing_tokens=existing,
            alphabet=alphabet,
            random_length=random_length,
        )


def test_returned_token_is_never_in_existing_tokens() -> None:
    existing = {"INN_AAAAAAAAA", "INN_BBBBBBBBB"}
    token = generate_identifier_token(IdentifierType.INN, existing_tokens=existing)
    assert token not in existing


# ---------------------------------------------------------------------------
# Валидация входных параметров
# ---------------------------------------------------------------------------


def test_invalid_identifier_type_raises_value_error() -> None:
    with pytest.raises(ValueError):
        generate_identifier_token("not-an-identifier-type", existing_tokens=set())  # type: ignore[arg-type]


def test_entity_type_is_not_accepted_as_identifier_type() -> None:
    """Публичный API принимает IdentifierType, а не FieldType/EntityType/str prefix."""
    with pytest.raises(ValueError):
        generate_identifier_token(EntityType.COMPANY, existing_tokens=set())  # type: ignore[arg-type]


def test_invalid_alphabet_raises_value_error() -> None:
    with pytest.raises(ValueError):
        generate_identifier_token(IdentifierType.INN, existing_tokens=set(), alphabet="AAB")
    with pytest.raises(ValueError):
        generate_identifier_token(IdentifierType.INN, existing_tokens=set(), alphabet="")


def test_invalid_random_length_raises_value_error() -> None:
    with pytest.raises(ValueError):
        generate_identifier_token(IdentifierType.INN, existing_tokens=set(), random_length=0)
    with pytest.raises(ValueError):
        generate_identifier_token(IdentifierType.INN, existing_tokens=set(), random_length=-1)


def test_public_api_has_no_way_to_pass_arbitrary_prefix() -> None:
    sig = inspect.signature(generate_identifier_token)
    assert "prefix" not in sig.parameters


def test_public_api_has_no_dangerous_identifier_value_parameter() -> None:
    """
    Архитектурная гарантия: у generate_identifier_token нет параметра,
    через который можно было бы передать реальное значение
    идентификатора — значит token не может зависеть от него ни при каких
    обстоятельствах. Проверяем именно отсутствие конкретных "опасных"
    имён, а не фиксируем весь набор параметров функции — иначе тест
    ломался бы при любом безобидном будущем расширении сигнатуры.
    """
    sig = inspect.signature(generate_identifier_token)
    for dangerous_name in ("identifier_value", "real_value", "value"):
        assert dangerous_name not in sig.parameters


# ---------------------------------------------------------------------------
# Namespace separation: identifier tokens vs entity aliases
# ---------------------------------------------------------------------------


def test_identifier_token_prefixes_do_not_overlap_entity_alias_prefixes() -> None:
    entity_prefixes = {entity_type.alias_prefix for entity_type in EntityType}
    identifier_prefixes = set(_EXPECTED_PREFIXES.values())
    assert entity_prefixes.isdisjoint(identifier_prefixes)


def test_generated_identifier_tokens_and_entity_aliases_are_distinguishable() -> None:
    for entity_type in EntityType:
        alias = generate_alias(entity_type, existing_aliases=set())
        for identifier_type in IdentifierType:
            token = generate_identifier_token(identifier_type, existing_tokens=set())
            assert alias.split("_", 1)[0] != token.split("_", 1)[0]


# ---------------------------------------------------------------------------
# Cross-layer invariant: token никогда не проходит Stage 5 validators
# ---------------------------------------------------------------------------


def test_generated_tokens_fail_all_stage5_identifier_validators() -> None:
    for identifier_type in IdentifierType:
        token = generate_identifier_token(identifier_type, existing_tokens=set())
        assert is_valid_inn(token) is False
        assert is_valid_kpp(token) is False
        assert is_valid_ogrn(token) is False
        assert is_valid_ogrnip(token) is False
