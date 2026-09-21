"""
Derived Analytical Workbook Restore (Stage 9C).

Композиция Stage 9C core (app.restore.analytical) и Stage 9C writer
(app.restore.analytical_writer) в одну операцию: "derived analytical
workbook + persistent stores -> restored, локально-readable workbook",
без ProvenanceStore, без job_id, без изменения ни одного frozen-
контракта Stage 8.x/9A/9B.

======================================================================
Value-driven, не coordinate-driven
======================================================================

В отличие от Stage 9B (app.orchestration.restore_single_file), здесь
НЕТ provenance_path/provenance_password/DataAnonymizer.JobId/binding —
derived analytical workbook мог быть произвольно реструктурирован
внешним инструментом анализа, поэтому единственный источник истины —
ТЕКУЩЕЕ значение ячейки (см. app.restore.analytical).

======================================================================
Already-restored guard
======================================================================

Если source уже содержит custom property с exact именем
DataAnonymizer.AnalyticallyRestored (в ЛЮБОМ значении, включая
дубликаты) — restore_analytical_workbook отклоняет вызов ДО какого-либо
scan/resolution: presence-check по имени, без интерпретации значения.
Это предотвращает наиболее вероятную workflow-ошибку (случайно указан
уже восстановленный, а не анонимизированный файл).

======================================================================
Safe cumulative workflow
======================================================================

Результат этой функции — строго локальный, readable artifact,
маркированный DataAnonymizer.AnalyticallyRestored="true". Он НЕ должен
использоваться как anonymized input следующего внешнего AI-цикла
анализа. Stage 9C не реализует и не блокирует какую-либо сетевую
логику — workflow-guard, опирающийся на этот marker, будет реализован в
будущем Stage 10.
"""

from __future__ import annotations

import dataclasses
import os
from pathlib import Path
from typing import Optional, Union

import openpyxl
from openpyxl.workbook.workbook import Workbook

from app.mapping.base import MappingStore
from app.mapping.identifier_base import IdentifierMappingStore
from app.restore.analytical import build_replacement_plan, apply_replacement_plan
from app.restore.analytical_writer import write_analytical_restored_workbook
from app.restore.core import RestoreValidationError

_PathLike = Union[str, Path]

_SUPPORTED_EXTENSION = ".xlsx"
_ANALYTICALLY_RESTORED_PROPERTY_NAME = "DataAnonymizer.AnalyticallyRestored"


@dataclasses.dataclass(frozen=True)
class AnalyticalRestoreResult:
    """
    Минимальный результат успешного restore_analytical_workbook — только
    операционные метаданные, без confidential-данных.
    """

    output_path: Path
    restored_entity_cells: int
    restored_identifier_cells: int


def restore_analytical_workbook(
    source_path: _PathLike,
    destination_path: _PathLike,
    mapping_store: MappingStore,
    identifier_store: Optional[IdentifierMappingStore],
) -> AnalyticalRestoreResult:
    """
    Восстанавливает entity aliases и (опционально) identifier tokens в
    derived analytical workbook, используя persistent stores. Работает
    исключительно value-driven — без provenance, без job_id, без
    привязки к исходной анонимизации.

    :param source_path: путь к derived analytical .xlsx workbook; только
        читается, никогда не изменяется.
    :param destination_path: путь к restored .xlsx workbook; не должен
        существовать (никакой overwrite); родительский каталог должен
        уже существовать.
    :param mapping_store: уже открытый MappingStore; не создаётся, не
        закрывается, не мутируется этой функцией.
    :param identifier_store: уже открытый IdentifierMappingStore, либо
        None — тогда восстанавливаются только entity aliases, identifier
        tokens остаются unchanged.

    :returns: AnalyticalRestoreResult только после полностью успешного
        resolution и атомарной публикации destination.

    :raises TypeError: source_path/destination_path не str/Path.
    :raises RestoreValidationError: нарушение preflight-контракта, либо
        source уже содержит DataAnonymizer.AnalyticallyRestored.
    :raises AmbiguousRestoreError: одна и та же строка одновременно
        найдена как entity alias и как identifier token.
    :raises Exception: любое leaf-исключение openpyxl/IO распространяется
        как есть, без оборачивания.
    """
    source, destination = _preflight(source_path, destination_path)

    workbook = openpyxl.load_workbook(
        str(source), read_only=False, data_only=False, keep_vba=False, keep_links=True
    )
    try:
        _check_not_already_restored(workbook)

        entity_replacements, identifier_replacements = build_replacement_plan(
            workbook, mapping_store, identifier_store
        )

        apply_replacement_plan(workbook, entity_replacements, identifier_replacements)

        write_analytical_restored_workbook(workbook, destination)
    finally:
        workbook.close()

    return AnalyticalRestoreResult(
        output_path=Path(destination_path),
        restored_entity_cells=len(entity_replacements),
        restored_identifier_cells=len(identifier_replacements),
    )


def _check_not_already_restored(workbook: Workbook) -> None:
    matches = [
        prop for prop in workbook.custom_doc_props if prop.name == _ANALYTICALLY_RESTORED_PROPERTY_NAME
    ]
    if matches:
        raise RestoreValidationError(
            f"source workbook уже содержит {_ANALYTICALLY_RESTORED_PROPERTY_NAME!r} — "
            "повторное analytical restore отклонено"
        )


# ----------------------------------------------------------------------
# Preflight
# ----------------------------------------------------------------------


def _preflight(source_path: object, destination_path: object) -> tuple[Path, Path]:
    _validate_path_type(source_path, "source_path")
    _validate_path_type(destination_path, "destination_path")

    source = Path(source_path)
    destination = Path(destination_path)

    if source.suffix.lower() != _SUPPORTED_EXTENSION:
        raise RestoreValidationError(
            f"source_path должен иметь расширение {_SUPPORTED_EXTENSION}, "
            f"получено: {source.suffix!r}"
        )
    if destination.suffix.lower() != _SUPPORTED_EXTENSION:
        raise RestoreValidationError(
            f"destination_path должен иметь расширение {_SUPPORTED_EXTENSION}, "
            f"получено: {destination.suffix!r}"
        )

    if not source.exists():
        raise RestoreValidationError(f"source_path не существует: {source}")
    if not source.is_file():
        raise RestoreValidationError(f"source_path должен быть файлом: {source}")

    if not destination.parent.exists():
        raise RestoreValidationError(
            f"Родительский каталог destination_path не существует: {destination.parent}"
        )
    if not destination.parent.is_dir():
        raise RestoreValidationError(
            f"Родительский каталог destination_path должен быть директорией: {destination.parent}"
        )
    if destination.exists():
        raise RestoreValidationError(f"destination_path уже существует: {destination}")

    if _paths_equal(source, destination):
        raise RestoreValidationError("source_path и destination_path не должны совпадать")

    return source, destination


def _validate_path_type(value: object, name: str) -> None:
    if not isinstance(value, (str, Path)):
        raise TypeError(f"{name} должен быть str или Path, получено: {type(value)!r}")


def _paths_equal(a: Path, b: Path) -> bool:
    """Та же формула, что Stage 8.3/8B/9A/9B/Writer: os.path.normcase(str(path.resolve()))."""
    return os.path.normcase(str(a.resolve())) == os.path.normcase(str(b.resolve()))
