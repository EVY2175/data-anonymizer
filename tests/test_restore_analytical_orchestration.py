"""
Тесты Derived Analytical Workbook Restore orchestration (Stage 9C) —
app.orchestration.restore_analytical.restore_analytical_workbook.

Интеграционные тесты полного pipeline: workbook строится напрямую через
openpyxl (Stage 9C работает с derived analytical workbook, а не с
выходом anonymize_workbook — provenance/job_id здесь не участвуют
вообще). Все данные synthetic; ИНН — валидный по чек-сумме, но
синтетический.
"""

from __future__ import annotations

from pathlib import Path

import openpyxl
import pytest
from openpyxl.packaging.custom import StringProperty

from app.mapping.identifier_memory import InMemoryIdentifierMappingStore
from app.mapping.memory import InMemoryMappingStore
from app.models.entities import EntityType, MappingEntry
from app.models.identifiers import IdentifierMappingEntry, IdentifierType
from app.orchestration.restore_analytical import AnalyticalRestoreResult, restore_analytical_workbook
from app.restore.analytical import AmbiguousRestoreError
from app.restore.core import RestoreValidationError

VALID_INN = "7707083893"
COMPANY_NAME = "ООО Ромашка"
MARKER_PROPERTY = "DataAnonymizer.AnalyticallyRestored"
JOB_ID_PROPERTY = "DataAnonymizer.JobId"
RESTORED_FROM_JOB_ID_PROPERTY = "DataAnonymizer.RestoredFromJobId"


# ---------------------------------------------------------------------------
# Фикстуры/помощники
# ---------------------------------------------------------------------------


def _analytical_workbook(path: Path, sheet_data: dict[str, list[list[object]]]) -> None:
    wb = openpyxl.Workbook()
    first = True
    for sheet_name, rows in sheet_data.items():
        ws = wb.active if first else wb.create_sheet(sheet_name)
        ws.title = sheet_name
        first = False
        for row_idx, row in enumerate(rows, start=1):
            for col_idx, value in enumerate(row, start=1):
                ws.cell(row=row_idx, column=col_idx, value=value)
    wb.save(path)
    wb.close()


@pytest.fixture()
def mapping_store() -> InMemoryMappingStore:
    return InMemoryMappingStore()


@pytest.fixture()
def identifier_store() -> InMemoryIdentifierMappingStore:
    return InMemoryIdentifierMappingStore()


def _add_alias(store: InMemoryMappingStore, alias: str, real_value: str) -> None:
    store.add(MappingEntry(alias=alias, real_value=real_value, entity_type=EntityType.COMPANY))


def _add_token(store: InMemoryIdentifierMappingStore, token: str, value: str, id_type: IdentifierType = IdentifierType.INN) -> None:
    store.add(IdentifierMappingEntry(token=token, identifier_value=value, identifier_type=id_type))


# ---------------------------------------------------------------------------
# Preflight / validation
# ---------------------------------------------------------------------------


def test_source_path_wrong_type_raises(mapping_store: InMemoryMappingStore) -> None:
    with pytest.raises(TypeError):
        restore_analytical_workbook(123, "dest.xlsx", mapping_store, None)


def test_destination_path_wrong_type_raises(mapping_store: InMemoryMappingStore) -> None:
    with pytest.raises(TypeError):
        restore_analytical_workbook("source.xlsx", 123, mapping_store, None)


def test_source_suffix_invalid_raises(tmp_path: Path, mapping_store: InMemoryMappingStore) -> None:
    bad_source = tmp_path / "source.txt"
    bad_source.write_text("not excel", encoding="utf-8")
    destination = tmp_path / "restored.xlsx"

    with pytest.raises(RestoreValidationError):
        restore_analytical_workbook(bad_source, destination, mapping_store, None)


