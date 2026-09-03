"""Tests for ``/api/v1/install-skill``.

Covers two fixes that landed together:

1. Auto-derive ``CAURA_API_URL`` from the request Host (and
   ``X-Forwarded-Proto`` when proxied) so ``curl
   https://caura.ai/api/v1/install-skill | bash`` yields a script that
   keeps fetching from caura.ai — not from ``http://localhost:8000``
   which was the old default.
2. Forward the caller's ``X-API-Key`` into the generated script so its
   internal curls carry auth. Required on edge-gated deploys (caura.ai
   nginx rejects unauthenticated calls on every path).
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from core_api.routes.plugin import router
from tests._legacy_contracts import FROZEN_PLUGIN_SLUG

pytestmark = pytest.mark.unit

CLAUDE_SKILL_DIR = f"$HOME/.claude/skills/{FROZEN_PLUGIN_SLUG}"
CODEX_SKILL_DIR = f"$HOME/.agents/skills/{FROZEN_PLUGIN_SLUG}"
DEFAULT_SKILL_ENDPOINT = f"/api/v1/skill/{FROZEN_PLUGIN_SLUG}"


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
    assert "CAURA_API_URL=http://testserver" in resp.text
    assert "http://localhost:8000" not in resp.text


def test_api_url_override_via_query_param():
    """Explicit ``?api_url=`` wins over the auto-derived default."""
    client = _client()
    resp = client.get("/api/v1/install-skill?api_url=https://explicit.example.com")
    assert resp.status_code == 200
    assert "CAURA_API_URL=https://explicit.example.com" in resp.text


def test_x_forwarded_proto_and_host_preferred_over_raw():
    """Behind a proxy, ``X-Forwarded-Proto`` / ``X-Forwarded-Host`` should
    be treated as authoritative — otherwise a user's script generated
    against ``https://caura.ai`` would read as ``http://internal-ip``."""
    client = _client()
    resp = client.get(
        "/api/v1/install-skill?agent=both",
        headers={
            "X-Forwarded-Proto": "https",
            "X-Forwarded-Host": "caura.ai",
        },
    )
    assert resp.status_code == 200
    assert "CAURA_API_URL=https://caura.ai" in resp.text
    # The raw ``Host: testserver`` header must not leak through.
    assert "testserver" not in resp.text


def test_api_key_header_forwarded_into_script():
    """Caller's ``X-API-Key`` is baked into the script, and the internal
    curl calls carry ``-H "X-API-Key: $CAURA_API_KEY"``."""
    client = _client()
    resp = client.get(
        "/api/v1/install-skill?agent=claude-code",
        headers={"X-API-Key": "mc_test_key_abc123"},
    )
    assert resp.status_code == 200
    script = resp.text
    # ``shlex.quote`` only wraps when the value has special chars. An
    # ``mc_``-style key is shell-safe and emitted unquoted, which is fine.
    assert "CAURA_API_KEY=mc_test_key_abc123" in script
    assert '-H "X-API-Key: $CAURA_API_KEY"' in script


def test_no_api_key_header_means_no_key_in_script():
    """When the caller didn't send a key, the script must not emit a
    stray ``-H "X-API-Key: "`` header — curl rejects empty headers."""
    client = _client()
    resp = client.get("/api/v1/install-skill?agent=claude-code")
    assert resp.status_code == 200
    script = resp.text
    assert "CAURA_API_KEY=" not in script
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
    assert CLAUDE_SKILL_DIR in resp.text
    assert CODEX_SKILL_DIR in resp.text


def test_claude_code_only_skips_codex_block():
    client = _client()
    resp = client.get("/api/v1/install-skill?agent=claude-code")
    assert resp.status_code == 200
    assert CLAUDE_SKILL_DIR in resp.text
    assert CODEX_SKILL_DIR not in resp.text


def test_codex_only_skips_claude_block():
    client = _client()
    resp = client.get("/api/v1/install-skill?agent=codex")
    assert resp.status_code == 200
    assert CODEX_SKILL_DIR in resp.text
    assert CLAUDE_SKILL_DIR not in resp.text


# --- skill selector (?skill=) -------------------------------------------------


