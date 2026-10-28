"""Provider credentials, stored-key compatibility, and endpoint boundaries."""

from __future__ import annotations

import base64
import hashlib
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from cryptography.fernet import Fernet

from common.events.memory_enrich_publisher import publish_memory_enrich_request
from common.events.memory_enrich_request import MemoryEnrichRequest
from core_api.services import organization_settings as org
from core_api.services import settings_crypto as crypto

pytestmark = pytest.mark.unit
KEY = base64.urlsafe_b64encode(b"a" * 32).decode()
OTHER_KEY = base64.urlsafe_b64encode(b"b" * 32).decode()
PROVIDER_KEY = "synthetic-provider-credential"


@pytest.fixture
def encryption(monkeypatch):
    monkeypatch.setenv("SETTINGS_ENCRYPTION_KEY", KEY)
    monkeypatch.delenv("SETTINGS_ENCRYPTION_PREVIOUS_KEYS", raising=False)
    monkeypatch.setattr(org.global_settings, "settings_encryption_key", KEY)


async def test_published_credentials_are_ciphertext(encryption, monkeypatch):
    bus = SimpleNamespace(publish=AsyncMock())
    monkeypatch.setattr(
        "common.events.memory_enrich_publisher.get_event_bus", lambda: bus
    )
    await publish_memory_enrich_request(
        memory_id=uuid4(),
        content="A decision",
        tenant_id="tenant-A",
        tenant_config=SimpleNamespace(openai_api_key=PROVIDER_KEY),
    )
    event = bus.publish.await_args.args[1]
    assert PROVIDER_KEY not in event.model_dump_json()
    assert event.payload["openai_api_key"] is None
    token = event.payload["encrypted_api_keys"]["openai_api_key"]
    assert token.startswith("enc:v2:")
    assert crypto.decrypt_api_key(token) == PROVIDER_KEY


@pytest.mark.parametrize("shape", ["legacy", "encrypted"])
def test_worker_accepts_both_rollout_shapes(shape, encryption):
    from core_worker.consumer import _build_tenant_config

    credentials = {"openai_api_key": PROVIDER_KEY}
    if shape == "encrypted":
        credentials = {"encrypted_api_keys": crypto.encrypt_api_keys(credentials)}
    request = MemoryEnrichRequest(
        memory_id=uuid4(),
        tenant_id="tenant-A",
        content="A decision",
        **credentials,
    )
    config = _build_tenant_config(request)
    assert config is not None
    assert config.openai_api_key == PROVIDER_KEY


def test_worker_refuses_undecryptable_credentials(encryption):
    from core_worker.consumer import _build_tenant_config

    request = MemoryEnrichRequest(
        memory_id=uuid4(),
        tenant_id="tenant-A",
        content="A decision",
        enrichment_provider="openai",
        encrypted_api_keys={"openai_api_key": "enc:v1:unreadable"},
    )
    with pytest.raises(ValueError, match="decrypt"):
        _build_tenant_config(request)


def test_new_encryption_refuses_a_passphrase(monkeypatch):
    monkeypatch.setattr(
        org.global_settings, "settings_encryption_key", "short-passphrase"
    )
    with pytest.raises(ValueError, match="Fernet"):
        crypto.encrypt_api_keys({"openai_api_key": PROVIDER_KEY})


def test_versioned_keys_rotate_and_read_old_rows(encryption, monkeypatch):
    old = "enc:v1:" + Fernet(OTHER_KEY.encode()).encrypt(PROVIDER_KEY.encode()).decode()
    monkeypatch.setenv("SETTINGS_ENCRYPTION_PREVIOUS_KEYS", OTHER_KEY)
    assert crypto.decrypt_api_key(old) == PROVIDER_KEY
    rotated = crypto.encrypt_api_keys({"openai_api_key": old})["openai_api_key"]
    assert rotated.startswith("enc:v2:")
    assert crypto.decrypt_api_key(rotated) == PROVIDER_KEY
    monkeypatch.delenv("SETTINGS_ENCRYPTION_PREVIOUS_KEYS")
    assert crypto.decrypt_api_key(rotated) == PROVIDER_KEY


