"""Encryption at rest for tenant provider keys (``settings.api_keys.*``).

Tenant OpenAI/Anthropic/OpenRouter/Gemini keys were stored in the
``organization_settings`` JSONB exactly as submitted, although
``SETTINGS_ENCRYPTION_KEY`` has been required in production for this purpose.
They are now written as ``enc:v1:<Fernet token>`` and decrypted only where a
provider call needs them (``ResolvedConfig``).

* No key configured (dev, standalone): values are stored as before, so a
  local install keeps working without new setup.
* Values written before this change have no prefix and are read as-is; they
  are encrypted the next time the tenant saves them.
* ``SETTINGS_ENCRYPTION_KEY`` is normally a Fernet key. Any other non-empty
  string is accepted by deriving a Fernet key from its SHA-256, so an
  operator who set a random string does not lose settings writes.
"""

from __future__ import annotations

import base64
import hashlib
import logging
from functools import lru_cache

from cryptography.fernet import Fernet, InvalidToken

logger = logging.getLogger(__name__)

PREFIX = "enc:v1:"


@lru_cache(maxsize=4)
def _fernet_for(key: str) -> Fernet:
    try:
        return Fernet(key.encode())
    except (ValueError, TypeError):
        derived = base64.urlsafe_b64encode(hashlib.sha256(key.encode()).digest())
        return Fernet(derived)


def _fernet() -> Fernet | None:
    from core_api.config import settings

    key = (settings.settings_encryption_key or "").strip()
    return _fernet_for(key) if key else None


def encrypt_api_keys(section: dict) -> dict:
    """``api_keys`` section with every non-empty string value encrypted."""
    f = _fernet()
    if f is None:
        return section
    out: dict = {}
    for name, value in section.items():
        if isinstance(value, str) and value and not value.startswith(PREFIX):
            out[name] = PREFIX + f.encrypt(value.encode()).decode()
        else:
            out[name] = value
    return out


def decrypt_api_key(value: str | None) -> str | None:
    """Plaintext of a stored key; unprefixed (legacy) values pass through.

    A value that cannot be decrypted (key rotated, corrupted row) yields
    ``None`` and an error log, so the caller falls back to the operator key
    exactly as for an unset tenant key, and nobody receives ciphertext.
    """
    if not isinstance(value, str) or not value.startswith(PREFIX):
        return value
    f = _fernet()
    if f is None:
        logger.error("stored provider key is encrypted but SETTINGS_ENCRYPTION_KEY is unset")
        return None
    try:
        return f.decrypt(value[len(PREFIX) :].encode()).decode()
    except InvalidToken:
        logger.error("stored provider key could not be decrypted with SETTINGS_ENCRYPTION_KEY")
        return None
