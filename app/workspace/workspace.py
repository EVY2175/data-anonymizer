"""
Жизненный цикл Workspace, привязка/диагностика/мутация/восстановление
персистентных сторов, adoption существующих сторов (Stage 10B.3).

======================================================================
Границы слоя
======================================================================

Этот модуль реализует ТОЛЬКО: create_workspace/open_workspace/
adopt_stores, диагностику привязки store<->manifest, контролируемую
мутацию сторов (store_mutation), явное восстановление после прерванной
мутации (recover_store_state) и безопасное чтение (read_stores). Реестр
артефактов, provenance-workflow, backup/restore, External-AI
authorization, CLI/GUI — вне объёма (Stage 10B.4+/10C/10D).

======================================================================
Бизнес-требование (frozen)
======================================================================

Один и тот же реальный объект должен получать один и тот же псевдоним во
всех месяцах при использовании одного Workspace — поэтому персистентный
MappingStore/IdentifierMappingStore является частью identity Workspace, и
эта модель ни при каких обстоятельствах не должна тихо подменяться свежим
пустым store (опечатка в пути, отсутствующий файл и т.п.).

======================================================================
OD-1..OD-8 (Stage 10B.3 Contract Review, frozen owner decisions)
======================================================================

OD-1: create_workspace/adopt_stores требуют пароль str, длиной не менее
MIN_CREATION_PASSWORD_LENGTH символов, без нормализации/strip/сложности.
open_workspace принимает любой непустой str — политика создания повторно
не проверяется, старые workspace с более коротким паролем остаются
открываемыми.

OD-2: mapping.enc/identifiers.enc физически НЕ создаются при
entry_count == 0 — остаются absent до первой реальной мутации.

OD-3/OD-4: store_mutation()/read_stores() никогда не возвращают сырые
EncryptedFileMappingStore/EncryptedFileIdentifierMappingStore. Мутация —
через ограниченный append-only façade (без clear()), становящийся
недействительным сразу после выхода из `with`. Чтение — через отдельные
read-only reader-объекты, структурно не имеющие методов мутации. Прямой
обход через приватные атрибуты (`_store` и т.п.) — намеренный обход
приватных внутренностей, вне threat model этого слайса.

OD-5: каждая durable запись manifest увеличивает revision на 1. Полный
успешный store_mutation() — это ДВЕ durable записи (pending=True, затем
pending=False) => +2. Recovery — ОДНА durable запись => +1.

OD-6: pending_store_mutation=True с состоянием "mapping AHEAD, identifier
CONSISTENT" (или наоборот) безопасно восстановим — каждый store
диагностируется независимо.

OD-7 (SECURITY): EncryptedFile*Store хранит password как атрибут ОБЪЕКТА
(self._password) — любое исключение из его конструктора/методов несёт
`self` в traceback-фрейме. Публичные границы Workspace никогда не
пропускают такое исключение как есть: оно перехватывается, из него
извлекается только безопасная классификация, except-блок ПОКИДАЕТСЯ, и
только тогда поднимается свежее исключение Workspace-слоя (тот же приём,
что уже закрыл MAJOR-1 в Stage 10B.2). Пароль-параметр (bare str)
дополнительно зачищается в каждом кадре, который держит его как локальную
переменную, тем же `finally`-паттерном, что и `app.workspace.storage`.

OD-8: adopt_stores без identifier_store_path — identifier-состояние
Workspace пустое (entry_count=0), файл отсутствует до первой мутации.

======================================================================
Stage 10B.4.1 (Contract & Architecture Review + Owner Decisions,
frozen) — artifact allocation/staging/registration
======================================================================

allocate_artifact_path/stage_analytical_file: ТОЛЬКО резервируют
структурно допустимое место (ArtifactSlot) — не создают запись manifest,
не требуют лока (`artifact_id`/путь генерируются через secrets.token_hex,
коллизия проверяется чтением ФС и практически невозможна при 128 битах
энтропии). stage_analytical_file — специализированная обёртка над
allocate_artifact_path(ANALYTICAL_CANONICAL) для внешнего (например,
ChatGPT-обновлённого) workbook: копирует байты атомарно, source остаётся
неизменным, не должен находиться внутри workspace root.

register_artifact — единственная security boundary: держит лок,
перечитывает manifest, отклоняет recovery_required, пересчитывает
РЕАЛЬНЫЙ SHA-256 файла (не доверяет тому, что было на момент allocation),
проверяет глобальную уникальность SHA, lineage (зеркалирует
app.workspace.models._validate_lineage — ДОЛЖНО оставаться синхронным с
этой frozen моделью), evidence (SafetyReport, Stage 10A) по kind,
provenance-привязку (ТОЛЬКО для ANONYMIZED_MONTHLY). provenance_sha256
Workspace ВСЕГДА вычисляет сама из фактических байт зашифрованного
sidecar на диске — НЕ принимает её на доверии от вызывающего кода
(вызывающий код передаёт только provenance_id). Зарегистрированный
артефакт — известный, хэш-привязанный объект реестра с валидной lineage;
это НЕ означает разрешение на отправку во внешний AI (Stage 10C).

Владельческие решения (owner decisions), зафиксированные для 10B.4:
OD-10B4-1 (latest rollback) и OD-10B4-3 (backup.json) относятся к
10B.4.2/10B.4.3, здесь не задействованы.

Correction Pass #1 (закрытие MINOR-1 Independent Adversarial Review):
provenance_sha256 и job_id-валидация теперь гарантированно относятся к
ОДНОМУ И ТОМУ ЖЕ неизменяемому снимку зашифрованных байт provenance-
sidecar (один read_bytes(), хэш от него же, job_id читается через
временную копию ЭТИХ ЖЕ байт) — устраняя возможность двух независимых
чтений мутирующего оригинала дать несогласованную привязку "job_id от
версии A, sha256 от версии B". См. _capture_provenance_snapshot/
_read_job_id_from_snapshot.
"""

from __future__ import annotations

import contextlib
import dataclasses
import hashlib
import os
import re
import secrets
import shutil
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator, Optional, Union

from app.mapping.base import (
    MappingConflictError,
    _PARENT_UNSET,
    _ParentAliasArg,
)
from app.mapping.encrypted_file import EncryptedFileMappingStore
from app.mapping.encrypted_identifier_file import EncryptedFileIdentifierMappingStore
from app.mapping.identifier_base import IdentifierMappingConflictError
from app.mapping.provenance_encrypted import EncryptedFileProvenanceStore
from app.models.entities import EntityType, MappingEntry
from app.models.identifiers import IdentifierMappingEntry, IdentifierType
from app.safety.external_ai import SafetyReport, sha256_file
from app.workspace.errors import (
    ArtifactNotFoundError,
    ArtifactRegistrationError,
    ArtifactRegistrationReason,
    WorkspaceAuthenticationError,
    WorkspaceBindingError,
    WorkspaceBindingReason,
    WorkspaceCorruptedError,
    WorkspaceCorruptedReason,
    WorkspaceInputError,
    WorkspaceLockedError,
    WorkspaceNotFoundError,
)
from app.workspace.locking import WorkspaceFileLock
from app.workspace.manifest import (
    EMPTY_IDENTIFIER_STATE,
    EMPTY_MAPPING_STATE,
    identifier_entries_prefix_digest,
    mapping_entries_prefix_digest,
)
from app.workspace.models import (
    MANIFEST_SCHEMA_VERSION,
    ArtifactKind,
    ArtifactRecord,
    ArtifactSlot,
    StoreRecoveryResult,
    StoreState,
    StoreSyncState,
    WorkspaceInfo,
    WorkspaceManifest,
)
from app.workspace.storage import load_encrypted_manifest, save_encrypted_manifest_atomic

# OD-1: backend-политика создания. UX/GUI-политика сюда не входит.
MIN_CREATION_PASSWORD_LENGTH = 8

_MAPPING_FILENAME = "mapping.enc"
_IDENTIFIER_FILENAME = "identifiers.enc"
_STORES_DIRNAME = "stores"
_WORKSPACE_ENC_NAME = "workspace.enc"
_WORKSPACE_LOCK_NAME = "workspace.lock"
# Разрешённые в "чистом skeleton" директории (см. _ensure_clean_skeleton_or_absent).
_STRUCTURAL_SUBDIRS = ("stores", "provenance", "safe", "local_plaintext", "backup")

