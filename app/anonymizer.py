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
    - Action.PSEUDONYMIZE для FieldType.COMPANY и FieldType.PERSON (entity
      pseudonymization через MappingStore, Stage 7A);
    - Action.PSEUDONYMIZE для FieldType.INN/KPP/OGRN (identifier
      tokenization через IdentifierMappingStore, Stage 7B.5) — отдельный
      от entity механизм, НЕ использующий EntityType (идентификаторы —
      не сущности; EntityType.INN/KPP/OGRN не существует и не должен
      появляться).

PSEUDONYMIZE для любого другого FieldType (BRANCH, DEPARTMENT, PHONE,
EMAIL, ADDRESS, FINANCIAL, UNKNOWN) — ошибка конфигурации
(UnsupportedPseudonymizationTargetError), а не молчаливое поведение:

    - BRANCH/DEPARTMENT требуют корректного parent_alias, которым
      занимается Stage 9 (hierarchy); присваивать им parent_alias=None
      сейчас означало бы заранее создать НЕПРАВИЛЬНУЮ identity, которую
      потом нельзя тихо "исправить" (parent_alias — часть identity
      MappingEntry, а не служебное поле);
    - для PHONE/EMAIL/ADDRESS/FINANCIAL/UNKNOWN пока не определён
      reversible pseudonymization-контракт вообще.

Этот модуль НЕ реализует: normalization, semantic entity resolution,
псевдонимизацию BRANCH/DEPARTMENT, иерархию/поиск родителя, Excel writer,
restore, аудит, domain-валидацию identifier значений сверх Stage 5
public-валидаторов (is_valid_inn/is_valid_kpp/is_valid_ogrn/
is_valid_ogrnip), provenance исходного Python-типа identifier-значения
(см. раздел "Identifier tokenization" ниже). Он также не создаёт и не
открывает MappingStore/IdentifierMappingStore — получает уже готовые
объекты снаружи и ничего не знает про шифрование/файлы.

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
Identifier tokenization (Stage 7B.5) — отдельный механизм от entity
======================================================================

FieldType.INN/KPP/OGRN под Action.PSEUDONYMIZE используют
IdentifierMappingStore (Stage 7B.1-7B.4), передаваемый через
keyword-only параметр identifier_store. Ключевые отличия от entity-пути:

    - валидность значения проверяется существующими Stage 5 public-
      валидаторами (is_valid_inn/is_valid_kpp/is_valid_ogrn/
      is_valid_ogrnip) — НЕ дублируется и не переопределяется здесь;
      неверный тип (bool/float/datetime/...) и неверная структура/
      checksum дают одно и то же InvalidIdentifierValueError;
    - identifier_value для identity — EXACT raw-представление (str как
      есть, либо str(value) для int), НИКОГДА не stripped-candidate,
      которым валидатор пользуется только внутри себя для проверки;
    - int и str, дающие одинаковый str(value)/значение, получают ОДНУ
      identity/token — Python-тип исходного значения не восстановим из
      IdentifierMappingStore (frozen contract, Stage 7B.0-correction);
      leading zero, потерянный Excel при чтении int, здесь НЕ
      восстанавливается и не угадывается identifier-слоем — какое именно
      Python-представление было у исходной ячейки, опционально фиксирует
      отдельный provenance-слой (см. раздел ниже), не подменяя эту
      identity-границу;
    - если ни одно правило не требует identifier tokenization,
      identifier_store вообще не читается (даже если передан) — ни
      all_tokens(), ни add_many(), ни любой другой метод.

======================================================================
Provenance identifier-ячеек (Stage 7C.4) — опциональный, отдельный слой
======================================================================

Keyword-only параметр provenance_store: Optional[ProvenanceStore] = None
позволяет опционально фиксировать, в каком именно Python-представлении
(str/int, см. IdentifierRepresentation) была КОНКРЕТНАЯ ячейка ДО
tokenization — по одной IdentifierCellProvenance на каждую УСПЕШНО
токенизированную identifier-ячейку (coordinate = table.sheet_name +
CellRecord.row/column), независимо от того, был ли token переиспользован
(persisted/pending) или сгенерирован заново. Provenance — per-cell, а не
per-new-mapping: одна и та же identity в двух ячейках даёт ОДИН token, но
ДВЕ provenance-записи.

