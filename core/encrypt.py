"""
EncryptedTextField для Django 6

Шифрование на уровне приложения через Fernet (симметричное,
AES-128-CBC + HMAC) из библиотеки `cryptography`.
Расширения PostgreSQL (pgcrypto и т.п.) не требуются — шифрование
и расшифровка происходят в Python до/после обращения к БД.

Использование:

    # settings.py
    FIELD_ENCRYPTION_KEYS = [
        env("FIELD_ENCRYPTION_KEY"),        # текущий (активный) ключ
        env("FIELD_ENCRYPTION_KEY_OLD", ""),# опционально: старые ключи для ротации
    ]

    # models.py
    from app.encrypt import EncryptedTextField

    class User(models.Model):
        access_token = EncryptedTextField(blank=True, null=True)
"""

import base64

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from django.db import models

from cryptography.fernet import Fernet, MultiFernet, InvalidToken
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF


# Фиксированная "соль" для HKDF при нормализации произвольного ключа.
# Это не секрет — она нужна только чтобы разделить контексты
# использования, если понадобится выводить несколько разных
# ключей из одного и того же значения.
_HKDF_SALT = b"django-encrypted-fields-v1"


def _normalize_key(raw_key) -> bytes:
    """
    Приводит произвольный ключ из настроек к формату, который
    понимает Fernet (32 байта в url-safe base64).

    Если raw_key уже валидный Fernet-ключ (например, результат
    Fernet.generate_key()) — используется как есть.
    Если это произвольная строка/фраза — она прогоняется через
    HKDF-SHA256 и приводится к нужному формату автоматически.
    """
    if isinstance(raw_key, str):
        raw_key = raw_key.encode("utf-8")

    try:
        # Если это уже валидный Fernet-ключ — Fernet его примет как есть
        Fernet(raw_key)
        return raw_key
    except Exception:
        pass

    # Произвольная строка — нормализуем через HKDF до нужного формата
    hkdf = HKDF(
        algorithm=hashes.SHA256(),
        length=32,
        salt=_HKDF_SALT,
        info=b"encrypted-text-field",
    )
    derived = hkdf.derive(raw_key)
    return base64.urlsafe_b64encode(derived)


def _get_crypter() -> MultiFernet:
    """
    Собирает MultiFernet.

    Приоритет:
      1. settings.FIELD_ENCRYPTION_KEYS — список ключей, первый активный,
         остальные только для расшифровки старых записей (ротация).
         Каждый ключ может быть как валидным Fernet-ключом
         (Fernet.generate_key()), так и произвольной строкой —
         см. _normalize_key.
      2. settings.FIELD_ENCRYPTION_KEY — один ключ, для простых случаев.
    """
    keys = getattr(settings, "FIELD_ENCRYPTION_KEYS", None)

    if not keys:
        single_key = getattr(settings, "FIELD_ENCRYPTION_KEY", None)
        if single_key:
            keys = [single_key]

    if not keys:
        raise ImproperlyConfigured(
            "Не задан FIELD_ENCRYPTION_KEY (или FIELD_ENCRYPTION_KEYS) "
            "в настройках Django"
        )

    fernets = [Fernet(_normalize_key(key)) for key in keys if key]

    if not fernets:
        raise ImproperlyConfigured(
            "FIELD_ENCRYPTION_KEYS задан, но пуст"
        )

    return MultiFernet(fernets)


class EncryptedTextField(models.TextField):
    """
    TextField, хранящий данные в БД в зашифрованном виде (base64 token
    Fernet). На уровне Python поле ведёт себя как обычная строка.

    Ограничения:
      - Точечный поиск/фильтрация по значению,
        сортировка, LIKE/ILIKE, индексы по значению — не работают
    """

    description = "Text encrypted with Fernet before storage"

    def get_internal_type(self):
        # Хранится как обычный text-столбец в БД
        return "TextField"

    def get_prep_value(self, value):
        # Вызывается перед записью в БД
        value = super().get_prep_value(value)
        if value is None or value == "":
            return value
        crypter = _get_crypter()
        token = crypter.encrypt(value.encode("utf-8"))
        return token.decode("utf-8")

    def from_db_value(self, value, expression, connection):
        # Вызывается при чтении из БД
        if value is None or value == "":
            return value
        crypter = _get_crypter()
        try:
            plaintext = crypter.decrypt(value.encode("utf-8"))
        except InvalidToken:
            # Значение либо повреждено, либо зашифровано ключом,
            # которого нет в FIELD_ENCRYPTION_KEYS.
            raise InvalidToken(
                "Не удалось расшифровать значение поля "
                f"'{self.name}': ключ не подходит или данные повреждены."
            )
        return plaintext.decode("utf-8")

    def to_python(self, value):
        # Используется, например, в формах/валидации при уже
        # расшифрованном значении
        if isinstance(value, str) or value is None:
            return value
        return str(value)