# Stage 10B.4.1: директории артефактов/provenance.
_SAFE_DIRNAME = "safe"
_LOCAL_PLAINTEXT_DIRNAME = "local_plaintext"
_PROVENANCE_DIRNAME = "provenance"
_ARTIFACT_EXTENSION = ".xlsx"
_PROVENANCE_EXTENSION = ".enc"
_ALLOCATION_MAX_ATTEMPTS = 8

_HEX32_RE = re.compile(r"[0-9a-f]{32}")
# Тот же period-формат, что заморожен в app.workspace.models._is_period
# (не импортируется напрямую — приватная деталь другого модуля).
_PERIOD_RE = re.compile(r"[0-9]{4}-(?:0[1-9]|1[0-2])")


def _is_hex32(value: object) -> bool:
    return isinstance(value, str) and _HEX32_RE.fullmatch(value) is not None


def _is_period(value: object) -> bool:
    return isinstance(value, str) and _PERIOD_RE.fullmatch(value) is not None


# ----------------------------------------------------------------------
# Пути раскладки
# ----------------------------------------------------------------------


def _workspace_enc_path(root: Path) -> Path:
    return root / _WORKSPACE_ENC_NAME


def _workspace_lock_path(root: Path) -> Path:
    return root / _WORKSPACE_LOCK_NAME


def _mapping_store_path(root: Path) -> Path:
    return root / _STORES_DIRNAME / _MAPPING_FILENAME


def _identifier_store_path(root: Path) -> Path:
    return root / _STORES_DIRNAME / _IDENTIFIER_FILENAME


def _artifact_dir_for_kind(root: Path, kind: ArtifactKind) -> Path:
    if kind is ArtifactKind.LOCAL_RESTORED:
        return root / _LOCAL_PLAINTEXT_DIRNAME
    return root / _SAFE_DIRNAME  # ANONYMIZED_MONTHLY и ANALYTICAL_CANONICAL — одна директория


def _artifact_path(root: Path, kind: ArtifactKind, artifact_id: str) -> Path:
    return _artifact_dir_for_kind(root, kind) / f"{artifact_id}{_ARTIFACT_EXTENSION}"


def _provenance_path(root: Path, provenance_id: str) -> Path:
    return root / _PROVENANCE_DIRNAME / f"{provenance_id}{_PROVENANCE_EXTENSION}"


def _now_timestamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ----------------------------------------------------------------------
# Валидация аргументов
# ----------------------------------------------------------------------


def _validate_path_like_argument(value: object, *, name: str) -> Path:
    if not isinstance(value, (str, Path)):
        raise TypeError(f"{name} должен быть str или Path, получено: {type(value)!r}")
    return Path(value)


def _validate_creation_password(password: object) -> None:
    # OD-1: только длина, без сложности/нормализации. Сам пароль в
    # сообщение не попадает; локал зачищается до raise (OD-7 дисциплина).
    if not isinstance(password, str):
        password = None  # noqa: F841
        raise WorkspaceInputError("password должен быть str")
    if len(password) < MIN_CREATION_PASSWORD_LENGTH:
        password = None  # noqa: F841
        raise WorkspaceInputError(
            f"password должен содержать не менее {MIN_CREATION_PASSWORD_LENGTH} символов"
        )


def _validate_open_password(password: object) -> None:
    # OD-1: open принимает любой непустой str — политика создания здесь
    # не проверяется повторно.
    if not isinstance(password, str):
        password = None  # noqa: F841
        raise WorkspaceInputError("password должен быть str")
    if password == "":
        password = None  # noqa: F841
        raise WorkspaceInputError("password не может быть пустым")


def _validate_source_password_argument(password: object, *, name: str) -> None:
    if not isinstance(password, str):
        password = None  # noqa: F841
        raise WorkspaceInputError(f"{name} должен быть str")
    if password == "":
        password = None  # noqa: F841
        raise WorkspaceInputError(f"{name} не может быть пустым")


# ----------------------------------------------------------------------
# Layout safety (симлинки/junction/неверный тип) — минимальная защита от
# очевидного редиректа, НЕ защита от злонамеренного локального
# администратора (frozen accepted limitation).
# ----------------------------------------------------------------------


def _is_reparse_like(path: Path) -> bool:
    if path.is_symlink():
        return True
    is_junction = getattr(path, "is_junction", None)
    return bool(is_junction and is_junction())


def _reject_unsafe_dir(path: Path) -> None:
    if not path.exists():
        return
    if _is_reparse_like(path) or not path.is_dir():
        raise WorkspaceCorruptedError(WorkspaceCorruptedReason.UNSAFE_LAYOUT)


def _reject_unsafe_file(path: Path) -> None:
    if not path.exists():
        return
    if _is_reparse_like(path) or not path.is_file():
        raise WorkspaceCorruptedError(WorkspaceCorruptedReason.UNSAFE_LAYOUT)


def _validate_layout_safety(root: Path) -> None:
    _reject_unsafe_file(_workspace_enc_path(root))
    _reject_unsafe_file(_workspace_lock_path(root))
    _reject_unsafe_dir(root / _STORES_DIRNAME)
    _reject_unsafe_file(_mapping_store_path(root))
    _reject_unsafe_file(_identifier_store_path(root))


def _reject_path_inside(candidate: Path, container: Path) -> None:
    try:
        candidate.resolve().relative_to(container.resolve())
    except ValueError:
        return
    raise WorkspaceInputError(
        "исходный путь стора не может находиться внутри целевого workspace root"
    )


def _reject_external_artifact_path_inside(candidate: Path, container: Path) -> None:
    try:
        candidate.resolve().relative_to(container.resolve())
    except ValueError:
        return
    raise WorkspaceInputError(
        "external_path не может находиться внутри workspace root"
    )


def _atomic_copy_file(source: Path, destination: Path) -> None:
    """
    Атомарная копия байт source -> destination: temp-файл в ТОЙ ЖЕ
    директории, что и destination, потоковое копирование, fsync,
    os.replace. Тот же паттерн, что уже используется для зашифрованных
    сторов/manifest (Stage 4/7B.4.1/7C.3/10B.2) — destination гарантированно
    ещё не существует (получен через allocate_artifact_path), поэтому
    коллизии на этапе записи не предполагается.
    """
    directory = destination.parent
    fd, tmp_name = tempfile.mkstemp(dir=str(directory), prefix=f".{destination.name}.", suffix=".tmp")
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as tmp_file:
            with open(source, "rb") as src_file:
                shutil.copyfileobj(src_file, tmp_file)
            tmp_file.flush()
            os.fsync(tmp_file.fileno())
        os.replace(str(tmp_path), str(destination))
    except Exception:
        try:
            tmp_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise


# ----------------------------------------------------------------------
# Clean skeleton (create_workspace/adopt_stores retry-safety, §13/§15)
# ----------------------------------------------------------------------


def _ensure_clean_skeleton_or_absent(root: Path) -> None:
    """
    root допускается либо отсутствовать, либо быть пустым, либо быть
    ТОЧНЫМ чистым skeleton прерванного create/adopt: `workspace.lock`
    (обычный 0-байтный файл) и/или любое подмножество ПУСТЫХ директорий
    из _STRUCTURAL_SUBDIRS. Любое иное содержимое — UNSAFE_LAYOUT, без
    попытки тихо почистить неизвестное содержимое.
    """
    if not root.exists():
        return
    if _is_reparse_like(root) or not root.is_dir():
        raise WorkspaceCorruptedError(WorkspaceCorruptedReason.UNSAFE_LAYOUT)

    entries = list(root.iterdir())
    if not entries:
        return

    for entry in entries:
        name = entry.name
        if name == _WORKSPACE_LOCK_NAME:
            if _is_reparse_like(entry) or not entry.is_file() or entry.stat().st_size != 0:
                raise WorkspaceCorruptedError(WorkspaceCorruptedReason.UNSAFE_LAYOUT)
            continue
        if name in _STRUCTURAL_SUBDIRS:
            if _is_reparse_like(entry) or not entry.is_dir():
                raise WorkspaceCorruptedError(WorkspaceCorruptedReason.UNSAFE_LAYOUT)
            if any(entry.iterdir()):
                raise WorkspaceCorruptedError(WorkspaceCorruptedReason.UNSAFE_LAYOUT)
            continue
        # workspace.enc, store-файлы, temp-файлы, посторонние файлы/папки —
        # недопустимо ни при каких условиях.
        raise WorkspaceCorruptedError(WorkspaceCorruptedReason.UNSAFE_LAYOUT)


def _ensure_structural_subdirs(root: Path) -> None:
    for name in _STRUCTURAL_SUBDIRS:
        (root / name).mkdir(exist_ok=True)


