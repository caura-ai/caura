"""``PATCH /memories/{id}`` adds entity links; the audit trail must say so.

The comment on this block read "replace if explicitly provided" and the audit
record read ``{"old": "replaced", ...}``, but ``memory_add_entity_links`` inserts
with ``ON CONFLICT (memory_id, entity_id) DO NOTHING`` and has no delete branch.
So a PATCH naming only entity A on a memory already linked to B left B linked
while the trail claimed the link set had been replaced — a record that disagreed
with the row it described.

**Add is the behaviour that was kept, and the wording is what changed.** Nothing
documented a replace: not the schema field, not the OpenAPI spec, not the route
docstring (which documents ``metadata``'s merge-vs-replace in detail and was
silent here), not the integration guide, and no test pinned one. The response
body has always reported the true, additive link set. Turning it into a real
replace would mean a shipped endpoint silently starting to DELETE links a caller
did not name — a destructive change to a public API, which is not something to
infer from a stale comment.

Both halves are pinned below, because either alone would let the bug back:
``test_a_patch_does_not_remove_links_it_does_not_name`` fails if someone
implements the replace, and ``test_the_audit_record_says_add_not_replace`` fails
if the wording drifts back.
"""

from __future__ import annotations

from uuid import UUID, uuid4

import pytest
from core_storage_api.services.postgres_service import get_read_session
from sqlalchemy import select, text

from common.embedding import fake_embedding
from common.models.audit import AuditLog

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


def _t() -> str:
    """A tenant id unique to one test AND visible to the end-of-run sweep.

    ``test-tenant-`` is what ``_setup_schema``'s teardown matches
    (``tenant_id LIKE 'test-tenant-%'``, and it reaches ``memory_entity_links``
    through the ``memories`` subquery), so any other prefix leaks rows — see
    ``tests.conftest.new_tenant_id``, which is the same contract.
    """
    return f"test-tenant-links-{uuid4().hex[:8]}"


async def _memory(sc, tenant: str) -> UUID:
    content = f"additive links {uuid4().hex[:8]}"
    row = await sc.create_memory(
        {
            "tenant_id": tenant,
            "agent_id": "test-agent",
            "content": content,
            "memory_type": "fact",
            "weight": 0.5,
            "status": "active",
            "visibility": "scope_team",
            "embedding": fake_embedding(content),
        }
    )
    return UUID(row["id"])


async def _entity(sc, tenant: str, name: str) -> str:
    row = await sc.create_entity(
        {
            "tenant_id": tenant,
            "entity_type": "person",
            "canonical_name": f"{name}-{uuid4().hex[:6]}",
        }
    )
    return row["id"]


async def _links(memory_id: UUID) -> dict[str, str]:
    """Read the raw join table, tenant-blind, to assert what actually persisted.

    SQL rather than ``memory_get_entity_links_for_memories``, which is now
    tenant-scoped on both ends. Everything here lives in one tenant so a scoped
    read would agree today, but an oracle that filters by the same predicate the
    code under test applies is one refactor away from asserting nothing.
    """
    async with get_read_session() as session:
        rows = (
            await session.execute(
                text("SELECT entity_id, role FROM memory_entity_links WHERE memory_id = :mid"),
                {"mid": memory_id},
            )
        ).all()
    return {str(entity_id): role for entity_id, role in rows}


async def _patch_links(memory_id: UUID, tenant: str, links: list[dict]) -> None:
    from core_api.schemas import MemoryUpdate
    from core_api.services.memory_service import update_memory

    await update_memory(memory_id, tenant, MemoryUpdate(entity_links=links))


class TestEntityLinksAreAdditive:
    async def test_a_patch_does_not_remove_links_it_does_not_name(self, sc) -> None:
        """The behaviour the wording now describes: B survives a PATCH naming only A."""
        tenant = _t()
        memory_id = await _memory(sc, tenant)
        a = await _entity(sc, tenant, "Aaa")
        b = await _entity(sc, tenant, "Bbb")

        await _patch_links(memory_id, tenant, [{"entity_id": b, "role": "object"}])
        assert await _links(memory_id) == {b: "object"}

        await _patch_links(memory_id, tenant, [{"entity_id": a, "role": "subject"}])

        assert await _links(memory_id) == {b: "object", a: "subject"}, (
            "a PATCH naming only A must not remove B — this endpoint has no replace mode"
        )

    async def test_a_resent_pair_keeps_its_original_role(self, sc) -> None:
        """``ON CONFLICT DO NOTHING`` preserves the first role, and that is deliberate.

        CAURA-686 chose DO NOTHING so concurrent writes to the same pair do not
        serialise on ``Lock/transactionid``. A caller cannot re-point a role by
        re-sending the pair, which is worth pinning: it is the second half of
        "additive", and the half most likely to be mistaken for a bug.
        """
        tenant = _t()
        memory_id = await _memory(sc, tenant)
        a = await _entity(sc, tenant, "Aaa")

        await _patch_links(memory_id, tenant, [{"entity_id": a, "role": "subject"}])
        await _patch_links(memory_id, tenant, [{"entity_id": a, "role": "object"}])

        assert await _links(memory_id) == {a: "subject"}, "the original role must survive a re-send"

    async def test_the_audit_record_says_add_not_replace(self, sc) -> None:
        """The trail must describe what happened to the row.

        Asserted against the ``audit_log`` table rather than a captured hook call,
        so what is pinned is the record an operator actually reads.
        """
        tenant = _t()
        memory_id = await _memory(sc, tenant)
        a = await _entity(sc, tenant, "Aaa")
        b = await _entity(sc, tenant, "Bbb")

        await _patch_links(memory_id, tenant, [{"entity_id": b, "role": "object"}])
        await _patch_links(memory_id, tenant, [{"entity_id": a, "role": "subject"}])

        async with get_read_session() as session:
            rows = (
                (
                    await session.execute(
                        select(AuditLog)
                        .where(AuditLog.tenant_id == tenant, AuditLog.action == "update")
                        .order_by(AuditLog.created_at)
                    )
                )
                .scalars()
                .all()
            )
        assert rows, "expected an update audit row for the PATCH"
        entry = (rows[-1].detail or {}).get("changes", {}).get("entity_links")
        assert entry is not None, "the PATCH must be recorded in the audit trail"

        assert entry.get("mode") == "add", f"audit must state the additive mode, got {entry!r}"
        # The specific regression: the row still holds B, so nothing was replaced.
        assert "replaced" not in str(entry), (
            f"audit claims a removal that never happens on this path: {entry!r}"
        )
        assert b in await _links(memory_id), "guard: B is still linked, so 'replaced' would be false"
