"""
Тесты app.security.crypto: AES-256-GCM + Scrypt + versioned binary container.
"""

from __future__ import annotations

import struct

import pytest
from cryptography.exceptions import InvalidTag

from app.security import crypto


# ---------------------------------------------------------------------------
# Вспомогательные функции
# ---------------------------------------------------------------------------


def _valid_container(
    *,
    n: int = crypto.SCRYPT_N,
    r: int = crypto.SCRYPT_R,
    p: int = crypto.SCRYPT_P,
    salt: bytes = b"S" * crypto.SALT_SIZE,
    nonce: bytes = b"N" * crypto.NONCE_SIZE,
    password: str = "test-password",
    plaintext: bytes = b"secret payload",
) -> bytes:
    """
    Строит валидный контейнер с полным контролем над salt/nonce/N/r/p —
    удобно для тестов AAD/структуры, где нужно вручную пересобрать header
    и зашифровать payload согласованно с ним.
    """
    header = crypto._HEADER_V1_STRUCT.pack(
        crypto.MAGIC, crypto.FORMAT_VERSION, crypto.KDF_ID_SCRYPT, n, r, p, salt, nonce
    )
    key = crypto._derive_key(password, salt, n, r, p)
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    payload = AESGCM(key).encrypt(nonce, plaintext, header)
    return header + payload


# ---------------------------------------------------------------------------
# 1-3. Round-trip: обычные данные, пустой plaintext, все значения байтов
# ---------------------------------------------------------------------------


def test_round_trip_basic() -> None:
    data = "Продажи за январь: 1 234 567.89 руб.".encode("utf-8")
    password = "correct horse battery staple"
    encrypted = crypto.encrypt_bytes(data, password)
    assert crypto.decrypt_bytes(encrypted, password) == data


def test_round_trip_empty_plaintext() -> None:
    encrypted = crypto.encrypt_bytes(b"", "pw")
    payload_len = len(encrypted) - crypto._HEADER_V1_SIZE
    assert payload_len == crypto.GCM_TAG_SIZE  # только tag, без ciphertext
    assert crypto.decrypt_bytes(encrypted, "pw") == b""


def test_round_trip_all_byte_values() -> None:
    data = bytes(range(256)) * 4  # включает 0x00 и все остальные значения байта
    encrypted = crypto.encrypt_bytes(data, "pw")
    assert crypto.decrypt_bytes(encrypted, "pw") == data


# ---------------------------------------------------------------------------
# 4-5. Unicode пароль, чувствительность к регистру/пробелам
# ---------------------------------------------------------------------------


def test_unicode_password() -> None:
    data = b"data"
    password = "пароль-с-юникодом-🔐"
    encrypted = crypto.encrypt_bytes(data, password)
    assert crypto.decrypt_bytes(encrypted, password) == data


@pytest.mark.parametrize(
    "correct,wrong",
    [
        ("password", "PASSWORD"),
        ("password", " password"),
        ("password", "password "),
        ("password", "Password"),
    ],
)
def test_password_is_case_and_whitespace_sensitive(correct: str, wrong: str) -> None:
    encrypted = crypto.encrypt_bytes(b"data", correct)
    with pytest.raises(crypto.DecryptionError):
        crypto.decrypt_bytes(encrypted, wrong)


# ---------------------------------------------------------------------------
# 6. Одинаковые plaintext+password дважды -> разные encrypted bytes
# ---------------------------------------------------------------------------


def test_same_input_twice_yields_different_ciphertext() -> None:
    a = crypto.encrypt_bytes(b"same data", "same password")
    b = crypto.encrypt_bytes(b"same data", "same password")
    assert a != b
    # но оба корректно расшифровываются
    assert crypto.decrypt_bytes(a, "same password") == b"same data"
    assert crypto.decrypt_bytes(b, "same password") == b"same data"


# ---------------------------------------------------------------------------
# 7-9. Неверный пароль, изменение ciphertext, изменение tag
# ---------------------------------------------------------------------------


def test_wrong_password_raises_decryption_error() -> None:
    encrypted = crypto.encrypt_bytes(b"data", "correct")
    with pytest.raises(crypto.DecryptionError):
        crypto.decrypt_bytes(encrypted, "incorrect")


def test_tampered_ciphertext_raises_decryption_error() -> None:
    encrypted = bytearray(crypto.encrypt_bytes(b"some plaintext data", "pw"))
    # payload = ciphertext(19 байт) + tag(16 байт); портим байт внутри ciphertext.
    tamper_index = crypto._HEADER_V1_SIZE + 2
    encrypted[tamper_index] ^= 0xFF
    with pytest.raises(crypto.DecryptionError):
        crypto.decrypt_bytes(bytes(encrypted), "pw")


