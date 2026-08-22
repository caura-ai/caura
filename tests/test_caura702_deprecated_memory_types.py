"""CAURA-702 — classifier-deprecated memory types (currently ``semantic``) are
kept out of the agent-writable vocabulary and folded into the default on every
write path.

Two behaviours are covered:

1. **Schema descriptions.** WRITE fields (create/update) advertise only the
   types a caller may set — no ``semantic`` and no server-reserved
   ``outcome``/``rule``/``insight``. The recall/list FILTER field still lists
   every type that may EXIST in storage, so historical rows written before a
   type was reserved/deprecated stay queryable.

2. **Bulk/ingest demotion.** A caller-supplied deprecated type is stored as the
   default (``fact``). The single-write pipeline already did this in
   ``MergeEnrichmentFields``; the bulk path — and ingest, which funnels through
   ``create_memories_bulk`` — now match, closing the CAURA-701 gap where
   ``semantic`` persisted via these paths.
"""

from __future__ import annotations

import uuid

import pytest

from common.enrichment.constants import (
    CLASSIFIER_DEPRECATED_MEMORY_TYPES,
    SERVER_RESERVED_MEMORY_TYPES,
)
from core_api.clients.storage_client import get_storage_client
from core_api.constants import (
    DEFAULT_MEMORY_TYPE,
    MEMORY_TYPES,
    MEMORY_TYPES_FILTER_DESCRIPTION,
    MEMORY_TYPES_WRITE,
    MEMORY_TYPES_WRITE_DESCRIPTION,
)
from core_api.schemas import (
    BulkMemoryCreate,
    BulkMemoryItem,
    MemoryCreate,
    MemoryUpdate,
)
from core_api.services.memory_service import (
    create_memories_bulk,
    create_memory,
    update_memory,
)

# Long enough to clear CheckContentLength's minimum-length quality gate.
_PADDING = (
    " This memory carries enough surrounding context to pass the content-length gate."
)


def _tenant() -> str:
    # ``test-tenant-%`` rows are auto-cleaned by the conftest schema fixture.
    return f"test-tenant-caura702-{uuid.uuid4().hex[:8]}"


# ---------------------------------------------------------------------------
# 1. Description split (pure unit — no DB)
# ---------------------------------------------------------------------------


def test_writable_types_exclude_deprecated_and_reserved():
    for t in CLASSIFIER_DEPRECATED_MEMORY_TYPES:
        assert t not in MEMORY_TYPES_WRITE, f"deprecated {t!r} leaked into writable set"
    for t in SERVER_RESERVED_MEMORY_TYPES:
        assert t not in MEMORY_TYPES_WRITE, f"reserved {t!r} leaked into writable set"
    # Everything else survives, in MEMORY_TYPES order.
    expected = [
        t
        for t in MEMORY_TYPES
        if t not in SERVER_RESERVED_MEMORY_TYPES
        and t not in CLASSIFIER_DEPRECATED_MEMORY_TYPES
    ]
    assert list(MEMORY_TYPES_WRITE) == expected


def test_write_description_hides_semantic_but_keeps_valid_values():
    assert "semantic" not in MEMORY_TYPES_WRITE_DESCRIPTION
    # openapi docs-lock regression test asserts this substring survives.
    assert "Valid values" in MEMORY_TYPES_WRITE_DESCRIPTION
    assert "fact" in MEMORY_TYPES_WRITE_DESCRIPTION
    assert "decision" in MEMORY_TYPES_WRITE_DESCRIPTION


def test_filter_description_still_lists_every_stored_type():
    # Historical ``semantic``/``insight``/… rows must remain filterable.
    for t in MEMORY_TYPES:
        assert t in MEMORY_TYPES_FILTER_DESCRIPTION, f"filter dropped {t!r}"


# ---------------------------------------------------------------------------
# 2. Bulk demotion (integration — real storage, covers ingest via bulk)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_bulk_write_demotes_deprecated_semantic_to_fact():
    tenant = _tenant()
    req = BulkMemoryCreate(
        tenant_id=tenant,
        fleet_id="test-fleet",
        agent_id="test-agent",
        items=[
            BulkMemoryItem(
                content="SKILLS EXPORT: single-file deploy workflow reference."
                + _PADDING,
                memory_type="semantic",
            )
        ],
    )
    resp = await create_memories_bulk(req, bulk_attempt_id=uuid.uuid4().hex)
    assert resp.results[0].status == "created", resp.results[0]

    mem = await get_storage_client().get_memory(resp.results[0].id)
    assert mem["memory_type"] == DEFAULT_MEMORY_TYPE  # "fact" — demoted from "semantic"


@pytest.mark.asyncio
async def test_bulk_write_keeps_non_deprecated_type():
    """Control: an ordinary writable type is stored unchanged."""
    tenant = _tenant()
    req = BulkMemoryCreate(
        tenant_id=tenant,
        fleet_id="test-fleet",
        agent_id="test-agent",
        items=[
            BulkMemoryItem(
                content="We decided to go with Postgres over MongoDB." + _PADDING,
                memory_type="decision",
            )
        ],
    )
    resp = await create_memories_bulk(req, bulk_attempt_id=uuid.uuid4().hex)
    assert resp.results[0].status == "created", resp.results[0]

    mem = await get_storage_client().get_memory(resp.results[0].id)
    assert mem["memory_type"] == "decision"


@pytest.mark.asyncio
async def test_update_demotes_deprecated_semantic_to_fact():
    """The update path (PATCH / MCP op=update) folds a caller-supplied deprecated
    type into the default — it bypasses MergeEnrichmentFields, so update_memory
    enforces the merger itself. Created as ``decision`` so the demotion (current
    type != default) is exercised deterministically."""
    tenant = _tenant()
    created = await create_memory(
        MemoryCreate(
            tenant_id=tenant,
            fleet_id="test-fleet",
            agent_id="test-agent",
            content="We decided to reuse the affiliate deploy workflow." + _PADDING,
            memory_type="decision",
            entity_links=[],
        )
    )
    updated = await update_memory(
        created.id, tenant, MemoryUpdate(memory_type="semantic")
    )
    assert updated.memory_type == DEFAULT_MEMORY_TYPE  # demoted from "semantic"

    mem = await get_storage_client().get_memory(created.id)
    assert mem["memory_type"] == DEFAULT_MEMORY_TYPE


@pytest.mark.asyncio
async def test_update_semantic_on_fact_row_is_noop():
    """A semantic->fact PATCH on a row already stored as the default records no
    phantom change (guards the demotion block against an old==new audit entry)."""
    tenant = _tenant()
    created = await create_memory(
        MemoryCreate(
            tenant_id=tenant,
            fleet_id="test-fleet",
            agent_id="test-agent",
            content="A durable reference fact about the deploy workflow." + _PADDING,
            memory_type="fact",
            entity_links=[],
        )
    )
    updated = await update_memory(
        created.id, tenant, MemoryUpdate(memory_type="semantic")
    )
    assert updated.memory_type == DEFAULT_MEMORY_TYPE  # unchanged — stays "fact"

    mem = await get_storage_client().get_memory(created.id)
    assert mem["memory_type"] == DEFAULT_MEMORY_TYPE
