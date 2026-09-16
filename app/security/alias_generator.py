"""
Генератор псевдонимов (alias) для сущностей.

Использует исключительно модуль secrets (криптографически стойкий
источник случайности) — модуль random в этом файле не используется и не
импортируется.

Alias имеет вид {PREFIX}_{СЛУЧАЙНАЯ_ЧАСТЬ}, где PREFIX берётся из
EntityType.alias_prefix (см. app.models.entities), а случайная часть
генерируется независимо от каких-либо внешних данных, включая alias
родительской сущности. Именно поэтому функция generate_alias не принимает
parent_alias как параметр — это гарантирует decorrelation между уровнями
иерархии на уровне сигнатуры, а не соглашения. Связь alias с его
родителем хранится отдельно, в MappingEntry.parent_alias.
"""

from __future__ import annotations

import secrets
from typing import AbstractSet

from app.models.entities import EntityType

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
    if not isinstance(alphabet, str) or not alphabet or len(set(alphabet)) != len(alphabet):
        raise ValueError("alphabet должен быть непустой строкой без повторяющихся символов")
    if not isinstance(random_length, int) or isinstance(random_length, bool) or random_length <= 0:
        raise ValueError("random_length должен быть положительным целым числом")

    prefix = entity_type.alias_prefix

    attempts = 0
    while attempts < _MAX_ATTEMPTS:
        attempts += 1
        random_part = "".join(secrets.choice(alphabet) for _ in range(random_length))
        candidate = f"{prefix}_{random_part}"
        if candidate not in existing_aliases:
            return candidate

    keyspace_size = len(alphabet) ** random_length
    raise AliasSpaceExhaustedError(
        "Не удалось сгенерировать уникальный alias за "
        f"{_MAX_ATTEMPTS} попыток (keyspace={keyspace_size}, "
        f"уже занято алиасов: {len(existing_aliases)}). "
        "Пространство псевдонимов исчерпано или практически исчерпано "
        "относительно количества уже выданных значений."
    )