# ----------------------------------------------------------------------
# OD-7: санитизация исключений store-слоя
#
# EncryptedFile*Store хранит password как self._password — любое
# исключение из его конструктора/методов несёт `self` в traceback-фрейме.
# Ниже — узкие обёртки, перехватывающие ИСКЛЮЧИТЕЛЬНО такие вызовы:
# исходное исключение обрабатывается, except-блок ПОКИДАЕТСЯ, локальные
# переменные, способные держать пароль/store-объект/сырые значения,
# зачищаются, и только тогда конструируется и поднимается свежее
# исключение — точно тот же приём, что закрыл MAJOR-1 в Stage 10B.2.
# ----------------------------------------------------------------------


def _construct_mapping_store(path: Path, password: str) -> EncryptedFileMappingStore:
    failed = False
    try:
        return EncryptedFileMappingStore(path, password)
    except Exception:
        failed = True
    password = None  # noqa: F841 — зачистка чувствительного локала фрейма
    if failed:
        raise WorkspaceBindingError(WorkspaceBindingReason.STORE_UNOPENABLE)
    raise AssertionError("unreachable")  # pragma: no cover


def _construct_identifier_store(path: Path, password: str) -> EncryptedFileIdentifierMappingStore:
    failed = False
    try:
        return EncryptedFileIdentifierMappingStore(path, password)
    except Exception:
        failed = True
    password = None  # noqa: F841 — зачистка чувствительного локала фрейма
    if failed:
        raise WorkspaceBindingError(WorkspaceBindingReason.STORE_UNOPENABLE)
    raise AssertionError("unreachable")  # pragma: no cover


def _construct_provenance_store_for_open(path: Path, password: str) -> EncryptedFileProvenanceStore:
    """
    OD-7-safe открытие СУЩЕСТВУЮЩЕГО provenance sidecar (job_id не
    передаётся — authoritative job_id читается из payload).
    EncryptedFileProvenanceStore тоже хранит password как атрибут
    объекта (self._password) — тот же риск и тот же приём, что и для
    mapping/identifier сторов (Stage 10B.3 OD-7).
    """
    failed = False
    try:
        return EncryptedFileProvenanceStore(path, password)
    except Exception:
        failed = True
    password = None  # noqa: F841 — зачистка чувствительного локала фрейма
    if failed:
        raise ArtifactRegistrationError(ArtifactRegistrationReason.PROVENANCE_INVALID)
    raise AssertionError("unreachable")  # pragma: no cover


def _mapping_store_add(store: EncryptedFileMappingStore, entry: MappingEntry) -> None:
    conflict_message: Optional[str] = None
    failed = False
    try:
        store.add(entry)
        return
    except MappingConflictError as exc:
        conflict_message = str(exc)
    except Exception:
        failed = True
    store = None  # noqa: F841 — зачистка (объект несёт _password)
    entry = None  # noqa: F841 — зачистка (MappingEntry несёт real_value)
    if conflict_message is not None:
        raise MappingConflictError(conflict_message)
    if failed:
        raise WorkspaceBindingError(WorkspaceBindingReason.STORE_UNOPENABLE)
    raise AssertionError("unreachable")  # pragma: no cover


def _identifier_store_add(store: EncryptedFileIdentifierMappingStore, entry: IdentifierMappingEntry) -> None:
    conflict_message: Optional[str] = None
    failed = False
    try:
        store.add(entry)
        return
    except IdentifierMappingConflictError as exc:
        conflict_message = str(exc)
    except Exception:
        failed = True
    store = None  # noqa: F841
    entry = None  # noqa: F841
    if conflict_message is not None:
        raise IdentifierMappingConflictError(conflict_message)
    if failed:
        raise WorkspaceBindingError(WorkspaceBindingReason.STORE_UNOPENABLE)
    raise AssertionError("unreachable")  # pragma: no cover


def _identifier_store_add_many(store: EncryptedFileIdentifierMappingStore, entries: tuple) -> None:
    conflict_message: Optional[str] = None
    failed = False
    try:
        store.add_many(entries)
        return
    except IdentifierMappingConflictError as exc:
        conflict_message = str(exc)
    except Exception:
        failed = True
    store = None  # noqa: F841
    entries = None  # noqa: F841
    if conflict_message is not None:
        raise IdentifierMappingConflictError(conflict_message)
    if failed:
        raise WorkspaceBindingError(WorkspaceBindingReason.STORE_UNOPENABLE)
    raise AssertionError("unreachable")  # pragma: no cover


def _sanitized_store_read(fn, *args, **kwargs):
    """OD-7: санитизирует любое исключение делегированного read-вызова
    store (bound-метод `fn` несёт исходный store через `fn.__self__`)."""
    failed = False
    try:
        return fn(*args, **kwargs)
    except Exception:
        failed = True
    fn = None  # noqa: F841 — зачистка (bound method держит __self__ = store)
    args = None  # noqa: F841
    kwargs = None  # noqa: F841
    if failed:
        raise WorkspaceInputError("некорректный вызов чтения store")
    raise AssertionError("unreachable")  # pragma: no cover


# ----------------------------------------------------------------------
# Store digest / state helpers (Stage 10B.1 digest, без дублирования)
# ----------------------------------------------------------------------


def _compute_mapping_state(store: EncryptedFileMappingStore) -> StoreState:
    entries = _sanitized_store_read(store.entries)
    return StoreState(entry_count=len(entries), prefix_digest=mapping_entries_prefix_digest(entries))


def _compute_identifier_state(store: EncryptedFileIdentifierMappingStore) -> StoreState:
    entries = _sanitized_store_read(store.entries)
    return StoreState(entry_count=len(entries), prefix_digest=identifier_entries_prefix_digest(entries))


# ----------------------------------------------------------------------
# Диагностика привязки store<->manifest (Contract Review §10/§12)
# ----------------------------------------------------------------------


def _diagnose_store(
    manifest_state: StoreState,
    store_path: Path,
    password: str,
    pending: bool,
    *,
    store_kind: str,
):
    """
    Возвращает (StoreSyncState, открытый_store) либо поднимает
    WorkspaceBindingError с точным reason по замороженной таблице
    состояний. `store_kind` — "mapping" или "identifier".
    """
    entry_count = manifest_state.entry_count
    exists = store_path.exists()

    try:
        # Критическая защита от "typo в пути" (Contract Review §21):
        # конструктор store молча даёт пустой store на отсутствующем
        # файле — это НЕДОПУСТИМО, если manifest ожидает записи. Этот
        # ранний raise — тоже внутри try/finally, иначе password не
        # успевал бы зачиститься в этом кадре до подъёма исключения
        # (обнаружено экспериментально при smoke-проверке OD-7).
        if entry_count > 0 and not exists:
            raise WorkspaceBindingError(WorkspaceBindingReason.STORE_MISSING)

        if store_kind == "mapping":
            store = _construct_mapping_store(store_path, password)
            prefix_fn = mapping_entries_prefix_digest
        else:
            store = _construct_identifier_store(store_path, password)
            prefix_fn = identifier_entries_prefix_digest
    finally:
        password = None  # noqa: F841 — зачистка сразу после открытия (успех или нет)

    entries = _sanitized_store_read(store.entries)
    actual_count = len(entries)

    if actual_count < entry_count:
        raise WorkspaceBindingError(WorkspaceBindingReason.STORE_REGRESSED)

    if actual_count == entry_count:
        if prefix_fn(entries) == manifest_state.prefix_digest:
            return StoreSyncState.CONSISTENT, store
        raise WorkspaceBindingError(WorkspaceBindingReason.STORE_DIVERGED)

    # actual_count > entry_count
    if prefix_fn(entries, entry_count) != manifest_state.prefix_digest:
        raise WorkspaceBindingError(WorkspaceBindingReason.STORE_DIVERGED)

    if pending:
        return StoreSyncState.AHEAD, store

    if entry_count == 0:
        raise WorkspaceBindingError(WorkspaceBindingReason.STORE_UNEXPECTED_ENTRIES)
    raise WorkspaceBindingError(WorkspaceBindingReason.STORE_AHEAD_UNMARKED)


def _verify_append_only(old_state: StoreState, new_state: StoreState, store, *, kind: str) -> None:
    if new_state.entry_count < old_state.entry_count:
        raise WorkspaceBindingError(WorkspaceBindingReason.APPEND_ONLY_VIOLATION)
    if old_state.entry_count == 0:
        return
    entries = _sanitized_store_read(store.entries)
    prefix_fn = mapping_entries_prefix_digest if kind == "mapping" else identifier_entries_prefix_digest
    if prefix_fn(entries, old_state.entry_count) != old_state.prefix_digest:
        raise WorkspaceBindingError(WorkspaceBindingReason.APPEND_ONLY_VIOLATION)


