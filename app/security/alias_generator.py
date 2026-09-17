"""
Генератор псевдонимов сущностей (alias) и identifier tokens (Stage 7B.2).

Использует исключительно модуль secrets (криптографически стойкий
источник случайности) — модуль random в этом файле не используется и не
импортируется.

======================================================================
Entity alias
======================================================================

Alias имеет вид {PREFIX}_{СЛУЧАЙНАЯ_ЧАСТЬ}, где PREFIX берётся из
EntityType.alias_prefix (см. app.models.entities), а случайная часть
генерируется независимо от каких-либо внешних данных, включая alias
родительской сущности. Именно поэтому функция generate_alias не принимает
parent_alias как параметр — это гарантирует decorrelation между уровнями
иерархии на уровне сигнатуры, а не соглашения. Связь alias с его
родителем хранится отдельно, в MappingEntry.parent_alias.

======================================================================
Identifier token (Stage 7B.2)
======================================================================

Token для формализованных идентификаторов (ИНН/КПП/ОГРН) имеет тот же
общий вид {PREFIX}_{СЛУЧАЙНАЯ_ЧАСТЬ}, но с собственным, не пересекающимся
с entity-префиксами пространством: "INN"/"KPP"/"OGRN" (полное слово, а не
одна буква, как у EntityType.alias_prefix) — сознательно, чтобы token
нельзя было спутать с entity alias ни по какому наивному разбору префикса.

generate_identifier_token принимает IdentifierType (app.models.identifiers)
и НИКОГДА не принимает и не видит реальное значение идентификатора —
token генерируется полностью независимо от него (та же гарантия
decorrelation на уровне сигнатуры, что и у generate_alias с parent_alias).
Domain-валидация (checksum ИНН/КПП/ОГРН) и lookup/store-семантика — не
этот модуль (см. Stage 5 и будущий Stage 7B.3+).

Обе публичные функции используют общее приватное ядро генерации
(_generate_with_prefix) — единственное место, где реализован сам
алгоритм "сгенерировать -> проверить коллизию -> retry -> exhaustion".
"""

from __future__ import annotations

import secrets
from typing import AbstractSet, Callable

from app.models.entities import EntityType
from app.models.identifiers import IdentifierType

# Crockford-style base32: цифры + заглавные буквы без I, L, O, U —
# символов, которые легко перепутать друг с другом или с 0/1.
PRODUCTION_ALPHABET: str = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"

# Длина случайной части alias в production. Обоснование (расчёт
# вероятности коллизии для 10 000 / 100 000 / 1 000 000 сущностей —
# зафиксирован в переписке по архитектуре проекта): при 9 символах и
# данном алфавите (keyspace = 32^9 ≈ 3.5×10^13) вероятность хотя бы одной
# коллизии не превышает ~1.4% даже при 1 000 000 уникальных сущностей,
# при этом сама коллизия в любом случае перехватывается и обрабатывается
# retry-логикой ниже, а не остаётся незамеченной.
PRODUCTION_RANDOM_LENGTH: int = 9

# Ограничение числа попыток retry при коллизии — защита от теоретически
# бесконечного цикла. При production-параметрах это ограничение
# практически недостижимо (см. расчёт выше); оно рассчитано в первую
# очередь на искусственно маленькие keyspace в unit-тестах.
_MAX_ATTEMPTS: int = 10_000


class AliasSpaceExhaustedError(Exception):
    """
    Пространство возможных псевдонимов исчерпано, либо коллизии не
    удаётся избежать за разумное число попыток.

    В production-конфигурации (Crockford32, 9 случайных символов,
    keyspace ≈ 3.5×10^13) это исключение практически недостижимо при
    реалистичных объёмах данных. Оно рассчитано на случай искусственно
    маленького keyspace (в первую очередь в тестах) или на случай, если
    множество уже выданных алиасов патологически велико относительно
    keyspace.
    """


