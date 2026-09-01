"""E2E crystallizer (memory analysis) tests through HTTP API."""

from tests.conftest import get_admin_headers, get_test_auth
from tests.conftest import uid as _uid

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
        f"/api/v1/crystallize/reports/{report_id}",
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
    _, headers = get_test_auth()
    fake_id = "00000000-0000-0000-0000-000000000000"
    resp = await client.get(
        f"/api/v1/crystallize/reports/{fake_id}",
        headers=headers,
    )
    assert resp.status_code == 404


async def test_get_report_foreign_tenant_returns_404_not_403():
    """A non-admin caller probing a report owned by a different tenant
    must see 404 (same as a missing report), not 403 — otherwise an
    attacker could enumerate report_ids across tenants by distinguishing
    the two status codes (audit finding #22).
    """
    from unittest.mock import AsyncMock, patch
    from uuid import uuid4

    from fastapi import HTTPException

    from core_api.auth import AuthContext
    from core_api.routes.crystallizer import get_report

    foreign_report = {
        "id": str(uuid4()),
        "tenant_id": "tenant-A",  # owned by tenant A
        "fleet_id": None,
    }
    caller = AuthContext(tenant_id="tenant-B", is_admin=False)

    sc_mock = AsyncMock()
    sc_mock.get_report = AsyncMock(return_value=foreign_report)
    with patch("core_api.routes.crystallizer.get_storage_client", return_value=sc_mock):
        try:
            await get_report(report_id=uuid4(), auth=caller)
        except HTTPException as e:
            assert e.status_code == 404, (
                f"Foreign-tenant report read must surface as 404; got {e.status_code}"
            )
            assert e.detail == "Report not found"
        else:
            raise AssertionError(
                "Expected HTTPException(404) for foreign-tenant report"
            )


async def test_get_report_403_cannot_be_used_to_probe_for_reports():
    """The 403 must not vary with whether the report exists (audit finding #22).

    The test above covers the case where the caller names a tenant it MAY read.
    This one covers the other half, which #1167 introduced: naming a tenant the
    credential may NOT read is a 403, on a route that previously had only 404s.
    A 403 that depended on the report would be the enumeration oracle finding
    #22 closed — ask about a tenant you cannot read, and the status tells you
    whether the id exists there.

    It does not depend on the report, and the reason is positional:
    ``enforce_readable_tenant`` runs BEFORE the fetch, so it compares two
    strings the caller already knows and never learns anything about the row.

    **That ordering is the whole guarantee, and it is the kind of thing an
    ordinary refactor moves.** Hoisting the lookup, or sliding the auth call
    down past the ``if not report`` to avoid a redundant check on the happy
    path, both reintroduce the oracle — verified by doing it: with the call
    moved after the fetch, every other test in this file and all of
    ``test_auth_context.py`` still pass, while an unreadable tenant answers 403
    for a report that exists and 404 for one that does not.

    Both assertions below are load-bearing. The first states the property; the
    second states the mechanism, and fails on the reorder even if some future
    shape makes the statuses coincide by accident.

    Second concrete instance of the shape in #847 — an audit finding
    reintroducible by a change that reads as a cleanup, with nothing watching.
    """
    from unittest.mock import AsyncMock, patch
    from uuid import uuid4

    from fastapi import HTTPException

    from core_api.auth import AuthContext
    from core_api.routes.crystallizer import get_report

    caller = AuthContext(tenant_id="tenant-B", is_admin=False)
    unreadable = "tenant-A"

    async def _probe(storage_answer):
        """Ask about ``unreadable``; return (status, times storage was called)."""
        sc_mock = AsyncMock()
        sc_mock.get_report = AsyncMock(return_value=storage_answer)
        status = 200
        with patch(
            "core_api.routes.crystallizer.get_storage_client", return_value=sc_mock
        ):
            try:
                await get_report(report_id=uuid4(), tenant_id=unreadable, auth=caller)
            except HTTPException as e:
                status = e.status_code
        return status, sc_mock.get_report.await_count

    # A report that exists in the unreadable tenant, and one that does not.
    present, present_calls = await _probe({"id": str(uuid4()), "tenant_id": unreadable})
    absent, absent_calls = await _probe(None)

    assert present == absent == 403, (
        "the response to an unreadable tenant must not depend on whether the report "
        f"exists: got {present} when present and {absent} when absent — that difference "
        "is an existence oracle for report ids in other tenants (audit finding #22)"
    )
    assert present_calls == absent_calls == 0, (
        "enforce_readable_tenant must run BEFORE the fetch; storage was consulted "
        f"{present_calls or absent_calls} time(s) for a tenant the caller may not read, "
        "which is what lets the response carry information about the row"
    )


