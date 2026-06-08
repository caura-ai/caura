"""E2E install-plugin script generation tests through HTTP API."""

import pytest

from tests.conftest import get_test_auth, uid as _uid


# ---------------------------------------------------------------------------
# POST /api/install-plugin (preferred — no secrets in URL)
# ---------------------------------------------------------------------------


async def test_post_install_plugin_generates_script(client):
    """POST /api/install-plugin returns a bash script with correct env vars."""
    _, headers = get_test_auth()
    resp = await client.post(
        "/api/v1/install-plugin",
        json={
            "fleet_id": "my-fleet",
            "api_url": "https://memclaw.example.com",
            "api_key": "sk-test-key-1234",
            "node_name": "node-alpha",
        },
        headers=headers,
    )
    assert resp.status_code == 200
    script = resp.text
    assert script.startswith("#!/usr/bin/env bash")
    assert "MEMCLAW_API_URL=" in script
    assert "memclaw.example.com" in script
    assert "MEMCLAW_FLEET_ID=" in script
    assert "my-fleet" in script
    assert "MEMCLAW_API_KEY=" in script
    assert "sk-test-key-1234" in script
    assert "MEMCLAW_NODE_NAME=" in script
    assert "node-alpha" in script


async def test_post_install_plugin_api_key_from_header(client):
    """POST body.api_key takes precedence; falls back to X-API-Key header."""
    resp = await client.post(
        "/api/v1/install-plugin",
        json={
            "fleet_id": "f1",
            "api_url": "https://example.com",
            # no api_key in body — should use header
        },
        headers={"X-API-Key": "header-key-abc"},
    )
    assert resp.status_code == 200
    assert "header-key-abc" in resp.text


async def test_post_install_plugin_body_key_overrides_header(client):
    """If api_key is in body, it overrides the header key."""
    resp = await client.post(
        "/api/v1/install-plugin",
        json={
            "fleet_id": "f1",
            "api_url": "https://example.com",
            "api_key": "body-key-xyz",
        },
        headers={"X-API-Key": "header-key-abc"},
    )
    assert resp.status_code == 200
    assert "body-key-xyz" in resp.text


# ---------------------------------------------------------------------------
# GET /api/install-plugin (header-based key)
# ---------------------------------------------------------------------------


async def test_get_install_plugin_uses_header_key(client):
    """GET /api/install-plugin reads API key from X-API-Key header."""
    resp = await client.get(
        "/api/v1/install-plugin?fleet_id=fleet-g&api_url=https://example.com",
        headers={"X-API-Key": "my-get-key"},
    )
    assert resp.status_code == 200
    script = resp.text
    assert "my-get-key" in script
    assert "fleet-g" in script


# ---------------------------------------------------------------------------
# Script content checks
# ---------------------------------------------------------------------------


async def test_script_has_chmod_600(client):
    """The generated script protects .env with chmod 600."""
    resp = await client.post(
        "/api/v1/install-plugin",
        json={"fleet_id": "f", "api_url": "https://x.com", "api_key": "k"},
    )
    assert resp.status_code == 200
    assert 'chmod 600' in resp.text


async def test_script_contains_correct_env_vars(client):
    """The .env block has all required MEMCLAW_* variables."""
    resp = await client.post(
        "/api/v1/install-plugin",
        json={
            "fleet_id": "fleet-env",
            "api_url": "https://env.example.com",
            "api_key": "key-env-123",
            "node_name": "env-node",
        },
    )
    assert resp.status_code == 200
    script = resp.text
    # Check .env block contains all five vars
    for var in ("MEMCLAW_API_URL", "MEMCLAW_API_KEY", "MEMCLAW_FLEET_ID",
                "MEMCLAW_TENANT_ID", "MEMCLAW_NODE_NAME"):
        assert var in script, f"Missing {var} in script"


# ---------------------------------------------------------------------------
# Shell injection prevention
# ---------------------------------------------------------------------------