class IdentifierTokenSpaceExhaustedError(Exception):
    """
    Пространство возможных identifier tokens (ИНН/КПП/ОГРН) исчерпано,
    либо коллизии не удаётся избежать за разумное число попыток.

    Отдельный от AliasSpaceExhaustedError класс (без общего предка, кроме
    Exception) — исчерпание пространства identifier tokens семантически
    не является исчерпанием пространства entity alias, и называть его
    "Alias..." было бы вводящим в заблуждение. Общий базовый класс между
    двумя исключениями сознательно не введён: на данный момент ни один
    вызывающий код не ловит оба сразу одним except, а общий retry/
    collision-алгоритм и так вынесен в _generate_with_prefix независимо
    от иерархии исключений (YAGNI).
    """


def _validate_alphabet(alphabet: object) -> None:
    if not isinstance(alphabet, str) or not alphabet or len(set(alphabet)) != len(alphabet):
        raise ValueError("alphabet должен быть непустой строкой без повторяющихся символов")


def _validate_random_length(random_length: object) -> None:
    if (
        not isinstance(random_length, int)
        or isinstance(random_length, bool)
        or random_length <= 0
    ):
        raise ValueError("random_length должен быть положительным целым числом")


def _alias_exhausted_message(keyspace_size: int, existing_count: int) -> str:
    """Дословно исторический текст AliasSpaceExhaustedError (до Stage 7B.2)."""
    return (
        "Не удалось сгенерировать уникальный alias за "
        f"{_MAX_ATTEMPTS} попыток (keyspace={keyspace_size}, "
        f"уже занято алиасов: {existing_count}). "
        "Пространство псевдонимов исчерпано или практически исчерпано "
        "относительно количества уже выданных значений."
    )


def _identifier_token_exhausted_message(
    prefix: str, keyspace_size: int, existing_count: int
) -> str:
    """
    Отдельный, семантически корректный текст для
    IdentifierTokenSpaceExhaustedError — не обязан совпадать с alias-
    сообщением. Не содержит и не может содержать реальное значение
    идентификатора (эта функция его не получает).
    """
    return (
        f"Не удалось сгенерировать уникальный identifier token с префиксом "
        f"{prefix!r} за {_MAX_ATTEMPTS} попыток (keyspace={keyspace_size}, "
        f"уже занято identifier tokens: {existing_count}). "
        "Пространство identifier tokens исчерпано или практически исчерпано "
        "относительно количества уже выданных значений."
    )


def _generate_with_prefix(
    prefix: str,
    existing: AbstractSet[str],
    alphabet: str,
    random_length: int,
    exhausted_error: type[Exception],
    exhausted_message: Callable[[int, int], str],
) -> str:
    """
    Общее ядро генерации: {prefix}_{СЛУЧАЙНАЯ_ЧАСТЬ через secrets.choice},
    retry при коллизии с existing, явное исключение после _MAX_ATTEMPTS.

    prefix здесь уже вычислен и проверен вызывающей публичной функцией
    (EntityType.alias_prefix либо префикс identifier token) — это ядро не
    знает и не должно знать, откуда взялся prefix.

    exhausted_error/exhausted_message — сознательно НЕ общий базовый
    exception и НЕ generic-фреймворк: exhausted_error — обычный класс
    исключения (type[Exception]), exhausted_message — обычная функция
    (keyspace_size, existing_count) -> str, которую вызывающая публичная
    функция уже подготовила (см. generate_alias/generate_identifier_token)
    так, чтобы текст ошибки был исторически/семантически корректным именно
    для неё, без единого общего "универсального" сообщения на двоих.
    """

    attempts = 0
    while attempts < _MAX_ATTEMPTS:
        attempts += 1
        random_part = "".join(secrets.choice(alphabet) for _ in range(random_length))
        candidate = f"{prefix}_{random_part}"
        if candidate not in existing:
            return candidate

    keyspace_size = len(alphabet) ** random_length
    raise exhausted_error(exhausted_message(keyspace_size, len(existing)))


