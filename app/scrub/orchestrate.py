"""
Stage 10C.2.5 — Safe Scrub Orchestration.

ОРКЕСТРАЦИЯ, а не реализация: этот модуль НЕ содержит собственной логики
resource/package preflight (Stage10C.2.1), package policy/inventory
(Stage10C.2.2), object-model scrub (Stage10C.2.3), post-save package
validation (Stage10C.2.4) или restored-marker/exact-known-value inspection
(Stage10A, app.safety.external_ai). Он вызывает эти пять уже закрытых
слоёв в строго зафиксированном порядке и владеет ЕДИНСТВЕННЫМ временным
файлом на всём протяжении его жизни.

======================================================================
Единственная публичная точка входа
======================================================================

scrub_workbook_for_external_candidate(anonymized_path, mapping_store,
identifier_store) -> Iterator[ScrubResult] (contextlib.contextmanager)

======================================================================
Разграничение (frozen, Stage10C.2.5 Architecture/Contract Freeze Pass)
======================================================================

Этот модуль НЕ является external-AI authorization gate. Он производит
временный, post-validated scrubbed-кандидат и передаёт управление
вызывающему коду РОВНО на время действия context manager-а. Проверка
registered artifact, workspace binding, SHA-привязки к постоянному
snapshot, safety/store state, Formula Safety Gate и прочих Final Security
Closure требований — ответственность БУДУЩЕГО, отдельного слоя, не этого
модуля.

======================================================================
Frozen pipeline (Stage10C.2.5 Contract Freeze Pass, §3/§9)
======================================================================

STEP 1  preflight_xlsx_package(source)                — Stage10C.2.1
STEP 2  inspect_package_policy(source)                 — Stage10C.2.2
STEP 3  inspect_workbook_for_external_ai(source, ...)  — Stage10A
STEP 4  интерпретация SafetyReport (см. ниже)
STEP 5  scrub_workbook_object_model(source) -> temp    — Stage10C.2.3
STEP 6  ownership guard: доказать temp != source       — MAJOR-1 correction
STEP 7  validate_scrubbed_workbook_package(temp)       — Stage10C.2.4
STEP 8  SHA-256(temp)
STEP 9  yield ScrubResult(...)
STEP 10 cleanup temp

Порядок НЕ переставляется. STEP 1 обязателен ДО STEP 3, даже несмотря
на то, что STEP 2 (inspect_package_policy) уже программно вызывает
preflight_xlsx_package внутри себя — это намеренный defense-in-depth/
order invariant (Contract Freeze Pass §10): app.safety.external_ai сама
НЕ применяет ни один resource-лимит Stage10C.2.1 (её собственный
_preflight проверяет только расширение/существование/is_file) — без
явного STEP 1 её internal openpyxl.load_workbook() получил бы
непроверенный на resource-бомбы ZIP.

======================================================================
SafetyReport interpretation (Contract Freeze Pass, OD-10C2.5-A/-B)
======================================================================

Из 22 FindingKind Stage10A ровно три являются BLOCKING
(RESTORED_MARKER, KNOWN_ENTITY_VALUE, KNOWN_IDENTIFIER_VALUE) — это
решает САМА app.safety.external_ai (SEVERITY_BY_KIND), этот модуль
никогда не переопределяет и не понижает severity. Приоритет
детерминирован (Contract Freeze Pass §12): RESTORED_MARKER >
KNOWN_ENTITY_VALUE > KNOWN_IDENTIFIER_VALUE > defensive INTERNAL_FAILURE
(для гипотетического будущего blocking kind, который сегодня не может
возникнуть, но которому нельзя дать тихо пройти, если он появится).
Restored-marker транслируется в уже существующий, зарезервированный с
самого Stage10C.2 Final Contract Freeze reason
PackageScrubReason.RESTORED_MARKER_PRESENT — PackageScrubReason НЕ
расширяется. Для двух оставшихся blocking kind введена НОВАЯ, отдельная,
минимальная модель ошибок этого модуля (ScrubOrchestrationError) — она
не заменяет и не изменяет app.scrub.errors.

======================================================================
Temp ownership (Contract Freeze Pass, §7/§14/§17)
======================================================================

Единственный temporary-файл во всей цепочке — тот, что возвращает
scrub_workbook_object_model (Stage10C.2.3 сама создаёт его через
tempfile.mkstemp, вне workspace, случайное имя, и сама очищает его при
СВОЕЙ внутренней ошибке). Этот модуль НЕ создаёт второй temp. Владение
переходит сюда НЕ в момент получения Path, а ТОЛЬКО после успешного
прохождения ownership guard (см. ниже) — до этого момента возвращённый
Path НЕ считается принадлежащим orchestration, и ни один cleanup helper
не вызывается для него.

======================================================================
Ownership guard (MAJOR-1 correction, Independent Security Review §21)
======================================================================

Независимый Security Review показал: если бы 2.3 (нарушив свой уже
закрытый контракт) вернул source path вместо настоящего temp,
последующий cleanup при сбое STEP 7/STEP 8/исключении caller-а удалил
бы source — необратимая потеря исходного workbook. Реальный,
неизменный контракт scrub_workbook_object_model этого не делает (всегда
возвращает свежий tempfile.mkstemp() путь), поэтому это НЕ достижимо
через нормальную работу системы — но catastrophic consequence при
почти нулевой стоимости defense-in-depth требует явной проверки.

STEP 6 доказывает через os.path.samefile(temp_path, source_path) —
единственный стандартный Python primitive для вопроса "это один и тот
же файл на диске?" на уровне OS device/inode identity (на Windows —
file index, получаемый через тот же stat-механизм), который БЕСПЛАТНО
разрешает relative/absolute представление, "."/".." литералы и
symlink/junction alias (stat разыменовывает их сам) — без построения
отдельной filesystem-security архитектуры. Любая невозможность доказать
различие (OSError сравнения, например путь исчез между шагами)
трактуется как "различие НЕ доказано" — fail-closed:
ScrubOrchestrationError(INTERNAL_FAILURE), поднимается ВНЕ try/except
(без implicit chaining), без unlink чего-либо (ни temp, ни тем более
source) и без утечки текста нижележащей ошибки сравнения.

Полноценная защита от hostile TOCTOU-race (symlink/junction, подменённый
ПОСЛЕ прохождения guard, но ДО следующего использования пути) — вне
scope этого correction pass; остаётся задокументированным debt, как и
прочие TOCTOU-ограничения этого модуля.

======================================================================
Cleanup state machine (Contract Freeze Pass §15/§22, MAJOR-1 correction)
======================================================================

A.  Сбой STEP 1-4              — temp не существует, ошибка как есть.
B.  Сбой STEP 5 (сам scrub)    — temp не существует (Stage10C.2.3 сама
                                  очищает при СВОЕЙ внутренней ошибке).
B2. Сбой STEP 6 (ownership     — temp/returned path НЕ считается owned;
    guard, MAJOR-1)              unlink НЕ вызывается ни для него, ни
                                  тем более для source;
                                  ScrubOrchestrationError(INTERNAL_FAILURE)
                                  поднимается вне try/except.
C.  Сбой STEP 7 (postvalidate) — best-effort unlink, ОРИГИНАЛЬНОЕ
                                  исключение пробрасывается без wrap/chain.
D.  Сбой STEP 8 (SHA)          — best-effort unlink, НОВОЕ
                                  ScrubOrchestrationError(INTERNAL_FAILURE)
                                  поднимается ВНЕ except-блока
                                  (flag-then-raise-outside-except, OD-7).
E.  Исключение caller-а ПОСЛЕ yield (включая BaseException:
    KeyboardInterrupt/SystemExit/GeneratorExit) — best-effort unlink,
    исключение caller-а пробрасывается БЕЗ ИЗМЕНЕНИЙ.
F.  Штатный выход caller-а     — strict unlink; при сбое —
                                  ScrubOrchestrationError(CLEANUP_FAILED).

Во всех случаях (C/D/E), где temp удаляется НА ФОНЕ уже случившегося
(или пробрасываемого) исключения, cleanup-сбой ПОДАВЛЯЕТСЯ полностью
(best-effort) — он никогда не может заменить собой первичное исключение.
CLEANUP_FAILED поднимается ИСКЛЮЧИТЕЛЬНО в сценарии F, где никакого
первичного исключения нет и маскировать нечего. B2 НЕ вызывает вообще
никакой unlink — путь ещё не owned.

======================================================================
SHA-256 / exact-byte semantics (Contract Freeze Pass, §16/§21/OD-10C2.5-C)
======================================================================

sha256 в ScrubResult — SHA-256 exact bytes temp-файла, вычисленный
СТРОГО после успешного STEP 6 (validate PASS) и СТРОГО до yield. Это
снимок на момент вычисления — модуль НЕ делает temp неизменяемым и НЕ
пересчитывает hash после yield. Любая мутация temp caller-ом после
получения ScrubResult аннулирует соответствие sha256 фактическим байтам;
это ожидаемое, задокументированное поведение, не дефект.

======================================================================
OD-7
======================================================================

ScrubOrchestrationError никогда не включает: source/temp путь, worksheet
name, row/column, raw value, identifier, formula-текст, XML, текст
исключения нижнего уровня. Ни один internal try/except не создаёт implicit
chaining (__context__/__cause__ всегда None для новых
ScrubOrchestrationError) — новое исключение всегда поднимается ВНЕ
except-блока, перехватившего внутреннюю причину (тот же паттерн, что во
всех предыдущих Stage10C.2 модулях). Исключения, естественно
пробрасываемые ИЗ 2.1/2.2/2.3/2.4/Stage10A (PackageScrubError,
ExternalAiSafetyError и её подклассы, TypeError), не оборачиваются и не
меняются — их собственный privacy-контракт уже установлен в их модулях.

======================================================================
No network
======================================================================

Ни одна операция этого модуля не обращается к сети. Единственный I/O —
локальные файловые операции, уже выполняемые нижележащими слоями, плюс
локальное потоковое чтение temp-файла для SHA-256.
"""