def test_legacy_passphrase_rows_still_decrypt_during_migration(encryption, monkeypatch):
    legacy = "synthetic-old-passphrase"
    derived = base64.urlsafe_b64encode(hashlib.sha256(legacy.encode()).digest())
    token = "enc:v1:" + Fernet(derived).encrypt(PROVIDER_KEY.encode()).decode()
    monkeypatch.setenv("SETTINGS_ENCRYPTION_PREVIOUS_KEYS", derived.decode())
    assert crypto.decrypt_api_key(token) == PROVIDER_KEY
    rotated = crypto.encrypt_api_keys({"openai_api_key": token})["openai_api_key"]
    monkeypatch.delenv("SETTINGS_ENCRYPTION_PREVIOUS_KEYS")
    assert crypto.decrypt_api_key(rotated) == PROVIDER_KEY


@pytest.mark.parametrize("provider", ["openai", "anthropic", "openrouter", "gemini"])
def test_unreadable_tenant_key_does_not_use_operator_key(
    provider, encryption, monkeypatch
):
    name = provider + "_api_key"
    monkeypatch.setattr(org.global_settings, name, "synthetic-operator-key")
    config = org.ResolvedConfig({"api_keys": {name: "enc:v1:unreadable"}})
    with pytest.raises(ValueError, match="decrypt"):
        getattr(config, name)


@pytest.mark.parametrize("section", list(org.PROVIDER_OPTIONS))
async def test_settings_reject_unknown_model_ids(section, monkeypatch):
    storage = SimpleNamespace(
        get_org_settings_versioned=AsyncMock(return_value=({}, 1)),
        update_org_settings=AsyncMock(return_value={"settings": {}}),
    )
    monkeypatch.setattr(org, "get_storage_client", lambda: storage)
    with pytest.raises(ValueError, match="model"):
        await org.update_settings(
            "tenant-A", {section: {"provider": "openai", "model": "gpt-unlisted"}}
        )
    storage.update_org_settings.assert_not_awaited()


@pytest.mark.parametrize("tier", ["registry", "platform"])
def test_sidecar_never_receives_openai_credentials(tier, monkeypatch):
    from collections import OrderedDict

    from common.embedding import _platform, _registry

    # Restore process state as well as environment variables after each tier.
    monkeypatch.setattr(_platform, "_platform_embedding", None)
    monkeypatch.setattr(_platform, "_platform_init_errors", [])
    monkeypatch.setattr(_registry, "_openai_provider_cache", OrderedDict())

    prefix = "OPENAI_EMBEDDING" if tier == "registry" else "PLATFORM_EMBEDDING"
    monkeypatch.setenv(prefix + "_BASE_URL", "https://tei.invalid/v1")
    monkeypatch.setenv(prefix + "_SEND_DIMENSIONS", "false")
    monkeypatch.setenv(prefix + "_MODEL", "BAAI/bge-m3")
    monkeypatch.setenv("OPENAI_API_KEY", "synthetic-operator-key")
    monkeypatch.setenv("PLATFORM_EMBEDDING_API_KEY", "synthetic-platform-key")
    monkeypatch.setenv("PLATFORM_EMBEDDING_PROVIDER", "openai")
    if tier == "registry":
        provider = _registry.get_embedding_provider(
            "openai",
            SimpleNamespace(
                openai_api_key=PROVIDER_KEY, embedding_model="text-embedding-3-large"
            ),
        )
    else:
        _platform.init_platform_embedding()
        provider = _platform.get_platform_embedding()
    assert provider is not None
    assert provider._client.api_key not in (
        PROVIDER_KEY,
        "synthetic-operator-key",
        "synthetic-platform-key",
    )
    assert provider.model == "BAAI/bge-m3"


async def test_display_reports_encryption_and_credential_source(
    encryption, monkeypatch
):
    monkeypatch.setattr(
        org,
        "get_raw_settings",
        AsyncMock(return_value={"api_keys": {"gemini_api_key": PROVIDER_KEY}}),
    )
    displayed = await org.get_settings_for_display("tenant-A")
    assert displayed["provider_credentials"]["encryption_enabled"] is True
    assert displayed["provider_credentials"]["sources"]["gemini"] == "tenant"
    assert PROVIDER_KEY not in json.dumps(displayed)


