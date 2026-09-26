"""08/14 L-30 + 09/02 L-48 — two report-lookup parameters that filtered nothing.

``GET /reports/running`` and ``GET /reports/latest`` both declared ``report_type``
and ``/latest`` also declared ``fleet_id``. core-api's storage client forwarded
all of them and ``_reserve_report`` even wrote ``"report_type":
"crystallization"`` into the create body. Nothing filtered on either:
``analysis_reports`` has no ``report_type`` column at all, so ``report_add``'s
``_filter_fields`` dropped it on insert, and ``report_get_latest_completed``
took only ``tenant_id``.

The two are not equally serious and are fixed in opposite directions.

* ``report_type`` is **removed**. There is one report type, the model is named
  after it, and no column exists to hold it — the parameter only ever
  advertised a scope that was never applied.
* ``fleet_id`` is **implemented**, because it has a caller that depends on it.
  ``_type_ii_watermark`` reads this route's ``completed_at`` to decide which
  subjects the nightly Type-II sweep may skip. Answered with a *different*
  fleet's more recent run, a fleet whose own sweep is older skips subjects that
  have changed since — and that helper's own docstring calls skipping a changed
  subject the unrecoverable direction.

Absent ``fleet_id`` still means ANY fleet here, unlike the sibling
``/reports/running`` where absent means ``IS NULL``. The asymmetry is
deliberate and is pinned below: this route also backs ``GET /crystallize/latest``,
a user-facing "show me my most recent report" with no fleet concept in its API,
which filtering to ``IS NULL`` would 404 for every tenant that only ever runs
fleet-scoped crystallization.
"""

from __future__ import annotations

import tokenize
import uuid
from pathlib import Path

import pytest
from httpx import AsyncClient

from tests.test_integration import PREFIX

# No module-level ``pytest.mark.asyncio``, unlike the sibling report tests:
# ``asyncio_mode = auto`` already covers the async cases, and this file mixes in
# two that touch no database — marking those async only to satisfy a blanket
# mark trips ASYNC240 on their ``Path`` reads.


def _tenant() -> str:
    """See ``test_report_read_tenant_scope`` — the prefix is what the root
    suite's sweep reclaims by."""
    return f"test-tenant-{uuid.uuid4().hex[:8]}"


async def _completed(client: AsyncClient, tenant_id: str, fleet_id: str | None) -> str:
    """A completed report, created now.

    ``started_at`` comes from the column's ``now()`` default rather than a
    supplied value, so reports created in sequence order by creation — which is
    what ``ORDER BY started_at DESC`` reads and what makes "the sibling fleet
    ran more recently" reproducible without freezing a clock.
    """
    resp = await client.post(
        f"{PREFIX}/reports",
        json={
            "tenant_id": tenant_id,
            "fleet_id": fleet_id,
            "trigger": "manual",
            "status": "completed",
        },
    )
    assert resp.status_code == 200, resp.text
    return resp.json()["id"]


class TestLatestReportHonoursFleet:
    async def test_a_newer_sibling_fleet_does_not_answer_for_this_fleet(self, client: AsyncClient) -> None:
        """The whole defect: fleet A asks for its own clock and gets fleet B's.

        B is created second, so it is the tenant's latest by ``started_at`` and
        is what the unfiltered query returned. As the Type-II watermark that
        makes A skip every subject changed since B's run.
        """
        tenant = _tenant()
        fleet_a = await _completed(client, tenant, "fleet-a")
        await _completed(client, tenant, "fleet-b")

        resp = await client.get(
            f"{PREFIX}/reports/latest", params={"tenant_id": tenant, "fleet_id": "fleet-a"}
        )

        assert resp.status_code == 200, resp.text
        assert resp.json()["id"] == fleet_a
        assert resp.json()["fleet_id"] == "fleet-a"

    async def test_a_fleet_with_no_report_of_its_own_gets_nothing(self, client: AsyncClient) -> None:
        """404 rather than a sibling's row. The caller turns this into "scan
        everything", which is the safe direction; a sibling's row would not be."""
        tenant = _tenant()
        await _completed(client, tenant, "fleet-b")

        resp = await client.get(
            f"{PREFIX}/reports/latest", params={"tenant_id": tenant, "fleet_id": "fleet-a"}
        )

        assert resp.status_code == 404, resp.text

    async def test_without_a_fleet_the_route_still_answers_across_fleets(self, client: AsyncClient) -> None:
        """Non-regression guard, not evidence of the fix.

        ``GET /crystallize/latest`` calls this with a tenant and nothing else.
        Had absent ``fleet_id`` been made to mean ``IS NULL`` — matching the
        sibling route — that endpoint would 404 for every tenant whose runs are
        all fleet-scoped.
        """
        tenant = _tenant()
        await _completed(client, tenant, "fleet-a")
        newest = await _completed(client, tenant, "fleet-b")

        resp = await client.get(f"{PREFIX}/reports/latest", params={"tenant_id": tenant})

        assert resp.status_code == 200, resp.text
        assert resp.json()["id"] == newest


class TestReportTypeIsGone:
    def test_the_column_it_was_written_to_never_existed(self) -> None:
        """``report_add`` filters the body to real columns, so the create body's
        ``report_type`` was accepted and dropped — no error, no column, no
        filter. Asserted against the model rather than a migration, because the
        model is what ``_filter_fields`` reads."""
        from common.models.analysis_report import CrystallizationReport

        columns = {c.key for c in CrystallizationReport.__table__.columns}
        assert "report_type" not in columns

    @pytest.mark.parametrize("route", ["running", "latest"])
    async def test_an_older_caller_still_sending_it_is_unaffected(
        self, client: AsyncClient, route: str
    ) -> None:
        """Removing a query parameter is only safe because FastAPI ignores
        unknown ones. A 422 here would mean a deploy ordering hazard: a core-api
        still sending ``?report_type=`` would break against new storage."""
        tenant = _tenant()
        await _completed(client, tenant, None)

        resp = await client.get(
            f"{PREFIX}/reports/{route}",
            params={"tenant_id": tenant, "report_type": "crystallization"},
        )

        assert resp.status_code != 422, resp.text

    def test_no_layer_still_names_it(self) -> None:
        """The parameter spanned three layers — route, storage client, caller —
        and a leftover in any one of them is the same dead filter in a smaller
        place. Pinned as a sweep because that is the failure mode: a removal
        that stops one layer short.

        Tokenised rather than grepped: every one of those files now *documents*
        the removal, so a line-wise search matches the explanations and reports
        the fix as the defect. Only NAME tokens count — strings and comments are
        where it is supposed to appear from here on.
        """
        root = Path(__file__).resolve().parents[2]
        layers = [
            "core-storage-api/src/core_storage_api/routers/reports.py",
            "core-api/src/core_api/clients/storage_client.py",
            "core-api/src/core_api/services/crystallizer_service.py",
        ]
        offenders = []
        for layer in layers:
            with (root / layer).open("rb") as handle:
                for token in tokenize.tokenize(handle.readline):
                    if token.type == tokenize.NAME and token.string == "report_type":
                        offenders.append(f"{layer}:{token.start[0]}")
        assert not offenders, f"report_type still reaches code here: {offenders}"
