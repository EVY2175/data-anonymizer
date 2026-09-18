"""
Тесты Restore Original Workbook (Stage 9B) —
app.orchestration.restore_single_file.restore_workbook.

Интеграционные тесты полного pipeline: реальный anonymize_workbook/
anonymize_workbooks (Stage 8.3/8B, без изменений) используется для
получения подлинных anonymized workbook + provenance sidecar, после чего
проверяется restore_workbook. Все данные synthetic; ИНН — валидный по
чек-сумме, но синтетический.
"""

from __future__ import annotations

from pathlib import Path

import openpyxl
import pytest
from openpyxl.packaging.custom import IntProperty, StringProperty

from app.mapping.identifier_memory import InMemoryIdentifierMappingStore
from app.mapping.memory import InMemoryMappingStore
from app.models.identifiers import IdentifierMappingEntry, IdentifierType
from app.models.rules import Action, FieldRule, FieldType
from app.orchestration.batch import AnonymizationBatchItem, anonymize_workbooks
from app.orchestration.restore_single_file import RestoreJobResult, restore_workbook
from app.orchestration.single_file import anonymize_workbook
from app.restore.core import RestoreValidationError, UnresolvedRestoreError

VALID_INN = "7707083893"
COMPANY_NAME = "ООО Ромашка"
PASSWORD = "test-restore-password"
JOB_ID_PROPERTY = "DataAnonymizer.JobId"
RESTORED_FROM_JOB_ID_PROPERTY = "DataAnonymizer.RestoredFromJobId"


# ---------------------------------------------------------------------------
# Фикстуры/помощники
# ---------------------------------------------------------------------------


def _source_workbook(path: Path, sheet_name: str, headers: list[object], rows: list[list[object]]) -> None:
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = sheet_name
    for col_idx, header in enumerate(headers, start=1):
        ws.cell(row=1, column=col_idx, value=header)
    for row_idx, row in enumerate(rows, start=2):
        for col_idx, value in enumerate(row, start=1):
            ws.cell(row=row_idx, column=col_idx, value=value)
    wb.save(path)


def _company_rule(column_name: str = "Company") -> FieldRule:
    return FieldRule(column_name=column_name, field_type=FieldType.COMPANY, action=Action.PSEUDONYMIZE)


def _inn_rule(column_name: str = "INN") -> FieldRule:
    return FieldRule(column_name=column_name, field_type=FieldType.INN, action=Action.PSEUDONYMIZE)


@pytest.fixture()
def mapping_store() -> InMemoryMappingStore:
    return InMemoryMappingStore()


@pytest.fixture()
def identifier_store() -> InMemoryIdentifierMappingStore:
    return InMemoryIdentifierMappingStore()


@pytest.fixture()
def anonymized_fixture(
    tmp_path: Path, mapping_store: InMemoryMappingStore, identifier_store: InMemoryIdentifierMappingStore
) -> tuple[Path, Path, str]:
    """Реальный (anonymized_path, provenance_path, job_id) — company + INN, одна строка."""
    source = tmp_path / "plain_source.xlsx"
    _source_workbook(source, "Data", ["Company", "INN"], [[COMPANY_NAME, VALID_INN]])
    anonymized = tmp_path / "anonymized.xlsx"
    provenance = tmp_path / "provenance.enc"
    rules = {"Data": {1: _company_rule(), 2: _inn_rule()}}
    result = anonymize_workbook(
        source, anonymized, rules, mapping_store, identifier_store,
        provenance_path=provenance, provenance_password=PASSWORD,
    )
    return anonymized, provenance, result.job_id


def _dummy_provenance_file(tmp_path: Path, name: str = "dummy_provenance.enc") -> Path:
    path = tmp_path / name
    path.write_bytes(b"not a real encrypted provenance container")
    return path


# ---------------------------------------------------------------------------
# A. Preflight
# ---------------------------------------------------------------------------


def test_invalid_source_type_raises(mapping_store: InMemoryMappingStore) -> None:
    with pytest.raises(TypeError):
        restore_workbook(
            123, "dest.xlsx", mapping_store, None, provenance_path="prov.enc", provenance_password=PASSWORD
        )


def test_invalid_destination_type_raises(mapping_store: InMemoryMappingStore) -> None:
    with pytest.raises(TypeError):
        restore_workbook(
            "source.xlsx", 123, mapping_store, None, provenance_path="prov.enc", provenance_password=PASSWORD
        )


