"""
Тесты Stage 10B.3: adopt_stores() -- создание Workspace поверх уже
существующих mapping/identifier сторов с ре-шифрованием под пароль
Workspace.

Группы:
    A. Успешный adopt (mapping + identifier)
    B. OD-8: adopt без identifier source
    C. Защитные проверки путей (containment / same file / missing)
    D. OD-7: неверный source password -- санитизировано
    E. Верификация после записи + fail-closed при её провале
    F. Clean-skeleton retry применим и к adopt_stores
    G. Политика паролей (OD-1 для target, non-empty для source)
    E2. Correction Pass #2 (MINOR-1): равное количество записей, но
       семантическая порча содержимого -- обязана обнаруживаться полной
       entry-эквивалентностью, а не только сравнением count
    H. Correction Pass #2 (BLOCKER-1): детерминированные TOCTOU-гонки
       adopt_stores против create_workspace/adopt_stores
"""

from __future__ import annotations

import dataclasses
import os
import threading

import pytest

from app.mapping.encrypted_file import EncryptedFileMappingStore
from app.mapping.encrypted_identifier_file import EncryptedFileIdentifierMappingStore
from app.models.entities import EntityType, MappingEntry
from app.models.identifiers import IdentifierMappingEntry, IdentifierType
from app.workspace import adopt_stores, create_workspace, open_workspace
from app.workspace.errors import (
    WorkspaceBindingError,
    WorkspaceBindingReason,
    WorkspaceCorruptedError,
    WorkspaceCorruptedReason,
    WorkspaceInputError,
)

TARGET_PASSWORD = "target-workspace-password-1"
SOURCE_MAPPING_PASSWORD = "SENTINEL_SRC_MAPPING_PW_9A11"
SOURCE_IDENTIFIER_PASSWORD = "SENTINEL_SRC_IDENTIFIER_PW_7B22"


def _mentry(alias: str, real_value: str = None) -> MappingEntry:
    if real_value is None:
        real_value = f"R_{alias}"
    return MappingEntry(alias=alias, real_value=real_value, entity_type=EntityType.COMPANY, parent_alias=None)


def _ientry(token: str, value: str) -> IdentifierMappingEntry:
    return IdentifierMappingEntry(token=token, identifier_value=value, identifier_type=IdentifierType.INN)


def _make_source_mapping(path, password, entries):
    path.parent.mkdir(parents=True, exist_ok=True)
    store = EncryptedFileMappingStore(path, password)
    for e in entries:
        store.add(e)
    return store


def _make_source_identifier(path, password, entries):
    path.parent.mkdir(parents=True, exist_ok=True)
    store = EncryptedFileIdentifierMappingStore(path, password)
    store.add_many(entries)
    return store


# ---------------------------------------------------------------------------
# A. Успешный adopt
# ---------------------------------------------------------------------------


def test_adopt_both_stores_success(tmp_path):
    src_mapping_path = tmp_path / "src" / "mapping.enc"
    src_identifier_path = tmp_path / "src" / "identifiers.enc"
    src_mapping_entries = (_mentry("C_AAAAAAAAA"), _mentry("C_BBBBBBBBB"))
    src_identifier_entries = (_ientry("INN_A0000000", "1"), _ientry("INN_B0000000", "2"))
    source_mapping = _make_source_mapping(src_mapping_path, SOURCE_MAPPING_PASSWORD, src_mapping_entries)
    _make_source_identifier(src_identifier_path, SOURCE_IDENTIFIER_PASSWORD, src_identifier_entries)

    target_root = tmp_path / "target"
    ws = adopt_stores(
        target_root, TARGET_PASSWORD,
        mapping_store_path=src_mapping_path, mapping_store_password=SOURCE_MAPPING_PASSWORD,
        identifier_store_path=src_identifier_path, identifier_store_password=SOURCE_IDENTIFIER_PASSWORD,
        label="adopted",
    )

    assert ws.info.revision == 1
    assert ws.info.label == "adopted"
    assert ws.info.mapping_entry_count == 2
    assert ws.info.identifier_entry_count == 2

    mreader, ireader = ws.read_stores()
    assert mreader.entries() == src_mapping_entries
    assert ireader.entries() == src_identifier_entries

    # Источник остался неизменным.
    assert source_mapping.entries() == src_mapping_entries


