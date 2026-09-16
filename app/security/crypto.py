"""
Криптографическое ядро DataAnonymizer: симметричное шифрование
произвольных bytes паролем.

Модуль работает исключительно с bytes <-> bytes. Он ничего не знает про
файлы, пути, mapping или Excel — это сознательное архитектурное
ограничение Этапа 3 (файловое хранилище зашифрованного mapping появится
отдельно, на Этапе 4, поверх этого модуля).

======================================================================
Пайплайн
======================================================================

Шифрование:

    plaintext bytes
        -> password (str) + случайная соль -> Scrypt -> derived key (32 байта)
        -> AES-256-GCM(derived key, случайный nonce, plaintext, AAD=header)
        -> versioned binary container (bytes)

Расшифровка:

    encrypted container (bytes)
        -> разбор и структурная валидация header (ДО какой-либо криптографии)
        -> валидация параметров KDF (N/r/p) на предмет разумности (ДО Scrypt)
        -> password (str) + соль из контейнера -> Scrypt -> derived key
        -> AES-256-GCM authentication/decrypt (header снова как AAD)
        -> plaintext bytes

======================================================================
Формат контейнера (version 1)
======================================================================

Контейнер — это конкатенация header (фиксированного размера для version 1)
и payload (ciphertext || GCM tag, переменной длины). Никакого pickle,
JSON, base64-обёртки или готового формата вроде Fernet — только
самостоятельно определённый бинарный layout через struct, big-endian.

Header (ровно 40 байт для version 1), формат struct ">4sBBIBB16s12s":

    смещение  размер  поле       тип      описание
    --------  ------  ---------  -------  ------------------------------
    0         4       magic      4s       b"DANZ" (Data ANonymiZer)
    4         1       version    B        FORMAT_VERSION = 1
    5         1       kdf_id     B        KDF_ID_SCRYPT = 1
    6         4       n          I        параметр Scrypt N (uint32, BE)
    10        1       r          B        параметр Scrypt r (uint8)
    11        1       p          B        параметр Scrypt p (uint8)
    12        16      salt       16s      соль Scrypt (16 случайных байт)
    28        12      nonce      12s      nonce AES-GCM (12 случайных байт)
    --------  ------  ---------  -------  ------------------------------
    итого: 40 байт

Payload (после header, произвольной длины >= 16 байт):

    ciphertext || GCM tag (16 байт)

`AESGCM.encrypt()` из `cryptography` сам возвращает `ciphertext || tag`
единым blob'ом — отделять tag вручную не нужно, библиотека сама проверяет
его при decrypt. Для пустого plaintext payload равен ровно 16 байтам
(один только tag, без ciphertext) — это валидный, ожидаемый случай.

Полный контейнер = header (40 байт) + payload (>= 16 байт).

======================================================================
Почему header — это AAD (Associated Authenticated Data)
======================================================================

Header (все 40 байт: magic, version, kdf_id, N, r, p, salt, nonce)
передаётся в AESGCM.encrypt/decrypt как associated_data. Header НЕ
шифруется (он и так не секретен — это параметры, необходимые для того,
чтобы расшифровать), но благодаря AAD он АУТЕНТИФИЦИРУЕТСЯ вместе с
ciphertext одним и тем же GCM-тегом. Если атакующий незаметно подменит
N, r, p, salt, nonce, version или kdf_id, тег перестанет сходиться и
decrypt закончится ошибкой аутентификации — то есть подмена параметров,
из которых потом выводится ключ, не может привести к тихой расшифровке
с "подсунутыми" параметрами.

======================================================================
Wrong password и tampering — намеренно неразличимы
======================================================================

AES-GCM authentication failure (`cryptography.exceptions.InvalidTag`)
возникает и при неверном пароле (неверный derived key), и при повреждении
ciphertext/tag/header. Криптографически безопасного способа различить эти
два случая нет, а попытка его дать превратилась бы в oracle, помогающий
атаке. Поэтому оба случая всегда превращаются в один и тот же
`DecryptionError` с нейтральным сообщением, а `InvalidTag` никогда не
покидает этот модуль напрямую.

======================================================================
Защита от KDF DoS
======================================================================

N/r/p при расшифровке читаются из НЕДОВЕРЕННОГО контейнера — до того, как
GCM подтвердит его подлинность. Если передать их в Scrypt без проверки,
специально сформированный (но структурно валидный) файл с огромными N/r
способен заставить приложение выделить гигабайты памяти и надолго
загрузить CPU ещё до того, как мы вообще узнаем, легитимен ли контейнер.
Поэтому параметры проверяются на разумные границы (и на оценку
потребления памяти) ДО любого вызова Scrypt — см. `_validate_kdf_parameters`.
"""

