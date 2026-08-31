"""E2E crystallizer (memory analysis) tests through HTTP API."""

from tests.conftest import get_test_auth, get_admin_headers, uid as _uid


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _write_memory(client, tenant_id, headers, content):
    """Write a memory so crystallization has something to analyse."""
    tag = _uid()
    resp = await client.post(
        "/api/v1/memories",
        json={
            "tenant_id": tenant_id,
            "content": f"{content} [{tag}]",
            "agent_id": f"cryst-agent-{tag}",
            "fleet_id": f"cryst-fleet-{tag}",
            "memory_type": "fact",
        },
        headers=headers,
    )
    assert resp.status_code == 201, f"Write memory failed: {resp.text}"
    return resp.json()


async def _crystallize(client, tenant_id, headers):
    """Trigger crystallization; returns (status_code, data).

    Crystallization processes all existing memories for the tenant, so it may
    return 409 on repeated runs if the DB already has crystal summaries from
    a prior run.  Callers should handle both 200 and 409.
    """
    resp = await client.post(
        "/api/v1/crystallize",
        json={"tenant_id": tenant_id},
        headers=headers,
    )
    return resp.status_code, resp.json()


# ---------------------------------------------------------------------------
# POST /api/crystallize — trigger for a single tenant
# ---------------------------------------------------------------------------


async def test_trigger_crystallization(client):
    """POST /api/crystallize returns a report_id and status='running'."""
    tenant_id, headers = get_test_auth()
    await _write_memory(client, tenant_id, headers, "Crystallize test fact")

    code, data = await _crystallize(client, tenant_id, headers)
    # 200 on clean DB, 409 if crystal summaries already exist
    assert code in (200, 409), f"Unexpected status {code}: {data}"
    if code == 200:
        assert "report_id" in data
        assert data["status"] == "running"


# ---------------------------------------------------------------------------
# POST /api/crystallize/all — admin only
# ---------------------------------------------------------------------------


async def test_trigger_crystallize_all_as_admin(client):
    """POST /api/crystallize/all with admin key succeeds."""
    tenant_id, auth_headers = get_test_auth()
    admin_headers = get_admin_headers()

    await _write_memory(client, tenant_id, auth_headers, "Crystallize-all test")

    resp = await client.post(
        "/api/v1/crystallize/all",
        headers=admin_headers,
    )
    # 200 on clean DB, 409 if crystal summaries already exist
    assert resp.status_code in (200, 409), f"Unexpected: {resp.text}"
    if resp.status_code == 200:
        data = resp.json()
        assert "reports" in data
        assert isinstance(data["reports"], list)


async def test_crystallize_all_non_admin_forbidden(client):
    """POST /api/crystallize/all without admin key returns 403."""
    resp = await client.post(
        "/api/v1/crystallize/all",
        headers={"X-Tenant-ID": "some-tenant"},
    )
    assert resp.status_code == 403


# ---------------------------------------------------------------------------
# GET /api/crystallize/reports — list reports
# ---------------------------------------------------------------------------


async def test_list_reports(client):
    """GET /api/crystallize/reports returns a list for the tenant."""
    tenant_id, headers = get_test_auth()

    # Trigger one so there's at least one report
    await _write_memory(client, tenant_id, headers, "Report list test")
    await _crystallize(client, tenant_id, headers)

    resp = await client.get(
        f"/api/v1/crystallize/reports?tenant_id={tenant_id}",
        headers=headers,
    )
    assert resp.status_code == 200
    reports = resp.json()
    assert isinstance(reports, list)
    assert len(reports) >= 1
    report = reports[0]
    assert "id" in report
    assert "tenant_id" in report
    assert "status" in report
    assert "trigger" in report


# ---------------------------------------------------------------------------
# GET /api/crystallize/reports/{id} — get report details
# ---------------------------------------------------------------------------