def test_adopt_reencrypts_under_target_password(tmp_path):
    src_mapping_path = tmp_path / "src" / "mapping.enc"
    _make_source_mapping(src_mapping_path, SOURCE_MAPPING_PASSWORD, (_mentry("C_AAAAAAAAA"),))

    target_root = tmp_path / "target"
    adopt_stores(
        target_root, TARGET_PASSWORD,
        mapping_store_path=src_mapping_path, mapping_store_password=SOURCE_MAPPING_PASSWORD,
    )

    # Целевой store открывается ТОЛЬКО под паролем workspace, не source.
    target_mapping_path = target_root / "stores" / "mapping.enc"
    with pytest.raises(Exception):
        EncryptedFileMappingStore(target_mapping_path, SOURCE_MAPPING_PASSWORD)
    reopened = EncryptedFileMappingStore(target_mapping_path, TARGET_PASSWORD)
    assert reopened.entries() == (_mentry("C_AAAAAAAAA"),)


def test_adopt_preserves_insertion_order(tmp_path):
    src_mapping_path = tmp_path / "src" / "mapping.enc"
    entries = tuple(_mentry(f"C_{i:09d}") for i in range(5))
    _make_source_mapping(src_mapping_path, SOURCE_MAPPING_PASSWORD, entries)

    target_root = tmp_path / "target"
    ws = adopt_stores(
        target_root, TARGET_PASSWORD,
        mapping_store_path=src_mapping_path, mapping_store_password=SOURCE_MAPPING_PASSWORD,
    )
    mreader, _ = ws.read_stores()
    assert mreader.entries() == entries


def test_adopted_workspace_usable_for_further_mutation(tmp_path):
    src_mapping_path = tmp_path / "src" / "mapping.enc"
    _make_source_mapping(src_mapping_path, SOURCE_MAPPING_PASSWORD, (_mentry("C_AAAAAAAAA"),))

    target_root = tmp_path / "target"
    ws = adopt_stores(
        target_root, TARGET_PASSWORD,
        mapping_store_path=src_mapping_path, mapping_store_password=SOURCE_MAPPING_PASSWORD,
    )
    with ws.store_mutation() as (mf, idf):
        mf.add(_mentry("C_BBBBBBBBB"))
    assert ws.info.mapping_entry_count == 2
    assert ws.info.revision == 3  # 1 (adopt) + 2 (mutation)


# ---------------------------------------------------------------------------
# B. OD-8: adopt без identifier source
# ---------------------------------------------------------------------------


def test_adopt_without_identifier_source(tmp_path):
    src_mapping_path = tmp_path / "src" / "mapping.enc"
    _make_source_mapping(src_mapping_path, SOURCE_MAPPING_PASSWORD, (_mentry("C_AAAAAAAAA"),))

    target_root = tmp_path / "target"
    ws = adopt_stores(
        target_root, TARGET_PASSWORD,
        mapping_store_path=src_mapping_path, mapping_store_password=SOURCE_MAPPING_PASSWORD,
    )
    assert ws.info.identifier_entry_count == 0
    assert not (target_root / "stores" / "identifiers.enc").exists()
    assert ws.identifier_sync.name == "CONSISTENT"


# ---------------------------------------------------------------------------
# C. Защитные проверки путей
# ---------------------------------------------------------------------------


