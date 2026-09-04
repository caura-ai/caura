"""H-06 + M-30 — /recall resolves identity by the same rule as /search.

Two findings, one defect: ``recall_endpoint`` re-derived the read identity
itself, straight from ``body.filter_agent_id``, instead of using the rule
``/search`` applies.

H-06 (high, disclosure + gate evasion). ``/search`` refuses an agent
credential that names a peer::

    if auth.agent_id and body.filter_agent_id != auth.agent_id: 403

``/recall`` had no such check, and fed the raw ``filter_agent_id`` into three
places at once: ``get_or_create_agent``, the trust<2 fleet forcing, and
``caller_agent_id`` (the VISIBILITY identity). So agent-A, trust 1 in fleet
F1, could POST ``{filter_agent_id: "agent-B"}`` where B is trust 3 in F2 and
get B's private ``scope_agent`` rows AND a tenant-wide scope, because the
forcing is keyed off B's trust level rather than A's.

M-30 (medium, silent drop). ``SearchRequest.caller_agent_id`` is documented as
"assert the agent identity this search runs as". ``/search`` honours it;
``/recall`` hardwired ``caller_agent_id=body.filter_agent_id`` and never read
the field. The same body returned different rows on the two routes, with no
error — and the SDKs forward the field verbatim, so it no-op'd silently.

Both are fixed by one shared ``_resolve_read_identity``, which is the point:
two routes parsing the same ``SearchRequest`` must not disagree about what its
fields mean.
"""

from __future__ import annotations

import uuid

import pytest

from tests.conftest import get_test_auth
from tests.conftest import uid as _uid

pytestmark = pytest.mark.integration


@pytest.fixture
def as_auth(monkeypatch):
    """Authenticate as a specific agent identity within a tenant.

    ``get_test_auth()`` returns the ADMIN key, which resolves to
    ``tenant_id=None``. The gate block is behind ``if auth.tenant_id:`` and the
    spoof guard keys off ``auth.agent_id``, so the admin key exercises neither
    — a test written with it would pass before and after any change here.
    """
    from core_api.app import app
    from core_api.auth import AuthContext, get_auth_context
    from core_api.tenant_context import set_current_tenant

    def _install(tenant_id: str, agent_id: str | None = None):
        async def _dep():
            set_current_tenant(tenant_id)
            return AuthContext(
                tenant_id=tenant_id,
                agent_id=agent_id,
                readable_tenant_ids=[tenant_id],
            )

        app.dependency_overrides[get_auth_context] = _dep

    yield _install
    from core_api.app import app as _app
    from core_api.auth import get_auth_context as _gac

    _app.dependency_overrides.pop(_gac, None)


async def _seed_agent(sc, tenant_id, agent_id, *, fleet_id, trust):
    await sc.create_or_update_agent(
        {
            "tenant_id": tenant_id,
            "agent_id": agent_id,
            "fleet_id": fleet_id,
            "trust_level": trust,
        }
    )


async def _write_private(client, headers, tenant_id, *, content, agent_id, fleet_id):
    """A ``scope_agent`` row — readable only by its own author."""
    resp = await client.post(
        "/api/v1/memories",
        json={
            "tenant_id": tenant_id,
            "content": content,
            "agent_id": agent_id,
            "fleet_id": fleet_id,
            "visibility": "scope_agent",
            "memory_type": "fact",
            "write_mode": "strong",
        },
        headers=headers,
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


async def _setup(client, sc):
    """Peer B holds a private memory; attacker A is trust-1 in another fleet."""
    tenant_id, headers = get_test_auth()
    nonce = f"h06-{uuid.uuid4().hex}"
    peer = f"peer-b-{_uid()}"
    attacker = f"agent-a-{_uid()}"
    await _seed_agent(sc, tenant_id, peer, fleet_id=f"fleet-f2-{_uid()}", trust=3)
    await _seed_agent(sc, tenant_id, attacker, fleet_id=f"fleet-f1-{_uid()}", trust=1)
    await _write_private(
        client,
        headers,
        tenant_id,
        content=f"{nonce} peer B private scope_agent memory",
        agent_id=peer,
        fleet_id=None,
    )
    return tenant_id, headers, nonce, peer, attacker


# ---------------------------------------------------------------------------
# H-06 — the spoof guard
# ---------------------------------------------------------------------------


async def test_recall_refuses_a_filter_agent_id_that_is_not_the_caller(
    client, sc, as_auth
):
    """THE DISCLOSURE. Fails without the fix: 200 carrying peer B's private row.

    Agent A names peer B. Pre-fix that made B the visibility identity, so B's
    ``scope_agent`` content came back to A.
    """
    tenant_id, _, nonce, peer, attacker = await _setup(client, sc)
    as_auth(tenant_id, attacker)

    resp = await client.post(
        "/api/v1/recall",
        json={"tenant_id": tenant_id, "query": nonce, "filter_agent_id": peer},
    )
    assert resp.status_code == 403, (
        f"recall let an agent name a peer; body={resp.text[:400]}"
    )
    assert "does not match the authenticated agent identity" in resp.text
    # On the content too: a later change answering 200-with-filtering instead
    # of 403 must still not surface the peer's private row.
    assert "peer B private scope_agent memory" not in resp.text


async def test_recall_refuses_a_caller_agent_id_that_is_not_the_caller(
    client, sc, as_auth
):
    """The same escalation by the other spelling.

    ``caller_agent_id`` feeds the identity directly, so guarding only
    ``filter_agent_id`` would have left the hole open under a different field
    name. Fails without the fix.
    """
    tenant_id, _, nonce, peer, attacker = await _setup(client, sc)
    as_auth(tenant_id, attacker)

    resp = await client.post(
        "/api/v1/recall",
        json={"tenant_id": tenant_id, "query": nonce, "caller_agent_id": peer},
    )
    assert resp.status_code == 403, resp.text
    assert "does not match the authenticated agent identity" in resp.text


async def test_recall_allows_an_agent_naming_itself(client, sc, as_auth):
    """The gate must not over-refuse: naming yourself is the normal case."""
    tenant_id, _, nonce, _peer, attacker = await _setup(client, sc)
    as_auth(tenant_id, attacker)

    resp = await client.post(
        "/api/v1/recall",
        json={"tenant_id": tenant_id, "query": nonce, "filter_agent_id": attacker},
    )
    assert resp.status_code == 200, resp.text


async def test_recall_tenant_key_may_still_name_any_agent(client, sc, as_auth):
    """A tenant-scoped credential is unaffected — the restriction is on agent keys.

    Pins the scope of the guard. Closing the hole by refusing every named agent
    would break the dashboard and every tenant-key integration.
    """
    tenant_id, _, nonce, peer, _attacker = await _setup(client, sc)
    as_auth(tenant_id, None)  # tenant-scoped: no agent identity

    resp = await client.post(
        "/api/v1/recall",
        json={"tenant_id": tenant_id, "query": nonce, "filter_agent_id": peer},
    )
    assert resp.status_code == 200, resp.text


# ---------------------------------------------------------------------------
# M-30 — caller_agent_id is honoured, not dropped
# ---------------------------------------------------------------------------


async def test_recall_honours_caller_agent_id_as_the_visibility_identity(
    client, sc, as_auth
):
    """THE SILENT DROP. Fails without the fix.

    A tenant-scoped caller asserts ``caller_agent_id`` exactly as the schema
    instructs. On ``/search`` that surfaces the named agent's ``scope_agent``
    rows; on ``/recall`` the field was never read, so the identical body
    returned a result set missing them — no error, just different answers.

    Asserted as a PARITY claim between the two routes rather than against a
    hardcoded expectation, so the test states the contract the findings are
    actually about.
    """
    tenant_id, _headers, nonce, peer, _attacker = await _setup(client, sc)
    as_auth(tenant_id, None)

    body = {"tenant_id": tenant_id, "query": nonce, "caller_agent_id": peer}

    search = await client.post("/api/v1/search", json=body)
    assert search.status_code == 200, search.text
    search_ids = {m["id"] for m in search.json()["items"]}

    recall = await client.post("/api/v1/recall", json=body)
    assert recall.status_code == 200, recall.text
    recall_ids = {m["id"] for m in recall.json()["memories"]}

    assert search_ids, "fixture produced no searchable rows — test is vacuous"
    assert recall_ids == search_ids, (
        "/recall dropped caller_agent_id: same body, different rows. "
        f"search={sorted(search_ids)} recall={sorted(recall_ids)}"
    )
