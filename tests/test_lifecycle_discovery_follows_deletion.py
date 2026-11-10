"""Which tenants the scheduled jobs pick up once data is being deleted.

* A suppressed tenant (its org deleted and waiting out the grace period) is in
  none of the lists that schedule work on its content: the lifecycle fanout's
  active list and the forge, digest and interviewer opt-in lists. The
  interviewer's retention pass still lists it, so its finished jobs age out.
* Purge discovery lists a tenant once one of its soft-deleted rows is older
  than the shortest retention an org can set. Each org's own window is applied
  by the purge itself, so an org on a short window is no longer skipped until
  its oldest row reaches the 30-day maximum. A suppressed tenant stays listed.

Full path: core-api ``tenants.py`` -> storage client -> the in-process
storage app (conftest ASGI bridge) -> test DB.
"""

from __future__ import annotations

from uuid import uuid4

from sqlalchemy import text

from core_api.services.organization_settings import invalidate_cache, update_settings
from core_api.services.tenants import (
    list_active_tenant_ids,
    list_tenants_with_agent_digest_enabled,
    list_tenants_with_interviewer_disabled,
    list_tenants_with_interviewer_enabled,
    list_tenants_with_purgeable_memories,
    list_tenants_with_skills_factory_enabled,
)
from core_storage_api.services.postgres_service import get_session


def _tenant() -> str:
    return f"test-tenant-deletion-{uuid4().hex[:8]}"


async def _memory(tenant_id: str, *, deleted_days_ago: int | None = None) -> None:
    """A committed memory row: live, or soft-deleted that many days ago.

    ``make_interval`` of NULL is NULL, so a live row gets no ``deleted_at``.
    """
    async with get_session() as session:
        await session.execute(
            text(
                """
                INSERT INTO memories
                    (tenant_id, agent_id, content, memory_type, status, deleted_at)
                VALUES
                    (:tenant_id, 'agent-1', 'x', 'fact', :status,
                     now() - make_interval(days => CAST(:days AS integer)))
                """
            ),
            {
                "tenant_id": tenant_id,
                "status": "active" if deleted_days_ago is None else "deleted",
                "days": deleted_days_ago,
            },
        )


async def _suppress(tenant_id: str) -> None:
    """Mark the tenant suppressed, as the org-deletion event does."""
    async with get_session() as session:
        await session.execute(
            text(
                "INSERT INTO public.tenant_suppression (tenant_id, suppressed_at) "
                "VALUES (:tenant_id, now())"
            ),
            {"tenant_id": tenant_id},
        )


async def _opt_in(org_id: str) -> None:
    await update_settings(
        org_id,
        {
            "skills_factory": {"enabled": True},
            "agent_digest": {"enabled": True},
            "interviewer": {"enabled": True},
        },
    )
    invalidate_cache(org_id)


async def test_a_suppressed_tenant_gets_no_lifecycle_work():
    live, suppressed = _tenant(), _tenant()
    for tenant in (live, suppressed):
        await _memory(tenant)
    await _suppress(suppressed)

    active = await list_active_tenant_ids()

    assert live in active
    assert suppressed not in active


async def test_a_suppressed_org_drops_out_of_every_opt_in_list():
    live, suppressed = _tenant(), _tenant()
    for org in (live, suppressed):
        await _opt_in(org)
    await _suppress(suppressed)

    for listing in (
        list_tenants_with_skills_factory_enabled,
        list_tenants_with_agent_digest_enabled,
        list_tenants_with_interviewer_enabled,
    ):
        listed = await listing()
        assert live in listed, listing.__name__
        assert suppressed not in listed, listing.__name__


async def test_a_suppressed_orgs_interview_jobs_still_age_out():
    """The retention pass runs for orgs with the interviewer off; a suppressed
    org counts as off, so its finished jobs are still purged."""
    org = _tenant()
    await _opt_in(org)
    await _suppress(org)

    assert org in await list_tenants_with_interviewer_disabled()


async def test_purge_finds_a_tenant_on_a_short_retention_window():
    short, recent = _tenant(), _tenant()
    await update_settings(short, {"lifecycle": {"memory_retention_days": 1}})
    invalidate_cache(short)
    await _memory(short, deleted_days_ago=2)
    await _memory(recent, deleted_days_ago=0)

    purgeable = await list_tenants_with_purgeable_memories()

    assert short in purgeable
    assert recent not in purgeable


async def test_a_suppressed_tenant_is_still_purged():
    org = _tenant()
    await _memory(org, deleted_days_ago=31)
    await _suppress(org)

    assert org in await list_tenants_with_purgeable_memories()
