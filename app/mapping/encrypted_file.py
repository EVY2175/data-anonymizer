"""
Файловое хранилище MappingStore, которое сохраняет таблицу соответствия на
диск исключительно в зашифрованном виде.

======================================================================
Критическое требование безопасности
======================================================================

На диске никогда не должно оказаться plaintext mapping — ни как основной
файл, ни как временный файл, ни как backup. JSON-сериализация таблицы
соответствия существует ТОЛЬКО в оперативной памяти процесса. Полный
пайплайн:

    MappingEntry objects
        -> сериализация в RAM (JSON, UTF-8, только в памяти)
        -> plaintext bytes в RAM
        -> encrypt_bytes(...)  (app.security.crypto, см. ниже)
        -> encrypted bytes
        -> temporary файл РЯДОМ с целевым файлом (та же ФС/каталог)
        -> atomic os.replace на целевой файл

На диск попадают только encrypted bytes — ни разу за весь пайплайн
сериализованный JSON или отдельные значения mapping не пишутся в файл в
открытом виде, даже временно.

Криптография не дублируется: этот модуль использует исключительно
encrypt_bytes/decrypt_bytes из app.security.crypto (Этап 3) и ничего не
знает о деталях контейнера (magic, версия, KDF и т.д.).

======================================================================
Композиция с InMemoryMappingStore
======================================================================

EncryptedFileMappingStore не переизобретает индексацию/бизнес-логику
mapping (alias-конфликты, identity-конфликты, неоднозначный поиск и
т.д.) — вся она уже реализована и протестирована в InMemoryMappingStore
(Этап 2). Этот класс делегирует всё чтение напрямую внутреннему
InMemoryMappingStore, а мутации (add/clear) проводит по схеме
"candidate store -> persist -> commit", описанной ниже.

======================================================================
Transaction-like consistency RAM <-> disk
======================================================================

add() и clear() никогда не переводят RAM-состояние в новое, пока новое
зашифрованное содержимое не записано на диск атомарно и полностью
успешно:

    1. создать candidate InMemoryMappingStore (копия текущего состояния);
    2. применить мутацию (add/clear) к candidate;
    3. сериализовать candidate целиком в RAM;
    4. зашифровать candidate целиком в RAM;
    5. атомарно записать encrypted bytes на диск (см. _atomic_write);
    6. только после успешного шага 5 сделать candidate текущим store.

Если на любом из шагов 3-5 возникает исключение — текущий self._store
(RAM) и текущий файл на диске остаются в точности такими же, как были до
вызова. Само исключение не перехватывается и не маскируется — оно
проходит наружу как есть (в т.ч. любые ошибки app.security.crypto).

Единственное отступление от этой схемы — идемпотентный повтор ТОЧНО той
же записи в add(): по контракту MappingStore это не должно быть ошибкой,
но и не обязано порождать перезапись файла (см. docstring add()).

======================================================================
Формат mapping payload (после расшифровки, версия 1)
======================================================================

    {
        "schema_version": 1,
        "entries": [ {alias, real_value, entity_type, parent_alias}, ... ]
    }

Версия схемы payload (schema_version) независима от версии crypto
контейнера (FORMAT_VERSION в app.security.crypto) — это две разные оси
эволюции формата. Для каждой MappingEntry используются существующие
to_dict()/from_dict() (Этап 1) без изменений их контракта.

======================================================================
Разделение ошибок: crypto-layer vs mapping-layer
======================================================================

- Неверный пароль либо повреждённый/подделанный контейнер
  -> app.security.crypto.DecryptionError (не перехватывается и не
     оборачивается — проходит как есть).
- Структурно некорректный/неизвестной версии crypto-контейнер
  -> соответствующие исключения app.security.crypto (InvalidContainerError,
     UnsupportedContainerVersionError, UnsupportedKDFError,
     InvalidKDFParametersError) — также проходят как есть.
- Валидный crypto-контейнер, но НЕвалидный после расшифровки mapping
  payload (не JSON, не тот schema_version, битая MappingEntry и т.д.)
  -> исключения ЭТОГО модуля: InvalidMappingPayloadError /
     UnsupportedMappingSchemaVersionError.
- Расшифрованный payload структурно валиден, но содержит противоречивые
  записи (конфликт alias/identity) -> MappingConflictError из
  app.mapping.base — та же самая ошибка, что и при обычном add() в
  InMemoryMappingStore, а не отдельный новый класс: это ровно то же
  нарушение целостности данных.

Ни один из этих случаев не подменяется и не маскируется другим типом
ошибки.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Optional, Union

from app.mapping.base import (
    _PARENT_UNSET,
    MappingStore,
    _ParentAliasArg,
)
from app.mapping.memory import InMemoryMappingStore
from app.models.entities import EntityType, MappingEntry
from app.security.crypto import decrypt_bytes, encrypt_bytes

# ----------------------------------------------------------------------
# Исключения mapping-payload-слоя (Этап 4)
#
# Отдельные от app.security.crypto (crypto-layer) и от
# app.mapping.base.MappingConflictError/AmbiguousMappingError
# (business-logic-layer, уже существуют и переиспользуются как есть).
# ----------------------------------------------------------------------


class MappingFileError(Exception):
    """
    Базовое исключение файлового/payload-слоя EncryptedFileMappingStore.

    Поднимается только для УЖЕ расшифрованных, УЖЕ аутентифицированных
    (GCM) байт — то есть после того, как app.security.crypto подтвердил
    подлинность контейнера. Ошибки самого crypto-контейнера (неверный
    пароль, повреждение, неизвестная версия и т.д.) остаются исключениями
    app.security.crypto и никогда не оборачиваются в MappingFileError.
    """


class InvalidMappingPayloadError(MappingFileError):
    """
    Расшифрованный plaintext payload не соответствует ожидаемой схеме
    mapping payload: невалидный UTF-8, невалидный JSON, верхний уровень не
    объект, отсутствуют обязательные поля, entries не список, отдельная
    запись не объект, повреждённая MappingEntry (в т.ч. некорректный
    EntityType) и т.п.
    """


class UnsupportedMappingSchemaVersionError(InvalidMappingPayloadError):
    """
    schema_version, прочитанный из payload, структурно присутствует, но не
    поддерживается этой версией DataAnonymizer.

    Выделено в отдельный класс (аналогично
    UnsupportedContainerVersionError на crypto-слое) — это концептуально
    другая ситуация, чем "payload вообще не распознаётся как mapping
    payload".
    """


# ----------------------------------------------------------------------
# Схема mapping payload
# ----------------------------------------------------------------------

MAPPING_PAYLOAD_SCHEMA_VERSION = 1


class EncryptedFileMappingStore(MappingStore):
    """
    MappingStore, хранящий все записи в оперативной памяти
    (InMemoryMappingStore, через композицию) и персистирующий их на диск
    исключительно в зашифрованном виде.

    Если целевой файл (path) не существует на момент создания объекта —
    создаётся пустой store в памяти, сам файл на диске пока не создаётся
    (появится при первой реальной мутации, изменяющей состояние). Если
    родительский каталог целевого файла не существует — конструктор
    поднимает FileNotFoundError (произвольное дерево каталогов
    автоматически не создаётся).

    Если целевой файл существует — он читается, расшифровывается
    (app.security.crypto.decrypt_bytes) и разбирается как mapping payload
    (см. docstring модуля) уже в конструкторе.
    """

    def __init__(self, path: Union[str, Path], password: str) -> None:
        if not isinstance(path, (str, Path)):
            raise TypeError(f"path должен быть str или Path, получено: {type(path)!r}")
        if not isinstance(password, str):
            raise TypeError(f"password должен быть str, получено: {type(password)!r}")

        self._path = Path(path)
        self._password = password

        parent = self._path.parent
        if not parent.exists():
            raise FileNotFoundError(
                f"Родительский каталог целевого файла не существует: {parent}"
            )

        if self._path.exists():
            self._store = self._load_from_disk()
        else:
            self._store = InMemoryMappingStore()

    # ------------------------------------------------------------------
    # Публичное свойство (безопасно раскрывать — путь не секретен)
    # ------------------------------------------------------------------

    @property
    def path(self) -> Path:
        return self._path

    # ------------------------------------------------------------------
    # Запись (add/clear) — transaction-like consistency RAM <-> disk
    # ------------------------------------------------------------------

    def add(self, entry: MappingEntry) -> None:
        if not isinstance(entry, MappingEntry):
            raise TypeError(f"add() принимает MappingEntry, получено: {type(entry)!r}")

        existing = self._store.get_by_alias(entry.alias)
        if existing == entry:
            # Идемпотентный повтор ТОЧНО той же записи: состояние не
            # меняется, поэтому ни candidate, ни persist не нужны (см.
            # docstring модуля/ТЗ п.7).
            return

        candidate = self._clone_store()
        # Может поднять MappingConflictError/TypeError — в этом случае
        # self._store и файл на диске остаются нетронутыми.
        candidate.add(entry)

        self._persist_and_commit(candidate)

    def clear(self) -> None:
        if not self._store.all_aliases():
            # Уже пусто — переписывать файл необязательно (ТЗ п.7).
            return

        empty_candidate = InMemoryMappingStore()
        self._persist_and_commit(empty_candidate)

    def _persist_and_commit(self, candidate: InMemoryMappingStore) -> None:
        """
        Сериализует+шифрует+атомарно записывает candidate на диск и ТОЛЬКО
        после успешного завершения делает candidate текущим self._store.

        Если что-либо на пути к успешной записи поднимает исключение, оно
        распространяется наружу как есть, а self._store остаётся прежним.
        """
        self._persist(candidate)
        self._store = candidate

    def _clone_store(self) -> InMemoryMappingStore:
        """Независимая копия текущего состояния — основа для candidate."""
        clone = InMemoryMappingStore()
        for entry in self._store.entries():
            clone.add(entry)
        return clone

    # ------------------------------------------------------------------
    # Чтение — простое делегирование InMemoryMappingStore
    # ------------------------------------------------------------------

    def get_by_alias(self, alias: str) -> Optional[MappingEntry]:
        return self._store.get_by_alias(alias)

    def get_by_real_value(
        self,
        real_value: str,
        entity_type: Optional[EntityType] = None,
        parent_alias: _ParentAliasArg = _PARENT_UNSET,
    ) -> Optional[MappingEntry]:
        return self._store.get_by_real_value(real_value, entity_type, parent_alias)

    def contains_alias(self, alias: str) -> bool:
        return self._store.contains_alias(alias)

    def all_aliases(self) -> set[str]:
        return self._store.all_aliases()

    def entries(self) -> tuple[MappingEntry, ...]:
        return self._store.entries()

    # ------------------------------------------------------------------
    # Загрузка с диска
    # ------------------------------------------------------------------

    def _load_from_disk(self) -> InMemoryMappingStore:
        encrypted = self._path.read_bytes()
        # Намеренно НЕ оборачивается в try/except: DecryptionError и любые
        # другие исключения app.security.crypto должны проходить наружу
        # как есть (см. docstring модуля).
        plaintext = decrypt_bytes(encrypted, self._password)
        return self._parse_payload(plaintext)

    @staticmethod
    def _parse_payload(plaintext: bytes) -> InMemoryMappingStore:
        try:
            text = plaintext.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise InvalidMappingPayloadError(
                "Расшифрованный mapping payload не является валидным UTF-8"
            ) from exc

        try:
            data = json.loads(text)
        except json.JSONDecodeError as exc:
            raise InvalidMappingPayloadError(
                f"Расшифрованный mapping payload не является валидным JSON: {exc}"
            ) from exc

        if not isinstance(data, dict):
            raise InvalidMappingPayloadError(
                "Верхний уровень mapping payload должен быть JSON-объектом, "
                f"получено: {type(data).__name__}"
            )

        if "schema_version" not in data:
            raise InvalidMappingPayloadError(
                "В mapping payload отсутствует обязательное поле schema_version"
            )

        schema_version = data["schema_version"]
        if schema_version != MAPPING_PAYLOAD_SCHEMA_VERSION:
            raise UnsupportedMappingSchemaVersionError(
                f"Неподдерживаемая версия схемы mapping payload: {schema_version!r} "
                f"(поддерживается: {MAPPING_PAYLOAD_SCHEMA_VERSION})"
            )

        if "entries" not in data:
            raise InvalidMappingPayloadError(
                "В mapping payload отсутствует обязательное поле entries"
            )

        entries_raw = data["entries"]
        if not isinstance(entries_raw, list):
            raise InvalidMappingPayloadError(
                f"Поле entries должно быть списком, получено: {type(entries_raw).__name__}"
            )

        store = InMemoryMappingStore()
        for index, entry_raw in enumerate(entries_raw):
            if not isinstance(entry_raw, dict):
                raise InvalidMappingPayloadError(
                    f"Запись mapping под индексом {index} должна быть JSON-объектом, "
                    f"получено: {type(entry_raw).__name__}"
                )
            try:
                entry = MappingEntry.from_dict(entry_raw)
            except ValueError as exc:
                raise InvalidMappingPayloadError(
                    f"Запись mapping под индексом {index} повреждена: {exc}"
                ) from exc

            # Конфликт alias/identity между записями payload — это то же
            # самое нарушение целостности данных, что и при обычном add() в
            # InMemoryMappingStore, поэтому переиспользуется тот же
            # MappingConflictError, а не отдельный новый класс.
            store.add(entry)

        return store

    # ------------------------------------------------------------------
    # Сериализация + шифрование + атомарная запись
    # ------------------------------------------------------------------

    @staticmethod
    def _serialize(store: InMemoryMappingStore) -> bytes:
        """Сериализация payload в RAM. JSON никогда не попадает на диск."""
        payload = {
            "schema_version": MAPPING_PAYLOAD_SCHEMA_VERSION,
            "entries": [entry.to_dict() for entry in store.entries()],
        }
        # Детерминированная сериализация: стабильные separators и
        # sort_keys, чтобы одно и то же состояние store всегда давало один
        # и тот же plaintext (до шифрования, которое всё равно рандомизирует
        # результат солью/nonce, но сам JSON остаётся воспроизводимым).
        text = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return text.encode("utf-8")

    def _persist(self, candidate: InMemoryMappingStore) -> None:
        plaintext = self._serialize(candidate)
        # encrypt_bytes может поднять TypeError/ValueError (некорректный
        # password/plaintext на уровне контракта crypto) — они проходят
        # наружу как есть, до какой-либо записи на диск.
        encrypted = encrypt_bytes(plaintext, self._password)
        self._atomic_write(encrypted)

    def _atomic_write(self, encrypted: bytes) -> None:
        """
        Атомарная запись encrypted bytes на диск:

            temp-файл в ТОМ ЖЕ каталоге -> write -> flush -> fsync ->
            close -> os.replace(temp, target).

        На диск попадают только encrypted bytes — ни разу не пишется
        plaintext/JSON. При любой ошибке ДО успешного os.replace: старый
        target не повреждается (мы никогда не пишем в него напрямую), а
        temp-файл удаляется best-effort, не маскируя исходное исключение.
        """
        directory = self._path.parent
        fd, tmp_name = tempfile.mkstemp(
            dir=str(directory), prefix=f".{self._path.name}.", suffix=".tmp"
        )
        tmp_path = Path(tmp_name)
        try:
            with os.fdopen(fd, "wb") as tmp_file:
                tmp_file.write(encrypted)
                tmp_file.flush()
                os.fsync(tmp_file.fileno())
            os.replace(str(tmp_path), str(self._path))
        except Exception:
            try:
                tmp_path.unlink(missing_ok=True)
            except OSError:
                # Best-effort cleanup: ошибка удаления temp-файла не должна
                # маскировать исходную ошибку записи/замены.
                pass
            raise
