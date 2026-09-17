"""
Файловое хранилище IdentifierMappingStore, сохраняющее identifier mapping
(ИНН/КПП/ОГРН) на диск исключительно в зашифрованном виде — Stage 7B.4.1.

======================================================================
Отдельный от Stage 4 файл и формат
======================================================================

Это НЕ расширение EncryptedFileMappingStore (Stage 4) — отдельный класс,
использующий composition над InMemoryIdentifierMappingStore (Stage 7B.3),
с собственным, независимым payload-форматом и собственной версией схемы
(IDENTIFIER_PAYLOAD_SCHEMA_VERSION), никак не связанной с
MAPPING_PAYLOAD_SCHEMA_VERSION Stage 4. Entity-mapping payload/файл/схема
Stage 4 этим модулем не читаются, не пишутся и не меняются. Два payload
никогда не объединяются в один файл.

======================================================================
Критическое требование безопасности (то же, что и Stage 4)
======================================================================

На диске никогда не должно оказаться plaintext identifier mapping — ни
как основной файл, ни как временный файл. JSON-сериализация существует
ТОЛЬКО в оперативной памяти процесса:

    IdentifierMappingEntry objects
        -> сериализация в RAM (JSON, UTF-8, только в памяти)
        -> plaintext bytes в RAM
        -> encrypt_bytes(...)  (app.security.crypto, Этап 3, без изменений)
        -> encrypted bytes
        -> temporary файл РЯДОМ с целевым файлом (та же ФС/каталог)
        -> atomic os.replace на целевой файл

Криптография не дублируется и не меняется: используются исключительно
encrypt_bytes/decrypt_bytes из app.security.crypto как есть.

======================================================================
Композиция с InMemoryIdentifierMappingStore
======================================================================

Этот класс не переизобретает identity/conflict-логику (Stage 7B.3) — она
уже реализована и протестирована в InMemoryIdentifierMappingStore. Чтение
делегируется напрямую; мутации (add/clear) проводятся по той же схеме
"candidate store -> persist -> commit", что и в Stage 4:

    1. создать candidate InMemoryIdentifierMappingStore (копия текущего состояния);
    2. применить мутацию (add/clear) к candidate;
    3. сериализовать candidate целиком в RAM;
    4. зашифровать candidate целиком в RAM;
    5. атомарно записать encrypted bytes на диск;
    6. только после успешного шага 5 сделать candidate текущим store.

Если на любом из шагов 3-5 возникает исключение — текущий self._store
(RAM) и текущий файл на диске остаются в точности такими же, как были до
вызова. Исключение не перехватывается и не маскируется.

Единственное отступление — идемпотентный повтор ТОЧНО той же записи в
add(): по контракту IdentifierMappingStore это не ошибка, но и не
обязано порождать перезапись файла.

======================================================================
Формат identifier payload (после расшифровки, версия 1)
======================================================================

    {
        "schema_version": 1,
        "entries": [ {"token", "identifier_value", "identifier_type"}, ... ]
    }

IdentifierMappingEntry (Stage 7B.1) не имеет to_dict()/from_dict() — модель
намеренно не расширяется только ради симметрии со Stage 4; сериализация и
парсинг сделаны явно, прямо в этом модуле.

======================================================================
Разделение ошибок: crypto-layer vs identifier-payload-layer vs identity-layer
======================================================================

- Неверный пароль либо повреждённый/подделанный контейнер
  -> app.security.crypto.DecryptionError (проходит как есть).
- Структурно некорректный/неизвестной версии crypto-контейнер
  -> соответствующие исключения app.security.crypto (проходят как есть).
- Валидный crypto-контейнер, но НЕвалидный после расшифровки identifier
  payload (не JSON, не тот schema_version, битая запись и т.д.)
  -> исключения ЭТОГО модуля: InvalidIdentifierPayloadError /
     UnsupportedIdentifierPayloadSchemaVersionError.
- Расшифрованный payload структурно валиден, но содержит противоречивые
  записи (конфликт token/identity) -> IdentifierMappingConflictError из
  app.mapping.identifier_base — та же ошибка, что и при обычном add() в
  InMemoryIdentifierMappingStore, а не отдельный новый класс.

Ни один из этих случаев не подменяется и не маскируется другим типом
ошибки.

======================================================================
Конфиденциальность
======================================================================

Ни InvalidIdentifierPayloadError, ни
UnsupportedIdentifierPayloadSchemaVersionError никогда не включают в
текст сообщения сырое значение identifier_value, token, полный dict
записи или plaintext JSON целиком — только индекс записи, имя поля и
ожидаемый тип/категорию. Это строже, чем аналогичное сообщение Stage 4
для entity real_value (там entity-имя не считается настолько
конфиденциальным).

======================================================================
Stage 7B.4.1 vs Stage 7B.4.2
======================================================================

Этот модуль реализует ТОЛЬКО correctness/security (Stage 7B.4.1):
persistence, atomic write, strict payload validation, конфиденциальность
ошибок. add() перезаписывает весь payload на каждый реальный вызов —
ровно то же архитектурное поведение, что и у Stage 4 сегодня. Вопрос
производительности/batching — отдельный, ещё не решённый Stage 7B.4.2,
здесь не затрагивается и не оптимизируется.

Provenance (точное восстановление исходного Python-типа int/str для
convergent identity) этим модулем не решается и не хранится — отдельный,
ещё не спроектированный слой перед Stage 8 (Writer).
"""

