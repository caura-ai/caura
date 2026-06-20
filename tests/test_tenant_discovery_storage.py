"""Fix 2 Phase 1 — tenant-discovery helpers now read through core-storage-api.

These exercise the full path (core-api tenants.py -> storage_client -> the
in-process storage-api app via the conftest ASGI bridge -> test DB). The ``db``
argument to the helpers is vestigial (ignored); passed through for back-compat.
"""

import uuid

from core_api.services.organization_settings import invalidate_cache, update_settings
from core_api.services.tenants import (
    list_active_tenant_ids,
    list_tenants_with_purgeable_memories,
    list_tenants_with_skills_factory_enabled,
)


def _org() -> str:
    return f"test-tenant-{uuid.uuid4().hex[:8]}"


async def test_list_active_tenant_ids_returns_sorted_str_list(db):
    """Reads via storage-api and returns a sorted list of tenant-id strings."""
    result = await list_active_tenant_ids(db)
    assert isinstance(result, list)
    assert all(isinstance(t, str) for t in result)
    assert result == sorted(result)


async def test_list_purgeable_returns_sorted_str_list(db):
    """purgeable variant: same shape contract; SQL bounded to old soft-deleted rows."""
    result = await list_tenants_with_purgeable_memories(db)
    assert isinstance(result, list)
    assert all(isinstance(t, str) for t in result)
    assert result == sorted(result)


async def test_skills_factory_enabled_lists_opted_in_org(db):
    """End-to-end JSONB filter: an org that flips ``skills_factory.enabled`` via
    the settings write path shows up in the discovery list (read via storage-api)."""
    org = _org()
    await update_settings(None, org, {"skills_factory": {"enabled": True}})
    invalidate_cache(org)

    enabled = await list_tenants_with_skills_factory_enabled(db)
    assert org in enabled
    assert enabled == sorted(enabled)
