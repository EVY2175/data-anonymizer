"""
Ошибки Workspace-слоя (Stage 10B.1).

Все сообщения фиксированные: они никогда не содержат пароль, значения
ячеек, alias/token, идентификаторы, названия листов, repr сторов, текст
исключений нижних слоёв. Единственное исключение из этого правила —
WorkspaceInputError: вызывающий код сам передаёт сообщение и отвечает за
то, чтобы в нём был только безопасный путь или суффикс.

Модуль чисто декларативный: он не выполняет ввод-вывод и ничего не
оборачивает — обёртывание ошибок нижних слоёв появится вместе с
соответствующими операциями в следующих slice'ах.
"""

from __future__ import annotations

import enum

# ----------------------------------------------------------------------
# Reason-enum'ы
# ----------------------------------------------------------------------


class WorkspaceCorruptedReason(enum.Enum):
    MANIFEST_INVALID_CONTAINER = "manifest_invalid_container"
    MANIFEST_INVALID_PAYLOAD = "manifest_invalid_payload"
    MANIFEST_UNSUPPORTED_VERSION = "manifest_unsupported_version"
    LAYOUT_INCOMPLETE = "layout_incomplete"
    UNSAFE_LAYOUT = "unsafe_layout"
    LOCK_FILE_MISSING = "lock_file_missing"
    BACKUP_INVALID = "backup_invalid"
    BACKUP_FOREIGN = "backup_foreign"
    ADOPT_VERIFICATION_FAILED = "adopt_verification_failed"


class WorkspaceBindingReason(enum.Enum):
    STORE_MISSING = "store_missing"
    STORE_UNOPENABLE = "store_unopenable"
    STORE_REGRESSED = "store_regressed"
    STORE_DIVERGED = "store_diverged"
    STORE_UNEXPECTED_ENTRIES = "store_unexpected_entries"
    STORE_AHEAD_UNMARKED = "store_ahead_unmarked"
    RECOVERY_REQUIRED = "recovery_required"
    APPEND_ONLY_VIOLATION = "append_only_violation"


class WorkspaceLockedReason(enum.Enum):
    HELD_BY_OTHER = "held_by_other"
    NESTED_ACQUISITION = "nested_acquisition"
    NOT_HELD = "not_held"


class ArtifactRegistrationReason(enum.Enum):
    SLOT_INVALID = "slot_invalid"
    FILE_INVALID = "file_invalid"
    INVALID_PERIOD = "invalid_period"
    INVALID_LINEAGE = "invalid_lineage"
    DUPLICATE_SHA256 = "duplicate_sha256"
    EVIDENCE_REQUIRED = "evidence_required"
    EVIDENCE_NOT_ALLOWED = "evidence_not_allowed"
    EVIDENCE_HASH_MISMATCH = "evidence_hash_mismatch"
    EVIDENCE_BLOCKING_FINDINGS = "evidence_blocking_findings"
    JOB_ID_INVALID = "job_id_invalid"
    PROVENANCE_REQUIRED = "provenance_required"
    PROVENANCE_NOT_ALLOWED = "provenance_not_allowed"
    PROVENANCE_INVALID = "provenance_invalid"
    POINTER_KIND_INVALID = "pointer_kind_invalid"


# ----------------------------------------------------------------------
# Фиксированные сообщения
# ----------------------------------------------------------------------

_CORRUPTED_MESSAGES = {
    WorkspaceCorruptedReason.MANIFEST_INVALID_CONTAINER: "Контейнер manifest повреждён или имеет недопустимую структуру",
    WorkspaceCorruptedReason.MANIFEST_INVALID_PAYLOAD: "Содержимое manifest не соответствует схеме",
    WorkspaceCorruptedReason.MANIFEST_UNSUPPORTED_VERSION: "Версия схемы manifest не поддерживается",
    WorkspaceCorruptedReason.LAYOUT_INCOMPLETE: "Раскладка каталогов workspace неполна",
    WorkspaceCorruptedReason.UNSAFE_LAYOUT: "Раскладка workspace содержит недопустимые элементы",
    WorkspaceCorruptedReason.LOCK_FILE_MISSING: "Отсутствует файл workspace.lock",
    WorkspaceCorruptedReason.BACKUP_INVALID: "Набор backup недействителен",
    WorkspaceCorruptedReason.BACKUP_FOREIGN: "Набор backup принадлежит другому workspace",
    WorkspaceCorruptedReason.ADOPT_VERIFICATION_FAILED: "Проверка перенесённого стора не пройдена",
}

_BINDING_MESSAGES = {
    WorkspaceBindingReason.STORE_MISSING: "Файл стора отсутствует, хотя manifest ожидает записи",
    WorkspaceBindingReason.STORE_UNOPENABLE: "Стор не удаётся открыть паролем workspace",
    WorkspaceBindingReason.STORE_REGRESSED: "Стор содержит меньше записей, чем зафиксировано в manifest",
    WorkspaceBindingReason.STORE_DIVERGED: "Начало стора не совпадает с зафиксированным в manifest",
    WorkspaceBindingReason.STORE_UNEXPECTED_ENTRIES: "Стор содержит записи, которых manifest не ожидает",
    WorkspaceBindingReason.STORE_AHEAD_UNMARKED: "Стор опережает manifest без отметки контролируемой мутации",
    WorkspaceBindingReason.RECOVERY_REQUIRED: "Требуется явное восстановление состояния сторов",
    WorkspaceBindingReason.APPEND_ONLY_VIOLATION: "Нарушено правило «стор только растёт»",
}

