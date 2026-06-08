"""Unit tests for the install-credential identity plumbing.

Covers ``AuthContext`` field wiring from the gateway-injected
``X-Caura-Credential-Kind`` and ``X-Install-UUID`` headers, plus the
``BulkMemoryCreate.agent_id`` schema relaxation. The bulk-write
endpoint's runtime behaviour (auto-fill on broker callers) is
exercised end-to-end on the docker compose stack and not retested
here — these unit tests keep the schema-level contract honest so a
future ``agent_id: str`` regression in schemas.py would surface in CI
instead of at integration time.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from core_api.auth import AuthContext, get_auth_context
from core_api.config import settings
from core_api.schemas import BulkMemoryCreate, BulkMemoryItem


@pytest.fixture
def _disable_standalone(monkeypatch):
    """conftest defaults ``IS_STANDALONE=true`` so the OSS storage-less
    test suite can hit endpoints without auth. The Path-4 X-Tenant-ID
    branch under test is gated on ``not settings.is_standalone``, so
    this fixture flips it off for the install-credential tests only."""
    monkeypatch.setattr(settings, "is_standalone", False)
    monkeypatch.setattr(settings, "memclaw_api_key", "")
    monkeypatch.setattr(settings, "admin_api_key", "")
    monkeypatch.setattr(settings, "api_key", "")


def _request(headers: dict[str, str]):
    """Minimal stand-in for ``starlette.requests.Request`` — the
    auth function only reads ``request.headers.get(...)``, which a
    plain dict (case-insensitive via lowercase keys) satisfies.

    Headers must be lowercase here because the real Starlette
    ``Headers`` class is case-insensitive but the function reads via
    ``.get("x-..." lowercased)``."""
    return SimpleNamespace(headers={k.lower(): v for k, v in headers.items()})


# ── AuthContext install-credential plumbing ─────────────────────────


@pytest.mark.unit
class TestInstallCredentialAuthContext:
    """``get_auth_context`` Path 4 (X-Tenant-ID header) extracts the
    new credential-kind + install-uuid headers when present."""

    async def test_install_credential_headers_populate_context(self, _disable_standalone):
        request = _request(
            {
                "X-Tenant-ID": "tenant-broker-01",
                "X-Caura-Credential-Kind": "install_credential",
                "X-Install-UUID": "e435b91d-202b-4da4-9e21-dee620b033f8",
            }
        )
        ctx: AuthContext = await get_auth_context(request, key=None)

        assert ctx.tenant_id == "tenant-broker-01"
        assert ctx.is_install_credential is True
        assert ctx.install_uuid == "e435b91d-202b-4da4-9e21-dee620b033f8"

    async def test_user_api_key_kind_leaves_flag_false(self, _disable_standalone):
        """A gateway that always forwards the header but with kind
        ``user_api_key`` (the default for ``mc_`` keys) must keep
        the broker-mode flag off."""
        request = _request(
            {
                "X-Tenant-ID": "tenant-dashboard",
                "X-Caura-Credential-Kind": "user_api_key",
            }
        )
        ctx = await get_auth_context(request, key=None)

        assert ctx.tenant_id == "tenant-dashboard"
        assert ctx.is_install_credential is False
        assert ctx.install_uuid is None

    async def test_missing_credential_kind_header_defaults_to_false(self, _disable_standalone):
        """Gateways that haven't been updated to forward the new
        header (or OSS-direct deployments without a gateway) leave
        the flag False — preserves the existing CAURA-602 contract
        for non-broker callers."""
        request = _request({"X-Tenant-ID": "tenant-legacy"})
        ctx = await get_auth_context(request, key=None)

        assert ctx.tenant_id == "tenant-legacy"
        assert ctx.is_install_credential is False
        assert ctx.install_uuid is None

    async def test_credential_kind_header_is_case_insensitive(self, _disable_standalone):
        """HTTP headers are case-insensitive; the routing layer
        sometimes lowercases them. Both forms must produce the same
        AuthContext."""
        upper = _request(
            {
                "X-Tenant-ID": "t",
                "X-Caura-Credential-Kind": "INSTALL_CREDENTIAL",
                "X-Install-UUID": "u",
            }
        )
        upper_ctx = await get_auth_context(upper, key=None)
        assert upper_ctx.is_install_credential is True


# ── BulkMemoryCreate schema relaxation ──────────────────────────────


@pytest.mark.unit
class TestBulkMemoryCreateAgentIdOptional:
    """``agent_id`` is now Optional on the wire so memclawd broker
    calls (per cloud-data-plane.md §2.4) can omit it. The route
    handler fills in ``broker:{install_uuid}`` when the caller
    authenticates as an install credential; non-install callers
    still surface a downstream validation if they don't populate it."""

    def test_accepts_missing_agent_id(self):
        """No Pydantic 422 when agent_id is absent — previously this
        was a required ``str`` and would raise."""
        req = BulkMemoryCreate(
            tenant_id="t",
            items=[BulkMemoryItem(content="x")],
        )
        assert req.agent_id is None

    def test_accepts_explicit_none_agent_id(self):
        """SDK clients that serialise with ``exclude_none=True``
        sometimes still emit ``{"agent_id": null}`` — must not 422."""
        req = BulkMemoryCreate(
            tenant_id="t",
            agent_id=None,
            items=[BulkMemoryItem(content="x")],
        )
        assert req.agent_id is None

    def test_accepts_string_agent_id(self):
        """The dashboard / SDK path still passes a string and the
        schema must keep accepting it — relaxation is one-way."""
        req = BulkMemoryCreate(
            tenant_id="t",
            agent_id="dashboard-agent-01",
            items=[BulkMemoryItem(content="x")],
        )
        assert req.agent_id == "dashboard-agent-01"