Если provenance_store не передан (None), никакая provenance-работа не
выполняется вообще (ни один IdentifierCellProvenance не создаётся).
provenance_store НИКОГДА не заменяет требование identifier_store для
identifier PSEUDONYMIZE — см. MissingIdentifierStoreError ниже.

Эта функция не читает, не генерирует и не сравнивает
provenance_store.job_id — job_id целиком принадлежит store/sidecar,
созданному ДО вызова этой функции (см. app.mapping.provenance_base).

======================================================================
Атомарность (или, точнее, её отсутствие) — РАЗНАЯ для entity и identifier
======================================================================

anonymize_flat_table строит НОВЫЙ FlatTable и никогда не изменяет входной
(CellRecord/FlatTable неизменяемы физически). Но MappingStore и
IdentifierMappingStore — внешние mutable-зависимости с РАЗНЫМИ
гарантиями при ошибке в середине обработки таблицы:

    - MappingStore (entity, COMPANY/PERSON): MappingEntry добавляется
      немедленно, по мере обработки ячеек (как и всегда на Stage 7A) —
      успешно добавленные ДО ошибки записи остаются в store,
      отката/rollback нет. Это открытый вопрос будущих этапов, а не то,
      что здесь маскируется;
    - IdentifierMappingStore (Stage 7B.5): новые IdentifierMappingEntry
      накапливаются ТОЛЬКО в памяти во время обработки и передаются в
      identifier_store.add_many(...) ОДИН раз, только после успешного
      построения ВСЕЙ результирующей таблицы — любая ошибка (в том числе
      в более поздней entity-ячейке) оставляет identifier_store
      полностью нетронутым.

Любая ошибка MappingStore/IdentifierMappingStore (MappingConflictError,
IdentifierMappingConflictError и т.п.) всегда распространяется наружу
как есть, а не подавляется.

provenance_store (Stage 7C.4) следует ТОЙ ЖЕ схеме, что и identifier_store,
но КОММИТИТСЯ ПОСЛЕ него: новые IdentifierCellProvenance накапливаются
только в памяти и передаются в provenance_store.add_many(...) один раз,
сразу после identifier_store.add_many(...) и до return. Если
identifier-коммит падает — provenance_store.add_many не вызывается вообще.
Если identifier-коммит уже успешен, а падает provenance-коммит — исключение
распространяется, результирующая таблица не возвращается, но
identifier_store к этому моменту уже может содержать новые записи. Эта
асимметрия сознательно принята (см. Stage 7C.4 design review) — без
rollback, 2PC или кросс-файловой транзакции.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Callable, Optional

from app.detectors import is_valid_inn, is_valid_kpp, is_valid_ogrn, is_valid_ogrnip
from app.excel.models import CellRecord, FlatTable
from app.mapping.base import MappingStore
from app.mapping.identifier_base import IdentifierMappingStore
from app.mapping.provenance_base import ProvenanceStore
from app.models.entities import EntityType, MappingEntry
from app.models.identifiers import IdentifierMappingEntry, IdentifierType
from app.models.provenance import IdentifierCellProvenance, IdentifierRepresentation
from app.models.rules import Action, FieldRule, FieldType
from app.security.alias_generator import generate_alias, generate_identifier_token

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
    Action.PSEUDONYMIZE запрошен для FieldType, для которого сознательно
    не определена ни entity pseudonymization, ни identifier tokenization
    (см. docstring модуля). Поддержаны:
        - entity pseudonymization (Stage 7A): FieldType.COMPANY,
          FieldType.PERSON;
        - identifier tokenization (Stage 7B.5): FieldType.INN,
          FieldType.KPP, FieldType.OGRN.
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


class MissingIdentifierStoreError(InvalidRuleConfigurationError):
    """
    rules содержит хотя бы одно правило Action.PSEUDONYMIZE для
    FieldType.INN/KPP/OGRN, но identifier_store не передан (None).

    Обнаруживается на том же pre-row-processing этапе, что и остальные
    InvalidRuleConfigurationError — до обработки строк и до mutation
    mapping_store/identifier_store.
    """