def test_invalid_provenance_type_raises(mapping_store: InMemoryMappingStore) -> None:
    with pytest.raises(TypeError):
        restore_workbook(
            "source.xlsx", "dest.xlsx", mapping_store, None, provenance_path=123, provenance_password=PASSWORD
        )


def test_source_non_xlsx_raises(tmp_path: Path, mapping_store: InMemoryMappingStore) -> None:
    bad_source = tmp_path / "source.txt"
    bad_source.write_text("not excel", encoding="utf-8")
    destination = tmp_path / "restored.xlsx"
    provenance = _dummy_provenance_file(tmp_path)

    with pytest.raises(RestoreValidationError):
        restore_workbook(
            bad_source, destination, mapping_store, None, provenance_path=provenance, provenance_password=PASSWORD
        )


def test_destination_non_xlsx_raises(
    tmp_path: Path, mapping_store: InMemoryMappingStore, anonymized_fixture: tuple[Path, Path, str]
) -> None:
    anonymized, provenance, _ = anonymized_fixture
    destination = tmp_path / "restored.txt"

    with pytest.raises(RestoreValidationError):
        restore_workbook(
            anonymized, destination, mapping_store, None, provenance_path=provenance, provenance_password=PASSWORD
        )


def test_source_missing_raises(tmp_path: Path, mapping_store: InMemoryMappingStore) -> None:
    missing_source = tmp_path / "nope.xlsx"
    destination = tmp_path / "restored.xlsx"
    provenance = _dummy_provenance_file(tmp_path)

    with pytest.raises(RestoreValidationError):
        restore_workbook(
            missing_source, destination, mapping_store, None, provenance_path=provenance, provenance_password=PASSWORD
        )


def test_source_is_directory_raises(tmp_path: Path, mapping_store: InMemoryMappingStore) -> None:
    directory_as_source = tmp_path / "source_dir.xlsx"
    directory_as_source.mkdir()
    destination = tmp_path / "restored.xlsx"
    provenance = _dummy_provenance_file(tmp_path)

    with pytest.raises(RestoreValidationError):
        restore_workbook(
            directory_as_source, destination, mapping_store, None,
            provenance_path=provenance, provenance_password=PASSWORD,
        )


def test_destination_parent_missing_raises(
    tmp_path: Path, mapping_store: InMemoryMappingStore, anonymized_fixture: tuple[Path, Path, str]
) -> None:
    anonymized, provenance, _ = anonymized_fixture
    destination = tmp_path / "missing_dir" / "restored.xlsx"

    with pytest.raises(RestoreValidationError):
        restore_workbook(
            anonymized, destination, mapping_store, None, provenance_path=provenance, provenance_password=PASSWORD
        )


def test_destination_already_exists_raises(
    tmp_path: Path, mapping_store: InMemoryMappingStore, anonymized_fixture: tuple[Path, Path, str]
) -> None:
    anonymized, provenance, _ = anonymized_fixture
    destination = tmp_path / "restored.xlsx"
    destination.write_bytes(b"already here")

    with pytest.raises(RestoreValidationError):
        restore_workbook(
            anonymized, destination, mapping_store, None, provenance_path=provenance, provenance_password=PASSWORD
        )
    assert destination.read_bytes() == b"already here"  # не тронут


def test_provenance_missing_raises(
    tmp_path: Path, mapping_store: InMemoryMappingStore, anonymized_fixture: tuple[Path, Path, str]
) -> None:
    anonymized, _, _ = anonymized_fixture
    destination = tmp_path / "restored.xlsx"
    missing_provenance = tmp_path / "nope.enc"

    with pytest.raises(RestoreValidationError):
        restore_workbook(
            anonymized, destination, mapping_store, None,
            provenance_path=missing_provenance, provenance_password=PASSWORD,
        )


def test_provenance_is_directory_raises(
    tmp_path: Path, mapping_store: InMemoryMappingStore, anonymized_fixture: tuple[Path, Path, str]
) -> None:
    anonymized, _, _ = anonymized_fixture
    destination = tmp_path / "restored.xlsx"
    directory_as_provenance = tmp_path / "provenance_dir.enc"
    directory_as_provenance.mkdir()

    with pytest.raises(RestoreValidationError):
        restore_workbook(
            anonymized, destination, mapping_store, None,
            provenance_path=directory_as_provenance, provenance_password=PASSWORD,
        )