def test_tampered_gcm_tag_raises_decryption_error() -> None:
    encrypted = bytearray(crypto.encrypt_bytes(b"data", "pw"))
    encrypted[-1] ^= 0xFF  # последний байт контейнера всегда внутри GCM tag
    with pytest.raises(crypto.DecryptionError):
        crypto.decrypt_bytes(bytes(encrypted), "pw")


# ---------------------------------------------------------------------------
# 10 / 19. Изменение authenticated header (salt/nonce) -> DecryptionError.
# Это ключевой тест, доказывающий, что header реально используется как AAD.
# ---------------------------------------------------------------------------


def test_tampered_salt_in_aad_header_raises_decryption_error() -> None:
    container = bytearray(_valid_container())
    salt_offset = 12  # magic(4)+version(1)+kdf_id(1)+n(4)+r(1)+p(1) = 12
    container[salt_offset] ^= 0xFF  # контейнер остаётся структурно валидным
    with pytest.raises(crypto.DecryptionError):
        crypto.decrypt_bytes(bytes(container), "test-password")


def test_tampered_nonce_in_aad_header_raises_decryption_error() -> None:
    container = bytearray(_valid_container())
    nonce_offset = 12 + crypto.SALT_SIZE  # сразу после salt
    container[nonce_offset] ^= 0xFF
    with pytest.raises(crypto.DecryptionError):
        crypto.decrypt_bytes(bytes(container), "test-password")


# ---------------------------------------------------------------------------
# 11-14. Структурные ошибки контейнера
# ---------------------------------------------------------------------------


def test_bad_magic_raises_invalid_container_error() -> None:
    container = b"XXXX" + crypto.encrypt_bytes(b"data", "pw")[4:]
    with pytest.raises(crypto.InvalidContainerError):
        crypto.decrypt_bytes(container, "pw")


def test_truncated_header_raises_invalid_container_error() -> None:
    encrypted = crypto.encrypt_bytes(b"data", "pw")
    truncated = encrypted[:10]  # меньше даже common-prefix+часть остального header
    with pytest.raises(crypto.InvalidContainerError):
        crypto.decrypt_bytes(truncated, "pw")


def test_truncated_common_prefix_raises_invalid_container_error() -> None:
    with pytest.raises(crypto.InvalidContainerError):
        crypto.decrypt_bytes(b"DA", "pw")  # короче даже magic+version


def test_truncated_payload_raises_invalid_container_error() -> None:
    encrypted = crypto.encrypt_bytes(b"data", "pw")
    # Оставляем весь header, но обрезаем payload короче GCM-тега.
    truncated = encrypted[: crypto._HEADER_V1_SIZE + crypto.GCM_TAG_SIZE - 1]
    with pytest.raises(crypto.InvalidContainerError):
        crypto.decrypt_bytes(truncated, "pw")


def test_unsupported_format_version_raises_specific_error() -> None:
    container = bytearray(crypto.encrypt_bytes(b"data", "pw"))
    container[4] = 99  # смещение version
    with pytest.raises(crypto.UnsupportedContainerVersionError):
        crypto.decrypt_bytes(bytes(container), "pw")


def test_unsupported_kdf_id_raises_specific_error() -> None:
    container = bytearray(crypto.encrypt_bytes(b"data", "pw"))
    container[5] = 99  # смещение kdf_id
    with pytest.raises(crypto.UnsupportedKDFError):
        crypto.decrypt_bytes(bytes(container), "pw")


# ---------------------------------------------------------------------------
# 16-18. Невалидные N / r / p (структурно валидный header, плохие параметры)
# ---------------------------------------------------------------------------


def _container_with_kdf_params(n: int, r: int, p: int) -> bytes:
    header = crypto._HEADER_V1_STRUCT.pack(
        crypto.MAGIC,
        crypto.FORMAT_VERSION,
        crypto.KDF_ID_SCRYPT,
        n,
        r,
        p,
        b"S" * crypto.SALT_SIZE,
        b"N" * crypto.NONCE_SIZE,
    )
    # Payload не обязан быть осмысленным — ошибка параметров должна
    # произойти раньше, чем мы дойдём до AES-GCM.
    return header + b"0" * crypto.GCM_TAG_SIZE


@pytest.mark.parametrize("bad_n", [2**14 + 1, 2**13, 2**21, 0, -(2**14)])
def test_invalid_scrypt_n_rejected(bad_n: int) -> None:
    # bad_n может не влезть в uint32 (например, отрицательное) —
    # для таких значений просто пропускаем упаковку через struct и
    # проверяем валидатор напрямую.
    try:
        container = _container_with_kdf_params(bad_n, crypto.SCRYPT_R, crypto.SCRYPT_P)
    except struct.error:
        with pytest.raises(crypto.InvalidKDFParametersError):
            crypto._validate_kdf_parameters(bad_n, crypto.SCRYPT_R, crypto.SCRYPT_P)
        return
    with pytest.raises(crypto.InvalidKDFParametersError):
        crypto.decrypt_bytes(container, "pw")


