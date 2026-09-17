"""
Консервативные детекторы типов полей и формализованных российских
идентификаторов (ИНН, КПП, ОГРН, ОГРНИП).

======================================================================
Границы ответственности Stage 5
======================================================================

Этот модуль отвечает ТОЛЬКО на вопрос "что это за поле/значение?"
(DETECTION/VALIDATION). Он НИКОГДА не решает, что с этим полем/значением
делать — то есть не назначает Action (PSEUDONYMIZE/REMOVE/KEEP) и не
создаёт FieldRule. Это решение принимается отдельным, более поздним
слоем, которому известен контекст всего файла и политика анонимизации.

Модуль детерминированный и не имеет побочных эффектов: никакого файлового
I/O, сети, внешних API, LLM, mutable global state, зависимости от
MappingStore/crypto/Excel. Реальные значения идентификаторов нигде не
логируются.

======================================================================
FieldType.OGRN покрывает и ОГРН, и ОГРНИП
======================================================================

У ОГРН (13 цифр, юридические лица) и ОГРНИП (15 цифр, ИП) разные форматы
и разные алгоритмы контрольной суммы, поэтому внутренние валидаторы
разделены (`is_valid_ogrn` только для 13 цифр, `is_valid_ogrnip` только
для 15 цифр). Но на уровне `app.models.rules.FieldType` (Stage 1) для
обоих не заведено отдельного значения — по явному решению ТЗ Stage 5
`FieldType.OGRNIP` НЕ добавляется, и `detect_value_type` классифицирует
оба формата как `FieldType.OGRN`. Различить их при необходимости можно
явным вызовом `is_valid_ogrn`/`is_valid_ogrnip` по длине значения.

======================================================================
Input policy для числовых идентификаторов (важно для будущего Excel reader)
======================================================================

Excel имеет обыкновение превращать длинные числовые идентификаторы в
float/scientific notation, необратимо теряя точность (например,
7707083893 -> 7707083893.0 -> 7.707083893e+09). Поэтому:

- str  -> допускается (единственная нормализация — strip() внешних
  пробелов; внутренние пробелы, дефисы, точки, слэши и т.п. не
  удаляются);
- int  -> допускается, используется точное десятичное представление
  (str(value)); ведущие нули у int не восстанавливаются (int их и не
  хранит);
- bool -> НЕ считается int-идентификатором, хотя bool является
  подклассом int в Python;
- float -> ВСЕГДА отвергается, независимо от значения (даже если это
  "круглое" число вроде 7707083893.0) — иначе часть точности,
  потерянной Excel, могла бы тихо "восстановиться" в похожий, но неверный
  идентификатор;
- None и любые прочие типы -> невалидны.

Для КПП действует та же политика типов (str/int допускаются, float/bool/
None — нет), т.к. КПП тоже формализованный идентификатор колонки Excel.

======================================================================
Только ASCII-цифры
======================================================================

Для всех числовых идентификаторов и для цифровых позиций КПП принимаются
только ASCII-цифры 0-9. Регулярные выражения используют явные символьные
классы `[0-9]` и `[0-9A-Z]`, а не `\\d`/`\\w` — символьный класс с
буквальным диапазоном кодовых точек не расширяется никакими Unicode-
цифрами (арабо-индийскими, полноширинными и т.п.) независимо от флагов
re, в отличие от `\\d`.
"""

from __future__ import annotations

import re
import unicodedata
from typing import Optional

from app.models.rules import FieldType

# ----------------------------------------------------------------------
# Общие помощники: input policy + ASCII-only проверки
# ----------------------------------------------------------------------

# Буквальный символьный класс: соответствует ТОЛЬКО ASCII '0'-'9', не
# расширяется Unicode-цифрами ни при каких флагах re (в отличие от \d).
_ASCII_DIGITS_ONLY_RE = re.compile(r"[0-9]+")

