"""
Multi-file batch anonymization orchestration (Stage 8B).

Тонкий слой поверх уже существующего single-file orchestration
(app.orchestration.single_file.anonymize_workbook, Stage 8.3): применяет
его РОВНО один раз к каждому элементу batch, последовательно, в input
order. Этот модуль НЕ повторяет Reader/Anonymizer/Writer pipeline, НЕ
создаёт ProvenanceStore, НЕ генерирует job_id, НЕ читает/пишет Excel
самостоятельно — вся фактическая работа делегируется anonymize_workbook.

======================================================================
Persistent stores — та же injected identity, что и в Stage 8.3
======================================================================

mapping_store и identifier_store передаются вызывающей стороной и
передаются КАЖДОМУ вызову anonymize_workbook как ОДИН И ТОТ ЖЕ объект —
этот модуль их не создаёт, не копирует, не очищает (clear()), не
закрывает, не удаляет, не заменяет. Именно переиспользование одного и
того же persistent store между items batch (а не что-либо в этом модуле)
обеспечивает: одна и та же exact raw company/identifier identity в
разных файлах batch получает один и тот же alias/token.

======================================================================
Per-item state и job_id — не батчевые, а per-call
======================================================================

Каждый item несёт собственные source_path/destination_path/
provenance_path/rules_by_sheet. Каждый вызов anonymize_workbook
генерирует собственный job_id и создаёт собственный
EncryptedFileProvenanceStore — независимо от других items. У batch НЕТ
собственного batch_id: он не даёт пользы ни для security, ни для
restoration (per-output identity уже полностью покрывается job_id
Stage 8.3), поэтому сознательно не вводится.

======================================================================
Materialization — ровно один раз
======================================================================

items материализуется в tuple ровно один раз, в самом начале
anonymize_workbooks, ДО batch preflight и ДО первого anonymize_workbook.
Если сам items (generator) поднимает исключение во время материализации,
оно пробрасывается как есть — до этого момента ни один store, ни один
файл ещё не тронуты.

======================================================================
Batch preflight — только то, что Stage 8.3 в принципе не может увидеть
======================================================================

Batch preflight выполняет дешёвые, не ветвящиеся filesystem/type/
extension-проверки по каждому item (тип item/путей, расширение .xlsx
source/destination, существование source, существование родительских
каталогов destination/provenance, отсутствие уже существующего
provenance_path) и полную межфайловую (cross-item) проверку коллизий
путей — единственное, что действительно требует видимости ВСЕГО batch
сразу, а не одного item за раз.

Batch preflight СОЗНАТЕЛЬНО НЕ дублирует сложную multi-branch
структурную валидацию rules_by_sheet (Mapping-типы, non-empty,
sheet-name/column-key validity, bool-исключение, FieldRule-instance
проверка) — приватная _validate_rules_by_sheet Stage 8.3 не
импортируется и не копируется, чтобы не создавать вторую расходящуюся
реализацию одного сложного контракта. Эта граница — принятое,
документированное MVP-ограничение: если item[0] корректен, а item[1]
содержит невалидный rules_by_sheet, item[0] может успешно завершиться
раньше, чем item[1] упадёт уже в Stage 8.3 (см. Stage 8B Contract
Freeze, раздел 24).

======================================================================
Path collisions — same-item и cross-item
======================================================================

Same-item: для каждого отдельного item source_path/destination_path/
provenance_path должны попарно различаться (та же формула path equality,
что Stage 8.3/Writer: os.path.normcase(str(path.resolve()))).

Cross-item: НИ ОДИН resolved путь одного item не должен совпадать
НИ С ОДНИМ resolved путём другого item, независимо от роли (source/
destination/provenance) — иначе последовательная обработка item A могла
бы перезаписать ещё не прочитанный source item B. Это единая, намеренно
строгая MVP-политика.

Обе категории коллизий обнаруживаются ДО первого anonymize_workbook —
существующий provenance_path проверяется простым Path.exists(), без
открытия/расшифровки.

======================================================================
Fail-fast — без транзакций и без rollback
======================================================================

Items обрабатываются строго последовательно, в input order, без
сортировки и без parallelize. Если item N падает — исключение (leaf
Stage 8.3/Reader/Anonymizer/Stores/Writer, либо, до начала обработки,
собственный BatchValidationError/TypeError этого модуля) пробрасывается
как есть, item N+1 и далее не запускаются, AnonymizationBatchResult не
возвращается вообще. Уже успешно завершённые предыдущие items (их
output/provenance/persistent-store изменения) НЕ откатываются — batch не
вводит глобальную транзакцию поверх уже существующих (различных) гарантий
отдельных stores/Writer.

Retry после partial failure — целиком ответственность вызывающей
стороны: новый batch только из failed/not-run items, с новыми
provenance_path для каждого повторяемого item (Stage 8.3 запрещает
переиспользование существующего provenance_path); те же persistent
stores обеспечивают alias/token continuity автоматически. Этот модуль
не хранит и не поддерживает никакого retry/resume-состояния.
"""

