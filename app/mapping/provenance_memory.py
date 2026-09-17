"""
In-memory реализация ProvenanceStore (Stage 7C.2).

Хранит все записи исключительно в оперативной памяти процесса — никакого
файлового I/O, шифрования или job_id-генерации. Предназначена для
unit-тестов бизнес-логики и как основа контракта, под который позже
подставится encrypted-реализация (Stage 7C.3).
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Optional

from app.mapping.provenance_base import ProvenanceConflictError, ProvenanceStore
from app.models.provenance import IdentifierCellProvenance


class InMemoryProvenanceStore(ProvenanceStore):
    """
    Реализация ProvenanceStore, полностью работающая в RAM.

    Единственный authoritative индекс:
        - _by_coordinate: (sheet_name, row, column) -> IdentifierCellProvenance,
          O(1) поиск по координате.

    job_id передаётся вызывающим кодом при создании (store его не
    генерирует и не проверяет формат/энтропию) и остаётся неизменным на
    протяжении жизни объекта, включая clear().
    """

    def __init__(self, job_id: str) -> None:
        if not isinstance(job_id, str) or not job_id.strip():
            raise ValueError(
                f"job_id должен быть непустой строкой, получено: {job_id!r}"
            )
        self._job_id = job_id
        self._by_coordinate: dict[tuple[str, int, int], IdentifierCellProvenance] = {}

    @property
    def job_id(self) -> str:
        return self._job_id

    # ------------------------------------------------------------------
    # Запись
    # ------------------------------------------------------------------

    def add_many(self, entries: Iterable[IdentifierCellProvenance]) -> None:
        batch = tuple(entries)
        if not batch:
            return

        # Authoritative state — ровно _by_coordinate (см. __init__),
        # третьего индекса/derived mutable state нет, поэтому candidate
        # можно строить прямым shallow-copy этого dict.
        candidate = dict(self._by_coordinate)

        for entry in batch:
            if not isinstance(entry, IdentifierCellProvenance):
                raise TypeError(
                    "add_many() принимает IdentifierCellProvenance, получено: "
                    f"{type(entry)!r}"
                )

            key = self._coordinate_key(entry)
            existing = candidate.get(key)
            if existing is not None:
                if existing == entry:
                    # Идемпотентный повтор: та же запись уже есть по той
                    # же координате (сравнение по всем полям — token и
                    # representation в том числе, sheet_name/row/column
                    # уже совпадают по построению key).
                    continue
                # Либо тот же token с другим representation, либо другой
                # token — в обоих случаях конфликт координаты; сообщение
                # намеренно не включает ни token, ни координаты.
                raise ProvenanceConflictError("Conflicting provenance entry for coordinate.")

            candidate[key] = entry

        # Commit только после полного успеха всего batch.
        self._by_coordinate = candidate

    def clear(self) -> None:
        self._by_coordinate.clear()

    # ------------------------------------------------------------------
    # Чтение
    # ------------------------------------------------------------------

    def get_by_coordinate(
        self, sheet_name: str, row: int, column: int
    ) -> Optional[IdentifierCellProvenance]:
        self._validate_sheet_name(sheet_name)
        self._validate_coordinate_component(row, "row")
        self._validate_coordinate_component(column, "column")
        return self._by_coordinate.get((sheet_name, row, column))

    def entries(self) -> tuple[IdentifierCellProvenance, ...]:
        # Детерминированный snapshot: сортировка по (sheet_name, row,
        # column), а не insertion order — полезно уже сейчас для
        # предсказуемых тестов и будущей deterministic serialization
        # (Stage 7C.3), не только hot lookup path.
        return tuple(
            sorted(self._by_coordinate.values(), key=lambda e: (e.sheet_name, e.row, e.column))
        )

    # ------------------------------------------------------------------
    # Внутренние помощники
    # ------------------------------------------------------------------

    @staticmethod
    def _coordinate_key(entry: IdentifierCellProvenance) -> tuple[str, int, int]:
        return (entry.sheet_name, entry.row, entry.column)

    @staticmethod
    def _validate_sheet_name(sheet_name: object) -> None:
        if not isinstance(sheet_name, str) or not sheet_name.strip():
            raise ValueError(
                f"sheet_name должен быть непустой строкой, получено: {sheet_name!r}"
            )

    @staticmethod
    def _validate_coordinate_component(value: object, name: str) -> None:
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError(f"{name} должен быть int, получено: {type(value)!r}")
        if value < 1:
            raise ValueError(f"{name} должен быть >= 1, получено: {value}")