class InvalidIdentifierValueError(AnonymizationError):
    """
    Непустое значение ячейки под Action.PSEUDONYMIZE для
    FieldType.INN/KPP/OGRN не прошло проверку соответствующего Stage 5
    public-валидатора (is_valid_inn/is_valid_kpp/is_valid_ogrn/
    is_valid_ogrnip) — неверный Python-тип (bool/float/datetime/...) либо
    неверная структура/контрольная сумма при верном типе (str/int).

    Один и тот же exception используется для обеих причин отказа: сам
    валидатор возвращает единый bool, не различая "неверный тип" и
    "неверная структура", поэтому и здесь нет отдельных подклассов на
    каждую причину (см. Stage 7B.5 design review).

    Сообщение содержит только числовые координаты (row/column) — та же
    конвенция, что и у InvalidPseudonymizationValueError. Никогда не
    включает: сырое значение идентификатора, repr(value), sheet_name,
    текст формулы, полный CellRecord.
    """


# ----------------------------------------------------------------------
# FieldType -> EntityType (только то, что реально поддержано Stage 7A)
# ----------------------------------------------------------------------

_PSEUDONYMIZABLE_FIELD_TYPES: dict[FieldType, EntityType] = {
    FieldType.COMPANY: EntityType.COMPANY,
    FieldType.PERSON: EntityType.PERSON,
}

# ----------------------------------------------------------------------
# FieldType -> IdentifierType (Stage 7B.5) — отдельная от entity map:
# identifiers — не сущности (EntityType.INN/KPP/OGRN не существует).
# ----------------------------------------------------------------------

_IDENTIFIER_FIELD_TYPES: dict[FieldType, IdentifierType] = {
    FieldType.INN: IdentifierType.INN,
    FieldType.KPP: IdentifierType.KPP,
    FieldType.OGRN: IdentifierType.OGRN,
}

# Диспетчер Stage 5 public-валидаторов по IdentifierType. Используются
# КАК ЕСТЬ, без дублирования приватной _coerce_identifier_candidate: сама
# type-policy (bool/float отвергаются, str/int допускаются) уже целиком
# реализована внутри этих функций. Для OGRN валидным считается и ОГРН
# (13 цифр), и ОГРНИП (15 цифр) — оба относятся к IdentifierType.OGRN
# (см. app.models.identifiers).
_IDENTIFIER_VALIDATORS: dict[IdentifierType, Callable[[object], bool]] = {
    IdentifierType.INN: is_valid_inn,
    IdentifierType.KPP: is_valid_kpp,
    IdentifierType.OGRN: lambda value: is_valid_ogrn(value) or is_valid_ogrnip(value),
}


# ----------------------------------------------------------------------
# Публичный API
# ----------------------------------------------------------------------