def test_adopt_rejects_source_inside_target_root(tmp_path):
    target_root = tmp_path / "target"
    target_root.mkdir()
    src_mapping_path = target_root / "nested" / "mapping.enc"
    _make_source_mapping(src_mapping_path, SOURCE_MAPPING_PASSWORD, (_mentry("C_AAAAAAAAA"),))

    with pytest.raises(WorkspaceInputError):
        adopt_stores(
            target_root, TARGET_PASSWORD,
            mapping_store_path=src_mapping_path, mapping_store_password=SOURCE_MAPPING_PASSWORD,
        )


def test_adopt_rejects_same_file_for_both_sources(tmp_path):
    src_path = tmp_path / "src" / "shared.enc"
    _make_source_mapping(src_path, SOURCE_MAPPING_PASSWORD, (_mentry("C_AAAAAAAAA"),))

    with pytest.raises(WorkspaceInputError):
        adopt_stores(
            tmp_path / "target", TARGET_PASSWORD,
            mapping_store_path=src_path, mapping_store_password=SOURCE_MAPPING_PASSWORD,
            identifier_store_path=src_path, identifier_store_password=SOURCE_IDENTIFIER_PASSWORD,
        )


def test_adopt_rejects_missing_mapping_source(tmp_path):
    with pytest.raises(WorkspaceInputError):
        adopt_stores(
            tmp_path / "target", TARGET_PASSWORD,
            mapping_store_path=tmp_path / "nope.enc", mapping_store_password=SOURCE_MAPPING_PASSWORD,
        )


def test_adopt_rejects_missing_identifier_source(tmp_path):
    src_mapping_path = tmp_path / "src" / "mapping.enc"
    _make_source_mapping(src_mapping_path, SOURCE_MAPPING_PASSWORD, (_mentry("C_AAAAAAAAA"),))

    with pytest.raises(WorkspaceInputError):
        adopt_stores(
            tmp_path / "target", TARGET_PASSWORD,
            mapping_store_path=src_mapping_path, mapping_store_password=SOURCE_MAPPING_PASSWORD,
            identifier_store_path=tmp_path / "nope_ids.enc", identifier_store_password=SOURCE_IDENTIFIER_PASSWORD,
        )


# ---------------------------------------------------------------------------
# D. OD-7: неверный source password
# ---------------------------------------------------------------------------

_INTERNAL_FILENAME_SUFFIXES = (os.path.join("app", "workspace", "workspace.py"),)


def _internal_frame_locals_containing(exc: BaseException, sentinels: tuple) -> list:
    findings: list = []
    visited_ids: set = set()

    def scan_traceback(tb, owner_label: str) -> None:
        idx = 0
        while tb is not None:
            filename = tb.tb_frame.f_code.co_filename
            if filename.endswith(_INTERNAL_FILENAME_SUFFIXES):
                for name, value in tb.tb_frame.f_locals.items():
                    if isinstance(value, str):
                        text = value
                    elif isinstance(value, (bytes, bytearray)):
                        text = value.decode("utf-8", errors="replace")
                    else:
                        text = str(value)
                    for sentinel in sentinels:
                        if sentinel in text:
                            findings.append((owner_label, idx, os.path.basename(filename), tb.tb_frame.f_code.co_name, name))
            tb = tb.tb_next
            idx += 1

    def walk(current: BaseException, owner_label: str, depth: int = 0) -> None:
        if current is None or id(current) in visited_ids or depth > 10:
            return
        visited_ids.add(id(current))
        scan_traceback(current.__traceback__, owner_label)
        walk(current.__context__, owner_label + ".__context__", depth + 1)
        walk(current.__cause__, owner_label + ".__cause__", depth + 1)

    walk(exc, "exc")
    return findings