def test_destination_suffix_invalid_raises(tmp_path: Path, mapping_store: InMemoryMappingStore) -> None:
    source = tmp_path / "source.xlsx"
    _analytical_workbook(source, {"Data": [["A"]]})
    destination = tmp_path / "restored.txt"

    with pytest.raises(RestoreValidationError):
        restore_analytical_workbook(source, destination, mapping_store, None)


def test_source_missing_raises(tmp_path: Path, mapping_store: InMemoryMappingStore) -> None:
    missing_source = tmp_path / "nope.xlsx"
    destination = tmp_path / "restored.xlsx"

    with pytest.raises(RestoreValidationError):
        restore_analytical_workbook(missing_source, destination, mapping_store, None)


def test_source_is_directory_raises(tmp_path: Path, mapping_store: InMemoryMappingStore) -> None:
    directory_as_source = tmp_path / "source_dir.xlsx"
    directory_as_source.mkdir()
    destination = tmp_path / "restored.xlsx"

    with pytest.raises(RestoreValidationError):
        restore_analytical_workbook(directory_as_source, destination, mapping_store, None)


def test_destination_parent_missing_raises(tmp_path: Path, mapping_store: InMemoryMappingStore) -> None:
    source = tmp_path / "source.xlsx"
    _analytical_workbook(source, {"Data": [["A"]]})
    destination = tmp_path / "missing_dir" / "restored.xlsx"

    with pytest.raises(RestoreValidationError):
        restore_analytical_workbook(source, destination, mapping_store, None)


def test_destination_parent_not_directory_raises(tmp_path: Path, mapping_store: InMemoryMappingStore) -> None:
    source = tmp_path / "source.xlsx"
    _analytical_workbook(source, {"Data": [["A"]]})
    parent_as_file = tmp_path / "parent_is_file"
    parent_as_file.write_text("i am a file, not a directory", encoding="utf-8")
    destination = parent_as_file / "restored.xlsx"

    with pytest.raises(RestoreValidationError):
        restore_analytical_workbook(source, destination, mapping_store, None)


def test_destination_already_exists_raises(tmp_path: Path, mapping_store: InMemoryMappingStore) -> None:
    source = tmp_path / "source.xlsx"
    _analytical_workbook(source, {"Data": [["A"]]})
    destination = tmp_path / "restored.xlsx"
    destination.write_bytes(b"already here")

    with pytest.raises(RestoreValidationError):
        restore_analytical_workbook(source, destination, mapping_store, None)
    assert destination.read_bytes() == b"already here"


def test_source_equals_destination_raises(tmp_path: Path, mapping_store: InMemoryMappingStore) -> None:
    source = tmp_path / "source.xlsx"
    _analytical_workbook(source, {"Data": [["A"]]})

    with pytest.raises(RestoreValidationError):
        restore_analytical_workbook(source, source, mapping_store, None)


def test_only_xlsx_supported(tmp_path: Path, mapping_store: InMemoryMappingStore) -> None:
    source = tmp_path / "source.xlsm"
    source.write_bytes(b"not really xlsm content")
    destination = tmp_path / "restored.xlsx"

    with pytest.raises(RestoreValidationError):
        restore_analytical_workbook(source, destination, mapping_store, None)


# ---------------------------------------------------------------------------
# Full restore scenarios
# ---------------------------------------------------------------------------


def test_full_entity_restore(tmp_path: Path, mapping_store: InMemoryMappingStore) -> None:
    _add_alias(mapping_store, "C_ALIAS1", COMPANY_NAME)
    source = tmp_path / "analytical.xlsx"
    _analytical_workbook(source, {"Summary": [["Client"], ["C_ALIAS1"]]})
    destination = tmp_path / "restored.xlsx"

    result = restore_analytical_workbook(source, destination, mapping_store, None)

    assert result == AnalyticalRestoreResult(
        output_path=destination, restored_entity_cells=1, restored_identifier_cells=0
    )
    output = openpyxl.load_workbook(destination)
    assert output["Summary"]["A2"].value == COMPANY_NAME


