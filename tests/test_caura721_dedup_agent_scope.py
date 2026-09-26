"""CAURA-721 — the semantic dedup gate is scoped to the writing agent.

The defect: ``memory_find_semantic_duplicate`` filtered on ``(tenant, fleet)``
while the exact-hash gate beside it is keyed on
``(tenant, fleet, agent, content_hash)``. So Caura *admitted* an exact
cross-agent duplicate inside a fleet and then *refused* a paraphrase of it —
the semantic tier was the only gate disagreeing with the rule
``uq_memories_live_content_hash`` states outright: "two agents recording
identical content are two independent observations".

Why that was loss rather than deduplication: reads narrow by
``filter_agent_id``, so the refused agent could not retrieve the row that
replaced its write. For a ``scope_agent`` candidate the 409 additionally
returned the id, status and similarity of a row the caller has no right to
read.

Paths that reach the 409: ``write_mode=strong`` and the auto-chunk branch
(both via ``CheckSemanticDuplicate``), plus the content-change gate on update.
``write_mode=fast`` only tags ``near_duplicate_of`` advisorily, and
``create_memories_bulk`` skips the semantic tier altogether — so this is
deliberately tested through ``strong``.

The load-bearing test here is
``test_one_agent_restating_its_own_fact_is_still_refused``: pinning the owner
must not switch dedup off for the single-agent case, which is nearly all real
traffic.
"""

import hashlib
import uuid
from unittest.mock import AsyncMock, patch

import pytest

from common.embedding import fake_embedding
from core_api.services.memory_service import _find_semantic_duplicate
from tests.conftest import get_test_auth, new_tenant_id

FLEET = "caura721-fleet"
CONTENT = "The Q3 planning session concluded that hiring is paused until January."


def _content_hash(tenant_id: str, fleet_id: str | None, content: str) -> str:
    return hashlib.sha256(
        f"{tenant_id}:{fleet_id or ''}:{content}".encode()
    ).hexdigest()


async def _seed(tenant_id: str, *, agent_id: str, fleet_id: str | None, content: str):
    """Insert a row straight through the storage client (no write pipeline)."""
    from core_api.clients.storage_client import get_storage_client

    return await get_storage_client().create_memory(
        {
            "tenant_id": tenant_id,
            "fleet_id": fleet_id,
            "agent_id": agent_id,
            "memory_type": "fact",
            "content": content,
            "weight": 0.5,
            "embedding": fake_embedding(content),
            "content_hash": _content_hash(tenant_id, fleet_id, content),
            "status": "active",
        }
    )


async def _write(
    client,
    headers,
    tenant_id,
    *,
    agent_id,
    fleet_id,
    content,
    visibility,
    write_mode="strong",
):
    return await client.post(
        "/api/v1/memories",
        headers=headers,
        json={
            "tenant_id": tenant_id,
            "agent_id": agent_id,
            "fleet_id": fleet_id,
            "content": content,
            "visibility": visibility,
            "write_mode": write_mode,
        },
    )


# ---------------------------------------------------------------------------
# The lookup itself
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_another_agents_row_no_longer_refuses_the_write(tenant_id):
    """The defect, at the lookup: agent-b's write vs agent-a's row, one fleet."""
    await _seed(tenant_id, agent_id="agent-a", fleet_id=FLEET, content=CONTENT)

    dup = await _find_semantic_duplicate(
        tenant_id, FLEET, fake_embedding(CONTENT), agent_id="agent-b"
    )
    assert dup is None, "a row owned by another agent must not refuse this write"


@pytest.mark.integration
async def test_one_agent_restating_its_own_fact_is_still_refused(tenant_id):
    """The over-fix guard. Pinning the owner must not disable dedup.

    If this ever fails, CAURA-721 has turned the gate off for the single-agent
    case — which is nearly all real traffic — rather than narrowing it.
    """
    await _seed(tenant_id, agent_id="agent-a", fleet_id=FLEET, content=CONTENT)

    dup = await _find_semantic_duplicate(
        tenant_id, FLEET, fake_embedding(CONTENT), agent_id="agent-a"
    )
    assert dup is not None, "an agent restating its own fact must still be caught"
    assert dup["content"] == CONTENT


@pytest.mark.integration
async def test_omitting_the_owner_keeps_the_pre_caura721_behaviour(tenant_id):
    """``agent_id=None`` means "do not pin" — what every un-migrated caller gets."""
    await _seed(tenant_id, agent_id="agent-a", fleet_id=FLEET, content=CONTENT)

    dup = await _find_semantic_duplicate(tenant_id, FLEET, fake_embedding(CONTENT))
    assert dup is not None, "omitting the owner must not narrow the lookup"


@pytest.mark.integration
async def test_the_owner_does_not_widen_past_the_fleet(tenant_id):
    """Pinning the owner is an ADDITIONAL predicate, not a replacement.

    Same agent, different fleet, must still not match — otherwise the fix would
    have traded a too-wide agent scope for a too-wide fleet scope.
    """
    await _seed(tenant_id, agent_id="agent-a", fleet_id="fleet-a", content=CONTENT)

    dup = await _find_semantic_duplicate(
        tenant_id, "fleet-b", fake_embedding(CONTENT), agent_id="agent-a"
    )
    assert dup is None