# ----------------------------------------------------------------------
# Stage 10B.4.1: валидация register_artifact (lineage/evidence/provenance)
#
# Каждый хелпер отвечает ровно на один вопрос и поднимает КОНКРЕТНЫЙ
# ArtifactRegistrationReason — генерическому ValueError модели
# (app.workspace.models.ArtifactRecord/_validate_lineage) здесь не
# доверяем как публичному контракту: он остаётся defense-in-depth при
# фактическом построении WorkspaceManifest в register_artifact.
# ----------------------------------------------------------------------


def _validate_registration_lineage(kind: ArtifactKind, parents: list, period: str) -> None:
    """
    Зеркалирует app.workspace.models._validate_lineage (frozen, Stage
    10B.1) — ДОЛЖНО оставаться синхронным с этой моделью. Дублируется
    здесь исключительно ради точного ArtifactRegistrationReason ДО
    попытки построить ArtifactRecord (сама модель тоже переисполнит эту
    проверку независимо при финальной сборке manifest).
    """
    kinds = tuple(parent.kind for parent in parents)
    monthly = ArtifactKind.ANONYMIZED_MONTHLY
    canonical = ArtifactKind.ANALYTICAL_CANONICAL

    if kind is monthly:
        if kinds != ():
            raise ArtifactRegistrationError(ArtifactRegistrationReason.INVALID_LINEAGE)
    elif kind is canonical:
        if kinds not in ((monthly,), (canonical,), (canonical, monthly)):
            raise ArtifactRegistrationError(ArtifactRegistrationReason.INVALID_LINEAGE)
        for parent in parents:
            if parent.kind is monthly:
                if parent.period != period:
                    raise ArtifactRegistrationError(ArtifactRegistrationReason.INVALID_LINEAGE)
            else:
                if parent.period > period:
                    raise ArtifactRegistrationError(ArtifactRegistrationReason.INVALID_LINEAGE)
    else:  # LOCAL_RESTORED
        if not (len(parents) == 1 and kinds[0] in (canonical, monthly)):
            raise ArtifactRegistrationError(ArtifactRegistrationReason.INVALID_LINEAGE)
        if parents[0].period != period:
            raise ArtifactRegistrationError(ArtifactRegistrationReason.INVALID_LINEAGE)


def _validate_evidence(kind: ArtifactKind, safety_report: object, actual_sha256: str) -> None:
    """
    ANONYMIZED_MONTHLY/ANALYTICAL_CANONICAL требуют SafetyReport (Stage
    10A), чей sha256 совпадает с фактическим файлом и без блокирующих
    findings. LOCAL_RESTORED НЕ допускает SafetyReport как evidence
    (файл заведомо содержит восстановленные реальные значения).

    Регистрация — НЕ финальная авторизация внешнего AI (Stage 10C):
    здесь проверяется только факт наличия непротиворечивого evidence на
    момент регистрации, не покрытие/policy/coverage.
    """
    if kind is ArtifactKind.LOCAL_RESTORED:
        if safety_report is not None:
            raise ArtifactRegistrationError(ArtifactRegistrationReason.EVIDENCE_NOT_ALLOWED)
        return

    if safety_report is None:
        raise ArtifactRegistrationError(ArtifactRegistrationReason.EVIDENCE_REQUIRED)
    if not isinstance(safety_report, SafetyReport):
        raise TypeError(f"safety_report должен быть SafetyReport, получено: {type(safety_report)!r}")
    if safety_report.sha256 != actual_sha256:
        raise ArtifactRegistrationError(ArtifactRegistrationReason.EVIDENCE_HASH_MISMATCH)
    if safety_report.has_blocking_findings:
        raise ArtifactRegistrationError(ArtifactRegistrationReason.EVIDENCE_BLOCKING_FINDINGS)


def _capture_provenance_snapshot(provenance_path: Path) -> tuple:
    """
    Correction Pass #1 (закрытие MINOR-1 Independent Review): захватывает
    НЕИЗМЕНЯЕМЫЙ снимок зашифрованных байт provenance-файла ОДНИМ чтением
    и немедленно вычисляет их SHA-256 от ЭТОГО ЖЕ буфера. Это единственный
    источник как provenance_sha256, так и байт, которые ниже будут
    провалидированы (через временную копию) на предмет job_id — то есть
    "что хэшировано" и "что провалидировано" гарантированно относятся к
    ОДНОМ И ТОМУ ЖЕ снимку, а не к двум независимым чтениям оригинального
    (потенциально мутирующего между чтениями) пути.

    Умышленно НЕ переиспользует sha256_file (тот читает файл заново
    отдельным потоковым проходом) — здесь необходимо хэшировать ИМЕННО
    те байты, что уже в памяти, без повторного обращения к диску.
    """
    failed = False
    try:
        snapshot = provenance_path.read_bytes()
    except OSError:
        failed = True
    if failed:
        raise ArtifactRegistrationError(ArtifactRegistrationReason.PROVENANCE_INVALID)
    digest = hashlib.sha256(snapshot).hexdigest()
    return snapshot, digest


def _read_job_id_from_snapshot(provenance_path: Path, snapshot: bytes, password: str) -> str:
    """
    Записывает ИММУТАБЕЛЬНЫЙ снимок зашифрованных байт (и ТОЛЬКО их —
    никогда plaintext) во временный файл РЯДОМ с оригиналом (тот же
    паттерн atomic-write, что и везде в проекте: секретное имя через
    tempfile.mkstemp, та же директория/файловая система), открывает его
    через уже существующий EncryptedFileProvenanceStore (без дублирования
    crypto/parsing-логики закрытого provenance-модуля) и возвращает
    job_id, прочитанный ИМЕННО из этого снимка — гарантируя, что job_id и
    SHA (см. _capture_provenance_snapshot) относятся к одним и тем же
    байтам. Временный файл удаляется в finally независимо от исхода;
    никогда не регистрируется как provenance workspace.

    OD-7 (Correction Pass #2, закрытие MAJOR-1/MINOR-1 Focused
    Re-Review): password — параметр этой функции, поэтому ВЕСЬ
    операционный путь — mkstemp, открытие fd, запись, flush, fsync,
    открытие снимка через EncryptedFileProvenanceStore — обёрнут ОДНИМ
    try/finally, зачищающим password, начиная с ПЕРВОЙ операции функции
    (mkstemp тоже способен поднять OSError ДО входа в защищённый блок —
    именно этот пробел давал утечку password через frame locals при
    сбое mkstemp). Любой сбой на пути подготовки снимка
    (mkstemp/fdopen/write/flush/fsync) транслируется в уже существующий
    ArtifactRegistrationError(PROVENANCE_INVALID) — сырой OSError никогда
    не покидает эту функцию. Флаг `failed` используется вместо
    непосредственного `raise` внутри `except`, чтобы финальный
    санитизированный `raise` происходил СНАРУЖИ активного except-блока
    (тот же приём, что и везде в OD-7-хелперах этого модуля) — итоговое
    исключение не получает `__context__` от перехваченного OSError.
    """
    tmp_path = None
    fd = None
    failed = False
    store = None
    try:
        try:
            directory = provenance_path.parent
            fd, tmp_name = tempfile.mkstemp(
                dir=str(directory), prefix=".provenance-snapshot-", suffix=".tmp"
            )
        except Exception:
            failed = True
        else:
            tmp_path = Path(tmp_name)

        if not failed:
            try:
                tmp_file = os.fdopen(fd, "wb")
            except Exception:
                try:
                    os.close(fd)
                except OSError:
                    pass
                failed = True
            else:
                fd = None  # noqa: F841 — владение handle перешло к tmp_file
                try:
                    with tmp_file:
                        tmp_file.write(snapshot)
                        tmp_file.flush()
                        os.fsync(tmp_file.fileno())
                except Exception:
                    failed = True

        if not failed:
            store = _construct_provenance_store_for_open(tmp_path, password)
    finally:
        password = None  # noqa: F841 — зачистка независимо от того, где произошёл сбой
        if tmp_path is not None:
            try:
                tmp_path.unlink(missing_ok=True)
            except OSError:
                pass

    if failed:
        raise ArtifactRegistrationError(ArtifactRegistrationReason.PROVENANCE_INVALID)

    job_id = store.job_id
    store = None  # noqa: F841 — зачистка (объект несёт _password)
    return job_id