def test_full_identifier_restore(
    tmp_path: Path, mapping_store: InMemoryMappingStore, identifier_store: InMemoryIdentifierMappingStore
) -> None:
    _add_token(identifier_store, "INN_TOKEN1", VALID_INN)
    source = tmp_path / "analytical.xlsx"
    _analytical_workbook(source, {"Summary": [["INN"], ["INN_TOKEN1"]]})
    destination = tmp_path / "restored.xlsx"

    result = restore_analytical_workbook(source, destination, mapping_store, identifier_store)

    assert result == AnalyticalRestoreResult(
        output_path=destination, restored_entity_cells=0, restored_identifier_cells=1
    )
    output = openpyxl.load_workbook(destination)
    assert output["Summary"]["A2"].value == VALID_INN


def test_mixed_restore(
    tmp_path: Path, mapping_store: InMemoryMappingStore, identifier_store: InMemoryIdentifierMappingStore
) -> None:
    _add_alias(mapping_store, "C_ALIAS1", COMPANY_NAME)
    _add_token(identifier_store, "INN_TOKEN1", VALID_INN)
    source = tmp_path / "analytical.xlsx"
    _analytical_workbook(source, {"Summary": [["Client", "INN"], ["C_ALIAS1", "INN_TOKEN1"]]})
    destination = tmp_path / "restored.xlsx"

    result = restore_analytical_workbook(source, destination, mapping_store, identifier_store)

    assert result.restored_entity_cells == 1
    assert result.restored_identifier_cells == 1
    output = openpyxl.load_workbook(destination)
    assert output["Summary"]["A2"].value == COMPANY_NAME
    assert output["Summary"]["B2"].value == VALID_INN


def test_identifier_store_none_entity_only_mode(tmp_path: Path, mapping_store: InMemoryMappingStore) -> None:
    _add_alias(mapping_store, "C_ALIAS1", COMPANY_NAME)
    source = tmp_path / "analytical.xlsx"
    _analytical_workbook(source, {"Summary": [["Client", "INN"], ["C_ALIAS1", "INN_TOKEN1"]]})
    destination = tmp_path / "restored.xlsx"

    result = restore_analytical_workbook(source, destination, mapping_store, None)

    assert result.restored_entity_cells == 1
    assert result.restored_identifier_cells == 0
    output = openpyxl.load_workbook(destination)
    assert output["Summary"]["A2"].value == COMPANY_NAME
    assert output["Summary"]["B2"].value == "INN_TOKEN1"  # unchanged, identifier_store=None


def test_zero_zero_success(tmp_path: Path, mapping_store: InMemoryMappingStore) -> None:
    source = tmp_path / "analytical.xlsx"
    _analytical_workbook(source, {"Summary": [["KPI"], ["Рост выручки 20%"]]})
    destination = tmp_path / "restored.xlsx"

    result = restore_analytical_workbook(source, destination, mapping_store, None)

    assert result == AnalyticalRestoreResult(
        output_path=destination, restored_entity_cells=0, restored_identifier_cells=0
    )
    assert destination.exists()


def test_multiple_worksheets(
    tmp_path: Path, mapping_store: InMemoryMappingStore, identifier_store: InMemoryIdentifierMappingStore
) -> None:
    _add_alias(mapping_store, "C_ALIAS1", COMPANY_NAME)
    _add_token(identifier_store, "INN_TOKEN1", VALID_INN)
    source = tmp_path / "analytical.xlsx"
    _analytical_workbook(
        source, {"First": [["Client"], ["C_ALIAS1"]], "Second": [["INN"], ["INN_TOKEN1"]]}
    )
    destination = tmp_path / "restored.xlsx"

    result = restore_analytical_workbook(source, destination, mapping_store, identifier_store)

    assert result.restored_entity_cells == 1
    assert result.restored_identifier_cells == 1


