"""
Файловое хранилище ProvenanceStore, сохраняющее per-cell identifier
provenance на диск исключительно в зашифрованном виде — Stage 7C.3.

======================================================================
Отдельный от Stage 4/7B.4.1 файл и формат
======================================================================

Это НЕ расширение EncryptedFileMappingStore/EncryptedFileIdentifierMappingStore
— отдельный класс, использующий composition над InMemoryProvenanceStore
(Stage 7C.2), с собственным, независимым payload-форматом и собственной
версией схемы (PROVENANCE_PAYLOAD_SCHEMA_VERSION), никак не связанной с
MAPPING_PAYLOAD_SCHEMA_VERSION/IDENTIFIER_PAYLOAD_SCHEMA_VERSION. Entity-
и identifier-mapping payload/файлы/схемы этим модулем не читаются, не
пишутся и не меняются. Ни один payload никогда не объединяется с другим.

======================================================================
Критическое требование безопасности (то же, что и у Stage 4/7B.4.1)
======================================================================

На диске никогда не должно оказаться plaintext provenance — ни как
основной файл, ни как временный файл. JSON-сериализация существует
ТОЛЬКО в оперативной памяти процесса:

    IdentifierCellProvenance objects + job_id
        -> сериализация в RAM (JSON, UTF-8, только в памяти)
        -> plaintext bytes в RAM
        -> encrypt_bytes(...)  (app.security.crypto, без изменений)
        -> encrypted bytes
        -> temporary файл РЯДОМ с целевым файлом (та же ФС/каталог)
        -> atomic os.replace на целевой файл

Криптография не дублируется и не меняется: используются исключительно
encrypt_bytes/decrypt_bytes из app.security.crypto как есть.

======================================================================
Композиция с InMemoryProvenanceStore
======================================================================

Этот класс не переизобретает identity/conflict-логику (Stage 7C.2) — она
уже реализована и протестирована в InMemoryProvenanceStore. Чтение
делегируется напрямую; add_many/clear проводятся по схеме
"candidate store -> persist -> commit":

    1. создать candidate InMemoryProvenanceStore (тот же job_id, копия
       текущего состояния через public entries()/add_many());
    2. применить batch к candidate (целиком, через её собственный
       транзакционный add_many — без внутреннего per-entry цикла на
       этом уровне);
    3. сериализовать candidate целиком в RAM;
    4. зашифровать candidate целиком в RAM;
    5. атомарно записать encrypted bytes на диск;
    6. только после успешного шага 5 сделать candidate текущим self._store.

Если на любом из шагов 2-5 возникает исключение — текущий self._store
(RAM) и текущий файл на диске остаются в точности такими же, как были до
вызова. Исключение не перехватывается и не маскируется.

======================================================================
КРИТИЧЕСКАЯ ПОПРАВКА: immediate empty sidecar при создании (Stage 7C.3
corrective design review)
======================================================================

В отличие от EncryptedFileMappingStore/EncryptedFileIdentifierMappingStore
(где отсутствующий файл означает "пустой store в памяти, файл появится
при первой мутации"), EncryptedFileProvenanceStore ЭТОГО модуля
НЕМЕДЛЕННО персистирует пустой encrypted payload при создании НОВОГО
sidecar (path отсутствует). Причина: job_id — сам по себе значимое
durable provenance-состояние ("этот sidecar принадлежит этому job"), а
не просто контейнер для будущих записей. "Нет provenance-записей" и
"provenance sidecar не существует вообще" должны быть различимы на
уровне файловой системы:

    - path отсутствует -> provenance sidecar для этого job не существует;
    - path существует, entries=[] -> валидный sidecar для job X с нулём
      identifier-cell provenance записей;
    - path существует, entries=[...] -> валидный sidecar для job X с
      provenance записями.

Успешное завершение конструктора для НОВОГО sidecar поэтому ВСЕГДА
означает: файл существует, зашифрован, содержит job_id и entries=[].
Никакого "успешно создан, но только в памяти" состояния не существует.

======================================================================
Create vs Open — job_id authority
======================================================================

path отсутствует (создание нового sidecar):
    job_id ОБЯЗАТЕЛЕН (ValueError, если None). Валидируется через
    InMemoryProvenanceStore(job_id=...) (Stage 7C.2 контракт). Немедленно
    персистируется пустой payload (см. выше) ДО успешного завершения
    конструктора.

path существует (открытие существующего sidecar):
    job_id ДОЛЖЕН быть опущен (ValueError, если передан) — эта проверка
    происходит ДО какой-либо расшифровки/разбора payload, чисто по
    Path.exists(). Authoritative job_id читается ИСКЛЮЧИТЕЛЬНО из
    расшифрованного payload — конструктор никогда не сверяет и не
    заменяет его переданным значением (которого на этом пути просто нет).

======================================================================
Формат provenance payload (после расшифровки, версия 1)
======================================================================

    {
        "schema_version": 1,
        "job_id": "...",
        "entries": [
            {"sheet_name", "row", "column", "token", "representation"}, ...
        ]
    }

IdentifierCellProvenance (Stage 7C.1) не имеет to_dict()/from_dict() —
модель намеренно не расширяется только ради симметрии с другими Stage;
сериализация и парсинг сделаны явно, прямо в этом модуле. job_id — это
store-level поле верхнего уровня, а НЕ поле отдельной записи (см.
app.models.provenance/app.mapping.provenance_base).

======================================================================
Разделение ошибок: crypto-layer vs provenance-payload-layer vs identity-layer
======================================================================

- Неверный пароль либо повреждённый/подделанный контейнер
  -> app.security.crypto.DecryptionError (проходит как есть).
- Структурно некорректный/неизвестной версии crypto-контейнер (в т.ч.
  0-байтный файл) -> соответствующие исключения app.security.crypto
  (проходят как есть).
- Валидный crypto-контейнер, но НЕвалидный после расшифровки provenance
  payload (не JSON, не тот schema_version, битая запись, malformed
  job_id и т.д.) -> исключения ЭТОГО модуля: InvalidProvenancePayloadError /
  UnsupportedProvenancePayloadSchemaVersionError.
- Расшифрованный payload структурно валиден, но содержит противоречивые
  записи (конфликт координаты/token/representation) ->
  ProvenanceConflictError из app.mapping.provenance_base — та же ошибка,
  что и при обычном add_many() в InMemoryProvenanceStore, а не отдельный
  новый класс.

Ни один из этих случаев не подменяется и не маскируется другим типом
ошибки.

======================================================================
Конфиденциальность
======================================================================

Ни InvalidProvenancePayloadError, ни
UnsupportedProvenancePayloadSchemaVersionError никогда не включают в
текст сообщения job_id, sheet_name, token, полный dict записи или
plaintext JSON целиком — только индекс записи, имя поля и ожидаемый
тип/категорию.
"""

