"""
Тесты Multi-file Batch Orchestration (Stage 8B) —
app.orchestration.batch.anonymize_workbooks.

Все workbook — synthetic (никаких реальных компаний/ФИО); ИНН — валидные
по чек-сумме, но синтетические (те же значения, что уже используются в
tests/test_identifier_anonymizer.py / tests/test_single_file_orchestration.py).

mapping_store/identifier_store переиспользуются между items batch и между
последовательными вызовами anonymize_workbooks (retry-тест) — это то, что
доказывает главный бизнес-инвариант Stage 8B: pseudonym/token stability
зависит от persistent store, а не от job_id/batch-вызова.
"""

from __future__ import annotations

import ast
import dataclasses
from pathlib import Path

import openpyxl
import pytest

import app.orchestration.batch as batch_module
import app.orchestration.single_file as single_file_module
from app.anonymizer import (
    InvalidPseudonymizationValueError,
    InvalidRuleConfigurationError,
    MissingIdentifierStoreError,
)
from app.excel.models import CellRecord, FlatTable
from app.excel.writer import WriterValidationError
from app.excel.writer import write_anonymized_workbook as real_write_anonymized_workbook
from app.mapping.identifier_memory import InMemoryIdentifierMappingStore
from app.mapping.memory import InMemoryMappingStore
from app.mapping.provenance_encrypted import EncryptedFileProvenanceStore
from app.models.rules import Action, FieldRule, FieldType
from app.orchestration.batch import (
    AnonymizationBatchItem,
    AnonymizationBatchResult,
    BatchValidationError,
    anonymize_workbooks,
)

VALID_INN = "7707083893"
VALID_INN_2 = "7707083886"
PASSWORD = "test-batch-password"


# ---------------------------------------------------------------------------
# Фикстуры/помощники
# ---------------------------------------------------------------------------


def _source_workbook(
    path: Path, sheet_name: str, headers: list[object], rows: list[list[object]]
) -> None:
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


def _valid_item(
    tmp_path: Path,
    suffix: str,
    *,
    company: str = "ООО Ромашка",
) -> AnonymizationBatchItem:
    source = tmp_path / f"source_{suffix}.xlsx"
    _source_workbook(source, "Data", ["Company"], [[company]])
    return AnonymizationBatchItem(
        source_path=source,
        destination_path=tmp_path / f"output_{suffix}.xlsx",
        provenance_path=tmp_path / f"provenance_{suffix}.enc",
        rules_by_sheet={"Data": {1: _company_rule()}},
    )


def _valid_inn_item(tmp_path: Path, suffix: str, inn: str) -> AnonymizationBatchItem:
    source = tmp_path / f"source_{suffix}.xlsx"
    _source_workbook(source, "Data", ["INN"], [[inn]])
    return AnonymizationBatchItem(
        source_path=source,
        destination_path=tmp_path / f"output_{suffix}.xlsx",
        provenance_path=tmp_path / f"provenance_{suffix}.enc",
        rules_by_sheet={"Data": {1: _inn_rule()}},
    )


def _shared_path_for(tmp_path: Path, role_a: str, role_b: str) -> Path:
    """
    Строит один общий путь, физическое состояние (существование) и
    расширение которого одновременно удовлетворяют per-item preflight-
    требованиям ОБЕИХ ролей role_a/role_b — так, чтобы тест реально
    проверял cross-item collision matrix, а не случайно упал раньше на
    независимой per-item причине (например, "source не существует").
    """
    roles = {role_a, role_b}
    needs_xlsx = "source_path" in roles or "destination_path" in roles
    needs_exists = "source_path" in roles
    suffix = ".xlsx" if needs_xlsx else ".enc"
    path = tmp_path / f"shared{suffix}"
    if needs_exists:
        _source_workbook(path, "Data", ["Company"], [["ООО Ромашка"]])
    return path


def _make_call_counter(monkeypatch: pytest.MonkeyPatch) -> dict[str, int]:
    """Оборачивает anonymize_workbook (Stage 8.3), как его видит batch.py, счётчиком."""
    counts = {"anonymize_workbook": 0}
    real = batch_module.anonymize_workbook

    def counting(*args: object, **kwargs: object) -> object:
        counts["anonymize_workbook"] += 1
        return real(*args, **kwargs)

    monkeypatch.setattr(batch_module, "anonymize_workbook", counting)
    return counts


class _SecretLeakProbe:
    """Объект, чей repr/str содержит маркер — используется, чтобы доказать,
    что error-сообщения никогда не включают repr произвольного значения."""

    def __repr__(self) -> str:
        return "SECRET_LEAK_PROBE_MARKER"

    def __str__(self) -> str:
        return "SECRET_LEAK_PROBE_MARKER"


