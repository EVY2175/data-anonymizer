"""
Тесты Stage 10B.2: app.workspace.storage (load/save encrypted manifest).

Группы:
    A. Round-trip и базовый контракт
    B. Password contract
    C. Error mapping (missing / auth / container / payload / version)
    D. Size gates (encrypted и plaintext)
    E. NOTE-1: конфиденциальность цепочки исключений
    F. Атомарная запись / failure injection
    G. No plaintext temp / same-directory temp / temp cleanup
    H. Correction Pass MAJOR-1: password/plaintext через exc.__traceback__
    I. Correction Pass MINOR-1: TOCTOU bounded read
    L. Correction Pass #2: load_encrypted_manifest — валидация до try/finally
    SAV. Correction Pass #2: save_encrypted_manifest_atomic конфиденциальность
"""

from __future__ import annotations

import json
import os
import traceback
from pathlib import Path

import pytest

from app.security import crypto
from app.workspace import storage as storage_module
from app.workspace.errors import (
    WorkspaceAuthenticationError,
    WorkspaceCorruptedError,
    WorkspaceCorruptedReason,
    WorkspaceInputError,
    WorkspaceNotFoundError,
)
from app.workspace.manifest import (
    EMPTY_IDENTIFIER_STATE,
    EMPTY_MAPPING_STATE,
    parse_manifest,
    serialize_manifest,
)
from app.workspace.models import WorkspaceManifest
from app.workspace.storage import (
    MAX_ENCRYPTED_MANIFEST_BYTES,
    MAX_PLAINTEXT_MANIFEST_BYTES,
    load_encrypted_manifest,
    save_encrypted_manifest_atomic,
)

PASSWORD = "test-workspace-password-123"
TS = "2026-09-21T12:00:00Z"
# Синтетические sentinel'ы — никогда не путать с реальными данными клиентов.
PLAINTEXT_SENTINEL = "SENTINEL_STORAGE_PLAINTEXT_9F31"
PASSWORD_SENTINEL = "SENTINEL_STORAGE_PASSWORD_7C42"


@pytest.fixture()
def target_path(tmp_path: Path) -> Path:
    return tmp_path / "workspace.enc"


def _manifest(label: object = None, revision: int = 1) -> WorkspaceManifest:
    return WorkspaceManifest(
        schema_version=1,
        workspace_id="a" * 32,
        label=label,
        created_at=TS,
        revision=revision,
        pending_store_mutation=False,
        mapping_state=EMPTY_MAPPING_STATE,
        identifier_state=EMPTY_IDENTIFIER_STATE,
        artifacts=(),
        latest_analytical_artifact_id=None,
    )


def _write_encrypted(path: Path, plaintext: bytes, password: str = PASSWORD) -> None:
    """Собирает payload только в памяти, шифрует и лишь тогда пишет на диск."""
    path.write_bytes(crypto.encrypt_bytes(plaintext, password))


# ---------------------------------------------------------------------------
# A. Round-trip и базовый контракт
# ---------------------------------------------------------------------------


def test_round_trip_recovers_exact_canonical_manifest(target_path: Path) -> None:
    manifest = _manifest(label="Отчёт за январь")
    save_encrypted_manifest_atomic(target_path, manifest, PASSWORD)

    loaded = load_encrypted_manifest(target_path, PASSWORD)

    assert loaded == manifest
    assert serialize_manifest(loaded) == serialize_manifest(manifest)


def test_committed_bytes_decrypt_and_parse_correctly(target_path: Path) -> None:
    manifest = _manifest(label=None, revision=7)
    save_encrypted_manifest_atomic(target_path, manifest, PASSWORD)

    raw = target_path.read_bytes()
    plaintext = crypto.decrypt_bytes(raw, PASSWORD)
    assert plaintext == serialize_manifest(manifest)
    assert parse_manifest(plaintext) == manifest


def test_load_rejects_non_path_type() -> None:
    with pytest.raises(TypeError):
        load_encrypted_manifest(12345, PASSWORD)  # type: ignore[arg-type]


def test_save_rejects_non_manifest_type(target_path: Path) -> None:
    with pytest.raises(TypeError):
        save_encrypted_manifest_atomic(target_path, {"not": "a manifest"}, PASSWORD)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# B. Password contract
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("bad_password", [None, 123, 1.5, b"bytes-not-str"])
def test_load_rejects_non_str_password(target_path: Path, bad_password: object) -> None:
    save_encrypted_manifest_atomic(target_path, _manifest(), PASSWORD)
    with pytest.raises(WorkspaceInputError):
        load_encrypted_manifest(target_path, bad_password)  # type: ignore[arg-type]


def test_load_rejects_empty_password(target_path: Path) -> None:
    save_encrypted_manifest_atomic(target_path, _manifest(), PASSWORD)
    with pytest.raises(WorkspaceInputError):
        load_encrypted_manifest(target_path, "")


@pytest.mark.parametrize("bad_password", [None, 123, ""])
def test_save_rejects_invalid_password(target_path: Path, bad_password: object) -> None:
    with pytest.raises(WorkspaceInputError):
        save_encrypted_manifest_atomic(target_path, _manifest(), bad_password)  # type: ignore[arg-type]
    assert not target_path.exists()


def test_password_not_normalized_different_forms_are_different_keys(target_path: Path) -> None:
    # Та же политика, что и app.security.crypto: "password", " password" и
    # "PASSWORD" не эквивалентны, нормализация не производится.
    save_encrypted_manifest_atomic(target_path, _manifest(), "password")
    with pytest.raises(WorkspaceAuthenticationError):
        load_encrypted_manifest(target_path, " password")
    with pytest.raises(WorkspaceAuthenticationError):
        load_encrypted_manifest(target_path, "PASSWORD")
    assert load_encrypted_manifest(target_path, "password") == _manifest()


