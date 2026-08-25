"""``DELETE /memories`` must not wipe a tenant by omission — SAFE-03 / C28.

The endpoint deletes every memory matching its filters. With no ``fleet_id``
and no other narrowing filter, "matching" means the whole tenant — and that
case was reachable by leaving one query parameter off an otherwise ordinary
fleet-scoped call. Nothing in the request distinguished "clear this fleet" from
"clear everything", so the two could not be told apart by a gate, a log, or an
audit row.

Agent credentials have been gated at trust >= 3 since the BFLA fix. Tenant and
user credentials were not gated at all, by design — it is the dashboard's
"reset workspace". The fix is therefore a statement of intent rather than a
permission: the unbounded call requires ``confirm_scope=tenant``, and every
narrowed call is untouched.

That last property is most of what these tests are for. A safety gate that also
breaks ordinary filtered deletes would be reverted within a day, so each
narrowing input is checked independently.
"""

from __future__ import annotations

import uuid

import pytest

from tests.conftest import get_test_auth

pytestmark = pytest.mark.integration


def _tenant() -> tuple[str, dict]:
    return get_test_auth(f"t-c28-{uuid.uuid4().hex[:8]}")


async def test_unbounded_delete_is_refused_without_confirmation(client) -> None:
    tenant_id, headers = _tenant()
    resp = await client.delete(f"/api/v1/memories?tenant_id={tenant_id}", headers=headers)
    assert resp.status_code == 400, resp.text
    detail = resp.json()["detail"]
    # The message has to name the parameter and the way out, or the caller's
    # next move is to guess.
    assert "confirm_scope=tenant" in detail
    assert "every memory" in detail.lower()


async def test_unbounded_delete_proceeds_when_confirmed(client) -> None:
    tenant_id, headers = _tenant()
    resp = await client.delete(
        f"/api/v1/memories?tenant_id={tenant_id}&confirm_scope=tenant", headers=headers
    )
    assert resp.status_code == 204, resp.text


async def test_a_wrong_confirmation_value_is_not_a_confirmation(client) -> None:
    """``confirm_scope=true`` is the plausible guess, and it must not work."""
    tenant_id, headers = _tenant()
    resp = await client.delete(
        f"/api/v1/memories?tenant_id={tenant_id}&confirm_scope=true", headers=headers
    )
    assert resp.status_code == 400, resp.text


@pytest.mark.parametrize(
    "narrowing",
    [
        "fleet_id=f-c28",
        "agent_id=a-c28",
        "memory_type=fact",
        "status=active",
    ],
)
async def test_any_narrowing_filter_keeps_the_old_behaviour(client, narrowing: str) -> None:
    """Each filter is checked on its own: one missing from the guard's list
    would silently reclassify a narrowed delete as a tenant wipe."""
    tenant_id, headers = _tenant()
    resp = await client.delete(
        f"/api/v1/memories?tenant_id={tenant_id}&{narrowing}", headers=headers
    )
    assert resp.status_code == 204, f"{narrowing} was refused: {resp.text}"


@pytest.mark.parametrize(
    "body",
    [
        {"exclude_ids": ["11111111-1111-4111-8111-111111111111"]},
        {"metadata_filter": {"load_test_run_id": "r-1"}},
    ],
)
async def test_body_side_narrowing_also_counts(client, body: dict) -> None:
    """``exclude_ids`` and ``metadata_filter`` arrive in the body, not the
    query — the guard reads both, and the load-test harness depends on the
    second one still working unconfirmed."""
    tenant_id, headers = _tenant()
    resp = await client.request(
        "DELETE",
        f"/api/v1/memories?tenant_id={tenant_id}",
        json=body,
        headers=headers,
    )
    assert resp.status_code == 204, resp.text


async def test_a_refused_call_deletes_nothing(client) -> None:
    """The guard must run before the delete, not alongside it.

    A gate that returns 400 *after* issuing the soft-delete would pass every
    other test in this file — the status code is right, the message is right —
    while doing exactly the damage it exists to prevent. So this writes a row,
    gets refused, and checks the row survived.
    """
    tenant_id, headers = get_test_auth(f"t-c28-{uuid.uuid4().hex[:8]}")
    written = await client.post(
        "/api/v1/memories",
        json={"tenant_id": tenant_id, "content": "c28 survivor", "agent_id": "a-c28"},
        headers=headers,
    )
    assert written.status_code == 201, written.text
    memory_id = written.json()["id"]

    refused = await client.delete(f"/api/v1/memories?tenant_id={tenant_id}", headers=headers)
    assert refused.status_code == 400, refused.text

    after = await client.get(f"/api/v1/memories/{memory_id}?tenant_id={tenant_id}", headers=headers)
    assert after.status_code == 200, "the refused delete removed the row anyway"
    assert after.json()["status"] != "deleted"