class _CountingIterable:
    """Iterable, считающий количество вызовов __iter__ — доказывает, что
    anonymize_workbooks материализует items ровно один раз."""

    def __init__(self, values: list[object]) -> None:
        self._values = values
        self.iter_calls = 0

    def __iter__(self):
        self.iter_calls += 1
        return iter(self._values)


class _ProbeError(Exception):
    """Маркерное исключение — доказывает, что сбой генератора во время
    материализации пробрасывается unchanged, а не оборачивается в
    BatchValidationError."""


@pytest.fixture()
def mapping_store() -> InMemoryMappingStore:
    return InMemoryMappingStore()


@pytest.fixture()
def identifier_store() -> InMemoryIdentifierMappingStore:
    return InMemoryIdentifierMappingStore()


# ---------------------------------------------------------------------------
# Empty batch
# ---------------------------------------------------------------------------


def test_empty_batch_raises(mapping_store: InMemoryMappingStore) -> None:
    with pytest.raises(BatchValidationError):
        anonymize_workbooks([], mapping_store, None, provenance_password=PASSWORD)


def test_empty_batch_zero_job_side_effects(
    mapping_store: InMemoryMappingStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    counts = _make_call_counter(monkeypatch)
    with pytest.raises(BatchValidationError):
        anonymize_workbooks([], mapping_store, None, provenance_password=PASSWORD)
    assert counts["anonymize_workbook"] == 0


# ---------------------------------------------------------------------------
# Item / path type validation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("invalid_item", [_SecretLeakProbe(), {"source_path": "x"}, 42, "not an item"])
def test_invalid_item_type_raises_type_error(
    mapping_store: InMemoryMappingStore, monkeypatch: pytest.MonkeyPatch, invalid_item: object
) -> None:
    counts = _make_call_counter(monkeypatch)
    with pytest.raises(TypeError) as exc_info:
        anonymize_workbooks([invalid_item], mapping_store, None, provenance_password=PASSWORD)
    assert "SECRET_LEAK_PROBE_MARKER" not in str(exc_info.value)
    assert counts["anonymize_workbook"] == 0


@pytest.mark.parametrize("role", ["source_path", "destination_path", "provenance_path"])
def test_invalid_path_type_raises_type_error(
    tmp_path: Path, mapping_store: InMemoryMappingStore, monkeypatch: pytest.MonkeyPatch, role: str
) -> None:
    counts = _make_call_counter(monkeypatch)
    item = dataclasses.replace(_valid_item(tmp_path, "a"), **{role: _SecretLeakProbe()})

    with pytest.raises(TypeError) as exc_info:
        anonymize_workbooks([item], mapping_store, None, provenance_password=PASSWORD)

    assert "SECRET_LEAK_PROBE_MARKER" not in str(exc_info.value)
    assert counts["anonymize_workbook"] == 0


# ---------------------------------------------------------------------------
# Extensions
# ---------------------------------------------------------------------------


def test_invalid_source_extension_raises(
    tmp_path: Path, mapping_store: InMemoryMappingStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    counts = _make_call_counter(monkeypatch)
    bad_source = tmp_path / "source_a.txt"
    bad_source.write_text("not excel", encoding="utf-8")
    item = dataclasses.replace(_valid_item(tmp_path, "a"), source_path=bad_source)

    with pytest.raises(BatchValidationError):
        anonymize_workbooks([item], mapping_store, None, provenance_password=PASSWORD)
    assert counts["anonymize_workbook"] == 0


def test_invalid_destination_extension_raises(
    tmp_path: Path, mapping_store: InMemoryMappingStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    counts = _make_call_counter(monkeypatch)
    item = dataclasses.replace(_valid_item(tmp_path, "a"), destination_path=tmp_path / "output_a.txt")

    with pytest.raises(BatchValidationError):
        anonymize_workbooks([item], mapping_store, None, provenance_password=PASSWORD)
    assert counts["anonymize_workbook"] == 0


# ---------------------------------------------------------------------------
# Source existence
# ---------------------------------------------------------------------------


def test_missing_source_raises(
    tmp_path: Path, mapping_store: InMemoryMappingStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    counts = _make_call_counter(monkeypatch)
    item = dataclasses.replace(_valid_item(tmp_path, "a"), source_path=tmp_path / "nope.xlsx")

    with pytest.raises(BatchValidationError):
        anonymize_workbooks([item], mapping_store, None, provenance_password=PASSWORD)
    assert counts["anonymize_workbook"] == 0


def test_source_is_directory_raises(
    tmp_path: Path, mapping_store: InMemoryMappingStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    counts = _make_call_counter(monkeypatch)
    directory_as_source = tmp_path / "source_dir.xlsx"
    directory_as_source.mkdir()
    item = dataclasses.replace(_valid_item(tmp_path, "a"), source_path=directory_as_source)

    with pytest.raises(BatchValidationError):
        anonymize_workbooks([item], mapping_store, None, provenance_password=PASSWORD)
    assert counts["anonymize_workbook"] == 0


# ---------------------------------------------------------------------------
# Parent directories
# ---------------------------------------------------------------------------


def test_missing_destination_parent_raises(
    tmp_path: Path, mapping_store: InMemoryMappingStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    counts = _make_call_counter(monkeypatch)
    item = dataclasses.replace(
        _valid_item(tmp_path, "a"), destination_path=tmp_path / "missing_dir" / "output.xlsx"
    )

    with pytest.raises(BatchValidationError):
        anonymize_workbooks([item], mapping_store, None, provenance_password=PASSWORD)
    assert counts["anonymize_workbook"] == 0


def test_missing_provenance_parent_raises(
    tmp_path: Path, mapping_store: InMemoryMappingStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    counts = _make_call_counter(monkeypatch)
    item = dataclasses.replace(
        _valid_item(tmp_path, "a"), provenance_path=tmp_path / "missing_dir" / "provenance.enc"
    )

    with pytest.raises(BatchValidationError):
        anonymize_workbooks([item], mapping_store, None, provenance_password=PASSWORD)
    assert counts["anonymize_workbook"] == 0


# ---------------------------------------------------------------------------
# Existing provenance / existing destination
# ---------------------------------------------------------------------------


def test_existing_provenance_raises(
    tmp_path: Path, mapping_store: InMemoryMappingStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    counts = _make_call_counter(monkeypatch)
    item = _valid_item(tmp_path, "a")
    item.provenance_path.write_bytes(b"already here")

    with pytest.raises(BatchValidationError):
        anonymize_workbooks([item], mapping_store, None, provenance_password=PASSWORD)
    assert counts["anonymize_workbook"] == 0


def test_existing_provenance_in_later_item_rejected_before_first_job(
    tmp_path: Path, mapping_store: InMemoryMappingStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    item[0] полностью валиден и никак не связан с item[1] (все шесть путей
    item[0]/item[1] попарно различны — это НЕ collision-сценарий). У
    item[1] заранее существует provenance_path (никак не пересекающийся с
    путями item[0]). Batch должен обнаружить это ДО первого
    anonymize_workbook — item[0] не должен успеть запуститься, несмотря
    на то, что сам он абсолютно корректен.
    """
    counts = _make_call_counter(monkeypatch)

    item_a = _valid_item(tmp_path, "a")
    item_b = _valid_item(tmp_path, "b")

    preexisting_marker = b"pre-existing sidecar content, unrelated to this batch"
    item_b.provenance_path.write_bytes(preexisting_marker)

    # Явное подтверждение отсутствия любой collision между item_a и item_b:
    # ни один из шести путей не совпадает ни с одним другим.
    paths_a = {item_a.source_path, item_a.destination_path, item_a.provenance_path}
    paths_b = {item_b.source_path, item_b.destination_path, item_b.provenance_path}
    assert paths_a.isdisjoint(paths_b)

    with pytest.raises(BatchValidationError):
        anonymize_workbooks([item_a, item_b], mapping_store, None, provenance_password=PASSWORD)

    assert counts["anonymize_workbook"] == 0

    assert not item_a.destination_path.exists()
    assert not item_a.provenance_path.exists()

    assert mapping_store.entries() == ()

    # Существующий sidecar item_b не открывался, не расшифровывался, не
    # удалялся и не перезаписывался batch preflight'ом.
    assert item_b.provenance_path.exists()
    assert item_b.provenance_path.read_bytes() == preexisting_marker


def test_existing_destination_allowed_and_replaced(
    tmp_path: Path, mapping_store: InMemoryMappingStore
) -> None:
    item = _valid_item(tmp_path, "a")
    wb = openpyxl.Workbook()
    wb.active["A1"] = "PRE-EXISTING DESTINATION CONTENT"
    wb.save(item.destination_path)

    result = anonymize_workbooks([item], mapping_store, None, provenance_password=PASSWORD)

    output = openpyxl.load_workbook(item.destination_path)
    assert "Data" in output.sheetnames
    assert "Sheet" not in output.sheetnames
    assert isinstance(result, AnonymizationBatchResult)


# ---------------------------------------------------------------------------
# Password
# ---------------------------------------------------------------------------


def test_empty_password_raises(
    tmp_path: Path, mapping_store: InMemoryMappingStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    counts = _make_call_counter(monkeypatch)
    item = _valid_item(tmp_path, "a")

    with pytest.raises(BatchValidationError):
        anonymize_workbooks([item], mapping_store, None, provenance_password="")
    assert counts["anonymize_workbook"] == 0


def test_non_str_password_raises_type_error(
    tmp_path: Path, mapping_store: InMemoryMappingStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    counts = _make_call_counter(monkeypatch)
    item = _valid_item(tmp_path, "a")

    with pytest.raises(TypeError) as exc_info:
        anonymize_workbooks([item], mapping_store, None, provenance_password=_SecretLeakProbe())  # type: ignore[arg-type]

    assert "SECRET_LEAK_PROBE_MARKER" not in str(exc_info.value)
    assert counts["anonymize_workbook"] == 0


def test_whitespace_only_password_accepted(
    tmp_path: Path, mapping_store: InMemoryMappingStore
) -> None:
    item = _valid_item(tmp_path, "a")
    result = anonymize_workbooks([item], mapping_store, None, provenance_password="   ")
    assert len(result.results) == 1
    assert item.destination_path.exists()


def test_password_value_never_appears_in_result_repr(
    tmp_path: Path, mapping_store: InMemoryMappingStore
) -> None:
    item = _valid_item(tmp_path, "a")
    result = anonymize_workbooks([item], mapping_store, None, provenance_password=PASSWORD)
    assert PASSWORD not in repr(result)


# ---------------------------------------------------------------------------
# Materialization
# ---------------------------------------------------------------------------


def test_generator_materialized_exactly_once(
    tmp_path: Path, mapping_store: InMemoryMappingStore
) -> None:
    item_a = _valid_item(tmp_path, "a")
    item_b = _valid_item(tmp_path, "b", company="ООО Лютик")
    iterable = _CountingIterable([item_a, item_b])

    result = anonymize_workbooks(iterable, mapping_store, None, provenance_password=PASSWORD)

    assert iterable.iter_calls == 1
    assert len(result.results) == 2


def test_generator_exception_propagates_unchanged(
    mapping_store: InMemoryMappingStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    counts = _make_call_counter(monkeypatch)

    def failing_generator():
        raise _ProbeError("boom during materialization")
        yield  # pragma: no cover - делает функцию generator'ом

    with pytest.raises(_ProbeError):
        anonymize_workbooks(failing_generator(), mapping_store, None, provenance_password=PASSWORD)

    assert counts["anonymize_workbook"] == 0


# ---------------------------------------------------------------------------
# Same-item path collisions
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "field_a,field_b",
    [
        ("source_path", "destination_path"),
        ("source_path", "provenance_path"),
        ("destination_path", "provenance_path"),
    ],
)
def test_same_item_path_collision_rejected(
    tmp_path: Path,
    mapping_store: InMemoryMappingStore,
    monkeypatch: pytest.MonkeyPatch,
    field_a: str,
    field_b: str,
) -> None:
    counts = _make_call_counter(monkeypatch)
    item = _valid_item(tmp_path, "a")
    shared_value = getattr(item, field_a)
    item = dataclasses.replace(item, **{field_b: shared_value})

    with pytest.raises(BatchValidationError):
        anonymize_workbooks([item], mapping_store, None, provenance_password=PASSWORD)
    assert counts["anonymize_workbook"] == 0


# ---------------------------------------------------------------------------
# Cross-item path collisions
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "role_a,role_b",
    [
        ("source_path", "source_path"),
        ("destination_path", "destination_path"),
        ("provenance_path", "provenance_path"),
        ("destination_path", "source_path"),
        ("source_path", "destination_path"),
        ("provenance_path", "destination_path"),
        ("destination_path", "provenance_path"),
    ],
)
def test_cross_item_path_collision_rejected(
    tmp_path: Path,
    mapping_store: InMemoryMappingStore,
    monkeypatch: pytest.MonkeyPatch,
    role_a: str,
    role_b: str,
) -> None:
    counts = _make_call_counter(monkeypatch)
    shared = _shared_path_for(tmp_path, role_a, role_b)

    item_a = dataclasses.replace(_valid_item(tmp_path, "a"), **{role_a: shared})
    item_b = dataclasses.replace(_valid_item(tmp_path, "b"), **{role_b: shared})

    with pytest.raises(BatchValidationError):
        anonymize_workbooks([item_a, item_b], mapping_store, None, provenance_password=PASSWORD)
    assert counts["anonymize_workbook"] == 0


@pytest.mark.parametrize("direction", ["provenance_equals_source", "source_equals_provenance"])
def test_cross_item_provenance_source_pair_rejected_before_first_job(
    tmp_path: Path,
    mapping_store: InMemoryMappingStore,
    monkeypatch: pytest.MonkeyPatch,
    direction: str,
) -> None:
    """
    provenance_path требует НЕ существовать, source_path требует
    существовать — для ОДНОГО физического пути эти требования взаимно
    исключают друг друга, поэтому данная пара коллизий фактически всегда
    перехватывается ближайшим per-item preflight-check (существование
    provenance ЛИБО существование source), а не явной cross-item collision
    matrix. Итог идентичен frozen-контракту: BatchValidationError до
    первого anonymize_workbook, что и проверяется здесь — конкретный текст
    сообщения не фиксируется.
    """
    counts = _make_call_counter(monkeypatch)
    item_a = _valid_item(tmp_path, "a")
    item_b = _valid_item(tmp_path, "b")

    if direction == "provenance_equals_source":
        item_b = dataclasses.replace(item_b, provenance_path=item_a.source_path)
    else:
        item_a = dataclasses.replace(item_a, source_path=item_b.provenance_path)

    with pytest.raises(BatchValidationError):
        anonymize_workbooks([item_a, item_b], mapping_store, None, provenance_password=PASSWORD)
    assert counts["anonymize_workbook"] == 0


# ---------------------------------------------------------------------------
# Success
# ---------------------------------------------------------------------------


def test_two_file_batch_success(tmp_path: Path, mapping_store: InMemoryMappingStore) -> None:
    item_a = _valid_item(tmp_path, "a", company="ООО Ромашка")
    item_b = _valid_item(tmp_path, "b", company="ООО Лютик")

    result = anonymize_workbooks([item_a, item_b], mapping_store, None, provenance_password=PASSWORD)

    assert len(result.results) == 2
    assert item_a.destination_path.exists()
    assert item_b.destination_path.exists()
    assert item_a.provenance_path.exists()
    assert item_b.provenance_path.exists()


def test_input_order_preserved_in_result(tmp_path: Path, mapping_store: InMemoryMappingStore) -> None:
    item_a = _valid_item(tmp_path, "a")
    item_b = _valid_item(tmp_path, "b")
    item_c = _valid_item(tmp_path, "c")

    result = anonymize_workbooks(
        [item_a, item_b, item_c], mapping_store, None, provenance_password=PASSWORD
    )

    assert [r.output_path for r in result.results] == [
        item_a.destination_path,
        item_b.destination_path,
        item_c.destination_path,
    ]


def test_same_company_across_files_gets_same_alias(
    tmp_path: Path, mapping_store: InMemoryMappingStore
) -> None:
    item_a = _valid_item(tmp_path, "a", company="ООО Ромашка")
    item_b = _valid_item(tmp_path, "b", company="ООО Ромашка")

    anonymize_workbooks([item_a, item_b], mapping_store, None, provenance_password=PASSWORD)

    alias_a = openpyxl.load_workbook(item_a.destination_path)["Data"].cell(row=2, column=1).value
    alias_b = openpyxl.load_workbook(item_b.destination_path)["Data"].cell(row=2, column=1).value

    assert alias_a == alias_b
    assert len(mapping_store.entries()) == 1


def test_same_identifier_across_files_gets_same_token(
    tmp_path: Path, mapping_store: InMemoryMappingStore, identifier_store: InMemoryIdentifierMappingStore
) -> None:
    item_a = _valid_inn_item(tmp_path, "a", VALID_INN)
    item_b = _valid_inn_item(tmp_path, "b", VALID_INN)

    anonymize_workbooks(
        [item_a, item_b], mapping_store, identifier_store, provenance_password=PASSWORD
    )

    token_a = openpyxl.load_workbook(item_a.destination_path)["Data"].cell(row=2, column=1).value
    token_b = openpyxl.load_workbook(item_b.destination_path)["Data"].cell(row=2, column=1).value

    assert token_a == token_b
    assert len(identifier_store.entries()) == 1


def test_different_job_id_per_output(tmp_path: Path, mapping_store: InMemoryMappingStore) -> None:
    item_a = _valid_item(tmp_path, "a")
    item_b = _valid_item(tmp_path, "b")

    result = anonymize_workbooks([item_a, item_b], mapping_store, None, provenance_password=PASSWORD)

    assert result.results[0].job_id != result.results[1].job_id


def test_separate_provenance_sidecar_per_output(
    tmp_path: Path, mapping_store: InMemoryMappingStore
) -> None:
    item_a = _valid_item(tmp_path, "a")
    item_b = _valid_item(tmp_path, "b")

    result = anonymize_workbooks([item_a, item_b], mapping_store, None, provenance_password=PASSWORD)

    assert result.results[0].provenance_path != result.results[1].provenance_path
    provenance_a = EncryptedFileProvenanceStore(result.results[0].provenance_path, PASSWORD)
    provenance_b = EncryptedFileProvenanceStore(result.results[1].provenance_path, PASSWORD)
    assert provenance_a.job_id == result.results[0].job_id
    assert provenance_b.job_id == result.results[1].job_id


def test_job_id_output_provenance_binding_per_item(
    tmp_path: Path, mapping_store: InMemoryMappingStore
) -> None:
    item_a = _valid_item(tmp_path, "a")
    item_b = _valid_item(tmp_path, "b")

    result = anonymize_workbooks([item_a, item_b], mapping_store, None, provenance_password=PASSWORD)

    for job_result in result.results:
        output = openpyxl.load_workbook(job_result.output_path)
        assert output.custom_doc_props["DataAnonymizer.JobId"].value == job_result.job_id
        reopened = EncryptedFileProvenanceStore(job_result.provenance_path, PASSWORD)
        assert reopened.job_id == job_result.job_id


def test_multi_sheet_item_success(tmp_path: Path, mapping_store: InMemoryMappingStore) -> None:
    source = tmp_path / "source_multi.xlsx"
    wb = openpyxl.Workbook()
    ws1 = wb.active
    ws1.title = "First"
    ws1["A1"] = "Company"
    ws1["A2"] = "ООО Ромашка"
    ws2 = wb.create_sheet("Second")
    ws2["A1"] = "Company"
    ws2["A2"] = "ООО Лютик"
    wb.save(source)

    item = AnonymizationBatchItem(
        source_path=source,
        destination_path=tmp_path / "output_multi.xlsx",
        provenance_path=tmp_path / "provenance_multi.enc",
        rules_by_sheet={"First": {1: _company_rule()}, "Second": {1: _company_rule()}},
    )

    result = anonymize_workbooks([item], mapping_store, None, provenance_password=PASSWORD)

    assert result.results[0].processed_sheet_names == ("First", "Second")
    assert len(mapping_store.entries()) == 2


def test_same_mapping_store_object_passed_to_every_call(
    tmp_path: Path, mapping_store: InMemoryMappingStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen_stores: list[object] = []
    real = batch_module.anonymize_workbook

    def spying(source_path, destination_path, rules_by_sheet, mapping_store_arg, identifier_store_arg, **kwargs):
        seen_stores.append(mapping_store_arg)
        return real(source_path, destination_path, rules_by_sheet, mapping_store_arg, identifier_store_arg, **kwargs)

    monkeypatch.setattr(batch_module, "anonymize_workbook", spying)

    item_a = _valid_item(tmp_path, "a")
    item_b = _valid_item(tmp_path, "b")
    anonymize_workbooks([item_a, item_b], mapping_store, None, provenance_password=PASSWORD)

    assert len(seen_stores) == 2
    assert seen_stores[0] is mapping_store
    assert seen_stores[1] is mapping_store


def test_same_identifier_store_object_passed_to_every_call(
    tmp_path: Path,
    mapping_store: InMemoryMappingStore,
    identifier_store: InMemoryIdentifierMappingStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen_stores: list[object] = []
    real = batch_module.anonymize_workbook

    def spying(source_path, destination_path, rules_by_sheet, mapping_store_arg, identifier_store_arg, **kwargs):
        seen_stores.append(identifier_store_arg)
        return real(source_path, destination_path, rules_by_sheet, mapping_store_arg, identifier_store_arg, **kwargs)

    monkeypatch.setattr(batch_module, "anonymize_workbook", spying)

    item_a = _valid_inn_item(tmp_path, "a", VALID_INN)
    item_b = _valid_inn_item(tmp_path, "b", VALID_INN_2)
    anonymize_workbooks([item_a, item_b], mapping_store, identifier_store, provenance_password=PASSWORD)

    assert len(seen_stores) == 2
    assert seen_stores[0] is identifier_store
    assert seen_stores[1] is identifier_store


# ---------------------------------------------------------------------------
# Failure semantics
# ---------------------------------------------------------------------------


def test_item_a_success_item_b_anonymizer_failure_item_c_not_run(
    tmp_path: Path, mapping_store: InMemoryMappingStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    counts = _make_call_counter(monkeypatch)

    item_a = _valid_item(tmp_path, "a", company="ООО Ромашка")

    bad_source = tmp_path / "source_b.xlsx"
    _source_workbook(bad_source, "Data", ["Company"], [[42]])  # невалидное значение под COMPANY
    item_b = AnonymizationBatchItem(
        source_path=bad_source,
        destination_path=tmp_path / "output_b.xlsx",
        provenance_path=tmp_path / "provenance_b.enc",
        rules_by_sheet={"Data": {1: _company_rule()}},
    )

    item_c = _valid_item(tmp_path, "c")

    with pytest.raises(InvalidPseudonymizationValueError):
        anonymize_workbooks(
            [item_a, item_b, item_c], mapping_store, None, provenance_password=PASSWORD
        )

    assert counts["anonymize_workbook"] == 2  # A и B — C не должен был запуститься

    assert item_a.destination_path.exists()
    assert item_a.provenance_path.exists()
    assert len(mapping_store.entries()) == 1
    assert mapping_store.entries()[0].real_value == "ООО Ромашка"

    assert not item_b.destination_path.exists()
    assert item_b.provenance_path.exists()  # sidecar B уже создан к моменту сбоя

    assert not item_c.destination_path.exists()
    assert not item_c.provenance_path.exists()


def test_item_b_writer_failure_a_remains_c_not_run(
    tmp_path: Path, mapping_store: InMemoryMappingStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    call_count = {"n": 0}

    def wrapped_writer(source_path_arg, tables, destination_path_arg, *, job_id):
        call_count["n"] += 1
        if call_count["n"] == 1:
            # Первый вызов — item A, должен пройти нормально.
            return real_write_anonymized_workbook(
                source_path_arg, tables, destination_path_arg, job_id=job_id
            )
        # Второй вызов — item B: намеренно провоцируем реальный
        # WriterValidationError через bogus FlatTable для несуществующего
        # в source листа.
        bogus = FlatTable(
            sheet_name="GhostSheet",
            header_row=(CellRecord(row=1, column=1, value="X"),),
            rows=(),
        )
        return real_write_anonymized_workbook(
            source_path_arg, tuple(tables) + (bogus,), destination_path_arg, job_id=job_id
        )

    monkeypatch.setattr(single_file_module, "write_anonymized_workbook", wrapped_writer)

    item_a = _valid_item(tmp_path, "a")
    item_b = _valid_item(tmp_path, "b")
    item_c = _valid_item(tmp_path, "c")

    with pytest.raises(WriterValidationError):
        anonymize_workbooks([item_a, item_b, item_c], mapping_store, None, provenance_password=PASSWORD)

    assert item_a.destination_path.exists()
    assert not item_b.destination_path.exists()
    assert not item_c.destination_path.exists()


def test_no_batch_result_returned_on_failure(
    tmp_path: Path, mapping_store: InMemoryMappingStore
) -> None:
    item_a = _valid_item(tmp_path, "a")
    bad_source = tmp_path / "source_b.xlsx"
    _source_workbook(bad_source, "Data", ["Company"], [[42]])
    item_b = AnonymizationBatchItem(
        source_path=bad_source,
        destination_path=tmp_path / "output_b.xlsx",
        provenance_path=tmp_path / "provenance_b.enc",
        rules_by_sheet={"Data": {1: _company_rule()}},
    )

    try:
        anonymize_workbooks([item_a, item_b], mapping_store, None, provenance_password=PASSWORD)
        assert False, "ожидалось исключение"
    except InvalidPseudonymizationValueError:
        pass  # AnonymizationBatchResult НЕ создан — функция не дошла до return


# ---------------------------------------------------------------------------
# Rule validation boundary (frozen MVP limitation)
# ---------------------------------------------------------------------------


def test_invalid_rules_in_later_item_not_caught_by_batch_preflight(
    tmp_path: Path, mapping_store: InMemoryMappingStore
) -> None:
    item_a = _valid_item(tmp_path, "a")
    item_b = _valid_item(tmp_path, "b")
    # Правило покрывает несуществующую в source-таблице колонку 2 —
    # структурно валидный Mapping[int, FieldRule], но coverage-нарушение
    # обнаруживается только внутри anonymize_flat_table (Stage 7A), а не
    # batch preflight (frozen MVP boundary, см. Contract Freeze §24).
    item_b = dataclasses.replace(
        item_b, rules_by_sheet={"Data": {1: _company_rule(), 2: _company_rule("Extra")}}
    )

    with pytest.raises(InvalidRuleConfigurationError):
        anonymize_workbooks([item_a, item_b], mapping_store, None, provenance_password=PASSWORD)

    assert item_a.destination_path.exists()  # A успел завершиться до падения B


# ---------------------------------------------------------------------------
# identifier_store=None
# ---------------------------------------------------------------------------


def test_identifier_store_none_later_item_requires_identifier_tokenization(
    tmp_path: Path, mapping_store: InMemoryMappingStore
) -> None:
    item_a = _valid_item(tmp_path, "a")
    item_b = _valid_inn_item(tmp_path, "b", VALID_INN)
    item_c = _valid_item(tmp_path, "c")

    with pytest.raises(MissingIdentifierStoreError):
        anonymize_workbooks([item_a, item_b, item_c], mapping_store, None, provenance_password=PASSWORD)

    assert item_a.destination_path.exists()
    assert not item_b.destination_path.exists()
    assert not item_c.destination_path.exists()


# ---------------------------------------------------------------------------
# Retry end-to-end
# ---------------------------------------------------------------------------


def test_retry_after_partial_failure_with_new_provenance_paths(
    tmp_path: Path, mapping_store: InMemoryMappingStore
) -> None:
    item_a = _valid_item(tmp_path, "a", company="ООО Ромашка")

    broken_source_b = tmp_path / "source_b.xlsx"
    _source_workbook(broken_source_b, "Data", ["Company"], [[42]])  # невалидное значение
    item_b = AnonymizationBatchItem(
        source_path=broken_source_b,
        destination_path=tmp_path / "output_b.xlsx",
        provenance_path=tmp_path / "provenance_b.enc",
        rules_by_sheet={"Data": {1: _company_rule()}},
    )
    item_c = _valid_item(tmp_path, "c", company="ООО Ромашка")

    with pytest.raises(InvalidPseudonymizationValueError):
        anonymize_workbooks([item_a, item_b, item_c], mapping_store, None, provenance_password=PASSWORD)

    assert item_a.destination_path.exists()
    assert not item_c.destination_path.exists()

    provenance_a_mtime_before = item_a.provenance_path.stat().st_mtime_ns
    destination_a_mtime_before = item_a.destination_path.stat().st_mtime_ns

    # Caller исправляет причину сбоя B и формирует НОВЫЙ batch только из
    # B + C, с НОВЫМИ provenance_path для ОБОИХ (единое MVP-правило —
    # даже для C, который вообще не запускался).
    fixed_source_b = tmp_path / "source_b_fixed.xlsx"
    _source_workbook(fixed_source_b, "Data", ["Company"], [["ООО Ромашка"]])
    retry_item_b = AnonymizationBatchItem(
        source_path=fixed_source_b,
        destination_path=item_b.destination_path,
        provenance_path=tmp_path / "provenance_b_retry.enc",
        rules_by_sheet={"Data": {1: _company_rule()}},
    )
    retry_item_c = AnonymizationBatchItem(
        source_path=item_c.source_path,
        destination_path=item_c.destination_path,
        provenance_path=tmp_path / "provenance_c_retry.enc",
        rules_by_sheet=item_c.rules_by_sheet,
    )

    retry_result = anonymize_workbooks(
        [retry_item_b, retry_item_c], mapping_store, None, provenance_password=PASSWORD
    )

    assert len(retry_result.results) == 2
    assert retry_item_b.destination_path.exists()
    assert retry_item_c.destination_path.exists()

    alias_a = openpyxl.load_workbook(item_a.destination_path)["Data"].cell(row=2, column=1).value
    alias_b = openpyxl.load_workbook(retry_item_b.destination_path)["Data"].cell(row=2, column=1).value
    alias_c = openpyxl.load_workbook(retry_item_c.destination_path)["Data"].cell(row=2, column=1).value
    assert alias_a == alias_b == alias_c

    # A не запускался повторно: файлы A физически не изменились.
    assert item_a.provenance_path.stat().st_mtime_ns == provenance_a_mtime_before
    assert item_a.destination_path.stat().st_mtime_ns == destination_a_mtime_before


# ---------------------------------------------------------------------------
# Exact raw identity
# ---------------------------------------------------------------------------


def test_exact_raw_identity_not_normalized_across_batch(
    tmp_path: Path, mapping_store: InMemoryMappingStore
) -> None:
    item_a = _valid_item(tmp_path, "a", company="ООО Альфа")
    item_b = _valid_item(tmp_path, "b", company="ООО «Альфа»")

    anonymize_workbooks([item_a, item_b], mapping_store, None, provenance_password=PASSWORD)

    alias_a = openpyxl.load_workbook(item_a.destination_path)["Data"].cell(row=2, column=1).value
    alias_b = openpyxl.load_workbook(item_b.destination_path)["Data"].cell(row=2, column=1).value

    assert alias_a != alias_b
    assert len(mapping_store.entries()) == 2


# ---------------------------------------------------------------------------
# Security
# ---------------------------------------------------------------------------


def test_batch_item_dataclass_fields_exact() -> None:
    field_names = {f.name for f in dataclasses.fields(AnonymizationBatchItem)}
    assert field_names == {"source_path", "destination_path", "provenance_path", "rules_by_sheet"}


def test_batch_result_dataclass_fields_exact() -> None:
    field_names = {f.name for f in dataclasses.fields(AnonymizationBatchResult)}
    assert field_names == {"results"}


def test_no_clear_call_in_batch_module() -> None:
    source_text = Path(batch_module.__file__).read_text(encoding="utf-8")
    assert ".clear(" not in source_text


def test_batch_module_does_not_import_excel_or_crypto_internals() -> None:
    """
    AST-based (а не naive substring) проверка — намеренно, чтобы не
    ловить ложные совпадения в docstring-прозе модуля, которая явно
    ОБСУЖДАЕТ, что именно batch.py НЕ создаёт/не импортирует (см. Stage
    8.2 corrective review, тот же урок про naive text-grep).
    """
    source_text = Path(batch_module.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source_text)

    imported_names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                imported_names.add(alias.name)
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            imported_names.add(module)
            for alias in node.names:
                imported_names.add(f"{module}.{alias.name}")

    forbidden_substrings = ("openpyxl", "secrets", "app.excel", "provenance_encrypted", "provenance_base")
    for name in imported_names:
        for forbidden in forbidden_substrings:
            assert forbidden not in name, f"batch.py не должен импортировать {name!r}"
