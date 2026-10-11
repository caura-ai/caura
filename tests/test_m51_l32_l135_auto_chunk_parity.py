"""An auto-chunked write keeps what the single write keeps (B25: M-51, L-32, L-135).

A write longer than ``CHUNKING_THRESHOLD_CHARS``, on a tenant with auto-chunking
on, is stored as a parent row plus one child per fact the chunker extracts. That
branch builds its rows by hand, and three things the single write does were
missing from it:

- M-51: the caller's ``entity_links`` were never written, and the answer echoed
  ``entity_links=[]``. Owner decision (2026-10-05): the links go on the parent
  only, and the answer echoes the links that persisted, as the single write's
  does (H-05).
- L-32: ``is_inferred``, which the crystallizer and insights pass, reached
  neither the parent nor the children. The children copy the parent's flag.
- L-135: ``_chunk_content`` checked a chunk's ``suggested_type`` against
  ``MEMORY_TYPES`` only, so a server-reserved type (``outcome``, ``rule``)
  reached a child row. It now coerces to ``MEMORY_TYPES_WRITE``, as
  ``ingest_commit`` already did (M-42), so preview, auto-chunk and commit agree.

Real storage through the in-process bridge. Only the tenant config and the
chunking LLM are stubbed.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch
from uuid import UUID, uuid4

import pytest
from sqlalchemy import text

from common.enrichment.constants import DEFAULT_MEMORY_TYPE
from core_api.constants import CHUNKING_THRESHOLD_CHARS
from core_api.schemas import EntityLinkIn, MemoryCreate
from core_api.services import ingest_service
from core_api.services.memory_service import create_memory
from core_api.services.organization_settings import ResolvedConfig
from core_storage_api.services.postgres_service import get_read_session
from tests.conftest import new_tenant_id

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

CHUNKS = (
    "The quarterly report shows revenue grew by twelve percent.",
    "The migration to the new billing system finished in March.",
)
_LINE = "Quarterly report notes and the billing migration status. "
CONTENT = _LINE * (CHUNKING_THRESHOLD_CHARS // len(_LINE) + 1)


def _config() -> ResolvedConfig:
    return ResolvedConfig(
        {
            "chunking": {"auto_chunk_enabled": True},
            "entity_extraction": {"enabled": False},
        }
    )


async def _write(
    tenant: str,
    *,
    entity_links: tuple[EntityLinkIn, ...] = (),
    is_inferred: bool = False,
):
    async def _chunks(_content, _focus, _cfg):
        return [{"content": c, "suggested_type": "fact"} for c in CHUNKS]

    with (
        patch(
            "core_api.services.organization_settings.resolve_config",
            AsyncMock(return_value=_config()),
        ),
        patch.object(ingest_service, "_chunk_content", new=_chunks),
    ):
        return await create_memory(
            MemoryCreate(
                tenant_id=tenant,
                agent_id="writer",
                content=CONTENT,
                entity_links=list(entity_links),
            ),
            is_inferred=is_inferred,
        )


async def _family(parent_id: UUID) -> dict[str, bool]:
    """``{memory_id: is_inferred}`` for the parent and its children, read from
    the table so the oracle is not the code under test."""
    async with get_read_session() as session:
        rows = (
            await session.execute(
                text(
                    "SELECT m.id, m.is_inferred FROM memories m WHERE m.id = :pid"
                    " OR m.metadata->>'parent_memory_id' = :pid_text"
                ),
                {"pid": parent_id, "pid_text": str(parent_id)},
            )
        ).all()
    return {str(memory_id): inferred for memory_id, inferred in rows}


async def _links(parent_id: UUID) -> set[tuple[str, str, str]]:
    """Every ``(memory_id, entity_id, role)`` link on the parent or a child."""
    async with get_read_session() as session:
        rows = (
            await session.execute(
                text(
                    "SELECT l.memory_id, l.entity_id, l.role"
                    " FROM memory_entity_links l JOIN memories m ON m.id = l.memory_id"
                    " WHERE m.id = :pid OR m.metadata->>'parent_memory_id' = :pid_text"
                ),
                {"pid": parent_id, "pid_text": str(parent_id)},
            )
        ).all()
    return {(str(m), str(e), role) for m, e, role in rows}


async def test_the_callers_links_land_on_the_parent_only(sc):
    tenant = new_tenant_id()
    row = await sc.create_entity(
        {
            "tenant_id": tenant,
            "entity_type": "concept",
            "canonical_name": f"billing migration {uuid4().hex[:6]}",
        }
    )
    entity = row["id"]

    out = await _write(
        tenant,
        entity_links=(
            EntityLinkIn(entity_id=UUID(entity), role="subject"),
            # No such entity: dropped, as the single write drops it.
            EntityLinkIn(entity_id=uuid4(), role="object"),
        ),
    )

    family = await _family(out.id)
    assert len(family) == 1 + len(CHUNKS), "the multi-fact branch ran"
    assert await _links(out.id) == {(str(out.id), entity, "subject")}
    echoed = [(str(link.entity_id), link.role) for link in out.entity_links]
    assert echoed == [(entity, "subject")], "the answer echoes what persisted"


@pytest.mark.parametrize("inferred", [True, False])
async def test_the_parent_and_its_children_carry_is_inferred(sc, inferred):
    out = await _write(new_tenant_id(), is_inferred=inferred)

    family = await _family(out.id)
    assert len(family) == 1 + len(CHUNKS), "the multi-fact branch ran"
    assert set(family.values()) == {inferred}, family


async def test_a_chunk_type_a_caller_may_not_write_becomes_the_default():
    raw = [
        {
            "content": "The release shipped on time after the review.",
            "suggested_type": "outcome",
        },
        {
            "content": "Every deploy must pass the staging smoke test.",
            "suggested_type": "rule",
        },
        {
            "content": "The team chose Postgres for the new ledger service.",
            "suggested_type": "decision",
        },
    ]
    llm = AsyncMock(return_value=raw)
    with patch.object(ingest_service, "call_with_fallback", llm):
        facts = await ingest_service._chunk_content(
            "source text", None, ResolvedConfig({})
        )

    assert [f["suggested_type"] for f in facts] == [
        DEFAULT_MEMORY_TYPE,
        DEFAULT_MEMORY_TYPE,
        "decision",
    ]
