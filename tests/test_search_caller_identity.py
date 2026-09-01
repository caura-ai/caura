"""POST /search — asserting a caller identity without filtering to it (#1197).

Baseline first. #1197 claimed ``recall_count`` was "permanently dead for
tenant-key callers". That is not right, and the first two tests here pin the
accurate version: a tenant key that sets ``filter_agent_id`` ALREADY moves the
counter today, because the route derived the visibility identity from the
filter. What was dead is the bump for a tenant key that does not filter — and
the cost of the workaround was the filtering itself, not a dead counter.
"""

from __future__ import annotations

import uuid

import pytest

pytestmark = pytest.mark.asyncio


def _uid() -> str:
    return uuid.uuid4().hex[:8]


@pytest.fixture
def as_auth(monkeypatch):
    """Controlled AuthContext, mirroring the gateway header-trust path."""
    from core_api.app import app
    from core_api.auth import AuthContext, get_auth_context
    from core_api.tenant_context import set_current_tenant

    def _install(tenant_id: str, agent_id: str | None = None, **kwargs):
        async def _dep():
            set_current_tenant(tenant_id)
            return AuthContext(
                tenant_id=tenant_id,
                agent_id=agent_id,
                readable_tenant_ids=[tenant_id],
                **kwargs,
            )

        app.dependency_overrides[get_auth_context] = _dep

    yield _install
    from core_api.app import app as _app
    from core_api.auth import get_auth_context as _gac

    _app.dependency_overrides.pop(_gac, None)


async def _seed(client, tenant: str, agent: str, visibility: str = "scope_team") -> None:
    resp = await client.post(
        "/api/v1/memories",
        json={
            "tenant_id": tenant,
            "agent_id": agent,
            "memory_type": "fact",
            "content": f"a recallable fact {_uid()}",
            "visibility": visibility,
        },
    )
    assert resp.status_code == 201, resp.text


async def _search(client, tenant: str, **body) -> dict:
    resp = await client.post(
        "/api/v1/search",
        json={"tenant_id": tenant, "query": "recallable fact", "top_k": 5, **body},
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


# --- baseline: what the tenant key could already do --------------------------


async def test_a_tenant_key_that_filters_already_tracks_recalls(client, as_auth):
    """The correction to #1197: filtering already carries an identity."""
    tenant = f"tenant-{_uid()}"
    as_auth(tenant)  # tenant-scoped: auth.agent_id is None
    await _seed(client, tenant, "agent-a")
    data = await _search(client, tenant, filter_agent_id="agent-a")
    assert data["recall_tracked"] is True


async def test_a_tenant_key_that_does_not_filter_tracks_nothing(client, as_auth):
    """...and this is the case that was actually dead."""
    tenant = f"tenant-{_uid()}"
    as_auth(tenant)
    await _seed(client, tenant, "agent-a")
    data = await _search(client, tenant)
    assert data["recall_tracked"] is False


# --- identity without filtering ---------------------------------------------


async def test_caller_agent_id_grants_identity_without_filtering(client, as_auth):
    """The whole point: assert an identity, keep seeing everyone's rows.

    The seed is deliberate. A ``scope_agent`` row is visible ONLY to the agent
    that wrote it, so it is the one thing a tenant key cannot see without
    presenting an identity — which makes it the only assertion here that can
    tell "the field worked" apart from "the field was silently ignored".
    ``SearchRequest`` is ``extra="ignore"``, so a test that merely counted
    unfiltered rows would pass just as happily against a build with no such
    field at all.
    """
    tenant = f"tenant-{_uid()}"
    as_auth(tenant)
    await _seed(client, tenant, "agent-a", visibility="scope_agent")
    await _seed(client, tenant, "agent-b", visibility="scope_team")

    bare = await _search(client, tenant)
    asserted = await _search(client, tenant, caller_agent_id="agent-a")
    filtered = await _search(client, tenant, filter_agent_id="agent-a")

    # No identity: agent-a's private row is invisible.
    assert len(bare["items"]) == 1
    # Identity asserted: agent-a's private row PLUS the shared one.
    assert len(asserted["items"]) == 2
    # The old workaround buys the same visibility at the cost of the filter.
    assert len(filtered["items"]) == 1


async def test_an_authenticated_agent_identity_wins(client, as_auth):
    """Precedence: a credential that carries an identity is not overridable."""
    tenant = f"tenant-{_uid()}"
    as_auth(tenant, agent_id="agent-a")
    await _seed(client, tenant, "agent-a")
    # Naming ITSELF is allowed, so this exercises precedence rather than the
    # 403 below — auth.agent_id is what reaches the pipeline either way.
    data = await _search(client, tenant, caller_agent_id="agent-a")
    assert data["recall_tracked"] is True


async def test_an_agent_credential_may_not_assert_a_peer_identity(client, as_auth):
    """The escalation this field must not reopen.

    ``caller_agent_id`` feeds exactly what ``filter_agent_id`` used to smuggle
    in — the visibility identity and the subject of the trust<2 fleet forcing —
    so the existing 403 has to cover the new spelling too.
    """
    tenant = f"tenant-{_uid()}"
    as_auth(tenant, agent_id="agent-a")
    resp = await client.post(
        "/api/v1/search",
        json={
            "tenant_id": tenant,
            "query": "anything",
            "top_k": 5,
            "caller_agent_id": "agent-b",
        },
    )
    assert resp.status_code == 403, resp.text


# --- ranking stays put unless the tenant says otherwise ----------------------


async def test_an_asserted_identity_does_not_move_recall_count_by_default(client, as_auth):
    """The reason this is not just a one-line field.

    recall_boost defaults to True, so a bump reshuffles results for every
    caller in the tenant — not only the one that sent the field.
    """
    tenant = f"tenant-{_uid()}"
    as_auth(tenant)
    await _seed(client, tenant, "agent-a", visibility="scope_agent")
    data = await _search(client, tenant, caller_agent_id="agent-a")
    # The scope_agent row proves the identity actually took effect — without
    # that, "no bump" would also be satisfied by the field being ignored.
    assert len(data["items"]) == 1
    assert data["recall_tracked"] is False


async def test_a_tenant_can_opt_in_to_tracking_asserted_recalls(client, as_auth, monkeypatch):
    """...and the capability #1197 asked for is still reachable."""
    from core_api.services.organization_settings import ResolvedConfig

    monkeypatch.setattr(
        ResolvedConfig,
        "recall_for_asserted_identity",
        property(lambda self: True),
    )

    tenant = f"tenant-{_uid()}"
    as_auth(tenant)
    await _seed(client, tenant, "agent-a", visibility="scope_agent")
    data = await _search(client, tenant, caller_agent_id="agent-a")
    assert len(data["items"]) == 1
    assert data["recall_tracked"] is True


async def test_an_authenticated_identity_is_unaffected_by_the_setting(client, as_auth):
    """The setting gates ASSERTED identities only — it must not gate real ones.

    Without this, shipping the default-off switch would silently stop recall
    tracking for every agent-credential caller in the system.
    """
    tenant = f"tenant-{_uid()}"
    as_auth(tenant, agent_id="agent-a")
    await _seed(client, tenant, "agent-a")
    data = await _search(client, tenant)
    assert data["recall_tracked"] is True
