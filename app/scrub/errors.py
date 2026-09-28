"""
Исключения Stage 10C.2 (Metadata & Package Scrub).

Базовый класс Stage10CError переиспользуется из app.coverage.errors
(Stage 10C.1, закрыт) — однонаправленная зависимость app.scrub ->
app.coverage, принятое решение Stage10C.2 Final Contract Freeze
(§27 "Stage10CError dependency decision"): Stage10C.1 не изменяется,
новый потребитель существующего публичного символа.

PackageScrubReason включает все семь значений, зафиксированных Stage10C.2
Final Contract Freeze, даже если текущий substage (10C.2.1, resource +
ZIP structural preflight) поднимает только первые три
(INVALID_INPUT_PACKAGE, RESOURCE_LIMIT_EXCEEDED,
UNSUPPORTED_PACKAGE_CONTENT) — это сделано намеренно, чтобы последующие
substages (10C.2.2-.5) не расширяли публичный enum задним числом.

Та же дисциплина OD-7, что и во всём Stage10C: текст сообщения фиксирован
по reason в модульном словаре, никогда не включает raw XML, URL, путь
входного файла, worksheet name, содержимое ячеек, company/person,
identifier, connection string, пароль.
"""

from __future__ import annotations

import enum

from app.coverage.errors import Stage10CError

# ----------------------------------------------------------------------
# Reason-enum
# ----------------------------------------------------------------------


class PackageScrubReason(enum.Enum):
    INVALID_INPUT_PACKAGE = "invalid_input_package"
    RESOURCE_LIMIT_EXCEEDED = "resource_limit_exceeded"
    UNSUPPORTED_PACKAGE_CONTENT = "unsupported_package_content"
    RESTORED_MARKER_PRESENT = "restored_marker_present"
    KNOWN_RAW_WORKSHEET_TITLE = "known_raw_worksheet_title"
    SCRUB_FAILED = "scrub_failed"
    POST_VALIDATION_FAILED = "post_validation_failed"


# ----------------------------------------------------------------------
# Фиксированные сообщения
# ----------------------------------------------------------------------

_PACKAGE_SCRUB_MESSAGES = {
    PackageScrubReason.INVALID_INPUT_PACKAGE: (
        "Пакет XLSX недействителен, повреждён или не может быть безопасно открыт"
    ),
    PackageScrubReason.RESOURCE_LIMIT_EXCEEDED: (
        "Пакет XLSX превышает допустимый ресурсный лимит"
    ),
    PackageScrubReason.UNSUPPORTED_PACKAGE_CONTENT: (
        "Пакет XLSX содержит структуру, не входящую в допустимую политику"
    ),
    PackageScrubReason.RESTORED_MARKER_PRESENT: (
        "Обнаружен маркер ранее восстановленного workbook"
    ),
    PackageScrubReason.KNOWN_RAW_WORKSHEET_TITLE: (
        "Название листа совпадает с известным конфиденциальным значением"
    ),
    PackageScrubReason.SCRUB_FAILED: (
        "Не удалось безопасно выполнить scrub пакета"
    ),
    PackageScrubReason.POST_VALIDATION_FAILED: (
        "Результат scrub не прошёл проверку целостности"
    ),
}


def _require_reason(reason: object) -> None:
    if not isinstance(reason, PackageScrubReason):
        raise TypeError(f"reason должен быть PackageScrubReason, получено: {type(reason)!r}")


# ----------------------------------------------------------------------
# Исключение
# ----------------------------------------------------------------------


class PackageScrubError(Stage10CError):
    """
    Ошибка Stage10C.2 package/metadata scrub слоя. Stage10C.2.1 (resource +
    ZIP structural preflight) поднимает только INVALID_INPUT_PACKAGE,
    RESOURCE_LIMIT_EXCEEDED и UNSUPPORTED_PACKAGE_CONTENT.
    """

    def __init__(self, reason: PackageScrubReason) -> None:
        _require_reason(reason)
        super().__init__(_PACKAGE_SCRUB_MESSAGES[reason])
        self.reason = reason
