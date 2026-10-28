"""Settings encryption with explicit legacy readers and fail-closed decryption."""

from __future__ import annotations

import os
from functools import lru_cache
from typing import TypeGuard

from common.settings_crypto import PREFIX as PREFIX
from common.settings_crypto import ProviderKeyDecryptionError, SettingsCipher


@lru_cache(maxsize=4)
def _fernet_for(key: str, previous_keys: str = "") -> SettingsCipher:
    return SettingsCipher(key, previous_keys)


def _fernet() -> SettingsCipher | None:
    from core_api.config import settings

    key = settings.settings_encryption_key or ""
    return (
        _fernet_for(
            key,
            os.environ.get("SETTINGS_ENCRYPTION_PREVIOUS_KEYS", ""),
        )
        if key
        else None
    )


def encryption_enabled() -> bool:
    try:
        return _fernet() is not None
    except ValueError:
        return False


def needs_encryption(value: object) -> TypeGuard[str]:
    if not isinstance(value, str) or not value or not encryption_enabled():
        return False
    cipher = _fernet()
    return cipher is not None and not value.startswith(PREFIX + cipher.key_id + ":")


def encrypt_api_keys(section: dict) -> dict:
    cipher = _fernet()
    if cipher is None:
        return section
    return {
        name: cipher.encrypt(value) if isinstance(value, str) and value else value
        for name, value in section.items()
    }


def decrypt_api_key(value: str | None) -> str | None:
    if not isinstance(value, str) or not value.startswith("enc:"):
        return value
    try:
        cipher = _fernet()
    except ValueError:
        raise ProviderKeyDecryptionError("Configured provider key could not be decrypted") from None
    if cipher is None:
        raise ProviderKeyDecryptionError("Configured provider key could not be decrypted")
    return cipher.decrypt(value)