def anonymize_flat_table(
    table: FlatTable,
    rules: Mapping[int, FieldRule],
    mapping_store: MappingStore,
    *,
    identifier_store: Optional[IdentifierMappingStore] = None,
    provenance_store: Optional[ProvenanceStore] = None,
) -> FlatTable:
    """
    Применяет rules (индекс колонки -> FieldRule) к table через
    mapping_store (и, при необходимости, identifier_store/
    provenance_store) и возвращает НОВЫЙ FlatTable.

    :param table: исходная таблица; не изменяется (физически не может
        быть изменена — CellRecord/FlatTable неизменяемы).
    :param rules: 1-based индекс колонки (как CellRecord.column) ->
        FieldRule. Должен покрывать РОВНО те колонки, что есть в table —
        ни одной пропущенной, ни одной лишней.
    :param mapping_store: уже открытый MappingStore; эта функция не
        создаёт, не открывает и не закрывает хранилище и ничего не знает
        про пароли/шифрование/файлы.
    :param identifier_store: уже открытый IdentifierMappingStore — нужен,
        только если rules содержит хотя бы одно Action.PSEUDONYMIZE для
        FieldType.INN/KPP/OGRN (Stage 7B.5). Если таких правил нет,
        identifier_store допустимо не передавать (None) и он вообще не
        читается — ни all_tokens(), ни add_many(), ни любой другой метод.
    :param provenance_store: уже открытый ProvenanceStore (Stage 7C.4) —
        опционален независимо от identifier_store. Если передан, для
        каждой успешно токенизированной identifier-ячейки создаётся одна
        IdentifierCellProvenance (coordinate = table.sheet_name +
        CellRecord.row/column, representation — по исходному
        Python-типу значения ячейки). Если не передан (None), никакая
        provenance-работа не выполняется. Никогда не заменяет требование
        identifier_store — см. MissingIdentifierStoreError.

    ======================================================================
    Транзакционность: ДВЕ разные гарантии для двух store (Stage 7B.5)
    ======================================================================

    Эта функция НЕ является полностью atomic. У entity- и identifier-
    сторон РАЗНЫЕ, явно различные гарантии:

        - mapping_store (entity: COMPANY/PERSON) — сохраняет исходную
          Stage 7A eager/partial-failure семантику БЕЗ ИЗМЕНЕНИЙ:
          MappingEntry добавляется немедленно, по мере обработки ячеек;
          если ошибка возникает позже (в любой ячейке, включая
          identifier), уже добавленные entity-записи остаются в
          mapping_store — отката нет (как и на Stage 7A).

        - identifier_store (INN/KPP/OGRN) — получает более сильную
          гарантию: новые IdentifierMappingEntry накапливаются ТОЛЬКО в
          памяти (pending_entries) во время обработки и передаются в
          identifier_store.add_many(...) ОДИН раз, только после успешного
          построения ВСЕЙ результирующей таблицы. Любая ошибка до этого
          момента (в любой ячейке, включая более позднюю entity-ячейку)
          оставляет identifier_store полностью нетронутым — all-or-nothing
          относительно обработки всей таблицы.

    Эта асимметрия — сознательное решение (см. Stage 7B.5 design review),
    а не недосмотр: mapping_store не имеет batch-API (Stage 2-4 не
    меняются), а identifier_store уже спроектирован для all-or-nothing
    batch-вставки (Stage 7B.4.3) — грех было бы не использовать её здесь.

    provenance_store (Stage 7C.4), если передан, коммитится ТЕМ ЖЕ
    all-or-nothing batch-способом, но СТРОГО ПОСЛЕ identifier_store: если
    identifier_store.add_many(...) падает — provenance_store.add_many(...)
    не вызывается вообще; если identifier-коммит уже прошёл успешно, а
    провалился provenance-коммит — identifier_store к этому моменту уже
    может содержать новые записи, а результирующая таблица не
    возвращается. Это принятая асимметрия (см. Stage 7C.4 design review),
    без rollback/2PC/кросс-файловой транзакции.

    :raises InvalidRuleConfigurationError: некорректное покрытие/ключи rules.
    :raises MissingIdentifierStoreError: rules требует identifier
        tokenization, но identifier_store не передан. Поднимается до
        обработки строк и до mutation mapping_store/identifier_store.
    :raises UnsupportedPseudonymizationTargetError: PSEUDONYMIZE запрошен
        для FieldType, не поддержанного ни entity-, ни identifier-путём.
    :raises InvalidPseudonymizationValueError: непустое non-str значение
        под PSEUDONYMIZE для COMPANY/PERSON.
    :raises InvalidIdentifierValueError: непустое значение под
        PSEUDONYMIZE для INN/KPP/OGRN не прошло Stage 5 validator.
    :raises FormulaPseudonymizationError: строка формулы под PSEUDONYMIZE.
    :raises MappingConflictError: как есть, из mapping_store.add(...).
    :raises IdentifierMappingConflictError: как есть, из
        identifier_store.add_many(...).
    :raises ProvenanceConflictError: как есть, из
        provenance_store.add_many(...).
    """
    _validate_rules(table, rules)
    _validate_pseudonymization_targets(rules)

    needs_identifier_store = _requires_identifier_store(rules)
    if needs_identifier_store and identifier_store is None:
        raise MissingIdentifierStoreError(
            "rules содержит Action.PSEUDONYMIZE для FieldType.INN/KPP/OGRN, "
            "но identifier_store не передан"
        )

    # Один раз за весь вызов — не на каждую ячейку/сущность (см. docstring
    # модуля, раздел про O(n²)). Локальная копия, безопасная для мутации.
    existing_aliases: set[str] = set(mapping_store.all_aliases())

    # identifier_store читается ТОЛЬКО если он реально нужен — если ни
    # одно правило не требует identifier tokenization, all_tokens() не
    # вызывается вообще, даже если identifier_store передан.
    used_tokens: set[str] = set(identifier_store.all_tokens()) if needs_identifier_store else set()
    pending_by_identity: dict[tuple[IdentifierType, str], str] = {}
    pending_entries: list[IdentifierMappingEntry] = []

    # Provenance-работа выполняется только если provenance_store реально
    # передан — None здесь означает "не собирать provenance вообще", а не
    # "собрать и не сохранить" (см. docstring модуля, Stage 7C.4).
    pending_provenance_entries: Optional[list[IdentifierCellProvenance]] = (
        [] if provenance_store is not None else None
    )

    new_rows = tuple(
        _anonymize_row(
            row,
            rules,
            mapping_store,
            existing_aliases,
            identifier_store,
            used_tokens,
            pending_by_identity,
            pending_entries,
            table.sheet_name,
            pending_provenance_entries,
        )
        for row in table.rows
    )

    candidate = FlatTable(sheet_name=table.sheet_name, header_row=table.header_row, rows=new_rows)

    # Ровно один batch-commit, только если появилась хотя бы одна новая
    # identifier-запись; если всё было либо passthrough, либо reuse уже
    # существующих identity — identifier_store вообще не переписывается
    # (см. EncryptedFileIdentifierMappingStore.add_many, Stage 7B.4.3).
    if pending_entries:
        identifier_store.add_many(pending_entries)  # type: ignore[union-attr]

    # Provenance-коммит СТРОГО ПОСЛЕ identifier-коммита (frozen order,
    # Stage 7C.4 design review) — если предыдущий шаг падает, до этой
    # строки исполнение не доходит.
    if pending_provenance_entries:
        provenance_store.add_many(pending_provenance_entries)  # type: ignore[union-attr]

    return candidate


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
        if (
            rule.action is Action.PSEUDONYMIZE
            and rule.field_type not in _PSEUDONYMIZABLE_FIELD_TYPES
            and rule.field_type not in _IDENTIFIER_FIELD_TYPES
        ):
            raise UnsupportedPseudonymizationTargetError(
                f"Колонка {column}: Action.PSEUDONYMIZE не поддержан для "
                f"FieldType.{rule.field_type.name} (поддержаны только COMPANY, "
                "PERSON, INN, KPP и OGRN)"
            )