# ---------------------------------------------------------------------------
# The wire body
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_the_owner_reaches_the_storage_payload():
    mock_sc = AsyncMock()
    mock_sc.find_semantic_duplicate = AsyncMock(return_value=None)

    with patch(
        "core_api.services.memory_service.get_storage_client", return_value=mock_sc
    ):
        await _find_semantic_duplicate("t", "f", [0.0], agent_id="agent-a")

    assert mock_sc.find_semantic_duplicate.call_args[0][0]["agent_id"] == "agent-a"


@pytest.mark.unit
async def test_no_owner_leaves_the_body_byte_identical():
    """An unpinned call must not start sending ``agent_id: null``.

    A storage older than CAURA-721 ignores unknown keys, so this is about the
    contract rather than a crash: a caller that pins nothing should produce
    exactly the body it produced before.
    """
    mock_sc = AsyncMock()
    mock_sc.find_semantic_duplicate = AsyncMock(return_value=None)

    with patch(
        "core_api.services.memory_service.get_storage_client", return_value=mock_sc
    ):
        await _find_semantic_duplicate("t", "f", [0.0])

    assert "agent_id" not in mock_sc.find_semantic_duplicate.call_args[0][0]


# ---------------------------------------------------------------------------
# End to end, through the route that actually 409s
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.parametrize("visibility", ["scope_team", "scope_agent"])
async def test_two_agents_in_one_fleet_can_both_record_the_same_fact(
    client, visibility
):
    tenant_id, headers = get_test_auth(new_tenant_id())

    a = await _write(
        client,
        headers,
        tenant_id,
        agent_id="agent-a",
        fleet_id=FLEET,
        content=CONTENT,
        visibility=visibility,
    )
    assert a.status_code == 201, a.text

    b = await _write(
        client,
        headers,
        tenant_id,
        agent_id="agent-b",
        fleet_id=FLEET,
        content=CONTENT,
        visibility=visibility,
    )
    assert b.status_code == 201, b.text
    assert b.json()["id"] != a.json()["id"], "two independent observations, two rows"


@pytest.mark.integration
async def test_an_agent_rewriting_its_own_fact_is_still_refused_over_rest(client):
    """The end-to-end half of the over-fix guard.

    Uses a paraphrase-free restatement with a distinct content hash, so the
    refusal can only come from the SEMANTIC tier — an identical body would be
    caught by the exact-hash gate and prove nothing about this change.
    """
    tenant_id, headers = get_test_auth(new_tenant_id())

    a = await _write(
        client,
        headers,
        tenant_id,
        agent_id="agent-a",
        fleet_id=FLEET,
        content=CONTENT,
        visibility="scope_team",
    )
    assert a.status_code == 201, a.text

    again = await _write(
        client,
        headers,
        tenant_id,
        agent_id="agent-a",
        fleet_id=FLEET,
        content=CONTENT + " ",
        visibility="scope_team",
    )
    assert again.status_code == 409, again.text
    assert again.json()["error"]["details"]["reason"] == "semantic_similarity"


@pytest.mark.integration
async def test_the_second_agents_row_is_retrievable_by_that_agent(client, monkeypatch):
    """The point of the fix: the write survives AND its author can read it.

    Before CAURA-721 agent-b was refused and then, searching with
    ``filter_agent_id=agent-b``, got zero rows — the fact was gone, not shared.
    """
    from core_api.services import memory_service

    monkeypatch.setattr(memory_service, "_USE_PIPELINE_SEARCH", True)
    tenant_id, headers = get_test_auth(new_tenant_id())

    await _write(
        client,
        headers,
        tenant_id,
        agent_id="agent-a",
        fleet_id=FLEET,
        content=CONTENT,
        visibility="scope_team",
    )
    b = await _write(
        client,
        headers,
        tenant_id,
        agent_id="agent-b",
        fleet_id=FLEET,
        content=CONTENT,
        visibility="scope_team",
    )
    assert b.status_code == 201, b.text

    resp = await client.post(
        "/api/v1/search",
        headers=headers,
        json={
            "tenant_id": tenant_id,
            "query": "Q3 planning hiring paused January",
            "fleet_ids": [FLEET],
            "filter_agent_id": "agent-b",
            "top_k": 5,
        },
    )
    assert resp.status_code == 200, resp.text
    assert b.json()["id"] in [i["id"] for i in resp.json()["items"]]


@pytest.mark.integration
async def test_an_edit_is_not_refused_by_another_agents_row(client):
    """The update path's content-change gate pins the edited row's own owner."""
    tenant_id, headers = get_test_auth(new_tenant_id())

    a = await _write(
        client,
        headers,
        tenant_id,
        agent_id="agent-a",
        fleet_id=FLEET,
        content=CONTENT,
        visibility="scope_team",
    )
    assert a.status_code == 201, a.text

    other = f"Bob switched the deploy window to Thursdays. {uuid.uuid4().hex[:8]}"
    b = await _write(
        client,
        headers,
        tenant_id,
        agent_id="agent-b",
        fleet_id=FLEET,
        content=other,
        visibility="scope_team",
    )
    assert b.status_code == 201, b.text

    # Edit agent-b's row into agent-a's content. Allowed: different owners.
    resp = await client.patch(
        f"/api/v1/memories/{b.json()['id']}",
        headers=headers,
        params={"tenant_id": tenant_id},
        json={"content": CONTENT},
    )
    assert resp.status_code == 200, resp.text
