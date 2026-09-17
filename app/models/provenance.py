"""
Provenance-модели identifier-ячеек (Stage 7C.1).

Этот модуль — ТОЛЬКО модель данных для будущего coordinate-keyed
provenance слоя (Stage 7C.2+), спроектированного отдельно от
IdentifierMappingStore (app.mapping.identifier_base), поскольку они
отвечают на разные вопросы (см. Stage 7C design review):

    - IdentifierMappingStore: глобальная, переиспользуемая между многими
      запусками семантическая identity — (identifier_type, identifier_value)
      <-> token;
    - IdentifierCellProvenance: per-cell метаданные ОДНОГО конкретного
      запуска анонимизации — "в каком именно Python-представлении была
      КОНКРЕТНАЯ ячейка при чтении", необходимые для точного restore
      int/str convergence (Stage 7B.0-correction).

Модуль сознательно НЕ реализует:
    - lookup/store-семантику, O(1) индекс по координате (это
      ProvenanceStore, Stage 7C.2);
    - encryption/serialization/schema_version (это
      EncryptedFileProvenanceStore, Stage 7C.3);
    - job_id — это STORE-level (per-sidecar, не per-entry) свойство
      целого ProvenanceStore (Stage 7C.2), не свойство отдельной записи;
    - anonymizer integration (Stage 7C.4), Writer/Restore (Stage 7C.5+,
      Stage 8, Stage 10);
    - парсинг/валидацию формата token (INN_/KPP_/OGRN_ префикс,
      Crockford-алфавит случайной части) — это ответственность генератора
      (Stage 7B.2) и, при необходимости распознать semantic identity,
      IdentifierMappingStore (Stage 7B.3+). Здесь token — непрозрачная,
      структурно провалидированная строка.

======================================================================
Почему НЕТ identifier_value/identifier_type/job_id
======================================================================

identifier_value и identifier_type уже доступны через
IdentifierMappingStore.get_by_token(token) — дублирование их здесь
создало бы второй, избыточный источник потенциально чувствительных
данных без архитектурной выгоды (защита от этого — явное архитектурное
решение Stage 7C design review). job_id идентифицирует ЦЕЛИКОМ один
provenance sidecar/запуск анонимизации, а не отдельную ячейку — поэтому
он не может быть полем IdentifierCellProvenance по определению (это
свойство контейнера, а не элемента).

======================================================================
Coordinate identity — (sheet_name, row, column), без отдельного класса
======================================================================

Stage 7C.1 сознательно НЕ вводит отдельный Coordinate-класс: тройка полей
(sheet_name, row, column) уже полностью выражает identity ячейки для
будущего ProvenanceStore (Stage 7C.2), который построит по ней O(1)
индекс. Введение дополнительной абстракции сейчас было бы преждевременным
усложнением на уровне модели данных.

======================================================================
sheet_name/token — EXACT, без нормализации
======================================================================

Как и IdentifierMappingEntry (app.models.identifiers), модель не
выполняет strip()/casefold()/normalize над sheet_name/token: `.strip()`
используется ТОЛЬКО как predicate для распознавания
пустой/whitespace-only строки, результат strip() никогда не
присваивается обратно.
"""

from __future__ import annotations

import dataclasses
from enum import Enum


class IdentifierRepresentation(Enum):
    """
    Как именно исходное значение ячейки было представлено в Python на
    момент чтения (до identifier tokenization, Stage 7B.5) — ровно два
    значения, соответствующие принятой type-policy Stage 7B.5/Stage 5
    (str и int — единственные допустимые исходные типы под identifier
    PSEUDONYMIZE; bool/float/прочее отвергаются раньше и провenance для
    них не создаётся).
    """

    STRING = "string"
    INTEGER = "integer"


@dataclasses.dataclass(frozen=True)
class IdentifierCellProvenance:
    """
    Provenance одной identifier-ячейки: по какой координате какой token
    был записан и в каком Python-представлении находилось исходное
    значение.

    Identity ячейки — (sheet_name, row, column); token и representation —
    полезная нагрузка. Намеренно НЕ хранит identifier_value,
    identifier_type (оба выводятся через IdentifierMappingStore) и job_id
    (store-level свойство, не свойство отдельной записи) — см. docstring
    модуля.

    Объект неизменяемый (frozen), как и IdentifierMappingEntry: запись
    provenance — уже свершившийся факт результата обработки ячейки.
    """

    sheet_name: str
    row: int
    column: int
    token: str
    representation: IdentifierRepresentation

    def __post_init__(self) -> None:
        if not isinstance(self.sheet_name, str) or not self.sheet_name.strip():
            raise ValueError(
                "IdentifierCellProvenance.sheet_name обязателен и не может быть "
                "пустым/состоящим только из пробелов"
            )
        if isinstance(self.row, bool) or not isinstance(self.row, int):
            raise ValueError(
                f"IdentifierCellProvenance.row должен быть int, получено: {type(self.row)!r}"
            )
        if self.row < 1:
            raise ValueError(
                f"IdentifierCellProvenance.row должен быть >= 1, получено: {self.row}"
            )
        if isinstance(self.column, bool) or not isinstance(self.column, int):
            raise ValueError(
                f"IdentifierCellProvenance.column должен быть int, получено: {type(self.column)!r}"
            )
        if self.column < 1:
            raise ValueError(
                f"IdentifierCellProvenance.column должен быть >= 1, получено: {self.column}"
            )
        if not isinstance(self.token, str) or not self.token.strip():
            raise ValueError(
                "IdentifierCellProvenance.token обязателен и не может быть "
                "пустым/состоящим только из пробелов"
            )
        if not isinstance(self.representation, IdentifierRepresentation):
            raise ValueError(
                "IdentifierCellProvenance.representation должен быть значением "
                f"IdentifierRepresentation, получено: {type(self.representation)!r}"
            )