def test_source_equals_destination_raises(
    mapping_store: InMemoryMappingStore, anonymized_fixture: tuple[Path, Path, str]
) -> None:
    anonymized, provenance, _ = anonymized_fixture

    with pytest.raises(RestoreValidationError):
        restore_workbook(
            anonymized, anonymized, mapping_store, None, provenance_path=provenance, provenance_password=PASSWORD
        )


def test_source_equals_provenance_raises(
    tmp_path: Path, mapping_store: InMemoryMappingStore, anonymized_fixture: tuple[Path, Path, str]
) -> None:
    anonymized, _, _ = anonymized_fixture
    destination = tmp_path / "restored.xlsx"

    with pytest.raises(RestoreValidationError):
        restore_workbook(
            anonymized, destination, mapping_store, None, provenance_path=anonymized, provenance_password=PASSWORD
        )


def test_destination_equals_provenance_raises(
    mapping_store: InMemoryMappingStore, anonymized_fixture: tuple[Path, Path, str]
) -> None:
    anonymized, provenance, _ = anonymized_fixture

    with pytest.raises(RestoreValidationError):
        restore_workbook(
            anonymized, provenance, mapping_store, None, provenance_path=provenance, provenance_password=PASSWORD
        )


def test_password_non_str_raises_type_error(
    tmp_path: Path, mapping_store: InMemoryMappingStore, anonymized_fixture: tuple[Path, Path, str]
) -> None:
    anonymized, provenance, _ = anonymized_fixture
    destination = tmp_path / "restored.xlsx"

    with pytest.raises(TypeError):
        restore_workbook(
            anonymized, destination, mapping_store, None, provenance_path=provenance, provenance_password=12345  # type: ignore[arg-type]
        )


def test_password_exact_empty_raises(
    tmp_path: Path, mapping_store: InMemoryMappingStore, anonymized_fixture: tuple[Path, Path, str]
) -> None:
    anonymized, provenance, _ = anonymized_fixture
    destination = tmp_path / "restored.xlsx"

    with pytest.raises(RestoreValidationError):
        restore_workbook(anonymized, destination, mapping_store, None, provenance_path=provenance, provenance_password="")


def test_whitespace_only_password_accepted(
    tmp_path: Path, mapping_store: InMemoryMappingStore, identifier_store: InMemoryIdentifierMappingStore
) -> None:
    source = tmp_path / "plain_source.xlsx"
    _source_workbook(source, "Data", ["Company"], [[COMPANY_NAME]])
    anonymized = tmp_path / "anonymized.xlsx"
    provenance = tmp_path / "provenance.enc"
    anonymize_workbook(
        source, anonymized, {"Data": {1: _company_rule()}}, mapping_store, None,
        provenance_path=provenance, provenance_password="   ",
    )
    destination = tmp_path / "restored.xlsx"

    result = restore_workbook(
        anonymized, destination, mapping_store, None, provenance_path=provenance, provenance_password="   "
    )

    assert isinstance(result, RestoreJobResult)
    assert destination.exists()


# ---------------------------------------------------------------------------
# B. Metadata / binding
# ---------------------------------------------------------------------------


def test_missing_job_id_property_raises(tmp_path: Path, mapping_store: InMemoryMappingStore) -> None:
    plain_source = tmp_path / "not_anonymized.xlsx"
    _source_workbook(plain_source, "Data", ["Company"], [[COMPANY_NAME]])  # никогда не проходил anonymize_workbook
    destination = tmp_path / "restored.xlsx"
    provenance = _dummy_provenance_file(tmp_path)

    with pytest.raises(RestoreValidationError):
        restore_workbook(
            plain_source, destination, mapping_store, None, provenance_path=provenance, provenance_password=PASSWORD
        )


def _corrupt_job_id(anonymized: Path, corrupted_path: Path, mutate) -> None:
    wb = openpyxl.load_workbook(anonymized)
    while True:
        try:
            del wb.custom_doc_props[JOB_ID_PROPERTY]
        except KeyError:
            break
    mutate(wb)
    wb.save(corrupted_path)
    wb.close()


def test_duplicate_job_id_property_raises(
    tmp_path: Path, mapping_store: InMemoryMappingStore, anonymized_fixture: tuple[Path, Path, str]
) -> None:
    anonymized, provenance, job_id = anonymized_fixture
    corrupted = tmp_path / "corrupted.xlsx"
    _corrupt_job_id(
        anonymized, corrupted,
        lambda wb: (
            wb.custom_doc_props.props.append(StringProperty(name=JOB_ID_PROPERTY, value=job_id)),
            wb.custom_doc_props.props.append(StringProperty(name=JOB_ID_PROPERTY, value=job_id)),
        ),
    )
    destination = tmp_path / "restored.xlsx"

    with pytest.raises(RestoreValidationError):
        restore_workbook(
            corrupted, destination, mapping_store, None, provenance_path=provenance, provenance_password=PASSWORD
        )