from __future__ import annotations

import os
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Union

from app.mapping.base import MappingStore
from app.mapping.identifier_base import IdentifierMappingStore
from app.models.rules import FieldRule
from app.orchestration.single_file import AnonymizationJobResult, anonymize_workbook

_PathLike = Union[str, Path]

_SUPPORTED_EXTENSION = ".xlsx"

_PATH_ROLES = ("source_path", "destination_path", "provenance_path")


class BatchValidationError(ValueError):
    """
    Batch-конфигурация не удовлетворяет preflight-контракту этого модуля:
    пустой batch, неверный тип item/путей, неверное расширение source/
    destination, отсутствующий source, отсутствующий родительский
    каталог destination/provenance, уже существующий provenance_path,
    совпадение путей внутри одного item, коллизия путей между разными
    items, пустой provenance_password.

    Используется ИСКЛЮЧИТЕЛЬНО для собственных preflight-проверок этого
    модуля — исключения anonymize_workbook (и его собственных leaf-
    компонентов: Reader/anonymizer/stores/Writer/crypto) никогда не
    оборачиваются в этот класс и пробрасываются как есть.

    Сообщение никогда не включает: значения ячеек, реальные названия
    компаний/ФИО, сырые идентификаторы, пароли, repr произвольных
    объектов item/path.
    """


@dataclass(frozen=True)
class AnonymizationBatchItem:
    """
    Один элемент batch — ровно то, что нужно для одного вызова
    anonymize_workbook: source/destination/provenance пути и
    rules_by_sheet. Без item_id/label/name/metadata/password/job_id/
    batch_id — эти поля не участвуют ни в одном бизнес-инварианте и
    создали бы лишнюю поверхность для случайного хранения confidential
    metadata.
    """

    source_path: _PathLike
    destination_path: _PathLike
    provenance_path: _PathLike
    rules_by_sheet: Mapping[str, Mapping[int, FieldRule]]


@dataclass(frozen=True)
class AnonymizationBatchResult:
    """
    Результат полностью успешного batch — просто набор уже существующих
    AnonymizationJobResult (Stage 8.3, каждый уже свободен от
    confidential-данных), в input order. Возвращается ТОЛЬКО если ВСЕ
    items batch завершились успешно (fail-fast — см. docstring модуля).
    """

    results: tuple[AnonymizationJobResult, ...]


def anonymize_workbooks(
    items: Iterable[AnonymizationBatchItem],
    mapping_store: MappingStore,
    identifier_store: Optional[IdentifierMappingStore],
    *,
    provenance_password: str,
) -> AnonymizationBatchResult:
    """
    Анонимизирует несколько workbook последовательно, вызывая
    anonymize_workbook (Stage 8.3) один раз на каждый item, с одними и
    теми же mapping_store/identifier_store/provenance_password для всех.

    :param items: batch items; материализуется ровно один раз, в самом
        начале, до любой preflight-проверки и до первого
        anonymize_workbook. Порядок материализации — input order,
        сохраняется в результате без сортировки.
    :param mapping_store: уже открытый, долгоживущий MappingStore —
        не создаётся, не закрывается, не очищается этой функцией; один
        и тот же объект передаётся каждому anonymize_workbook.
    :param identifier_store: уже открытый, долгоживущий
        IdentifierMappingStore, либо None, если ни один item не требует
        identifier tokenization; тот же объект передаётся каждому
        anonymize_workbook.
    :param provenance_password: один пароль для ВСЕХ provenance sidecar
        этого batch; opaque non-empty строка (exact crypto-контракт, без
        нормализации — та же семантика, что Stage 8.3).

    :returns: AnonymizationBatchResult только после успешного завершения
        ВСЕХ items, в input order.

    :raises TypeError: любой item не AnonymizationBatchItem, либо любой
        из его путей не str/Path, либо provenance_password не str.
    :raises BatchValidationError: любое нарушение batch-level preflight-
        контракта этого модуля (см. docstring класса).
    :raises Exception: любое исключение, поднятое anonymize_workbook при
        обработке конкретного item (включая его собственные leaf-
        исключения Reader/anonymizer/stores/Writer), распространяется
        как есть, без оборачивания — обработка последующих items при
        этом не выполняется (fail-fast).
    """
    materialized_items = tuple(items)

    _batch_preflight(materialized_items, provenance_password)

    results: list[AnonymizationJobResult] = []
    for item in materialized_items:
        result = anonymize_workbook(
            item.source_path,
            item.destination_path,
            item.rules_by_sheet,
            mapping_store,
            identifier_store,
            provenance_path=item.provenance_path,
            provenance_password=provenance_password,
        )
        results.append(result)

    return AnonymizationBatchResult(results=tuple(results))


