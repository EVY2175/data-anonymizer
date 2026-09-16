"""
In-memory реализация MappingStore.

Хранит все записи исключительно в оперативной памяти процесса — никакого
файлового I/O. Предназначена для unit-тестов бизнес-логики и как основа
контракта, под который позже подставится EncryptedFileMappingStore.
"""

from __future__ import annotations

from typing import Optional

from app.mapping.base import (
    _PARENT_UNSET,
    AmbiguousMappingError,
    MappingConflictError,
    MappingStore,
    _ParentAliasArg,
)
from app.models.entities import EntityType, MappingEntry


class InMemoryMappingStore(MappingStore):
    """
    Реализация MappingStore, полностью работающая в RAM.

    Поддерживает три индекса:
    - _by_alias: alias -> MappingEntry, O(1) поиск по alias;
    - _by_identity: (real_value, entity_type, parent_alias) -> MappingEntry,
      O(1) поиск по полной identity (в том числе когда parent_alias явно
      равен None — top-level сущность тоже имеет полностью определённую
      identity);
    - _by_real_value: real_value -> list[MappingEntry], записи,
      разделяющие одно real_value. Используется только когда entity_type
      и/или parent_alias не заданы явно (см. семантику sentinel
      _PARENT_UNSET в app.mapping.base) — тогда нужно отличить "ровно одна
      подходящая запись" от "несколько подходящих записей", и делать это
      приходится лишь среди записей с этим real_value, а не по всему
      хранилищу целиком.
    """

    def __init__(self) -> None:
        self._by_alias: dict[str, MappingEntry] = {}
        self._by_identity: dict[tuple[str, EntityType, Optional[str]], MappingEntry] = {}
        self._by_real_value: dict[str, list[MappingEntry]] = {}

    # ------------------------------------------------------------------
    # Запись
    # ------------------------------------------------------------------

    def add(self, entry: MappingEntry) -> None:
        if not isinstance(entry, MappingEntry):
            raise TypeError(f"add() принимает MappingEntry, получено: {type(entry)!r}")

        identity = self._identity_key(entry)

        existing_by_alias = self._by_alias.get(entry.alias)
        if existing_by_alias is not None:
            if existing_by_alias == entry:
                # Идемпотентный повтор: та же запись уже есть под тем же alias.
                return
            raise MappingConflictError(
                f"Alias {entry.alias!r} уже связан с другой записью mapping: "
                f"real_value={existing_by_alias.real_value!r}, "
                f"entity_type={existing_by_alias.entity_type!r}, "
                f"parent_alias={existing_by_alias.parent_alias!r}"
            )

        existing_by_identity = self._by_identity.get(identity)
        if existing_by_identity is not None:
            # existing_by_identity == entry здесь невозможно: если бы обе
            # записи совпадали полностью (включая alias), мы бы уже вышли
            # выше через existing_by_alias. Значит, alias отличается — это
            # конфликт "одна identity -> два разных alias".
            raise MappingConflictError(
                f"Комбинация (real_value={entry.real_value!r}, "
                f"entity_type={entry.entity_type!r}, "
                f"parent_alias={entry.parent_alias!r}) уже связана с alias "
                f"{existing_by_identity.alias!r}; повторная попытка связать "
                f"её с {entry.alias!r}"
            )

        self._by_alias[entry.alias] = entry
        self._by_identity[identity] = entry
        self._by_real_value.setdefault(entry.real_value, []).append(entry)

    # ------------------------------------------------------------------
    # Чтение
    # ------------------------------------------------------------------

    def get_by_alias(self, alias: str) -> Optional[MappingEntry]:
        self._validate_alias(alias)
        return self._by_alias.get(alias)

    def get_by_real_value(
        self,
        real_value: str,
        entity_type: Optional[EntityType] = None,
        parent_alias: _ParentAliasArg = _PARENT_UNSET,
    ) -> Optional[MappingEntry]:
        self._validate_real_value(real_value)
        self._validate_entity_type(entity_type)

        # parent_alias передан явно (в т.ч. как None), если он отличается
        # от sentinel "не задано". Это единственное место, где мы вообще
        # смотрим на _PARENT_UNSET — дальше работаем с обычным bool-флагом.
        parent_alias_specified = parent_alias is not _PARENT_UNSET
        if parent_alias_specified:
            self._validate_parent_alias_filter(parent_alias)

        if entity_type is not None and parent_alias_specified:
            # Полная identity задана явно (включая случай parent_alias=None
            # для top-level сущности) — прямой O(1) поиск по индексу.
            # Неоднозначность здесь в принципе невозможна: ключ словаря
            # уникален по построению.
            return self._by_identity.get((real_value, entity_type, parent_alias))

        candidates = list(self._by_real_value.get(real_value, ()))
        if entity_type is not None:
            candidates = [c for c in candidates if c.entity_type is entity_type]
        if parent_alias_specified:
            candidates = [c for c in candidates if c.parent_alias == parent_alias]

        if not candidates:
            return None
        if len(candidates) == 1:
            return candidates[0]

        raise AmbiguousMappingError(
            f"real_value={real_value!r} с фильтрами entity_type={entity_type!r}, "
            f"parent_alias={'не задан' if not parent_alias_specified else parent_alias!r} "
            f"соответствует {len(candidates)} записям mapping; уточните "
            "entity_type и/или parent_alias"
        )

    def contains_alias(self, alias: str) -> bool:
        self._validate_alias(alias)
        return alias in self._by_alias

    def all_aliases(self) -> set[str]:
        # Новый set: изменение результата не влияет на внутренний индекс.
        return set(self._by_alias.keys())

    def entries(self) -> tuple[MappingEntry, ...]:
        # Новый tuple из уже неизменяемых MappingEntry: результат — снимок,
        # независимый от последующих изменений хранилища.
        return tuple(self._by_alias.values())

    def clear(self) -> None:
        self._by_alias.clear()
        self._by_identity.clear()
        self._by_real_value.clear()

    # ------------------------------------------------------------------
    # Внутренние помощники
    # ------------------------------------------------------------------

    @staticmethod
    def _identity_key(entry: MappingEntry) -> tuple[str, EntityType, Optional[str]]:
        return (entry.real_value, entry.entity_type, entry.parent_alias)

    @staticmethod
    def _validate_alias(alias: object) -> None:
        if not isinstance(alias, str) or not alias.strip():
            raise ValueError(f"alias должен быть непустой строкой, получено: {alias!r}")

    @staticmethod
    def _validate_real_value(real_value: object) -> None:
        if not isinstance(real_value, str) or not real_value.strip():
            raise ValueError(
                f"real_value должен быть непустой строкой, получено: {real_value!r}"
            )

    @staticmethod
    def _validate_entity_type(entity_type: object) -> None:
        if entity_type is not None and not isinstance(entity_type, EntityType):
            raise ValueError(
                f"entity_type должен быть EntityType или None, получено: {type(entity_type)!r}"
            )

    @staticmethod
    def _validate_parent_alias_filter(parent_alias: object) -> None:
        """
        Валидация parent_alias, переданного ЯВНО вызывающим кодом (то есть
        когда parent_alias is not _PARENT_UNSET).

        None здесь — легитимное, осмысленное значение ("искать запись без
        родителя"), а не признак "фильтр не задан" (эта роль уже отыграна
        sentinel'ом до вызова этой функции) — поэтому None не отклоняется.
        Всё остальное должно быть непустой строкой.
        """
        if parent_alias is None:
            return
        if not isinstance(parent_alias, str) or not parent_alias.strip():
            raise ValueError(
                "parent_alias, если указан явно, должен быть None либо непустой "
                f"строкой, получено: {parent_alias!r}"
            )