from __future__ import annotations

import json
import os
import tempfile
from collections.abc import Iterable
from pathlib import Path
from typing import Optional, Union

from app.mapping.provenance_base import ProvenanceConflictError, ProvenanceStore
from app.mapping.provenance_memory import InMemoryProvenanceStore
from app.models.provenance import IdentifierCellProvenance, IdentifierRepresentation
from app.security.crypto import decrypt_bytes, encrypt_bytes

# ----------------------------------------------------------------------
# Исключения provenance-payload-слоя (Stage 7C.3)
#
# Отдельные от app.security.crypto (crypto-layer), от
# app.mapping.provenance_base.ProvenanceConflictError (identity-layer,
# переиспользуется как есть) и от entity/identifier payload-слоёв
# (другой формат, не связаны с этим).
# ----------------------------------------------------------------------


class ProvenanceFileError(Exception):
    """
    Базовое исключение файлового/payload-слоя
    EncryptedFileProvenanceStore.

    Поднимается только для УЖЕ расшифрованных, УЖЕ аутентифицированных
    (GCM) байт. Ошибки самого crypto-контейнера остаются исключениями
    app.security.crypto и никогда не оборачиваются в это исключение.
    """


class InvalidProvenancePayloadError(ProvenanceFileError):
    """
    Расшифрованный plaintext provenance payload не соответствует
    ожидаемой схеме: невалидный UTF-8, невалидный JSON, верхний уровень не
    объект, отсутствуют/лишние top-level поля, entries не список, запись
    не объект, отсутствуют/лишние поля записи, неверный тип/пустое
    значение job_id/sheet_name/token, некорректные row/column,
    некорректный representation.

    Сообщение НИКОГДА не включает job_id, sheet_name, token, полный dict
    записи или plaintext JSON целиком.
    """


class UnsupportedProvenancePayloadSchemaVersionError(InvalidProvenancePayloadError):
    """
    schema_version, прочитанный из provenance payload, структурно
    корректен (int, не bool), но не поддерживается этой версией
    DataAnonymizer.
    """


# ----------------------------------------------------------------------
# Схема provenance payload
# ----------------------------------------------------------------------

PROVENANCE_PAYLOAD_SCHEMA_VERSION = 1

