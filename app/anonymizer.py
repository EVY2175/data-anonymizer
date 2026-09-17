"""
Stage 7A — Flat Anonymizer: применение явных правил (FieldRule) к
FlatTable через MappingStore, без работы с openpyxl и без каких-либо
представлений об Excel-формате.

======================================================================
Границы Stage 7A
======================================================================

Поддержаны:
    - Action.KEEP;
    - Action.REMOVE;
    - Action.PSEUDONYMIZE ТОЛЬКО для FieldType.COMPANY и FieldType.PERSON.

PSEUDONYMIZE для любого другого FieldType (BRANCH, DEPARTMENT, INN, KPP,
OGRN, PHONE, EMAIL, ADDRESS, FINANCIAL, UNKNOWN) — ошибка конфигурации
(UnsupportedPseudonymizationTargetError), а не молчаливое поведение:

    - BRANCH/DEPARTMENT требуют корректного parent_alias, которым
      занимается Stage 9 (hierarchy); присваивать им parent_alias=None
      сейчас означало бы заранее создать НЕПРАВИЛЬНУЮ identity, которую
      потом нельзя тихо "исправить" (parent_alias — часть identity
      MappingEntry, а не служебное поле);
    - INN/KPP/OGRN получат отдельный механизм identifier tokenization на
      Stage 7B и сознательно НЕ используют EntityType (идентификаторы —
      не сущности; EntityType.INN/KPP/OGRN не существует и не должен
      появляться из-за Stage 7A);
    - для PHONE/EMAIL/ADDRESS/FINANCIAL/UNKNOWN пока не определён
      reversible pseudonymization-контракт вообще.

Этот модуль НЕ реализует: normalization, semantic entity resolution,
токенизацию идентификаторов, псевдонимизацию BRANCH/DEPARTMENT,
иерархию/поиск родителя, Excel writer, restore, аудит, batch-persistence
MappingStore. Он также не создаёт и не открывает MappingStore — получает
уже готовый объект снаружи и ничего не знает про шифрование/файлы.

======================================================================
DataAnonymizer — обратимый псевдонимизатор, а не entity resolution
======================================================================

Ключевой принцип Stage 7A: используется EXACT исходное значение ячейки,
без какой-либо нормализации (strip/casefold/lower/upper/Unicode
normalization/collapse spaces/семантическое сопоставление). Только
побайтово/строково идентичные значения считаются одной identity:

    "ООО Ромашка"     -> alias A
    "ООО Ромашка"     -> alias A (тот же exact real_value)
    "ООО  Ромашка"     -> alias B (два пробела — другая строка)
    " ООО Ромашка "    -> alias C (внешние пробелы — другая строка)

Это намеренное архитектурное решение ради точного будущего Stage 10
restore: как только значение хоть немного нормализуется перед тем, как
стать MappingEntry.real_value, часть исходных вариантов написания
безвозвратно теряется. Normalization, если/когда понадобится семантическая
дедупликация, — задача отдельного, более позднего компонента, а не
Stage 7A.

======================================================================
Пустые/пробельные значения — reversible-контракт
======================================================================

None, "" и любая строка, состоящая только из пробельных символов (в т.ч.
" ", "   ") — под Action.PSEUDONYMIZE возвращаются РОВНО такими же, какими
были, без создания MappingEntry и без генерации alias. `str.strip()`
используется ЗДЕСЬ только для того, чтобы РАСПОЗНАТЬ "пустое" значение —
результат strip() никогда не становится real_value и никогда не участвует
в поиске/создании identity: для непустых значений (в т.ч. с ведущими/
хвостовыми пробелами внутри непустого текста, например " ООО Ромашка ")
identity строится строго по исходной, ничем не тронутой строке.

======================================================================
Формулы
======================================================================

Stage 6 хранит формулы как обычные str (например, "=SUM(A2:A5)"), поэтому
простой isinstance(value, str) недостаточен, чтобы отличить формулу от
обычного текста. Под Action.PSEUDONYMIZE строка, начинающаяся с "=",
поднимает FormulaPseudonymizationError — MappingEntry не создаётся, alias
не генерируется. Stage 7A не вычисляет формулы. Под KEEP формула остаётся
как есть; под REMOVE формула, как и любое другое значение, становится
None.

======================================================================
Тип значения под PSEUDONYMIZE
======================================================================

Для COMPANY/PERSON псевдонимизируются только непустые значения типа str.
int/float/bool/datetime.datetime/date/time и любой другой non-str тип —
InvalidPseudonymizationValueError, без попытки автоматически привести
значение к str. Stage 7A не угадывает смысл повреждённых/неожиданных
данных.

======================================================================
Rule binding — только по индексу колонки
======================================================================

rules: Mapping[int, FieldRule] — ключ ЭТО 1-based индекс колонки, в
точности совпадающий с CellRecord.column. FieldRule.column_name НИКОГДА
не используется как ключ связывания — это только человекочитаемые
метаданные. Дублирующиеся/пустые/числовые заголовки, которые Stage 6
намеренно сохраняет как есть, поэтому не создают неоднозначности:
две колонки с одинаковым заголовком "ИНН" адресуются разными ключами
rules (например, {1: rule_a, 2: rule_b}) и могут иметь разные правила.

Покрытие правилами — строгое: rules обязан содержать ровно те индексы
колонок, что есть в table (ни одного пропущенного, ни одного лишнего) —
иначе InvalidRuleConfigurationError. Это сознательный fail-safe: если в
исходном файле появилась новая колонка, DataAnonymizer не должен молча
оставить её неанонимизированной под неявным KEEP по умолчанию.

======================================================================
Alias lookup и генерация — без O(n²)
======================================================================

Поиск существующего alias — один O(1) вызов
mapping_store.get_by_real_value(real_value, entity_type=..., parent_alias=None)
(с ЯВНО переданным parent_alias=None, что означает "top-level identity",
а не "не фильтровать по parent_alias" — см. sentinel-семантику в
app.mapping.base). Если записи нет — новый alias генерируется через
существующий Stage 1 generate_alias (app.security.alias_generator) и
сохраняется через mapping_store.add(...).

mapping_store.all_aliases() вызывается РОВНО ОДИН РАЗ за весь вызов
anonymize_flat_table — не на каждую ячейку/сущность. Дальше рабочее
множество занятых alias пополняется локально в памяти по мере генерации
новых alias, поэтому проверка коллизий остаётся O(1) амортизированно на
протяжении всего прохода, а не O(n) на каждую новую сущность.

======================================================================
Атомарность (или, точнее, её отсутствие на этом этапе)
======================================================================

anonymize_flat_table строит НОВЫЙ FlatTable и никогда не изменяет входной
(CellRecord/FlatTable неизменяемы физически). Но MappingStore — внешняя
mutable-зависимость: если ошибка (FormulaPseudonymizationError,
InvalidPseudonymizationValueError, MappingConflictError и т.п.) возникает
в середине обработки таблицы, MappingEntry, успешно добавленные ДО этой
ошибки, остаются в store — отката/rollback на Stage 7A нет. Это открытый
вопрос будущих этапов, а не то, что здесь маскируется. Любая ошибка
MappingStore (в первую очередь MappingConflictError) всегда
распространяется наружу как есть, а не подавляется.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Optional

from app.excel.models import CellRecord, FlatTable
from app.mapping.base import MappingStore
from app.models.entities import EntityType, MappingEntry
from app.models.rules import Action, FieldRule, FieldType
from app.security.alias_generator import generate_alias

# ----------------------------------------------------------------------
# Исключения
# ----------------------------------------------------------------------


class AnonymizationError(Exception):
    """Базовое исключение слоя анонимизации (Stage 7A, app.anonymizer)."""


class InvalidRuleConfigurationError(AnonymizationError):
    """
    rules не покрывает FlatTable корректно: отсутствует правило для
    существующей колонки, задано правило для несуществующей колонки, либо
    ключ rules не является допустимым 1-based индексом колонки (не int,
    bool вместо int, либо значение < 1).
    """


class UnsupportedPseudonymizationTargetError(AnonymizationError):
    """
    Action.PSEUDONYMIZE запрошен для FieldType, для которого Stage 7A
    сознательно не определяет псевдонимизацию (см. docstring модуля).
    Поддержаны только FieldType.COMPANY и FieldType.PERSON.
    """


class InvalidPseudonymizationValueError(AnonymizationError):
    """
    Непустое значение ячейки под Action.PSEUDONYMIZE имеет тип, отличный
    от str. Stage 7A не преобразует такие значения в str автоматически.
    """


class FormulaPseudonymizationError(AnonymizationError):
    """
    Значение ячейки под Action.PSEUDONYMIZE — строка формулы Excel
    (начинается с "="). Stage 7A не вычисляет формулы и не
    псевдонимизирует их текст.
    """


# ----------------------------------------------------------------------
# FieldType -> EntityType (только то, что реально поддержано Stage 7A)
# ----------------------------------------------------------------------

_PSEUDONYMIZABLE_FIELD_TYPES: dict[FieldType, EntityType] = {
    FieldType.COMPANY: EntityType.COMPANY,
    FieldType.PERSON: EntityType.PERSON,
}


# ----------------------------------------------------------------------
# Публичный API
# ----------------------------------------------------------------------


def anonymize_flat_table(
    table: FlatTable,
    rules: Mapping[int, FieldRule],
    mapping_store: MappingStore,
) -> FlatTable:
    """
    Применяет rules (индекс колонки -> FieldRule) к table через
    mapping_store и возвращает НОВЫЙ FlatTable.

    :param table: исходная таблица; не изменяется (физически не может
        быть изменена — CellRecord/FlatTable неизменяемы).
    :param rules: 1-based индекс колонки (как CellRecord.column) ->
        FieldRule. Должен покрывать РОВНО те колонки, что есть в table —
        ни одной пропущенной, ни одной лишней.
    :param mapping_store: уже открытый MappingStore; эта функция не
        создаёт, не открывает и не закрывает хранилище и ничего не знает
        про пароли/шифрование/файлы.
    :raises InvalidRuleConfigurationError: некорректное покрытие/ключи rules.
    :raises UnsupportedPseudonymizationTargetError: PSEUDONYMIZE запрошен
        для неподдерживаемого на Stage 7A FieldType.
    :raises InvalidPseudonymizationValueError: непустое non-str значение
        под PSEUDONYMIZE.
    :raises FormulaPseudonymizationError: строка формулы под PSEUDONYMIZE.
    :raises MappingConflictError: как есть, из mapping_store.add(...).
    """
    _validate_rules(table, rules)
    _validate_pseudonymization_targets(rules)

    # Один раз за весь вызов — не на каждую ячейку/сущность (см. docstring
    # модуля, раздел про O(n²)). Локальная копия, безопасная для мутации.
    existing_aliases: set[str] = set(mapping_store.all_aliases())

    new_rows = tuple(
        _anonymize_row(row, rules, mapping_store, existing_aliases) for row in table.rows
    )

    return FlatTable(sheet_name=table.sheet_name, header_row=table.header_row, rows=new_rows)


# ----------------------------------------------------------------------
# Валидация конфигурации правил
# ----------------------------------------------------------------------


def _validate_rules(table: FlatTable, rules: Mapping[int, FieldRule]) -> None:
    for key in rules.keys():
        if isinstance(key, bool) or not isinstance(key, int):
            raise InvalidRuleConfigurationError(
                f"Ключ rules должен быть int (1-based индекс колонки), "
                f"получено: {key!r} ({type(key).__name__})"
            )
        if key < 1:
            raise InvalidRuleConfigurationError(
                f"Ключ rules должен быть >= 1, получено: {key}"
            )

    table_columns = {cell.column for cell in table.header_row}
    rule_columns = set(rules.keys())

    missing = table_columns - rule_columns
    if missing:
        raise InvalidRuleConfigurationError(
            f"Отсутствуют явные правила для колонок: {sorted(missing)}"
        )

    extra = rule_columns - table_columns
    if extra:
        raise InvalidRuleConfigurationError(
            f"Правила заданы для несуществующих в table колонок: {sorted(extra)}"
        )


def _validate_pseudonymization_targets(rules: Mapping[int, FieldRule]) -> None:
    for column, rule in rules.items():
        if rule.action is Action.PSEUDONYMIZE and rule.field_type not in _PSEUDONYMIZABLE_FIELD_TYPES:
            raise UnsupportedPseudonymizationTargetError(
                f"Колонка {column}: Action.PSEUDONYMIZE не поддержан на Stage 7A "
                f"для FieldType.{rule.field_type.name} (поддержаны только COMPANY и PERSON)"
            )


# ----------------------------------------------------------------------
# Построчная/поячейковая обработка
# ----------------------------------------------------------------------


def _anonymize_row(
    row: tuple[CellRecord, ...],
    rules: Mapping[int, FieldRule],
    mapping_store: MappingStore,
    existing_aliases: set[str],
) -> tuple[CellRecord, ...]:
    return tuple(
        _anonymize_cell(cell, rules[cell.column], mapping_store, existing_aliases) for cell in row
    )


def _anonymize_cell(
    cell: CellRecord,
    rule: FieldRule,
    mapping_store: MappingStore,
    existing_aliases: set[str],
) -> CellRecord:
    if rule.action is Action.KEEP:
        return cell

    if rule.action is Action.REMOVE:
        return CellRecord(row=cell.row, column=cell.column, value=None)

    # Action.PSEUDONYMIZE — единственный оставшийся вариант; конфигурация
    # уже проверена в _validate_pseudonymization_targets.
    return _pseudonymize_cell(cell, rule.field_type, mapping_store, existing_aliases)


def _pseudonymize_cell(
    cell: CellRecord,
    field_type: FieldType,
    mapping_store: MappingStore,
    existing_aliases: set[str],
) -> CellRecord:
    value = cell.value

    # Пустые/пробельные значения — exact passthrough, без MappingEntry.
    # strip() используется ТОЛЬКО для распознавания "пусто", а не как
    # значение identity (см. docstring модуля).
    if value is None or (isinstance(value, str) and value.strip() == ""):
        return cell

    if isinstance(value, str) and value.startswith("="):
        raise FormulaPseudonymizationError(
            f"Ячейка (row={cell.row}, column={cell.column}) содержит формулу Excel "
            f"и не может быть псевдонимизирована на Stage 7A: {value!r}"
        )

    if not isinstance(value, str):
        raise InvalidPseudonymizationValueError(
            f"Ячейка (row={cell.row}, column={cell.column}): значение для "
            f"FieldType.{field_type.name} должно быть str, получено: "
            f"{type(value).__name__}"
        )

    entity_type = _PSEUDONYMIZABLE_FIELD_TYPES[field_type]
    real_value = value  # EXACT — без strip/casefold/normalize.

    existing_entry: Optional[MappingEntry] = mapping_store.get_by_real_value(
        real_value, entity_type=entity_type, parent_alias=None
    )

    if existing_entry is not None:
        alias = existing_entry.alias
    else:
        alias = generate_alias(entity_type, existing_aliases)
        mapping_store.add(
            MappingEntry(alias=alias, real_value=real_value, entity_type=entity_type, parent_alias=None)
        )
        existing_aliases.add(alias)

    return CellRecord(row=cell.row, column=cell.column, value=alias)
