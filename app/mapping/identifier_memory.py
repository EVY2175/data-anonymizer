"""
In-memory реализация IdentifierMappingStore (Stage 7B.3).

Хранит все записи исключительно в оперативной памяти процесса — никакого
файлового I/O, шифрования или token-генерации. Предназначена для
unit-тестов бизнес-логики и как основа контракта, под который позже
подставится encrypted-реализация (Stage 7B.4).
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Optional

from app.mapping.identifier_base import IdentifierMappingConflictError, IdentifierMappingStore
from app.models.identifiers import IdentifierMappingEntry, IdentifierType


class InMemoryIdentifierMappingStore(IdentifierMappingStore):
    """
    Реализация IdentifierMappingStore, полностью работающая в RAM.

    Поддерживает два индекса:
        - _by_token: token -> IdentifierMappingEntry, O(1) поиск по token;
        - _by_identity: (identifier_type, identifier_value) ->
          IdentifierMappingEntry, O(1) поиск по полной identity.

    _by_identifier_value (индекс только по значению, без identifier_type)
    сознательно не создаётся — partial lookup вне контракта Stage 7B.3
    (identifier_type всегда известен заранее на вызывающей стороне).
    """

    def __init__(self) -> None:
        self._by_token: dict[str, IdentifierMappingEntry] = {}
        self._by_identity: dict[tuple[IdentifierType, str], IdentifierMappingEntry] = {}

    # ------------------------------------------------------------------
    # Запись
    # ------------------------------------------------------------------

    def add(self, entry: IdentifierMappingEntry) -> None:
        if not isinstance(entry, IdentifierMappingEntry):
            raise TypeError(
                f"add() принимает IdentifierMappingEntry, получено: {type(entry)!r}"
            )

        identity = self._identity_key(entry)

        existing_by_token = self._by_token.get(entry.token)
        if existing_by_token is not None:
            if existing_by_token == entry:
                # Идемпотентный повтор: та же запись уже есть под тем же token.
                return
            raise IdentifierMappingConflictError(
                f"Token {entry.token!r} уже связан с другой identifier identity "
                f"(identifier_type={existing_by_token.identifier_type.value!r}). "
                "Raw identifier value намеренно не показан в этом сообщении."
            )

        existing_by_identity = self._by_identity.get(identity)
        if existing_by_identity is not None:
            # existing_by_identity == entry здесь невозможно: если бы обе
            # записи совпадали полностью (включая token), мы бы уже вышли
            # выше через existing_by_token. Значит, token отличается — это
            # конфликт "одна identity -> два разных token".
            raise IdentifierMappingConflictError(
                f"Identifier identity (identifier_type={entry.identifier_type.value!r}) "
                f"уже связана с token {existing_by_identity.token!r}; повторная "
                f"попытка связать её с token {entry.token!r}. Raw identifier value "
                "намеренно не показан в этом сообщении."
            )

        self._by_token[entry.token] = entry
        self._by_identity[identity] = entry

    def add_many(self, entries: Iterable[IdentifierMappingEntry]) -> None:
        batch = tuple(entries)
        if not batch:
            return

        # Authoritative state — ровно _by_token/_by_identity (см. __init__),
        # третьего индекса/derived mutable state нет, поэтому candidate
        # можно строить прямым shallow-copy этих двух dict, без replay
        # существующих записей через add(): это уже валидное состояние.
        candidate = InMemoryIdentifierMappingStore()
        candidate._by_token = self._by_token.copy()
        candidate._by_identity = self._by_identity.copy()

        for entry in batch:
            # Может поднять IdentifierMappingConflictError/TypeError — в
            # этом случае self не тронут, candidate отбрасывается целиком.
            candidate.add(entry)

        # Commit только после полного успеха всего batch.
        self._by_token = candidate._by_token
        self._by_identity = candidate._by_identity

    def clear(self) -> None:
        self._by_token.clear()
        self._by_identity.clear()

    # ------------------------------------------------------------------
    # Чтение
    # ------------------------------------------------------------------

    def get_by_token(self, token: str) -> Optional[IdentifierMappingEntry]:
        self._validate_token(token)
        return self._by_token.get(token)

    def get_by_identity(
        self, identifier_type: IdentifierType, identifier_value: str
    ) -> Optional[IdentifierMappingEntry]:
        self._validate_identifier_type(identifier_type)
        self._validate_identifier_value(identifier_value)
        return self._by_identity.get((identifier_type, identifier_value))

    def all_tokens(self) -> set[str]:
        # Новый set: изменение результата не влияет на внутренний индекс.
        return set(self._by_token.keys())

    def entries(self) -> tuple[IdentifierMappingEntry, ...]:
        # Новый tuple из уже неизменяемых IdentifierMappingEntry — снимок,
        # независимый от последующих изменений хранилища. dict сохраняет
        # порядок вставки, поэтому entries() тоже возвращает его.
        return tuple(self._by_token.values())

    # ------------------------------------------------------------------
    # Внутренние помощники
    # ------------------------------------------------------------------

    @staticmethod
    def _identity_key(entry: IdentifierMappingEntry) -> tuple[IdentifierType, str]:
        return (entry.identifier_type, entry.identifier_value)

    @staticmethod
    def _validate_token(token: object) -> None:
        if not isinstance(token, str) or not token.strip():
            raise ValueError(f"token должен быть непустой строкой, получено: {token!r}")

    @staticmethod
    def _validate_identifier_value(identifier_value: object) -> None:
        if not isinstance(identifier_value, str) or not identifier_value.strip():
            raise ValueError(
                f"identifier_value должен быть непустой строкой, получено: {identifier_value!r}"
            )

    @staticmethod
    def _validate_identifier_type(identifier_type: object) -> None:
        if not isinstance(identifier_type, IdentifierType):
            raise ValueError(
                "identifier_type должен быть значением IdentifierType, получено: "
                f"{type(identifier_type)!r}"
            )
