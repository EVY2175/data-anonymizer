"""
Single-file anonymization orchestration (Stage 8.3).

Композиция уже существующих компонентов (app.excel.reader, app.anonymizer,
app.excel.writer) в одну операцию "один source workbook -> один output
workbook + один encrypted provenance sidecar", без изменения ни одного из
их frozen-контрактов.

======================================================================
Persistent stores vs. per-job artifacts (frozen Stage 8.3 contract)
======================================================================

MappingStore и IdentifierMappingStore — ДОЛГОЖИВУЩИЕ хранилища
соответствий, создаваемые/открываемые вызывающей стороной и передаваемые
сюда через dependency injection. Этот модуль:

    - НЕ создаёт их;
    - НЕ знает их пароли;
    - НЕ закрывает, НЕ очищает (clear()), НЕ удаляет их;
    - НЕ связывает их с job_id.

Именно поэтому одна и та же exact identity (компания/идентификатор) при
использовании ОДНОГО И ТОГО ЖЕ persistent store между разными вызовами
anonymize_workbook (разные месяцы, разные периоды) получает один и тот же
pseudonym/token — эта стабильность целиком обеспечивается уже
существующим MappingStore.get_by_real_value/IdentifierMappingStore.
get_by_identity (app.anonymizer), а не чем-либо в этом модуле.

job_id, напротив, — НОВЫЙ opaque artifact на КАЖДЫЙ вызов
anonymize_workbook: генерируется здесь (secrets.token_hex(16)), ровно
один раз за job, никогда не выводится из filename/path/содержимого
книги/company-данных, никогда не сохраняется в MappingStore или
IdentifierMappingStore — только в ProvenanceStore и, через Writer, в
workbook-metadata. job_id и persistent mapping identity — архитектурно
полностью развязанные понятия.

======================================================================
ProvenanceStore ownership — асимметрия с MappingStore, намеренная
======================================================================

В отличие от MappingStore/IdentifierMappingStore (injected), ProvenanceStore
создаётся ЭТИМ модулем из переданных provenance_path/provenance_password —
потому что конструктор нового EncryptedFileProvenanceStore неразрывно
требует job_id, а job_id генерируется именно здесь. Возможность передать
уже готовый ProvenanceStore извне намеренно не предусмотрена: единственный
архитектурно чистый вариант — чтобы "кто генерирует job_id" и "кто создаёт
sidecar" совпадали.

Stage 8.3 не поддерживает anonymization без provenance: provenance_path/
provenance_password — обязательные (не Optional) keyword-only параметры.

======================================================================
Preflight — обязательный early-fail этап
======================================================================

anonymize_workbook начинает с preflight validation ДО чтения workbook, ДО
генерации job_id, ДО создания ProvenanceStore, ДО anonymize_flat_table и
ДО Writer. Preflight проверяет только форму job-конфигурации (типы путей,
расширения, существование/родительские каталоги, попарное различие
source/destination/provenance, отсутствие коллизии provenance_path,
структуру rules_by_sheet, минимальный password-контракт) — он НЕ читает
содержимое workbook и НЕ проверяет существование конкретного worksheet
внутри книги (это уже требует чтения — забота read-phase). Единственное
собственное исключение этого модуля — OrchestrationValidationError —
поднимается ТОЛЬКО preflight'ом; исключения leaf-компонентов (Reader,
anonymizer, stores, Writer, crypto) никогда не оборачиваются и
пробрасываются как есть.

======================================================================
Read-all-before-job
======================================================================

ВСЕ выбранные worksheets читаются ПОЛНОСТЬЮ до генерации job_id и до
создания ProvenanceStore. Если чтение любого листа падает — job_id не
создаётся, sidecar не создаётся, ни один store не мутирован, Writer не
вызывается. Это не устраняет более поздние асимметрии (см. ниже), но
минимизирует число частичных job-артефактов для самого раннего класса
ошибок (отсутствующий/повреждённый лист).

======================================================================
Наследуемые асимметрии (НЕ устраняются этим модулем)
======================================================================

MappingStore.add() — eager, без batch: entity-алиасы могут стать durable
до более позднего сбоя того же job (в том числе на более позднем листе
той же книги). IdentifierMappingStore.add_many/ProvenanceStore.add_many —
all-or-nothing каждый для своего батча, с уже принятой асимметрией
"identifier commit успешен, provenance commit падает" (frozen Stage 7C.4).
Эта функция вызывает anonymize_flat_table последовательно по одному разу
на каждый выбранный лист — значит к моменту вызова Writer'а
identifier/provenance-коммиты для ВСЕХ успешно обработанных листов уже
состоялись. Ни rollback, ни 2PC, ни cross-store transaction здесь не
вводятся — как и automatic cleanup/удаление provenance sidecar при любом
последующем сбое (sidecar остаётся допустимым failed-job artifact; retry
использует новый job_id и новый provenance_path).

Writer остаётся единственным владельцем atomic workbook output — этот
модуль не создаёт второй temp-слой и не вызывает os.replace самостоятельно.
"""

