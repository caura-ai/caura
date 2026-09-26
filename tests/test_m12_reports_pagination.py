"""09/02 M-12 — /crystallize/reports advertised pagination it did not do.

``GET /crystallize/reports`` declared ``limit`` (ge=1, le=100) and ``offset``
(ge=0), validated both, and then called ``sc.list_reports(tenant_id)`` without
them. The storage route took only ``tenant_id`` too, so the query ran on
``report_list_by_tenant``'s OWN defaults — ``limit=10, offset=0``.

The effect is worse than "the parameters are ignored":

* ``?limit=100`` returned **10** rows, silently capped;
* ``?offset=50`` returned the **first** 10, so paging returned page 1 forever.

The window is now threaded through all three layers. The SQL never needed
changing — ``report_list_by_tenant`` has always had ``ORDER BY started_at DESC
OFFSET ... LIMIT ...``; it simply never received a caller's window.
"""

import inspect

import pytest

pytestmark = pytest.mark.unit


def test_the_query_could_always_paginate():
    """Establishes that this was a plumbing bug, not a missing feature — and
    that the fix belongs above the SQL, not in it."""
    from core_storage_api.services.postgres_service import PostgresService

    sig = inspect.signature(PostgresService.report_list_by_tenant)
    assert "limit" in sig.parameters
    assert "offset" in sig.parameters
    src = inspect.getsource(PostgresService.report_list_by_tenant)
    assert ".offset(offset)" in src and ".limit(limit)" in src


def test_core_api_route_passes_what_it_validates():
    """The defect itself: declared, bounded, and then dropped."""
    from core_api.routes import crystallizer

    src = inspect.getsource(crystallizer.list_reports)
    assert "limit=limit" in src
    assert "offset=offset" in src
    assert "sc.list_reports(tenant_id)" not in src, "the un-paged call is the bug"


def test_storage_client_forwards_the_window():
    from core_api.clients.storage_client import CoreStorageClient

    sig = inspect.signature(CoreStorageClient.list_reports)
    assert "limit" in sig.parameters
    assert "offset" in sig.parameters
    src = inspect.getsource(CoreStorageClient.list_reports)
    assert "limit=limit" in src and "offset=offset" in src


def test_storage_route_accepts_and_forwards_the_window():
    """The middle layer. Threading only the two ends would leave the same bug
    with more code — the storage route was the one that swallowed it."""
    from core_storage_api.routers import reports

    sig = inspect.signature(reports.list_reports)
    assert "limit" in sig.parameters
    assert "offset" in sig.parameters
    src = inspect.getsource(reports.list_reports)
    assert "limit=limit" in src and "offset=offset" in src


def test_defaults_preserve_the_previous_window_for_unpaged_callers():
    """Every layer defaults to the service's own ``limit=10, offset=0``, so a
    caller that passes nothing sees exactly what it saw before. A different
    default here would silently change an existing response size."""
    from core_api.clients.storage_client import CoreStorageClient
    from core_storage_api.routers import reports

    for fn in (CoreStorageClient.list_reports, reports.list_reports):
        params = inspect.signature(fn).parameters
        assert params["limit"].default == 10, fn
        assert params["offset"].default == 0, fn


def test_the_core_api_edge_still_bounds_the_window():
    """Bounds live at the public edge, and must stay there: the storage route is
    internal and deliberately unbounded, so removing these would let a caller
    ask for an unbounded scan."""
    from core_api.routes import crystallizer

    src = inspect.getsource(crystallizer.list_reports)
    assert "ge=1" in src and "le=100" in src  # limit
    assert "ge=0" in src  # offset
