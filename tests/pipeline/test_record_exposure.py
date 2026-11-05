"""Unit tests for memory exposure recording (no DB).

``schedule_exposure`` decides what a read exposed and to whom; the storage call
and ``track_task`` are patched out. The rules: a chunk or an atomic fact counts
for its parent, STM-injected rows are not memories, a read with no agent or by
a person records nothing, one batch per tenant, and a failure never raises.
"""

import contextlib
import uuid
from types import SimpleNamespace

import pytest

from core_api.audit_actor import audit_principal
from core_api.pipeline.compositions.search import (
    build_search_explain_pipeline,
    build_search_pipeline,
)
from core_api.pipeline.context import PipelineContext
from core_api.pipeline.steps.search.record_exposure import RecordExposure
from core_api.services import memory_exposure as mod


def _hit(tenant="t1", metadata=None, injected=False):
    return SimpleNamespace(
        id=uuid.uuid4(), tenant_id=tenant, metadata=metadata, injected=injected
    )


def _chunk_of(parent, source="auto_chunk"):
    return {"_system": {"parent_memory_id": str(parent), "source": source}}


@pytest.fixture
def captured(monkeypatch):
    calls: list[tuple[str, dict]] = []

    async def fake_record(tenant_id, body):
        calls.append((tenant_id, body))

    def run_now(coro):
        # Drive the coroutine to completion synchronously: fake_record never awaits.
        with contextlib.suppress(StopIteration):
            coro.send(None)

    monkeypatch.setattr(mod, "_record", fake_record)
    monkeypatch.setattr(mod, "track_task", run_now)
    monkeypatch.setattr(mod, "server_trace_id", lambda: "mcp-abc")
    return calls


def test_one_batch_with_the_agent_the_trace_and_each_memory_once(captured):
    a, b = _hit(), _hit()
    mod.schedule_exposure([a, b, a], agent_id="agent-1", tenant_id="t1")

    [(tenant, body)] = captured
    assert tenant == "t1"
    assert body["agent_id"] == "agent-1"
    assert body["memory_ids"] == [str(a.id), str(b.id)]
    assert body["trace_id"] == "mcp-abc"
    assert body["seen_at"]


@pytest.mark.parametrize("source", ["auto_chunk", "atomic_fact_fanout"])
def test_a_derived_row_counts_for_its_parent(captured, source):
    parent = uuid.uuid4()
    whole = _hit()
    mod.schedule_exposure(
        [
            _hit(metadata=_chunk_of(parent, source)),
            _hit(metadata=_chunk_of(parent, source)),
            whole,
        ],
        agent_id="agent-1",
        tenant_id="t1",
    )
    [(_, body)] = captured
    assert body["memory_ids"] == [str(parent), str(whole.id)]


def test_storage_dicts_are_read_through_metadata_underscore(captured):
    parent = uuid.uuid4()
    row = {"id": str(uuid.uuid4()), "tenant_id": "t1", "metadata_": _chunk_of(parent)}
    mod.schedule_exposure([row], agent_id="agent-1", tenant_id="t1")
    [(_, body)] = captured
    assert body["memory_ids"] == [str(parent)]


def test_a_parent_key_from_another_writer_is_not_followed(captured):
    # ``parent_memory_id`` is reserved but ``source`` is not: only the two
    # derivation writers' rows map to a parent (record_recall_value.parent_of).
    row = _hit(metadata={"parent_memory_id": str(uuid.uuid4()), "source": "caller"})
    mod.schedule_exposure([row], agent_id="agent-1", tenant_id="t1")
    [(_, body)] = captured
    assert body["memory_ids"] == [str(row.id)]


def test_injected_rows_are_not_memories(captured):
    real = _hit()
    mod.schedule_exposure(
        [_hit(injected=True), real], agent_id="agent-1", tenant_id="t1"
    )
    [(_, body)] = captured
    assert body["memory_ids"] == [str(real.id)]


def test_one_batch_per_tenant_the_rows_came_from(captured):
    home, sibling = _hit("t1"), _hit("t2")
    mod.schedule_exposure([home, sibling], agent_id="agent-1", tenant_id="t1")
    assert sorted((t, b["memory_ids"]) for t, b in captured) == [
        ("t1", [str(home.id)]),
        ("t2", [str(sibling.id)]),
    ]


@pytest.mark.parametrize("agent_id", [None, ""])
def test_no_agent_records_nothing(captured, agent_id):
    mod.schedule_exposure([_hit()], agent_id=agent_id, tenant_id="t1")
    assert captured == []


def test_a_person_records_nothing(captured):
    token = audit_principal.set({"credential_kind": "person", "agent_id": None})
    try:
        mod.schedule_exposure(
            [_hit()], agent_id="named-by-the-dashboard", tenant_id="t1"
        )
    finally:
        audit_principal.reset(token)
    assert captured == []


def test_an_empty_read_records_nothing(captured):
    mod.schedule_exposure([], agent_id="agent-1", tenant_id="t1")
    mod.schedule_exposure(None, agent_id="agent-1", tenant_id="t1")
    assert captured == []


def test_a_failure_is_swallowed(monkeypatch):
    def boom(*_a, **_k):
        raise RuntimeError("storage down")

    monkeypatch.setattr(mod, "exposure_targets", boom)
    mod.schedule_exposure([_hit()], agent_id="agent-1", tenant_id="t1")


@pytest.mark.asyncio
async def test_the_storage_failure_is_logged_not_raised(monkeypatch, caplog):
    class Down:
        async def record_exposure(self, *_a, **_k):
            raise RuntimeError("storage down")

    monkeypatch.setattr(mod, "get_storage_client", lambda: Down())
    await mod._record("t1", {"agent_id": "a", "memory_ids": []})
    assert "memory exposure not recorded" in caplog.text


@pytest.mark.asyncio
async def test_the_step_hands_the_results_and_the_caller_over(captured):
    hit = _hit()
    ctx = PipelineContext(
        data={"results": [hit], "caller_agent_id": "agent-1", "tenant_id": "t1"}
    )
    await RecordExposure().execute(ctx)
    [(_, body)] = captured
    assert body == {**body, "agent_id": "agent-1", "memory_ids": [str(hit.id)]}


def test_search_records_exposure_and_explain_does_not():
    assert "record_exposure" in [s.name for s in build_search_pipeline()._steps]
    assert "record_exposure" not in [
        s.name for s in build_search_explain_pipeline()._steps
    ]