def test_adopt_wrong_mapping_source_password_sanitized(tmp_path):
    src_mapping_path = tmp_path / "src" / "mapping.enc"
    _make_source_mapping(src_mapping_path, SOURCE_MAPPING_PASSWORD, (_mentry("C_AAAAAAAAA"),))

    with pytest.raises(WorkspaceBindingError) as excinfo:
        adopt_stores(
            tmp_path / "target", TARGET_PASSWORD,
            mapping_store_path=src_mapping_path, mapping_store_password="WRONG-" + SOURCE_MAPPING_PASSWORD,
        )
    assert excinfo.value.reason is WorkspaceBindingReason.STORE_UNOPENABLE
    assert _internal_frame_locals_containing(excinfo.value, (SOURCE_MAPPING_PASSWORD, TARGET_PASSWORD)) == []
    assert not (tmp_path / "target").exists() or not (tmp_path / "target" / "workspace.enc").exists()


def test_adopt_wrong_identifier_source_password_sanitized(tmp_path):
    src_mapping_path = tmp_path / "src" / "mapping.enc"
    src_identifier_path = tmp_path / "src" / "identifiers.enc"
    _make_source_mapping(src_mapping_path, SOURCE_MAPPING_PASSWORD, (_mentry("C_AAAAAAAAA"),))
    _make_source_identifier(src_identifier_path, SOURCE_IDENTIFIER_PASSWORD, (_ientry("INN_A0000000", "1"),))

    with pytest.raises(WorkspaceBindingError) as excinfo:
        adopt_stores(
            tmp_path / "target", TARGET_PASSWORD,
            mapping_store_path=src_mapping_path, mapping_store_password=SOURCE_MAPPING_PASSWORD,
            identifier_store_path=src_identifier_path,
            identifier_store_password="WRONG-" + SOURCE_IDENTIFIER_PASSWORD,
        )
    assert excinfo.value.reason is WorkspaceBindingReason.STORE_UNOPENABLE
    assert _internal_frame_locals_containing(excinfo.value, (SOURCE_IDENTIFIER_PASSWORD,)) == []
    # Ничего не должно было быть создано в target -- ошибка на этапе
    # чтения источников, ДО mkdir/lock/store.
    assert not (tmp_path / "target").exists()


# ---------------------------------------------------------------------------
# E. Верификация после записи + fail-closed
# ---------------------------------------------------------------------------


def test_adopt_verification_failure_leaves_no_workspace_enc(tmp_path, monkeypatch):
    src_mapping_path = tmp_path / "src" / "mapping.enc"
    _make_source_mapping(src_mapping_path, SOURCE_MAPPING_PASSWORD, (_mentry("C_AAAAAAAAA"),))

    import app.workspace.workspace as workspace_module

    original = workspace_module._sanitized_store_read

    call_count = {"n": 0}

    def _flaky_read(fn, *args, **kwargs):
        call_count["n"] += 1
        # Первый вызов реального entries() после записи в target -- это
        # верификационное чтение; подменяем его результат на "пустой",
        # симулируя провал верификации (§20).
        if fn.__name__ == "entries" and call_count["n"] >= 2:
            return ()
        return original(fn, *args, **kwargs)

    monkeypatch.setattr(workspace_module, "_sanitized_store_read", _flaky_read)

    target_root = tmp_path / "target"
    with pytest.raises(WorkspaceCorruptedError) as excinfo:
        adopt_stores(
            target_root, TARGET_PASSWORD,
            mapping_store_path=src_mapping_path, mapping_store_password=SOURCE_MAPPING_PASSWORD,
        )
    assert excinfo.value.reason is WorkspaceCorruptedReason.ADOPT_VERIFICATION_FAILED
    assert not (target_root / "workspace.enc").exists()


# ---------------------------------------------------------------------------
# E2. Correction Pass #2 (MINOR-1): равное count, но семантическая порча
#
# Независимый обзор указал: верификация ТОЛЬКО по len() пропустила бы
# ситуацию "количество совпадает, содержимое отличается". Тесты ниже
# доказывают, что ТЕКУЩАЯ (усиленная) верификация -- полная поэлементная
# entry-эквивалентность -- реально ловит именно такую порчу, инструментируя
# read-границу так, чтобы вернуть последовательность ТОЙ ЖЕ ДЛИНЫ, что и
# source, но с изменённым семантическим полем одной записи.
# ---------------------------------------------------------------------------


