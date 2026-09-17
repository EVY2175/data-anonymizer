"""
Тесты app.mapping.provenance_base (ProvenanceStore) и
app.mapping.provenance_memory (InMemoryProvenanceStore).

Stage 7C.2 — строго in-memory storage contract. Здесь НЕ тестируется:
encrypted persistence (Stage 7C.3), anonymizer integration (Stage 7C.4),
job_id generation (ответственность вызывающего кода, вне scope этого
модуля), Writer/Restore (Stage 7C.5+).
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from app.mapping.provenance_base import ProvenanceConflictError, ProvenanceStore
from app.mapping.provenance_memory import InMemoryProvenanceStore
from app.models.provenance import IdentifierCellProvenance, IdentifierRepresentation

JOB_ID = "job-1"


@pytest.fixture()
def store() -> InMemoryProvenanceStore:
    return InMemoryProvenanceStore(job_id=JOB_ID)


def _entry(
    sheet_name: str,
    row: int,
    column: int,
    token: str,
    representation: IdentifierRepresentation = IdentifierRepresentation.STRING,
) -> IdentifierCellProvenance:
    return IdentifierCellProvenance(
        sheet_name=sheet_name, row=row, column=column, token=token, representation=representation
    )


# ---------------------------------------------------------------------------
# A. Контракт ProvenanceStore (ABC)
# ---------------------------------------------------------------------------


def test_abstract_store_cannot_be_instantiated() -> None:
    with pytest.raises(TypeError):
        ProvenanceStore()  # type: ignore[abstract]


def test_in_memory_store_satisfies_provenance_store_contract(
    store: InMemoryProvenanceStore,
) -> None:
    assert isinstance(store, ProvenanceStore)


def test_abstract_api_has_exactly_five_members() -> None:
    assert ProvenanceStore.__abstractmethods__ == {
        "job_id",
        "add_many",
        "get_by_coordinate",
        "entries",
        "clear",
    }


def test_no_single_add_method() -> None:
    assert not hasattr(ProvenanceStore, "add")
    assert not hasattr(InMemoryProvenanceStore, "add")


def test_no_forbidden_extra_methods() -> None:
    forbidden_names = (
        "save",
        "load",
        "serialize",
        "deserialize",
        "path",
        "password",
        "schema_version",
        "get_by_token",
        "get_by_job",
        "generate_job_id",
    )
    for name in forbidden_names:
        assert not hasattr(ProvenanceStore, name)
        assert not hasattr(InMemoryProvenanceStore, name)


# ---------------------------------------------------------------------------
# B. job_id contract
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("valid_job_id", ["job-1", "0123456789abcdef", " job-1 "])
def test_job_id_accepts_valid_exact(valid_job_id: str) -> None:
    store = InMemoryProvenanceStore(job_id=valid_job_id)
    assert store.job_id == valid_job_id


@pytest.mark.parametrize("invalid_job_id", [None, 123, "", "   "])
def test_job_id_rejects_invalid(invalid_job_id: object) -> None:
    with pytest.raises(ValueError):
        InMemoryProvenanceStore(job_id=invalid_job_id)  # type: ignore[arg-type]


def test_job_id_not_generated_by_store() -> None:
    store_a = InMemoryProvenanceStore(job_id="job-a")
    store_b = InMemoryProvenanceStore(job_id="job-a")
    assert store_a.job_id == store_b.job_id == "job-a"


# ---------------------------------------------------------------------------
# C. Basic store behavior
# ---------------------------------------------------------------------------


def test_empty_store(store: InMemoryProvenanceStore) -> None:
    assert store.entries() == ()
    assert store.get_by_coordinate("Sheet1", 1, 1) is None


def test_add_many_empty_batch_is_noop(store: InMemoryProvenanceStore) -> None:
    store.add_many([])
    assert store.entries() == ()


def test_successful_one_entry_batch(store: InMemoryProvenanceStore) -> None:
    entry = _entry("Sheet1", 2, 3, "INN_AAAAAAAAA")
    store.add_many([entry])
    assert store.entries() == (entry,)
    assert store.get_by_coordinate("Sheet1", 2, 3) == entry


def test_successful_multiple_entry_batch(store: InMemoryProvenanceStore) -> None:
    entry_a = _entry("Sheet1", 1, 1, "INN_AAAAAAAAA")
    entry_b = _entry("Sheet1", 2, 1, "KPP_BBBBBBBBB")
    entry_c = _entry("Sheet1", 3, 1, "OGRN_CCCCCCCC")
    store.add_many([entry_a, entry_b, entry_c])
    assert set(store.entries()) == {entry_a, entry_b, entry_c}


def test_missing_coordinate_returns_none(store: InMemoryProvenanceStore) -> None:
    store.add_many([_entry("Sheet1", 1, 1, "INN_AAAAAAAAA")])
    assert store.get_by_coordinate("Sheet1", 99, 99) is None


def test_get_by_coordinate_does_not_normalize_sheet_name(store: InMemoryProvenanceStore) -> None:
    entry = _entry(" Sheet1 ", 1, 1, "INN_AAAAAAAAA")
    store.add_many([entry])
    assert store.get_by_coordinate(" Sheet1 ", 1, 1) == entry
    assert store.get_by_coordinate("Sheet1", 1, 1) is None


# ---------------------------------------------------------------------------
# D. Same token, multiple coordinates
# ---------------------------------------------------------------------------


def test_same_token_different_coordinates_both_stored(store: InMemoryProvenanceStore) -> None:
    entry_string = _entry("Sheet1", 2, 1, "INN_AAAAAAAAA", IdentifierRepresentation.STRING)
    entry_integer = _entry("Sheet1", 3, 1, "INN_AAAAAAAAA", IdentifierRepresentation.INTEGER)
    store.add_many([entry_string, entry_integer])
    assert store.get_by_coordinate("Sheet1", 2, 1) == entry_string
    assert store.get_by_coordinate("Sheet1", 3, 1) == entry_integer
    assert len(store.entries()) == 2


# ---------------------------------------------------------------------------
# E. Exact duplicate semantics
# ---------------------------------------------------------------------------


def test_exact_duplicate_against_existing_is_idempotent(store: InMemoryProvenanceStore) -> None:
    entry = _entry("Sheet1", 1, 1, "INN_AAAAAAAAA")
    store.add_many([entry])
    store.add_many([entry])
    assert store.entries() == (entry,)


def test_exact_duplicates_inside_same_batch_is_idempotent(store: InMemoryProvenanceStore) -> None:
    entry = _entry("Sheet1", 1, 1, "INN_AAAAAAAAA")
    store.add_many([entry, entry, entry])
    assert store.entries() == (entry,)


def test_mixed_existing_and_new_duplicates_correct_final_state(
    store: InMemoryProvenanceStore,
) -> None:
    entry_a = _entry("Sheet1", 1, 1, "INN_AAAAAAAAA")
    store.add_many([entry_a])

    entry_b = _entry("Sheet1", 2, 1, "KPP_BBBBBBBBB")
    entry_c = _entry("Sheet1", 3, 1, "OGRN_CCCCCCCC")
    store.add_many([entry_a, entry_b, entry_a, entry_c, entry_b])

    assert set(store.entries()) == {entry_a, entry_b, entry_c}
    assert len(store.entries()) == 3


# ---------------------------------------------------------------------------
# F. Conflict semantics
# ---------------------------------------------------------------------------


def test_same_coordinate_different_token_conflicts(store: InMemoryProvenanceStore) -> None:
    entry_a = _entry("Sheet1", 1, 1, "INN_AAAAAAAAA")
    store.add_many([entry_a])
    before = store.entries()

    conflicting = _entry("Sheet1", 1, 1, "INN_DIFFERENT")
    with pytest.raises(ProvenanceConflictError):
        store.add_many([conflicting])

    assert store.entries() == before


def test_same_coordinate_same_token_different_representation_conflicts(
    store: InMemoryProvenanceStore,
) -> None:
    entry_a = _entry("Sheet1", 1, 1, "INN_AAAAAAAAA", IdentifierRepresentation.STRING)
    store.add_many([entry_a])
    before = store.entries()

    conflicting = _entry("Sheet1", 1, 1, "INN_AAAAAAAAA", IdentifierRepresentation.INTEGER)
    with pytest.raises(ProvenanceConflictError):
        store.add_many([conflicting])

    assert store.entries() == before


# ---------------------------------------------------------------------------
# G. Late conflict / intra-batch conflict (transactional core)
# ---------------------------------------------------------------------------


def test_late_conflict_against_existing_leaves_store_unchanged(
    store: InMemoryProvenanceStore,
) -> None:
    entry_a = _entry("Sheet1", 1, 1, "INN_AAAAAAAAA")
    store.add_many([entry_a])
    initial_snapshot = store.entries()

    entry_b = _entry("Sheet1", 2, 1, "KPP_BBBBBBBBB")
    entry_c = _entry("Sheet1", 3, 1, "OGRN_CCCCCCCC")
    entry_d = _entry("Sheet1", 4, 1, "INN_DDDDDDDDD")
    conflicting_a = _entry("Sheet1", 1, 1, "INN_CONFLICT")

    with pytest.raises(ProvenanceConflictError):
        store.add_many([entry_b, entry_c, entry_d, conflicting_a])

    assert store.entries() == initial_snapshot
    assert store.get_by_coordinate("Sheet1", 2, 1) is None
    assert store.get_by_coordinate("Sheet1", 3, 1) is None
    assert store.get_by_coordinate("Sheet1", 4, 1) is None


def test_intra_batch_conflict_leaves_empty_store_unchanged(
    store: InMemoryProvenanceStore,
) -> None:
    entry_a = _entry("Sheet1", 1, 1, "INN_AAAAAAAAA")
    entry_b = _entry("Sheet1", 2, 1, "KPP_BBBBBBBBB")
    entry_c = _entry("Sheet1", 3, 1, "OGRN_CCCCCCCC")
    conflicting_a = _entry("Sheet1", 1, 1, "INN_CONFLICT")

    with pytest.raises(ProvenanceConflictError):
        store.add_many([entry_a, entry_b, entry_c, conflicting_a])

    assert store.entries() == ()


# ---------------------------------------------------------------------------
# H. Iterable contract (generator, materialize-once)
# ---------------------------------------------------------------------------


def test_add_many_accepts_generator(store: InMemoryProvenanceStore) -> None:
    entries = [
        _entry("Sheet1", 1, 1, "INN_AAAAAAAAA"),
        _entry("Sheet1", 2, 1, "KPP_BBBBBBBBB"),
    ]
    store.add_many(e for e in entries)
    assert set(store.entries()) == set(entries)


def test_add_many_materializes_iterable_exactly_once(store: InMemoryProvenanceStore) -> None:
    consumption_count = {"n": 0}

    def one_shot() -> Iterator[IdentifierCellProvenance]:
        consumption_count["n"] += 1
        yield _entry("Sheet1", 1, 1, "INN_AAAAAAAAA")
        yield _entry("Sheet1", 2, 1, "KPP_BBBBBBBBB")

    store.add_many(one_shot())

    assert consumption_count["n"] == 1
    assert len(store.entries()) == 2


# ---------------------------------------------------------------------------
# I. Invalid entry type
# ---------------------------------------------------------------------------


def test_invalid_entry_type_raises_and_store_unchanged(store: InMemoryProvenanceStore) -> None:
    valid_a = _entry("Sheet1", 1, 1, "INN_AAAAAAAAA")
    valid_b = _entry("Sheet1", 2, 1, "KPP_BBBBBBBBB")

    with pytest.raises(TypeError):
        store.add_many([valid_a, "not-a-provenance-entry", valid_b])  # type: ignore[list-item]

    assert store.entries() == ()


# ---------------------------------------------------------------------------
# J. Deterministic entries() ordering
# ---------------------------------------------------------------------------


def test_entries_are_sorted_by_sheet_row_column() -> None:
    store = InMemoryProvenanceStore(job_id=JOB_ID)
    entry_sheet_b = _entry("SheetB", 5, 2, "INN_AAAAAAAAA")
    entry_sheet_a_row10 = _entry("SheetA", 10, 1, "KPP_BBBBBBBBB")
    entry_sheet_a_row2_col3 = _entry("SheetA", 2, 3, "OGRN_CCCCCCCC")
    entry_sheet_a_row2_col1 = _entry("SheetA", 2, 1, "INN_DDDDDDDDD")

    store.add_many(
        [entry_sheet_b, entry_sheet_a_row10, entry_sheet_a_row2_col3, entry_sheet_a_row2_col1]
    )

    assert store.entries() == (
        entry_sheet_a_row2_col1,
        entry_sheet_a_row2_col3,
        entry_sheet_a_row10,
        entry_sheet_b,
    )


# ---------------------------------------------------------------------------
# K. clear()
# ---------------------------------------------------------------------------


def test_clear_removes_all_entries_but_keeps_job_id(store: InMemoryProvenanceStore) -> None:
    store.add_many(
        [_entry("Sheet1", 1, 1, "INN_AAAAAAAAA"), _entry("Sheet1", 2, 1, "KPP_BBBBBBBBB")]
    )
    original_job_id = store.job_id

    store.clear()

    assert store.entries() == ()
    assert store.get_by_coordinate("Sheet1", 1, 1) is None
    assert store.job_id == original_job_id


# ---------------------------------------------------------------------------
# L. Confidentiality
# ---------------------------------------------------------------------------


def test_conflict_message_does_not_expose_token_job_id_sheet_name() -> None:
    secret_job_id = "SuperSecretJobIdentifier999"
    secret_sheet_name = "SuperSecretSheetName999"
    secret_token = "INN_SUPERSECRETTOKEN"

    store = InMemoryProvenanceStore(job_id=secret_job_id)
    store.add_many([_entry(secret_sheet_name, 1, 1, secret_token)])

    conflicting = _entry(secret_sheet_name, 1, 1, "INN_DIFFERENTTOKEN")
    with pytest.raises(ProvenanceConflictError) as exc_info:
        store.add_many([conflicting])

    message = str(exc_info.value)
    assert secret_job_id not in message
    assert secret_sheet_name not in message
    assert secret_token not in message
    assert "INN_DIFFERENTTOKEN" not in message