from __future__ import annotations

import os
import secrets
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Union

from app.anonymizer import anonymize_flat_table
from app.excel.reader import read_flat_table
from app.excel.writer import write_anonymized_workbook
from app.mapping.base import MappingStore
from app.mapping.identifier_base import IdentifierMappingStore
from app.mapping.provenance_encrypted import EncryptedFileProvenanceStore
from app.models.rules import FieldRule

_PathLike = Union[str, Path]

_SUPPORTED_EXTENSION = ".xlsx"

_MaterializedRules = tuple[tuple[str, Mapping[int, FieldRule]], ...]


class OrchestrationValidationError(ValueError):
    """
    Job-конфигурация не удовлетворяет preflight-контракту этого модуля:
    неверный тип/расширение пути, отсутствующий source, отсутствующий
    родительский каталог destination/provenance, совпадение source/
    destination/provenance, уже существующий provenance_path, невалидная
    структура rules_by_sheet, пустой provenance_password.

    Используется ИСКЛЮЧИТЕЛЬНО для собственных preflight-проверок этого
    модуля — исключения Reader/anonymizer/stores/Writer/crypto никогда не
    оборачиваются в этот класс и пробрасываются как есть.

    Сообщение никогда не включает: значения ячеек, реальные названия
    компаний/ФИО, сырые идентификаторы, содержимое mapping, пароли.
    """


@dataclass(frozen=True)
class AnonymizationJobResult:
    """
    Минимальный результат успешного anonymize_workbook — только
    операционные метаданные, без confidential-данных: ни MappingEntry/
    IdentifierMappingEntry/IdentifierCellProvenance, ни real_value/сырых
    идентификаторов, ни паролей.
    """

    job_id: str
    output_path: Path
    provenance_path: Path
    processed_sheet_names: tuple[str, ...]


def anonymize_workbook(
    source_path: _PathLike,
    destination_path: _PathLike,
    rules_by_sheet: Mapping[str, Mapping[int, FieldRule]],
    mapping_store: MappingStore,
    identifier_store: Optional[IdentifierMappingStore],
    *,
    provenance_path: _PathLike,
    provenance_password: str,
) -> AnonymizationJobResult:
    """
    Анонимизирует выбранные worksheets одной source-книги и записывает
    результат в destination-книгу вместе с encrypted provenance sidecar.

    :param source_path: путь к исходной .xlsx книге; только читается.
    :param destination_path: путь к выходной .xlsx книге; должен
        отличаться от source_path; родительский каталог должен уже
        существовать.
    :param rules_by_sheet: sheet_name -> (1-based индекс колонки ->
        FieldRule) для каждого выбранного листа; ключи sheet_name задают
        и набор обрабатываемых листов, и порядок их обработки.
    :param mapping_store: уже открытый, долгоживущий MappingStore —
        не создаётся и не закрывается этой функцией.
    :param identifier_store: уже открытый, долгоживущий
        IdentifierMappingStore, либо None, если ни один лист не требует
        identifier tokenization (см. anonymize_flat_table).
    :param provenance_path: путь к НОВОМУ encrypted provenance sidecar;
        должен ещё не существовать.
    :param provenance_password: пароль нового provenance sidecar; opaque
        non-empty строка (exact crypto-контракт, без нормализации).

    :returns: AnonymizationJobResult только после успешного Writer.

    :raises TypeError: source_path/destination_path/provenance_path не
        str/Path, либо provenance_password не str.
    :raises OrchestrationValidationError: любое нарушение preflight-
        контракта этого модуля (см. docstring класса).
    :raises Exception: любое исключение Reader/anonymizer/stores/Writer
        распространяется как есть, без оборачивания.
    """
    source, destination, provenance, materialized_rules = _preflight(
        source_path,
        destination_path,
        provenance_path,
        rules_by_sheet,
        provenance_password,
    )

    # Read-all-before-job: если чтение любого листа падает, код ниже
    # (генерация job_id, создание ProvenanceStore, anonymizer, Writer)
    # не выполняется вообще — исключение из этого tuple() пробрасывается
    # немедленно.
    tables = tuple(
        read_flat_table(source, sheet_name=sheet_name)
        for sheet_name, _ in materialized_rules
    )

    job_id = secrets.token_hex(16)

    provenance_store = EncryptedFileProvenanceStore(
        provenance, provenance_password, job_id=job_id
    )

    # Один и тот же mapping_store/identifier_store/provenance_store для
    # ВСЕХ листов — обеспечивает cross-sheet identity-консистентность
    # внутри одной книги бесплатно (anonymize_flat_table сам перечитывает
    # all_aliases()/all_tokens() в начале каждого вызова).
    anonymized_tables = tuple(
        anonymize_flat_table(
            table,
            column_rules,
            mapping_store,
            identifier_store=identifier_store,
            provenance_store=provenance_store,
        )
        for table, (_, column_rules) in zip(tables, materialized_rules)
    )

    write_anonymized_workbook(source, anonymized_tables, destination, job_id=job_id)

    return AnonymizationJobResult(
        job_id=job_id,
        output_path=destination,
        provenance_path=provenance,
        processed_sheet_names=tuple(sheet_name for sheet_name, _ in materialized_rules),
    )