def test_adopt_equal_count_semantic_corruption_detected_for_mapping(tmp_path, monkeypatch):
    src_mapping_path = tmp_path / "src" / "mapping.enc"
    source_entries = (_mentry("C_AAAAAAAAA"), _mentry("C_BBBBBBBBB"))
    _make_source_mapping(src_mapping_path, SOURCE_MAPPING_PASSWORD, source_entries)

    import app.workspace.workspace as workspace_module

    original = workspace_module._sanitized_store_read
    call_count = {"n": 0}

    def _corrupting_read(fn, *args, **kwargs):
        call_count["n"] += 1
        # Вызов #2 entries() -- это переоткрытый target (верификационное
        # чтение); подменяем ОДНО поле первой записи, сохраняя ту же
        # длину последовательности, что и у source (len не изменился бы,
        # если бы верификация сравнивала только count).
        if fn.__name__ == "entries" and call_count["n"] == 2:
            real = original(fn, *args, **kwargs)
            if len(real) == len(source_entries) and real:
                corrupted_first = dataclasses.replace(real[0], real_value="CORRUPTED-SEMANTIC-VALUE")
                return (corrupted_first,) + tuple(real[1:])
            return real
        return original(fn, *args, **kwargs)

    monkeypatch.setattr(workspace_module, "_sanitized_store_read", _corrupting_read)

    target_root = tmp_path / "target"
    with pytest.raises(WorkspaceCorruptedError) as excinfo:
        adopt_stores(
            target_root, TARGET_PASSWORD,
            mapping_store_path=src_mapping_path, mapping_store_password=SOURCE_MAPPING_PASSWORD,
        )
    assert excinfo.value.reason is WorkspaceCorruptedReason.ADOPT_VERIFICATION_FAILED
    assert not (target_root / "workspace.enc").exists()
    # Конфиденциальность: подменённое/различающееся значение не должно
    # попасть в текст публичного исключения.
    assert "CORRUPTED-SEMANTIC-VALUE" not in str(excinfo.value)


def test_adopt_equal_count_semantic_corruption_detected_for_identifier(tmp_path, monkeypatch):
    src_mapping_path = tmp_path / "src" / "mapping.enc"
    src_identifier_path = tmp_path / "src" / "identifiers.enc"
    _make_source_mapping(src_mapping_path, SOURCE_MAPPING_PASSWORD, (_mentry("C_AAAAAAAAA"),))
    source_identifier_entries = (_ientry("INN_A0000000", "1"), _ientry("INN_B0000000", "2"))
    _make_source_identifier(src_identifier_path, SOURCE_IDENTIFIER_PASSWORD, source_identifier_entries)

    import app.workspace.workspace as workspace_module

    original = workspace_module._sanitized_store_read
    call_count = {"n": 0}

    def _corrupting_read(fn, *args, **kwargs):
        call_count["n"] += 1
        # Порядок entries()-вызовов при mapping+identifier adopt: 1=source
        # mapping, 2=source identifier, 3=reopened mapping (не трогаем),
        # 4=reopened identifier -- именно его портим.
        if fn.__name__ == "entries" and call_count["n"] == 4:
            real = original(fn, *args, **kwargs)
            if len(real) == len(source_identifier_entries) and real:
                corrupted_first = dataclasses.replace(real[0], identifier_value="9999999999")
                return (corrupted_first,) + tuple(real[1:])
            return real
        return original(fn, *args, **kwargs)

    monkeypatch.setattr(workspace_module, "_sanitized_store_read", _corrupting_read)

    target_root = tmp_path / "target"
    with pytest.raises(WorkspaceCorruptedError) as excinfo:
        adopt_stores(
            target_root, TARGET_PASSWORD,
            mapping_store_path=src_mapping_path, mapping_store_password=SOURCE_MAPPING_PASSWORD,
            identifier_store_path=src_identifier_path, identifier_store_password=SOURCE_IDENTIFIER_PASSWORD,
        )
    assert excinfo.value.reason is WorkspaceCorruptedReason.ADOPT_VERIFICATION_FAILED
    assert not (target_root / "workspace.enc").exists()
    assert "9999999999" not in str(excinfo.value)


