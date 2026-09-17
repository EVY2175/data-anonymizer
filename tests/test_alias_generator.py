"""
Тесты генератора псевдонимов (app.security.alias_generator).
"""

from __future__ import annotations

import inspect
import re
import secrets

import pytest

from app.models.entities import EntityType
from app.security.alias_generator import (
    PRODUCTION_ALPHABET,
    PRODUCTION_RANDOM_LENGTH,
    AliasSpaceExhaustedError,
    _MAX_ATTEMPTS,
    generate_alias,
)


# ---------------------------------------------------------------------------
# Формат alias
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("entity_type", list(EntityType))
def test_alias_matches_prefix_and_alphabet(entity_type: EntityType) -> None:
    alias = generate_alias(entity_type, existing_aliases=set())
    pattern = re.compile(
        rf"^{re.escape(entity_type.alias_prefix)}_[{re.escape(PRODUCTION_ALPHABET)}]"
        rf"{{{PRODUCTION_RANDOM_LENGTH}}}$"
    )
    assert pattern.match(alias), f"alias {alias!r} не соответствует формату для {entity_type}"


def test_production_alphabet_has_32_unique_symbols_without_ambiguous_chars() -> None:
    assert len(PRODUCTION_ALPHABET) == 32
    assert len(set(PRODUCTION_ALPHABET)) == 32
    for forbidden in ("I", "L", "O", "U"):
        assert forbidden not in PRODUCTION_ALPHABET


def test_production_random_length_is_nine() -> None:
    assert PRODUCTION_RANDOM_LENGTH == 9


def test_module_does_not_use_random_module() -> None:
    """
    Требование архитектуры: криптографически безопасная случайность
    только через secrets, random не используется вообще.
    """
    import app.security.alias_generator as module

    source = inspect.getsource(module)
    assert "import random" not in source
    assert "\nrandom." not in source
    assert " random." not in source


# ---------------------------------------------------------------------------
# Массовая генерация без дублей
# ---------------------------------------------------------------------------

def test_bulk_generation_has_no_duplicates() -> None:
    """
    Десятки тысяч генераций с production-параметрами — без единого дубля.
    """
    existing: set[str] = set()
    count = 30_000
    for _ in range(count):
        alias = generate_alias(EntityType.COMPANY, existing_aliases=existing)
        assert alias not in existing
        existing.add(alias)
    assert len(existing) == count


def test_bulk_generation_mixed_entity_types_no_duplicates() -> None:
    """
    Разные типы сущностей имеют разные префиксы, поэтому даже при
    одинаковой случайной части алиасы не совпадают между типами —
    проверяем это на смешанной генерации.
    """
    existing: set[str] = set()
    entity_types = list(EntityType)
    for i in range(5_000):
        entity_type = entity_types[i % len(entity_types)]
        alias = generate_alias(entity_type, existing_aliases=existing)
        assert alias not in existing
        assert alias.startswith(entity_type.alias_prefix + "_")
        existing.add(alias)
    assert len(existing) == 5_000


# ---------------------------------------------------------------------------
# Collision retry
# ---------------------------------------------------------------------------

def test_collision_triggers_retry_and_does_not_return_existing_alias(monkeypatch) -> None:
    """
    На маленьком keyspace принудительно вызываем коллизию: первая
    "случайная" последовательность совпадает с уже занятым alias, вторая —
    свободна. Генератор обязан не вернуть занятый alias, а сделать retry и
    вернуть новый.
    """
    alphabet = "AB"
    random_length = 1
    existing = {"C_A"}  # единственное уже занятое значение в keyspace {A, B}

    sequence = iter(["A", "B"])  # первый выбор коллизирует, второй — свободен
    calls = []

    def fake_choice(seq):
        calls.append(seq)
        return next(sequence)

    monkeypatch.setattr(secrets, "choice", fake_choice)

    alias = generate_alias(
        EntityType.COMPANY,
        existing_aliases=existing,
        alphabet=alphabet,
        random_length=random_length,
    )

    assert alias == "C_B"
    assert alias not in existing
    assert len(calls) == 2  # подтверждает, что был именно retry


# ---------------------------------------------------------------------------
# Exhaustion
# ---------------------------------------------------------------------------

def test_exhaustion_raises_explicit_exception_not_infinite_loop() -> None:
    """
    Keyspace из одного возможного значения, которое уже занято —
    генератор обязан быстро завершиться понятным исключением, а не
    зациклиться.
    """
    alphabet = "A"
    random_length = 1
    existing = {"C_A"}  # единственное возможное значение уже занято

    with pytest.raises(AliasSpaceExhaustedError):
        generate_alias(
            EntityType.COMPANY,
            existing_aliases=existing,
            alphabet=alphabet,
            random_length=random_length,
        )


