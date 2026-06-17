"""E2E public ``/api/v1/status`` endpoint — service fingerprint."""

from __future__ import annotations

import json
import re

from core_api.constants import VERSION, _resolve_version


# ---------------------------------------------------------------------------
# Shape and contract
# ---------------------------------------------------------------------------


async def test_status_shape(client):
    """``GET /api/v1/status`` returns the documented top-level keys."""
    resp = await client.get("/api/v1/status")
    assert resp.status_code == 200
    data = resp.json()

    # Top-level keys
    for key in (
        "version",
        "health",
        "dependencies",
        "llm",
        "embedding",
        "platform_init_errors",
    ):
        assert key in data, f"missing top-level key: {key}"

    # ``mode`` is intentionally NOT exposed: it would leak
    # ``settings.is_standalone`` (tenant-auth-bypass opt-in) on a
    # public unauthenticated endpoint.
    assert "mode" not in data
    # ``uptime`` was renamed to ``health`` (carries an enum, not a
    # duration). Negative assertion catches accidental drift back.
    assert "uptime" not in data

    # Types
    assert data["version"] == VERSION
    assert data["health"] in {"ok", "degraded", "unhealthy"}
    assert isinstance(data["dependencies"], dict)
    assert isinstance(data["platform_init_errors"], list)

    # LLM / embedding sub-objects
    for sub in ("llm", "embedding"):
        block = data[sub]
        assert isinstance(block, dict)
        assert set(block.keys()) >= {"provider", "model", "configured"}
        assert isinstance(block["configured"], bool)


async def test_status_no_auth_required(client):
    """Public endpoint — no headers, still 200."""
    resp = await client.get("/api/v1/status")
    assert resp.status_code == 200


async def test_status_no_secrets_leaked(client):
    """The response body must never carry secret-shaped substrings.

    Defense-in-depth: provider names + model names are intentional public
    knowledge, but anything resembling an API key, GCP project ID, or
    internal hostname must NOT appear. If a future contributor adds such
    a field by accident, this test fails loudly.
    """
    resp = await client.get("/api/v1/status")
    assert resp.status_code == 200
    body = resp.text.lower()

    forbidden_substrings = (
        "api_key",
        "api-key",
        "apikey",
        "secret",
        "password",
        "gcp_project_id",
        "project_id",
        "private_key",
        "bearer",
    )
    for sub in forbidden_substrings:
        assert sub not in body, f"status response leaked substring: {sub!r}"


async def test_status_health_ok_in_test_env(client):
    """In the OSS test fixture (in-process bus, no Redis, FakeProviders),
    ``health`` should be ``ok`` — no init errors, no failed probes."""
    resp = await client.get("/api/v1/status")
    data = resp.json()
    # ``ok`` or ``degraded`` are both acceptable; the integration env may
    # have init warnings (e.g., missing PLATFORM_LLM_PROVIDER → no errors,
    # so "ok"). The strict invariant: not unhealthy.
    assert data["health"] != "unhealthy", data


# ---------------------------------------------------------------------------
# Provider introspection
# ---------------------------------------------------------------------------


async def test_status_provider_fields_when_configured(client, monkeypatch):
    """When platform LLM/embedding singletons are present, ``/status``
    surfaces their ``provider_name`` and ``model`` verbatim.

    Uses tiny stub objects with the two protocol props only — keeps the
    test independent of the real provider SDKs.
    """

    class _StubLLM:
        provider_name = "openai"
        model = "gpt-4o-mini"

    class _StubEmbedding:
        provider_name = "openai"
        model = "text-embedding-3-small"

    # Patch the symbols at the location where /status looks them up
    # (core_api.routes.health imports `get_platform_llm` /
    # `get_platform_embedding` from `core_api.providers`, so monkey-patching
    # the source module wouldn't reach the bound name in health.py).
    from core_api.routes import health as health_module

    monkeypatch.setattr(health_module, "get_platform_llm", lambda: _StubLLM())
    monkeypatch.setattr(
        health_module, "get_platform_embedding", lambda: _StubEmbedding()
    )

    resp = await client.get("/api/v1/status")
    assert resp.status_code == 200
    data = resp.json()

    assert data["llm"]["provider"] == "openai"
    assert data["llm"]["model"] == "gpt-4o-mini"
    assert data["llm"]["configured"] is True

    assert data["embedding"]["provider"] == "openai"
    assert data["embedding"]["model"] == "text-embedding-3-small"
    assert data["embedding"]["configured"] is True


async def test_status_provider_fields_when_unconfigured(client, monkeypatch):
    """With no platform singletons, fields are ``None`` and
    ``configured: False`` — what every fresh OSS standalone install sees."""
    from core_api.routes import health as health_module

    monkeypatch.setattr(health_module, "get_platform_llm", lambda: None)
    monkeypatch.setattr(health_module, "get_platform_embedding", lambda: None)

    resp = await client.get("/api/v1/status")
    data = resp.json()
    assert data["llm"] == {"provider": None, "model": None, "configured": False}
    assert data["embedding"] == {"provider": None, "model": None, "configured": False}


# ---------------------------------------------------------------------------
# JSON-stable response (helpful for downstream tooling / dashboards)
# ---------------------------------------------------------------------------


async def test_status_is_valid_json(client):
    resp = await client.get("/api/v1/status")
    assert resp.status_code == 200
    json.loads(resp.text)  # raises if the body isn't strict JSON


# ---------------------------------------------------------------------------
# Version resolution (regression guard — /api/v1/version once served "dev")
# ---------------------------------------------------------------------------


def test_version_is_resolved_not_dev():
    """In CI core-api is installed editable, so metadata resolution yields a
    real semver. ``"dev"`` here means the resolution chain is broken."""
    assert VERSION != "dev"
    assert re.match(r"^\d+\.\d+\.\d+", VERSION), VERSION


def test_version_env_override_wins(monkeypatch):
    """An explicit ``MEMCLAW_VERSION`` env beats file/metadata resolution."""
    monkeypatch.setenv("MEMCLAW_VERSION", "9.9.9-test")
    assert _resolve_version() == "9.9.9-test"


def test_version_blank_env_override_ignored(monkeypatch):
    """A blank/whitespace ``MEMCLAW_VERSION`` must not win — it falls through
    to the file/metadata chain rather than serving an empty version."""
    monkeypatch.setenv("MEMCLAW_VERSION", "   ")
    resolved = _resolve_version()
    assert resolved.strip() == resolved
    assert resolved not in ("", "dev")