_TOP_LEVEL_KEYS = frozenset({"schema_version", "job_id", "entries"})
_ENTRY_KEYS = frozenset({"sheet_name", "row", "column", "token", "representation"})


class EncryptedFileProvenanceStore(ProvenanceStore):
    """
    ProvenanceStore, хранящий все записи в оперативной памяти
    (InMemoryProvenanceStore, через composition) и персистирующий их на
    диск исключительно в зашифрованном виде.

    В отличие от EncryptedFileMappingStore/EncryptedFileIdentifierMappingStore,
    создание НОВОГО sidecar (path отсутствует) НЕМЕДЛЕННО персистирует
    пустой encrypted payload — успешное завершение конструктора всегда
    означает durable sidecar на диске (см. docstring модуля, раздел про
    corrective design review). Открытие СУЩЕСТВУЮЩЕГО sidecar (path
    существует) запрещает передавать job_id — authoritative job_id
    читается исключительно из payload.
    """

    def __init__(
        self, path: Union[str, Path], password: str, *, job_id: Optional[str] = None
    ) -> None:
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
            if job_id is not None:
                raise ValueError(
                    "job_id не должен передаваться при открытии уже существующего "
                    "provenance sidecar — authoritative job_id читается из payload"
                )
            self._store = self._load_from_disk()
        else:
            if job_id is None:
                raise ValueError(
                    "job_id обязателен при создании нового provenance sidecar"
                )
            # InMemoryProvenanceStore.__init__ валидирует job_id (Stage 7C.2
            # контракт: exact non-empty str, без нормализации).
            candidate = InMemoryProvenanceStore(job_id=job_id)
            # КРИТИЧНО (corrective design review): пустой sidecar
            # персистируется НЕМЕДЛЕННО — успешное завершение __init__
            # обязано означать durable файл на диске. self._store
            # становится authoritative только после успешной persist.
            self._persist_and_commit(candidate)

    # ------------------------------------------------------------------
    # Публичные свойства (путь не секретен)
    # ------------------------------------------------------------------

    @property
    def path(self) -> Path:
        return self._path

    @property
    def job_id(self) -> str:
        return self._store.job_id

    # ------------------------------------------------------------------
    # Запись (add_many/clear) — transaction-like consistency RAM <-> disk
    # ------------------------------------------------------------------

    def add_many(self, entries: Iterable[IdentifierCellProvenance]) -> None:
        batch = tuple(entries)
        if not batch:
            return

        candidate = self._clone_store()
        before = len(candidate.entries())

        # Может поднять ProvenanceConflictError/TypeError — в этом случае
        # ни self._store, ни файл на диске не тронуты: candidate целиком
        # в памяти и отбрасывается. Весь batch делегируется ОДНИМ вызовом
        # уже транзакционному InMemoryProvenanceStore.add_many (Stage
        # 7C.2) — без дополнительного per-entry цикла на этом уровне.
        candidate.add_many(batch)

        after = len(candidate.entries())
        if after == before:
            # Весь batch — уже существующие точные дубликаты: ни
            # candidate, ни файл переписывать не нужно.
            return

        self._persist_and_commit(candidate)

    def clear(self) -> None:
        if not self._store.entries():
            # Уже пусто (в т.ч. сразу после создания — см. docstring
            # модуля) — переписывать файл необязательно.
            return

        empty_candidate = InMemoryProvenanceStore(job_id=self._store.job_id)
        self._persist_and_commit(empty_candidate)

    def _persist_and_commit(self, candidate: InMemoryProvenanceStore) -> None:
        """
        Сериализует+шифрует+атомарно записывает candidate на диск и ТОЛЬКО
        после успешного завершения делает candidate текущим self._store.

        Единственная точка вызова persist — используется и при создании
        НОВОГО sidecar (из __init__), и при add_many/clear. Никакого
        отдельного, более слабого write-пути для создания нет.
        """
        self._persist(candidate)
        self._store = candidate

    def _clone_store(self) -> InMemoryProvenanceStore:
        """Независимая копия текущего состояния (тот же job_id) через
        public entries()/add_many() — без обращения к приватным индексам."""
        clone = InMemoryProvenanceStore(job_id=self._store.job_id)
        clone.add_many(self._store.entries())
        return clone

    # ------------------------------------------------------------------
    # Чтение — простое делегирование InMemoryProvenanceStore
    # ------------------------------------------------------------------

    def get_by_coordinate(
        self, sheet_name: str, row: int, column: int
    ) -> Optional[IdentifierCellProvenance]:
        return self._store.get_by_coordinate(sheet_name, row, column)

    def entries(self) -> tuple[IdentifierCellProvenance, ...]:
        return self._store.entries()

    # ------------------------------------------------------------------
    # Загрузка с диска
    # ------------------------------------------------------------------

    def _load_from_disk(self) -> InMemoryProvenanceStore:
        encrypted = self._path.read_bytes()
        # Намеренно НЕ оборачивается в try/except: DecryptionError и любые
        # другие исключения app.security.crypto (в т.ч. для 0-байтного
        # файла) должны проходить наружу как есть.
        plaintext = decrypt_bytes(encrypted, self._password)
        return self._parse_payload(plaintext)

    @staticmethod
    def _parse_payload(plaintext: bytes) -> InMemoryProvenanceStore:
        try:
            text = plaintext.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise InvalidProvenancePayloadError(
                "Расшифрованный provenance payload не является валидным UTF-8"
            ) from exc

        try:
            data = json.loads(text)
        except json.JSONDecodeError as exc:
            raise InvalidProvenancePayloadError(
                f"Расшифрованный provenance payload не является валидным JSON: {exc}"
            ) from exc

        if not isinstance(data, dict):
            raise InvalidProvenancePayloadError(
                "Верхний уровень provenance payload должен быть JSON-объектом, "
                f"получено: {type(data).__name__}"
            )

        top_level_keys = set(data.keys())
        missing_top = _TOP_LEVEL_KEYS - top_level_keys
        if missing_top:
            raise InvalidProvenancePayloadError(
                "В provenance payload отсутствуют обязательные поля верхнего "
                f"уровня: {sorted(missing_top)}"
            )
        unknown_top = top_level_keys - _TOP_LEVEL_KEYS
        if unknown_top:
            raise InvalidProvenancePayloadError(
                f"Provenance payload содержит неизвестные поля верхнего уровня: "
                f"{sorted(unknown_top)}"
            )

        schema_version = data["schema_version"]
        if isinstance(schema_version, bool) or not isinstance(schema_version, int):
            raise InvalidProvenancePayloadError(
                "Поле schema_version должно быть int, получено: "
                f"{type(schema_version).__name__}"
            )
        if schema_version != PROVENANCE_PAYLOAD_SCHEMA_VERSION:
            raise UnsupportedProvenancePayloadSchemaVersionError(
                f"Неподдерживаемая версия схемы provenance payload: "
                f"{schema_version!r} (поддерживается: {PROVENANCE_PAYLOAD_SCHEMA_VERSION})"
            )

        job_id_raw = data["job_id"]
        if not isinstance(job_id_raw, str) or not job_id_raw.strip():
            raise InvalidProvenancePayloadError(
                "Поле job_id должно быть непустой строкой (не whitespace-only)"
            )

        entries_raw = data["entries"]
        if not isinstance(entries_raw, list):
            raise InvalidProvenancePayloadError(
                f"Поле entries должно быть списком, получено: {type(entries_raw).__name__}"
            )

        parsed_entries = tuple(
            EncryptedFileProvenanceStore._parse_entry(entry_raw, index)
            for index, entry_raw in enumerate(entries_raw)
        )

        # job_id_raw передаётся EXACT, без strip/normalize — используется
        # только как predicate выше. Конфликт координат/token/representation
        # между записями payload — то же самое нарушение целостности, что и
        # при обычном add_many() в InMemoryProvenanceStore, поэтому
        # переиспользуется тот же ProvenanceConflictError, не оборачивается.
        store = InMemoryProvenanceStore(job_id=job_id_raw)
        store.add_many(parsed_entries)

        return store

    @staticmethod
    def _parse_entry(entry_raw: object, index: int) -> IdentifierCellProvenance:
        if not isinstance(entry_raw, dict):
            raise InvalidProvenancePayloadError(
                f"Запись provenance под индексом {index} должна быть "
                f"JSON-объектом, получено: {type(entry_raw).__name__}"
            )

        entry_keys = set(entry_raw.keys())
        missing = _ENTRY_KEYS - entry_keys
        if missing:
            raise InvalidProvenancePayloadError(
                f"Запись provenance под индексом {index} не содержит "
                f"обязательные поля: {sorted(missing)}"
            )
        unknown = entry_keys - _ENTRY_KEYS
        if unknown:
            raise InvalidProvenancePayloadError(
                f"Запись provenance под индексом {index} содержит "
                f"неизвестные поля: {sorted(unknown)}"
            )

        sheet_name = entry_raw["sheet_name"]
        if not isinstance(sheet_name, str) or not sheet_name.strip():
            raise InvalidProvenancePayloadError(
                f"Запись provenance под индексом {index}: поле 'sheet_name' "
                "должно быть непустой строкой (не whitespace-only)"
            )

        row = entry_raw["row"]
        if isinstance(row, bool) or not isinstance(row, int):
            raise InvalidProvenancePayloadError(
                f"Запись provenance под индексом {index}: поле 'row' должно "
                f"быть int, получено: {type(row).__name__}"
            )
        if row < 1:
            raise InvalidProvenancePayloadError(
                f"Запись provenance под индексом {index}: поле 'row' должно "
                "быть >= 1"
            )

        column = entry_raw["column"]
        if isinstance(column, bool) or not isinstance(column, int):
            raise InvalidProvenancePayloadError(
                f"Запись provenance под индексом {index}: поле 'column' "
                f"должно быть int, получено: {type(column).__name__}"
            )
        if column < 1:
            raise InvalidProvenancePayloadError(
                f"Запись provenance под индексом {index}: поле 'column' "
                "должно быть >= 1"
            )

        token = entry_raw["token"]
        if not isinstance(token, str) or not token.strip():
            raise InvalidProvenancePayloadError(
                f"Запись provenance под индексом {index}: поле 'token' "
                "должно быть непустой строкой (не whitespace-only)"
            )

        representation_raw = entry_raw["representation"]
        if not isinstance(representation_raw, str):
            raise InvalidProvenancePayloadError(
                f"Запись provenance под индексом {index}: поле "
                f"'representation' должно быть строкой, получено: "
                f"{type(representation_raw).__name__}"
            )
        try:
            representation = IdentifierRepresentation(representation_raw)
        except ValueError as exc:
            allowed = sorted(member.value for member in IdentifierRepresentation)
            # Сознательно НЕ включаем сюда representation_raw — та же
            # бланкетная политика "никогда не эхировать пользовательское
            # raw-значение", что и в identifier payload-слое.
            raise InvalidProvenancePayloadError(
                f"Запись provenance под индексом {index}: недопустимое "
                f"значение representation (допустимо: {allowed})"
            ) from exc

        # sheet_name/token передаются EXACT, без strip/normalize — их
        # .strip() выше использовался только для проверки "не
        # whitespace-only", не для изменения сохраняемого значения.
        #
        # Финальное создание модели обёрнуто в узкий try/except: к этому
        # моменту все поля уже независимо проверены выше и
        # IdentifierCellProvenance.__post_init__ не должен падать — но
        # если валидация модели когда-либо изменится, внешний контракт
        # этого метода (InvalidProvenancePayloadError, а не сырой
        # ValueError/TypeError модельного слоя) не должен молча
        # нарушиться. str(exc) исходного исключения модели намеренно НЕ
        # попадает в сообщение.
        try:
            return IdentifierCellProvenance(
                sheet_name=sheet_name,
                row=row,
                column=column,
                token=token,
                representation=representation,
            )
        except (TypeError, ValueError) as exc:
            raise InvalidProvenancePayloadError(
                f"Запись provenance под индексом {index} структурно повреждена"
            ) from exc

    # ------------------------------------------------------------------
    # Сериализация + шифрование + атомарная запись
    # ------------------------------------------------------------------

    @staticmethod
    def _serialize(store: InMemoryProvenanceStore) -> bytes:
        """Сериализация payload в RAM. JSON никогда не попадает на диск."""
        payload = {
            "schema_version": PROVENANCE_PAYLOAD_SCHEMA_VERSION,
            "job_id": store.job_id,
            "entries": [
                {
                    "sheet_name": entry.sheet_name,
                    "row": entry.row,
                    "column": entry.column,
                    "token": entry.token,
                    "representation": entry.representation.value,
                }
                for entry in store.entries()
            ],
        }
        text = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return text.encode("utf-8")

    def _persist(self, candidate: InMemoryProvenanceStore) -> None:
        plaintext = self._serialize(candidate)
        # encrypt_bytes может поднять TypeError/ValueError (некорректный
        # password/plaintext на уровне контракта crypto) — они проходят
        # наружу как есть, до какой-либо записи на диск.
        encrypted = encrypt_bytes(plaintext, self._password)
        self._atomic_write(encrypted)

    def _atomic_write(self, encrypted: bytes) -> None:
        """
        Атомарная запись encrypted bytes на диск (идентично Stage
        4/7B.4.1):

            temp-файл в ТОМ ЖЕ каталоге -> write -> flush -> fsync ->
            close -> os.replace(temp, target).

        На диск попадают только encrypted bytes. При любой ошибке ДО
        успешного os.replace: старый target (если был) не повреждается,
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
                pass
            raise