def test_adopt_verification_would_not_be_caught_by_count_only(tmp_path):
    """
    Прямая демонстрация того, ПОЧЕМУ count-only была недостаточной: две
    РАЗНЫЕ по содержимому последовательности одинаковой длины дают
    isinstance(..., tuple) с равным len(), но НЕ равны как tuple, и дают
    разный digest -- то есть старая проверка (len only) пропустила бы то,
    что новая (entry equality + digest) ловит.
    """
    a = (_mentry("C_AAAAAAAAA", real_value="R1"),)
    b = (_mentry("C_AAAAAAAAA", real_value="R2"),)
    assert len(a) == len(b)
    assert a != b
    from app.workspace.manifest import mapping_entries_prefix_digest

    assert mapping_entries_prefix_digest(a) != mapping_entries_prefix_digest(b) or a != b


# ---------------------------------------------------------------------------
# F. Clean-skeleton retry
# ---------------------------------------------------------------------------


def test_adopt_retries_on_clean_skeleton(tmp_path):
    src_mapping_path = tmp_path / "src" / "mapping.enc"
    _make_source_mapping(src_mapping_path, SOURCE_MAPPING_PASSWORD, (_mentry("C_AAAAAAAAA"),))

    target_root = tmp_path / "target"
    target_root.mkdir()
    (target_root / "workspace.lock").touch()

    ws = adopt_stores(
        target_root, TARGET_PASSWORD,
        mapping_store_path=src_mapping_path, mapping_store_password=SOURCE_MAPPING_PASSWORD,
    )
    assert ws.info.mapping_entry_count == 1


def test_adopt_rejects_unsafe_existing_target(tmp_path):
    src_mapping_path = tmp_path / "src" / "mapping.enc"
    _make_source_mapping(src_mapping_path, SOURCE_MAPPING_PASSWORD, (_mentry("C_AAAAAAAAA"),))

    target_root = tmp_path / "target"
    target_root.mkdir()
    (target_root / "workspace.enc").write_bytes(b"existing")

    with pytest.raises(WorkspaceCorruptedError) as excinfo:
        adopt_stores(
            target_root, TARGET_PASSWORD,
            mapping_store_path=src_mapping_path, mapping_store_password=SOURCE_MAPPING_PASSWORD,
        )
    assert excinfo.value.reason is WorkspaceCorruptedReason.UNSAFE_LAYOUT


# ---------------------------------------------------------------------------
# G. Политика паролей
# ---------------------------------------------------------------------------


def test_adopt_target_password_policy_od1(tmp_path):
    src_mapping_path = tmp_path / "src" / "mapping.enc"
    _make_source_mapping(src_mapping_path, SOURCE_MAPPING_PASSWORD, (_mentry("C_AAAAAAAAA"),))

    with pytest.raises(WorkspaceInputError):
        adopt_stores(
            tmp_path / "target", "short1",
            mapping_store_path=src_mapping_path, mapping_store_password=SOURCE_MAPPING_PASSWORD,
        )
    assert not (tmp_path / "target").exists()


def test_adopt_source_password_must_be_nonempty_str(tmp_path):
    src_mapping_path = tmp_path / "src" / "mapping.enc"
    _make_source_mapping(src_mapping_path, SOURCE_MAPPING_PASSWORD, (_mentry("C_AAAAAAAAA"),))

    with pytest.raises(WorkspaceInputError):
        adopt_stores(
            tmp_path / "target", TARGET_PASSWORD,
            mapping_store_path=src_mapping_path, mapping_store_password="",
        )


