"""Provider keys saved before encryption are encrypted when first loaded (M-99).

``PUT /settings`` has encrypted ``api_keys`` since caura PR #1769, but a key
saved before then stays plaintext until its tenant saves again. core-api swaps
it for its ciphertext the first time it loads that tenant's settings, through
storage's compare-and-swap, which replaces a key only while it still holds the
plaintext that was read. Without a key configured (dev, standalone) nothing
changes, as on the save path. ``PUT /settings`` also refuses a provider key that
is not a string: only strings are encrypted, and it could not work as a key.
"""

from __future__ import annotations

import pytest
from cryptography.fernet import Fernet

from core_api.clients.storage_client import get_storage_client
from core_api.config import settings
from core_api.services import organization_settings
from core_api.services.settings_crypto import PREFIX, decrypt_api_key
from tests.conftest import new_tenant_id

pytestmark = pytest.mark.asyncio


async def _seeded(api_keys: dict) -> str:
    """An org holding ``api_keys`` as stored before encryption: written straight
    to storage, past the save path that encrypts."""
    org = new_tenant_id()
    await get_storage_client().update_org_settings(
        org, {"api_keys": api_keys}, changed_by="seed"
    )
    return org


async def _stored(org: str) -> dict:
    return (await get_storage_client().get_org_settings(org)).get("api_keys", {})


async def test_a_legacy_plaintext_key_is_encrypted_when_first_loaded(monkeypatch):
    org = await _seeded({"openai_api_key": "sk-legacy", "anthropic_api_key": ""})
    key = Fernet.generate_key().decode()
    monkeypatch.setattr(settings, "settings_encryption_key", key)

    loaded = await organization_settings.get_raw_settings(org)

    stored = await _stored(org)
    assert stored["openai_api_key"].startswith(PREFIX)
    assert decrypt_api_key(stored["openai_api_key"]) == "sk-legacy"
    assert stored["anthropic_api_key"] == ""
    assert loaded["api_keys"] == stored


async def test_a_failed_swap_still_loads_the_settings(monkeypatch):
    org = await _seeded({"openai_api_key": "sk-legacy"})
    key = Fernet.generate_key().decode()
    monkeypatch.setattr(settings, "settings_encryption_key", key)

    async def _storage_down(*_args, **_kwargs):
        raise RuntimeError("storage unavailable")

    monkeypatch.setattr(get_storage_client(), "encrypt_org_api_keys", _storage_down)

    loaded = await organization_settings.get_raw_settings(org)

    assert loaded["api_keys"] == {"openai_api_key": "sk-legacy"}


async def test_without_an_encryption_key_nothing_changes(monkeypatch):
    org = await _seeded({"openai_api_key": "sk-legacy"})
    monkeypatch.setattr(settings, "settings_encryption_key", "")

    await organization_settings.get_raw_settings(org)

    assert (await _stored(org))["openai_api_key"] == "sk-legacy"


@pytest.mark.parametrize(
    "value",
    [["sk-in-a-list"], {"key": "sk-in-an-object"}, 12345],
    ids=["list", "object", "number"],
)
async def test_a_provider_key_that_is_not_a_string_is_refused(value):
    org = new_tenant_id()

    with pytest.raises(ValueError, match=r"api_keys\.openai_api_key"):
        await organization_settings.update_settings(
            org, {"api_keys": {"openai_api_key": value}}
        )

    assert await _stored(org) == {}