@pytest.mark.parametrize("bad_r", [0, 33, 255])
def test_invalid_scrypt_r_rejected(bad_r: int) -> None:
    container = _container_with_kdf_params(crypto.SCRYPT_N, bad_r, crypto.SCRYPT_P)
    with pytest.raises(crypto.InvalidKDFParametersError):
        crypto.decrypt_bytes(container, "pw")


@pytest.mark.parametrize("bad_p", [0, 17, 255])
def test_invalid_scrypt_p_rejected(bad_p: int) -> None:
    container = _container_with_kdf_params(crypto.SCRYPT_N, crypto.SCRYPT_R, bad_p)
    with pytest.raises(crypto.InvalidKDFParametersError):
        crypto.decrypt_bytes(container, "pw")


def test_scrypt_n_must_be_power_of_two() -> None:
    # 2**14 + 2**13 = 24576 — в допустимом диапазоне по величине, но не
    # является степенью двойки.
    bad_n = 2**14 + 2**13
    container = _container_with_kdf_params(bad_n, crypto.SCRYPT_R, crypto.SCRYPT_P)
    with pytest.raises(crypto.InvalidKDFParametersError):
        crypto.decrypt_bytes(container, "pw")


# ---------------------------------------------------------------------------
# 19. Огромные KDF-параметры отклоняются ДО фактического Scrypt
# (доказываем через monkeypatch, что Scrypt.derive не вызывался)
# ---------------------------------------------------------------------------


def test_huge_kdf_parameters_rejected_before_scrypt_runs(monkeypatch) -> None:
    called = {"derive": False}

    def fake_derive(self, data):  # pragma: no cover - не должен вызываться
        called["derive"] = True
        raise AssertionError("Scrypt.derive не должен вызываться для отклонённых параметров")

    monkeypatch.setattr("cryptography.hazmat.primitives.kdf.scrypt.Scrypt.derive", fake_derive)

    # N=2**20, r=32, p=16 — каждое значение по отдельности в допустимых
    # границах, но их произведение (128*N*r) многократно превышает бюджет
    # памяти. Именно этот комбинированный случай и обязан быть отловлен.
    huge_container = _container_with_kdf_params(2**20, 32, 16)

    with pytest.raises(crypto.InvalidKDFParametersError):
        crypto.decrypt_bytes(huge_container, "pw")

    assert called["derive"] is False


def test_huge_n_alone_rejected_before_scrypt_runs(monkeypatch) -> None:
    called = {"derive": False}

    def fake_derive(self, data):  # pragma: no cover
        called["derive"] = True
        raise AssertionError("не должен вызываться")

    monkeypatch.setattr("cryptography.hazmat.primitives.kdf.scrypt.Scrypt.derive", fake_derive)

    huge_container = _container_with_kdf_params(2**20, crypto.SCRYPT_R, crypto.SCRYPT_P)

    with pytest.raises(crypto.InvalidKDFParametersError):
        crypto.decrypt_bytes(huge_container, "pw")

    assert called["derive"] is False


# ---------------------------------------------------------------------------
# 20-23. Невалидные типы/значения аргументов публичного API
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("bad_plaintext", ["not bytes", 123, None, bytearray(b"x"), [1, 2]])
def test_encrypt_rejects_invalid_plaintext_type(bad_plaintext) -> None:
    with pytest.raises(TypeError):
        crypto.encrypt_bytes(bad_plaintext, "pw")  # type: ignore[arg-type]


@pytest.mark.parametrize("bad_encrypted", ["not bytes", 123, None, bytearray(b"x"), [1, 2]])
def test_decrypt_rejects_invalid_encrypted_type(bad_encrypted) -> None:
    with pytest.raises(TypeError):
        crypto.decrypt_bytes(bad_encrypted, "pw")  # type: ignore[arg-type]


@pytest.mark.parametrize("bad_password", [123, None, b"bytes-password", 3.14])
def test_encrypt_rejects_invalid_password_type(bad_password) -> None:
    with pytest.raises(TypeError):
        crypto.encrypt_bytes(b"data", bad_password)  # type: ignore[arg-type]


@pytest.mark.parametrize("bad_password", [123, None, b"bytes-password", 3.14])
def test_decrypt_rejects_invalid_password_type(bad_password) -> None:
    encrypted = crypto.encrypt_bytes(b"data", "pw")
    with pytest.raises(TypeError):
        crypto.decrypt_bytes(encrypted, bad_password)  # type: ignore[arg-type]


def test_encrypt_rejects_empty_password() -> None:
    with pytest.raises(ValueError):
        crypto.encrypt_bytes(b"data", "")