def _requires_identifier_store(rules: Mapping[int, FieldRule]) -> bool:
    return any(
        rule.action is Action.PSEUDONYMIZE and rule.field_type in _IDENTIFIER_FIELD_TYPES
        for rule in rules.values()
    )


# ----------------------------------------------------------------------
# Построчная/поячейковая обработка
# ----------------------------------------------------------------------


def _anonymize_row(
    row: tuple[CellRecord, ...],
    rules: Mapping[int, FieldRule],
    mapping_store: MappingStore,
    existing_aliases: set[str],
    identifier_store: Optional[IdentifierMappingStore],
    used_tokens: set[str],
    pending_by_identity: dict[tuple[IdentifierType, str], str],
    pending_entries: list[IdentifierMappingEntry],
    sheet_name: str,
    pending_provenance_entries: Optional[list[IdentifierCellProvenance]],
) -> tuple[CellRecord, ...]:
    return tuple(
        _anonymize_cell(
            cell,
            rules[cell.column],
            mapping_store,
            existing_aliases,
            identifier_store,
            used_tokens,
            pending_by_identity,
            pending_entries,
            sheet_name,
            pending_provenance_entries,
        )
        for cell in row
    )


def _anonymize_cell(
    cell: CellRecord,
    rule: FieldRule,
    mapping_store: MappingStore,
    existing_aliases: set[str],
    identifier_store: Optional[IdentifierMappingStore],
    used_tokens: set[str],
    pending_by_identity: dict[tuple[IdentifierType, str], str],
    pending_entries: list[IdentifierMappingEntry],
    sheet_name: str,
    pending_provenance_entries: Optional[list[IdentifierCellProvenance]],
) -> CellRecord:
    if rule.action is Action.KEEP:
        return cell

    if rule.action is Action.REMOVE:
        return CellRecord(row=cell.row, column=cell.column, value=None)

    # Action.PSEUDONYMIZE — единственный оставшийся вариант; конфигурация
    # уже проверена в _validate_pseudonymization_targets. identifier_store
    # для REMOVE/KEEP выше не читается вообще, независимо от field_type.
    if rule.field_type in _IDENTIFIER_FIELD_TYPES:
        return _pseudonymize_identifier_cell(
            cell,
            _IDENTIFIER_FIELD_TYPES[rule.field_type],
            used_tokens,
            pending_by_identity,
            pending_entries,
            identifier_store,  # type: ignore[arg-type]
            sheet_name,
            pending_provenance_entries,
        )

    return _pseudonymize_entity_cell(cell, rule.field_type, mapping_store, existing_aliases)