# ----------------------------------------------------------------------
# Preflight
# ----------------------------------------------------------------------


def _preflight(
    source_path: object,
    destination_path: object,
    provenance_path: object,
    rules_by_sheet: object,
    provenance_password: object,
) -> tuple[Path, Path, Path, _MaterializedRules]:
    _validate_path_type(source_path, "source_path")
    _validate_path_type(destination_path, "destination_path")
    _validate_path_type(provenance_path, "provenance_path")

    source = Path(source_path)
    destination = Path(destination_path)
    provenance = Path(provenance_path)

    if source.suffix.lower() != _SUPPORTED_EXTENSION:
        raise OrchestrationValidationError(
            f"source_path должен иметь расширение {_SUPPORTED_EXTENSION}, "
            f"получено: {source.suffix!r}"
        )
    if destination.suffix.lower() != _SUPPORTED_EXTENSION:
        raise OrchestrationValidationError(
            f"destination_path должен иметь расширение {_SUPPORTED_EXTENSION}, "
            f"получено: {destination.suffix!r}"
        )

    if not source.exists():
        raise OrchestrationValidationError(f"source_path не существует: {source}")
    if not source.is_file():
        raise OrchestrationValidationError(f"source_path должен быть файлом: {source}")

    if not destination.parent.exists():
        raise OrchestrationValidationError(
            f"Родительский каталог destination_path не существует: {destination.parent}"
        )
    if not provenance.parent.exists():
        raise OrchestrationValidationError(
            f"Родительский каталог provenance_path не существует: {provenance.parent}"
        )

    if _paths_refer_to_same_file(source, destination):
        raise OrchestrationValidationError(
            "source_path и destination_path не должны совпадать"
        )
    if _paths_refer_to_same_file(provenance, source):
        raise OrchestrationValidationError(
            "provenance_path не должен совпадать с source_path"
        )
    if _paths_refer_to_same_file(provenance, destination):
        raise OrchestrationValidationError(
            "provenance_path не должен совпадать с destination_path"
        )

    # Коллизия обнаруживается здесь, простой файловой проверкой, ДО
    # Reader и ДО попытки конструирования EncryptedFileProvenanceStore —
    # существующий файл не открывается, не читается, не изменяется.
    if provenance.exists():
        raise OrchestrationValidationError(
            f"provenance_path уже существует: {provenance}"
        )

    materialized_rules = _validate_rules_by_sheet(rules_by_sheet)

    _validate_provenance_password(provenance_password)

    return source, destination, provenance, materialized_rules


def _validate_path_type(value: object, name: str) -> None:
    if not isinstance(value, (str, Path)):
        raise TypeError(f"{name} должен быть str или Path, получено: {type(value)!r}")


