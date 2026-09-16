"""
Модели правил обработки полей (колонок) Excel-файла.

FieldType описывает, какой тип данных обнаружен в поле, Action — какое
действие к нему применить, FieldRule связывает конкретную колонку с этой
парой значений.

Модуль сознательно не содержит логики чтения/анализа Excel — только
модели данных, которыми эта логика будет оперировать на следующих этапах
(detectors.py, anonymizer.py).
"""

from __future__ import annotations

import dataclasses
from enum import Enum
from typing import Any


class FieldType(Enum):
    """Распознанный тип содержимого поля (колонки)."""

    COMPANY = "company"
    BRANCH = "branch"
    DEPARTMENT = "department"
    PERSON = "person"
    INN = "inn"
    KPP = "kpp"
    OGRN = "ogrn"
    PHONE = "phone"
    EMAIL = "email"
    ADDRESS = "address"
    FINANCIAL = "financial"
    UNKNOWN = "unknown"


class Action(Enum):
    """Действие, которое будет применено к полю при обезличивании."""

    PSEUDONYMIZE = "pseudonymize"
    REMOVE = "remove"
    KEEP = "keep"


@dataclasses.dataclass
class FieldRule:
    """
    Правило обработки одного поля (колонки): что это за поле и что с ним
    делать.

    В отличие от MappingEntry, объект изменяемый (не frozen): пользователь
    должен иметь возможность изменить action после автоматического анализа
    структуры файла (см. согласованный сценарий "Анализ перед
    обезличиванием").
    """

    column_name: str
    field_type: FieldType
    action: Action

    def __post_init__(self) -> None:
        if not isinstance(self.column_name, str) or not self.column_name.strip():
            raise ValueError("FieldRule.column_name обязателен и не может быть пустым")
        if not isinstance(self.field_type, FieldType):
            raise ValueError(
                "FieldRule.field_type должен быть значением FieldType, "
                f"получено: {type(self.field_type)!r}"
            )
        if not isinstance(self.action, Action):
            raise ValueError(
                f"FieldRule.action должен быть значением Action, получено: {type(self.action)!r}"
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "column_name": self.column_name,
            "field_type": self.field_type.value,
            "action": self.action.value,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "FieldRule":
        if not isinstance(data, dict):
            raise ValueError("FieldRule.from_dict ожидает dict")

        missing = [key for key in ("column_name", "field_type", "action") if key not in data]
        if missing:
            raise ValueError(f"В данных FieldRule отсутствуют обязательные поля: {missing}")

        field_type_raw = data["field_type"]
        try:
            field_type = (
                field_type_raw
                if isinstance(field_type_raw, FieldType)
                else FieldType(field_type_raw)
            )
        except ValueError as exc:
            raise ValueError(f"Некорректное значение field_type: {field_type_raw!r}") from exc

        action_raw = data["action"]
        try:
            action = action_raw if isinstance(action_raw, Action) else Action(action_raw)
        except ValueError as exc:
            raise ValueError(f"Некорректное значение action: {action_raw!r}") from exc

        return cls(
            column_name=data["column_name"],
            field_type=field_type,
            action=action,
        )