async def test_get_report_by_id(client):
    """GET /api/crystallize/reports/{id} returns full report details."""
    tenant_id, headers = get_test_auth()

    await _write_memory(client, tenant_id, headers, "Report detail unique test")
    await _crystallize(client, tenant_id, headers)

    # Get report ID from the reports list (reliable regardless of crystallize outcome)
    list_resp = await client.get(
        f"/api/v1/crystallize/reports?tenant_id={tenant_id}&limit=1",
        headers=headers,
    )
    assert list_resp.status_code == 200
    reports = list_resp.json()
    assert len(reports) >= 1
    report_id = reports[0]["id"]

    resp = await client.get(
        f"/api/v1/crystallize/reports/{report_id}?tenant_id={tenant_id}",
        headers=headers,
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["id"] == report_id
    assert data["tenant_id"] == tenant_id
    for key in ("summary", "hygiene", "health", "issues", "crystallization"):
        assert key in data, f"Missing key '{key}' in report detail"


async def test_get_report_not_found(client):
    """GET /api/crystallize/reports/{id} returns 404 for non-existent report."""
    tenant_id, headers = get_test_auth()
    fake_id = "00000000-0000-0000-0000-000000000000"
    resp = await client.get(
        f"/api/v1/crystallize/reports/{fake_id}?tenant_id={tenant_id}",
        headers=headers,
    )
    assert resp.status_code == 404


async def test_get_report_foreign_tenant_returns_404_not_403():
    """A non-admin caller probing a report owned by a different tenant
    must see 404 (same as a missing report), not 403 — otherwise an
    attacker could enumerate report_ids across tenants by distinguishing
    the two status codes (audit finding #22).

    The shape of this changed when storage stopped answering to a bare
    primary key. The caller now names a tenant, and the only tenant it may
    name is its own — so the way to reach another tenant's report is to ask
    for it under YOUR tenant, and storage's predicate misses. The row never
    leaves storage, where before it was fetched and then discarded here.
    """
    from unittest.mock import AsyncMock, patch
    from uuid import uuid4

    from fastapi import HTTPException

    from core_api.auth import AuthContext
    from core_api.routes.crystallizer import get_report

    caller = AuthContext(tenant_id="tenant-B", is_admin=False)

    sc_mock = AsyncMock()
    # Storage is scoped: asked for tenant-B, a tenant-A report is not a row.
    sc_mock.get_report = AsyncMock(return_value=None)
    with patch("core_api.routes.crystallizer.get_storage_client", return_value=sc_mock):
        try:
            await get_report(report_id=uuid4(), tenant_id="tenant-B", auth=caller)
        except HTTPException as e:
            assert e.status_code == 404, (
                f"Foreign-tenant report read must surface as 404; got {e.status_code}"
            )
            assert e.detail == "Report not found"
        else:
            raise AssertionError(
                "Expected HTTPException(404) for foreign-tenant report"
            )
    # The tenant reached storage: without it the fetch is by primary key again
    # and the 404 above would be answering a question nobody scoped.
    assert sc_mock.get_report.await_args.args[1] == "tenant-B"


async def test_get_report_403_is_keyed_on_the_tenant_not_the_report():
    """The oracle contract of audit finding #22, stated as its whole matrix.

    Requiring a ``tenant_id`` introduces a 403 on a path that previously had
    only 404s, and a 403 that varied with ``report_id`` would be exactly the
    enumeration oracle finding #22 closed. It does not vary:
    ``enforce_readable_tenant`` runs BEFORE the fetch and sees only the named
    tenant, so an unreadable tenant answers 403 for every id — including ids
    that do not exist anywhere — and every question about a report's existence
    still collapses to 404.

    What the 403 discloses is "this credential may not read tenant T", which
    the caller already knew about its own key.
    """
    from unittest.mock import AsyncMock, patch
    from uuid import uuid4

    from fastapi import HTTPException

    from core_api.auth import AuthContext
    from core_api.routes.crystallizer import get_report

    caller = AuthContext(tenant_id="tenant-B", is_admin=False)
    sc_mock = AsyncMock()
    sc_mock.get_report = AsyncMock(return_value=None)

    async def _status(tenant_id: str, report_id) -> int:
        with patch("core_api.routes.crystallizer.get_storage_client", return_value=sc_mock):
            try:
                await get_report(report_id=report_id, tenant_id=tenant_id, auth=caller)
            except HTTPException as e:
                return e.status_code
        return 200

    real_looking = uuid4()
    nonexistent = uuid4()

    # Unreadable tenant: same 403 whichever id is named, so the id is not
    # being answered. This is the assertion that makes it not an oracle.
    assert await _status("tenant-A", real_looking) == 403
    assert await _status("tenant-A", nonexistent) == 403
    # ...and it fires before storage is consulted at all.
    assert sc_mock.get_report.await_count == 0

    # Readable tenant: absent rows are 404, as they always were.
    assert await _status("tenant-B", nonexistent) == 404


def _report_payload(tenant_id: str) -> dict:
    from uuid import uuid4

    return {
        "id": str(uuid4()),
        "tenant_id": tenant_id,
        "fleet_id": None,
        "trigger": "manual",
        "status": "completed",
        "summary": {},
        "hygiene": {},
        "health": {},
        "issues": [],
        "crystallization": {},
    }


async def test_get_report_foreign_tenant_admin_bypass():
    """Admin keys keep their cross-tenant read ability — by NAMING the tenant.

    An admin ``AuthContext`` has ``tenant_id=None``, so there is no tenant to
    infer and storage now demands one. The capability is unchanged; what changed
    is that the admin says which tenant it is reading, instead of the ability
    falling out of an unscoped fetch. That is the whole of the API change this
    endpoint takes.
    """
    from unittest.mock import AsyncMock, patch
    from uuid import uuid4

    from core_api.auth import AuthContext
    from core_api.routes.crystallizer import get_report

    admin = AuthContext(tenant_id=None, is_admin=True)

    sc_mock = AsyncMock()
    sc_mock.get_report = AsyncMock(return_value=_report_payload("tenant-A"))
    with patch("core_api.routes.crystallizer.get_storage_client", return_value=sc_mock):
        result = await get_report(report_id=uuid4(), tenant_id="tenant-A", auth=admin)
    # Admin sees the full payload.
    assert result["tenant_id"] == "tenant-A"
    # And the tenant it named is what storage was asked for.
    assert sc_mock.get_report.await_args.args[1] == "tenant-A"


async def test_get_report_honours_a_cross_tenant_read_key():
    """A widened read key reaches its non-home readable tenants.

    ``enforce_readable_tenant``, not ``enforce_tenant``: the latter is the WRITE
    gate and pins to the home tenant, so copying it onto this read path would
    403 a credential that is entitled to the row. ``readable_tenant_ids`` comes
    from the credential and never from the request, which is what keeps this
    from being the "caller names its own scope" shape rejected as option C on
    #1167.
    """
    from unittest.mock import AsyncMock, patch
    from uuid import uuid4

    from core_api.auth import AuthContext
    from core_api.routes.crystallizer import get_report

    caller = AuthContext(
        tenant_id="tenant-B",
        is_admin=False,
        readable_tenant_ids=["tenant-B", "tenant-A"],
    )

    sc_mock = AsyncMock()
    sc_mock.get_report = AsyncMock(return_value=_report_payload("tenant-A"))
    with patch("core_api.routes.crystallizer.get_storage_client", return_value=sc_mock):
        result = await get_report(report_id=uuid4(), tenant_id="tenant-A", auth=caller)

    assert result["tenant_id"] == "tenant-A"
    assert sc_mock.get_report.await_args.args[1] == "tenant-A"


async def test_get_latest_report_empty_returns_200_null(client):
    """GET /api/crystallize/latest returns 200 with body ``null`` when the
    tenant has no completed reports — empty state, not a missing resource.

    Regression for CAURA-646: previously returned 404 + ``NOT_FOUND``,
    forcing every client to special-case 404 as "actually empty". The
    URL itself ("the tenant's latest report") is well-defined; only the
    optional value behind it is unset, so 200 + ``null`` is the correct
    contract. The sibling ``/reports/{report_id}`` (above) still 404s
    because that endpoint genuinely points at an opaque id.
    """
    # Use a unique per-test tenant id — ``get_test_auth()`` returns
    # the shared ``"default"`` tenant, which is contaminated with
    # completed reports from earlier tests in this file. The admin
    # API key bypasses ``enforce_tenant``, so an arbitrary tenant_id
    # routes through cleanly without auth wiring.
    fresh_tenant = f"caura-646-empty-{_uid()}"
    _, headers = get_test_auth()
    resp = await client.get(
        f"/api/v1/crystallize/latest?tenant_id={fresh_tenant}",
        headers=headers,
    )
    assert resp.status_code == 200, (
        f"Expected 200 (empty state) but got {resp.status_code}: {resp.text}"
    )
    assert resp.json() is None, (
        f"Expected null body for empty state, got {resp.json()!r}"
    )


# ---------------------------------------------------------------------------
# Auth required
# ---------------------------------------------------------------------------


async def test_crystallize_auth_required(client):
    """POST /api/crystallize with valid auth is accepted."""
    tenant_id, headers = get_test_auth()
    await _write_memory(client, tenant_id, headers, "Auth-required test")

    code, _ = await _crystallize(client, tenant_id, headers)
    # 200 or 409 are both valid (means auth passed); anything else is a problem
    assert code in (200, 409)


async def test_reports_auth_required(client):
    """GET /api/crystallize/reports requires auth."""
    tenant_id, headers = get_test_auth()
    resp = await client.get(
        f"/api/v1/crystallize/reports?tenant_id={tenant_id}",
        headers=headers,
    )
    assert resp.status_code == 200