# КПП: 4 ASCII-цифры + 2 символа (ASCII-цифра ИЛИ заглавная ASCII A-Z) +
# 3 ASCII-цифры. Буквальные диапазоны кодовых точек — Unicode-цифры и
# кириллица-двойники латинских букв не совпадают ни с одним из классов.
_KPP_STRUCTURE_RE = re.compile(r"[0-9]{4}[0-9A-Z]{2}[0-9]{3}")


def _coerce_identifier_candidate(value: object) -> Optional[str]:
    """
    Приводит value к строке-кандидату для дальнейшей структурной/
    checksum-проверки формализованного идентификатора, либо возвращает
    None, если value должно быть безоговорочно отвергнуто на уровне типа
    (см. docstring модуля, раздел "Input policy").

    bool проверяется ДО int, т.к. в Python bool — подкласс int, а по ТЗ
    bool не должен считаться int-идентификатором.
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, str):
        # Единственная разрешённая нормализация строки — strip() внешних
        # пробелов; внутренние разделители не трогаем и целостность
        # формата проверяем на них ниже как есть.
        return value.strip()
    if isinstance(value, int):
        # Точное десятичное представление, без попытки восстановить
        # ведущие нули (int их не хранит).
        return str(value)
    # float, None и любые прочие типы — не идентификатор.
    return None


def _is_ascii_digits(candidate: str) -> bool:
    return bool(_ASCII_DIGITS_ONLY_RE.fullmatch(candidate))


# ----------------------------------------------------------------------
# ИНН
# ----------------------------------------------------------------------

_INN10_WEIGHTS = (2, 4, 10, 3, 5, 9, 4, 6, 8)
_INN12_WEIGHTS_N11 = (7, 2, 4, 10, 3, 5, 9, 4, 6, 8)
_INN12_WEIGHTS_N12 = (3, 7, 2, 4, 10, 3, 5, 9, 4, 6, 8)


def _checksum_digit(digits: str, weights: tuple[int, ...]) -> int:
    total = sum(int(digit) * weight for digit, weight in zip(digits, weights))
    return total % 11 % 10


def _is_valid_inn10(digits: str) -> bool:
    if not _is_ascii_digits(digits):
        return False
    return _checksum_digit(digits, _INN10_WEIGHTS) == int(digits[9])


def _is_valid_inn12(digits: str) -> bool:
    if not _is_ascii_digits(digits):
        return False
    n11_ok = _checksum_digit(digits, _INN12_WEIGHTS_N11) == int(digits[10])
    n12_ok = _checksum_digit(digits, _INN12_WEIGHTS_N12) == int(digits[11])
    return n11_ok and n12_ok


def is_valid_inn(value: object) -> bool:
    """
    ИНН юридического лица (10 цифр, одна контрольная цифра) либо ИНН
    физического лица/ИП (12 цифр, две контрольные цифры) — обе проверяются
    строго по алгоритму ФНС. Любая другая длина -> False.
    """
    candidate = _coerce_identifier_candidate(value)
    if candidate is None:
        return False
    if len(candidate) == 10:
        return _is_valid_inn10(candidate)
    if len(candidate) == 12:
        return _is_valid_inn12(candidate)
    return False


# ----------------------------------------------------------------------
# ОГРН / ОГРНИП
# ----------------------------------------------------------------------


def _is_valid_ogrn13(digits: str) -> bool:
    if not _is_ascii_digits(digits):
        return False
    check_digit = (int(digits[:12]) % 11) % 10
    return check_digit == int(digits[12])


def is_valid_ogrn(value: object) -> bool:
    """ОГРН юридического лица: ровно 13 ASCII-цифр, checksum обязателен."""
    candidate = _coerce_identifier_candidate(value)
    if candidate is None or len(candidate) != 13:
        return False
    return _is_valid_ogrn13(candidate)


def _is_valid_ogrnip15(digits: str) -> bool:
    if not _is_ascii_digits(digits):
        return False
    check_digit = (int(digits[:14]) % 13) % 10
    return check_digit == int(digits[14])


def is_valid_ogrnip(value: object) -> bool:
    """ОГРНИП индивидуального предпринимателя: ровно 15 ASCII-цифр."""
    candidate = _coerce_identifier_candidate(value)
    if candidate is None or len(candidate) != 15:
        return False
    return _is_valid_ogrnip15(candidate)


# ----------------------------------------------------------------------
# КПП (без checksum — более слабый детектор, чем ИНН/ОГРН)
# ----------------------------------------------------------------------


def is_valid_kpp(value: object) -> bool:
    """
    КПП: ровно 9 символов, формат [0-9]{4}[0-9A-Z]{2}[0-9]{3} целиком.

    У КПП нет контрольной суммы — это чисто структурная проверка, поэтому
    КПП является более слабым сигналом детекции, чем ИНН/ОГРН/ОГРНИП.
    lowercase-буквы и кириллические двойники латинских букв не приводятся
    и не принимаются: несоответствие формату означает False, а не попытку
    "исправить" значение.
    """
    candidate = _coerce_identifier_candidate(value)
    if candidate is None or len(candidate) != 9:
        return False
    return bool(_KPP_STRUCTURE_RE.fullmatch(candidate))


# ----------------------------------------------------------------------
# detect_value_type — консервативная детекция по значению
# ----------------------------------------------------------------------


def detect_value_type(value: object) -> FieldType:
    """
    Консервативно классифицирует одно значение как один из формализованных
    российских идентификаторов, ИМЕЮЩИХ CHECKSUM, либо FieldType.UNKNOWN.

    Порядок проверок: ИНН -> ОГРН -> ОГРНИП -> UNKNOWN. Порядок не влияет
    на результат: длины форматов попарно различны (ИНН: 10/12, ОГРН: 13,
    ОГРНИП: 15), поэтому ни одно валидное значение не может одновременно
    пройти проверку двух разных идентификаторов — это зафиксировано
    тестами.

    КПП сознательно ИСКЛЮЧЁН из автоматической детекции по значению, хотя
    is_valid_kpp() как структурный валидатор существует и продолжает
    работать. Причина — отсутствие контрольной суммы у КПП: формат
    [0-9]{4}[0-9A-Z]{2}[0-9]{3} слишком широк и без дополнительного
    контекста совпадает с произвольными 9-значными идентификаторами
    (внутренние ID, номера договоров и т.п. — например, "123456789" или
    "770701001" сами по себе НЕ являются надёжным признаком КПП). У ИНН,
    ОГРН и ОГРНИП, в отличие от КПП, есть контрольная сумма, поэтому по
    одному значению их распознавание остаётся надёжным и не отключено.
    Единственный источник, где детекция КПП оправдана, — контекст
    названия столбца (см. detect_column_type: "КПП" -> FieldType.KPP),
    не одиночное значение.

    Обычный текст (названия компаний, ФИО, адреса и т.п.) и любые другие
    типы полей (COMPANY, BRANCH, DEPARTMENT, PERSON, PHONE, EMAIL,
    ADDRESS, FINANCIAL) НЕ детектируются по значению на этом этапе — для
    них нет надёжного формального правила в ТЗ Stage 5. Такие значения,
    как и любые другие нераспознанные (включая структурно похожие на КПП),
    возвращают FieldType.UNKNOWN (никогда не None).
    """
    if is_valid_inn(value):
        return FieldType.INN
    if is_valid_ogrn(value):
        return FieldType.OGRN
    if is_valid_ogrnip(value):
        # ОГРН и ОГРНИП сознательно не различаются на уровне FieldType —
        # см. docstring модуля.
        return FieldType.OGRN
    return FieldType.UNKNOWN


# ----------------------------------------------------------------------
# detect_column_type — детекция по названию столбца
# ----------------------------------------------------------------------


def _normalize_column_name(column_name: str) -> str:
    """
    Безопасная нормализация заголовка столбца: strip -> Unicode NFC ->
    casefold -> схлопывание последовательностей whitespace до одного
    пробела. Никакого fuzzy/substring matching здесь нет — это только
    подготовка строки к точному сравнению по словарю.
    """
    normalized = column_name.strip()
    normalized = unicodedata.normalize("NFC", normalized)
    normalized = normalized.casefold()
    normalized = re.sub(r"\s+", " ", normalized).strip()
    return normalized


# Точные (не fuzzy, не substring) соответствия нормализованных заголовков
# столбцов типам полей. Ключи уже в нормализованной форме (strip + NFC +
# casefold + схлопнутый whitespace), т.к. это единственная форма, с
# которой их сравнивает detect_column_type.
_COLUMN_NAME_TO_FIELD_TYPE: dict[str, FieldType] = {
    # COMPANY
    "компания": FieldType.COMPANY,
    "организация": FieldType.COMPANY,
    "наименование организации": FieldType.COMPANY,
    "наименование компании": FieldType.COMPANY,
    "контрагент": FieldType.COMPANY,
    # BRANCH
    "филиал": FieldType.BRANCH,
    "наименование филиала": FieldType.BRANCH,
    # DEPARTMENT
    "подразделение": FieldType.DEPARTMENT,
    "отдел": FieldType.DEPARTMENT,
    "департамент": FieldType.DEPARTMENT,
    "структурное подразделение": FieldType.DEPARTMENT,
    # PERSON
    "фио": FieldType.PERSON,
    "ф.и.о.": FieldType.PERSON,
    "сотрудник": FieldType.PERSON,
    "физическое лицо": FieldType.PERSON,
    # INN
    "инн": FieldType.INN,
    "инн организации": FieldType.INN,
    "инн контрагента": FieldType.INN,
    # KPP
    "кпп": FieldType.KPP,
    "кпп организации": FieldType.KPP,
    "кпп контрагента": FieldType.KPP,
    # OGRN (включая ОГРНИП — см. docstring модуля)
    "огрн": FieldType.OGRN,
    "огрнип": FieldType.OGRN,
    "огрн организации": FieldType.OGRN,
    # PHONE
    "телефон": FieldType.PHONE,
    "номер телефона": FieldType.PHONE,
    "мобильный телефон": FieldType.PHONE,
    # EMAIL
    "email": FieldType.EMAIL,
    "e-mail": FieldType.EMAIL,
    "электронная почта": FieldType.EMAIL,
    "адрес электронной почты": FieldType.EMAIL,
    # ADDRESS
    "адрес": FieldType.ADDRESS,
    "юридический адрес": FieldType.ADDRESS,
    "фактический адрес": FieldType.ADDRESS,
    "почтовый адрес": FieldType.ADDRESS,
    # FINANCIAL
    "выручка": FieldType.FINANCIAL,
    "оборот": FieldType.FINANCIAL,
    "доход": FieldType.FINANCIAL,
    "расходы": FieldType.FINANCIAL,
    "маржинальность": FieldType.FINANCIAL,
    "прибыль": FieldType.FINANCIAL,
}


def detect_column_type(column_name: object) -> FieldType:
    """
    Детектирует тип поля по названию столбца через точное сравнение с
    таблицей известных заголовков (после безопасной нормализации).

    Не-str, неизвестные и частично совпадающие (substring) заголовки —
    например, "Комментарий про ИНН" — дают FieldType.UNKNOWN, а не
    ложное срабатывание: сравнение всегда идёт по нормализованной строке
    целиком, никогда по вхождению подстроки.
    """
    if not isinstance(column_name, str):
        return FieldType.UNKNOWN
    normalized = _normalize_column_name(column_name)
    return _COLUMN_NAME_TO_FIELD_TYPE.get(normalized, FieldType.UNKNOWN)