def _validate_and_open_provenance(
    root: Path,
    password: str,
    kind: ArtifactKind,
    provenance_id: object,
    source_job_id: object,
) -> tuple:
    """
    Возвращает (provenance_id, provenance_sha256, source_job_id).

    Для НЕ-ANONYMIZED_MONTHLY: provenance_id/source_job_id обязаны быть
    None (PROVENANCE_NOT_ALLOWED/JOB_ID_INVALID иначе), результат — три
    None.

    Для ANONYMIZED_MONTHLY: provenance_id/source_job_id обязаны быть
    заданы и иметь формат 32 lowercase hex; provenance sidecar по
    производному пути обязан существовать, быть обычным non-reparse
    файлом. Захватывается ОДИН неизменяемый снимок его зашифрованных
    байт (_capture_provenance_snapshot); provenance_sha256 вычисляется
    ИЗ ЭТОГО снимка, и job_id читается (через временную копию ТЕХ ЖЕ
    байт) ТОЖЕ ИЗ ЭТОГО снимка — Correction Pass #1 закрывает возможность
    зарегистрировать ArtifactRecord, где провалидированный job_id
    относится к одной версии файла, а provenance_sha256 — к другой.
    provenance_sha256 Workspace ВСЕГДА вычисляет САМА — не принимает её
    на доверии от вызывающего кода (у register_artifact нет такого
    параметра вовсе).
    """
    # OD-7: весь остаток функции держит `password` живым локалом кадра —
    # ЛЮБОЙ raise ниже (включая ранние, до какого-либо использования
    # password) обязан происходить внутри try/finally, зачищающего его,
    # иначе пароль остаётся достижим через traceback этого кадра (тот же
    # класс дефекта, что был найден и закрыт в _diagnose_store, Stage
    # 10B.3 Correction Pass).
    try:
        if kind is not ArtifactKind.ANONYMIZED_MONTHLY:
            if provenance_id is not None:
                raise ArtifactRegistrationError(ArtifactRegistrationReason.PROVENANCE_NOT_ALLOWED)
            if source_job_id is not None:
                raise ArtifactRegistrationError(ArtifactRegistrationReason.JOB_ID_INVALID)
            return None, None, None

        if provenance_id is None:
            raise ArtifactRegistrationError(ArtifactRegistrationReason.PROVENANCE_REQUIRED)
        if not _is_hex32(provenance_id):
            raise ArtifactRegistrationError(ArtifactRegistrationReason.PROVENANCE_INVALID)
        if not _is_hex32(source_job_id):
            raise ArtifactRegistrationError(ArtifactRegistrationReason.JOB_ID_INVALID)

        provenance_path = _provenance_path(root, provenance_id)
        if (
            _is_reparse_like(provenance_path)
            or not provenance_path.exists()
            or not provenance_path.is_file()
        ):
            raise ArtifactRegistrationError(ArtifactRegistrationReason.PROVENANCE_INVALID)

        snapshot, provenance_sha256 = _capture_provenance_snapshot(provenance_path)
        try:
            actual_job_id = _read_job_id_from_snapshot(provenance_path, snapshot, password)
        finally:
            snapshot = None  # noqa: F841 — зачистка (зашифрованный, но незачем задерживать в памяти)
    finally:
        password = None  # noqa: F841 — зачистка сразу после открытия (успех или нет)

    if actual_job_id != source_job_id:
        raise ArtifactRegistrationError(ArtifactRegistrationReason.JOB_ID_INVALID)

    return provenance_id, provenance_sha256, source_job_id


# ----------------------------------------------------------------------
# Read-only reader façades (OD-4) — структурно без методов мутации.
# ----------------------------------------------------------------------


class MappingStoreReader:
    """Read-only обёртка над mapping store. Не имеет add/add_many/clear."""

    __slots__ = ("_store",)

    def __init__(self, store: EncryptedFileMappingStore) -> None:
        self._store = store

    def get_by_alias(self, alias: str) -> Optional[MappingEntry]:
        return _sanitized_store_read(self._store.get_by_alias, alias)

    def get_by_real_value(
        self,
        real_value: str,
        entity_type: Optional[EntityType] = None,
        parent_alias: _ParentAliasArg = _PARENT_UNSET,
    ) -> Optional[MappingEntry]:
        return _sanitized_store_read(
            self._store.get_by_real_value, real_value, entity_type, parent_alias
        )

    def contains_alias(self, alias: str) -> bool:
        return _sanitized_store_read(self._store.contains_alias, alias)

    def all_aliases(self) -> set:
        return _sanitized_store_read(self._store.all_aliases)

    def entries(self) -> tuple:
        return _sanitized_store_read(self._store.entries)


class IdentifierMappingStoreReader:
    """Read-only обёртка над identifier store. Не имеет add/add_many/clear."""

    __slots__ = ("_store",)

    def __init__(self, store: EncryptedFileIdentifierMappingStore) -> None:
        self._store = store

    def get_by_token(self, token: str) -> Optional[IdentifierMappingEntry]:
        return _sanitized_store_read(self._store.get_by_token, token)

    def get_by_identity(
        self, identifier_type: IdentifierType, identifier_value: str
    ) -> Optional[IdentifierMappingEntry]:
        return _sanitized_store_read(self._store.get_by_identity, identifier_type, identifier_value)

    def all_tokens(self) -> set:
        return _sanitized_store_read(self._store.all_tokens)

    def entries(self) -> tuple:
        return _sanitized_store_read(self._store.entries)


# ----------------------------------------------------------------------
# Restricted append-only mutation façades (OD-3) — недействительны вне
# активного store_mutation().
# ----------------------------------------------------------------------

_FACADE_INACTIVE_MESSAGE = "Mutation-объект store недействителен вне блока store_mutation()"


class MappingMutationFacade:
    """
    Ограниченный append-only façade для mapping store внутри
    `Workspace.store_mutation()`. Не экспонирует clear() — Workspace
    мутация обязана быть append-only (OD-3). Инвалидируется сразу по
    выходу из `with` (успех/исключение тела/что угодно ещё) — любой
    вызов после этого поднимает WorkspaceInputError с фиксированным
    безопасным сообщением, не раскрывающим внутреннее состояние.
    """

    __slots__ = ("_store", "_active")

    def __init__(self, store: EncryptedFileMappingStore) -> None:
        self._store = store
        self._active = True

    def _require_active(self) -> None:
        if not self._active:
            raise WorkspaceInputError(_FACADE_INACTIVE_MESSAGE)

    def _deactivate(self) -> None:
        self._active = False
        self._store = None  # noqa: F841 — убираем ссылку на store (несёт _password)

    def add(self, entry: MappingEntry) -> None:
        self._require_active()
        _mapping_store_add(self._store, entry)

    def get_by_alias(self, alias: str) -> Optional[MappingEntry]:
        self._require_active()
        return _sanitized_store_read(self._store.get_by_alias, alias)

    def get_by_real_value(
        self,
        real_value: str,
        entity_type: Optional[EntityType] = None,
        parent_alias: _ParentAliasArg = _PARENT_UNSET,
    ) -> Optional[MappingEntry]:
        self._require_active()
        return _sanitized_store_read(
            self._store.get_by_real_value, real_value, entity_type, parent_alias
        )

    def contains_alias(self, alias: str) -> bool:
        self._require_active()
        return _sanitized_store_read(self._store.contains_alias, alias)

    def all_aliases(self) -> set:
        self._require_active()
        return _sanitized_store_read(self._store.all_aliases)

    def entries(self) -> tuple:
        self._require_active()
        return _sanitized_store_read(self._store.entries)


class IdentifierMutationFacade:
    """Аналог MappingMutationFacade для identifier store."""

    __slots__ = ("_store", "_active")

    def __init__(self, store: EncryptedFileIdentifierMappingStore) -> None:
        self._store = store
        self._active = True

    def _require_active(self) -> None:
        if not self._active:
            raise WorkspaceInputError(_FACADE_INACTIVE_MESSAGE)

    def _deactivate(self) -> None:
        self._active = False
        self._store = None  # noqa: F841

    def add(self, entry: IdentifierMappingEntry) -> None:
        self._require_active()
        _identifier_store_add(self._store, entry)

    def add_many(self, entries) -> None:
        self._require_active()
        _identifier_store_add_many(self._store, tuple(entries))

    def get_by_token(self, token: str) -> Optional[IdentifierMappingEntry]:
        self._require_active()
        return _sanitized_store_read(self._store.get_by_token, token)

    def get_by_identity(
        self, identifier_type: IdentifierType, identifier_value: str
    ) -> Optional[IdentifierMappingEntry]:
        self._require_active()
        return _sanitized_store_read(self._store.get_by_identity, identifier_type, identifier_value)

    def all_tokens(self) -> set:
        self._require_active()
        return _sanitized_store_read(self._store.all_tokens)

    def entries(self) -> tuple:
        self._require_active()
        return _sanitized_store_read(self._store.entries)