from __future__ import annotations

import secrets
import struct

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.scrypt import Scrypt

# ----------------------------------------------------------------------
# Исключения криптографического слоя
# ----------------------------------------------------------------------


class CryptoError(Exception):
    """Базовое исключение криптографического слоя DataAnonymizer."""


class InvalidContainerError(CryptoError):
    """
    Контейнер структурно некорректен: неверный MAGIC, усечённый header
    или усечённый payload (короче минимального размера GCM-тега).

    Эта ошибка обнаруживается по одной только длине/структуре байтов, ещё
    до какой-либо криптографии.
    """


class UnsupportedContainerVersionError(CryptoError):
    """FORMAT_VERSION контейнера неизвестен этой версии DataAnonymizer."""


class UnsupportedKDFError(CryptoError):
    """KDF_ID контейнера неизвестен (сейчас поддерживается только Scrypt)."""


class InvalidKDFParametersError(CryptoError):
    """
    Параметры Scrypt (N/r/p), прочитанные из контейнера, структурно
    допустимы (правильный тип/размер поля), но выходят за разумные
    границы или означали бы чрезмерное потребление памяти/CPU.

    Выделено в отдельный класс, а не смешано с InvalidContainerError:
    "контейнер повреждён/усечён" и "контейнер цел, но просит нас
    выполнить потенциально разрушительный по ресурсам Scrypt" — разные по
    смыслу ситуации, и вызывающему коду может понадобиться реагировать на
    них по-разному.
    """


class DecryptionError(CryptoError):
    """
    Не удалось расшифровать данные: неверный пароль или контейнер
    повреждён/подделан. Оба сценария намеренно неразличимы — см. docstring
    модуля.
    """


# ----------------------------------------------------------------------
# Константы формата и production-параметры
# ----------------------------------------------------------------------

MAGIC = b"DANZ"
FORMAT_VERSION = 1
KDF_ID_SCRYPT = 1

SALT_SIZE = 16
NONCE_SIZE = 12
KEY_SIZE = 32  # AES-256
GCM_TAG_SIZE = 16  # фиксированный размер тега у AESGCM из `cryptography`

# Header version 1: magic(4s) version(B) kdf_id(B) n(I) r(B) p(B) salt(16s) nonce(12s)
# Big-endian — размер и порядок байт зафиксированы явно, а не полагаются
# на native struct layout текущей платформы.
_HEADER_V1_STRUCT = struct.Struct(">4sBBIBB16s12s")
_HEADER_V1_SIZE = _HEADER_V1_STRUCT.size  # 40 байт

# Минимальный префикс, которого достаточно, чтобы прочитать magic+version
# и понять, версия ли это 1 (и только тогда require остальные 40 байт).
# Так "неизвестная версия" отличается от "версия 1, но файл усечён", даже
# если сам контейнер короче полного header'а version 1.
_COMMON_PREFIX_STRUCT = struct.Struct(">4sB")
_COMMON_PREFIX_SIZE = _COMMON_PREFIX_STRUCT.size  # 5 байт

# Production-параметры Scrypt (используются при encrypt_bytes).
SCRYPT_N = 2**15
SCRYPT_R = 8
SCRYPT_P = 1

# ----------------------------------------------------------------------
# Границы допустимых KDF-параметров для version 1 (защита от DoS).
#
# Читаются из недоверенного контейнера при расшифровке, поэтому должны
# быть проверены до запуска Scrypt (см. docstring модуля и
# _validate_kdf_parameters). Production-константы выше (N=2**15, r=8, p=1)
# заведомо укладываются во все границы ниже.
# ----------------------------------------------------------------------

_MIN_N = 2**14
_MAX_N = 2**20
_MIN_R = 1
_MAX_R = 32
_MIN_P = 1
_MAX_P = 16

# Оценка потребления памяти Scrypt приблизительно равна 128*N*r байт
# (формула из ТЗ/RFC 7914). Одних только границ N и r по отдельности
# недостаточно: при N=2**20 и r=32 одновременно (оба значения по
# отдельности допустимы) оценка составила бы 128*2**20*32 ≈ 4 ГиБ на один
# вызов расшифровки — реальный вектор DoS. Поэтому дополнительно
# проверяем ИХ ПРОИЗВЕДЕНИЕ отдельным бюджетом.
#
# 256 МиБ выбраны как бюджет: это в 8 раз больше, чем требуют
# production-параметры (N=2**15, r=8 -> 32 МиБ), что оставляет разумный
# запас на будущее ужесточение параметров, но не допускает разгона
# памяти до гигабайт одним specially crafted контейнером.
_MAX_SCRYPT_MEMORY_BYTES = 256 * 1024 * 1024