def test_exhaustion_after_filling_entire_small_keyspace() -> None:
    """
    Полностью заполняем маленький keyspace (алфавит из 2 символов, длина
    1 => 2 возможных значения), затем следующий вызов обязан упасть с
    понятным исключением.
    """
    alphabet = "AB"
    random_length = 1
    existing: set[str] = set()

    for _ in range(2):
        alias = generate_alias(
            EntityType.COMPANY,
            existing_aliases=existing,
            alphabet=alphabet,
            random_length=random_length,
        )
        existing.add(alias)

    assert existing == {"C_A", "C_B"}

    with pytest.raises(AliasSpaceExhaustedError):
        generate_alias(
            EntityType.COMPANY,
            existing_aliases=existing,
            alphabet=alphabet,
            random_length=random_length,
        )


def test_exhaustion_error_message_matches_historical_wording() -> None:
    """
    Regression (Stage 7B.2 corrective pass): извлечение общего core для
    generate_alias/generate_identifier_token не должно было изменить
    исторический текст AliasSpaceExhaustedError. Фиксируем его дословно.
    """
    alphabet = "A"
    random_length = 1
    existing = {"C_A"}  # единственное возможное значение уже занято

    with pytest.raises(AliasSpaceExhaustedError) as exc_info:
        generate_alias(
            EntityType.COMPANY,
            existing_aliases=existing,
            alphabet=alphabet,
            random_length=random_length,
        )

    expected_message = (
        "Не удалось сгенерировать уникальный alias за "
        f"{_MAX_ATTEMPTS} попыток (keyspace=1, "
        "уже занято алиасов: 1). "
        "Пространство псевдонимов исчерпано или практически исчерпано "
        "относительно количества уже выданных значений."
    )
    assert str(exc_info.value) == expected_message


# ---------------------------------------------------------------------------
# Decorrelation
# ---------------------------------------------------------------------------

def test_generate_alias_has_no_parent_alias_parameter() -> None:
    """
    Decorrelation гарантируется архитектурно: у функции физически нет
    входного параметра, через который можно было бы передать alias
    родителя, — значит, она не может ни использовать его, ни зависеть
    от него.
    """
    sig = inspect.signature(generate_alias)
    assert "parent_alias" not in sig.parameters
    assert "parent" not in sig.parameters


def test_child_alias_is_not_derived_from_parent_alias(monkeypatch) -> None:
    """
    Даже если оба alias сгенерированы из одного контролируемого источника
    случайности подряд (как в реальном сценарии "сначала родитель, потом
    дочерняя сущность"), дочерний alias не является конкатенацией или
    иной производной от alias родителя.
    """
    chars = iter(list("ABCDEFGHJ") + list("KMNPQRSTV"))
    monkeypatch.setattr(secrets, "choice", lambda seq: next(chars))

    company_alias = generate_alias(EntityType.COMPANY, existing_aliases=set())
    branch_alias = generate_alias(EntityType.BRANCH, existing_aliases=set())

    assert company_alias != branch_alias
    assert company_alias not in branch_alias
    assert branch_alias not in company_alias
    # префикс родителя не "просачивается" внутрь alias дочерней сущности
    assert EntityType.COMPANY.alias_prefix not in branch_alias.split("_", 1)[1]


def test_same_parent_produces_independent_child_aliases() -> None:
    """
    Два вызова generate_alias для дочерних сущностей одного и того же
    (логического) родителя не совпадают. Единственная связь между ними —
    явный parent_alias в MappingEntry, а не сам alias.
    """
    existing: set[str] = set()
    branch_alias_1 = generate_alias(EntityType.BRANCH, existing_aliases=existing)
    existing.add(branch_alias_1)
    branch_alias_2 = generate_alias(EntityType.BRANCH, existing_aliases=existing)

    assert branch_alias_1 != branch_alias_2


# ---------------------------------------------------------------------------
# Валидация входных параметров
# ---------------------------------------------------------------------------

def test_invalid_entity_type_raises_value_error() -> None:
    with pytest.raises(ValueError):
        generate_alias("not-an-entity-type", existing_aliases=set())  # type: ignore[arg-type]


def test_invalid_alphabet_raises_value_error() -> None:
    with pytest.raises(ValueError):
        generate_alias(EntityType.COMPANY, existing_aliases=set(), alphabet="AAB")
    with pytest.raises(ValueError):
        generate_alias(EntityType.COMPANY, existing_aliases=set(), alphabet="")


def test_invalid_random_length_raises_value_error() -> None:
    with pytest.raises(ValueError):
        generate_alias(EntityType.COMPANY, existing_aliases=set(), random_length=0)
    with pytest.raises(ValueError):
        generate_alias(EntityType.COMPANY, existing_aliases=set(), random_length=-1)