def test_default_skill_slug_unchanged():
    """No ``?skill=`` → the installer is the original default one: the same
    install paths, the same fetch URL, the 'Caura' title, and no trace of
    company-brain. Guards the 'default load is unaffected' contract."""
    client = _client()
    resp = client.get("/api/v1/install-skill?agent=both")
    assert resp.status_code == 200
    script = resp.text
    assert "=== Caura Skill Installer (direct-MCP) ===" in script
    assert CLAUDE_SKILL_DIR in script
    assert CODEX_SKILL_DIR in script
    assert DEFAULT_SKILL_ENDPOINT in script
    assert "company-brain" not in script


def test_skill_company_brain_installs_to_company_brain_dirs():
    """``?skill=company-brain`` swaps the skill name through the paths, the
    fetch URL, and the title — and never touches the default skill dirs."""
    client = _client()
    resp = client.get("/api/v1/install-skill?agent=both&skill=company-brain")
    assert resp.status_code == 200
    script = resp.text
    assert "=== Company Brain Skill Installer (direct-MCP) ===" in script
    assert "$HOME/.claude/skills/company-brain" in script
    assert "$HOME/.agents/skills/company-brain" in script
    assert "/api/v1/skill/company-brain" in script
    assert f"skills/{FROZEN_PLUGIN_SLUG}" not in script


def test_skill_caura_installs_to_caura_dirs():
    """``?skill=caura`` swaps the skill name through the paths, the fetch
    URL, and the title — same shape as company-brain, proving the new
    slug is independently allowlisted rather than piggybacking on the
    default. Never touches the historical slug's dirs."""
    client = _client()
    resp = client.get("/api/v1/install-skill?agent=both&skill=caura")
    assert resp.status_code == 200
    script = resp.text
    assert "=== Caura Skill Installer (direct-MCP) ===" in script
    assert "$HOME/.claude/skills/caura" in script
    assert "$HOME/.agents/skills/caura" in script
    assert "/api/v1/skill/caura" in script
    assert f"skills/{FROZEN_PLUGIN_SLUG}" not in script


def test_invalid_skill_returns_400():
    client = _client()
    resp = client.get("/api/v1/install-skill?skill=not-a-real-skill")
    assert resp.status_code == 400
    assert "Invalid 'skill' parameter" in resp.text


def test_skill_param_is_allowlisted_no_path_traversal():
    """A traversal-looking value is rejected by the allowlist, never used to
    build a path."""
    client = _client()
    resp = client.get("/api/v1/install-skill?skill=../../etc/passwd")
    assert resp.status_code == 400
    assert "Invalid 'skill' parameter" in resp.text


# --- /skill/{skill} serving route ---------------------------------------------


def test_serve_default_skill_still_works():
    client = _client()
    resp = client.get(DEFAULT_SKILL_ENDPOINT)
    assert resp.status_code == 200
    assert f"name: {FROZEN_PLUGIN_SLUG}" in resp.text


def test_serve_company_brain_skill():
    client = _client()
    resp = client.get("/api/v1/skill/company-brain")
    assert resp.status_code == 200
    assert "name: company-brain" in resp.text


def test_serve_caura_skill_independently_of_memclaw():  # legacy-name-ok: rule 3 compat-alias test, dual-path transition
    """/skill/caura serves its own file (static/skills/caura/SKILL.md),
    not a forwarded copy of the historical route's response. This is the
    in-process proof that the two slugs are genuinely dual-served
    rather than one aliasing the other — the thing an authenticated
    request against the live deploy would otherwise be needed to show.
    Proves the CODE is correct; does not by itself prove the deployed
    edge reaches this code path (see PR body)."""
    client = _client()
    caura_resp = client.get("/api/v1/skill/caura")
    legacy_resp = client.get(DEFAULT_SKILL_ENDPOINT)
    assert caura_resp.status_code == 200
    assert legacy_resp.status_code == 200
    assert "name: caura" in caura_resp.text
    assert f"name: {FROZEN_PLUGIN_SLUG}" in legacy_resp.text
    # The two skills describe the same product and are mostly identical
    # prose, but their self-referential install paths must differ —
    # this is what would break if /skill/caura silently went back to
    # forwarding the historical route's response.
    assert "skills/caura/SKILL.md" in caura_resp.text
    assert "skills/caura/SKILL.md" not in legacy_resp.text


def test_serve_unknown_skill_returns_404():
    client = _client()
    resp = client.get("/api/v1/skill/not-a-real-skill")
    assert resp.status_code == 404
