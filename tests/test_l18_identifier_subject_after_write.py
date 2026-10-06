"""L-18: an identifier subject's entity is created only once the row is written.

EmitMemoryTriple's identifier path upserted the subject entity inside the step.
The step runs before the semantic gate in strong mode and before WriteMemoryRow
in every pipeline, so a write refused afterwards (a semantic-duplicate 409, or
the concurrent-insert 409 in WriteMemoryRow) left an entity no memory
references, listable through ``/entities`` and ``/graph``. The step's own
"Phase B" deferral only covered the gates inside the step.

The step now looks the identifier up and creates nothing. A miss is held as a
pending triple. The semantic gate still treats it as a subject no candidate can
share, as it treated the fresh entity's id. A step right after WriteMemoryRow
creates the entity and writes the triple to the row.

The new step is reached through the pipeline builders, so on a tree without it
these tests fail on an assertion rather than an import.
"""

from __future__ import annotations

import logging
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID, uuid4

import pytest

from core_api.pipeline.compositions.write import (
    build_fast_persist_pipeline,
    build_fast_write_pipeline,
    build_persist_pipeline,
    build_strong_write_pipeline,
)
from core_api.pipeline.context import PipelineContext
from core_api.pipeline.step import StepOutcome
from core_api.pipeline.steps.write.check_semantic_duplicate import (
    CheckSemanticDuplicate,
)
from core_api.pipeline.steps.write.emit_memory_triple import EmitMemoryTriple
from core_api.schemas import MemoryCreate

TOKEN = "TOKEN-736C57D0"
CONTENT = f"{TOKEN} has release date 2027-05-01"
FLEET = "fleet-a"


def _tenant() -> str:
    return f"test-l18-{uuid4().hex[:8]}"


def _ctx(tenant: str) -> tuple[MemoryCreate, PipelineContext]:
    data = MemoryCreate(
        tenant_id=tenant, fleet_id=FLEET, agent_id="test-agent", content=CONTENT
    )
    ctx = PipelineContext(
        data={"input": data, "memory_fields": {"metadata": {}}},
        tenant_config=SimpleNamespace(triple_emission_enabled=True),
    )
    return data, ctx


def _step_after_the_write(pipeline):
    names = [step.name for step in pipeline._steps]
    step = pipeline._steps[names.index("write_memory_row") + 1]
    assert step.name == "create_pending_subject", names
    return step


@pytest.mark.unit
@pytest.mark.parametrize(
    "builder",
    [
        build_strong_write_pipeline,
        build_fast_write_pipeline,
        build_persist_pipeline,
        build_fast_persist_pipeline,
    ],
)
def test_every_pipeline_that_emits_a_triple_creates_its_subject_after_the_write(
    builder,
):
    names = [step.name for step in builder()._steps]
    assert names.index("emit_memory_triple") < names.index("write_memory_row")
    _step_after_the_write(builder())


@pytest.mark.integration
@pytest.mark.asyncio
async def test_a_new_identifier_creates_no_entity_before_the_write(sc):
    """What a refused write leaves behind: everything up to the gates ran."""
    tenant = _tenant()
    data, ctx = _ctx(tenant)

    assert await EmitMemoryTriple().execute(ctx) is None

    assert await sc.list_entities(tenant) == []
    assert data.subject_entity_id is None


@pytest.mark.integration
@pytest.mark.asyncio
async def test_a_known_identifier_is_the_subject_at_once(sc):
    """The control: an identifier the table already holds is used as before."""
    tenant = _tenant()
    existing = await sc.create_entity(
        {
            "tenant_id": tenant,
            "fleet_id": FLEET,
            "entity_type": "identifier",
            "canonical_name": TOKEN,
            "attributes": {},
        }
    )
    data, ctx = _ctx(tenant)

    assert await EmitMemoryTriple().execute(ctx) is None

    assert data.subject_entity_id == UUID(str(existing["id"]))
    assert [e["id"] for e in await sc.list_entities(tenant)] == [existing["id"]]


@pytest.mark.integration
@pytest.mark.asyncio
async def test_the_entity_and_the_triple_land_after_the_write(sc):
    tenant = _tenant()
    data, ctx = _ctx(tenant)
    assert await EmitMemoryTriple().execute(ctx) is None
    row = await sc.create_memory(
        {
            "tenant_id": tenant,
            "fleet_id": FLEET,
            "agent_id": "test-agent",
            "memory_type": "fact",
            "content": CONTENT,
            "status": "active",
            "visibility": "scope_team",
        }
    )
    ctx.data["memory_id"] = row["id"]
    ctx.data["memory"] = row

    step = _step_after_the_write(build_strong_write_pipeline())
    assert await step.execute(ctx) is None

    [entity] = await sc.list_entities(tenant)
    assert (entity["canonical_name"], entity["entity_type"]) == (TOKEN, "identifier")
    stored = await sc.get_memory(str(row["id"]), tenant, read=False)
    triple = (stored["subject_entity_id"], stored["predicate"], stored["object_value"])
    assert triple == (str(entity["id"]), "release_date", "2027-05-01")
    assert data.subject_entity_id == UUID(str(entity["id"]))


