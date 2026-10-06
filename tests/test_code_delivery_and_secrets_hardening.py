"""Hardening around code delivery to nodes and stored provider keys.

* ``POST /fleet/commands``: a ``deploy``/``update_plugin`` with custom
  ``source`` runs that code on the node, and commands are not yet signed. Only
  an org admin may queue custom source, and ``env_vars`` may not redirect the
  node's key, change it, or switch off its safety settings.
* Installer scripts embed ``api_url`` and send the installer's key to it. A
  caller-supplied value is accepted only for this server's own origin or an
  operator-allowlisted one.
* Tenant provider keys are encrypted at rest, and the settings audit diff
  never records their values.
"""

from __future__ import annotations

import pytest
from fastapi import HTTPException
from starlette.requests import Request

from common.organization_settings_merge import diff_settings
from core_api.auth import AuthContext
from core_api.config import settings
from core_api.routes import fleet, plugin
from core_api.services import settings_crypto

pytestmark = pytest.mark.unit

NODE = "11111111-2222-3333-4444-555555555555"


def _cmd(command: str, payload: dict | None) -> fleet.CommandIn:
    return fleet.CommandIn(node_id=NODE, command=command, payload=payload)


TENANT_KEY = AuthContext(tenant_id="t1")
ORG_ADMIN = AuthContext(tenant_id="t1", org_role="admin")
OPERATOR = AuthContext(tenant_id=None, is_admin=True)


# ── fleet code delivery ─────────────────────────────────────────────


@pytest.mark.parametrize("command", ["deploy", "update_plugin"])
def test_tenant_key_cannot_queue_custom_source(command):
    with pytest.raises(HTTPException) as exc:
        fleet._enforce_code_delivery_policy(
            _cmd(command, {"source": "evil()"}), TENANT_KEY
        )
    assert exc.value.status_code == 403


@pytest.mark.parametrize("auth", [ORG_ADMIN, OPERATOR], ids=["org-admin", "operator"])
def test_org_admin_may_queue_custom_source(auth):
    fleet._enforce_code_delivery_policy(_cmd("deploy", {"source": "ok()"}), auth)


def test_tenant_key_may_still_trigger_a_canonical_redeploy():
    """No ``source``: the node fetches the server's own plugin source."""
    fleet._enforce_code_delivery_policy(
        _cmd("deploy", {"target_version": "2.24.0"}), TENANT_KEY
    )
    fleet._enforce_code_delivery_policy(_cmd("deploy", None), TENANT_KEY)


@pytest.mark.parametrize(
    "key",
    [
        "CAURA_API_URL",
        "CAURA_API_KEY",
        "CAURA_TENANT_ID",
        "CAURA_ALLOW_INSECURE_HTTP",
        "CAURA_REQUIRE_SIGNED_COMMANDS",
        "MEMCLAW_API_URL",  # legacy-name-ok: rule 3 dual-read alias
        "caura_api_key",
    ],
)
@pytest.mark.parametrize("auth", [TENANT_KEY, ORG_ADMIN], ids=["tenant", "org-admin"])
def test_env_vars_cannot_redirect_or_weaken_the_node(key, auth):
    with pytest.raises(HTTPException) as exc:
        fleet._enforce_code_delivery_policy(
            _cmd("update_plugin", {"env_vars": {key: "x"}}), auth
        )
    assert exc.value.status_code == 422
    assert key in exc.value.detail


def test_ordinary_env_vars_still_pass():
    fleet._enforce_code_delivery_policy(
        _cmd("update_plugin", {"env_vars": {"CAURA_RECALL_POLICY": "auto"}}), TENANT_KEY
    )


def test_other_commands_are_untouched():
    fleet._enforce_code_delivery_policy(
        _cmd("ping", {"source": "irrelevant"}), TENANT_KEY
    )


# ── installer api_url ───────────────────────────────────────────────


def _request(host: str = "caura.ai", proto: str = "https") -> Request:
    return Request(
        {
            "type": "http",
            "method": "GET",
            "scheme": "http",
            "path": "/api/v1/install-plugin",
            "query_string": b"",
            "server": ("core-api", 8000),
            "headers": [
                (b"host", b"core-api:8000"),
                (b"x-forwarded-host", host.encode()),
                (b"x-forwarded-proto", proto.encode()),
            ],
        }
    )


def test_no_api_url_uses_the_serving_origin():
    assert plugin._resolve_installer_api_url(_request(), None) == "https://caura.ai"


