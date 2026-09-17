"""
Абстракция provenance-хранилища для identifier-ячеек (Stage 7C.2).

======================================================================
Почему это НЕ IdentifierMappingStore
======================================================================

ProvenanceStore архитектурно похож на IdentifierMappingStore
(app.mapping.identifier_base) там, где это действительно полезно
(add_many с идемпотентным точным дублем и конфликтом, all-or-nothing
batch, O(1) lookup, snapshot-семантика entries()), но сознательно НЕ
является его обобщением и НЕ разделяет с ним общий базовый
класс/exception — они отвечают на принципиально разные вопросы (см.
Stage 7C design review):

    - IdentifierMappingStore: token <-> (identifier_type, identifier_value)
      — глобальная, переиспользуемая МЕЖДУ МНОГИМИ запусками анонимизации
      семантическая identity;
    - ProvenanceStore: coordinate (sheet_name, row, column) -> (token,
      representation) — per-cell метаданные ОДНОГО конкретного запуска
      анонимизации (job), идентифицируемого job_id.

Из этого следует:

    - identity здесь ТРЁХкомпонентная — (sheet_name, row, column), а не
      (identifier_type, identifier_value);
    - здесь НЕТ single-entry add(): единственный write-путь — add_many().
      Provenance всегда производится как результат обработки ЦЕЛОЙ
      таблицы за один раз (а не по одной независимой записи в разных,
      несвязанных контекстах, как исторически было с
      IdentifierMappingStore.add() до появления add_many в Stage 7B.4.3)
      — легитимного incremental single-write use case для provenance не
      найдено (Stage 7C design review);
    - store дополнительно несёт REQUIRED, STORE-level job_id — свойство
      ЦЕЛОГО store/sidecar, не свойство отдельной записи, поэтому не
      входит в IdentifierCellProvenance (app.models.provenance). Store
      НЕ генерирует job_id самостоятельно — он передаётся вызывающим
      кодом при создании;
    - ProvenanceConflictError НЕ наследуется от IdentifierMappingConflictError
      и ни от чего специального — тот же принцип "нет общего базового
      exception между разными identity-пространствами", что уже применён
      между Stage 2 (MappingConflictError) и Stage 7B.3
      (IdentifierMappingConflictError).

======================================================================
Что этот модуль НЕ делает
======================================================================

Не выполняет: encrypted persistence, JSON-сериализацию, работу с
файлами, crypto, atomic writes (это будущий Stage 7C.3); не генерирует
job_id (ответственность вызывающего кода, вне этого модуля); не
интегрируется с anonymizer.py (Stage 7C.4); не участвует в Writer/Restore
(Stage 7C.5+, Stage 8, Stage 10).

======================================================================
Coordinate identity — EXACT, без нормализации
======================================================================

sheet_name не нормализуется (strip/casefold/NFC) — "Sheet1" и " Sheet1 "
разные coordinate identity. Тот же exact-value принцип, что уже применён
к identifier_value/token в Stage 7B.

======================================================================
Безопасность: token/job_id/sheet_name/координаты никогда не попадают в
текст конфликтных исключений
======================================================================

ProvenanceConflictError сообщает только сам факт конфликта — не
включает token, job_id, sheet_name, координаты или что-либо ещё,
потенциально идентифицирующее данные. Это даже строже, чем
IdentifierMappingConflictError (который безопасно включает token и
identifier_type-категорию, но не raw identifier_value) — для provenance
не найдено необходимости включать в сообщение вообще что-либо, кроме
факта конфликта.
"""

from __future__ import annotations

import abc
from collections.abc import Iterable
from typing import Optional

from app.models.provenance import IdentifierCellProvenance


class ProvenanceConflictError(Exception):
    """
    Попытка добавить в ProvenanceStore запись, которая противоречит уже
    имеющимся данным: та же координата (sheet_name, row, column) уже
    связана с другим token, либо с тем же token, но другим
    representation.

    Сообщение НИКОГДА не включает token/job_id/sheet_name/координаты —
    только сам факт конфликта.
    """


class ProvenanceStore(abc.ABC):
    """
    Контракт provenance-хранилища: coordinate (sheet_name, row, column)
    -> IdentifierCellProvenance (token + representation), в рамках ОДНОГО
    anonymization job, идентифицируемого job_id.

    Оба компонента coordinate lookup (все три: sheet_name, row, column)
    обязательны при любом обращении — partial lookup не поддерживается и
    не нужен (координата всегда полностью известна на вызывающей
    стороне).
    """

    @property
    @abc.abstractmethod
    def job_id(self) -> str:
        """
        Opaque identifier этого store/sidecar — REQUIRED, устанавливается
        вызывающим кодом при создании store (store НЕ генерирует его
        самостоятельно и не оценивает его формат/энтропию). Не выводится
        и не проверяется против workbook здесь — сравнение с
        workbook-level job_id является ответственностью будущего Restore
        (Stage 7C.5+).
        """

    @abc.abstractmethod
    def add_many(self, entries: Iterable[IdentifierCellProvenance]) -> None:
        """
        Добавляет сразу несколько provenance-записей атомарно
        (all-or-nothing).

        entries материализуется ровно один раз (list/tuple/generator/
        one-shot iterator принимаются одинаково); если сам entries
        поднимает исключение во время материализации, хранилище остаётся
        полностью неизменным (материализация происходит раньше любой
        мутации состояния).

        Пустой entries — no-op.

        Точный дубликат (та же coordinate identity и полностью равная
        запись, что уже есть в хранилище либо ранее в этом же batch) —
        no-op для этой записи, не конфликт.

        Если хотя бы одна запись batch конфликтует (та же coordinate с
        другим token, либо та же coordinate и тот же token, но другим
        representation — против уже существующего состояния ИЛИ против
        другой записи этого же batch), поднимается ProvenanceConflictError
        и НИ ОДНА запись batch не применяется — хранилище остаётся в
        точности таким, каким было до вызова.

        Одинаковый token в разных координатах — нормальный, ожидаемый
        случай (один и тот же identifier может встречаться много раз в
        таблице) и НЕ является конфликтом.

        :raises ProvenanceConflictError: если любая запись batch
            конфликтует с уже существующими данными или с другой записью
            этого же batch.
        :raises TypeError: если entries содержит элемент, не являющийся
            IdentifierCellProvenance.
        """

    @abc.abstractmethod
    def get_by_coordinate(
        self, sheet_name: str, row: int, column: int
    ) -> Optional[IdentifierCellProvenance]:
        """
        Ищет запись по полной coordinate identity (sheet_name, row, column).

        Все три компонента обязательны — partial lookup не поддерживается.
        sheet_name не нормализуется.

        :returns: запись, либо None, если такой координаты нет.
        """

    @abc.abstractmethod
    def entries(self) -> tuple[IdentifierCellProvenance, ...]:
        """
        Возвращает snapshot всех записей хранилища, детерминированно
        отсортированный по (sheet_name, row, column).

        IdentifierCellProvenance неизменяем (frozen), а сам возвращаемый
        tuple — независимая копия внутренней коллекции.
        """

    @abc.abstractmethod
    def clear(self) -> None:
        """
        Полностью очищает хранилище (все provenance-записи). job_id при
        этом НЕ меняется — это свойство самого store, а не его
        содержимого.
        """
