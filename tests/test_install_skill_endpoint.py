"""Tests for ``/api/v1/install-skill``.

Covers two fixes that landed together:

1. Auto-derive ``MEMCLAW_API_URL`` from the request Host (and
   ``X-Forwarded-Proto`` when proxied) so ``curl
   https://memclaw.dev/api/v1/install-skill | bash`` yields a script that
   keeps fetching from memclaw.dev — not from ``http://localhost:8000``
   which was the old default.
2. Forward the caller's ``X-API-Key`` into the generated script so its
   internal curls carry auth. Required on edge-gated deploys (memclaw.dev
   nginx rejects unauthenticated calls on every path).
"""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

import pytest

from core_api.routes.plugin import router

pytestmark = pytest.mark.unit


def _client() -> TestClient:
    app = FastAPI()
    app.include_router(router, prefix="/api/v1")
    return TestClient(app)


def test_api_url_auto_derived_from_request_host():
    """Caller hits the endpoint with no ``?api_url=…`` → installer URL is
    the scheme+host the caller actually used (not the old localhost default)."""
    client = _client()
    # TestClient default Host is ``testserver``; scheme is http.
    resp = client.get("/api/v1/install-skill?agent=claude-code")
    assert resp.status_code == 200
    assert "MEMCLAW_API_URL=http://testserver" in resp.text
    assert "http://localhost:8000" not in resp.text


def test_api_url_override_via_query_param():
    """Explicit ``?api_url=`` wins over the auto-derived default."""
    client = _client()
    resp = client.get("/api/v1/install-skill?api_url=https://explicit.example.com")
    assert resp.status_code == 200
    assert "MEMCLAW_API_URL=https://explicit.example.com" in resp.text


def test_x_forwarded_proto_and_host_preferred_over_raw():
    """Behind a proxy, ``X-Forwarded-Proto`` / ``X-Forwarded-Host`` should
    be treated as authoritative — otherwise a user's script generated
    against ``https://memclaw.dev`` would read as ``http://internal-ip``."""
    client = _client()
    resp = client.get(
        "/api/v1/install-skill?agent=both",
        headers={
            "X-Forwarded-Proto": "https",
            "X-Forwarded-Host": "memclaw.dev",
        },
    )
    assert resp.status_code == 200
    assert "MEMCLAW_API_URL=https://memclaw.dev" in resp.text
    # The raw ``Host: testserver`` header must not leak through.
    assert "testserver" not in resp.text


def test_api_key_header_forwarded_into_script():
    """Caller's ``X-API-Key`` is baked into the script, and the internal
    curl calls carry ``-H "X-API-Key: $MEMCLAW_API_KEY"``."""
    client = _client()
    resp = client.get(
        "/api/v1/install-skill?agent=claude-code",
        headers={"X-API-Key": "mc_test_key_abc123"},
    )
    assert resp.status_code == 200
    script = resp.text
    # ``shlex.quote`` only wraps when the value has special chars. An
    # ``mc_``-style key is shell-safe and emitted unquoted, which is fine.
    assert "MEMCLAW_API_KEY=mc_test_key_abc123" in script
    assert '-H "X-API-Key: $MEMCLAW_API_KEY"' in script


def test_no_api_key_header_means_no_key_in_script():
    """When the caller didn't send a key, the script must not emit a
    stray ``-H "X-API-Key: "`` header — curl rejects empty headers."""
    client = _client()
    resp = client.get("/api/v1/install-skill?agent=claude-code")
    assert resp.status_code == 200
    script = resp.text
    assert "MEMCLAW_API_KEY=" not in script
    assert "X-API-Key" not in script


def test_invalid_agent_returns_400():
    client = _client()
    resp = client.get("/api/v1/install-skill?agent=not-a-real-agent")
    assert resp.status_code == 400
    assert "Invalid 'agent' parameter" in resp.text


def test_both_agent_emits_both_install_blocks():
    client = _client()
    resp = client.get("/api/v1/install-skill?agent=both")
    assert resp.status_code == 200
    assert "$HOME/.claude/skills/memclaw" in resp.text
    assert "$HOME/.agents/skills/memclaw" in resp.text


def test_claude_code_only_skips_codex_block():
    client = _client()
    resp = client.get("/api/v1/install-skill?agent=claude-code")
    assert resp.status_code == 200
    assert "$HOME/.claude/skills/memclaw" in resp.text
    assert "$HOME/.agents/skills/memclaw" not in resp.text


def test_codex_only_skips_claude_block():
    client = _client()
    resp = client.get("/api/v1/install-skill?agent=codex")
    assert resp.status_code == 200
    assert "$HOME/.agents/skills/memclaw" in resp.text
    assert "$HOME/.claude/skills/memclaw" not in resp.text