def test_password_never_appears_in_exception_text(target_path: Path) -> None:
    secret_password = "SUPER_SECRET_PW_MUST_NOT_LEAK"
    wrong_password = "ANOTHER_SECRET_WRONG_PW_VALUE"
    save_encrypted_manifest_atomic(target_path, _manifest(), secret_password)
    try:
        load_encrypted_manifest(target_path, wrong_password)
    except WorkspaceAuthenticationError as exc:
        # Проверяем текст самого исключения (str/repr) и "итоговую строку"
        # форматирования (без исходного кода фреймов — format_exception_only
        # не печатает текст строк вызова, поэтому не даёт ложных
        # срабатываний из-за литералов пароля в коде самого теста).
        summary = "".join(traceback.format_exception_only(type(exc), exc))
        rendered = str(exc) + repr(exc) + summary
        assert secret_password not in rendered
        assert wrong_password not in rendered


# ---------------------------------------------------------------------------
# C. Error mapping
# ---------------------------------------------------------------------------


def test_missing_file_raises_not_found(target_path: Path) -> None:
    with pytest.raises(WorkspaceNotFoundError):
        load_encrypted_manifest(target_path, PASSWORD)


def test_wrong_password_raises_authentication_error(target_path: Path) -> None:
    save_encrypted_manifest_atomic(target_path, _manifest(), PASSWORD)
    with pytest.raises(WorkspaceAuthenticationError):
        load_encrypted_manifest(target_path, "definitely-wrong")


def test_tampered_ciphertext_raises_authentication_error(target_path: Path) -> None:
    save_encrypted_manifest_atomic(target_path, _manifest(), PASSWORD)
    raw = bytearray(target_path.read_bytes())
    raw[-1] ^= 0xFF  # флип последнего байта payload (внутри GCM-тега)
    target_path.write_bytes(bytes(raw))
    with pytest.raises(WorkspaceAuthenticationError):
        load_encrypted_manifest(target_path, PASSWORD)


def test_bad_magic_raises_invalid_container(target_path: Path) -> None:
    save_encrypted_manifest_atomic(target_path, _manifest(), PASSWORD)
    raw = target_path.read_bytes()
    target_path.write_bytes(b"XXXX" + raw[4:])
    with pytest.raises(WorkspaceCorruptedError) as excinfo:
        load_encrypted_manifest(target_path, PASSWORD)
    assert excinfo.value.reason is WorkspaceCorruptedReason.MANIFEST_INVALID_CONTAINER


@pytest.mark.parametrize("cutoff", [0, 1, 4, 5, 20, 39])
def test_truncated_container_raises_invalid_container(target_path: Path, cutoff: int) -> None:
    save_encrypted_manifest_atomic(target_path, _manifest(), PASSWORD)
    raw = target_path.read_bytes()
    target_path.write_bytes(raw[:cutoff])
    with pytest.raises(WorkspaceCorruptedError) as excinfo:
        load_encrypted_manifest(target_path, PASSWORD)
    assert excinfo.value.reason is WorkspaceCorruptedReason.MANIFEST_INVALID_CONTAINER


def test_unsupported_container_version_raises_invalid_container(target_path: Path) -> None:
    save_encrypted_manifest_atomic(target_path, _manifest(), PASSWORD)
    raw = bytearray(target_path.read_bytes())
    raw[4] = 99  # смещение version в header'е crypto-контейнера
    target_path.write_bytes(bytes(raw))
    with pytest.raises(WorkspaceCorruptedError) as excinfo:
        load_encrypted_manifest(target_path, PASSWORD)
    assert excinfo.value.reason is WorkspaceCorruptedReason.MANIFEST_INVALID_CONTAINER


def test_unsupported_kdf_id_raises_invalid_container(target_path: Path) -> None:
    save_encrypted_manifest_atomic(target_path, _manifest(), PASSWORD)
    raw = bytearray(target_path.read_bytes())
    raw[5] = 77  # смещение kdf_id
    target_path.write_bytes(bytes(raw))
    with pytest.raises(WorkspaceCorruptedError) as excinfo:
        load_encrypted_manifest(target_path, PASSWORD)
    assert excinfo.value.reason is WorkspaceCorruptedReason.MANIFEST_INVALID_CONTAINER


def test_invalid_kdf_parameters_raises_invalid_container(target_path: Path) -> None:
    save_encrypted_manifest_atomic(target_path, _manifest(), PASSWORD)
    raw = bytearray(target_path.read_bytes())
    # Поле n (uint32 BE) со смещения 6 — выставляем заведомо вне границ
    # _MAX_N крипто-слоя, не трогая аутентификацию (обнаруживается до GCM).
    raw[6:10] = (2**31).to_bytes(4, "big")
    target_path.write_bytes(bytes(raw))
    with pytest.raises(WorkspaceCorruptedError) as excinfo:
        load_encrypted_manifest(target_path, PASSWORD)
    assert excinfo.value.reason is WorkspaceCorruptedReason.MANIFEST_INVALID_CONTAINER


def test_successful_decrypt_invalid_utf8_raises_invalid_payload(target_path: Path) -> None:
    _write_encrypted(target_path, b"\xff\xfe\xfd not valid utf-8")
    with pytest.raises(WorkspaceCorruptedError) as excinfo:
        load_encrypted_manifest(target_path, PASSWORD)
    assert excinfo.value.reason is WorkspaceCorruptedReason.MANIFEST_INVALID_PAYLOAD


def test_successful_decrypt_malformed_json_raises_invalid_payload(target_path: Path) -> None:
    _write_encrypted(target_path, b"{not valid json")
    with pytest.raises(WorkspaceCorruptedError) as excinfo:
        load_encrypted_manifest(target_path, PASSWORD)
    assert excinfo.value.reason is WorkspaceCorruptedReason.MANIFEST_INVALID_PAYLOAD