def _validate_kdf_parameters(n: int, r: int, p: int) -> None:
    """
    Проверяет параметры Scrypt на разумные границы и на оценку
    потребления памяти — ДО того, как они попадут в сам Scrypt.

    КРИТИЧНО для безопасности: n/r/p на этом этапе прочитаны из
    контейнера, чья подлинность ЕЩЁ НЕ подтверждена GCM-аутентификацией
    (она происходит позже). Специально сформированный, но структурно
    валидный файл может выставить огромные N/r, чтобы заставить
    приложение потратить гигабайты RAM и заметное время CPU ещё до того,
    как мы узнаем, что сам файл вообще подлинный/имеет смысл
    расшифровывать. Поэтому эта проверка обязана пройти раньше вызова
    Scrypt.derive(...).
    """

    if n & (n - 1) != 0 or n <= 0:
        raise InvalidKDFParametersError(f"Scrypt N={n} должен быть степенью двойки")
    if n < _MIN_N or n > _MAX_N:
        raise InvalidKDFParametersError(
            f"Scrypt N={n} вне допустимого диапазона [{_MIN_N}, {_MAX_N}]"
        )
    if r < _MIN_R or r > _MAX_R:
        raise InvalidKDFParametersError(
            f"Scrypt r={r} вне допустимого диапазона [{_MIN_R}, {_MAX_R}]"
        )
    if p < _MIN_P or p > _MAX_P:
        raise InvalidKDFParametersError(
            f"Scrypt p={p} вне допустимого диапазона [{_MIN_P}, {_MAX_P}]"
        )

    estimated_memory = 128 * n * r
    if estimated_memory > _MAX_SCRYPT_MEMORY_BYTES:
        raise InvalidKDFParametersError(
            f"Оценка потребления памяти Scrypt (128*N*r={estimated_memory} байт) "
            f"превышает допустимый бюджет {_MAX_SCRYPT_MEMORY_BYTES} байт"
        )


# ----------------------------------------------------------------------
# Валидация входов публичного API
# ----------------------------------------------------------------------


def _validate_plaintext(plaintext: object) -> None:
    if not isinstance(plaintext, bytes):
        raise TypeError(f"plaintext должен быть bytes, получено: {type(plaintext)!r}")


def _validate_encrypted(encrypted: object) -> None:
    if not isinstance(encrypted, bytes):
        raise TypeError(f"encrypted должен быть bytes, получено: {type(encrypted)!r}")


def _validate_password(password: object) -> None:
    # Пароль должен отклоняться понятной ошибкой ДО запуска KDF — и по
    # типу, и по факту пустоты. Пароль НИКАК не изменяется (не trim, не
    # нормализация регистра/Unicode): "password", " password" и "PASSWORD"
    # обязаны давать разные ключи.
    if not isinstance(password, str):
        raise TypeError(f"password должен быть str, получено: {type(password)!r}")
    if password == "":
        raise ValueError("password не может быть пустым")


# ----------------------------------------------------------------------
# Внутренние помощники: KDF, разбор header
# ----------------------------------------------------------------------


def _derive_key(password: str, salt: bytes, n: int, r: int, p: int) -> bytes:
    # Пароль кодируется в UTF-8 как есть, без каких-либо преобразований —
    # это единственный шаг между вводом пользователя и KDF.
    kdf = Scrypt(salt=salt, length=KEY_SIZE, n=n, r=r, p=p)
    return kdf.derive(password.encode("utf-8"))


