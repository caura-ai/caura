"""Entity search reads aliases, and takes the user's text literally (L-129, L-48).

L-129. First-seen-wins keeps an entity's first canonical name and justifies
dropping a later surface form with "they remain searchable / discoverable" via
``attributes._aliases``. Neither search read them: the FTS trigger (migration
001) built ``search_vector`` from ``canonical_name`` alone and fired only on
``UPDATE OF canonical_name``, and ``GET /entities?search=`` ILIKEd
``canonical_name`` only. A merged surface form could not be found. Migration
057 puts the aliases in the vector and fires on ``attributes`` too, and the list
search matches an alias.

L-48. The list search built ``ILIKE '%{search}%'`` without escaping, so ``%``
and ``_`` in the user's text were wildcards (``%`` returned everything) and a
backslash escaped the next character instead of matching itself.

Against the migrated schema: the trigger under test exists only there.
"""

import uuid

import pytest

from core_storage_api.services.postgres_service import PostgresService

pytestmark = [pytest.mark.asyncio, pytest.mark.usefixtures("_ensure_schema")]

_svc = PostgresService()


def _tenant() -> str:
    return f"test-search-{uuid.uuid4().hex[:8]}"


async def _entity(tenant: str, name: str, attributes=None):
    return await _svc.entity_add(
        {
            "tenant_id": tenant,
            "entity_type": "organization",
            "canonical_name": name,
            "attributes": attributes or {},
        }
    )


async def _listed(tenant: str, search: str) -> set[str]:
    return {e.canonical_name for e in await _svc.entity_list(tenant, search=search)}


# ── L-48: the list search is a plain substring match ──────────────────────


async def test_list_search_takes_percent_and_underscore_literally():
    tenant = _tenant()
    for name in ("axb", "a_b", "50% off"):
        await _entity(tenant, name)

    assert await _listed(tenant, "a_b") == {"a_b"}
    assert await _listed(tenant, "%") == {"50% off"}


async def test_list_search_takes_a_backslash_literally():
    tenant = _tenant()
    await _entity(tenant, "c:\\temp")
    await _entity(tenant, "c:temp")

    assert await _listed(tenant, "C:\\temp") == {"c:\\temp"}


# ── L-129: both searches find an alias ────────────────────────────────────


async def test_list_search_matches_an_alias():
    tenant = _tenant()
    name = "international business machines"
    await _entity(tenant, name, {"_aliases": [name, "IBM"]})

    assert await _listed(tenant, "ibm") == {name}


async def test_entity_fts_finds_an_alias():
    tenant = _tenant()
    entity = await _entity(tenant, "international business machines", {"_aliases": ["IBM"]})

    assert await _svc.entity_fts_search(["ibm"], tenant) == [entity.id]


async def test_an_alias_added_later_is_indexed_too():
    """The trigger fires on ``attributes``: aliases arrive by update, not insert."""
    tenant = _tenant()
    entity = await _entity(tenant, "international business machines")

    await _svc.entity_update(entity.id, tenant, {"attributes": {"_aliases": ["IBM"]}})

    assert await _svc.entity_fts_search(["ibm"], tenant) == [entity.id]


async def test_a_malformed_alias_list_does_not_break_the_write():
    """The control: the trigger reads ``_aliases`` defensively, so a value that
    is not a list indexes nothing extra and the write still lands."""
    tenant = _tenant()
    entity = await _entity(tenant, "globex", {"_aliases": "not-a-list"})

    assert await _svc.entity_fts_search(["globex"], tenant) == [entity.id]