def _pseudonymize_entity_cell(
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


def _pseudonymize_identifier_cell(
    cell: CellRecord,
    identifier_type: IdentifierType,
    used_tokens: set[str],
    pending_by_identity: dict[tuple[IdentifierType, str], str],
    pending_entries: list[IdentifierMappingEntry],
    identifier_store: IdentifierMappingStore,
    sheet_name: str,
    pending_provenance_entries: Optional[list[IdentifierCellProvenance]],
) -> CellRecord:
    """
    Stage 7B.5: identifier tokenization для одной ячейки FieldType.INN/
    KPP/OGRN под Action.PSEUDONYMIZE.

    identifier_store НЕ мутируется здесь — новая IdentifierMappingEntry
    (если понадобилась) только добавляется в pending_entries; фактический
    identifier_store.add_many(pending_entries) происходит один раз, после
    полной успешной обработки всей таблицы (см. anonymize_flat_table).

    Stage 7C.4: если pending_provenance_entries не None (т.е. вызывающая
    сторона передала provenance_store), для ЛЮБОЙ успешно токенизированной
    ячейки — независимо от того, был token переиспользован (persisted/
    pending) или сгенерирован заново — в pending_provenance_entries
    добавляется одна IdentifierCellProvenance с EXACT координатой этой
    ячейки и EXACT токеном, который реально попадёт в возвращаемый
    CellRecord. provenance_store сам НЕ мутируется здесь.
    """
    value = cell.value

    # Пустые/пробельные значения — exact passthrough, без
    # IdentifierMappingEntry и без обращения к validator/identifier_store
    # (тот же контракт, что и для COMPANY/PERSON).
    if value is None or (isinstance(value, str) and value.strip() == ""):
        return cell

    if isinstance(value, str) and value.startswith("="):
        raise FormulaPseudonymizationError(
            f"Ячейка (row={cell.row}, column={cell.column}) содержит формулу Excel "
            f"и не может быть псевдонимизирована на Stage 7A: {value!r}"
        )

    validator = _IDENTIFIER_VALIDATORS[identifier_type]
    if not validator(value):
        raise InvalidIdentifierValueError(
            f"Ячейка (row={cell.row}, column={cell.column}): значение не является "
            f"допустимым {identifier_type.value.upper()}"
        )

    # Успешная валидация гарантирует (по контракту is_valid_*/
    # _coerce_identifier_candidate — Stage 5), что value — str либо int
    # (не bool): любой другой тип уже дал False выше. identifier_value —
    # EXACT raw-представление, НИКОГДА stripped-candidate валидатора: для
    # str это сама строка как есть (внешние пробелы сохраняются и создают
    # отдельную identity); для int — точное десятичное str(value), без
    # восстановления ведущих нулей (int их не хранит — это принятая
    # граница ответственности, не решается здесь, см. docstring модуля).
    identifier_value = value if isinstance(value, str) else str(value)

    # Representation (Stage 7C.4) — по ИСХОДНОМУ Python-типу value, той же
    # проверкой isinstance(value, str), что уже используется выше для
    # identifier_value: не по identifier_value/token/validator.
    representation = (
        IdentifierRepresentation.STRING if isinstance(value, str) else IdentifierRepresentation.INTEGER
    )

    identity = (identifier_type, identifier_value)

    existing_entry = identifier_store.get_by_identity(identifier_type, identifier_value)
    if existing_entry is not None:
        token = existing_entry.token
    elif identity in pending_by_identity:
        token = pending_by_identity[identity]
    else:
        token = generate_identifier_token(identifier_type, used_tokens)
        used_tokens.add(token)
        pending_by_identity[identity] = token
        pending_entries.append(
            IdentifierMappingEntry(
                token=token, identifier_value=identifier_value, identifier_type=identifier_type
            )
        )

    # Provenance — per-cell, а не per-new-mapping: добавляется во ВСЕХ
    # трёх ветках выше (persisted/pending/generated), только если
    # provenance_store вообще был передан вызывающей стороной.
    if pending_provenance_entries is not None:
        pending_provenance_entries.append(
            IdentifierCellProvenance(
                sheet_name=sheet_name,
                row=cell.row,
                column=cell.column,
                token=token,
                representation=representation,
            )
        )

    return CellRecord(row=cell.row, column=cell.column, value=token)
