"""ENT2-013: stats and a filtered delete honour the filters the list honours.

``GET /memories`` filters by ``run_id``, a created window and
``exclude_memory_types``. ``/memories/stats`` and ``DELETE /memories`` declared
none of them, and FastAPI drops an undeclared query parameter silently. So a
count for a filtered view counted the coarse set, and a delete narrowed by fleet
and run removed the whole fleet. Each case is written so that the pre-fix answer
is the wrong one.
"""

from __future__ import annotations

import pytest

from tests.conftest import get_test_auth
from tests.conftest import uid as _uid

pytestmark = pytest.mark.integration

FUTURE = "2999-01-01T00:00:00Z"
PAST = "2000-01-01T00:00:00Z"
# Two rows in run-a (a fact and a decision) and one fact in run-b.
ROWS = (("run-a", "fact"), ("run-a", "decision"), ("run-b", "fact"))


async def _seed(client):
    """``ROWS``, in one fleet."""
    tenant_id, headers = get_test_auth(f"t-ent2-013-{_uid()}")
    fleet = f"f-{_uid()}"
    for run_id, memory_type in ROWS:
        resp = await client.post(
            "/api/v1/memories",
            json={
                "tenant_id": tenant_id,
                "fleet_id": fleet,
                "agent_id": "ent2-013-agent",
                "content": f"ENT2-013 {run_id} {memory_type} {_uid()}",
                "memory_type": memory_type,
                "run_id": run_id,
                "visibility": "scope_org",
            },
            headers=headers,
        )
        assert resp.status_code == 201, resp.text
    return tenant_id, fleet, headers


async def _total(client, tenant_id, headers, query):
    url = f"/api/v1/memories/stats?tenant_id={tenant_id}&{query}"
    resp = await client.get(url, headers=headers)
    assert resp.status_code == 200, resp.text
    return resp.json()["total"]


async def _left(client, tenant_id, headers):
    """``(run_id, memory_type)`` of the live rows, sorted."""
    url = f"/api/v1/memories?tenant_id={tenant_id}&limit=50"
    resp = await client.get(url, headers=headers)
    assert resp.status_code == 200, resp.text
    return sorted((m["run_id"], m["memory_type"]) for m in resp.json()["items"])


async def test_stats_count_only_the_run_asked_for(client):
    tenant_id, _, headers = await _seed(client)
    assert await _total(client, tenant_id, headers, "run_id=run-a") == 2


async def test_stats_honour_the_created_window_and_excluded_types(client):
    tenant_id, _, headers = await _seed(client)
    assert await _total(client, tenant_id, headers, f"created_after={FUTURE}") == 0
    assert await _total(client, tenant_id, headers, f"created_before={PAST}") == 0
    excluded = await _total(client, tenant_id, headers, "exclude_memory_types=decision")
    assert excluded == 2


async def test_a_delete_narrowed_by_fleet_and_run_takes_only_that_run(client):
    tenant_id, fleet, headers = await _seed(client)
    url = f"/api/v1/memories?tenant_id={tenant_id}&fleet_id={fleet}&run_id=run-a"
    resp = await client.delete(url, headers=headers)
    assert resp.status_code == 204, resp.text
    assert await _left(client, tenant_id, headers) == [("run-b", "fact")]


async def test_a_delete_honours_the_created_window_and_excluded_types(client):
    tenant_id, fleet, headers = await _seed(client)
    for query in (f"created_after={FUTURE}", f"created_before={PAST}"):
        url = f"/api/v1/memories?tenant_id={tenant_id}&fleet_id={fleet}&{query}"
        resp = await client.delete(url, headers=headers)
        assert resp.status_code == 204, resp.text
    assert len(await _left(client, tenant_id, headers)) == 3
    url = (
        f"/api/v1/memories?tenant_id={tenant_id}&fleet_id={fleet}"
        "&exclude_memory_types=fact"
    )
    resp = await client.delete(url, headers=headers)
    assert resp.status_code == 204, resp.text
    facts = [("run-a", "fact"), ("run-b", "fact")]
    assert await _left(client, tenant_id, headers) == facts


@pytest.mark.parametrize(
    "narrowing",
    [
        "run_id=run-a",
        f"created_after={FUTURE}",
        f"created_before={PAST}",
        "exclude_memory_types=fact",
    ],
)
async def test_each_new_filter_narrows_a_delete_on_its_own(client, narrowing):
    """Each one narrows the delete, so it needs no confirm_scope (C28)."""
    tenant_id, headers = get_test_auth(f"t-ent2-013-{_uid()}")
    url = f"/api/v1/memories?tenant_id={tenant_id}&{narrowing}"
    resp = await client.delete(url, headers=headers)
    assert resp.status_code == 204, f"{narrowing} was refused: {resp.text}"
