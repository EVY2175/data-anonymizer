"""
Неизменяемые модели Stage 10C.2.1 (resource + ZIP structural preflight).

PackagePreflightResult никогда не хранит: имена частей пакета, worksheet
names, relationship targets, XML-содержимое, значения ячеек, путь
входного файла — только безопасные неотрицательные числовые счётчики.
"""

from __future__ import annotations

import dataclasses


def _is_nonneg_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


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
