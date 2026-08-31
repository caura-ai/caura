"""``GET /reports/{report_id}`` must be told whose report it is returning.

The read half of the pair #1082 fixed the write half of (#1167). The route took a
bare UUID and ``report_get_by_id`` was ``session.get(CrystallizationReport,
report_id)`` — a primary-key fetch with no tenant predicate — so on a service
that authenticates no request (GHSA-wgvw-28pq-jc36), published by docker-compose
on 0.0.0.0:8002, knowing an id was enough to read the row.

What the row carries is the reason this is worth a fix rather than a note:
``tenant_id`` and ``fleet_id``, plus the ``summary``, ``hygiene``, ``health``,
``usage_data``, ``issues`` and ``crystallization`` blobs. That is a tenant's
memory-hygiene posture, its open issues and its usage figures in one response.
Disclosure only — nothing here writes — but it is the whole report, not a count.

Two things get pinned separately, because they fail for different reasons:

* **The predicate.** A stranger asking about someone else's report gets 404
  while the owner gets 200. Reverting ``CrystallizationReport.tenant_id ==
  tenant_id`` breaks this and nothing else here.
* **The requirement.** Omitting ``tenant_id`` is a 422, not a fallback to the
  old unscoped fetch. This is the half that a caller's forgetfulness reaches,
  and making the parameter optional would restore the hole while every test
  above kept passing.

And the 404 is the same 404 in both directions — foreign report and absent
report are indistinguishable — so the route cannot be used to test whether a
report id exists in some other tenant. That collapse is deliberate (audit
finding #22, where core-api makes the same one a layer up) and is asserted on
the response bodies, not just the status codes.
"""

from __future__ import annotations

import uuid

import pytest
from httpx import AsyncClient

from tests.test_integration import PREFIX

pytestmark = [pytest.mark.asyncio]


def _tenant() -> str:
    """A tenant id unique to one test and visible to the root suite's sweep.

    The ``test-tenant-`` prefix is not cosmetic: ``_setup_schema`` there cleans
    with ``tenant_id LIKE 'test-tenant-%'``, so a tenant minted with any other
    prefix is never reclaimed — which is how #858 left 9,186 rows behind.
    """
    return f"test-tenant-{uuid.uuid4().hex[:8]}"


async def _report(client: AsyncClient, tenant_id: str) -> str:
    resp = await client.post(
        f"{PREFIX}/reports",
        json={"tenant_id": tenant_id, "trigger": "manual"},
    )
    assert resp.status_code == 200, resp.text
    return resp.json()["id"]


class TestReportReadTenantScope:
    async def test_another_tenants_report_is_not_returned(self, client: AsyncClient) -> None:
        """The attack, stated as the request an attacker would send."""
        victim, attacker = _tenant(), _tenant()
        report_id = await _report(client, victim)

        theirs = await client.get(f"{PREFIX}/reports/{report_id}", params={"tenant_id": attacker})

        assert theirs.status_code == 404, theirs.text

    async def test_the_owner_still_reads_its_own_report(self, client: AsyncClient) -> None:
        """The other side of the same predicate, and not a formality.

        Asserted separately so the test above cannot pass because the fixture
        failed to create anything: a 404 for the attacker means nothing unless
        the owner gets a 200 for the same id.
        """
        owner = _tenant()
        report_id = await _report(client, owner)

        mine = await client.get(f"{PREFIX}/reports/{report_id}", params={"tenant_id": owner})

        assert mine.status_code == 200, mine.text
        body = mine.json()
        assert body["id"] == report_id
        assert body["tenant_id"] == owner
        # The blobs the disclosure was about are still served to their owner.
        for key in ("summary", "hygiene", "health", "issues", "crystallization"):
            assert key in body, f"missing {key!r} — the owner's own read lost a field"

    async def test_an_omitted_tenant_is_rejected(self, client: AsyncClient) -> None:
        """The bug: a bare UUID was the whole request.

        A 422 rather than a fallback. Optional-with-a-fallback is the shape this
        route's two siblings had — ``/entities/{id}/relations`` and
        ``/entities/{id}/with-memories`` scoped to the addressed row's OWN tenant
        when the parameter was omitted, which is a predicate satisfied by
        construction for whatever id the caller supplies.
        """
        owner = _tenant()
        report_id = await _report(client, owner)

        resp = await client.get(f"{PREFIX}/reports/{report_id}")

        assert resp.status_code == 422, resp.text
        assert "tenant_id" in resp.text

    async def test_a_foreign_id_is_indistinguishable_from_a_missing_one(self, client: AsyncClient) -> None:
        """Both 404 with the same body, so this is not an existence oracle."""
        victim, attacker = _tenant(), _tenant()
        report_id = await _report(client, victim)

        foreign = await client.get(f"{PREFIX}/reports/{report_id}", params={"tenant_id": attacker})
        missing = await client.get(f"{PREFIX}/reports/{uuid.uuid4()}", params={"tenant_id": attacker})

        assert foreign.status_code == missing.status_code == 404
        assert foreign.json() == missing.json()

    async def test_find_running_still_resolves_its_own_report(self, client: AsyncClient) -> None:
        """``GET /reports/running`` reaches the same method and must keep working.

        It is the second caller of ``report_get_by_id`` and the one that had a
        tenant all along — it obtains the id from the tenant-scoped
        ``report_find_running`` first. Covered here because "the scoped fetch
        broke the crystallization lock lookup" is a silent failure: a 404 from
        this route reads as "no run in flight", which releases the short-circuit
        that serialises crystallization.
        """
        owner = _tenant()
        report_id = await _report(client, owner)

        resp = await client.get(f"{PREFIX}/reports/running", params={"tenant_id": owner})

        assert resp.status_code == 200, resp.text
        assert resp.json()["id"] == report_id
