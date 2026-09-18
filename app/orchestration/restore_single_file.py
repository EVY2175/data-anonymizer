"""
Restore Original Workbook (Stage 9B).

Композиция Stage 9A (app.restore.core) и Restore-specific atomic writer
(app.restore.writer) в одну операцию: "один anonymized workbook + его
существующий provenance sidecar + persistent stores -> один restored
workbook", без изменения ни одного frozen-контракта Stage 7C-8B.

======================================================================
Persistent stores — injected, read-only
======================================================================

mapping_store/identifier_store передаются вызывающей стороной; этот
модуль их не создаёт, не закрывает, не мутирует (никогда не вызывает
add/add_many/clear). identifier_store — Optional: допустим None, только
если provenance sidecar не содержит ни одной identifier-записи.

======================================================================
job_id binding — обязательная защита
======================================================================

workbook DataAnonymizer.JobId должен ТОЧНО совпадать с job_id открытого
provenance sidecar ДО любого per-cell resolution. Mismatch — hard failure
всего restore job, ещё до identifier/entity lookup.

======================================================================
All-or-nothing, destination never overwritten
======================================================================

Всё resolution (identifier + entity) выполняется в RAM над уже
загруженным workbook, без мутации, до финального успеха ОБЕИХ фаз.
destination не должен существовать (frozen owner decision) — финальная
гарантия обеспечивается os.rename в app.restore.writer, а не только
preflight-проверкой (см. её docstring).
"""

from __future__ import annotations

import dataclasses
import os
from pathlib import Path
from typing import Optional, Union

import openpyxl

from app.mapping.base import MappingStore
from app.mapping.identifier_base import IdentifierMappingStore
from app.mapping.provenance_encrypted import EncryptedFileProvenanceStore
from app.restore.core import (
    RestoreValidationError,
    apply_replacement_plan,
    check_job_id_binding,
    read_workbook_job_id,
    resolve_entity_replacements,
    resolve_identifier_replacements,
    snapshot_structural_bounds,
)
from app.restore.writer import write_restored_workbook

_PathLike = Union[str, Path]

_SUPPORTED_EXTENSION = ".xlsx"


@dataclasses.dataclass(frozen=True)
class RestoreJobResult:
    """
    Минимальный результат успешного restore_workbook — только
    операционные метаданные, без confidential-данных.
    """

    output_path: Path
    source_job_id: str


def restore_workbook(
    source_path: _PathLike,
    destination_path: _PathLike,
    mapping_store: MappingStore,
    identifier_store: Optional[IdentifierMappingStore],
    *,
    provenance_path: _PathLike,
    provenance_password: str,
) -> RestoreJobResult:
    """
    Восстанавливает anonymized workbook в новый restored workbook,
    используя существующий provenance sidecar и persistent stores.

    :param source_path: путь к anonymized .xlsx книге; только читается,
        никогда не изменяется.
    :param destination_path: путь к restored .xlsx книге; не должен
        существовать (frozen owner decision — никакой overwrite);
        родительский каталог должен уже существовать.
    :param mapping_store: уже открытый MappingStore; не создаётся, не
        закрывается, не мутируется этой функцией.
    :param identifier_store: уже открытый IdentifierMappingStore, либо
        None, если provenance sidecar не содержит identifier-записей.
    :param provenance_path: путь к УЖЕ СУЩЕСТВУЮЩЕМУ encrypted provenance
        sidecar (инверсия Stage 8.3: там provenance_path обязан НЕ
        существовать).
    :param provenance_password: пароль provenance sidecar; opaque
        non-empty строка (exact crypto-контракт, без нормализации).

    :returns: RestoreJobResult только после полностью успешного
        restoration и атомарной публикации destination.

    :raises TypeError: source_path/destination_path/provenance_path не
        str/Path, либо provenance_password не str.
    :raises RestoreValidationError: любое нарушение preflight/binding-
        контракта (см. app.restore.core).
    :raises UnresolvedRestoreError: любая per-cell content-level
        неразрешимость (см. app.restore.core).
    :raises Exception: любое leaf-исключение crypto/openpyxl/IO
        распространяется как есть, без оборачивания.
    """
    source, destination, provenance = _preflight(
        source_path, destination_path, provenance_path, provenance_password
    )

    workbook = openpyxl.load_workbook(
        str(source), read_only=False, data_only=False, keep_vba=False, keep_links=True
    )
    try:
        workbook_job_id = read_workbook_job_id(workbook)

        provenance_store = EncryptedFileProvenanceStore(provenance, provenance_password)
        provenance_entries = provenance_store.entries()

        if provenance_entries and identifier_store is None:
            raise RestoreValidationError(
                "identifier_store обязателен: provenance содержит identifier-записи"
            )

        check_job_id_binding(workbook_job_id, provenance_store.job_id)

        structural_bounds = snapshot_structural_bounds(workbook)

        identifier_plan = resolve_identifier_replacements(
            workbook, structural_bounds, provenance_entries, identifier_store
        )

        excluded_coordinates = {(entry.sheet_name, entry.row, entry.column) for entry in provenance_entries}
        entity_plan = resolve_entity_replacements(
            workbook, structural_bounds, mapping_store, excluded_coordinates
        )

        apply_replacement_plan(workbook, identifier_plan)
        apply_replacement_plan(workbook, entity_plan)

        write_restored_workbook(workbook, destination, source_job_id=workbook_job_id)
    finally:
        workbook.close()

    return RestoreJobResult(output_path=destination, source_job_id=workbook_job_id)


# ----------------------------------------------------------------------
# Preflight
# ----------------------------------------------------------------------


def _preflight(
    source_path: object, destination_path: object, provenance_path: object, provenance_password: object
) -> tuple[Path, Path, Path]:
    _validate_path_type(source_path, "source_path")
    _validate_path_type(destination_path, "destination_path")
    _validate_path_type(provenance_path, "provenance_path")

    source = Path(source_path)
    destination = Path(destination_path)
    provenance = Path(provenance_path)

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
    if destination.exists():
        raise RestoreValidationError(f"destination_path уже существует: {destination}")

    if not provenance.exists():
        raise RestoreValidationError(f"provenance_path не существует: {provenance}")
    if not provenance.is_file():
        raise RestoreValidationError(f"provenance_path должен быть файлом: {provenance}")

    if _paths_equal(source, destination):
        raise RestoreValidationError("source_path и destination_path не должны совпадать")
    if _paths_equal(source, provenance):
        raise RestoreValidationError("source_path и provenance_path не должны совпадать")
    if _paths_equal(destination, provenance):
        raise RestoreValidationError("destination_path и provenance_path не должны совпадать")

    _validate_provenance_password(provenance_password)

    return source, destination, provenance


def _validate_path_type(value: object, name: str) -> None:
    if not isinstance(value, (str, Path)):
        raise TypeError(f"{name} должен быть str или Path, получено: {type(value)!r}")


def _paths_equal(a: Path, b: Path) -> bool:
    """Та же формула, что Stage 8.3/8B/Writer: os.path.normcase(str(path.resolve()))."""
    return os.path.normcase(str(a.resolve())) == os.path.normcase(str(b.resolve()))


def _validate_provenance_password(password: object) -> None:
    # Точное зеркалирование Stage 8.3: exact str, "" отклоняется БЕЗ
    # .strip() — whitespace-only пароль принимается.
    if not isinstance(password, str):
        raise TypeError(f"provenance_password должен быть str, получено: {type(password)!r}")
    if password == "":
        raise RestoreValidationError("provenance_password не может быть пустым")