def generate_alias(
    entity_type: EntityType,
    existing_aliases: AbstractSet[str],
    *,
    alphabet: str = PRODUCTION_ALPHABET,
    random_length: int = PRODUCTION_RANDOM_LENGTH,
) -> str:
    """
    Генерирует новый, ранее не встречавшийся alias для сущности типа
    entity_type.

    Параметры alphabet и random_length предназначены ТОЛЬКО для
    unit-тестов (например, чтобы детерминированно вызвать коллизию или
    исчерпание пространства на маленьком keyspace). Production-код обязан
    вызывать generate_alias без этих параметров, чтобы использовались
    PRODUCTION_ALPHABET и PRODUCTION_RANDOM_LENGTH.

    :param entity_type: тип сущности, определяющий префикс alias.
    :param existing_aliases: множество уже выданных алиасов (для проверки
        коллизий). Функция не изменяет этот набор.
    :param alphabet: алфавит случайной части (только для тестов).
    :param random_length: длина случайной части (только для тестов).
    :raises AliasSpaceExhaustedError: если за _MAX_ATTEMPTS попыток не
        удалось получить alias, отсутствующий в existing_aliases.
    :raises ValueError: при некорректных входных параметрах.
    """

    if not isinstance(entity_type, EntityType):
        raise ValueError(
            f"entity_type должен быть значением EntityType, получено: {type(entity_type)!r}"
        )
    _validate_alphabet(alphabet)
    _validate_random_length(random_length)

    return _generate_with_prefix(
        entity_type.alias_prefix,
        existing_aliases,
        alphabet,
        random_length,
        AliasSpaceExhaustedError,
        _alias_exhausted_message,
    )


# Полнословные префиксы identifier token — сознательно НЕ однобуквенные,
# как у EntityType.alias_prefix, чтобы token нельзя было спутать с entity
# alias ни по какому наивному разбору префикса (см. docstring модуля).
_IDENTIFIER_TOKEN_PREFIXES: dict[IdentifierType, str] = {
    IdentifierType.INN: "INN",
    IdentifierType.KPP: "KPP",
    IdentifierType.OGRN: "OGRN",
}


def generate_identifier_token(
    identifier_type: IdentifierType,
    existing_tokens: AbstractSet[str],
    *,
    alphabet: str = PRODUCTION_ALPHABET,
    random_length: int = PRODUCTION_RANDOM_LENGTH,
) -> str:
    """
    Генерирует новый, ранее не встречавшийся identifier token для
    identifier_type (ИНН/КПП/ОГРН).

    Функция НЕ принимает и не видит реальное значение идентификатора —
    единственный вход, влияющий на префикс, это identifier_type; случайная
    часть не зависит ни от каких внешних данных. Вызывающий код не может
    задать произвольный prefix напрямую — только через identifier_type
    (см. _IDENTIFIER_TOKEN_PREFIXES).

    Параметры alphabet и random_length предназначены ТОЛЬКО для
    unit-тестов, как и у generate_alias.

    :param identifier_type: тип идентификатора, определяющий префикс token.
    :param existing_tokens: множество уже выданных tokens (для проверки
        коллизий). Функция не изменяет этот набор.
    :param alphabet: алфавит случайной части (только для тестов).
    :param random_length: длина случайной части (только для тестов).
    :raises IdentifierTokenSpaceExhaustedError: если за _MAX_ATTEMPTS
        попыток не удалось получить token, отсутствующий в existing_tokens.
    :raises ValueError: при некорректных входных параметрах.
    """

    if not isinstance(identifier_type, IdentifierType):
        raise ValueError(
            "identifier_type должен быть значением IdentifierType, получено: "
            f"{type(identifier_type)!r}"
        )
    _validate_alphabet(alphabet)
    _validate_random_length(random_length)

    prefix = _IDENTIFIER_TOKEN_PREFIXES[identifier_type]
    return _generate_with_prefix(
        prefix,
        existing_tokens,
        alphabet,
        random_length,
        IdentifierTokenSpaceExhaustedError,
        lambda keyspace_size, existing_count: _identifier_token_exhausted_message(
            prefix, keyspace_size, existing_count
        ),
    )
