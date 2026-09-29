import base64

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from django.db import models

from cryptography.fernet import Fernet, MultiFernet, InvalidToken
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF


_HKDF_SALT = b"django-encrypted-fields-v1"


def _normalize_key(raw_key) -> bytes:
    if isinstance(raw_key, str):
        raw_key = raw_key.encode("utf-8")

    try:
        Fernet(raw_key)
        return raw_key
    except Exception:
        pass

    hkdf = HKDF(
        algorithm=hashes.SHA256(),
        length=32,
        salt=_HKDF_SALT,
        info=b"encrypted-text-field",
    )
    derived = hkdf.derive(raw_key)
    return base64.urlsafe_b64encode(derived)


def _get_crypter() -> MultiFernet:
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
    description = "Text encrypted with Fernet before storage"

    def get_internal_type(self):
        return "TextField"

    def get_prep_value(self, value):
        value = super().get_prep_value(value)
        if value is None or value == "":
            return value
        crypter = _get_crypter()
        token = crypter.encrypt(value.encode("utf-8"))
        return token.decode("utf-8")

    def from_db_value(self, value, expression, connection):
        if value is None or value == "":
            return value
        crypter = _get_crypter()
        try:
            plaintext = crypter.decrypt(value.encode("utf-8"))
        except InvalidToken:
            raise InvalidToken(
                "Не удалось расшифровать значение поля "
                f"'{self.name}': ключ не подходит или данные повреждены."
            )
        return plaintext.decode("utf-8")

    def to_python(self, value):
        if isinstance(value, str) or value is None:
            return value
        return str(value)