def _paths_refer_to_same_file(a: Path, b: Path) -> bool:
    """
    Та же семантика, что и у app.excel.writer._paths_refer_to_same_file
    (frozen Stage 8.2 contract): resolve() устраняет относительность/'..',
    os.path.normcase учитывает регистронезависимость Windows. Writer не
    экспортирует свою приватную функцию как public API, поэтому здесь она
    реализована заново с ТОЧНО той же формулой, а не импортирована.
    """
    return os.path.normcase(str(a.resolve())) == os.path.normcase(str(b.resolve()))


def _validate_rules_by_sheet(rules_by_sheet: object) -> _MaterializedRules:
    """
    Только структурная проверка формы rules_by_sheet — не дублирует
    semantic-валидацию FieldRule/anonymize_flat_table (покрытие колонок
    table, существование листа в книге и т.п. остаются их зоной
    ответственности). Материализует ВНЕШНИЙ rules_by_sheet.items() РОВНО
    ОДИН РАЗ здесь: дальнейшая обработка job'а больше никогда не
    обращается к исходному объекту rules_by_sheet напрямую — набор
    выбранных листов и порядок их обработки фиксируются этим снимком.

    ВАЖНО (граница гарантии): материализация НЕ глубокая. Значения
    внешнего Mapping — вложенные per-sheet Mapping[int, FieldRule] —
    остаются ТЕМИ ЖЕ объектами, что передала вызывающая сторона (не
    копируются), и сами FieldRule — mutable dataclass (app.models.rules,
    намеренно немораживаемый ради внешнего workflow редактирования
    правил пользователем) — тоже не клонируются и не замораживаются.
    Для типичного однопоточного синхронного вызова это не проблема
    (вызывающий код физически не может выполниться параллельно с уже
    идущим job'ом). Конкурентная мутация per-sheet Mapping/FieldRule из
    другого потока ВО ВРЕМЯ выполнения anonymize_workbook не
    поддерживается и не гарантируется этим слоем — такой сценарий вне
    границ frozen-контракта Stage 8.3 и сознательно не решается здесь
    (ни deep copy, ни заморозка FieldRule не вводятся).
    """
    if not isinstance(rules_by_sheet, Mapping):
        raise OrchestrationValidationError(
            f"rules_by_sheet должен быть Mapping, получено: {type(rules_by_sheet)!r}"
        )

    items = tuple(rules_by_sheet.items())
    if not items:
        raise OrchestrationValidationError("rules_by_sheet не должен быть пустым")

    for sheet_name, column_rules in items:
        if not isinstance(sheet_name, str) or not sheet_name.strip():
            raise OrchestrationValidationError(
                f"Ключ rules_by_sheet должен быть непустой строкой, получено: {sheet_name!r}"
            )
        if not isinstance(column_rules, Mapping):
            raise OrchestrationValidationError(
                f"Лист {sheet_name!r}: rules_by_sheet[sheet_name] должен быть "
                f"Mapping, получено: {type(column_rules)!r}"
            )

        for column_index, rule in column_rules.items():
            # bool — подкласс int; существующий anonymizer._validate_rules
            # (app.anonymizer) явно отвергает bool как column index —
            # preflight обязан обнаружить это же нарушение раньше, а не
            # полагаться на то, что anonymizer поднимет ошибку позже.
            if isinstance(column_index, bool) or not isinstance(column_index, int):
                raise OrchestrationValidationError(
                    f"Лист {sheet_name!r}: ключ column index должен быть int, "
                    f"получено: {type(column_index)!r}"
                )
            if not isinstance(rule, FieldRule):
                raise OrchestrationValidationError(
                    f"Лист {sheet_name!r}: значение правила должно быть FieldRule, "
                    f"получено: {type(rule)!r}"
                )

    return items


def _validate_provenance_password(password: object) -> None:
    # Точное зеркалирование app.security.crypto._validate_password:
    # exact str, пустая строка "" отклоняется БЕЗ .strip() — whitespace-
    # only пароль ("   ") принимается, т.к. существующий crypto-контракт
    # его принимает. Никаких новых правил длины/сложности/нормализации.
    if not isinstance(password, str):
        raise TypeError(f"provenance_password должен быть str, получено: {type(password)!r}")
    if password == "":
        raise OrchestrationValidationError("provenance_password не может быть пустым")