def test_decrypt_rejects_empty_password() -> None:
    encrypted = crypto.encrypt_bytes(b"data", "pw")
    with pytest.raises(ValueError):
        crypto.decrypt_bytes(encrypted, "")


def test_empty_password_rejected_before_kdf(monkeypatch) -> None:
    """Пустой пароль должен отклоняться до запуска Scrypt, а не внутри него."""

    def fake_derive(self, data):  # pragma: no cover
        raise AssertionError("Scrypt.derive не должен вызываться для пустого пароля")

    monkeypatch.setattr("cryptography.hazmat.primitives.kdf.scrypt.Scrypt.derive", fake_derive)
    with pytest.raises(ValueError):
        crypto.encrypt_bytes(b"data", "")


# ---------------------------------------------------------------------------
# 24-25. Container начинается с MAGIC+version; layout детерминирован при
# замоканной случайности
# ---------------------------------------------------------------------------


def test_container_starts_with_magic_and_version() -> None:
    encrypted = crypto.encrypt_bytes(b"data", "pw")
    assert encrypted[:4] == crypto.MAGIC
    assert encrypted[4] == crypto.FORMAT_VERSION


def test_header_layout_deterministic_with_mocked_randomness(monkeypatch) -> None:
    """
    При замоканном источнике случайности header (magic/version/kdf_id/N/r/p/
    salt/nonce) должен быть побайтово детерминирован и в точности
    соответствовать документированному layout version 1.
    """
    fake_salt = bytes([0xAA]) * crypto.SALT_SIZE
    fake_nonce = bytes([0xBB]) * crypto.NONCE_SIZE
    sequence = iter([fake_salt, fake_nonce])

    def fake_token_bytes(n: int) -> bytes:
        value = next(sequence)
        assert len(value) == n
        return value

    monkeypatch.setattr(crypto.secrets, "token_bytes", fake_token_bytes)

    encrypted = crypto.encrypt_bytes(b"deterministic", "pw")
    header = encrypted[: crypto._HEADER_V1_SIZE]

    expected_header = crypto._HEADER_V1_STRUCT.pack(
        crypto.MAGIC,
        crypto.FORMAT_VERSION,
        crypto.KDF_ID_SCRYPT,
        crypto.SCRYPT_N,
        crypto.SCRYPT_R,
        crypto.SCRYPT_P,
        fake_salt,
        fake_nonce,
    )
    assert header == expected_header

    magic, version, kdf_id, n, r, p, salt, nonce = crypto._HEADER_V1_STRUCT.unpack(header)
    assert magic == crypto.MAGIC
    assert version == crypto.FORMAT_VERSION
    assert kdf_id == crypto.KDF_ID_SCRYPT
    assert n == crypto.SCRYPT_N
    assert r == crypto.SCRYPT_R
    assert p == crypto.SCRYPT_P
    assert salt == fake_salt
    assert nonce == fake_nonce


# ---------------------------------------------------------------------------
# 26. Production-константы
# ---------------------------------------------------------------------------


def test_production_constants() -> None:
    assert crypto.SCRYPT_N == 2**15
    assert crypto.SCRYPT_R == 8
    assert crypto.SCRYPT_P == 1
    assert crypto.KEY_SIZE == 32
    assert crypto.SALT_SIZE == 16
    assert crypto.NONCE_SIZE == 12
    assert crypto.MAGIC == b"DANZ"
    assert crypto.FORMAT_VERSION == 1
    assert crypto._HEADER_V1_SIZE == 40


# ---------------------------------------------------------------------------
# 27. cryptography.exceptions.InvalidTag не утекает наружу
# ---------------------------------------------------------------------------


def test_invalid_tag_does_not_leak_as_is() -> None:
    encrypted = crypto.encrypt_bytes(b"data", "correct")
    with pytest.raises(crypto.DecryptionError) as exc_info:
        crypto.decrypt_bytes(encrypted, "wrong")

    assert not isinstance(exc_info.value, InvalidTag)
    assert exc_info.value.__cause__ is None  # `from None` оборвал цепочку
    assert "InvalidTag" not in str(exc_info.value)
    assert "неверный пароль" in str(exc_info.value) or "повреждены" in str(exc_info.value)


# ---------------------------------------------------------------------------
# Иерархия исключений
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "exc_class",
    [
        crypto.InvalidContainerError,
        crypto.UnsupportedContainerVersionError,
        crypto.UnsupportedKDFError,
        crypto.InvalidKDFParametersError,
        crypto.DecryptionError,
    ],
)
def test_all_crypto_exceptions_inherit_from_crypto_error(exc_class) -> None:
    assert issubclass(exc_class, crypto.CryptoError)
    assert issubclass(crypto.CryptoError, Exception)