@pytest.mark.unit
@pytest.mark.asyncio
async def test_a_failed_create_leaves_the_row_without_a_triple():
    """Never breaks the write: the old step skipped on the same failure."""
    data, ctx = _ctx(_tenant())
    ctx.data["pending_subject"] = {
        "canonical_name": TOKEN,
        "predicate": "release_date",
        "object_value": "2027-05-01",
    }
    ctx.data["memory_id"] = str(uuid4())
    step = _step_after_the_write(build_strong_write_pipeline())
    module = sys.modules[type(step).__module__]
    storage = MagicMock(update_memory=AsyncMock())

    with (
        patch.object(
            module, "upsert_entity", new=AsyncMock(side_effect=RuntimeError("down"))
        ),
        patch.object(module, "get_storage_client", return_value=storage),
    ):
        result = await step.execute(ctx)

    assert result.outcome == StepOutcome.SKIPPED
    assert result.detail["reason"] == "subject_upsert_failed"
    storage.update_memory.assert_not_awaited()
    assert data.subject_entity_id is None


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize(
    "update",
    [
        pytest.param({"return_value": None}, id="row-gone"),
        pytest.param({"side_effect": RuntimeError("down")}, id="update-raises"),
    ],
)
async def test_a_failed_update_gets_no_triple_and_names_the_entity(update, caplog):
    """``update_memory`` answers None for a missing row (a 404) rather than
    raising, so the step must not report a triple the row does not hold.

    Either way the entity is already committed and may now be unreferenced, so
    the warning names it, and a raising update is not reported as an upsert
    failure."""
    data, ctx = _ctx(_tenant())
    ctx.data["pending_subject"] = {
        "canonical_name": TOKEN,
        "predicate": "release_date",
        "object_value": "2027-05-01",
    }
    ctx.data["memory_id"] = str(uuid4())
    ctx.data["memory"] = {"id": ctx.data["memory_id"]}
    entity_id = uuid4()
    step = _step_after_the_write(build_strong_write_pipeline())
    module = sys.modules[type(step).__module__]
    storage = MagicMock(update_memory=AsyncMock(**update))

    with (
        patch.object(
            module,
            "upsert_entity",
            new=AsyncMock(return_value=SimpleNamespace(id=entity_id)),
        ),
        patch.object(module, "get_storage_client", return_value=storage),
        caplog.at_level(logging.WARNING, logger=module.__name__),
    ):
        result = await step.execute(ctx)

    assert result is not None and result.outcome == StepOutcome.SKIPPED, result
    assert result.detail["reason"] == "subject_update_failed"
    assert data.subject_entity_id is None
    assert "subject_entity_id" not in ctx.data["memory"]
    warnings = [r.getMessage() for r in caplog.records if r.name == module.__name__]
    assert any(
        str(entity_id) in w and data.tenant_id in w and ctx.data["memory_id"] in w
        for w in warnings
    ), warnings


@pytest.mark.unit
@pytest.mark.asyncio
async def test_the_semantic_gate_treats_a_new_identifier_as_a_different_subject():
    """A1 #17's preflight saw the freshly created entity's id, which no
    candidate could hold. A pending identifier must keep that answer, or a
    judge-band candidate about another subject goes to the LLM judge."""
    ctx = MagicMock()
    ctx.tenant_config = MagicMock(semantic_dedup_enabled=True)
    ctx.data = {
        "input": MagicMock(
            tenant_id="t1",
            fleet_id=FLEET,
            visibility="scope_team",
            subject_entity_id=None,
            content="new statement",
        ),
        "embedding": [0.1] * 10,
        "memory_fields": {"metadata": {}},
        "pending_subject": {
            "canonical_name": "billing-api-gateway",
            "predicate": "status",
            "object_value": "down",
        },
    }
    candidate = {
        "id": str(uuid4()),
        "similarity": 0.91,
        "content": "candidate statement",
        "subject_entity_id": str(uuid4()),
    }
    judge = AsyncMock(return_value=(False, 0.0))
    gate = "core_api.pipeline.steps.write.check_semantic_duplicate"

    with (
        patch(
            f"{gate}._find_semantic_duplicate", new=AsyncMock(return_value=candidate)
        ),
        patch(f"{gate}._llm_dedup_check", new=judge),
    ):
        result = await CheckSemanticDuplicate().execute(ctx)

    assert result is None
    judge.assert_not_called()
    metadata = ctx.data["memory_fields"]["metadata"]
    assert metadata["dedup_subject_preflight"] == "skipped_judge_subjects_differ"
