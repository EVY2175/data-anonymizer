"""
Модели плоской табличной структуры, прочитанной из Excel.

Модуль не содержит файлового I/O и не зависит от openpyxl напрямую — это
чистые immutable-модели данных, которыми оперирует app.excel.reader.

======================================================================
Почему НЕ dict по названиям колонок
======================================================================

Excel-файл может законно содержать: одинаковые заголовки, пустые
заголовки, числовые заголовки, заголовки произвольного вида. Отображение
"название колонки -> значение" потеряло бы часть данных (при дубликатах)
или было бы неоднозначным. Поэтому строки представлены как позиционные
кортежи CellRecord в порядке колонок (row/column — координаты исходного
Excel, 1-based, как в openpyxl), а не как dict.
"""

from __future__ import annotations

import dataclasses


@dataclasses.dataclass(frozen=True)
class CellRecord:
    """
    Одна ячейка, прочитанная из Excel, с исходными координатами.

    row/column — 1-based координаты исходного worksheet (как у openpyxl),
    что позволяет более поздним этапам (writer/anonymizer) однозначно
    адресоваться к исходной ячейке.

    value — значение РОВНО такое, каким его вернул openpyxl, без какой-либо
    нормализации/coercion (см. app.excel.reader). Ограничений на тип value
    сознательно нет: это может быть None, str, int, float, bool,
    datetime.datetime/date/time, строка формулы и т.д.
    """

    row: int
    column: int
    value: object

    def __post_init__(self) -> None:
        if isinstance(self.row, bool) or not isinstance(self.row, int):
            raise TypeError(f"CellRecord.row должен быть int, получено: {type(self.row)!r}")
        if self.row < 1:
            raise ValueError(f"CellRecord.row должен быть >= 1, получено: {self.row}")
        if isinstance(self.column, bool) or not isinstance(self.column, int):
            raise TypeError(
                f"CellRecord.column должен быть int, получено: {type(self.column)!r}"
            )
        if self.column < 1:
            raise ValueError(f"CellRecord.column должен быть >= 1, получено: {self.column}")


@dataclasses.dataclass(frozen=True)
class FlatTable:
    """
    Плоское представление одного worksheet: строка заголовков + строки
    данных, каждая — кортеж CellRecord в порядке колонок.

    header_row и каждая строка в rows содержат CellRecord ровно для всех
    колонок в пределах worksheet.max_column — включая пустые ячейки
    (value=None), без сжатия/пропуска.
    """

    sheet_name: str
    header_row: tuple[CellRecord, ...]
    rows: tuple[tuple[CellRecord, ...], ...]