from __future__ import annotations

import contextlib
import dataclasses
import enum
import hashlib
import os
from pathlib import Path
from typing import Iterator, Optional, Union

from app.coverage.errors import Stage10CError
from app.mapping.base import MappingStore
from app.mapping.identifier_base import IdentifierMappingStore
from app.safety.external_ai import FindingKind, SafetyReport, inspect_workbook_for_external_ai
from app.scrub.errors import PackageScrubError, PackageScrubReason
from app.scrub.inventory import inspect_package_policy
from app.scrub.mutate import scrub_workbook_object_model
from app.scrub.postvalidate import PackageValidationResult, validate_scrubbed_workbook_package
from app.scrub.preflight import preflight_xlsx_package

_PathLike = Union[str, Path]

_SHA256_CHUNK_SIZE = 1024 * 1024  # 1 MiB — потоковое чтение, не единым read()


# ----------------------------------------------------------------------
# Reason-enum / исключение (Contract Freeze Pass §5/§6 — ровно 4 значения,
# НЕ расширять)
# ----------------------------------------------------------------------


class ScrubOrchestrationReason(enum.Enum):
    KNOWN_ENTITY_VALUE_PRESENT = "known_entity_value_present"
    KNOWN_IDENTIFIER_VALUE_PRESENT = "known_identifier_value_present"
    INTERNAL_FAILURE = "internal_failure"
    CLEANUP_FAILED = "cleanup_failed"


