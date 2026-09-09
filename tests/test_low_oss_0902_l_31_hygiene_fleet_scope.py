"""oss-0902-l-31 — fleet-scoped crystallizer reports carried tenant-wide counts.

Five hygiene checks took a ``fleet_id`` parameter and never applied it, so a
report generated for one fleet reported every other fleet's orphans, expired
rows, stale rows, short content and broken links as its own.

The parameter was not the hard part — the STORAGE layer has always accepted a
fleet argument for all five. The scoping was thrown away in transit: the two
entity routes hardcoded ``fleet_id=None``, and ``/lifecycle-candidates`` passed
a positional ``None`` to three service calls that each take one. So the fix is a
thread, not a rewrite, and these tests pin every link in it — a fix at one layer
is silently undone by the next.
"""

import inspect

import pytest

pytestmark = pytest.mark.unit


@pytest.mark.parametrize(
    "fn",
    [
        "_check_orphaned_entities",
        "_check_broken_entity_links",
        "_check_expired_still_active",
        "_check_stale_memories",
        "_check_short_content",
    ],
)
def test_every_fleet_blind_check_now_forwards_its_fleet(fn):
    """All five took the parameter and dropped it. Reading the source is the
    point: each one has exactly one storage call, and it must carry fleet_id."""
    from core_api.services import crystallizer_service as cs

    src = inspect.getsource(getattr(cs, fn))
    assert "fleet_id" in src.split("-> dict:", 1)[1], f"{fn} still ignores fleet_id"


def test_storage_client_sends_fleet_only_when_set():
    """Omitted rather than sent as None, so a storage instance running ahead of
    core-api keeps its existing behaviour instead of seeing an explicit null."""
    from core_api.clients.storage_client import CoreStorageClient

    for m in (
        "find_orphaned_entities",
        "find_broken_entity_links",
        "get_lifecycle_candidates",
    ):
        sig = inspect.signature(getattr(CoreStorageClient, m))
        assert sig.parameters["fleet_id"].default is None, m
        src = inspect.getsource(getattr(CoreStorageClient, m))
        assert "if fleet_id is not None:" in src, m
        assert 'params["fleet_id"] = fleet_id' in src, m


def test_entity_routes_no_longer_hardcode_none():
    """This is where the scoping was actually lost — the service call below has
    always taken the argument."""
    from core_storage_api.routers import entities

    src = inspect.getsource(entities)
    assert "entity_find_orphaned(tenant_id, fleet_id=None)" not in src
    assert "entity_find_broken_links(tenant_id, fleet_id=None)" not in src
    assert "entity_find_orphaned(tenant_id, fleet_id=fleet_id)" in src
    assert "entity_find_broken_links(tenant_id, fleet_id=fleet_id)" in src


def test_lifecycle_route_forwards_fleet_to_all_three_queries():
    """One route feeds three of the five checks, so a miss here re-breaks three
    of them at once."""
    from core_storage_api.routers import memories

    # Whitespace-normalised: the formatter is free to rewrap these calls, and a
    # line-exact assertion would fail on reformatting rather than on a regression.
    src = " ".join(inspect.getsource(memories.get_lifecycle_candidates).split())
    assert "memory_find_expired_still_active(tenant_id, fleet_id)" in src
    assert "memory_find_stale_count(tenant_id, fleet_id" in src
    assert "memory_find_short_content(tenant_id, fleet_id" in src
    assert "(tenant_id, None" not in src


def test_route_signatures_accept_an_optional_fleet():
    from core_storage_api.routers import entities, memories

    for fn in (
        entities.find_orphaned_entities,
        entities.find_broken_entity_links,
        memories.get_lifecycle_candidates,
    ):
        p = inspect.signature(fn).parameters
        assert "fleet_id" in p, fn.__name__
        assert p["fleet_id"].default is None, fn.__name__