def test_empty_job_id_property_raises(
    tmp_path: Path, mapping_store: InMemoryMappingStore, anonymized_fixture: tuple[Path, Path, str]
) -> None:
    anonymized, provenance, _ = anonymized_fixture
    corrupted = tmp_path / "corrupted.xlsx"
    _corrupt_job_id(anonymized, corrupted, lambda wb: wb.custom_doc_props.append(StringProperty(name=JOB_ID_PROPERTY, value="")))
    destination = tmp_path / "restored.xlsx"

    with pytest.raises(RestoreValidationError):
        restore_workbook(
            corrupted, destination, mapping_store, None, provenance_path=provenance, provenance_password=PASSWORD
        )


def test_whitespace_only_job_id_property_raises(
    tmp_path: Path, mapping_store: InMemoryMappingStore, anonymized_fixture: tuple[Path, Path, str]
) -> None:
    anonymized, provenance, _ = anonymized_fixture
    corrupted = tmp_path / "corrupted.xlsx"
    _corrupt_job_id(anonymized, corrupted, lambda wb: wb.custom_doc_props.append(StringProperty(name=JOB_ID_PROPERTY, value="   ")))
    destination = tmp_path / "restored.xlsx"

    with pytest.raises(RestoreValidationError):
        restore_workbook(
            corrupted, destination, mapping_store, None, provenance_path=provenance, provenance_password=PASSWORD
        )


def test_non_string_job_id_property_raises(
    tmp_path: Path, mapping_store: InMemoryMappingStore, anonymized_fixture: tuple[Path, Path, str]
) -> None:
    anonymized, provenance, _ = anonymized_fixture
    corrupted = tmp_path / "corrupted.xlsx"
    _corrupt_job_id(anonymized, corrupted, lambda wb: wb.custom_doc_props.append(IntProperty(name=JOB_ID_PROPERTY, value=42)))
    destination = tmp_path / "restored.xlsx"

    with pytest.raises(RestoreValidationError):
        restore_workbook(
            corrupted, destination, mapping_store, None, provenance_path=provenance, provenance_password=PASSWORD
        )


def test_job_id_mismatch_raises(
    tmp_path: Path, mapping_store: InMemoryMappingStore, identifier_store: InMemoryIdentifierMappingStore
) -> None:
    # Два независимых job — workbook от одного, provenance от другого.
    source_a = tmp_path / "source_a.xlsx"
    _source_workbook(source_a, "Data", ["Company"], [[COMPANY_NAME]])
    anonymized_a = tmp_path / "anonymized_a.xlsx"
    provenance_a = tmp_path / "provenance_a.enc"
    anonymize_workbook(
        source_a, anonymized_a, {"Data": {1: _company_rule()}}, mapping_store, None,
        provenance_path=provenance_a, provenance_password=PASSWORD,
    )

    source_b = tmp_path / "source_b.xlsx"
    _source_workbook(source_b, "Data", ["Company"], [["ООО Лютик"]])
    anonymized_b = tmp_path / "anonymized_b.xlsx"
    provenance_b = tmp_path / "provenance_b.enc"
    anonymize_workbook(
        source_b, anonymized_b, {"Data": {1: _company_rule()}}, mapping_store, None,
        provenance_path=provenance_b, provenance_password=PASSWORD,
    )

    destination = tmp_path / "restored.xlsx"
    with pytest.raises(RestoreValidationError):
        restore_workbook(
            anonymized_a, destination, mapping_store, None,
            provenance_path=provenance_b, provenance_password=PASSWORD,  # sidecar от ДРУГОГО job
        )
    assert not destination.exists()