def _parse_header(container: bytes) -> tuple[dict, bytes, bytes]:
    """
    Разбирает и структурно валидирует header контейнера.

    Возвращает (поля_header, header_bytes, payload_bytes). Не бросает
    ничего, кроме определённых в этом модуле исключений — ни IndexError,
    ни struct.error наружу не просачиваются.
    """

    if len(container) < _COMMON_PREFIX_SIZE:
        raise InvalidContainerError(
            "Контейнер короче минимального префикса (magic + version)"
        )

    magic, version = _COMMON_PREFIX_STRUCT.unpack(container[:_COMMON_PREFIX_SIZE])

    if magic != MAGIC:
        raise InvalidContainerError("Неверная сигнатура контейнера (MAGIC)")

    if version != FORMAT_VERSION:
        raise UnsupportedContainerVersionError(
            f"Неподдерживаемая версия контейнера: {version}"
        )

    if len(container) < _HEADER_V1_SIZE:
        raise InvalidContainerError("Контейнер короче полного header version 1")

    header_bytes = container[:_HEADER_V1_SIZE]
    _, _, kdf_id, n, r, p, salt, nonce = _HEADER_V1_STRUCT.unpack(header_bytes)

    if kdf_id != KDF_ID_SCRYPT:
        raise UnsupportedKDFError(f"Неподдерживаемый KDF_ID: {kdf_id}")

    payload = container[_HEADER_V1_SIZE:]
    if len(payload) < GCM_TAG_SIZE:
        raise InvalidContainerError(
            "Payload короче минимального размера GCM-тега — контейнер усечён"
        )

    fields = {"n": n, "r": r, "p": p, "salt": salt, "nonce": nonce}
    return fields, header_bytes, payload


# ----------------------------------------------------------------------
# Публичный API
# ----------------------------------------------------------------------


def encrypt_bytes(plaintext: bytes, password: str) -> bytes:
    """
    Шифрует plaintext паролем и возвращает самодостаточный versioned
    binary container (см. docstring модуля).

    Каждый вызов использует НОВЫЕ случайные соль и nonce (secrets.token_bytes) —
    даже для одинаковых plaintext и password результат будет разным. Это
    не опция, а обязательное требование безопасности: одинаковый
    (ключ, nonce) для AES-GCM, использованный дважды, раскрывает XOR
    открытых текстов и ломает аутентификацию — поэтому nonce всегда новый,
    а свежая соль гарантирует, что при том же пароле не переиспользуется
    и производный ключ.

    :raises TypeError: если plaintext не bytes или password не str.
    :raises ValueError: если password пустой.
    """

    _validate_plaintext(plaintext)
    _validate_password(password)

    salt = secrets.token_bytes(SALT_SIZE)
    nonce = secrets.token_bytes(NONCE_SIZE)

    key = _derive_key(password, salt, SCRYPT_N, SCRYPT_R, SCRYPT_P)

    header = _HEADER_V1_STRUCT.pack(
        MAGIC, FORMAT_VERSION, KDF_ID_SCRYPT, SCRYPT_N, SCRYPT_R, SCRYPT_P, salt, nonce
    )

    # header передаётся как associated_data (AAD): не шифруется, но
    # участвует в GCM-аутентификации — см. docstring модуля.
    aesgcm = AESGCM(key)
    payload = aesgcm.encrypt(nonce, plaintext, header)

    return header + payload


def decrypt_bytes(encrypted: bytes, password: str) -> bytes:
    """
    Расшифровывает container, созданный encrypt_bytes, и возвращает
    исходный plaintext.

    :raises TypeError: если encrypted не bytes или password не str.
    :raises ValueError: если password пустой.
    :raises InvalidContainerError: контейнер структурно некорректен
        (неверный MAGIC, усечён header или payload).
    :raises UnsupportedContainerVersionError: неизвестная FORMAT_VERSION.
    :raises UnsupportedKDFError: неизвестный KDF_ID.
    :raises InvalidKDFParametersError: параметры Scrypt вне допустимых
        границ или означали бы чрезмерное потребление ресурсов.
    :raises DecryptionError: неверный пароль либо ciphertext/tag/header
        повреждены или подделаны (эти случаи неразличимы намеренно).
    """

    _validate_encrypted(encrypted)
    _validate_password(password)

    fields, header_bytes, payload = _parse_header(encrypted)

    # Структурная валидация header уже пройдена, но подлинность контейнера
    # (GCM-аутентификация) — ещё нет. Поэтому параметры KDF, прочитанные
    # из недоверенных байт, обязаны пройти проверку границ ресурсов
    # раньше, чем попадут в Scrypt.
    _validate_kdf_parameters(fields["n"], fields["r"], fields["p"])

    key = _derive_key(password, fields["salt"], fields["n"], fields["r"], fields["p"])

    aesgcm = AESGCM(key)
    try:
        plaintext = aesgcm.decrypt(fields["nonce"], payload, header_bytes)
    except InvalidTag:
        # Неверный пароль и повреждение/подделка данных дают здесь одно и
        # то же исключение библиотеки — и должны давать одну и ту же
        # реакцию наружу. `from None` намеренно обрывает цепочку
        # исключений, чтобы InvalidTag не "просвечивал" через traceback.
        raise DecryptionError(
            "Не удалось расшифровать данные: неверный пароль или данные повреждены."
        ) from None

    return plaintext
