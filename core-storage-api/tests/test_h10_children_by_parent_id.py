"""H-10 — the parent→child lookup governance remediation cascades through.

Runs against the real Postgres fixture, because the thing under test is a JSONB
key comparison (``metadata->>'parent_memory_id'``) and a stub would assert only
that the code calls itself. The core-api side stubs storage entirely, so without
these the cascade could ship with a query that matches nothing and every test
above it would still pass.

The rows this finds are ones a governance verdict is about to soft-delete, so
the two negative cases matter as much as the positive one: a query that reaches
across tenants would delete another tenant's rows on a drop.
"""

import uuid

import pytest

from core_storage_api.services.postgres_service import PostgresService

pytestmark = pytest.mark.asyncio


async def _insert(svc: PostgresService, tenant: str, metadata: dict, content: str | None = None):
    return await svc.memory_add(
        {
            "tenant_id": tenant,
            "agent_id": "h10-tester",
            "content": content or f"h10 canary {uuid.uuid4()}",
            "memory_type": "fact",
            "weight": 0.5,
            "metadata_": metadata,
            "status": "active",
            "visibility": "scope_team",
        }
    )


async def test_finds_every_child_of_the_parent():
    svc = PostgresService()
    tenant = f"h10-{uuid.uuid4().hex[:8]}"
    parent = await _insert(svc, tenant, {"auto_chunked": True, "child_count": 2})
    pid = str(parent.id)

    c1 = await _insert(svc, tenant, {"parent_memory_id": pid, "source": "auto_chunk"})
    c2 = await _insert(svc, tenant, {"parent_memory_id": pid, "source": "auto_chunk"})
    # An unrelated row in the same tenant must not be swept up — on a drop this
    # query decides what gets soft-deleted.
    await _insert(svc, tenant, {"source": "ordinary write"})

    found = await svc.memory_find_children_by_parent_id(tenant_id=tenant, parent_id=pid)

    assert {str(m.id) for m in found} == {str(c1.id), str(c2.id)}


async def test_does_not_cross_tenants():
    """The one filter that is a boundary rather than a preference.

    A child id is a UUID, so a collision is not the worry — a query that
    forgot the tenant filter is, and it would soft-delete a stranger's rows the
    moment one tenant's governance policy fired.
    """
    svc = PostgresService()
    tenant_a = f"h10a-{uuid.uuid4().hex[:8]}"
    tenant_b = f"h10b-{uuid.uuid4().hex[:8]}"
    parent = await _insert(svc, tenant_a, {"auto_chunked": True})
    pid = str(parent.id)

    await _insert(svc, tenant_a, {"parent_memory_id": pid})
    # Same parent id, different tenant. Contrived on purpose: it isolates the
    # tenant predicate from the metadata predicate.
    await _insert(svc, tenant_b, {"parent_memory_id": pid})

    found_a = await svc.memory_find_children_by_parent_id(tenant_id=tenant_a, parent_id=pid)
    found_b = await svc.memory_find_children_by_parent_id(tenant_id=tenant_b, parent_id=pid)

    assert len(found_a) == 1
    assert len(found_b) == 1
    assert {m.tenant_id for m in found_a} == {tenant_a}
    assert {m.tenant_id for m in found_b} == {tenant_b}


async def test_skips_rows_already_soft_deleted():
    """A row already gone needs no second remediation, and re-deleting it would
    put a duplicate destructive audit row in the compliance log."""
    svc = PostgresService()
    tenant = f"h10-{uuid.uuid4().hex[:8]}"
    parent = await _insert(svc, tenant, {"auto_chunked": True})
    pid = str(parent.id)

    live = await _insert(svc, tenant, {"parent_memory_id": pid})
    gone = await _insert(svc, tenant, {"parent_memory_id": pid})
    assert await svc.memory_soft_delete_by_ids(tenant, [gone.id]) == 1

    found = await svc.memory_find_children_by_parent_id(tenant_id=tenant, parent_id=pid)

    assert [str(m.id) for m in found] == [str(live.id)]


async def test_a_parent_with_no_children_finds_nothing():
    """Not a tautology: ``metadata->>'key'`` on rows lacking the key yields NULL,
    and a predicate written against the wrong operator can match those instead of
    skipping them — which would hand a drop every row in the tenant."""
    svc = PostgresService()
    tenant = f"h10-{uuid.uuid4().hex[:8]}"
    parent = await _insert(svc, tenant, {"auto_chunked": True})
    await _insert(svc, tenant, {"source": "no parent link"})
    await _insert(svc, tenant, {})

    found = await svc.memory_find_children_by_parent_id(tenant_id=tenant, parent_id=str(parent.id))

    assert found == []