# ----------------------------------------------------------------------
# Workspace
# ----------------------------------------------------------------------


class Workspace:
    """
    Живой объект открытого/созданного workspace. Держит один
    `WorkspaceFileLock` на весь свой жизненный цикл (nested acquisition
    между `lock()`/`store_mutation()`/`recover_store_state()` внутри
    одного объекта корректно даёт `WorkspaceLockedError(NESTED_ACQUISITION)`
    — см. Stage10B.2 `WorkspaceFileLock`).
    """

    def __init__(
        self,
        root: Path,
        password: str,
        manifest: WorkspaceManifest,
        mapping_sync: StoreSyncState,
        identifier_sync: StoreSyncState,
    ) -> None:
        self._root = root
        self._password = password
        self._manifest = manifest
        self._file_lock = WorkspaceFileLock(_workspace_lock_path(root))
        self.mapping_sync = mapping_sync
        self.identifier_sync = identifier_sync

    @property
    def root(self) -> Path:
        return self._root

    @property
    def recovery_required(self) -> bool:
        return self._manifest.pending_store_mutation

    @property
    def info(self) -> WorkspaceInfo:
        m = self._manifest
        return WorkspaceInfo(
            workspace_id=m.workspace_id,
            label=m.label,
            created_at=m.created_at,
            revision=m.revision,
            artifact_count=len(m.artifacts),
            latest_analytical_artifact_id=m.latest_analytical_artifact_id,
            mapping_entry_count=m.mapping_state.entry_count,
            identifier_entry_count=m.identifier_state.entry_count,
        )

    # ------------------------------------------------------------------
    # Lock
    # ------------------------------------------------------------------

    @contextlib.contextmanager
    def lock(self) -> Iterator[None]:
        self._file_lock.acquire()
        try:
            yield
        finally:
            self._file_lock.release()

    # ------------------------------------------------------------------
    # Read-only доступ (OD-4)
    # ------------------------------------------------------------------

    def read_stores(self) -> tuple[MappingStoreReader, IdentifierMappingStoreReader]:
        """
        Открывает СВЕЖИЕ read-only reader'ы поверх текущего persisted
        состояния (не требует lock — atomic replace на уровне ФС уже
        гарантирует, что читатель никогда не увидит частично записанный
        файл; см. Stage 10B.2). Обновляет кэшированные
        manifest/mapping_sync/identifier_sync объекта.
        """
        root = self._root
        password = self._password
        try:
            manifest = load_encrypted_manifest(_workspace_enc_path(root), password)
            mapping_sync, mapping_store = _diagnose_store(
                manifest.mapping_state,
                _mapping_store_path(root),
                password,
                manifest.pending_store_mutation,
                store_kind="mapping",
            )
            identifier_sync, identifier_store = _diagnose_store(
                manifest.identifier_state,
                _identifier_store_path(root),
                password,
                manifest.pending_store_mutation,
                store_kind="identifier",
            )
        finally:
            password = None  # noqa: F841

        self._manifest = manifest
        self.mapping_sync = mapping_sync
        self.identifier_sync = identifier_sync
        return MappingStoreReader(mapping_store), IdentifierMappingStoreReader(identifier_store)

    # ------------------------------------------------------------------
    # Контролируемая мутация (OD-3)
    # ------------------------------------------------------------------

    @contextlib.contextmanager
    def store_mutation(self) -> Iterator[tuple[MappingMutationFacade, IdentifierMutationFacade]]:
        self._file_lock.acquire()
        try:
            root = self._root
            password = self._password

            manifest = load_encrypted_manifest(_workspace_enc_path(root), password)
            if manifest.pending_store_mutation:
                raise WorkspaceBindingError(WorkspaceBindingReason.RECOVERY_REQUIRED)

            mapping_sync, mapping_store = _diagnose_store(
                manifest.mapping_state,
                _mapping_store_path(root),
                password,
                manifest.pending_store_mutation,
                store_kind="mapping",
            )
            identifier_sync, identifier_store = _diagnose_store(
                manifest.identifier_state,
                _identifier_store_path(root),
                password,
                manifest.pending_store_mutation,
                store_kind="identifier",
            )

            old_mapping_state = manifest.mapping_state
            old_identifier_state = manifest.identifier_state

            pending_manifest = dataclasses.replace(
                manifest,
                revision=manifest.revision + 1,
                pending_store_mutation=True,
            )
            save_encrypted_manifest_atomic(_workspace_enc_path(root), pending_manifest, password)
            self._manifest = pending_manifest

            mapping_facade = MappingMutationFacade(mapping_store)
            identifier_facade = IdentifierMutationFacade(identifier_store)
            try:
                yield mapping_facade, identifier_facade
            finally:
                mapping_facade._deactivate()
                identifier_facade._deactivate()

            # Сюда попадаем, только если тело `with` завершилось БЕЗ
            # исключения (иначе finally выше пробросил бы его дальше,
            # минуя код ниже — pending остаётся True на диске).
            new_mapping_state = _compute_mapping_state(mapping_store)
            new_identifier_state = _compute_identifier_state(identifier_store)

            _verify_append_only(old_mapping_state, new_mapping_state, mapping_store, kind="mapping")
            _verify_append_only(
                old_identifier_state, new_identifier_state, identifier_store, kind="identifier"
            )

            final_manifest = dataclasses.replace(
                pending_manifest,
                revision=pending_manifest.revision + 1,
                pending_store_mutation=False,
                mapping_state=new_mapping_state,
                identifier_state=new_identifier_state,
            )
            save_encrypted_manifest_atomic(_workspace_enc_path(root), final_manifest, password)
            self._manifest = final_manifest
            self.mapping_sync = StoreSyncState.CONSISTENT
            self.identifier_sync = StoreSyncState.CONSISTENT
        finally:
            password = None  # noqa: F841
            self._file_lock.release()

    # ------------------------------------------------------------------
    # Явное восстановление (Stage 10B.2 NOT auto-triggered by open)
    # ------------------------------------------------------------------

    def recover_store_state(self) -> StoreRecoveryResult:
        self._file_lock.acquire()
        try:
            root = self._root
            password = self._password

            manifest = load_encrypted_manifest(_workspace_enc_path(root), password)
            if not manifest.pending_store_mutation:
                raise WorkspaceInputError(
                    "Recovery не требуется: pending_store_mutation уже False"
                )

            mapping_sync, mapping_store = _diagnose_store(
                manifest.mapping_state,
                _mapping_store_path(root),
                password,
                True,
                store_kind="mapping",
            )
            identifier_sync, identifier_store = _diagnose_store(
                manifest.identifier_state,
                _identifier_store_path(root),
                password,
                True,
                store_kind="identifier",
            )

            new_mapping_state = _compute_mapping_state(mapping_store)
            new_identifier_state = _compute_identifier_state(identifier_store)

            mapping_adopted = new_mapping_state.entry_count - manifest.mapping_state.entry_count
            identifier_adopted = (
                new_identifier_state.entry_count - manifest.identifier_state.entry_count
            )

            final_manifest = dataclasses.replace(
                manifest,
                revision=manifest.revision + 1,
                pending_store_mutation=False,
                mapping_state=new_mapping_state,
                identifier_state=new_identifier_state,
            )
            save_encrypted_manifest_atomic(_workspace_enc_path(root), final_manifest, password)
            self._manifest = final_manifest
            self.mapping_sync = StoreSyncState.CONSISTENT
            self.identifier_sync = StoreSyncState.CONSISTENT

            return StoreRecoveryResult(
                mapping_entries_adopted=mapping_adopted,
                identifier_entries_adopted=identifier_adopted,
            )
        finally:
            password = None  # noqa: F841
            self._file_lock.release()

    # ------------------------------------------------------------------
    # Stage 10B.4.1: artifact allocation / staging / registration
    # ------------------------------------------------------------------

    def allocate_artifact_path(self, kind: ArtifactKind) -> ArtifactSlot:
        """
        Резервирует структурно допустимое место для потенциального
        артефакта — ТОЛЬКО путь+ID, никакого файла не создаёт, manifest
        не мутирует, лок не требуется (artifact_id — secrets.token_hex(16),
        коллизия с уже существующим файлом практически невозможна при
        128 битах энтропии; retry на явную проверку — defense-in-depth,
        не защита от реальной угрозы).
        """
        if not isinstance(kind, ArtifactKind):
            raise WorkspaceInputError("kind должен быть ArtifactKind")

        root = self._root
        _reject_unsafe_dir(_artifact_dir_for_kind(root, kind))

        for _ in range(_ALLOCATION_MAX_ATTEMPTS):
            artifact_id = secrets.token_hex(16)
            path = _artifact_path(root, kind, artifact_id)
            if not path.exists():
                return ArtifactSlot(artifact_id=artifact_id, kind=kind, path=path)
        raise WorkspaceInputError(
            "не удалось выделить уникальный artifact_id за отведённое число попыток"
        )

    def stage_analytical_file(self, external_path: Union[str, Path]) -> ArtifactSlot:
        """
        Копирует ВНЕШНИЙ (например, обновлённый внешним AI) workbook в
        свежий кандидатный слот ANALYTICAL_CANONICAL. Staging НЕ означает
        "доверенный/безопасный/зарегистрированный" — только физическое
        перемещение байт в допустимое место; source остаётся неизменным
        и не должен находиться внутри workspace root.
        """
        external = _validate_path_like_argument(external_path, name="external_path")
        if _is_reparse_like(external) or not external.exists() or not external.is_file():
            raise WorkspaceInputError("external_path должен быть обычным существующим файлом")
        if external.suffix.lower() != _ARTIFACT_EXTENSION:
            raise WorkspaceInputError(f"external_path должен иметь расширение {_ARTIFACT_EXTENSION}")
        _reject_external_artifact_path_inside(external, self._root)

        slot = self.allocate_artifact_path(ArtifactKind.ANALYTICAL_CANONICAL)
        _atomic_copy_file(external, slot.path)
        return slot

    def register_artifact(
        self,
        slot: ArtifactSlot,
        *,
        period: str,
        parent_artifact_ids: tuple = (),
        source_job_id: Optional[str] = None,
        provenance_id: Optional[str] = None,
        safety_report: Optional[SafetyReport] = None,
    ) -> ArtifactRecord:
        """
        Единственная security boundary регистрации артефакта. Держит
        лок, перечитывает manifest, отклоняет recovery_required,
        пересчитывает РЕАЛЬНЫЙ SHA-256 файла (не доверяет allocation-
        времени), проверяет глобальную уникальность SHA, lineage,
        evidence по kind, provenance-привязку (ANONYMIZED_MONTHLY).
        Registered ≠ авторизован для внешнего AI (Stage 10C).
        """
        if not isinstance(slot, ArtifactSlot):
            raise WorkspaceInputError("slot должен быть ArtifactSlot")
        if not isinstance(parent_artifact_ids, tuple) or not all(
            isinstance(parent_id, str) for parent_id in parent_artifact_ids
        ):
            raise WorkspaceInputError("parent_artifact_ids должен быть tuple[str, ...]")

        self._file_lock.acquire()
        try:
            root = self._root
            password = self._password
            try:
                # Перечитываем АКТУАЛЬНЫЙ manifest под локом — та же
                # дисциплина против stale Workspace объектов, что и
                # store_mutation()/recover_store_state() (Stage 10B.3).
                manifest = load_encrypted_manifest(_workspace_enc_path(root), password)
                if manifest.pending_store_mutation:
                    raise WorkspaceBindingError(WorkspaceBindingReason.RECOVERY_REQUIRED)

                expected_path = _artifact_path(root, slot.kind, slot.artifact_id)
                if slot.path != expected_path:
                    raise ArtifactRegistrationError(ArtifactRegistrationReason.SLOT_INVALID)
                if (
                    _is_reparse_like(slot.path)
                    or not slot.path.exists()
                    or not slot.path.is_file()
                ):
                    raise ArtifactRegistrationError(ArtifactRegistrationReason.FILE_INVALID)

                actual_sha256 = sha256_file(slot.path)
                existing_sha256 = {record.sha256 for record in manifest.artifacts}
                if actual_sha256 in existing_sha256:
                    raise ArtifactRegistrationError(ArtifactRegistrationReason.DUPLICATE_SHA256)

                if not _is_period(period):
                    raise ArtifactRegistrationError(ArtifactRegistrationReason.INVALID_PERIOD)

                if len(set(parent_artifact_ids)) != len(parent_artifact_ids):
                    raise ArtifactRegistrationError(ArtifactRegistrationReason.INVALID_LINEAGE)
                if slot.artifact_id in parent_artifact_ids:
                    raise ArtifactRegistrationError(ArtifactRegistrationReason.INVALID_LINEAGE)

                by_id = {record.artifact_id: record for record in manifest.artifacts}
                parents = []
                for parent_id in parent_artifact_ids:
                    if parent_id not in by_id:
                        raise ArtifactRegistrationError(ArtifactRegistrationReason.INVALID_LINEAGE)
                    parents.append(by_id[parent_id])

                _validate_registration_lineage(slot.kind, parents, period)
                _validate_evidence(slot.kind, safety_report, actual_sha256)
                (
                    final_provenance_id,
                    final_provenance_sha256,
                    final_source_job_id,
                ) = _validate_and_open_provenance(
                    root, password, slot.kind, provenance_id, source_job_id
                )

                record = ArtifactRecord(
                    artifact_id=slot.artifact_id,
                    kind=slot.kind,
                    sha256=actual_sha256,
                    created_at=_now_timestamp(),
                    parent_artifact_ids=parent_artifact_ids,
                    period=period,
                    source_job_id=final_source_job_id,
                    provenance_id=final_provenance_id,
                    provenance_sha256=final_provenance_sha256,
                )
                new_manifest = dataclasses.replace(
                    manifest,
                    revision=manifest.revision + 1,
                    artifacts=manifest.artifacts + (record,),
                )
                save_encrypted_manifest_atomic(_workspace_enc_path(root), new_manifest, password)
            finally:
                password = None  # noqa: F841
            self._manifest = new_manifest
            return record
        finally:
            self._file_lock.release()

    # ------------------------------------------------------------------
    # Stage 10B.4.1: read-only доступ к реестру артефактов
    # ------------------------------------------------------------------

    def get_artifact(self, artifact_id: str) -> ArtifactRecord:
        if not isinstance(artifact_id, str):
            raise WorkspaceInputError("artifact_id должен быть str")

        root = self._root
        password = self._password
        try:
            manifest = load_encrypted_manifest(_workspace_enc_path(root), password)
        finally:
            password = None  # noqa: F841
        self._manifest = manifest

        for record in manifest.artifacts:
            if record.artifact_id == artifact_id:
                return record
        raise ArtifactNotFoundError()

    def list_artifacts(
        self, *, kind: Optional[ArtifactKind] = None, period: Optional[str] = None
    ) -> tuple:
        if kind is not None and not isinstance(kind, ArtifactKind):
            raise WorkspaceInputError("kind должен быть ArtifactKind")
        if period is not None and not isinstance(period, str):
            raise WorkspaceInputError("period должен быть str")

        root = self._root
        password = self._password
        try:
            manifest = load_encrypted_manifest(_workspace_enc_path(root), password)
        finally:
            password = None  # noqa: F841
        self._manifest = manifest

        return tuple(
            record
            for record in manifest.artifacts
            if (kind is None or record.kind is kind) and (period is None or record.period == period)
        )


