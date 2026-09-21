"""
Analytical Restore Writer (Stage 9C) — metadata + atomic publication.

Отдельный от app/restore/writer.py (Stage 9B, closed) модуль: тот же
проверенный на этом Windows-окружении atomic-write паттерн (temp-файл в
каталоге назначения -> save -> fsync через O_RDWR -> os.rename), но с
ДРУГОЙ metadata semantics: DataAnonymizer.AnalyticallyRestored вместо
DataAnonymizer.JobId/RestoredFromJobId. Существующие
DataAnonymizer.JobId/DataAnonymizer.RestoredFromJobId (если source их
уже содержал) сохраняются как unrelated metadata — этот writer никогда
не ссылается на их имена и, соответственно, никогда их не трогает.

======================================================================
os.rename, а не os.replace
======================================================================

Идентичное Stage 9B решение: os.rename на этом Windows-окружении
поднимает FileExistsError, если destination уже существует, вместо
молчаливой перезаписи (в отличие от os.replace) — устраняет TOCTOU-окно
между preflight-проверкой и финальной публикацией.

======================================================================
Safe cumulative workflow — назначение marker'а
======================================================================

DataAnonymizer.AnalyticallyRestored="true" — единственная metadata,
которую создаёт Stage 9C. Она сигнализирует: этот workbook прошёл
локальное analytical restore и может содержать восстановленные
конфиденциальные значения (реальные названия компаний, ИНН/КПП/ОГРН).
Такой файл НЕ должен использоваться как anonymized input следующего
внешнего AI-цикла анализа. Будущий Stage 10 должен опираться именно на
этот marker для workflow-guard; Stage 9C сам никакой сетевой логики не
реализует и ничего не блокирует.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import Union

from openpyxl.packaging.custom import StringProperty
from openpyxl.workbook.workbook import Workbook

_ANALYTICALLY_RESTORED_PROPERTY_NAME = "DataAnonymizer.AnalyticallyRestored"

_PathLike = Union[str, Path]


def write_analytical_restored_workbook(workbook: Workbook, destination_path: _PathLike) -> None:
    """
    Обновляет metadata уже полностью восстановленного (все replacement
    применены вызывающей стороной) workbook и атомарно публикует его в
    destination_path.

    Предполагается, что destination уже прошёл preflight (не существует,
    родительский каталог существует) — эта функция не повторяет те
    проверки; финальная гарантия "не перезаписать существующий файл"
    обеспечивается самим os.rename (см. docstring модуля).

    :raises FileExistsError: destination появился конкурентно между
        preflight и этим вызовом (leaf, unchanged).
    :raises Exception: любая ошибка openpyxl.save()/fsync (leaf, unchanged).
    """
    destination = Path(destination_path)
    _replace_analytically_restored_marker(workbook)
    _atomic_publish(workbook, destination)


def _replace_analytically_restored_marker(workbook: Workbook) -> None:
    """
    Удаляет ВСЕ существующие custom properties с именем
    DataAnonymizer.AnalyticallyRestored (не только первую —
    CustomPropertyList.__delitem__ удаляет ровно одно совпадение за
    вызов, поэтому цикл повторяет del до KeyError) и добавляет ровно
    одну свежую со значением "true". Прочие custom properties (включая
    возможные DataAnonymizer.JobId/DataAnonymizer.RestoredFromJobId от
    предыдущих, не связанных с Stage 9C workflow) не затрагиваются —
    сравнение строго по exact имени.
    """
    while True:
        try:
            del workbook.custom_doc_props[_ANALYTICALLY_RESTORED_PROPERTY_NAME]
        except KeyError:
            break

    workbook.custom_doc_props.append(
        StringProperty(name=_ANALYTICALLY_RESTORED_PROPERTY_NAME, value="true")
    )


def _atomic_publish(workbook: Workbook, destination: Path) -> None:
    directory = destination.parent
    fd, tmp_name = tempfile.mkstemp(dir=str(directory), prefix=f".{destination.name}.", suffix=".tmp")
    os.close(fd)
    tmp_path = Path(tmp_name)
    try:
        workbook.save(str(tmp_path))

        fsync_fd = os.open(str(tmp_path), os.O_RDWR)
        try:
            os.fsync(fsync_fd)
        finally:
            os.close(fsync_fd)

        os.rename(str(tmp_path), str(destination))
    except Exception:
        try:
            tmp_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise
