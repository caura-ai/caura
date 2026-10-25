"""``GET /memories`` and ``GET /memories/stats`` apply the same read gates (#847).

The two routes read the same rows, one listing them and one counting them, so
a caller refused one has to be refused the other. That stopped holding once
already: ``GET /memories`` had ``enforce_fleet_read`` and the gateway-agent
preference, ``/memories/stats`` had neither until H-10 (#846), and a trust-1
agent could count a fleet the list would not show it. Each route has its own
regression tests in ``test_api_read_scope_params.py``; this asks both of them
the same questions at once, so the next hardening that lands on one route fails
here until the other has it too.

Both answers are pinned, not only their agreement, so the pair cannot drift
together either. Where the two serve a request, the count has to equal the
listed rows: a filter applied on one side only is the same drift, one layer
down.
"""

from __future__ import annotations

import pytest

from tests.conftest import get_test_auth, new_tenant_id
from tests.conftest import uid as _uid
from tests.test_api_read_scope_params import _as_agent, _set_trust, _write

# (case, query): served at trust 1.
_OPEN = [
    ("no params", ""),
    ("own agent_id", "agent_id={alice}"),
    ("home fleet_id", "fleet_id={home}"),
    ("scope=agent", "scope=agent"),
    ("scope=fleet, home fleet implied", "scope=fleet"),
    ("scope=fleet, home fleet named", "scope=fleet&fleet_id={home}"),
]
# (case, query): refused at trust 1, served at trust 2.
_GATED = [
    ("another fleet_id", "fleet_id={other}"),
    ("scope=agent naming another fleet", "scope=agent&fleet_id={other}"),
    ("scope=fleet, another fleet", "scope=fleet&fleet_id={other}"),
    ("scope=all", "scope=all"),
]


@pytest.fixture
async def tenant(client, sc):
    """alice writes in her home fleet and bob in another, both ``scope_org``,
    so for those rows the gates, not visibility, decide what comes back. bob
    also keeps one private row, which no request of alice's may count."""
    tenant_id, headers = get_test_auth(new_tenant_id())
    tag = _uid()
    names = {
        "alice": f"alice-{tag}",
        "bob": f"bob-{tag}",
        "home": f"home-{tag}",
        "other": f"other-{tag}",
    }
    await _write(
        client,
        tenant_id,
        headers,
        content=f"alice's row [{tag}]",
        agent_id=names["alice"],
        fleet_id=names["home"],
    )
    await _write(
        client,
        tenant_id,
        headers,
        content=f"bob's row [{tag}]",
        agent_id=names["bob"],
        fleet_id=names["other"],
    )
    private = await client.post(
        "/api/v1/memories",
        json={
            "tenant_id": tenant_id,
            "content": f"bob's private row [{tag}]",
            "agent_id": names["bob"],
            "fleet_id": names["other"],
            "memory_type": "fact",
            "visibility": "scope_agent",
        },
        headers=headers,
    )
    assert private.status_code == 201, private.text
    return tenant_id, names


async def _ask_both(client, tenant_id: str, agent_id: str, query: str):
    with _as_agent(tenant_id, agent_id):
        listed = await client.get(f"/api/v1/memories?tenant_id={tenant_id}&{query}")
        counted = await client.get(
            f"/api/v1/memories/stats?tenant_id={tenant_id}&{query}"
        )
    return listed, counted


@pytest.mark.parametrize("trust", [1, 2])
@pytest.mark.parametrize(
    ("case", "query", "refused_at_trust_1"),
    [(c, q, False) for c, q in _OPEN] + [(c, q, True) for c, q in _GATED],
    ids=[c for c, _ in _OPEN] + [c for c, _ in _GATED],
)
async def test_list_and_stats_answer_a_read_alike(
    client, sc, tenant, trust, case, query, refused_at_trust_1
):
    tenant_id, names = tenant
    await _set_trust(sc, tenant_id, names["alice"], names["home"], trust)

    listed, counted = await _ask_both(
        client, tenant_id, names["alice"], query.format(**names)
    )

    expected = 403 if refused_at_trust_1 and trust < 2 else 200
    assert (listed.status_code, counted.status_code) == (expected, expected), (
        f"{case}: list {listed.status_code} {listed.text[:200]} / "
        f"stats {counted.status_code} {counted.text[:200]}"
    )
    if expected == 200:
        assert counted.json()["total"] == len(listed.json()["items"]), case


async def test_a_peer_agent_id_never_reads_as_the_peer(client, sc, tenant):
    """The one case the two routes answer differently, pinned as such.

    On the list, ``agent_id`` is only the author filter; who is reading stays
    the authenticated agent. So naming a peer lists the peer's rows the CALLER
    may see, here bob's ``scope_org`` row, and never his private one. On stats,
    ``agent_id`` was also the visibility identity, which let a peer's private
    per-type counts through, so H-10 refuses an agent credential naming anyone
    but itself. Both keep the peer's view from the caller; making the routes
    agree is fine, but it should be a decision, not a side effect of a change to
    one of them.
    """
    tenant_id, names = tenant
    await _set_trust(sc, tenant_id, names["alice"], names["home"], 1)

    listed, counted = await _ask_both(
        client, tenant_id, names["alice"], f"agent_id={names['bob']}"
    )

    assert counted.status_code == 403, counted.text
    assert listed.status_code == 200, listed.text
    contents = {item["content"] for item in listed.json()["items"]}
    assert any(c.startswith("bob's row") for c in contents), contents
    assert not any(c.startswith("bob's private row") for c in contents), contents
