"""
Restore-specific atomic output writer.

Отдельный от app/excel/writer.py (Stage 8.2, closed) модуль: тот же
проверенный atomic-write паттерн (temp-файл в каталоге назначения ->
save -> fsync через O_RDWR -> финальная публикация), но с ДРУГОЙ metadata
semantics (DataAnonymizer.RestoredFromJobId вместо DataAnonymizer.JobId)
и ДРУГИМ финальным примитивом публикации.

======================================================================
os.rename, а не os.replace — единственное намеренное расхождение
======================================================================

Stage 8.2 Writer использует os.replace (безусловно перезаписывает
существующий destination) — это допустимо для anonymization-output.
Restore, по решению владельца продукта, обязан НИКОГДА не перезаписывать
уже существующий destination. Экспериментально проверено на этом
Windows-окружении: os.rename(src, dst), в отличие от os.replace, поднимает
FileExistsError, если dst уже существует, и работает как обычный atomic
move, если не существует. Это делает сам вызов os.rename атомарной
проверкой-и-действием одновременно — race-окно между "проверить, что
destination не существует" и "опубликовать файл" устраняется полностью,
а не только сужается: если между preflight-проверкой и этим вызовом
destination появился конкурентно, os.rename сам обнаружит это и
откажется перезаписывать (FileExistsError пробрасывается unchanged).
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

from openpyxl.packaging.custom import StringProperty
from openpyxl.workbook.workbook import Workbook

_JOB_ID_PROPERTY_NAME = "DataAnonymizer.JobId"
_RESTORED_FROM_JOB_ID_PROPERTY_NAME = "DataAnonymizer.RestoredFromJobId"


def write_restored_workbook(workbook: Workbook, destination: Path, *, source_job_id: str) -> None:
    """
    Обновляет metadata уже полностью восстановленного (все replacement
    применены вызывающей стороной) workbook и атомарно публикует его в
    destination.

    Предполагается, что destination уже прошёл preflight (не существует,
    родительский каталог существует) — эта функция не повторяет те
    проверки, финальная гарантия "не перезаписать существующий файл"
    обеспечивается самим os.rename (см. docstring модуля).

    :raises FileExistsError: destination появился конкурентно между
        preflight и этим вызовом (leaf, unchanged).
    :raises Exception: любая ошибка openpyxl.save()/fsync (leaf, unchanged).
    """
    _replace_restored_metadata(workbook, source_job_id)
    _atomic_publish(workbook, destination)


def _replace_restored_metadata(workbook: Workbook, source_job_id: str) -> None:
    """
    Удаляет ВСЕ существующие custom properties с именами
    DataAnonymizer.JobId и DataAnonymizer.RestoredFromJobId (не только
    первую — CustomPropertyList.__delitem__ удаляет ровно одно совпадение
    за вызов, поэтому цикл повторяет del до KeyError, тот же паттерн, что
    уже проверен в Stage 8.2 Writer) и добавляет ровно одну свежую
    DataAnonymizer.RestoredFromJobId = source_job_id. После успешного
    завершения этой функции DataAnonymizer.JobId отсутствует в workbook,
    что является frozen owner decision.
    """
    for name in (_JOB_ID_PROPERTY_NAME, _RESTORED_FROM_JOB_ID_PROPERTY_NAME):
        while True:
            try:
                del workbook.custom_doc_props[name]
            except KeyError:
                break

    workbook.custom_doc_props.append(
        StringProperty(name=_RESTORED_FROM_JOB_ID_PROPERTY_NAME, value=source_job_id)
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
