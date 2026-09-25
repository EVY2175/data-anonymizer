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
"""

from __future__ import annotations

import contextlib
import dataclasses
import secrets
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
from app.models.entities import EntityType, MappingEntry
from app.models.identifiers import IdentifierMappingEntry, IdentifierType
from app.workspace.errors import (
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