def test_hidden_and_veryhidden_worksheets_restored(tmp_path: Path, mapping_store: InMemoryMappingStore) -> None:
    _add_alias(mapping_store, "C_ALIAS1", COMPANY_NAME)
    _add_alias(mapping_store, "C_ALIAS2", "ООО Лютик")
    source = tmp_path / "analytical.xlsx"
    wb = openpyxl.Workbook()
    ws1 = wb.active
    ws1.title = "Visible"
    ws1["A1"] = "A"
    ws2 = wb.create_sheet("Hidden")
    ws2.sheet_state = "hidden"
    ws2["A1"] = "C_ALIAS1"
    ws3 = wb.create_sheet("VeryHidden")
    ws3.sheet_state = "veryHidden"
    ws3["A1"] = "C_ALIAS2"
    wb.save(source)
    wb.close()

    destination = tmp_path / "restored.xlsx"
    result = restore_analytical_workbook(source, destination, mapping_store, None)

    assert result.restored_entity_cells == 2
    output = openpyxl.load_workbook(destination)
    assert output["Hidden"]["A1"].value == COMPANY_NAME
    assert output["VeryHidden"]["A1"].value == "ООО Лютик"


# ---------------------------------------------------------------------------
# Ambiguity / all-or-nothing
# ---------------------------------------------------------------------------


def test_ambiguous_value_no_destination(
    tmp_path: Path, mapping_store: InMemoryMappingStore, identifier_store: InMemoryIdentifierMappingStore
) -> None:
    _add_alias(mapping_store, "COLLIDE", "ООО Тест")
    _add_token(identifier_store, "COLLIDE", VALID_INN)
    source = tmp_path / "analytical.xlsx"
    _analytical_workbook(source, {"Data": [["Value"], ["COLLIDE"]]})
    destination = tmp_path / "restored.xlsx"

    with pytest.raises(AmbiguousRestoreError):
        restore_analytical_workbook(source, destination, mapping_store, identifier_store)

    assert not destination.exists()


# ---------------------------------------------------------------------------
# Source immutability
# ---------------------------------------------------------------------------


def test_source_bytes_unchanged_on_success(tmp_path: Path, mapping_store: InMemoryMappingStore) -> None:
    _add_alias(mapping_store, "C_ALIAS1", COMPANY_NAME)
    source = tmp_path / "analytical.xlsx"
    _analytical_workbook(source, {"Data": [["Client"], ["C_ALIAS1"]]})
    bytes_before = source.read_bytes()
    destination = tmp_path / "restored.xlsx"

    restore_analytical_workbook(source, destination, mapping_store, None)

    assert source.read_bytes() == bytes_before


def test_source_bytes_unchanged_on_ambiguity_failure(
    tmp_path: Path, mapping_store: InMemoryMappingStore, identifier_store: InMemoryIdentifierMappingStore
) -> None:
    _add_alias(mapping_store, "COLLIDE", "ООО Тест")
    _add_token(identifier_store, "COLLIDE", VALID_INN)
    source = tmp_path / "analytical.xlsx"
    _analytical_workbook(source, {"Data": [["Value"], ["COLLIDE"]]})
    bytes_before = source.read_bytes()
    destination = tmp_path / "restored.xlsx"

    with pytest.raises(AmbiguousRestoreError):
        restore_analytical_workbook(source, destination, mapping_store, identifier_store)

    assert source.read_bytes() == bytes_before