def test_successful_decrypt_unsupported_manifest_schema_raises_unsupported_version(
    target_path: Path,
) -> None:
    save_encrypted_manifest_atomic(target_path, _manifest(), PASSWORD)
    payload = json.loads(crypto.decrypt_bytes(target_path.read_bytes(), PASSWORD))
    payload["schema_version"] = 2
    new_plaintext = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    _write_encrypted(target_path, new_plaintext)
    with pytest.raises(WorkspaceCorruptedError) as excinfo:
        load_encrypted_manifest(target_path, PASSWORD)
    assert excinfo.value.reason is WorkspaceCorruptedReason.MANIFEST_UNSUPPORTED_VERSION


def test_foreign_encrypted_container_type_is_rejected_by_strict_schema(target_path: Path) -> None:
    # OD-2: валидный зашифрованный контейнер ДРУГОГО типа приложения
    # успешно расшифровывается под тем же паролем (крипто-слой не знает
    # о типе содержимого), но строгая схема manifest его отклоняет.
    foreign_payload = json.dumps(
        {"schema_version": 1, "entries": []}, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    _write_encrypted(target_path, foreign_payload)
    with pytest.raises(WorkspaceCorruptedError) as excinfo:
        load_encrypted_manifest(target_path, PASSWORD)
    assert excinfo.value.reason is WorkspaceCorruptedReason.MANIFEST_INVALID_PAYLOAD


# ---------------------------------------------------------------------------
# D. Size gates
# ---------------------------------------------------------------------------


def test_oversized_encrypted_container_rejected_without_calling_decrypt(
    target_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with open(target_path, "wb") as f:
        f.seek(MAX_ENCRYPTED_MANIFEST_BYTES + 1)
        f.write(b"\0")

    called = {"n": 0}
    real_decrypt = storage_module.decrypt_bytes

    def spy_decrypt(*args: object, **kwargs: object) -> bytes:
        called["n"] += 1
        return real_decrypt(*args, **kwargs)  # pragma: no cover - не должен вызываться

    monkeypatch.setattr(storage_module, "decrypt_bytes", spy_decrypt)

    with pytest.raises(WorkspaceCorruptedError) as excinfo:
        load_encrypted_manifest(target_path, PASSWORD)

    assert excinfo.value.reason is WorkspaceCorruptedReason.MANIFEST_INVALID_CONTAINER
    assert called["n"] == 0


def test_oversized_plaintext_rejected_without_calling_parse_manifest(
    target_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    huge_plaintext = b"{" + b"a" * (MAX_PLAINTEXT_MANIFEST_BYTES + 16)
    _write_encrypted(target_path, huge_plaintext)

    called = {"n": 0}
    real_parse = storage_module.parse_manifest

    def spy_parse(*args: object, **kwargs: object) -> WorkspaceManifest:
        called["n"] += 1
        return real_parse(*args, **kwargs)  # pragma: no cover - не должен вызываться

    monkeypatch.setattr(storage_module, "parse_manifest", spy_parse)

    with pytest.raises(WorkspaceCorruptedError) as excinfo:
        load_encrypted_manifest(target_path, PASSWORD)

    assert excinfo.value.reason is WorkspaceCorruptedReason.MANIFEST_INVALID_PAYLOAD
    assert called["n"] == 0


def test_size_at_exact_boundary_is_accepted(target_path: Path) -> None:
    # Ровно на границе (не превышает) должен приниматься штатным путём.
    manifest = _manifest(label="ровно на границе не проверяем плотно — это smoke")
    save_encrypted_manifest_atomic(target_path, manifest, PASSWORD)
    assert target_path.stat().st_size <= MAX_ENCRYPTED_MANIFEST_BYTES
    assert load_encrypted_manifest(target_path, PASSWORD) == manifest


# ---------------------------------------------------------------------------
# E. NOTE-1: конфиденциальность цепочки исключений
# ---------------------------------------------------------------------------


def _all_exception_chain_text(exc: BaseException) -> str:
    parts = [str(exc), repr(exc)]
    parts.append("".join(traceback.format_exception(exc)))
    node = exc.__context__
    seen = set()
    while node is not None and id(node) not in seen:
        seen.add(id(node))
        parts.append(str(node))
        parts.append(repr(node))
        node = getattr(node, "__context__", None)
    if exc.__cause__ is not None:
        parts.append(str(exc.__cause__))
        parts.append(repr(exc.__cause__))
    return "\n".join(parts)


def test_malformed_json_does_not_leak_plaintext_via_exception_chain(target_path: Path) -> None:
    malformed = ('{"unterminated": "' + PLAINTEXT_SENTINEL).encode("utf-8")
    _write_encrypted(target_path, malformed)

    with pytest.raises(WorkspaceCorruptedError) as excinfo:
        load_encrypted_manifest(target_path, PASSWORD)

    exc = excinfo.value
    assert exc.__context__ is None
    assert exc.__cause__ is None
    rendered = _all_exception_chain_text(exc)
    assert PLAINTEXT_SENTINEL not in rendered


def test_invalid_utf8_does_not_leak_plaintext_via_exception_chain(target_path: Path) -> None:
    # UnicodeDecodeError.object может содержать полный исходный buffer —
    # намеренно кладём sentinel рядом с невалидным байтом.
    malformed = PLAINTEXT_SENTINEL.encode("utf-8") + b"\xff\xfe"
    _write_encrypted(target_path, malformed)

    with pytest.raises(WorkspaceCorruptedError) as excinfo:
        load_encrypted_manifest(target_path, PASSWORD)

    exc = excinfo.value
    assert exc.__context__ is None
    assert exc.__cause__ is None
    rendered = _all_exception_chain_text(exc)
    assert PLAINTEXT_SENTINEL not in rendered


def test_wrong_password_exception_has_no_context_or_cause(target_path: Path) -> None:
    save_encrypted_manifest_atomic(target_path, _manifest(), PASSWORD)
    with pytest.raises(WorkspaceAuthenticationError) as excinfo:
        load_encrypted_manifest(target_path, "wrong")
    assert excinfo.value.__context__ is None
    assert excinfo.value.__cause__ is None


def test_invalid_container_exception_has_no_context_or_cause(target_path: Path) -> None:
    save_encrypted_manifest_atomic(target_path, _manifest(), PASSWORD)
    target_path.write_bytes(b"short")
    with pytest.raises(WorkspaceCorruptedError) as excinfo:
        load_encrypted_manifest(target_path, PASSWORD)
    assert excinfo.value.__context__ is None
    assert excinfo.value.__cause__ is None


def test_unsupported_schema_exception_does_not_leak_sentinel_label(target_path: Path) -> None:
    manifest = _manifest(label=PLAINTEXT_SENTINEL)
    save_encrypted_manifest_atomic(target_path, manifest, PASSWORD)
    payload = json.loads(crypto.decrypt_bytes(target_path.read_bytes(), PASSWORD))
    payload["schema_version"] = 2
    new_plaintext = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    _write_encrypted(target_path, new_plaintext)

    with pytest.raises(WorkspaceCorruptedError) as excinfo:
        load_encrypted_manifest(target_path, PASSWORD)

    exc = excinfo.value
    assert exc.__context__ is None
    assert exc.__cause__ is None
    rendered = _all_exception_chain_text(exc)
    assert PLAINTEXT_SENTINEL not in rendered


# ---------------------------------------------------------------------------
# F. Атомарная запись / failure injection
# ---------------------------------------------------------------------------


def test_atomic_replace_fully_overwrites_previous_content(target_path: Path) -> None:
    first = _manifest(revision=1)
    second = _manifest(revision=2, label="new")
    save_encrypted_manifest_atomic(target_path, first, PASSWORD)
    save_encrypted_manifest_atomic(target_path, second, PASSWORD)
    assert load_encrypted_manifest(target_path, PASSWORD) == second


def test_failed_mkstemp_preserves_existing_file(
    target_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = _manifest(revision=1)
    save_encrypted_manifest_atomic(target_path, original, PASSWORD)
    original_bytes = target_path.read_bytes()

    def failing_mkstemp(*args: object, **kwargs: object):
        raise OSError("simulated mkstemp failure")

    monkeypatch.setattr(storage_module.tempfile, "mkstemp", failing_mkstemp)

    with pytest.raises(OSError):
        save_encrypted_manifest_atomic(target_path, _manifest(revision=2), PASSWORD)

    monkeypatch.undo()
    assert target_path.read_bytes() == original_bytes
    leftover = [p for p in target_path.parent.iterdir() if p != target_path]
    assert leftover == []


def test_failed_write_preserves_existing_file_and_removes_temp(
    target_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = _manifest(revision=1)
    save_encrypted_manifest_atomic(target_path, original, PASSWORD)
    original_bytes = target_path.read_bytes()

    original_fdopen = os.fdopen

    def failing_fdopen(fd: int, mode: str = "r", *args: object, **kwargs: object):
        raw = original_fdopen(fd, mode, *args, **kwargs)

        class _FailingWriter:
            def __enter__(self) -> "_FailingWriter":
                return self

            def __exit__(self, *exc_info: object) -> None:
                raw.close()

            def write(self, data: bytes) -> int:
                raise OSError("simulated write failure")

            def fileno(self) -> int:
                return raw.fileno()

            def flush(self) -> None:
                raw.flush()

        return _FailingWriter()

    monkeypatch.setattr(storage_module.os, "fdopen", failing_fdopen)

    with pytest.raises(OSError):
        save_encrypted_manifest_atomic(target_path, _manifest(revision=2), PASSWORD)

    monkeypatch.undo()
    assert target_path.read_bytes() == original_bytes
    leftover = [p for p in target_path.parent.iterdir() if p != target_path]
    assert leftover == []


def test_failed_fsync_preserves_existing_file_and_removes_temp(
    target_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = _manifest(revision=1)
    save_encrypted_manifest_atomic(target_path, original, PASSWORD)
    original_bytes = target_path.read_bytes()

    def failing_fsync(fd: int) -> None:
        raise OSError("simulated fsync failure")

    monkeypatch.setattr(storage_module.os, "fsync", failing_fsync)

    with pytest.raises(OSError):
        save_encrypted_manifest_atomic(target_path, _manifest(revision=2), PASSWORD)

    monkeypatch.undo()
    assert target_path.read_bytes() == original_bytes
    leftover = [p for p in target_path.parent.iterdir() if p != target_path]
    assert leftover == []


def test_failed_os_replace_preserves_existing_file(
    target_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = _manifest(revision=1)
    save_encrypted_manifest_atomic(target_path, original, PASSWORD)
    original_bytes = target_path.read_bytes()

    def failing_replace(*args: object, **kwargs: object) -> None:
        raise OSError("simulated os.replace failure")

    monkeypatch.setattr(storage_module.os, "replace", failing_replace)

    with pytest.raises(OSError):
        save_encrypted_manifest_atomic(target_path, _manifest(revision=2), PASSWORD)

    monkeypatch.undo()
    assert target_path.read_bytes() == original_bytes
    assert load_encrypted_manifest(target_path, PASSWORD) == original

    leftover = [p for p in target_path.parent.iterdir() if p != target_path]
    assert leftover == []


def test_cleanup_failure_does_not_mask_primary_exception(
    target_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = _manifest(revision=1)
    save_encrypted_manifest_atomic(target_path, original, PASSWORD)
    original_bytes = target_path.read_bytes()

    def failing_replace(*args: object, **kwargs: object) -> None:
        raise OSError("primary failure: simulated os.replace failure")

    monkeypatch.setattr(storage_module.os, "replace", failing_replace)

    original_unlink = Path.unlink

    def failing_unlink(self: Path, *args: object, **kwargs: object) -> None:
        raise OSError("secondary failure: simulated cleanup failure")

    monkeypatch.setattr(Path, "unlink", failing_unlink)

    with pytest.raises(OSError, match="primary failure"):
        save_encrypted_manifest_atomic(target_path, _manifest(revision=2), PASSWORD)

    monkeypatch.setattr(Path, "unlink", original_unlink)
    monkeypatch.undo()

    assert target_path.read_bytes() == original_bytes
    # temp-файл остаётся сиротой, т.к. его удаление тоже было заблокировано
    # — это принятая (не маскирующая первичную ошибку) деградация.
    leftover = [p for p in target_path.parent.iterdir() if p != target_path]
    for p in leftover:
        p.unlink()


def test_pre_replace_failure_leaves_no_target_when_creating_new_workspace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    new_target = tmp_path / "brand_new_workspace.enc"

    def failing_replace(*args: object, **kwargs: object) -> None:
        raise OSError("simulated os.replace failure")

    monkeypatch.setattr(storage_module.os, "replace", failing_replace)

    with pytest.raises(OSError):
        save_encrypted_manifest_atomic(new_target, _manifest(), PASSWORD)

    monkeypatch.undo()
    assert not new_target.exists()
    leftover = list(tmp_path.iterdir())
    assert leftover == []


# ---------------------------------------------------------------------------
# G. No plaintext temp / same-directory temp / temp cleanup
# ---------------------------------------------------------------------------


def test_temp_file_holds_ciphertext_only_same_directory(
    target_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: dict[str, bytes] = {}
    captured_dirs: dict[str, str] = {}
    real_replace = os.replace
    real_mkstemp = storage_module.tempfile.mkstemp

    def spy_mkstemp(*args: object, **kwargs: object):
        result = real_mkstemp(*args, **kwargs)
        captured_dirs["dir"] = kwargs.get("dir", "")
        return result

    def spy_replace(src: str, dst: str) -> None:
        captured["temp_bytes"] = Path(src).read_bytes()
        real_replace(src, dst)

    monkeypatch.setattr(storage_module.tempfile, "mkstemp", spy_mkstemp)
    monkeypatch.setattr(storage_module.os, "replace", spy_replace)

    manifest = _manifest(label=PLAINTEXT_SENTINEL)
    save_encrypted_manifest_atomic(target_path, manifest, PASSWORD)

    temp_bytes = captured["temp_bytes"]
    assert temp_bytes[:4] == crypto.MAGIC
    assert PLAINTEXT_SENTINEL.encode("utf-8") not in temp_bytes
    assert b"workspace_manifest" not in temp_bytes
    assert b"schema_version" not in temp_bytes
    assert captured_dirs["dir"] == str(target_path.parent)

    decrypted = crypto.decrypt_bytes(temp_bytes, PASSWORD)
    assert PLAINTEXT_SENTINEL in decrypted.decode("utf-8")


def test_temp_cleaned_up_after_successful_save(target_path: Path) -> None:
    save_encrypted_manifest_atomic(target_path, _manifest(), PASSWORD)
    leftover = [p for p in target_path.parent.iterdir() if p != target_path]
    assert leftover == []


def test_no_plaintext_reaches_disk_anywhere_in_directory(target_path: Path) -> None:
    manifest = _manifest(label=PLAINTEXT_SENTINEL)
    save_encrypted_manifest_atomic(target_path, manifest, PASSWORD)
    for entry in target_path.parent.iterdir():
        data = entry.read_bytes()
        assert PLAINTEXT_SENTINEL.encode("utf-8") not in data
        assert b"workspace_manifest" not in data


# ---------------------------------------------------------------------------
# H. Correction Pass MAJOR-1: password/plaintext через exc.__traceback__
#
# Independent Review показал, что зачистки __context__/__cause__
# недостаточно: exc.__traceback__.tb_frame.f_locals для ВНУТРЕННИХ
# фреймов storage.py/manifest.py мог содержать реальный password и
# полный расшифрованный plaintext — канал, не проявляющийся в обычном
# str()/repr()/traceback.format_exception(), но доступный обычным
# программным обходом traceback без capture_locals=True.
#
# ВАЖНО (ложноположительные срабатывания, задание Correction Pass §9):
# фрейм САМОГО теста (где стоит `try/except`) неизбежно содержит наши
# sentinel-переменные — это не утечка ИЗ реализации, а просто область
# видимости вызывающего кода. Поэтому проверка ниже фильтрует фреймы по
# имени файла: засчитываются только фреймы, чей co_filename оканчивается
# на app/workspace/storage.py или app/workspace/manifest.py (т.е.
# принадлежат самой реализации), а не файлу этого теста.
# ---------------------------------------------------------------------------

_INTERNAL_FILENAME_SUFFIXES = (
    os.path.join("app", "workspace", "storage.py"),
    os.path.join("app", "workspace", "manifest.py"),
)


def _internal_frame_locals_containing(exc: BaseException, sentinels: tuple) -> list:
    """
    Рекурсивно обходит exc.__traceback__ САМОГО exc и traceback каждого
    исключения, достижимого через цепочку __context__/__cause__ (сколь
    угодно глубоко), и возвращает найденные (owner, frame_index,
    filename, function, var_name) для локальных переменных ВНУТРЕННИХ
    (app/workspace/storage.py или app/workspace/manifest.py) фреймов,
    содержащих любой из sentinels. Пустой список означает отсутствие
    утечки через traceback-фреймы реализации.
    """
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
                            findings.append(
                                (owner_label, idx, os.path.basename(filename), tb.tb_frame.f_code.co_name, name)
                            )
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


def test_malformed_json_password_and_plaintext_not_in_traceback_frames(
    target_path: Path,
) -> None:
    malformed = ('{"unterminated": "' + PLAINTEXT_SENTINEL).encode("utf-8")
    _write_encrypted(target_path, malformed, password=PASSWORD_SENTINEL)

    with pytest.raises(WorkspaceCorruptedError) as excinfo:
        load_encrypted_manifest(target_path, PASSWORD_SENTINEL)

    findings = _internal_frame_locals_containing(
        excinfo.value, (PASSWORD_SENTINEL, PLAINTEXT_SENTINEL)
    )
    assert findings == []


def test_invalid_utf8_password_and_plaintext_not_in_traceback_frames(
    target_path: Path,
) -> None:
    malformed = PLAINTEXT_SENTINEL.encode("utf-8") + b"\xff\xfe"
    _write_encrypted(target_path, malformed, password=PASSWORD_SENTINEL)

    with pytest.raises(WorkspaceCorruptedError) as excinfo:
        load_encrypted_manifest(target_path, PASSWORD_SENTINEL)

    findings = _internal_frame_locals_containing(
        excinfo.value, (PASSWORD_SENTINEL, PLAINTEXT_SENTINEL)
    )
    assert findings == []


def test_unsupported_schema_password_and_plaintext_not_in_traceback_frames(
    target_path: Path,
) -> None:
    manifest = _manifest(label=PLAINTEXT_SENTINEL)
    save_encrypted_manifest_atomic(target_path, manifest, PASSWORD_SENTINEL)
    payload = json.loads(crypto.decrypt_bytes(target_path.read_bytes(), PASSWORD_SENTINEL))
    payload["schema_version"] = 2
    new_plaintext = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    _write_encrypted(target_path, new_plaintext, password=PASSWORD_SENTINEL)

    with pytest.raises(WorkspaceCorruptedError) as excinfo:
        load_encrypted_manifest(target_path, PASSWORD_SENTINEL)

    findings = _internal_frame_locals_containing(
        excinfo.value, (PASSWORD_SENTINEL, PLAINTEXT_SENTINEL)
    )
    assert findings == []


def test_wrong_password_sentinel_not_in_traceback_frames(target_path: Path) -> None:
    save_encrypted_manifest_atomic(target_path, _manifest(), PASSWORD_SENTINEL)

    with pytest.raises(WorkspaceAuthenticationError) as excinfo:
        load_encrypted_manifest(target_path, "wrong-" + PASSWORD_SENTINEL)

    findings = _internal_frame_locals_containing(excinfo.value, (PASSWORD_SENTINEL,))
    assert findings == []


def test_tampered_ciphertext_password_sentinel_not_in_traceback_frames(
    target_path: Path,
) -> None:
    save_encrypted_manifest_atomic(target_path, _manifest(), PASSWORD_SENTINEL)
    raw = bytearray(target_path.read_bytes())
    raw[-1] ^= 0xFF
    target_path.write_bytes(bytes(raw))

    with pytest.raises(WorkspaceAuthenticationError) as excinfo:
        load_encrypted_manifest(target_path, PASSWORD_SENTINEL)

    findings = _internal_frame_locals_containing(excinfo.value, (PASSWORD_SENTINEL,))
    assert findings == []


def test_missing_file_password_sentinel_not_in_traceback_frames(target_path: Path) -> None:
    # target_path не существует. password — параметр фрейма
    # load_encrypted_manifest с самого входа в функцию, задолго до
    # какой-либо расшифровки — важно проверить именно этот путь отдельно.
    with pytest.raises(WorkspaceNotFoundError) as excinfo:
        load_encrypted_manifest(target_path, PASSWORD_SENTINEL)

    findings = _internal_frame_locals_containing(excinfo.value, (PASSWORD_SENTINEL,))
    assert findings == []


def test_oversized_container_password_sentinel_not_in_traceback_frames(
    target_path: Path,
) -> None:
    with open(target_path, "wb") as f:
        f.seek(MAX_ENCRYPTED_MANIFEST_BYTES + 1)
        f.write(b"\0")

    with pytest.raises(WorkspaceCorruptedError) as excinfo:
        load_encrypted_manifest(target_path, PASSWORD_SENTINEL)

    findings = _internal_frame_locals_containing(excinfo.value, (PASSWORD_SENTINEL,))
    assert findings == []


def test_traceback_leak_check_would_catch_a_real_regression() -> None:
    """
    Валидность самой проверки (защита от false-negative): убеждаемся,
    что _internal_frame_locals_containing ДЕЙСТВИТЕЛЬНО находит sentinel,
    реально оставленный в locals фрейма, а не всегда возвращает пустой
    список независимо от содержимого. Имя файла этого теста временно
    добавляется в список "внутренних" суффиксов, чтобы фрейм ниже
    засчитывался как проверяемый (иначе сканер намеренно его игнорирует
    как код вызывающей стороны — см. пояснение в начале группы H).
    """

    def _frame_with_uncleared_secret(secret: str) -> None:
        raise ValueError("synthetic leak")

    try:
        _frame_with_uncleared_secret(PASSWORD_SENTINEL)
    except ValueError as synthetic_exc:
        global _INTERNAL_FILENAME_SUFFIXES
        original_suffixes = _INTERNAL_FILENAME_SUFFIXES
        _INTERNAL_FILENAME_SUFFIXES = original_suffixes + (os.path.basename(__file__),)
        try:
            positive_findings = _internal_frame_locals_containing(synthetic_exc, (PASSWORD_SENTINEL,))
        finally:
            _INTERNAL_FILENAME_SUFFIXES = original_suffixes
        assert positive_findings != [], "сканер обязан обнаруживать sentinel, реально присутствующий в frame locals"


# ---------------------------------------------------------------------------
# I. Correction Pass MINOR-1: TOCTOU bounded read
#
# Independent Review показал: предыдущая реализация проверяла
# target.stat().st_size ОДИН раз, затем безусловно читала весь файл —
# если файл рос МЕЖДУ этими двумя операциями, гейт не защищал. Тест
# ниже инструментирует именно новую границу чтения (_read_bounded), а
# не полагается на реальный тайминг гонки: подменяет _read_bounded так,
# чтобы файл дописывался ПОСЛЕ первого (быстрого) наблюдения размера, но
# ДО фактического чтения, и проверяет, что decrypt_bytes всё равно не
# вызывается и результат детерминирован.
# ---------------------------------------------------------------------------


def test_bounded_read_never_exceeds_limit_even_if_file_grows_during_read(
    target_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manifest = _manifest()
    save_encrypted_manifest_atomic(target_path, manifest, PASSWORD)

    real_read_bounded = storage_module._read_bounded
    grown = {"v": False}

    def growing_read_bounded(target: Path):
        if not grown["v"]:
            grown["v"] = True
            # Дописываем файл ПОСЛЕ прохождения быстрой stat()-проверки в
            # load_encrypted_manifest (она уже случилась к этому моменту,
            # так как _read_bounded вызывается ПОСЛЕ неё), но ДО
            # фактического f.read() внутри _read_bounded.
            with open(target_path, "ab") as f:
                f.write(b"\0" * (MAX_ENCRYPTED_MANIFEST_BYTES + 4096))
        return real_read_bounded(target)

    monkeypatch.setattr(storage_module, "_read_bounded", growing_read_bounded)

    called = {"n": 0}
    real_decrypt = storage_module.decrypt_bytes

    def spy_decrypt(*args: object, **kwargs: object) -> bytes:
        called["n"] += 1
        return real_decrypt(*args, **kwargs)  # pragma: no cover - не должен вызываться

    monkeypatch.setattr(storage_module, "decrypt_bytes", spy_decrypt)

    with pytest.raises(WorkspaceCorruptedError) as excinfo:
        load_encrypted_manifest(target_path, PASSWORD)

    assert excinfo.value.reason is WorkspaceCorruptedReason.MANIFEST_INVALID_CONTAINER
    assert called["n"] == 0
    # Файл на диске ДЕЙСТВИТЕЛЬНО вырос за пределы лимита — тест
    # подтверждает, что защита сработала НЕСМОТРЯ на это, а не потому,
    # что рост не произошёл.
    assert target_path.stat().st_size > MAX_ENCRYPTED_MANIFEST_BYTES


def test_bounded_read_reads_at_most_limit_plus_one_bytes(
    target_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    Проверяет саму границу чтения напрямую: _read_bounded никогда не
    возвращает больше MAX_ENCRYPTED_MANIFEST_BYTES + 1 байт, даже когда
    физический файл значительно больше.
    """
    huge_path = target_path.parent / "huge_raw.bin"
    with open(huge_path, "wb") as f:
        f.seek(MAX_ENCRYPTED_MANIFEST_BYTES * 2)
        f.write(b"\0")

    data = storage_module._read_bounded(huge_path)
    assert len(data) == MAX_ENCRYPTED_MANIFEST_BYTES + 1


# ---------------------------------------------------------------------------
# L. Correction Pass #2: load_encrypted_manifest — валидация до try/finally
#
# Independent Re-Review показал: в Correction Pass #1 защищённый
# try/finally начинался ПОСЛЕ вызовов _validate_path_argument/
# _validate_password_argument — TypeError из-за невалидного `path` (при
# РЕАЛЬНОМ пароле) поднимался ДО входа в защиту, оставляя password в
# кадре load_encrypted_manifest. Теперь try/finally охватывает функцию
# целиком, начиная с первой строки тела. Переиспользуем уже
# валидированный сканер _internal_frame_locals_containing (группа H) —
# он корректно фильтрует по имени файла, отличая кадры реализации от
# кадра самого теста (см. пояснение там же).
# ---------------------------------------------------------------------------


def test_load_invalid_path_type_password_not_in_traceback(target_path: Path) -> None:
    # L1: невалидный тип path + валидный (реальный) секретный пароль.
    with pytest.raises(TypeError) as excinfo:
        load_encrypted_manifest(12345, PASSWORD_SENTINEL)  # type: ignore[arg-type]
    findings = _internal_frame_locals_containing(excinfo.value, (PASSWORD_SENTINEL,))
    assert findings == []


def test_load_path_none_password_not_in_traceback(target_path: Path) -> None:
    # L2: path=None + валидный секретный пароль.
    with pytest.raises(TypeError) as excinfo:
        load_encrypted_manifest(None, PASSWORD_SENTINEL)  # type: ignore[arg-type]
    findings = _internal_frame_locals_containing(excinfo.value, (PASSWORD_SENTINEL,))
    assert findings == []


def test_load_invalid_password_bytes_not_in_traceback(target_path: Path) -> None:
    # L3: валидный path + невалидный ПО ТИПУ пароль (bytes вместо str) —
    # покрывает случай, когда вызывающий код по ошибке передал реальный
    # секрет не той формы (например, уже закодированный в bytes).
    save_encrypted_manifest_atomic(target_path, _manifest(), PASSWORD)
    secret_bytes = PASSWORD_SENTINEL.encode("utf-8")
    with pytest.raises(WorkspaceInputError) as excinfo:
        load_encrypted_manifest(target_path, secret_bytes)  # type: ignore[arg-type]
    findings = _internal_frame_locals_containing(excinfo.value, (PASSWORD_SENTINEL,))
    assert findings == []


def test_load_gap_a_regression_full_exception_graph(target_path: Path) -> None:
    """
    Прямое воспроизведение находки Independent Re-Review (Gap A):
    load_encrypted_manifest(12345, PW_SENTINEL) не должен оставлять
    пароль нигде в графе исключения — _internal_frame_locals_containing
    уже рекурсивно обходит и traceback, и цепочку __context__/__cause__
    (см. её docstring в группе H), поэтому пустой результат покрывает
    оба канала одной проверкой.
    """
    with pytest.raises(TypeError) as excinfo:
        load_encrypted_manifest(12345, PASSWORD_SENTINEL)  # type: ignore[arg-type]
    findings = _internal_frame_locals_containing(excinfo.value, (PASSWORD_SENTINEL,))
    assert findings == []


# ---------------------------------------------------------------------------
# SAV. Correction Pass #2: save_encrypted_manifest_atomic конфиденциальность
#
# Independent Re-Review показал: save_encrypted_manifest_atomic вообще
# не была затронута Correction Pass #1. Она держит password (аргумент)
# и plaintext (serialize_manifest(manifest)) в собственном фрейме —
# любое исключение, включая уже штатно тестируемый сбой os.replace
# (группа F), уносило оба значения в traceback. Теперь функция обёрнута
# в единый try/finally целиком.
# ---------------------------------------------------------------------------


def test_save_invalid_path_type_password_not_in_traceback() -> None:
    # SAV1: невалидный тип path + валидный секретный пароль.
    with pytest.raises(TypeError) as excinfo:
        save_encrypted_manifest_atomic(12345, _manifest(), PASSWORD_SENTINEL)  # type: ignore[arg-type]
    findings = _internal_frame_locals_containing(excinfo.value, (PASSWORD_SENTINEL,))
    assert findings == []


def test_save_invalid_manifest_password_not_in_traceback(target_path: Path) -> None:
    # SAV2: валидный path + невалидный manifest + валидный секретный пароль.
    with pytest.raises(TypeError) as excinfo:
        save_encrypted_manifest_atomic(
            target_path, {"not": "a manifest"}, PASSWORD_SENTINEL  # type: ignore[arg-type]
        )
    findings = _internal_frame_locals_containing(excinfo.value, (PASSWORD_SENTINEL,))
    assert findings == []


def test_save_os_replace_failure_password_not_in_traceback(
    target_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # SAV3: валидный path + валидный manifest + секретный пароль +
    # сбой os.replace (уже штатно тестируется в группе F без проверки
    # traceback — здесь проверяем именно конфиденциальность).
    def failing_replace(*args: object, **kwargs: object) -> None:
        raise OSError("simulated os.replace failure")

    monkeypatch.setattr(storage_module.os, "replace", failing_replace)

    with pytest.raises(OSError) as excinfo:
        save_encrypted_manifest_atomic(target_path, _manifest(), PASSWORD_SENTINEL)

    findings = _internal_frame_locals_containing(excinfo.value, (PASSWORD_SENTINEL,))
    assert findings == []


def test_save_os_replace_failure_plaintext_not_in_traceback(
    target_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # SAV4: manifest содержит PLAINTEXT_SENTINEL в label; сбой os.replace
    # ПОСЛЕ сериализации/шифрования — сериализованный plaintext не
    # должен быть достижим через traceback реализации. Вызывающий код
    # по-прежнему владеет исходным объектом manifest (это не нарушение
    # — см. пояснение задания) — проверяем ТОЛЬКО кадры storage.py.
    manifest = _manifest(label=PLAINTEXT_SENTINEL)

    def failing_replace(*args: object, **kwargs: object) -> None:
        raise OSError("simulated os.replace failure")

    monkeypatch.setattr(storage_module.os, "replace", failing_replace)

    with pytest.raises(OSError) as excinfo:
        save_encrypted_manifest_atomic(target_path, manifest, PASSWORD)

    findings = _internal_frame_locals_containing(excinfo.value, (PLAINTEXT_SENTINEL,))
    assert findings == []


def test_save_mkstemp_failure_password_and_plaintext_not_in_traceback(
    target_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # SAV5: сбой создания temp-файла (до какой-либо записи на диск).
    manifest = _manifest(label=PLAINTEXT_SENTINEL)

    def failing_mkstemp(*args: object, **kwargs: object):
        raise OSError("simulated mkstemp failure")

    monkeypatch.setattr(storage_module.tempfile, "mkstemp", failing_mkstemp)

    with pytest.raises(OSError) as excinfo:
        save_encrypted_manifest_atomic(target_path, manifest, PASSWORD_SENTINEL)

    findings = _internal_frame_locals_containing(
        excinfo.value, (PASSWORD_SENTINEL, PLAINTEXT_SENTINEL)
    )
    assert findings == []


def test_save_fsync_failure_password_and_plaintext_not_in_traceback(
    target_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # SAV6: сбой fsync (после write, до replace).
    manifest = _manifest(label=PLAINTEXT_SENTINEL)

    def failing_fsync(fd: int) -> None:
        raise OSError("simulated fsync failure")

    monkeypatch.setattr(storage_module.os, "fsync", failing_fsync)

    with pytest.raises(OSError) as excinfo:
        save_encrypted_manifest_atomic(target_path, manifest, PASSWORD_SENTINEL)

    findings = _internal_frame_locals_containing(
        excinfo.value, (PASSWORD_SENTINEL, PLAINTEXT_SENTINEL)
    )
    assert findings == []


def test_save_gap_b_regression_password_and_plaintext(
    target_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    Прямое воспроизведение находки Independent Re-Review (Gap B):
    успешная сериализация/шифрование, затем сбой os.replace — и
    password, и serialized plaintext (через label) не должны быть
    достижимы через traceback реализации.
    """
    manifest = _manifest(label=PLAINTEXT_SENTINEL)

    def failing_replace(*args: object, **kwargs: object) -> None:
        raise OSError("simulated")

    monkeypatch.setattr(storage_module.os, "replace", failing_replace)

    with pytest.raises(OSError) as excinfo:
        save_encrypted_manifest_atomic(target_path, manifest, PASSWORD_SENTINEL)

    findings = _internal_frame_locals_containing(
        excinfo.value, (PASSWORD_SENTINEL, PLAINTEXT_SENTINEL)
    )
    assert findings == []


def test_save_still_functions_correctly_after_correction(target_path: Path) -> None:
    """Обычный успешный путь не сломан правкой (round-trip)."""
    manifest = _manifest(label="normal label")
    save_encrypted_manifest_atomic(target_path, manifest, PASSWORD)
    assert load_encrypted_manifest(target_path, PASSWORD) == manifest