from __future__ import annotations

import json
import os
import tempfile
from collections.abc import Iterable
from pathlib import Path
from typing import Optional, Union

from app.mapping.identifier_base import IdentifierMappingConflictError, IdentifierMappingStore
from app.mapping.identifier_memory import InMemoryIdentifierMappingStore
from app.models.identifiers import IdentifierMappingEntry, IdentifierType
from app.security.crypto import decrypt_bytes, encrypt_bytes

# ----------------------------------------------------------------------
# Исключения identifier-payload-слоя (Stage 7B.4.1)
#
# Отдельные от app.security.crypto (crypto-layer), от
# app.mapping.identifier_base.IdentifierMappingConflictError
# (identity-layer, переиспользуется как есть) и от app.mapping.encrypted_file
# (entity mapping payload — другой формат, не связан с этим).
# ----------------------------------------------------------------------


class IdentifierMappingFileError(Exception):
    """
    Базовое исключение файлового/payload-слоя
    EncryptedFileIdentifierMappingStore.

    Поднимается только для УЖЕ расшифрованных, УЖЕ аутентифицированных
    (GCM) байт. Ошибки самого crypto-контейнера остаются исключениями
    app.security.crypto и никогда не оборачиваются в это исключение.
    """


class InvalidIdentifierPayloadError(IdentifierMappingFileError):
    """
    Расшифрованный plaintext identifier payload не соответствует
    ожидаемой схеме: невалидный UTF-8, невалидный JSON, верхний уровень не
    объект, отсутствуют/лишние top-level поля, entries не список, запись
    не объект, отсутствуют/лишние поля записи, неверный тип/пустое
    значение token или identifier_value, некорректный identifier_type.

    Сообщение НИКОГДА не включает сырое значение identifier_value, token,
    полный dict записи или plaintext JSON целиком.
    """


class UnsupportedIdentifierPayloadSchemaVersionError(InvalidIdentifierPayloadError):
    """
    schema_version, прочитанный из identifier payload, структурно
    корректен (int, не bool), но не поддерживается этой версией
    DataAnonymizer.
    """


# ----------------------------------------------------------------------
# Схема identifier payload
# ----------------------------------------------------------------------

IDENTIFIER_PAYLOAD_SCHEMA_VERSION = 1

_TOP_LEVEL_KEYS = frozenset({"schema_version", "entries"})
_ENTRY_KEYS = frozenset({"token", "identifier_value", "identifier_type"})