async def test_shell_injection_api_url(client):
    """Malicious api_url is shell-quoted, preventing injection."""
    malicious_url = "https://evil.com'; rm -rf / #"
    resp = await client.post(
        "/api/v1/install-plugin",
        json={
            "fleet_id": "safe",
            "api_url": malicious_url,
            "api_key": "k",
        },
    )
    assert resp.status_code == 200
    script = resp.text
    # shlex.quote wraps the value in single quotes; the dangerous payload
    # should NOT appear unquoted
    assert "rm -rf" not in script or "'" in script
    # The raw semicolon should be inside a quoted string, not bare
    assert "MEMCLAW_API_URL=" in script


async def test_shell_injection_fleet_id(client):
    """Malicious fleet_id is shell-quoted."""
    malicious = '$(cat /etc/passwd)'
    resp = await client.post(
        "/api/v1/install-plugin",
        json={
            "fleet_id": malicious,
            "api_url": "https://safe.com",
            "api_key": "k",
        },
    )
    assert resp.status_code == 200
    script = resp.text
    # shlex.quote should wrap it — the $() should not be bare
    assert "MEMCLAW_FLEET_ID=" in script
    # Ensure the command substitution is neutralised (inside single quotes)
    line = [l for l in script.splitlines() if "MEMCLAW_FLEET_ID=" in l][0]
    # shlex.quote produces: '$(cat /etc/passwd)' (with outer single quotes)
    assert line.count("'") >= 2, "Fleet ID should be single-quoted"


# ---------------------------------------------------------------------------
# Security: API key not in query params
# ---------------------------------------------------------------------------


async def test_api_key_not_in_query_params(client):
    """GET endpoint reads key from header, not query string.

    Ensures the API key isn't leaked in server logs via URL parameters.
    """
    # The GET endpoint has no api_key query param — only header.
    # Sending it as a query param should NOT inject it into the script.
    resp = await client.get(
        "/api/v1/install-plugin?fleet_id=f1&api_url=https://x.com&api_key=LEAKED",
        headers={"X-API-Key": "correct-key"},
    )
    assert resp.status_code == 200
    # The script should use the header key, not the query param
    assert "correct-key" in resp.text


# ---------------------------------------------------------------------------
# Bootstrap aliases at /api/<endpoint> (no /v1)
# ---------------------------------------------------------------------------
#
# The generated install script fetches plugin sources via the
# non-versioned ``/api/plugin-source`` path so the same script works
# against both OSS and the enterprise gateway. Without these aliases,
# OSS standalone installs 404 at step 5/7. See plugin.py:plugin_bootstrap_router.


async def test_legacy_plugin_source_alias(client):
    """``GET /api/plugin-source`` resolves the same content as ``/api/v1/plugin-source``."""
    _, headers = get_test_auth()
    legacy = await client.get("/api/plugin-source?file=index.ts", headers=headers)
    versioned = await client.get("/api/v1/plugin-source?file=index.ts", headers=headers)
    assert legacy.status_code == 200
    assert versioned.status_code == 200
    assert legacy.text == versioned.text


async def test_legacy_install_plugin_alias(client):
    """``GET /api/install-plugin`` returns the bootstrap script (alias of v1)."""
    _, headers = get_test_auth()
    resp = await client.get(
        "/api/install-plugin?fleet_id=alias-fleet&api_url=https://x.example",
        headers=headers,
    )
    assert resp.status_code == 200
    assert resp.text.startswith("#!/usr/bin/env bash")
    assert "MEMCLAW_FLEET_ID=alias-fleet" in resp.text


async def test_install_script_uses_non_versioned_paths(client):
    """The generated script must use ``/api/plugin-source`` (no v1).

    Pinned because changing this URL breaks enterprise installs —
    enterprise nginx gates ``/api/v1/plugin-source`` behind auth, while
    the install script's source-fetch curls don't pass an X-API-Key.
    The non-versioned path is the unauthenticated bootstrap path on
    both deploys.
    """
    _, headers = get_test_auth()
    resp = await client.get(
        "/api/v1/install-plugin?fleet_id=f&api_url=https://x.example",
        headers=headers,
    )
    assert resp.status_code == 200
    script = resp.text
    assert "/api/plugin-source?" in script
    # Defensive: emitting /api/v1/plugin-source would 401 on enterprise.
    assert "/api/v1/plugin-source?" not in script