@pytest.mark.parametrize(
    "value", ["https://caura.ai", "https://caura.ai/", "HTTPS://CAURA.AI"]
)
def test_the_serving_origin_may_be_named_explicitly(value):
    assert (
        plugin._resolve_installer_api_url(_request(), value).lower()
        == "https://caura.ai"
    )


@pytest.mark.parametrize(
    "value",
    [
        "https://attacker.example",
        "http://caura.ai",  # downgrade to plaintext
        "https://caura.ai.attacker.example",
        "https://caura.ai@attacker.example",
        "file:///etc/passwd",
        "javascript:alert(1)",
    ],
)
def test_any_other_api_url_is_refused(value, monkeypatch):
    monkeypatch.setattr(settings, "installer_allowed_api_urls", "")
    with pytest.raises(HTTPException) as exc:
        plugin._resolve_installer_api_url(_request(), value)
    assert exc.value.status_code == 400


def test_operator_allowlist_admits_a_proxied_public_origin(monkeypatch):
    monkeypatch.setattr(
        settings,
        "installer_allowed_api_urls",
        "https://memory.corp.example, https://b.example",
    )
    assert (
        plugin._resolve_installer_api_url(
            _request(host="core-api:8000", proto="http"), "https://memory.corp.example"
        )
        == "https://memory.corp.example"
    )


# ── provider keys at rest ───────────────────────────────────────────


def test_keys_are_encrypted_and_round_trip(monkeypatch):
    from cryptography.fernet import Fernet

    monkeypatch.setattr(
        settings, "settings_encryption_key", Fernet.generate_key().decode()
    )
    stored = settings_crypto.encrypt_api_keys(
        {"openai_api_key": "sk-real", "gemini_api_key": ""}
    )
    assert stored["openai_api_key"].startswith(settings_crypto.PREFIX)
    assert "sk-real" not in stored["openai_api_key"]
    assert stored["gemini_api_key"] == ""
    assert settings_crypto.decrypt_api_key(stored["openai_api_key"]) == "sk-real"


def test_a_non_fernet_key_string_still_works(monkeypatch):
    monkeypatch.setattr(
        settings, "settings_encryption_key", "just-a-long-random-operator-string"
    )
    settings_crypto._fernet_for.cache_clear()
    stored = settings_crypto.encrypt_api_keys({"openai_api_key": "sk-real"})
    assert settings_crypto.decrypt_api_key(stored["openai_api_key"]) == "sk-real"


def test_legacy_plaintext_values_still_read(monkeypatch):
    monkeypatch.setattr(settings, "settings_encryption_key", "k" * 40)
    assert settings_crypto.decrypt_api_key("sk-legacy-plain") == "sk-legacy-plain"
    assert settings_crypto.decrypt_api_key(None) is None


def test_no_key_configured_keeps_values_as_submitted(monkeypatch):
    monkeypatch.setattr(settings, "settings_encryption_key", "")
    assert settings_crypto.encrypt_api_keys({"openai_api_key": "sk-x"}) == {
        "openai_api_key": "sk-x"
    }


def test_undecryptable_value_never_returns_ciphertext(monkeypatch):
    from cryptography.fernet import Fernet

    monkeypatch.setattr(
        settings, "settings_encryption_key", Fernet.generate_key().decode()
    )
    stored = settings_crypto.encrypt_api_keys({"openai_api_key": "sk-real"})[
        "openai_api_key"
    ]
    monkeypatch.setattr(
        settings, "settings_encryption_key", Fernet.generate_key().decode()
    )
    assert settings_crypto.decrypt_api_key(stored) is None


def test_audit_diff_never_records_provider_key_values():
    diff = diff_settings(
        {
            "api_keys": {"openai_api_key": "sk-old"},
            "enrichment": {"provider": "openai"},
        },
        {
            "api_keys": {"openai_api_key": "sk-new", "gemini_api_key": "g-1"},
            "enrichment": {"provider": "gemini"},
        },
    )
    assert diff["api_keys.openai_api_key"] == ["****", "****"]
    assert diff["api_keys.gemini_api_key"] == [None, "****"]
    assert diff["enrichment.provider"] == ["openai", "gemini"]
    assert "sk-" not in repr(diff)


def test_audit_diff_masks_any_credential_named_setting():
    diff = diff_settings({}, {"deployment_id": "d-1", "deployment_token": "77923a4d"})
    assert diff["deployment_token"] == [None, "****"]
    assert diff["deployment_id"] == [None, "d-1"]
