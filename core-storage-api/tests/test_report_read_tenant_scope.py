"""``GET /reports/{report_id}`` must be told which tenant's report it returns.

The read half of the pair whose write half is ``test_report_update_tenant_scope``
(#1082). The route took a bare UUID and ``report_get_by_id`` was
``session.get(CrystallizationReport, report_id)`` --- a primary-key fetch with no
tenant predicate --- so a caller who knew an id read the row behind it through a
service that authenticates nothing.

What that row holds is the point. A crystallization report carries
``tenant_id`` and ``fleet_id`` plus six JSONB blobs: ``summary``, ``hygiene``,
``health``, ``usage_data``, ``issues`` and ``crystallization``. Together they are
a standing description of another tenant's memory estate --- its health scores,
its hygiene problems, its token spend, and an enumerated issue list. The write
sibling could substitute that content; this one hands it over.

``tenant_id`` is required now --- 422 when absent, rather than falling back to
"fetch by primary key" --- and is part of the SELECT predicate, so another
tenant's report is indistinguishable from one that does not exist.
"""

from __future__ import annotations

import uuid

import pytest
from httpx import AsyncClient

from tests.test_integration import PREFIX

pytestmark = [pytest.mark.asyncio]


async def _report(client: AsyncClient, tenant_id: str) -> str:
    resp = await client.post(
        f"{PREFIX}/reports",
        json={"tenant_id": tenant_id, "trigger": "manual"},
    )
    assert resp.status_code == 200, resp.text
    return resp.json()["id"]


class TestReportReadTenantScope:
    async def test_another_tenants_report_is_not_readable(self, client: AsyncClient) -> None:
        """The attack, stated as the request an attacker would send."""
        victim = f"rep-victim-{uuid.uuid4().hex[:8]}"
        attacker = f"rep-attacker-{uuid.uuid4().hex[:8]}"
        report_id = await _report(client, victim)

        resp = await client.get(f"{PREFIX}/reports/{report_id}", params={"tenant_id": attacker})

        assert resp.status_code == 404, resp.text
        # Not merely "no 200": the disclosure is the body, so pin that none of
        # it came back. A future regression that 404s but still serialises the
        # row into the error detail would pass a status-only assertion.
        assert victim not in resp.text

    async def test_omitted_tenant_id_is_rejected(self, client: AsyncClient) -> None:
        """No tenant used to mean "fetch by primary key"; it is now a 422."""
        tenant = f"rep-{uuid.uuid4().hex[:8]}"
        report_id = await _report(client, tenant)

        resp = await client.get(f"{PREFIX}/reports/{report_id}")

        assert resp.status_code == 422, resp.text
        assert "tenant_id" in resp.text

    async def test_own_report_is_readable(self, client: AsyncClient) -> None:
        """The supported call still works."""
        tenant = f"rep-{uuid.uuid4().hex[:8]}"
        report_id = await _report(client, tenant)

        resp = await client.get(f"{PREFIX}/reports/{report_id}", params={"tenant_id": tenant})

        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["id"] == report_id
        assert body["tenant_id"] == tenant
        assert body["status"] == "running"

    async def test_a_foreign_id_is_indistinguishable_from_a_missing_one(self, client: AsyncClient) -> None:
        """Both 404, so the endpoint is not an existence oracle for report UUIDs.

        This is the storage-level half of audit finding #22. The caller-facing
        route in ``core-api`` keeps the same property for its own callers; the
        403 it can raise is keyed on the named tenant rather than on
        ``report_id``, and ``tests/test_api_crystallizer.py`` pins that matrix.
        """
        victim = f"rep-victim-{uuid.uuid4().hex[:8]}"
        attacker = f"rep-attacker-{uuid.uuid4().hex[:8]}"
        report_id = await _report(client, victim)

        params = {"tenant_id": attacker}
        foreign = await client.get(f"{PREFIX}/reports/{report_id}", params=params)
        missing = await client.get(f"{PREFIX}/reports/{uuid.uuid4()}", params=params)

        assert foreign.status_code == missing.status_code == 404
        assert foreign.json() == missing.json()

    async def test_the_running_report_lookup_still_resolves(self, client: AsyncClient) -> None:
        """``GET /reports/running`` chains through the method this PR scoped.

        It calls ``report_find_running`` and then ``report_get_by_id`` with the
        id that came back. The second call gained a tenant argument; if it were
        threaded wrong, the pair would 404 on a report the first half had just
        found --- a break that no test above would see, because they all address
        the route by id.
        """
        tenant = f"rep-{uuid.uuid4().hex[:8]}"
        report_id = await _report(client, tenant)

        resp = await client.get(f"{PREFIX}/reports/running", params={"tenant_id": tenant})

        assert resp.status_code == 200, resp.text
        assert resp.json()["id"] == report_id