_LOCKED_MESSAGES = {
    WorkspaceLockedReason.HELD_BY_OTHER: "Workspace заблокирован другим владельцем",
    WorkspaceLockedReason.NESTED_ACQUISITION: "Повторный захват блокировки тем же объектом недопустим",
    WorkspaceLockedReason.NOT_HELD: "Операция требует удерживаемой блокировки workspace",
}

_REGISTRATION_MESSAGES = {
    ArtifactRegistrationReason.SLOT_INVALID: "Слот артефакта недействителен",
    ArtifactRegistrationReason.FILE_INVALID: "Файл артефакта отсутствует или недопустим",
    ArtifactRegistrationReason.INVALID_PERIOD: "Период артефакта недопустим",
    ArtifactRegistrationReason.INVALID_LINEAGE: "Родословная артефакта недопустима",
    ArtifactRegistrationReason.DUPLICATE_SHA256: "Артефакт с таким SHA-256 уже зарегистрирован",
    ArtifactRegistrationReason.EVIDENCE_REQUIRED: "Для этого типа артефакта требуется результат inspection",
    ArtifactRegistrationReason.EVIDENCE_NOT_ALLOWED: "Для этого типа артефакта результат inspection не допускается",
    ArtifactRegistrationReason.EVIDENCE_HASH_MISMATCH: "SHA-256 результата inspection не совпадает с файлом",
    ArtifactRegistrationReason.EVIDENCE_BLOCKING_FINDINGS: "Результат inspection содержит блокирующие признаки",
    ArtifactRegistrationReason.JOB_ID_INVALID: "Идентификатор job недопустим для этого типа артефакта",
    ArtifactRegistrationReason.PROVENANCE_REQUIRED: "Для этого типа артефакта требуется provenance",
    ArtifactRegistrationReason.PROVENANCE_NOT_ALLOWED: "Для этого типа артефакта provenance не допускается",
    ArtifactRegistrationReason.PROVENANCE_INVALID: "Provenance недействителен",
    ArtifactRegistrationReason.POINTER_KIND_INVALID: "Указатель допускается только на канонический аналитический артефакт",
}


def _require_reason(reason: object, enum_type: type) -> None:
    if not isinstance(reason, enum_type):
        raise TypeError(f"reason должен быть {enum_type.__name__}")


# ----------------------------------------------------------------------
# Исключения
# ----------------------------------------------------------------------


class WorkspaceError(Exception):
    """Базовое исключение Workspace-слоя."""


class WorkspaceInputError(WorkspaceError, ValueError):
    """
    Недопустимый аргумент/условие вызова. Единственное исключение, чьё
    сообщение задаёт вызывающий код (допустим только безопасный путь или
    суффикс — никогда значения, пароль, содержимое сторов/файлов).
    """

    def __init__(self, message: str) -> None:
        if not isinstance(message, str) or not message:
            raise TypeError("message должен быть непустой строкой")
        super().__init__(message)


class WorkspaceNotFoundError(WorkspaceError):
    """Manifest workspace не найден."""

    def __init__(self) -> None:
        super().__init__("Workspace не найден")


class WorkspaceAuthenticationError(WorkspaceError):
    """Неверный пароль либо данные повреждены/подделаны (случаи неразличимы)."""

    def __init__(self) -> None:
        super().__init__("Неверный пароль либо данные повреждены")


class WorkspaceCorruptedError(WorkspaceError):
    """Структура workspace, manifest или backup повреждена/недопустима."""

    def __init__(self, reason: WorkspaceCorruptedReason) -> None:
        _require_reason(reason, WorkspaceCorruptedReason)
        super().__init__(_CORRUPTED_MESSAGES[reason])
        self.reason = reason


class WorkspaceBindingError(WorkspaceError):
    """Нарушена привязка сторов к workspace либо требуется восстановление."""

    def __init__(self, reason: WorkspaceBindingReason) -> None:
        _require_reason(reason, WorkspaceBindingReason)
        super().__init__(_BINDING_MESSAGES[reason])
        self.reason = reason


class WorkspaceLockedError(WorkspaceError):
    """Проблема блокировки workspace."""

    def __init__(self, reason: WorkspaceLockedReason) -> None:
        _require_reason(reason, WorkspaceLockedReason)
        super().__init__(_LOCKED_MESSAGES[reason])
        self.reason = reason


class ArtifactNotFoundError(WorkspaceError):
    """Запись артефакта либо его файл не найдены."""

    def __init__(self) -> None:
        super().__init__("Артефакт не найден")


class ArtifactIntegrityError(WorkspaceError):
    """Файл артефакта не соответствует зарегистрированным данным."""

    def __init__(self) -> None:
        super().__init__("Целостность артефакта нарушена")


class ArtifactRegistrationError(WorkspaceError):
    """Регистрация артефакта отклонена."""

    def __init__(self, reason: ArtifactRegistrationReason) -> None:
        _require_reason(reason, ArtifactRegistrationReason)
        super().__init__(_REGISTRATION_MESSAGES[reason])
        self.reason = reason