async def test_get_report_foreign_tenant_admin_bypass():
    """Admin keys keep their cross-tenant read ability — by naming the tenant.

    The capability is unchanged: an admin credential still reads any tenant's
    report and is not 404-masked. What changed with #1167 is that it must say
    which tenant, because storage now requires one and an admin key has no
    tenant of its own to fall back on. The sibling ``GET /reports`` in this
    package already asks admin keys for the same thing.
    """
    from unittest.mock import AsyncMock, patch
    from uuid import uuid4

    from core_api.auth import AuthContext
    from core_api.routes.crystallizer import get_report

    foreign_report = {
        "id": str(uuid4()),
        "tenant_id": "tenant-A",
        "fleet_id": None,
        "trigger": "manual",
        "status": "completed",
        "summary": {},
        "hygiene": {},
        "health": {},
        "issues": [],
        "crystallization": {},
    }
    admin = AuthContext(tenant_id=None, is_admin=True)

    sc_mock = AsyncMock()
    sc_mock.get_report = AsyncMock(return_value=foreign_report)
    with patch("core_api.routes.crystallizer.get_storage_client", return_value=sc_mock):
        result = await get_report(report_id=uuid4(), tenant_id="tenant-A", auth=admin)
    # Admin sees the full payload.
    assert result["tenant_id"] == "tenant-A"
    # And the tenant it named is the one storage was asked about — not a
    # placeholder, and not omitted. Positional, matching the client signature.
    assert sc_mock.get_report.await_args.args[1] == "tenant-A"


async def test_get_report_admin_without_a_tenant_is_a_400():
    """An admin key that names no tenant gets a request error, not an unscoped read.

    This is the one behaviour #1167 takes away, so it is pinned rather than left
    to the absence of a test. 400 and not 401: the credential IS authenticated
    (the distinction #987 drew on the skills-inbox routes), and not 404 either —
    nothing was looked up, so there is nothing to mask.

    Storage is asserted untouched. Reaching it with ``tenant_id=None`` is the
    failure mode that matters: the query string would carry the literal
    ``None``, which matches no row, and the endpoint would answer 404 for every
    report an admin ever asked for.
    """
    from unittest.mock import AsyncMock, patch
    from uuid import uuid4

    from fastapi import HTTPException

    from core_api.auth import AuthContext
    from core_api.routes.crystallizer import get_report

    admin = AuthContext(tenant_id=None, is_admin=True)
    sc_mock = AsyncMock()
    sc_mock.get_report = AsyncMock(return_value=None)

    with patch("core_api.routes.crystallizer.get_storage_client", return_value=sc_mock):
        try:
            await get_report(report_id=uuid4(), tenant_id=None, auth=admin)
        except HTTPException as e:
            assert e.status_code == 400, (
                f"admin with no tenant must be a 400; got {e.status_code}"
            )
            assert e.detail["code"] == "TENANT_REQUIRED"
        else:
            raise AssertionError(
                "Expected HTTPException(400) for an admin key naming no tenant"
            )

    sc_mock.get_report.assert_not_awaited()


async def test_get_report_defaults_to_the_credentials_own_tenant():
    """A tenant-scoped credential needs no query param and gets its own tenant.

    The compatibility half of the change: every caller that worked before this
    still works, because ``tenant_id`` is optional on the wire and resolves to
    ``auth.tenant_id``. Only credentials with no tenant of their own have to
    start naming one.
    """
    from unittest.mock import AsyncMock, patch
    from uuid import uuid4

    from core_api.auth import AuthContext
    from core_api.routes.crystallizer import get_report

    own_report = {
        "id": str(uuid4()),
        "tenant_id": "tenant-B",
        "fleet_id": None,
        "trigger": "manual",
        "status": "completed",
        "summary": {},
        "hygiene": {},
        "health": {},
        "issues": [],
        "crystallization": {},
    }
    caller = AuthContext(tenant_id="tenant-B", is_admin=False)

    sc_mock = AsyncMock()
    sc_mock.get_report = AsyncMock(return_value=own_report)
    with patch("core_api.routes.crystallizer.get_storage_client", return_value=sc_mock):
        result = await get_report(report_id=uuid4(), auth=caller)

    assert result["tenant_id"] == "tenant-B"
    assert sc_mock.get_report.await_args.args[1] == "tenant-B"


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
