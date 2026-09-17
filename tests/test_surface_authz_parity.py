"""The MCP surface and ``GET /memories/count`` enforce what their REST neighbours do.

Five findings, one shape: a guard that one surface applies and its twin does
not. None of these is a guard that was wrong — each is a guard that was never
asked for, which is why they survived review on surfaces that otherwise look
carefully gated.

The suppression case is the one to read first. REST calls
``auth._block_if_suppressed`` from ``get_auth_context``, so every REST route
inherits it by construction; the MCP middleware resolved identity the same way
and then never asked the question, so a soft-deleted org kept full MCP read AND
write while REST 403'd the same credential.
"""

from __future__ import annotations

import pytest

from core_api import mcp_server
from core_api.constants import MAX_QUERY_LENGTH, MAX_SEARCH_TOP_K
from tests._mcp_test_helpers import parse_envelope, stub_storage_client

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]


def _wire_recall(monkeypatch, captured: dict):
    async def fake_search(**kwargs):
        captured.update(kwargs)
        return []

    monkeypatch.setattr(mcp_server, "search_memories", fake_search, raising=False)
    stub_storage_client(
        monkeypatch, get_agent={"agent_id": "a", "fleet_id": None, "trust_level": 5}
    )


# --------------------------------------------------------------------------
# oss-0814-m-05 — top_k had an upper bound only
# --------------------------------------------------------------------------


@pytest.mark.parametrize("bad_top_k", [-5, 0])
async def test_recall_clamps_non_positive_top_k_to_one(mcp_env, monkeypatch, bad_top_k):
    """A non-positive ``top_k`` must never reach the service.

    ``min(top_k, MAX)`` passed it straight through: the entity route returned a
    full unscored pool and the scored route sent a negative SQL LIMIT and 500'd.
    """
    captured: dict = {}
    _wire_recall(monkeypatch, captured)

    out = await mcp_server.caura_recall(query="hello", top_k=bad_top_k)

    assert captured.get("top_k") == 1, (
        f"top_k={bad_top_k} reached the service as {captured.get('top_k')!r}"
    )
    assert parse_envelope(out)["effective_top_k"] == 1


async def test_recall_still_caps_at_the_upper_bound(mcp_env, monkeypatch):
    """The clamp is two-sided — adding the floor must not drop the ceiling."""
    captured: dict = {}
    _wire_recall(monkeypatch, captured)

    await mcp_server.caura_recall(query="hello", top_k=MAX_SEARCH_TOP_K + 250)

    assert captured.get("top_k") == MAX_SEARCH_TOP_K


# --------------------------------------------------------------------------
# oss-0814-l-26 — query length was unbounded where REST caps it
# --------------------------------------------------------------------------


async def test_recall_refuses_a_query_longer_than_the_rest_cap(mcp_env, monkeypatch):
    """MCP accepted any length; REST's SearchRequest caps at MAX_QUERY_LENGTH.

    This is the one surface where the caller sets the cost of the request — an
    unbounded query reached the embedding provider and the FTS path.
    """
    captured: dict = {}
    _wire_recall(monkeypatch, captured)

    out = await mcp_server.caura_recall(query="x" * (MAX_QUERY_LENGTH + 1), top_k=5)

    assert parse_envelope(out)["error"]["code"] == "INVALID_ARGUMENTS"
    assert not captured, "an over-length query still reached the search service"


async def test_recall_accepts_a_query_exactly_at_the_cap(mcp_env, monkeypatch):
    """Boundary: the cap is inclusive, matching REST's ``max_length``."""
    captured: dict = {}
    _wire_recall(monkeypatch, captured)

    await mcp_server.caura_recall(query="x" * MAX_QUERY_LENGTH, top_k=5)

    assert len(captured.get("query", "")) == MAX_QUERY_LENGTH


# --------------------------------------------------------------------------
# oss-0814-l-04 — ValidationError escaped the single-write path
# --------------------------------------------------------------------------


async def test_write_returns_a_structured_error_for_an_invalid_weight(
    mcp_env, monkeypatch
):
    """``MemoryCreate`` raises ValidationError; only HTTPException was caught.

    The batch path already caught its own. This pins the single path's twin, so
    a caller gets INVALID_ARGUMENTS rather than an unstructured MCP error.
    """
    stub_storage_client(
        monkeypatch, get_agent={"agent_id": "a", "fleet_id": None, "trust_level": 5}
    )

    out = await mcp_server.caura_write(content="hello", weight=99.0, agent_id="a")

    assert parse_envelope(out)["error"]["code"] == "INVALID_ARGUMENTS"