def test_workbook_closed_after_failure(
    tmp_path: Path,
    mapping_store: InMemoryMappingStore,
    identifier_store: InMemoryIdentifierMappingStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    Doказывает, что workbook.close() вызывается ровно один раз даже при
    content-level сбое (token mismatch), возникающем ПОСЛЕ успешного
    load_workbook внутри restore_workbook — то есть внутри try/finally.
    Spy устанавливается на РЕАЛЬНЫЙ объект, возвращённый реальным
    load_workbook, а не на мок всей restore-логики.
    """
    source = tmp_path / "plain_source.xlsx"
    _source_workbook(source, "Data", ["Company", "INN"], [[COMPANY_NAME, VALID_INN]])
    anonymized = tmp_path / "anonymized.xlsx"
    provenance = tmp_path / "provenance.enc"
    anonymize_workbook(
        source, anonymized, {"Data": {1: _company_rule(), 2: _inn_rule()}}, mapping_store, identifier_store,
        provenance_path=provenance, provenance_password=PASSWORD,
    )

    # Ломаем identifier-ячейку -> token mismatch, сбой строго после load.
    wb = openpyxl.load_workbook(anonymized)
    wb["Data"]["B2"] = "TAMPERED"
    edited = tmp_path / "edited.xlsx"
    wb.save(edited)
    wb.close()

    close_calls = {"n": 0}
    real_load_workbook = openpyxl.load_workbook

    def spying_load_workbook(*args: object, **kwargs: object) -> object:
        loaded = real_load_workbook(*args, **kwargs)
        real_close = loaded.close

        def counting_close() -> None:
            close_calls["n"] += 1
            real_close()

        loaded.close = counting_close
        return loaded

    monkeypatch.setattr(openpyxl, "load_workbook", spying_load_workbook)

    destination = tmp_path / "restored.xlsx"
    with pytest.raises(UnresolvedRestoreError):
        restore_workbook(
            edited, destination, mapping_store, identifier_store,
            provenance_path=provenance, provenance_password=PASSWORD,
        )

    monkeypatch.undo()
    assert close_calls["n"] == 1


def test_correct_binding_succeeds(
    mapping_store: InMemoryMappingStore,
    identifier_store: InMemoryIdentifierMappingStore,
    tmp_path: Path,
    anonymized_fixture: tuple[Path, Path, str],
) -> None:
    anonymized, provenance, job_id = anonymized_fixture
    destination = tmp_path / "restored.xlsx"

    result = restore_workbook(
        anonymized, destination, mapping_store, identifier_store,
        provenance_path=provenance, provenance_password=PASSWORD,
    )

    assert result.source_job_id == job_id


# ---------------------------------------------------------------------------
# C/D. Identifier + Entity — интеграционный happy path
# ---------------------------------------------------------------------------


def test_full_restore_company_and_inn(
    tmp_path: Path,
    mapping_store: InMemoryMappingStore,
    identifier_store: InMemoryIdentifierMappingStore,
    anonymized_fixture: tuple[Path, Path, str],
) -> None:
    anonymized, provenance, job_id = anonymized_fixture
    destination = tmp_path / "restored.xlsx"

    result = restore_workbook(
        anonymized, destination, mapping_store, identifier_store,
        provenance_path=provenance, provenance_password=PASSWORD,
    )

    assert result == RestoreJobResult(output_path=destination, source_job_id=job_id)

    output = openpyxl.load_workbook(destination)
    assert output["Data"]["A2"].value == COMPANY_NAME
    assert output["Data"]["B2"].value == VALID_INN


def test_identifier_store_none_with_nonempty_provenance_raises(
    tmp_path: Path, mapping_store: InMemoryMappingStore, anonymized_fixture: tuple[Path, Path, str]
) -> None:
    anonymized, provenance, _ = anonymized_fixture  # содержит INN-provenance
    destination = tmp_path / "restored.xlsx"

    with pytest.raises(RestoreValidationError):
        restore_workbook(
            anonymized, destination, mapping_store, None, provenance_path=provenance, provenance_password=PASSWORD
        )
    assert not destination.exists()


def test_identifier_store_none_with_empty_provenance_succeeds(
    tmp_path: Path, mapping_store: InMemoryMappingStore
) -> None:
    source = tmp_path / "plain_source.xlsx"
    _source_workbook(source, "Data", ["Company"], [[COMPANY_NAME]])  # без identifier-полей
    anonymized = tmp_path / "anonymized.xlsx"
    provenance = tmp_path / "provenance.enc"
    anonymize_workbook(
        source, anonymized, {"Data": {1: _company_rule()}}, mapping_store, None,
        provenance_path=provenance, provenance_password=PASSWORD,
    )
    destination = tmp_path / "restored.xlsx"

    result = restore_workbook(
        anonymized, destination, mapping_store, None, provenance_path=provenance, provenance_password=PASSWORD
    )

    assert destination.exists()
    assert isinstance(result, RestoreJobResult)


def test_wrong_identifier_store_causes_unresolved_error(
    tmp_path: Path, mapping_store: InMemoryMappingStore, anonymized_fixture: tuple[Path, Path, str]
) -> None:
    anonymized, provenance, _ = anonymized_fixture
    wrong_identifier_store = InMemoryIdentifierMappingStore()  # "не тот" store, token отсутствует
    destination = tmp_path / "restored.xlsx"

    with pytest.raises(UnresolvedRestoreError):
        restore_workbook(
            anonymized, destination, mapping_store, wrong_identifier_store,
            provenance_path=provenance, provenance_password=PASSWORD,
        )
    assert not destination.exists()


# ---------------------------------------------------------------------------
# E. All-or-nothing
# ---------------------------------------------------------------------------


def test_later_identifier_failure_means_no_destination(
    tmp_path: Path, mapping_store: InMemoryMappingStore, identifier_store: InMemoryIdentifierMappingStore
) -> None:
    source = tmp_path / "plain_source.xlsx"
    _source_workbook(source, "Data", ["Company", "INN"], [[COMPANY_NAME, VALID_INN], ["ООО Лютик", "7707083886"]])
    anonymized = tmp_path / "anonymized.xlsx"
    provenance = tmp_path / "provenance.enc"
    anonymize_workbook(
        source, anonymized, {"Data": {1: _company_rule(), 2: _inn_rule()}}, mapping_store, identifier_store,
        provenance_path=provenance, provenance_password=PASSWORD,
    )

    # Эмулируем "отредактированный" workbook: значение INN во ВТОРОЙ строке
    # изменено после анонимизации — token mismatch на этой координате.
    wb = openpyxl.load_workbook(anonymized)
    wb["Data"]["B3"] = "EDITED_TOKEN"
    edited = tmp_path / "edited.xlsx"
    wb.save(edited)
    wb.close()

    source_before = edited.read_bytes()
    provenance_before = provenance.read_bytes()

    destination = tmp_path / "restored.xlsx"
    with pytest.raises(UnresolvedRestoreError):
        restore_workbook(
            edited, destination, mapping_store, identifier_store,
            provenance_path=provenance, provenance_password=PASSWORD,
        )

    assert not destination.exists()
    assert edited.read_bytes() == source_before
    assert provenance.read_bytes() == provenance_before


def test_entity_and_identifier_replacements_applied_together_atomically(
    tmp_path: Path,
    mapping_store: InMemoryMappingStore,
    identifier_store: InMemoryIdentifierMappingStore,
    anonymized_fixture: tuple[Path, Path, str],
) -> None:
    anonymized, provenance, _ = anonymized_fixture
    destination = tmp_path / "restored.xlsx"

    restore_workbook(
        anonymized, destination, mapping_store, identifier_store,
        provenance_path=provenance, provenance_password=PASSWORD,
    )

    output = openpyxl.load_workbook(destination)
    assert output["Data"]["A2"].value == COMPANY_NAME
    assert output["Data"]["B2"].value == VALID_INN


def test_stores_unchanged_after_failed_restore(
    tmp_path: Path, mapping_store: InMemoryMappingStore, anonymized_fixture: tuple[Path, Path, str]
) -> None:
    anonymized, provenance, _ = anonymized_fixture
    entries_before = mapping_store.entries()
    destination = tmp_path / "restored.xlsx"

    with pytest.raises(RestoreValidationError):
        # identifier_store=None при непустом provenance -> RestoreValidationError.
        restore_workbook(
            anonymized, destination, mapping_store, None, provenance_path=provenance, provenance_password=PASSWORD
        )

    assert mapping_store.entries() == entries_before


# ---------------------------------------------------------------------------
# F. Output
# ---------------------------------------------------------------------------


def test_source_unchanged_after_restore(
    tmp_path: Path,
    mapping_store: InMemoryMappingStore,
    identifier_store: InMemoryIdentifierMappingStore,
    anonymized_fixture: tuple[Path, Path, str],
) -> None:
    anonymized, provenance, _ = anonymized_fixture
    bytes_before = anonymized.read_bytes()
    destination = tmp_path / "restored.xlsx"

    restore_workbook(
        anonymized, destination, mapping_store, identifier_store,
        provenance_path=provenance, provenance_password=PASSWORD,
    )

    assert anonymized.read_bytes() == bytes_before


def test_provenance_sidecar_bytes_unchanged_after_restore(
    tmp_path: Path,
    mapping_store: InMemoryMappingStore,
    identifier_store: InMemoryIdentifierMappingStore,
    anonymized_fixture: tuple[Path, Path, str],
) -> None:
    anonymized, provenance, _ = anonymized_fixture
    provenance_bytes_before = provenance.read_bytes()
    destination = tmp_path / "restored.xlsx"

    restore_workbook(
        anonymized, destination, mapping_store, identifier_store,
        provenance_path=provenance, provenance_password=PASSWORD,
    )

    assert provenance.read_bytes() == provenance_bytes_before


def test_sheet_order_hidden_formulas_formatting_merged_preserved(
    tmp_path: Path, mapping_store: InMemoryMappingStore, identifier_store: InMemoryIdentifierMappingStore
) -> None:
    from openpyxl.styles import Font

    source = tmp_path / "rich_source.xlsx"
    wb = openpyxl.Workbook()
    ws1 = wb.active
    ws1.title = "First"
    ws1["A1"] = "Company"
    ws1["A2"] = COMPANY_NAME
    ws1["B1"] = "Bold"
    ws1["B2"] = "important"
    ws1["B2"].font = Font(bold=True)
    ws1.merge_cells("C1:D1")
    ws1["C1"] = "merged header"
    ws1["E2"] = "=1+1"

    ws2 = wb.create_sheet("Hidden")
    ws2.sheet_state = "hidden"
    ws2["A1"] = "unrelated"

    wb.save(source)
    wb.close()

    keep_rule = FieldRule(column_name="Keep", field_type=FieldType.UNKNOWN, action=Action.KEEP)
    anonymized = tmp_path / "anonymized.xlsx"
    provenance = tmp_path / "provenance.enc"
    anonymize_workbook(
        source, anonymized,
        {"First": {1: _company_rule(), 2: keep_rule, 3: keep_rule, 4: keep_rule, 5: keep_rule}},
        mapping_store, None,
        provenance_path=provenance, provenance_password=PASSWORD,
    )

    destination = tmp_path / "restored.xlsx"
    restore_workbook(
        anonymized, destination, mapping_store, identifier_store,
        provenance_path=provenance, provenance_password=PASSWORD,
    )

    output = openpyxl.load_workbook(destination)
    assert output.sheetnames == ["First", "Hidden"]
    assert output["Hidden"].sheet_state == "hidden"
    assert output["First"]["B2"].font.bold is True
    assert output["First"]["C1"].value == "merged header"
    assert ("C1:D1") in [str(r) for r in output["First"].merged_cells.ranges]
    assert output["First"]["E2"].value == "=1+1"
    assert output["First"]["A2"].value == COMPANY_NAME


# ---------------------------------------------------------------------------
# G. Security
# ---------------------------------------------------------------------------


def test_no_confidential_values_in_error_messages(
    tmp_path: Path, mapping_store: InMemoryMappingStore, identifier_store: InMemoryIdentifierMappingStore
) -> None:
    source = tmp_path / "plain_source.xlsx"
    _source_workbook(source, "Data", ["Company", "INN"], [[COMPANY_NAME, VALID_INN]])
    anonymized = tmp_path / "anonymized.xlsx"
    provenance = tmp_path / "provenance.enc"
    anonymize_workbook(
        source, anonymized, {"Data": {1: _company_rule(), 2: _inn_rule()}}, mapping_store, identifier_store,
        provenance_path=provenance, provenance_password=PASSWORD,
    )

    # Ломаем identifier-ячейку -> token mismatch (UnresolvedRestoreError).
    wb = openpyxl.load_workbook(anonymized)
    wb["Data"]["B2"] = "TAMPERED"
    edited = tmp_path / "edited.xlsx"
    wb.save(edited)
    wb.close()

    destination = tmp_path / "restored.xlsx"
    with pytest.raises(UnresolvedRestoreError) as exc_info:
        restore_workbook(
            edited, destination, mapping_store, identifier_store,
            provenance_path=provenance, provenance_password=PASSWORD,
        )

    message = str(exc_info.value)
    assert COMPANY_NAME not in message
    assert VALID_INN not in message
    assert PASSWORD not in message

    # Дополнительно проверяем preflight/binding-сообщения.
    with pytest.raises(RestoreValidationError) as exc_info_2:
        restore_workbook(
            anonymized, destination, mapping_store, None, provenance_path=provenance, provenance_password=PASSWORD
        )
    message_2 = str(exc_info_2.value)
    assert COMPANY_NAME not in message_2
    assert VALID_INN not in message_2
    assert PASSWORD not in message_2


def test_integer_representation_conversion_failure_does_not_leak_identifier_value(
    tmp_path: Path, mapping_store: InMemoryMappingStore, identifier_store: InMemoryIdentifierMappingStore
) -> None:
    """
    Целевой regression test именно для ветки _restore_representation с
    IdentifierRepresentation.INTEGER: token exact-matched, entry реально
    получен из IdentifierMappingStore (confidential identifier_value уже
    в scope), но int(identifier_value) невозможен. Проверяет, что ни
    sentinel-значение, ни token, ни пароль не попадают в сообщение
    исключения.
    """
    source = tmp_path / "plain_source.xlsx"
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Data"
    ws["A1"] = "INN"
    ws["A2"] = int(VALID_INN)  # реальный int -> IdentifierRepresentation.INTEGER
    wb.save(source)
    wb.close()

    anonymized = tmp_path / "anonymized.xlsx"
    provenance = tmp_path / "provenance.enc"
    anonymize_workbook(
        source, anonymized, {"Data": {1: _inn_rule()}}, mapping_store, identifier_store,
        provenance_path=provenance, provenance_password=PASSWORD,
    )

    real_entry = identifier_store.entries()[0]
    token = real_entry.token

    sentinel = "CONFIDENTIAL_IDENTIFIER_SENTINEL_NOT_A_NUMBER"
    corrupted_store = InMemoryIdentifierMappingStore()
    corrupted_store.add(
        IdentifierMappingEntry(token=token, identifier_value=sentinel, identifier_type=IdentifierType.INN)
    )

    destination = tmp_path / "restored.xlsx"
    with pytest.raises(UnresolvedRestoreError) as exc_info:
        restore_workbook(
            anonymized, destination, mapping_store, corrupted_store,
            provenance_path=provenance, provenance_password=PASSWORD,
        )

    message = str(exc_info.value)
    assert sentinel not in message
    assert token not in message
    assert PASSWORD not in message
    assert not destination.exists()


# ---------------------------------------------------------------------------
# H. Backward compatibility
# ---------------------------------------------------------------------------


def test_stage_8_3_artifact_restores_without_reanonymization(
    tmp_path: Path,
    mapping_store: InMemoryMappingStore,
    identifier_store: InMemoryIdentifierMappingStore,
    anonymized_fixture: tuple[Path, Path, str],
) -> None:
    """anonymized_fixture целиком построен через уже существующий,
    неизменённый app.orchestration.single_file.anonymize_workbook (Stage
    8.3) — этот тест лишь явно называет проверяемый compatibility-факт."""
    anonymized, provenance, job_id = anonymized_fixture
    destination = tmp_path / "restored.xlsx"

    result = restore_workbook(
        anonymized, destination, mapping_store, identifier_store,
        provenance_path=provenance, provenance_password=PASSWORD,
    )

    assert result.source_job_id == job_id


def test_stage_8b_batch_item_restores_independently(
    tmp_path: Path, mapping_store: InMemoryMappingStore, identifier_store: InMemoryIdentifierMappingStore
) -> None:
    source_a = tmp_path / "source_a.xlsx"
    _source_workbook(source_a, "Data", ["Company"], [[COMPANY_NAME]])
    source_b = tmp_path / "source_b.xlsx"
    _source_workbook(source_b, "Data", ["Company"], [["ООО Лютик"]])

    item_a = AnonymizationBatchItem(
        source_path=source_a,
        destination_path=tmp_path / "anonymized_a.xlsx",
        provenance_path=tmp_path / "provenance_a.enc",
        rules_by_sheet={"Data": {1: _company_rule()}},
    )
    item_b = AnonymizationBatchItem(
        source_path=source_b,
        destination_path=tmp_path / "anonymized_b.xlsx",
        provenance_path=tmp_path / "provenance_b.enc",
        rules_by_sheet={"Data": {1: _company_rule()}},
    )

    batch_result = anonymize_workbooks(
        [item_a, item_b], mapping_store, None, provenance_password=PASSWORD
    )

    # Восстанавливаем только item_a, независимо от item_b.
    destination_a = tmp_path / "restored_a.xlsx"
    result = restore_workbook(
        item_a.destination_path, destination_a, mapping_store, identifier_store,
        provenance_path=item_a.provenance_path, provenance_password=PASSWORD,
    )

    assert result.source_job_id == batch_result.results[0].job_id
    output = openpyxl.load_workbook(destination_a)
    assert output["Data"]["A2"].value == COMPANY_NAME