# ----------------------------------------------------------------------
# Batch preflight
# ----------------------------------------------------------------------


def _batch_preflight(items: tuple[object, ...], provenance_password: object) -> None:
    if not items:
        raise BatchValidationError("batch не должен быть пустым")

    _validate_provenance_password(provenance_password)

    resolved_paths = [_validate_item(index, item) for index, item in enumerate(items)]

    _validate_no_cross_item_collisions(resolved_paths)


def _validate_item(index: int, item: object) -> tuple[Path, Path, Path]:
    if not isinstance(item, AnonymizationBatchItem):
        raise TypeError(
            f"items[{index}] должен быть AnonymizationBatchItem, получено: {type(item)!r}"
        )

    source = _validate_path_type(item.source_path, index, "source_path")
    destination = _validate_path_type(item.destination_path, index, "destination_path")
    provenance = _validate_path_type(item.provenance_path, index, "provenance_path")

    if source.suffix.lower() != _SUPPORTED_EXTENSION:
        raise BatchValidationError(
            f"items[{index}].source_path должен иметь расширение "
            f"{_SUPPORTED_EXTENSION}, получено: {source.suffix!r}"
        )
    if destination.suffix.lower() != _SUPPORTED_EXTENSION:
        raise BatchValidationError(
            f"items[{index}].destination_path должен иметь расширение "
            f"{_SUPPORTED_EXTENSION}, получено: {destination.suffix!r}"
        )

    if not source.exists():
        raise BatchValidationError(f"items[{index}].source_path не существует: {source}")
    if not source.is_file():
        raise BatchValidationError(f"items[{index}].source_path должен быть файлом: {source}")

    if not destination.parent.exists():
        raise BatchValidationError(
            f"items[{index}]: родительский каталог destination_path не "
            f"существует: {destination.parent}"
        )
    if not provenance.parent.exists():
        raise BatchValidationError(
            f"items[{index}]: родительский каталог provenance_path не "
            f"существует: {provenance.parent}"
        )

    if _paths_equal(source, destination):
        raise BatchValidationError(
            f"items[{index}]: source_path и destination_path не должны совпадать"
        )
    if _paths_equal(source, provenance):
        raise BatchValidationError(
            f"items[{index}]: source_path и provenance_path не должны совпадать"
        )
    if _paths_equal(destination, provenance):
        raise BatchValidationError(
            f"items[{index}]: destination_path и provenance_path не должны совпадать"
        )

    if provenance.exists():
        raise BatchValidationError(f"items[{index}].provenance_path уже существует: {provenance}")

    return source, destination, provenance


def _validate_path_type(value: object, index: int, role: str) -> Path:
    if not isinstance(value, (str, Path)):
        raise TypeError(
            f"items[{index}].{role} должен быть str или Path, получено: {type(value)!r}"
        )
    return Path(value)


def _paths_equal(a: Path, b: Path) -> bool:
    """
    Та же формула, что app.orchestration.single_file._paths_refer_to_same_file
    и app.excel.writer._paths_refer_to_same_file (frozen Stage 8.2/8.3
    contract) — реализована заново с точно той же формулой, а не
    импортирована (обе — приватные helpers своих модулей).
    """
    return os.path.normcase(str(a.resolve())) == os.path.normcase(str(b.resolve()))


def _validate_no_cross_item_collisions(resolved_paths: list[tuple[Path, Path, Path]]) -> None:
    """
    Полная межфайловая проверка: ни один resolved путь одного item не
    должен совпадать ни с одним resolved путём другого item, независимо
    от роли. Один плоский словарь по всем (item, role) парам естественно
    покрывает все шесть направлений коллизии (source-source, destination-
    destination, provenance-provenance, destination-source, provenance-
    source, destination-provenance) без отдельного кода на каждую пару.
    Совпадения путей ВНУТРИ одного item сюда не попадают — они уже
    отклонены раньше, в _validate_item, до вызова этой функции.
    """
    seen: dict[str, tuple[int, str]] = {}
    for index, paths in enumerate(resolved_paths):
        for role, path in zip(_PATH_ROLES, paths):
            key = os.path.normcase(str(path.resolve()))
            if key in seen:
                other_index, other_role = seen[key]
                raise BatchValidationError(
                    f"Коллизия путей между items: items[{other_index}].{other_role} "
                    f"и items[{index}].{role} указывают на один и тот же файл: {path}"
                )
            seen[key] = (index, role)


def _validate_provenance_password(password: object) -> None:
    # Точное зеркалирование Stage 8.3 (app.orchestration.single_file.
    # _validate_provenance_password / app.security.crypto._validate_password):
    # exact str, "" отклоняется БЕЗ .strip() — whitespace-only пароль
    # принимается.
    if not isinstance(password, str):
        raise TypeError(f"provenance_password должен быть str, получено: {type(password)!r}")
    if password == "":
        raise BatchValidationError("provenance_password не может быть пустым")