# --------------------------------------------------------------------------
# oss-0814-m-30 / oss-0902-m-14 — suppression was never asked about on MCP
# --------------------------------------------------------------------------


class _Send:
    """Collect an ASGI response so the middleware's refusal can be asserted."""

    def __init__(self):
        self.status: int | None = None
        self.body = b""

    async def __call__(self, message):
        if message["type"] == "http.response.start":
            self.status = message["status"]
        elif message["type"] == "http.response.body":
            self.body += message.get("body", b"")


async def _run_middleware(
    monkeypatch,
    *,
    suppressed: bool,
    tenant: str | None = "t-live",
    looked_up: list[str] | None = None,
):
    """Drive the middleware once. ``tenant=None`` sends no identity at all.

    ``looked_up`` collects the tenants the suppression guard actually asked
    about — the only way to assert that it asked about none.
    """
    import core_api.suppression as supp

    async def fake_is_suppressed(tenant_id: str) -> bool:
        if looked_up is not None:
            looked_up.append(tenant_id)
        return suppressed

    monkeypatch.setattr(supp, "is_tenant_suppressed", fake_is_suppressed)
    monkeypatch.setattr(
        "core_api.config.settings.gateway_shared_secret", "", raising=False
    )

    reached = {"app": False}

    async def fake_app(scope, receive, send):
        reached["app"] = True

    mw = mcp_server.MCPAuthMiddleware(fake_app)
    send = _Send()
    scope = {
        "type": "http",
        "headers": [(b"x-tenant-id", tenant.encode())] if tenant else [],
    }

    async def receive():
        return {"type": "http.request"}

    await mw(scope, receive, send)
    return reached, send


async def test_a_suppressed_org_is_refused_before_reaching_any_mcp_tool(monkeypatch):
    """403 at the middleware, not per-tool.

    Gating in the middleware is what makes this hold for tools added later —
    the same reason REST puts it in ``get_auth_context`` rather than in routes.
    """
    reached, send = await _run_middleware(monkeypatch, suppressed=True)

    assert send.status == 403, f"suppressed org got {send.status}, not 403"
    assert not reached["app"], "request reached an MCP tool despite suppression"
    assert b"ORGANIZATION_SUSPENDED" in send.body


async def test_a_live_org_still_reaches_the_tools(monkeypatch):
    """The guard must not refuse everyone — the failure mode that hides itself."""
    reached, send = await _run_middleware(monkeypatch, suppressed=False)

    assert reached["app"], "a live tenant was blocked"
    assert send.status is None


async def test_the_refusal_does_not_name_soft_deletion(monkeypatch):
    """Same generic wording REST uses: naming it leaks org lifecycle state."""
    _, send = await _run_middleware(monkeypatch, suppressed=True)

    lowered = send.body.lower()
    assert b"deleted" not in lowered and b"soft" not in lowered


async def test_the_guard_asks_about_no_sentinel_tenant(monkeypatch):
    """``_NO_AUTH`` is a placeholder in the tenant slot, not a tenant.

    It reaches the guard on the deployment shape with no admin key, no
    ``CAURA_API_KEY`` and no caller key — where it stands for "nobody", so a
    suppression lookup for it can only ever answer False. The skip list named
    two of the three sentinels, which is the omission this pins: the assertion
    is on the lookups performed, because the wrong behaviour returns the same
    200 as the right one.
    """
    monkeypatch.setattr(mcp_server, "get_admin_key", lambda: "", raising=False)
    monkeypatch.setattr("core_api.config.settings.is_standalone", False, raising=False)

    looked_up: list[str] = []
    reached, send = await _run_middleware(
        monkeypatch, suppressed=False, tenant=None, looked_up=looked_up
    )

    # Assert the branch was REACHED rather than forcing it: the remaining
    # precondition is that no shared key is configured, and the resolved tenant
    # is the observable proof of it. Stubbing that setting instead would mean
    # naming a legacy-branded field the ratchet rightly refuses to see minted
    # again — and this says more, since a test that forces its own precondition
    # cannot notice the branch moving out from under it.
    assert mcp_server._get_tenant() == mcp_server._NO_AUTH, (
        "expected the no-credential path; a configured shared key would take "
        "a different branch and this would not be testing the sentinel at all"
    )
    assert looked_up == [], f"suppression was looked up for {looked_up!r}"
    assert reached["app"], "the request should still reach _check_auth's own refusal"
    assert send.status is None