# ---------------------------------------------------------------------------
# H. Correction Pass #2 (BLOCKER-1): детерминированные TOCTOU-гонки
#
# Тот же harness, что и в tests/test_workspace_lifecycle.py (продублирован
# здесь намеренно -- отдельный shared-хелпер вне разрешённого набора
# файлов не заводился). _ensure_clean_skeleton_or_absent вызывается
# ДВАЖДЫ (pre-lock/post-lock) и в create_workspace, и в adopt_stores,
# поэтому один и тот же harness покрывает все комбинации победителя и
# проигравшего между двумя функциями.
# ---------------------------------------------------------------------------

_RACE_WAIT_TIMEOUT = 10.0


def _force_stale_pre_lock_race(monkeypatch, loser_thread_name: str):
    import app.workspace.workspace as workspace_module

    real_check = workspace_module._ensure_clean_skeleton_or_absent
    loser_is_waiting = threading.Event()
    release_loser = threading.Event()
    call_counts: dict = {}
    counts_lock = threading.Lock()

    def _patched(root_path):
        real_check(root_path)
        if threading.current_thread().name == loser_thread_name:
            with counts_lock:
                call_counts[loser_thread_name] = call_counts.get(loser_thread_name, 0) + 1
                is_first_call = call_counts[loser_thread_name] == 1
            if is_first_call:
                loser_is_waiting.set()
                completed = release_loser.wait(timeout=_RACE_WAIT_TIMEOUT)
                if not completed:
                    raise AssertionError(
                        "release_loser не был выставлен вовремя -- тест завис бы без таймаута"
                    )

    monkeypatch.setattr(workspace_module, "_ensure_clean_skeleton_or_absent", _patched)
    return loser_is_waiting, release_loser


def _wait_ok(event: threading.Event) -> bool:
    return event.wait(timeout=_RACE_WAIT_TIMEOUT)


def test_create_vs_adopt_race_create_wins(tmp_path, monkeypatch):
    """A = create_workspace (побеждает), B = adopt_stores (проигрывает)."""
    root = tmp_path / "ws"
    src_mapping_path = tmp_path / "src" / "mapping.enc"
    source_entries = (_mentry("C_LOSERADOPT"),)
    source_store = _make_source_mapping(src_mapping_path, SOURCE_MAPPING_PASSWORD, source_entries)

    loser_waiting, release_loser = _force_stale_pre_lock_race(monkeypatch, "contender-B-adopt")

    results: dict = {}

    def run_b():
        try:
            adopt_stores(
                root, "password-contender-B-1",
                mapping_store_path=src_mapping_path, mapping_store_password=SOURCE_MAPPING_PASSWORD,
                label="loser-B-adopt",
            )
            results["b_error"] = None
        except Exception as exc:  # noqa: BLE001
            results["b_error"] = exc

    thread_b = threading.Thread(target=run_b, name="contender-B-adopt")
    thread_b.start()
    assert _wait_ok(loser_waiting)

    ws_a = create_workspace(root, "password-contender-A-1", label="winner-A-create")

    release_loser.set()
    thread_b.join(timeout=_RACE_WAIT_TIMEOUT)
    assert not thread_b.is_alive()

    assert isinstance(results.get("b_error"), WorkspaceCorruptedError)
    assert results["b_error"].reason is WorkspaceCorruptedReason.UNSAFE_LAYOUT

    final = open_workspace(root, "password-contender-A-1")
    assert final.info.workspace_id == ws_a.info.workspace_id
    assert final.info.label == "winner-A-create"
    assert final.info.mapping_entry_count == 0  # create_workspace, не adoption

    # Источник проигравшего B не тронут.
    assert source_store.entries() == source_entries


