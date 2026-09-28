"""
Неизменяемые модели Stage 10C.2.1 (resource + ZIP structural preflight) и
Stage 10C.2.2 (package policy / inventory).

PackagePreflightResult и PackageInventory никогда не хранят: имена частей
пакета, worksheet names, relationship targets, URL, XML-содержимое,
значения ячеек, комментарии, значения свойств, shared strings, путь
входного файла — только безопасные неотрицательные числовые счётчики и
boolean-флаги.
"""

from __future__ import annotations

import dataclasses


def _is_nonneg_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _is_strict_bool(value: object) -> bool:
    return isinstance(value, bool)


@dataclasses.dataclass(frozen=True)
class PackagePreflightResult:
    """
    Результат app.scrub.preflight.preflight_xlsx_package: неконфиденциальные
    счётчики, достаточные как вход для последующего Stage10C.2.2 (package
    policy/inventory), без повторного обращения к дорогостоящим проверкам.
    """

    zip_entry_count: int
    total_uncompressed_bytes: int
    sheet_count: int
    nonempty_cell_count: int

    def __post_init__(self) -> None:
        for field_name in (
            "zip_entry_count",
            "total_uncompressed_bytes",
            "sheet_count",
            "nonempty_cell_count",
        ):
            if not _is_nonneg_int(getattr(self, field_name)):
                raise ValueError(f"PackagePreflightResult.{field_name} должен быть int >= 0")


@dataclasses.dataclass(frozen=True)
class PackageInventory:
    """
    Результат app.scrub.inventory.inspect_package_policy: неконфиденциальный
    package-level inventory Stage10C.2.2.

    Точная семантика счётчиков (Contract Correction + Final Freeze §3):
    - comment_part_count — число физических comments.xml частей (политика
      ограничивает <=1 на worksheet, поэтому <= MAX_WORKSHEETS, но это
      ограничение самой policy, а не врождённое свойство OOXML).
    - table_part_count — число физических table*.xml частей. НЕ ограничено
      sheet_count (несколько tables возможны на одном worksheet); верхняя
      граница — MAX_ZIP_ENTRIES (каждая table — отдельная ZIP-запись).
    - hyperlink_relationship_count — число relationship-ЗАПИСЕЙ Type=hyperlink
      (не физических частей, не ячеек, не URL, не внутренних переходов без
      relationship) — не ограничено ни MAX_WORKSHEETS, ни MAX_ZIP_ENTRIES
      напрямую, только косвенно через уже действующие resource-лимиты.
    """

    has_shared_strings: bool
    has_calc_chain: bool
    comment_part_count: int
    table_part_count: int
    hyperlink_relationship_count: int
    has_core_properties: bool
    has_app_properties: bool
    has_custom_properties: bool

    def __post_init__(self) -> None:
        for field_name in (
            "comment_part_count",
            "table_part_count",
            "hyperlink_relationship_count",
        ):
            if not _is_nonneg_int(getattr(self, field_name)):
                raise ValueError(f"PackageInventory.{field_name} должен быть int >= 0")
        for field_name in (
            "has_shared_strings",
            "has_calc_chain",
            "has_core_properties",
            "has_app_properties",
            "has_custom_properties",
        ):
            if not _is_strict_bool(getattr(self, field_name)):
                raise ValueError(f"PackageInventory.{field_name} должен быть bool")