# ----------------------------------------------------------------------
# create_workspace
# ----------------------------------------------------------------------


def create_workspace(
    root: Union[str, Path], password: str, *, label: Optional[str] = None
) -> Workspace:
    try:
        root_path = _validate_path_like_argument(root, name="root")
        _validate_creation_password(password)

        _ensure_clean_skeleton_or_absent(root_path)

        # root должен существовать ДО попытки создать workspace.lock
        # (WorkspaceFileLock._open(create=True) требует существующий
        # родительский каталог).
        root_path.mkdir(parents=True, exist_ok=True)

        file_lock = WorkspaceFileLock(_workspace_lock_path(root_path))
        file_lock.acquire(create=True)
        try:
            # POST-LOCK ревалидация (Correction Pass #2, закрытие BLOCKER-1
            # Independent Review): между pre-lock проверкой выше и
            # захватом лока другой логический creator/adopter мог успеть
            # ПОЛНОСТЬЮ инициализировать workspace на этом же root (лок —
            # персистентный, эксклюзивен только для ОДНОВРЕМЕННОГО
            # доступа, но не защищает от повторного использования уже
            # отработавшего лока другим создателем). Поэтому полную
            # skeleton-проверку выполняем ЕЩЁ РАЗ, уже держа эксклюзивный
            # лок — только она авторитетна. Собственный только что
            # созданный/открытый workspace.lock — легальный член чистого
            # skeleton (0-байтный файл), поэтому не мешает "победителю".
            _ensure_clean_skeleton_or_absent(root_path)

            _ensure_structural_subdirs(root_path)

            manifest = WorkspaceManifest(
                schema_version=MANIFEST_SCHEMA_VERSION,
                workspace_id=secrets.token_hex(16),
                label=label,
                created_at=_now_timestamp(),
                revision=1,
                pending_store_mutation=False,
                mapping_state=EMPTY_MAPPING_STATE,
                identifier_state=EMPTY_IDENTIFIER_STATE,
                artifacts=(),
                latest_analytical_artifact_id=None,
            )
            save_encrypted_manifest_atomic(_workspace_enc_path(root_path), manifest, password)
        finally:
            file_lock.release()

        return Workspace(root_path, password, manifest, StoreSyncState.CONSISTENT, StoreSyncState.CONSISTENT)
    finally:
        password = None  # noqa: F841