_ORCHESTRATION_MESSAGES = {
    ScrubOrchestrationReason.KNOWN_ENTITY_VALUE_PRESENT: (
        "Обнаружено известное конфиденциальное значение"
    ),
    ScrubOrchestrationReason.KNOWN_IDENTIFIER_VALUE_PRESENT: (
        "Обнаружено известное значение идентификатора"
    ),
    ScrubOrchestrationReason.INTERNAL_FAILURE: (
        "Внутренняя ошибка safe scrub orchestration"
    ),
    ScrubOrchestrationReason.CLEANUP_FAILED: (
        "Не удалось безопасно удалить временный файл после успешной проверки"
    ),
}


def _require_orchestration_reason(reason: object) -> None:
    if not isinstance(reason, ScrubOrchestrationReason):
        raise TypeError(f"reason должен быть ScrubOrchestrationReason, получено: {type(reason)!r}")


class ScrubOrchestrationError(Stage10CError):
    """
    Ошибка Stage10C.2.5 orchestration-слоя. НЕ используется для сигналов,
    у которых уже есть точный существующий reason в PackageScrubReason
    (restored-marker -> PackageScrubError(RESTORED_MARKER_PRESENT)) —
    только для двух Stage10A blocking-находок без собственного native
    reason (KNOWN_ENTITY_VALUE/KNOWN_IDENTIFIER_VALUE), внутреннего сбоя
    orchestration (INTERNAL_FAILURE) и cleanup-сбоя на success-path
    (CLEANUP_FAILED).
    """

    def __init__(self, reason: ScrubOrchestrationReason) -> None:
        _require_orchestration_reason(reason)
        super().__init__(_ORCHESTRATION_MESSAGES[reason])
        self.reason = reason