def test_adopt_vs_create_race_adopt_wins(tmp_path, monkeypatch):
    """A = adopt_stores (побеждает), B = create_workspace (проигрывает)."""
    root = tmp_path / "ws"
    src_mapping_path = tmp_path / "src" / "mapping.enc"
    source_entries = (_mentry("C_WINNERADOPT"),)
    _make_source_mapping(src_mapping_path, SOURCE_MAPPING_PASSWORD, source_entries)

    loser_waiting, release_loser = _force_stale_pre_lock_race(monkeypatch, "contender-B-create")

    results: dict = {}

    def run_b():
        try:
            create_workspace(root, "password-contender-B-1", label="loser-B-create")
            results["b_error"] = None
        except Exception as exc:  # noqa: BLE001
            results["b_error"] = exc

    thread_b = threading.Thread(target=run_b, name="contender-B-create")
    thread_b.start()
    assert _wait_ok(loser_waiting)

    ws_a = adopt_stores(
        root, "password-contender-A-1",
        mapping_store_path=src_mapping_path, mapping_store_password=SOURCE_MAPPING_PASSWORD,
        label="winner-A-adopt",
    )

    release_loser.set()
    thread_b.join(timeout=_RACE_WAIT_TIMEOUT)
    assert not thread_b.is_alive()

    assert isinstance(results.get("b_error"), WorkspaceCorruptedError)
    assert results["b_error"].reason is WorkspaceCorruptedReason.UNSAFE_LAYOUT

    final = open_workspace(root, "password-contender-A-1")
    assert final.info.workspace_id == ws_a.info.workspace_id
    assert final.info.label == "winner-A-adopt"
    assert final.info.mapping_entry_count == 1
    mreader, _ = final.read_stores()
    assert mreader.entries() == source_entries


def test_adopt_vs_adopt_race_exactly_one_winner(tmp_path, monkeypatch):
    """A и B -- оба adopt_stores, разные источники/метки; ровно один побеждает."""
    root = tmp_path / "ws"
    src_a_path = tmp_path / "src_a" / "mapping.enc"
    src_b_path = tmp_path / "src_b" / "mapping.enc"
    entries_a = (_mentry("C_SOURCEA01"),)
    entries_b = (_mentry("C_SOURCEB01"),)
    store_a = _make_source_mapping(src_a_path, "src-a-password-1", entries_a)
    store_b = _make_source_mapping(src_b_path, "src-b-password-1", entries_b)

    loser_waiting, release_loser = _force_stale_pre_lock_race(monkeypatch, "contender-B-adopt2")

    results: dict = {}

    def run_b():
        try:
            adopt_stores(
                root, "password-contender-B-1",
                mapping_store_path=src_b_path, mapping_store_password="src-b-password-1",
                label="loser-B-adopt2",
            )
            results["b_error"] = None
        except Exception as exc:  # noqa: BLE001
            results["b_error"] = exc

    thread_b = threading.Thread(target=run_b, name="contender-B-adopt2")
    thread_b.start()
    assert _wait_ok(loser_waiting)

    ws_a = adopt_stores(
        root, "password-contender-A-1",
        mapping_store_path=src_a_path, mapping_store_password="src-a-password-1",
        label="winner-A-adopt2",
    )

    release_loser.set()
    thread_b.join(timeout=_RACE_WAIT_TIMEOUT)
    assert not thread_b.is_alive()

    assert isinstance(results.get("b_error"), WorkspaceCorruptedError)
    assert results["b_error"].reason is WorkspaceCorruptedReason.UNSAFE_LAYOUT

    final = open_workspace(root, "password-contender-A-1")
    assert final.info.workspace_id == ws_a.info.workspace_id
    assert final.info.label == "winner-A-adopt2"
    mreader, _ = final.read_stores()
    assert mreader.entries() == entries_a  # НЕ entries_b -- проигравший не подмешался

    # Оба источника остались нетронутыми.
    assert store_a.entries() == entries_a
    assert store_b.entries() == entries_b