class EncryptedFileIdentifierMappingStore(IdentifierMappingStore):
    """
    IdentifierMappingStore, хранящий все записи в оперативной памяти
    (InMemoryIdentifierMappingStore, через composition) и персистирующий
    их на диск исключительно в зашифрованном виде.

    Семантически зеркалит EncryptedFileMappingStore (Stage 4): если
    целевой файл не существует — создаётся пустой store в памяти, сам
    файл не создаётся (появится при первой реальной мутации). Если
    родительский каталог не существует — FileNotFoundError (дерево
    каталогов автоматически не создаётся). Если файл существует — он
    читается, расшифровывается и разбирается как identifier payload уже
    в конструкторе.
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
            self._store = InMemoryIdentifierMappingStore()

    # ------------------------------------------------------------------
    # Публичное свойство (путь не секретен)
    # ------------------------------------------------------------------

    @property
    def path(self) -> Path:
        return self._path

    # ------------------------------------------------------------------
    # Запись (add/clear) — transaction-like consistency RAM <-> disk
    # ------------------------------------------------------------------

    def add(self, entry: IdentifierMappingEntry) -> None:
        if not isinstance(entry, IdentifierMappingEntry):
            raise TypeError(
                f"add() принимает IdentifierMappingEntry, получено: {type(entry)!r}"
            )

        existing = self._store.get_by_token(entry.token)
        if existing == entry:
            # Идемпотентный повтор ТОЧНО той же записи — ни candidate, ни
            # persist не нужны (см. docstring модуля).
            return

        candidate = self._clone_store()
        # Может поднять IdentifierMappingConflictError/TypeError — в этом
        # случае self._store и файл на диске остаются нетронутыми.
        candidate.add(entry)

        self._persist_and_commit(candidate)

    def add_many(self, entries: Iterable[IdentifierMappingEntry]) -> None:
        batch = tuple(entries)
        if not batch:
            return

        candidate = self._clone_store()
        before = len(candidate.entries())

        for entry in batch:
            # Может поднять IdentifierMappingConflictError/TypeError — в
            # этом случае ни self._store, ни файл на диске не тронуты:
            # candidate целиком в памяти и отбрасывается.
            candidate.add(entry)

        after = len(candidate.entries())
        if after == before:
            # Весь batch — уже существующие точные дубликаты: ни
            # candidate, ни файл переписывать не нужно.
            return

        self._persist_and_commit(candidate)

    def clear(self) -> None:
        if not self._store.all_tokens():
            # Уже пусто — переписывать файл необязательно.
            return

        empty_candidate = InMemoryIdentifierMappingStore()
        self._persist_and_commit(empty_candidate)

    def _persist_and_commit(self, candidate: InMemoryIdentifierMappingStore) -> None:
        """
        Сериализует+шифрует+атомарно записывает candidate на диск и ТОЛЬКО
        после успешного завершения делает candidate текущим self._store.
        """
        self._persist(candidate)
        self._store = candidate

    def _clone_store(self) -> InMemoryIdentifierMappingStore:
        """Независимая копия текущего состояния через public add() — без
        обращения к приватным dict-индексам."""
        clone = InMemoryIdentifierMappingStore()
        for entry in self._store.entries():
            clone.add(entry)
        return clone

    # ------------------------------------------------------------------
    # Чтение — простое делегирование InMemoryIdentifierMappingStore
    # ------------------------------------------------------------------

    def get_by_token(self, token: str) -> Optional[IdentifierMappingEntry]:
        return self._store.get_by_token(token)

    def get_by_identity(
        self, identifier_type: IdentifierType, identifier_value: str
    ) -> Optional[IdentifierMappingEntry]:
        return self._store.get_by_identity(identifier_type, identifier_value)

    def all_tokens(self) -> set[str]:
        return self._store.all_tokens()

    def entries(self) -> tuple[IdentifierMappingEntry, ...]:
        return self._store.entries()

    # ------------------------------------------------------------------
    # Загрузка с диска
    # ------------------------------------------------------------------

    def _load_from_disk(self) -> InMemoryIdentifierMappingStore:
        encrypted = self._path.read_bytes()
        # Намеренно НЕ оборачивается в try/except: DecryptionError и любые
        # другие исключения app.security.crypto должны проходить наружу
        # как есть.
        plaintext = decrypt_bytes(encrypted, self._password)
        return self._parse_payload(plaintext)

    @staticmethod
    def _parse_payload(plaintext: bytes) -> InMemoryIdentifierMappingStore:
        try:
            text = plaintext.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise InvalidIdentifierPayloadError(
                "Расшифрованный identifier mapping payload не является валидным UTF-8"
            ) from exc

        try:
            data = json.loads(text)
        except json.JSONDecodeError as exc:
            raise InvalidIdentifierPayloadError(
                f"Расшифрованный identifier mapping payload не является валидным JSON: {exc}"
            ) from exc

        if not isinstance(data, dict):
            raise InvalidIdentifierPayloadError(
                "Верхний уровень identifier mapping payload должен быть JSON-объектом, "
                f"получено: {type(data).__name__}"
            )

        top_level_keys = set(data.keys())
        missing_top = _TOP_LEVEL_KEYS - top_level_keys
        if missing_top:
            raise InvalidIdentifierPayloadError(
                "В identifier mapping payload отсутствуют обязательные поля верхнего "
                f"уровня: {sorted(missing_top)}"
            )
        unknown_top = top_level_keys - _TOP_LEVEL_KEYS
        if unknown_top:
            raise InvalidIdentifierPayloadError(
                f"Identifier mapping payload содержит неизвестные поля верхнего уровня: "
                f"{sorted(unknown_top)}"
            )

        schema_version = data["schema_version"]
        if isinstance(schema_version, bool) or not isinstance(schema_version, int):
            raise InvalidIdentifierPayloadError(
                "Поле schema_version должно быть int, получено: "
                f"{type(schema_version).__name__}"
            )
        if schema_version != IDENTIFIER_PAYLOAD_SCHEMA_VERSION:
            raise UnsupportedIdentifierPayloadSchemaVersionError(
                f"Неподдерживаемая версия схемы identifier mapping payload: "
                f"{schema_version!r} (поддерживается: {IDENTIFIER_PAYLOAD_SCHEMA_VERSION})"
            )

        entries_raw = data["entries"]
        if not isinstance(entries_raw, list):
            raise InvalidIdentifierPayloadError(
                f"Поле entries должно быть списком, получено: {type(entries_raw).__name__}"
            )

        store = InMemoryIdentifierMappingStore()
        for index, entry_raw in enumerate(entries_raw):
            entry = EncryptedFileIdentifierMappingStore._parse_entry(entry_raw, index)
            # Конфликт token/identity между записями payload — то же самое
            # нарушение целостности, что и при обычном add() в
            # InMemoryIdentifierMappingStore, поэтому переиспользуется тот
            # же IdentifierMappingConflictError, а не отдельный класс.
            store.add(entry)

        return store

    @staticmethod
    def _parse_entry(entry_raw: object, index: int) -> IdentifierMappingEntry:
        if not isinstance(entry_raw, dict):
            raise InvalidIdentifierPayloadError(
                f"Запись identifier mapping под индексом {index} должна быть "
                f"JSON-объектом, получено: {type(entry_raw).__name__}"
            )

        entry_keys = set(entry_raw.keys())
        missing = _ENTRY_KEYS - entry_keys
        if missing:
            raise InvalidIdentifierPayloadError(
                f"Запись identifier mapping под индексом {index} не содержит "
                f"обязательные поля: {sorted(missing)}"
            )
        unknown = entry_keys - _ENTRY_KEYS
        if unknown:
            raise InvalidIdentifierPayloadError(
                f"Запись identifier mapping под индексом {index} содержит "
                f"неизвестные поля: {sorted(unknown)}"
            )

        token = entry_raw["token"]
        if not isinstance(token, str) or not token.strip():
            raise InvalidIdentifierPayloadError(
                f"Запись identifier mapping под индексом {index}: поле 'token' "
                "должно быть непустой строкой (не whitespace-only)"
            )

        identifier_value = entry_raw["identifier_value"]
        if not isinstance(identifier_value, str) or not identifier_value.strip():
            raise InvalidIdentifierPayloadError(
                f"Запись identifier mapping под индексом {index}: поле "
                "'identifier_value' должно быть непустой строкой (не whitespace-only)"
            )

        identifier_type_raw = entry_raw["identifier_type"]
        if not isinstance(identifier_type_raw, str):
            raise InvalidIdentifierPayloadError(
                f"Запись identifier mapping под индексом {index}: поле "
                f"'identifier_type' должно быть строкой, получено: "
                f"{type(identifier_type_raw).__name__}"
            )
        try:
            identifier_type = IdentifierType(identifier_type_raw)
        except ValueError as exc:
            allowed = sorted(member.value for member in IdentifierType)
            # Сознательно НЕ включаем сюда identifier_type_raw: сама по себе
            # эта строка не конфиденциальна (это категория, не реальный
            # ИНН/КПП/ОГРН), но payload-layer exceptions придерживаются
            # более строгой политики "никогда не эхировать пользовательское
            # raw-значение", а не оценивают конфиденциальность каждого поля
            # по отдельности.
            raise InvalidIdentifierPayloadError(
                f"Запись identifier mapping под индексом {index}: недопустимое "
                f"значение identifier_type (допустимо: {allowed})"
            ) from exc

        # identifier_value передаётся EXACT, без strip/normalize — token.strip()/
        # identifier_value.strip() выше использовались только для проверки
        # "не whitespace-only", не для изменения сохраняемого значения.
        #
        # Финальное создание модели обёрнуто в узкий try/except: к этому
        # моменту token/identifier_value/identifier_type уже независимо
        # проверены выше и IdentifierMappingEntry.__post_init__ не должен
        # падать — но если валидация модели когда-либо изменится, внешний
        # контракт этого метода (InvalidIdentifierPayloadError, а не сырой
        # ValueError/TypeError модельного слоя) не должен молча нарушиться.
        # str(exc) исходного исключения модели намеренно НЕ попадает в
        # сообщение — после будущих изменений model validation он потенциально
        # мог бы содержать конфиденциальное значение.
        try:
            return IdentifierMappingEntry(
                token=token, identifier_value=identifier_value, identifier_type=identifier_type
            )
        except (TypeError, ValueError) as exc:
            raise InvalidIdentifierPayloadError(
                f"Запись identifier mapping под индексом {index} структурно повреждена"
            ) from exc

    # ------------------------------------------------------------------
    # Сериализация + шифрование + атомарная запись
    # ------------------------------------------------------------------

    @staticmethod
    def _serialize(store: InMemoryIdentifierMappingStore) -> bytes:
        """Сериализация payload в RAM. JSON никогда не попадает на диск."""
        payload = {
            "schema_version": IDENTIFIER_PAYLOAD_SCHEMA_VERSION,
            "entries": [
                {
                    "token": entry.token,
                    "identifier_value": entry.identifier_value,
                    "identifier_type": entry.identifier_type.value,
                }
                for entry in store.entries()
            ],
        }
        text = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return text.encode("utf-8")

    def _persist(self, candidate: InMemoryIdentifierMappingStore) -> None:
        plaintext = self._serialize(candidate)
        # encrypt_bytes может поднять TypeError/ValueError (некорректный
        # password/plaintext на уровне контракта crypto) — они проходят
        # наружу как есть, до какой-либо записи на диск.
        encrypted = encrypt_bytes(plaintext, self._password)
        self._atomic_write(encrypted)

    def _atomic_write(self, encrypted: bytes) -> None:
        """
        Атомарная запись encrypted bytes на диск (идентично Stage 4):

            temp-файл в ТОМ ЖЕ каталоге -> write -> flush -> fsync ->
            close -> os.replace(temp, target).

        На диск попадают только encrypted bytes. При любой ошибке ДО
        успешного os.replace: старый target не повреждается, temp-файл
        удаляется best-effort, не маскируя исходное исключение.
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
                pass
            raise
