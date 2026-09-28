"""
Неизменяемые модели Stage 10C.1.

WorksheetPolicy — единственные два допустимых значения: PROCESS/
REMOVE_ALL. Worksheet-level KEEP не существует ни как значение enum, ни
как-либо иначе.

IdentifierColumnEvidence/WorksheetScanResult/WorksheetPreparationResult
никогда не хранят: имя листа, FieldRule.column_name/фактический
заголовок, raw identifier/company/person значение, hash значения или
sample — только числовые координаты (worksheet_index/column, 1-based,
worksheet_index — позиция в workbook.worksheets ПОСЛЕ применения
REMOVE_ALL) и агрегированные счётчики/enum.
"""

from __future__ import annotations

import dataclasses
import enum
from pathlib import Path

from app.coverage.errors import ColumnSafetyReason
from app.models.identifiers import IdentifierType


class WorksheetPolicy(enum.Enum):
    PROCESS = "process"
    REMOVE_ALL = "remove_all"


def _is_position(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 1


@dataclasses.dataclass(frozen=True)
class IdentifierColumnEvidence:
    """
    Step A identifier-processing evidence (Stage 10C.1): сколько ячеек
    данной PSEUDONYMIZE-identifier-колонки должно быть обработано Stage 8
    до фактического вызова anonymize_workbook. Step B (сверка с фактическим
    output/store) — задача Stage 10C.4, здесь не реализуется.
    """

    worksheet_index: int
    column: int
    identifier_type: IdentifierType
    expected_count: int

    def __post_init__(self) -> None:
        if not _is_position(self.worksheet_index):
            raise ValueError("IdentifierColumnEvidence.worksheet_index должен быть int >= 1")
        if not _is_position(self.column):
            raise ValueError("IdentifierColumnEvidence.column должен быть int >= 1")
        if not isinstance(self.identifier_type, IdentifierType):
            raise ValueError(
                "IdentifierColumnEvidence.identifier_type должен быть значением IdentifierType, "
                f"получено: {type(self.identifier_type)!r}"
            )
        if (
            isinstance(self.expected_count, bool)
            or not isinstance(self.expected_count, int)
            or self.expected_count < 0
        ):
            raise ValueError("IdentifierColumnEvidence.expected_count должен быть int >= 0")


@dataclasses.dataclass(frozen=True)
class WorksheetScanResult:
    """
    Результат единого прохода по существующим ячейкам одного PROCESS-листа
    (app.coverage.worksheet_policy.scan_process_worksheet): формула-
    детекция, header cross-check, cross-type identifier detector и
    identifier expected-count evidence — вычисленные за один проход.
    """

    formula_found: bool
    header_mismatches: tuple[int, ...]
    keep_violations: tuple[tuple[int, ColumnSafetyReason], ...]
    identifier_evidence: tuple[IdentifierColumnEvidence, ...]


@dataclasses.dataclass(frozen=True)
class WorksheetPreparationResult:
    """
    Результат app.coverage.worksheet_policy.prepare_workbook_for_anonymization:
    путь к временному, уже провалидированному и подготовленному (после
    REMOVE_ALL) workbook, готовому быть переданным как source_path в Stage 8
    anonymize_workbook, плюс агрегированный identifier expected-count
    evidence по всем PROCESS-листам.
    """

    prepared_path: Path
    identifier_evidence: tuple[IdentifierColumnEvidence, ...]