@pytest.mark.parametrize(
    "section", ["enrichment", "recall", "entity_extraction", "fallback_llm"]
)
def test_operator_key_uses_operator_model(section, monkeypatch):
    monkeypatch.setattr(org.global_settings, "openai_api_key", "synthetic-operator-key")
    config = org.ResolvedConfig(
        {section: {"provider": "openai", "model": "gpt-4o-mini"}}
    )
    assert getattr(config, section + "_model") != "gpt-4o-mini"


async def test_legacy_retry_confirms_encrypted_replacement_before_ack(encryption):
    from common.events.base import Event
    from common.events.pubsub import PubSubEventBus

    event = Event(
        event_type="caura.memory.enrich-requested",
        payload={"openai_api_key": PROVIDER_KEY},
    )
    bus = PubSubEventBus(
        project_id="synthetic-project", subscription_prefix="core-worker"
    )
    bus.publish_confirmed = AsyncMock()
    assert await bus._replace_legacy_enrich_retry(event)
    replacement = bus.publish_confirmed.await_args.args[1]
    assert PROVIDER_KEY not in replacement.model_dump_json()
    token = replacement.payload["encrypted_api_keys"]["openai_api_key"]
    assert crypto.decrypt_api_key(token) == PROVIDER_KEY
    bus.publish_confirmed.side_effect = RuntimeError("synthetic transport refusal")
    with pytest.raises(RuntimeError, match="transport refusal"):
        await bus._replace_legacy_enrich_retry(event)


async def test_save_response_keeps_readonly_credential_status(encryption, monkeypatch):
    stored = crypto.encrypt_api_keys({"gemini_api_key": PROVIDER_KEY})
    storage = SimpleNamespace(
        update_org_settings=AsyncMock(
            return_value={"settings": {"api_keys": stored}, "changed": False}
        )
    )
    monkeypatch.setattr(org, "get_storage_client", lambda: storage)
    displayed = await org.update_settings(
        "tenant-A", {"provider_credentials": {"encryption_enabled": False}}
    )
    assert displayed["provider_credentials"]["encryption_enabled"] is True
    assert displayed["provider_credentials"]["sources"]["gemini"] == "tenant"
    assert "provider_credentials" not in storage.update_org_settings.await_args.args[1]


def test_versioned_rotation_uses_key_id_and_keeps_previous_reader(
    encryption, monkeypatch
):
    old = crypto.encrypt_api_keys({"openai_api_key": PROVIDER_KEY})["openai_api_key"]
    monkeypatch.setattr(org.global_settings, "settings_encryption_key", OTHER_KEY)
    monkeypatch.setenv("SETTINGS_ENCRYPTION_PREVIOUS_KEYS", KEY)
    assert crypto.decrypt_api_key(old) == PROVIDER_KEY
    rotated = crypto.encrypt_api_keys({"openai_api_key": old})["openai_api_key"]
    assert rotated.split(":")[2] != old.split(":")[2]
    monkeypatch.delenv("SETTINGS_ENCRYPTION_PREVIOUS_KEYS")
    assert crypto.decrypt_api_key(rotated) == PROVIDER_KEY
    with pytest.raises(ValueError, match="decrypt"):
        crypto.decrypt_api_key(old)


def test_provider_resolution_never_falls_back_on_decryption_failure(
    encryption, monkeypatch
):
    from unittest.mock import Mock

    from common.llm import registry

    fallback = Mock()
    monkeypatch.setattr(registry, "get_platform_llm", fallback)
    config = org.ResolvedConfig({"api_keys": {"openai_api_key": "enc:v1:unreadable"}})
    with pytest.raises(ValueError, match="decrypt"):
        registry.get_llm_provider("openai", config)
    fallback.assert_not_called()