# ----------------------------------------------------------------------
# open_workspace
# ----------------------------------------------------------------------


def open_workspace(root: Union[str, Path], password: str) -> Workspace:
    try:
        root_path = _validate_path_like_argument(root, name="root")
        _validate_open_password(password)

        if not root_path.exists():
            raise WorkspaceNotFoundError()
        _reject_unsafe_dir(root_path)
        _validate_layout_safety(root_path)

        file_lock = WorkspaceFileLock(_workspace_lock_path(root_path))
        file_lock.acquire(create=False)
        try:
            manifest = load_encrypted_manifest(_workspace_enc_path(root_path), password)

            mapping_sync, _ = _diagnose_store(
                manifest.mapping_state,
                _mapping_store_path(root_path),
                password,
                manifest.pending_store_mutation,
                store_kind="mapping",
            )
            identifier_sync, _ = _diagnose_store(
                manifest.identifier_state,
                _identifier_store_path(root_path),
                password,
                manifest.pending_store_mutation,
                store_kind="identifier",
            )
        finally:
            file_lock.release()

        return Workspace(root_path, password, manifest, mapping_sync, identifier_sync)
    finally:
        password = None  # noqa: F841


# ----------------------------------------------------------------------
# adopt_stores
# ----------------------------------------------------------------------


def adopt_stores(
    root: Union[str, Path],
    password: str,
    *,
    mapping_store_path: Union[str, Path],
    mapping_store_password: str,
    identifier_store_path: Optional[Union[str, Path]] = None,
    identifier_store_password: Optional[str] = None,
    label: Optional[str] = None,
) -> Workspace:
    try:
        root_path = _validate_path_like_argument(root, name="root")
        _validate_creation_password(password)

        mapping_source = _validate_path_like_argument(mapping_store_path, name="mapping_store_path")
        _validate_source_password_argument(mapping_store_password, name="mapping_store_password")

        identifier_source: Optional[Path] = None
        if identifier_store_path is not None:
            identifier_source = _validate_path_like_argument(
                identifier_store_path, name="identifier_store_path"
            )
            _validate_source_password_argument(
                identifier_store_password, name="identifier_store_password"
            )

        if not mapping_source.exists():
            raise WorkspaceInputError("mapping_store_path не существует")
        if identifier_source is not None and not identifier_source.exists():
            raise WorkspaceInputError("identifier_store_path не существует")
        if identifier_source is not None and mapping_source.resolve() == identifier_source.resolve():
            raise WorkspaceInputError(
                "mapping_store_path и identifier_store_path не могут быть одним файлом"
            )

        _reject_path_inside(mapping_source, root_path)
        if identifier_source is not None:
            _reject_path_inside(identifier_source, root_path)

        # Открываем и читаем ИСТОЧНИКИ под их СОБСТВЕННЫМИ паролями —
        # исходные файлы никогда не изменяются (только entries()-чтение).
        source_mapping = _construct_mapping_store(mapping_source, mapping_store_password)
        source_mapping_entries = _sanitized_store_read(source_mapping.entries)

        source_identifier_entries: tuple = ()
        if identifier_source is not None:
            source_identifier = _construct_identifier_store(
                identifier_source, identifier_store_password
            )
            source_identifier_entries = _sanitized_store_read(source_identifier.entries)

        _ensure_clean_skeleton_or_absent(root_path)
        root_path.mkdir(parents=True, exist_ok=True)

        file_lock = WorkspaceFileLock(_workspace_lock_path(root_path))
        file_lock.acquire(create=True)
        try:
            # POST-LOCK ревалидация — тот же приём и то же обоснование,
            # что и в create_workspace (Correction Pass #2, BLOCKER-1):
            # авторитетна только проверка ПОД эксклюзивным локом.
            _ensure_clean_skeleton_or_absent(root_path)

            _ensure_structural_subdirs(root_path)

            target_mapping = _construct_mapping_store(_mapping_store_path(root_path), password)
            for entry in source_mapping_entries:
                _mapping_store_add(target_mapping, entry)

            if source_identifier_entries:
                target_identifier = _construct_identifier_store(
                    _identifier_store_path(root_path), password
                )
                _identifier_store_add_many(target_identifier, source_identifier_entries)

            # Верификация (Correction Pass #2, MINOR-1: усилено с
            # count-only до полной семантики): переоткрыть target ПОД
            # WORKSPACE-паролем и сверить (а) полную упорядоченную
            # последовательность записей поэлементно (MappingEntry/
            # IdentifierMappingEntry — frozen-датаклассы со структурным
            # равенством по ВСЕМ полям, порядок кортежа значим) против
            # прочитанных из source, и (б) digest пересчитанных target-
            # записей против digest, ожидаемого от source-последовательности
            # (доп. проверка устойчивости persist/порядка, не замена
            # entry-equality). Сравнение count было бы недостаточным:
            # совпадение длины не доказывает совпадение alias/real_value/
            # entity_type/parent_alias или порядка. Не доверяем RAM-
            # состоянию сразу после записи.
            reopened_mapping = _construct_mapping_store(_mapping_store_path(root_path), password)
            reopened_mapping_entries = _sanitized_store_read(reopened_mapping.entries)
            expected_mapping_digest = mapping_entries_prefix_digest(source_mapping_entries)
            actual_mapping_digest = mapping_entries_prefix_digest(reopened_mapping_entries)
            if (
                reopened_mapping_entries != source_mapping_entries
                or actual_mapping_digest != expected_mapping_digest
            ):
                # Намеренно НЕ включаем сами записи/значения в исключение
                # (§12 Correction Pass #2) — только фиксированная причина.
                raise WorkspaceCorruptedError(WorkspaceCorruptedReason.ADOPT_VERIFICATION_FAILED)
            mapping_state = StoreState(
                entry_count=len(reopened_mapping_entries),
                prefix_digest=actual_mapping_digest,
            )

            identifier_state = EMPTY_IDENTIFIER_STATE
            if source_identifier_entries:
                reopened_identifier = _construct_identifier_store(
                    _identifier_store_path(root_path), password
                )
                reopened_identifier_entries = _sanitized_store_read(reopened_identifier.entries)
                expected_identifier_digest = identifier_entries_prefix_digest(source_identifier_entries)
                actual_identifier_digest = identifier_entries_prefix_digest(reopened_identifier_entries)
                if (
                    reopened_identifier_entries != source_identifier_entries
                    or actual_identifier_digest != expected_identifier_digest
                ):
                    raise WorkspaceCorruptedError(WorkspaceCorruptedReason.ADOPT_VERIFICATION_FAILED)
                identifier_state = StoreState(
                    entry_count=len(reopened_identifier_entries),
                    prefix_digest=actual_identifier_digest,
                )

            manifest = WorkspaceManifest(
                schema_version=MANIFEST_SCHEMA_VERSION,
                workspace_id=secrets.token_hex(16),
                label=label,
                created_at=_now_timestamp(),
                revision=1,
                pending_store_mutation=False,
                mapping_state=mapping_state,
                identifier_state=identifier_state,
                artifacts=(),
                latest_analytical_artifact_id=None,
            )
            save_encrypted_manifest_atomic(_workspace_enc_path(root_path), manifest, password)
        finally:
            file_lock.release()

        return Workspace(
            root_path, password, manifest, StoreSyncState.CONSISTENT, StoreSyncState.CONSISTENT
        )
    finally:
        password = None  # noqa: F841
        mapping_store_password = None  # noqa: F841
        identifier_store_password = None  # noqa: F841