def test_source_bytes_unchanged_on_writer_failure(
    tmp_path: Path, mapping_store: InMemoryMappingStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    _add_alias(mapping_store, "C_ALIAS1", COMPANY_NAME)
    source = tmp_path / "analytical.xlsx"
    _analytical_workbook(source, {"Data": [["Client"], ["C_ALIAS1"]]})
    bytes_before = source.read_bytes()
    destination = tmp_path / "restored.xlsx"

    def failing_save(self, *args, **kwargs):
        raise OSError("simulated save failure")

    monkeypatch.setattr(openpyxl.Workbook, "save", failing_save)

    with pytest.raises(OSError):
        restore_analytical_workbook(source, destination, mapping_store, None)

    monkeypatch.undo()
    assert source.read_bytes() == bytes_before
    assert not destination.exists()


# ---------------------------------------------------------------------------
# Store immutability
# ---------------------------------------------------------------------------


def test_stores_unchanged_after_restore(
    tmp_path: Path, mapping_store: InMemoryMappingStore, identifier_store: InMemoryIdentifierMappingStore
) -> None:
    _add_alias(mapping_store, "C_ALIAS1", COMPANY_NAME)
    _add_token(identifier_store, "INN_TOKEN1", VALID_INN)
    source = tmp_path / "analytical.xlsx"
    _analytical_workbook(source, {"Data": [["Client", "INN"], ["C_ALIAS1", "INN_TOKEN1"]]})
    destination = tmp_path / "restored.xlsx"

    mapping_before = mapping_store.entries()
    identifier_before = identifier_store.entries()

    restore_analytical_workbook(source, destination, mapping_store, identifier_store)

    assert mapping_store.entries() == mapping_before
    assert identifier_store.entries() == identifier_before


# ---------------------------------------------------------------------------
# workbook.close()
# ---------------------------------------------------------------------------


def _spy_load_workbook(monkeypatch: pytest.MonkeyPatch) -> dict[str, int]:
    close_calls = {"n": 0}
    real_load_workbook = openpyxl.load_workbook

    def spying_load_workbook(*args, **kwargs):
        loaded = real_load_workbook(*args, **kwargs)
        real_close = loaded.close

        def counting_close():
            close_calls["n"] += 1
            real_close()

        loaded.close = counting_close
        return loaded

    monkeypatch.setattr(openpyxl, "load_workbook", spying_load_workbook)
    return close_calls


def test_workbook_closed_on_success(tmp_path: Path, mapping_store: InMemoryMappingStore, monkeypatch: pytest.MonkeyPatch) -> None:
    _add_alias(mapping_store, "C_ALIAS1", COMPANY_NAME)
    source = tmp_path / "analytical.xlsx"
    _analytical_workbook(source, {"Data": [["Client"], ["C_ALIAS1"]]})
    destination = tmp_path / "restored.xlsx"

    close_calls = _spy_load_workbook(monkeypatch)

    restore_analytical_workbook(source, destination, mapping_store, None)

    monkeypatch.undo()
    assert close_calls["n"] == 1


def test_workbook_closed_on_ambiguity_failure(
    tmp_path: Path, mapping_store: InMemoryMappingStore, identifier_store: InMemoryIdentifierMappingStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    _add_alias(mapping_store, "COLLIDE", "ООО Тест")
    _add_token(identifier_store, "COLLIDE", VALID_INN)
    source = tmp_path / "analytical.xlsx"
    _analytical_workbook(source, {"Data": [["Value"], ["COLLIDE"]]})
    destination = tmp_path / "restored.xlsx"

    close_calls = _spy_load_workbook(monkeypatch)

    with pytest.raises(AmbiguousRestoreError):
        restore_analytical_workbook(source, destination, mapping_store, identifier_store)

    monkeypatch.undo()
    assert close_calls["n"] == 1


def test_workbook_closed_on_writer_failure(
    tmp_path: Path, mapping_store: InMemoryMappingStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    _add_alias(mapping_store, "C_ALIAS1", COMPANY_NAME)
    source = tmp_path / "analytical.xlsx"
    _analytical_workbook(source, {"Data": [["Client"], ["C_ALIAS1"]]})
    destination = tmp_path / "restored.xlsx"

    close_calls = _spy_load_workbook(monkeypatch)

    def failing_save(self, *args, **kwargs):
        raise OSError("simulated save failure")

    monkeypatch.setattr(openpyxl.Workbook, "save", failing_save)

    with pytest.raises(OSError):
        restore_analytical_workbook(source, destination, mapping_store, None)

    monkeypatch.undo()
    assert close_calls["n"] == 1


# ---------------------------------------------------------------------------
# Metadata marker in final artifact
# ---------------------------------------------------------------------------


def test_marker_present_in_final_artifact(tmp_path: Path, mapping_store: InMemoryMappingStore) -> None:
    source = tmp_path / "analytical.xlsx"
    _analytical_workbook(source, {"Data": [["A"]]})
    destination = tmp_path / "restored.xlsx"

    restore_analytical_workbook(source, destination, mapping_store, None)

    output = openpyxl.load_workbook(destination)
    assert output.custom_doc_props[MARKER_PROPERTY].value == "true"


# ---------------------------------------------------------------------------
# Already-restored guard
# ---------------------------------------------------------------------------


def _add_marker(path: Path, value: object = "true", duplicate: bool = False) -> None:
    wb = openpyxl.load_workbook(path)
    wb.custom_doc_props.append(StringProperty(name=MARKER_PROPERTY, value=value))
    if duplicate:
        wb.custom_doc_props.props.append(StringProperty(name=MARKER_PROPERTY, value=value))
    wb.save(path)
    wb.close()


def test_existing_marker_true_hard_fails(tmp_path: Path, mapping_store: InMemoryMappingStore) -> None:
    source = tmp_path / "analytical.xlsx"
    _analytical_workbook(source, {"Data": [["A"]]})
    _add_marker(source, "true")
    destination = tmp_path / "restored.xlsx"

    with pytest.raises(RestoreValidationError):
        restore_analytical_workbook(source, destination, mapping_store, None)
    assert not destination.exists()


def test_existing_marker_other_value_still_hard_fails(tmp_path: Path, mapping_store: InMemoryMappingStore) -> None:
    source = tmp_path / "analytical.xlsx"
    _analytical_workbook(source, {"Data": [["A"]]})
    _add_marker(source, "false")
    destination = tmp_path / "restored.xlsx"

    with pytest.raises(RestoreValidationError):
        restore_analytical_workbook(source, destination, mapping_store, None)
    assert not destination.exists()


def test_existing_marker_duplicate_still_hard_fails(tmp_path: Path, mapping_store: InMemoryMappingStore) -> None:
    source = tmp_path / "analytical.xlsx"
    _analytical_workbook(source, {"Data": [["A"]]})
    _add_marker(source, "true", duplicate=True)
    destination = tmp_path / "restored.xlsx"

    with pytest.raises(RestoreValidationError):
        restore_analytical_workbook(source, destination, mapping_store, None)
    assert not destination.exists()


# ---------------------------------------------------------------------------
# JobId / RestoredFromJobId preservation через полный orchestration flow
# ---------------------------------------------------------------------------


def test_existing_job_id_preserved_through_orchestration(tmp_path: Path, mapping_store: InMemoryMappingStore) -> None:
    _add_alias(mapping_store, "C_ALIAS1", COMPANY_NAME)
    source = tmp_path / "analytical.xlsx"
    _analytical_workbook(source, {"Data": [["Client"], ["C_ALIAS1"]]})
    wb = openpyxl.load_workbook(source)
    wb.custom_doc_props.append(StringProperty(name=JOB_ID_PROPERTY, value="unrelated-job-id"))
    wb.save(source)
    wb.close()
    destination = tmp_path / "restored.xlsx"

    restore_analytical_workbook(source, destination, mapping_store, None)

    output = openpyxl.load_workbook(destination)
    assert output.custom_doc_props[JOB_ID_PROPERTY].value == "unrelated-job-id"
    assert output.custom_doc_props[MARKER_PROPERTY].value == "true"


def test_existing_restored_from_job_id_preserved_through_orchestration(tmp_path: Path, mapping_store: InMemoryMappingStore) -> None:
    _add_alias(mapping_store, "C_ALIAS1", COMPANY_NAME)
    source = tmp_path / "analytical.xlsx"
    _analytical_workbook(source, {"Data": [["Client"], ["C_ALIAS1"]]})
    wb = openpyxl.load_workbook(source)
    wb.custom_doc_props.append(StringProperty(name=RESTORED_FROM_JOB_ID_PROPERTY, value="original-job-id"))
    wb.save(source)
    wb.close()
    destination = tmp_path / "restored.xlsx"

    restore_analytical_workbook(source, destination, mapping_store, None)

    output = openpyxl.load_workbook(destination)
    assert output.custom_doc_props[RESTORED_FROM_JOB_ID_PROPERTY].value == "original-job-id"
    assert output.custom_doc_props[MARKER_PROPERTY].value == "true"


# ---------------------------------------------------------------------------
# Error confidentiality
# ---------------------------------------------------------------------------


def test_error_confidentiality_no_sheet_name_no_values(
    tmp_path: Path, mapping_store: InMemoryMappingStore, identifier_store: InMemoryIdentifierMappingStore
) -> None:
    _add_alias(mapping_store, "COLLIDE", "ООО СекретноеИмя")
    _add_token(identifier_store, "COLLIDE", VALID_INN)
    source = tmp_path / "analytical.xlsx"
    _analytical_workbook(source, {"КлиентСекретноеИмя": [["Value"], ["COLLIDE"]]})
    destination = tmp_path / "restored.xlsx"

    with pytest.raises(AmbiguousRestoreError) as exc_info:
        restore_analytical_workbook(source, destination, mapping_store, identifier_store)

    message = str(exc_info.value)
    assert "КлиентСекретноеИмя" not in message
    assert "COLLIDE" not in message
    assert "ООО СекретноеИмя" not in message
    assert VALID_INN not in message


# ---------------------------------------------------------------------------
# Retry after failure
# ---------------------------------------------------------------------------


def test_retry_after_ambiguity_failure_with_fixed_store(
    tmp_path: Path, mapping_store: InMemoryMappingStore, identifier_store: InMemoryIdentifierMappingStore
) -> None:
    _add_alias(mapping_store, "COLLIDE", "ООО Тест")
    _add_token(identifier_store, "COLLIDE", VALID_INN)
    source = tmp_path / "analytical.xlsx"
    _analytical_workbook(source, {"Data": [["Value"], ["COLLIDE"]]})
    destination = tmp_path / "restored.xlsx"

    with pytest.raises(AmbiguousRestoreError):
        restore_analytical_workbook(source, destination, mapping_store, identifier_store)
    assert not destination.exists()

    # Caller исправляет причину: повторный вызов без identifier_store
    # (например, узнав, что для этого файла identifier restore не нужен).
    result = restore_analytical_workbook(source, destination, mapping_store, None)

    assert destination.exists()
    assert result.restored_entity_cells == 1


# ---------------------------------------------------------------------------
# output_path: Path(destination_path) без resolve()
# ---------------------------------------------------------------------------


def test_relative_str_destination_output_path_is_not_resolved(
    tmp_path: Path, mapping_store: InMemoryMappingStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    _add_alias(mapping_store, "C_ALIAS1", COMPANY_NAME)
    source = tmp_path / "analytical.xlsx"
    _analytical_workbook(source, {"Data": [["Client"], ["C_ALIAS1"]]})
    monkeypatch.chdir(tmp_path)

    result = restore_analytical_workbook(str(source), "rel.xlsx", mapping_store, None)

    # Относительный str destination: output_path обязан остаться относительным,
    # т.е. ровно Path(destination_path), а не Path(destination_path).resolve().
    assert result.output_path == Path("rel.xlsx")
    assert not result.output_path.is_absolute()
    assert (tmp_path / "rel.xlsx").exists()
    assert Path("rel.xlsx").exists()
    assert openpyxl.load_workbook(tmp_path / "rel.xlsx")["Data"]["A2"].value == COMPANY_NAME
