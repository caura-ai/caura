"""Shared, versioned provider-key encryption for API and worker processes."""

from __future__ import annotations

import hashlib
import logging
import os

from cryptography.fernet import Fernet, InvalidToken, MultiFernet

logger = logging.getLogger(__name__)
PREFIX = "enc:v2:"
LEGACY_PREFIX = "enc:v1:"
PROVIDER_KEY_FIELDS = (
    "openai_api_key",
    "anthropic_api_key",
    "openrouter_api_key",
    "gemini_api_key",
)


class ProviderKeyDecryptionError(ValueError):
    """A configured tenant credential is unavailable; never use another account."""


def valid_settings_key(key: str) -> bool:
    try:
        Fernet(key.encode("ascii"))
    except (ValueError, TypeError, UnicodeError, AttributeError):
        return False
    return True


class SettingsCipher:
    """Write with one real Fernet key; retain explicitly configured read keys.

    Key IDs are stable fingerprints of the master key, never of tenant keys.
    Legacy passphrase installations provision their existing derived Fernet key
    as an explicit previous read key before deploying this reader.
    """

    def __init__(self, key: str, previous_keys: str = ""):
        if not valid_settings_key(key):
            raise ValueError("SETTINGS_ENCRYPTION_KEY must be a valid Fernet key")
        self.key_id = hashlib.sha256(key.encode()).hexdigest()[:16]
        self.active = Fernet(key.encode())
        keys = [key, *(k.strip() for k in previous_keys.split(",") if k.strip())]
        self.readers: dict[str, Fernet] = {}
        for read_key in keys:
            if not valid_settings_key(read_key):
                raise ValueError(
                    "SETTINGS_ENCRYPTION_PREVIOUS_KEYS must contain Fernet keys"
                )
            self.readers[hashlib.sha256(read_key.encode()).hexdigest()[:16]] = Fernet(
                read_key.encode()
            )
        self.legacy = MultiFernet(list(self.readers.values()))

    def encrypt(self, value: str) -> str:
        if value.startswith("enc:"):
            value = self.decrypt(value)
        return PREFIX + self.key_id + ":" + self.active.encrypt(value.encode()).decode()

    def decrypt(self, value: str) -> str:
        try:
            if value.startswith(LEGACY_PREFIX):
                return self.legacy.decrypt(
                    value[len(LEGACY_PREFIX) :].encode()
                ).decode()
            if value.startswith(PREFIX):
                kid, token = value[len(PREFIX) :].split(":", 1)
                return self.readers[kid].decrypt(token.encode()).decode()
            if not value.startswith("enc:"):
                return value
        except (InvalidToken, ValueError, KeyError, UnicodeError):
            pass
        logger.error(
            "Configured provider key could not be decrypted; refusing provider fallback"
        )
        raise ProviderKeyDecryptionError(
            "Configured provider key could not be decrypted"
        )


def cipher_from_environment() -> SettingsCipher:
    return SettingsCipher(
        os.environ.get("SETTINGS_ENCRYPTION_KEY", ""),
        os.environ.get("SETTINGS_ENCRYPTION_PREVIOUS_KEYS", ""),
    )
