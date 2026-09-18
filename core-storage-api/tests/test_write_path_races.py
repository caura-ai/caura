"""OSS audit 08/14 + 09/02 — write paths that mishandle their own conflict.

Three findings, one shape: the INSERT correctly detects a conflict and the
RECOVERY from it is wrong, so a race that the database handled cleanly turns
into a 500, a dropped item, or a silent data loss the caller is told did not
happen.

* **M-63** — ``entity_add`` recovered via ``flush() → IntegrityError →
  rollback() → re-SELECT``, but ``get_session`` yields inside
  ``session.begin()``: the mid-block rollback closed the transaction the
  context manager still owned, so the re-SELECT could never run.
* **L-51** — ``memory_add_all``'s post-conflict re-query branched on
  ``fleet_id`` NULL-ness while the arbiter index groups
  ``COALESCE(fleet_id, '')``.
* **L-16** — concurrent first-time org-settings writes merged with a SHALLOW
  JSONB ``||``, dropping sibling sub-keys under a shared namespace.

Against real Postgres, because every one of these is about what the database
does under a genuine conflict. A stubbed session cannot produce a unique
violation, an ``ON CONFLICT`` no-op, or a row lock.
"""

from __future__ import annotations

import asyncio
import uuid

import pytest
from sqlalchemy import select

from common.models import Memory
from core_storage_api.services.postgres_service import PostgresService, get_session

pytestmark = pytest.mark.asyncio


def _entity(tenant: str, *, name: str = "AcmeCorp", fleet_id: str | None = None) -> dict:
    return {
        "tenant_id": tenant,
        "entity_type": "org",
        "canonical_name": name,
        "fleet_id": fleet_id,
    }


# ---------------------------------------------------------------------------
# M-63 — a dedup race must return the winner, not 500.
# ---------------------------------------------------------------------------


async def test_a_dedup_race_returns_the_winning_row(_ensure_schema):
    """The recovery path used to be unreachable code.

    ``rollback()`` inside ``session.begin()`` closes the transaction the
    context manager owns, so the re-SELECT raised ``InvalidRequestError:
    Can't operate on closed transaction`` before it could run — every entity
    dedup race became a 500, and the ``winner is None`` guard below it had
    never executed once.
    """
    svc = PostgresService()
    tenant = f"t-m63-{uuid.uuid4().hex[:8]}"

    first = await svc.entity_add(_entity(tenant))
    second = await svc.entity_add(_entity(tenant))

    assert second.id == first.id, "the race recovery returned a different row than the winner"


async def test_concurrent_entity_creates_converge_on_one_row(_ensure_schema):
    """The race as it actually arrives: parallel extraction tasks.

    Sequential calls prove the conflict is handled; only genuine concurrency
    proves the losers recover rather than raising.
    """
    svc = PostgresService()
    tenant = f"t-m63c-{uuid.uuid4().hex[:8]}"

    results = await asyncio.gather(
        *(svc.entity_add(_entity(tenant)) for _ in range(5)),
        return_exceptions=True,
    )
    failures = [r for r in results if isinstance(r, BaseException)]
    assert not failures, f"concurrent entity_add raised: {failures!r}"
    assert len({r.id for r in results}) == 1, "the racers disagreed about which row won"


async def test_entity_dedup_matches_the_index_coalesce_grouping(_ensure_schema):
    """``fleet_id=""`` and ``fleet_id=NULL`` are ONE row to the index.

    ``uq_entities_tenant_type_name_fleet`` keys on ``COALESCE(fleet_id, '')``,
    so a row stored as ``''`` occupies the same slot as a NULL one. The old
    re-SELECT chose its predicate on falsiness — ``''`` is falsy, so it looked
    for ``IS NULL`` and could not see the very row whose conflict sent it
    there, raising "conflict but re-select returned nothing" for a row plainly
    present.
    """
    svc = PostgresService()
    tenant = f"t-m63f-{uuid.uuid4().hex[:8]}"

    first = await svc.entity_add(_entity(tenant, fleet_id=""))
    second = await svc.entity_add(_entity(tenant, fleet_id=""))

    assert second.id == first.id, "an empty-string fleet_id did not resolve to the stored row"


# ---------------------------------------------------------------------------
# L-51 — the post-conflict re-query must group fleet_id like the arbiter.
# ---------------------------------------------------------------------------


def _bulk_item(tenant: str, crid: str, *, fleet_id: str | None) -> dict:
    return {
        "tenant_id": tenant,
        "fleet_id": fleet_id,
        "agent_id": "l51-tester",
        "client_request_id": crid,
        "content": f"l51 canary {crid}",
        "memory_type": "fact",
        "weight": 0.5,
        "status": "active",
        "visibility": "scope_team",
    }


