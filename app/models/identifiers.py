"""
Модели identifier tokenization (Stage 7B.1).

Этот модуль — ТОЛЬКО модель данных для будущего механизма токенизации
российских идентификаторов (ИНН/КПП/ОГРН), спроектированного отдельно от
EntityType/MappingEntry (app.models.entities), потому что идентификаторы —
не сущности (см. Stage 7B.0/Stage 7B.0-correction).

Модуль сознательно НЕ реализует:
    - lookup/equality store semantics (это IdentifierMappingStore,
      Stage 7B.3);
    - формат/генерацию token (Crockford32, длина, secrets) — Stage 7B.2;
    - domain-валидацию (checksum ИНН/КПП/ОГРН/ОГРНИП, соответствие
      token-префикса identifier_type) — Stage 7B.5 (интеграция с
      anonymizer).

Здесь есть только структурная валидация полей самой модели — то же
разделение ответственности, что уже принято для MappingEntry
(app.models.entities) и FieldRule (app.models.rules): модель проверяет
форму своих полей, но не бизнес-смысл значений.

======================================================================
IdentifierType — не FieldType
======================================================================

IdentifierType — узкий enum именно для identity-пространства identifier
mapping, а не переиспользование FieldType. FieldType отвечает на другой
вопрос ("что это за колонка Excel") и содержит значения, бессмысленные
здесь (BRANCH, PHONE, FINANCIAL...). Ровно так же в проекте уже разделены
EntityType и FieldType, даже когда пересекаются по названию (COMPANY есть
в обоих) — та же логика применяется между FieldType и IdentifierType.

ОГРНИП сознательно не имеет отдельного значения: как и FieldType.OGRN
на Stage 5, ОГРН (13 цифр) и ОГРНИП (15 цифр) относятся к одному
IdentifierType.OGRN — это не создаёт коллизий identity, поскольку строки
разной длины никогда не совпадут как identifier_value.

======================================================================
identifier_value — EXACT, без нормализации
======================================================================

identifier_value — строковое представление идентификатора, используемое
identity/token-lookup слоем (Stage 7B.3+). Модель принимает УЖЕ ГОТОВОЕ
значение и не выполняет никакого преобразования сама:

    - для исходного str  -> ожидается EXACT строка как есть;
    - для исходного int  -> ожидается str(value), вычисленный ДО создания
      этой записи (int -> str здесь не делается).

IdentifierMappingEntry не делает strip(), casefold(), collapse whitespace,
не убирает leading zeros, не выполняет checksum-проверку и не исправляет
значение. " 7701234567 " и "7701234567" — разные identifier_value и
должны сохраняться как есть; кто из них валиден как ИНН — вопрос другого
слоя (Stage 7B.5), не этой модели.

Единственная структурная проверка identifier_value здесь — что это str
и что после strip() остаётся непустой остаток (whitespace-only строка
отклоняется, т.к. Stage 7B вообще не создаёт mapping entry для
пустых/пробельных ячеек — соответствующая проверка строки для
strip()-детекции такая же, что уже используется для MappingEntry.alias
и FieldRule.column_name, а не новая практика).
"""

from __future__ import annotations

import dataclasses
from enum import Enum


class IdentifierType(Enum):
    """
    Тип формализованного российского идентификатора для identifier
    mapping identity. Ровно три значения — не EntityType, не FieldType.
    """

    INN = "inn"
    KPP = "kpp"
    OGRN = "ogrn"


@dataclasses.dataclass(frozen=True)
class IdentifierMappingEntry:
    """
    Одна запись identifier mapping: token <-> (identifier_type,
    identifier_value).

    Identity = (identifier_type, identifier_value); Python-тип исходного
    значения (int/str) сознательно НЕ хранится здесь — он не относится к
    identifier identity (см. docstring модуля и Stage 7B.0-correction).

    Объект неизменяемый (frozen), как и MappingEntry: запись mapping —
    уже свершившийся факт соответствия.
    """

    token: str
    identifier_value: str
    identifier_type: IdentifierType

    def __post_init__(self) -> None:
        if not isinstance(self.token, str) or not self.token.strip():
            raise ValueError(
                "IdentifierMappingEntry.token обязателен и не может быть "
                "пустым/состоящим только из пробелов"
            )
        if not isinstance(self.identifier_value, str) or not self.identifier_value.strip():
            raise ValueError(
                "IdentifierMappingEntry.identifier_value обязателен и не может "
                "быть пустым/состоящим только из пробелов"
            )
        if not isinstance(self.identifier_type, IdentifierType):
            raise ValueError(
                "IdentifierMappingEntry.identifier_type должен быть значением "
                f"IdentifierType, получено: {type(self.identifier_type)!r}"
            )