# ----------------------------------------------------------------------
# ScrubResult (Contract Freeze Pass §7/OD-10C2.5-C — ровно 3 поля)
# ----------------------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class ScrubResult:
    """
    Результат успешного прохождения полного pipeline, действительный
    ТОЛЬКО на момент yield (см. модульный докстринг, "SHA-256 / exact-byte
    semantics"). path перестаёт существовать сразу после выхода из
    context manager-а — caller обязан завершить всю работу с ним ДО
    выхода из `with`-блока.
    """

    path: Path
    validation: PackageValidationResult
    sha256: str


# ----------------------------------------------------------------------
# SafetyReport interpretation (Contract Freeze Pass §11/§12)
# ----------------------------------------------------------------------


def _interpret_safety_report(report: SafetyReport) -> None:
    """
    Читает ТОЛЬКО report.count(kind) и report.has_blocking_findings —
    принимает любой объект с этим же интерфейсом (duck typing), что
    намеренно позволяет unit-тестировать эту функцию минимальным
    controlled fake, не трогая production SafetyReport/FindingKind
    Stage10A (см. Contract Freeze Pass §38).

    Приоритет детерминирован и НЕ является выбором severity (severity уже
    решена Stage10A) — это только порядок, в котором опрашиваются уже
    blocking kind, чтобы exception был воспроизводим при одновременном
    срабатывании нескольких находок:
        RESTORED_MARKER > KNOWN_ENTITY_VALUE > KNOWN_IDENTIFIER_VALUE >
        defensive INTERNAL_FAILURE (has_blocking_findings=True, но ни
        один из трёх известных kind не сработал — сегодня недостижимо
        через реальный SafetyReport, но fail-closed обязателен).
    """
    if report.count(FindingKind.RESTORED_MARKER) > 0:
        raise PackageScrubError(PackageScrubReason.RESTORED_MARKER_PRESENT)
    if report.count(FindingKind.KNOWN_ENTITY_VALUE) > 0:
        raise ScrubOrchestrationError(ScrubOrchestrationReason.KNOWN_ENTITY_VALUE_PRESENT)
    if report.count(FindingKind.KNOWN_IDENTIFIER_VALUE) > 0:
        raise ScrubOrchestrationError(ScrubOrchestrationReason.KNOWN_IDENTIFIER_VALUE_PRESENT)
    if report.has_blocking_findings:
        raise ScrubOrchestrationError(ScrubOrchestrationReason.INTERNAL_FAILURE)


# ----------------------------------------------------------------------
# Ownership guard (MAJOR-1 correction — Independent Security Review §21,
# см. модульный докстринг "Ownership guard"). До успешного прохождения
# этой проверки returned path НЕ считается принадлежащим orchestration.
# ----------------------------------------------------------------------


def _temp_ownership_confirmed(temp_path: Path, source_path: Path) -> bool:
    """
    True, только если строго доказано (os.path.samefile — OS device/inode
    identity), что temp_path и source_path — разные файлы. Любая
    невозможность доказать различие (OSError сравнения) трактуется как НЕ
    доказано — fail-closed. Никогда не поднимает исключение и никогда не
    включает путь/текст нижележащей ошибки в свой результат.
    """
    try:
        return not os.path.samefile(temp_path, source_path)
    except OSError:
        return False


# ----------------------------------------------------------------------
# SHA-256 (Contract Freeze Pass §19/§20 — локальный, потоковый, не
# переиспользует app.safety.external_ai.sha256_file по явному указанию
# Implementation Pass §19: "реализовать локальный streaming SHA-256
# helper")
# ----------------------------------------------------------------------


def _compute_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as file:
        while True:
            chunk = file.read(_SHA256_CHUNK_SIZE)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


# ----------------------------------------------------------------------
# Cleanup helpers (Contract Freeze Pass §24 — два явных семантических
# режима, оба локальные — private _safe_unlink из app.scrub.mutate
# сознательно НЕ импортируется, чтобы не создавать coupling между
# закрытыми implementation details двух разных подстадий)
# ----------------------------------------------------------------------