async def test_every_sentinel_is_refused(monkeypatch):
    """The two enumerations of the sentinel roster agree.

    ``_SENTINEL_TENANTS`` (what the suppression guard skips) and ``_check_auth``
    (what each sentinel is refused with) are separate lists by necessity: the
    guard wants membership, ``_check_auth`` has to tell them apart to pick an
    error, and it must keep naming the error constants so ``tests/_error_codes``
    can still see ``UNAUTHORIZED``/``FORBIDDEN`` as reachable. Separate lists
    drift, and this is the drift that matters: a sentinel the guard skips but
    ``_check_auth`` does not know falls through to ``None`` — auth PASSES, and
    a tool runs with a placeholder string as its tenant.

    Not tautological. Deleting a branch from ``_check_auth`` fails this while
    every other test in the file stays green.
    """
    for sentinel in mcp_server._SENTINEL_TENANTS:
        monkeypatch.setattr(mcp_server, "_get_tenant", lambda s=sentinel: s)
        assert mcp_server._check_auth() is not None, (
            f"{sentinel!r} is skipped by the suppression guard but authenticates"
        )


# --------------------------------------------------------------------------
# oss-0902-m-28 — GET /memories/count had neither gate its neighbours have
# --------------------------------------------------------------------------


async def _count_with(monkeypatch, auth, *, fleet_id=None) -> dict:
    """Run ``memory_count`` against a fake storage client.

    Returns what the route asked storage for. One fake, not one per test: its
    signature has to track the real ``count_active``, and this round added a
    parameter to it — a second copy is a second thing to remember.
    """
    from core_api.routes import memories as mem_routes

    seen: dict = {}

    class _SC:
        async def count_active(
            self,
            tenant_id,
            fleet_id=None,
            status=None,
            exclude_scope_agent=False,
            caller_agent_id=None,
        ):
            seen["exclude_scope_agent"] = exclude_scope_agent
            seen["caller_agent_id"] = caller_agent_id
            return 7

    monkeypatch.setattr(mem_routes, "get_storage_client", lambda: _SC())
    await mem_routes.memory_count(
        tenant_id="t1", fleet_id=fleet_id, status=None, auth=auth
    )
    return seen


async def test_count_applies_the_fleet_read_gate(monkeypatch):
    """A fleet-scoped count is a fleet-scoped read.

    ``memory_stats`` carries this exact gate, with a comment recording that it
    too was once missing — ``count`` is the neighbour that was not revisited.
    """
    from core_api.agent_ids import AgentIdentity
    from core_api.auth import AuthContext
    from core_api.routes import memories as mem_routes

    calls: list[tuple] = []

    async def spy_gate(tenant_id, agent_id, fleet_id):
        calls.append((tenant_id, str(agent_id), fleet_id))

    monkeypatch.setattr(mem_routes, "enforce_fleet_read", spy_gate)

    await _count_with(
        monkeypatch,
        AuthContext(tenant_id="t1", agent_id=AgentIdentity("agent-a")),
        fleet_id="other-fleet",
    )

    assert calls == [("t1", "agent-a", "other-fleet")], (
        f"expected one fleet-read gate call, got {calls!r}"
    )


async def test_count_scopes_visibility_to_the_authenticated_agent(monkeypatch):
    """Scoping on, and the caller's own identity carried into it.

    Counting every ``scope_agent`` row returned, as a number, what the list
    route withholds. Excluding every one of them is the equal and opposite
    error — an agent's own count would come in under its own list. The
    identity is what separates the two. ``TestVisibilityFiltering`` in
    tests/test_visibility.py asserts the SQL that acts on it, against real rows.
    """
    from core_api.agent_ids import AgentIdentity
    from core_api.auth import AuthContext

    seen = await _count_with(
        monkeypatch, AuthContext(tenant_id="t1", agent_id=AgentIdentity("agent-a"))
    )

    assert seen.get("exclude_scope_agent") is True
    assert seen.get("caller_agent_id") == "agent-a"


async def test_count_sends_no_identity_for_a_credential_that_has_none(monkeypatch):
    """A tenant/user credential authenticates no agent.

    ``effective_agent_id`` returns None, and this route takes no ``agent_id``
    param for one to be asserted through — so there is nothing to forge, and
    storage falls back to hiding every private row.
    """
    from core_api.auth import AuthContext

    seen = await _count_with(monkeypatch, AuthContext(tenant_id="t1"))

    assert seen.get("exclude_scope_agent") is True
    assert seen.get("caller_agent_id") is None