@pytest.mark.parametrize(
    "payload",
    [
        {"agent_digest": {"model": "gpt-unlisted"}},
        {"governance": {"non_business": {"pregate": {"model": "gpt-unlisted"}}}},
        {"enrichment": {"provider": ["openai"]}},
    ],
)
def test_other_model_controls_are_validated(payload):
    with pytest.raises(ValueError):
        org._validate_provider_models(payload)


def test_events_import_does_not_require_encryption_dependency():
    import subprocess
    import sys
    from pathlib import Path

    code = """
import builtins
original = builtins.__import__
def checked(name, *args, **kwargs):
    if name == "cryptography" or name.startswith("cryptography."):
        raise ModuleNotFoundError("cryptography is not installed in the storage image")
    return original(name, *args, **kwargs)
builtins.__import__ = checked
import common.events
"""
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=Path(__file__).resolve().parents[1],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr


async def test_invalid_reader_preserves_row_and_refuses_provider(
    encryption, monkeypatch
):
    token = crypto.encrypt_api_keys({"openai_api_key": PROVIDER_KEY})["openai_api_key"]
    stored = {"api_keys": {"openai_api_key": token}}
    monkeypatch.setenv("SETTINGS_ENCRYPTION_PREVIOUS_KEYS", "synthetic-invalid-reader")
    assert await org._encrypt_legacy_api_keys("tenant-A", stored) == stored
    assert crypto.encryption_enabled() is False
    with pytest.raises(ValueError, match="decrypt"):
        _ = org.ResolvedConfig(stored).openai_api_key


@pytest.mark.parametrize(
    "legacy", [{"provider": "vertex"}, {"provider": "openai", "model": "gpt-4"}]
)
async def test_unchanged_legacy_model_settings_can_be_saved(legacy, monkeypatch):
    stored = {"enrichment": legacy}
    storage = SimpleNamespace(
        get_org_settings_versioned=AsyncMock(return_value=(stored, 1)),
        update_org_settings=AsyncMock(
            return_value={"settings": stored, "changed": False}
        ),
    )
    monkeypatch.setattr(org, "get_storage_client", lambda: storage)
    await org.update_settings("tenant-A", stored)
    storage.update_org_settings.assert_awaited_once()


def test_digest_model_controls_are_server_managed():
    with pytest.raises(ValueError, match="server-managed"):
        org._validate_provider_models(
            {
                "agent_digest": {
                    "provider": "anthropic",
                    "model": "claude-haiku-4-5-20251001",
                }
            }
        )


async def test_legacy_model_exception_does_not_allow_a_new_unlisted_model(monkeypatch):
    storage = SimpleNamespace(
        get_org_settings_versioned=AsyncMock(
            return_value=({"enrichment": {"provider": "openai", "model": "gpt-4"}}, 1)
        ),
        update_org_settings=AsyncMock(),
    )
    monkeypatch.setattr(org, "get_storage_client", lambda: storage)
    with pytest.raises(ValueError, match="supported model"):
        await org.update_settings(
            "tenant-A", {"enrichment": {"provider": "openai", "model": "gpt-unlisted"}}
        )
    storage.update_org_settings.assert_not_awaited()


async def test_unreadable_key_publishes_durable_failure_without_fallback(
    encryption, monkeypatch, caplog
):
    from core_worker.consumer import _build_tenant_config

    bus = SimpleNamespace(publish=AsyncMock())
    monkeypatch.setattr(
        "common.events.memory_enrich_publisher.get_event_bus", lambda: bus
    )
    config = org.ResolvedConfig(
        {
            "enrichment": {"provider": "openai"},
            "api_keys": {"openai_api_key": "enc:v1:synthetic-invalid-token"},
        }
    )
    await publish_memory_enrich_request(
        memory_id=uuid4(),
        content="A decision",
        tenant_id="tenant-A",
        tenant_config=config,
    )
    event = bus.publish.await_args.args[1]
    request = MemoryEnrichRequest(**event.payload)
    assert request.credential_error is True
    assert request.encrypted_api_keys == {}
    assert "synthetic-invalid-token" not in event.model_dump_json() + caplog.text
    assert "tenant-A" in caplog.text
    with pytest.raises(ValueError, match="decrypt"):
        _build_tenant_config(request)