def _best_effort_unlink(path: Path) -> None:
    """Никогда не поднимает — используется, когда уже есть (или пробрасывается) первичное исключение."""
    try:
        path.unlink(missing_ok=True)
    except Exception:
        pass


def _unlink_or_raise_cleanup_error(path: Path) -> None:
    """
    Используется ТОЛЬКО на success-path (сценарий F) — сбой здесь не
    может ничего замаскировать (первичного исключения нет), поэтому
    поднимается новое ScrubOrchestrationError(CLEANUP_FAILED) вместо
    молчаливого orphan-temp. Flag-then-raise-outside-except: raise
    происходит ПОСЛЕ выхода из try/except, __context__/__cause__ не
    затрагиваются.
    """
    unlink_failed = False
    try:
        path.unlink(missing_ok=True)
    except Exception:
        unlink_failed = True
    if unlink_failed:
        raise ScrubOrchestrationError(ScrubOrchestrationReason.CLEANUP_FAILED)


# ----------------------------------------------------------------------
# Public API
# ----------------------------------------------------------------------


@contextlib.contextmanager
def scrub_workbook_for_external_candidate(
    anonymized_path: _PathLike,
    mapping_store: MappingStore,
    identifier_store: Optional[IdentifierMappingStore],
) -> Iterator[ScrubResult]:
    """
    Safe Scrub Orchestration (Stage 10C.2.5) — единственная точка входа.
    См. модульный докстринг: frozen pipeline (STEP 1-10), ownership guard
    (MAJOR-1 correction), SafetyReport interpretation, temp ownership,
    cleanup state machine, SHA-256/exact-byte semantics. Это НЕ
    external-AI authorization gate (см. модульный докстринг,
    "Разграничение").

    :raises TypeError: anonymized_path/mapping_store/identifier_store
        неверного типа (поднимается нижележащими слоями — preflight_xlsx_package
        и inspect_workbook_for_external_ai).
    :raises PackageScrubError: см. PackageScrubReason — нативные ошибки
        Stage10C.2.1/.2.2/.2.3/.2.4 пробрасываются как есть;
        RESTORED_MARKER_PRESENT — restored-marker (Stage9B/9C) обнаружен
        Stage10A ДО mutation.
    :raises ScrubOrchestrationError: KNOWN_ENTITY_VALUE_PRESENT/
        KNOWN_IDENTIFIER_VALUE_PRESENT — известное confidential-значение/
        идентификатор обнаружен Stage10A ДО mutation; INTERNAL_FAILURE —
        внутренний сбой SHA-256, невозможность подтвердить ownership guard-ом
        (MAJOR-1 correction), что temp отличается от source, либо defensive
        fail-closed при нераспознанной blocking-находке; CLEANUP_FAILED —
        temp не удалось удалить после полностью успешного pipeline и
        штатного выхода caller-а из context manager-а.
    :raises Exception: любое исключение, естественно поднятое
        app.safety.external_ai.inspect_workbook_for_external_ai
        (ExternalAiSafetyError и подклассы) пробрасывается без изменений.
    """
    preflight_xlsx_package(anonymized_path)
    inspect_package_policy(anonymized_path)
    report = inspect_workbook_for_external_ai(anonymized_path, mapping_store, identifier_store)
    _interpret_safety_report(report)

    temp_path = scrub_workbook_object_model(anonymized_path)

    if not _temp_ownership_confirmed(temp_path, Path(anonymized_path)):
        raise ScrubOrchestrationError(ScrubOrchestrationReason.INTERNAL_FAILURE)

    try:
        validation = validate_scrubbed_workbook_package(temp_path)
    except Exception:
        _best_effort_unlink(temp_path)
        raise

    sha_failed = False
    sha256_value = ""
    try:
        sha256_value = _compute_sha256(temp_path)
    except Exception:
        sha_failed = True
    if sha_failed:
        _best_effort_unlink(temp_path)
        raise ScrubOrchestrationError(ScrubOrchestrationReason.INTERNAL_FAILURE)

    try:
        yield ScrubResult(path=temp_path, validation=validation, sha256=sha256_value)
    except BaseException:
        _best_effort_unlink(temp_path)
        raise
    else:
        _unlink_or_raise_cleanup_error(temp_path)