async def test_a_retried_attempt_resolves_across_the_coalesce_grouping(_ensure_schema):
    """A retry that spells fleetless as ``""`` must still find the NULL row.

    ``ix_memories_attempt_unique`` scopes ``(tenant_id, COALESCE(fleet_id, ''),
    client_request_id)``, and the method's own comment says the re-query
    matches that scope "exactly". It did not: it branched on ``fleet_id is not
    None``, so a retry passing ``""`` against a row stored NULL conflicted in
    the index and then missed in the lookup — the item fell through to the
    ``id: None`` branch and was reported as a per-item failure for a write that
    had already committed.
    """
    svc = PostgresService()
    tenant = f"t-l51-{uuid.uuid4().hex[:8]}"
    crid = f"crid-{uuid.uuid4().hex[:12]}"

    first = await svc.memory_add_all([_bulk_item(tenant, crid, fleet_id=None)])
    assert first[0]["was_inserted"] is True
    assert first[0]["id"] is not None

    # Same attempt, fleetless spelled the other way.
    retry = await svc.memory_add_all([_bulk_item(tenant, crid, fleet_id="")])
    assert retry[0]["was_inserted"] is False, "the retry inserted a second row for one attempt"
    assert retry[0]["id"] is not None, (
        "the retry could not resolve the row it had just conflicted with — the "
        f"re-query disagreed with the arbiter about fleet_id: {retry[0]}"
    )
    assert retry[0]["id"] == first[0]["id"]

    # And exactly one row exists for that attempt.
    async with get_session() as session:
        rows = (
            await session.execute(
                select(Memory.id).where(Memory.tenant_id == tenant, Memory.client_request_id == crid)
            )
        ).all()
    assert len(rows) == 1, f"the attempt produced {len(rows)} rows"


# ---------------------------------------------------------------------------
# L-16 — concurrent first-time org writes must not shallow-merge.
# ---------------------------------------------------------------------------


async def test_concurrent_first_time_org_writes_keep_both_sub_keys(_ensure_schema):
    """Two first writers under the SAME namespace, different sub-keys.

    ``FOR UPDATE`` locks nothing when the row does not exist yet, so the old
    code leaned on ``settings || EXCLUDED.settings`` — a SHALLOW merge. Its
    comment called that safe "because top-level schema keys are independent",
    which holds for writers touching DIFFERENT namespaces and is exactly wrong
    here: ``||`` replaces the whole ``enrichment`` object and the loser's
    sub-key is gone.
    """
    svc = PostgresService()
    org = f"org-l16-{uuid.uuid4().hex[:8]}"

    results = await asyncio.gather(
        svc.organization_settings_update(
            org_id=org, new_settings={"enrichment": {"provider": "openai"}}, changed_by="a"
        ),
        svc.organization_settings_update(
            org_id=org, new_settings={"enrichment": {"enabled": True}}, changed_by="b"
        ),
        return_exceptions=True,
    )
    failures = [r for r in results if isinstance(r, BaseException)]
    assert not failures, f"concurrent org settings update raised: {failures!r}"

    stored = await svc.organization_settings_get(org)
    assert stored.get("enrichment", {}) == {"provider": "openai", "enabled": True}, (
        f"a sibling sub-key was clobbered by the shallow merge: {stored}"
    )


async def test_the_org_settings_response_matches_what_was_stored(_ensure_schema):
    """The loser used to be told its write landed when it had not.

    Both racers returned ``changed: True`` with their OWN ``merged`` echoed
    back, and wrote an audit row for it — a success response quoting a value
    that was never stored. Whatever a caller is handed must be readable back.
    """
    svc = PostgresService()
    org = f"org-l16r-{uuid.uuid4().hex[:8]}"

    a, b = await asyncio.gather(
        svc.organization_settings_update(org_id=org, new_settings={"recall": {"depth": 3}}, changed_by="a"),
        svc.organization_settings_update(org_id=org, new_settings={"recall": {"window": 10}}, changed_by="b"),
    )
    stored = await svc.organization_settings_get(org)

    # The later writer's view must BE the stored state; the earlier one's must
    # be a subset of it (the other writer merged on top afterwards).
    for who, res in (("a", a), ("b", b)):
        for ns, body in res["settings"].items():
            for key, value in body.items():
                assert stored.get(ns, {}).get(key) == value, (
                    f"writer {who} was told {ns}.{key}={value!r} was written, "
                    f"but the stored row holds {stored!r}"
                )


async def test_a_noop_payload_writes_no_row_at_all(_ensure_schema):
    """Claiming the row must not happen before the diff is known to be real.

    The fix seeds the row with an INSERT so the lock has something to hold —
    done unconditionally that would create an empty row for a caller whose
    payload changes nothing, turning a pure no-op into a write.
    """
    svc = PostgresService()
    org = f"org-l16n-{uuid.uuid4().hex[:8]}"

    result = await svc.organization_settings_update(org_id=org, new_settings={}, changed_by="a")
    assert result == {"settings": {}, "changed": False}

    async with get_session() as session:
        from common.models.organization_settings import OrganizationSettings

        rows = (
            await session.execute(
                select(OrganizationSettings.org_id).where(OrganizationSettings.org_id == org)
            )
        ).all()
    assert rows == [], "a no-op payload created a settings row"
