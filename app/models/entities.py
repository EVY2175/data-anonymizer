"""
Модели сущностей для псевдонимизации.

Здесь определены:
- EntityType — типы сущностей, для которых создаются псевдонимы, вместе
  с префиксом alias как частью контракта самого типа;
- MappingEntry — одна запись соответствия «псевдоним <-> реальное значение»
  в будущем mapping.

Модуль не содержит файлового I/O, не зависит от openpyxl или cryptography
и не содержит бизнес-логики Excel — это чистые модели данных, которые
будут использованы на этапах MappingStore и Excel.
"""

from __future__ import annotations

import dataclasses
from enum import Enum
from typing import Any, Optional


class EntityType(Enum):
    """
    Тип сущности, для которой создаётся псевдоним.

    Префикс alias — часть контракта самого типа (см. alias_prefix), а не
    генератора псевдонимов: любой код, выдающий alias для сущности данного
    типа, обязан использовать именно этот префикс.
    """

    COMPANY = "company"
    BRANCH = "branch"
    DEPARTMENT = "department"
    PERSON = "person"
    # UNKNOWN — безопасное поведение по умолчанию для нераспознанных
    # сущностей. Имеет собственный, отдельный от типизированных сущностей
    # префикс, чтобы такие значения никогда не пересекались с ними по
    # пространству имён и были однозначно отличимы в mapping и в аудите.
    UNKNOWN = "unknown"

    @property
    def alias_prefix(self) -> str:
        """Префикс, с которого должен начинаться alias данного типа сущности."""
        return _ALIAS_PREFIXES[self]


# Таблица префиксов вынесена отдельно от if/elif: набор типов сущностей и
# их префиксов виден одним взглядом и расширяется добавлением одной строки.
_ALIAS_PREFIXES: dict["EntityType", str] = {
    EntityType.COMPANY: "C",
    EntityType.BRANCH: "B",
    EntityType.DEPARTMENT: "D",
    EntityType.PERSON: "P",
    EntityType.UNKNOWN: "X",
}


@dataclasses.dataclass(frozen=True)
class MappingEntry:
    """
    Одна запись соответствия «псевдоним <-> реальное значение».

    parent_alias хранит только ВНУТРЕННЮЮ связь иерархии (например,
    филиал -> компания) для использования в mapping и при восстановлении.
    Он не участвует в генерации alias и не должен использоваться для того,
    чтобы вывести alias родителя из alias дочерней сущности: единственный
    способ узнать эту связь — явно прочитать данное поле в mapping.

    Объект неизменяемый (frozen): запись mapping — это уже свершившийся
    факт соответствия, который не должен меняться "по месту".
    """

    alias: str
    real_value: str
    entity_type: EntityType
    parent_alias: Optional[str] = None

    def __post_init__(self) -> None:
        if not isinstance(self.alias, str) or not self.alias.strip():
            raise ValueError("MappingEntry.alias обязателен и не может быть пустым")
        if not isinstance(self.real_value, str) or not self.real_value:
            raise ValueError("MappingEntry.real_value обязателен и не может быть пустым")
        if not isinstance(self.entity_type, EntityType):
            raise ValueError(
                "MappingEntry.entity_type должен быть значением EntityType, "
                f"получено: {type(self.entity_type)!r}"
            )
        if self.parent_alias is not None:
            if not isinstance(self.parent_alias, str) or not self.parent_alias.strip():
                raise ValueError(
                    "MappingEntry.parent_alias, если указан, не может быть пустой строкой"
                )
            if self.parent_alias == self.alias:
                raise ValueError("MappingEntry.parent_alias не может совпадать с alias")

    def to_dict(self) -> dict[str, Any]:
        """Сериализация в простой dict — пригодно для JSON на следующих этапах."""
        return {
            "alias": self.alias,
            "real_value": self.real_value,
            "entity_type": self.entity_type.value,
            "parent_alias": self.parent_alias,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "MappingEntry":
        """Десериализация из dict, полученного из to_dict() (или совместимого JSON)."""
        if not isinstance(data, dict):
            raise ValueError("MappingEntry.from_dict ожидает dict")

        missing = [key for key in ("alias", "real_value", "entity_type") if key not in data]
        if missing:
            raise ValueError(f"В данных MappingEntry отсутствуют обязательные поля: {missing}")

        entity_type_raw = data["entity_type"]
        try:
            entity_type = (
                entity_type_raw
                if isinstance(entity_type_raw, EntityType)
                else EntityType(entity_type_raw)
            )
        except ValueError as exc:
            raise ValueError(
                f"Некорректное значение entity_type: {entity_type_raw!r}"
            ) from exc

        return cls(
            alias=data["alias"],
            real_value=data["real_value"],
            entity_type=entity_type,
            parent_alias=data.get("parent_alias"),
        )
