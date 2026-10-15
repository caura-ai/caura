"""Usage metering, savings side: only the measures Caura can stand behind.

Recall: tokens returned, and for a piece of a larger memory (auto-chunk or
atomic fact, named by the reserved ``parent_memory_id``) the full size of the
distinct parents it came from. A whole memory saves nothing. Results another
agent wrote count as shared memory in use. Duplicate merges record what they
retired.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from common.usage import ModelCall, ValueRecord, set_usage_sink, usage_scope
from core_api.pipeline.steps.search import record_recall_value as rrv
from core_api.services.ingest_chunking import doc_token_count


class ListSink:
    def __init__(self) -> None:
        self.values: list[ValueRecord] = []

    def add_model_call(self, call: ModelCall) -> None:
        pass

    def add_value(self, record: ValueRecord) -> None:
        self.values.append(record)

    def totals(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for v in self.values:
            out[v.metric] = out.get(v.metric, 0) + v.quantity
        return out


@pytest.fixture
def sink():
    s = ListSink()
    set_usage_sink(s)
    yield s
    set_usage_sink(None)


def _row(content, *, agent="writer", parent=None, source=None, nested=False):
    meta: dict = {}
    if parent or source:
        target = meta.setdefault("_system", {}) if nested else meta
        if parent:
            target["parent_memory_id"] = parent
        if source:
            target["source"] = source
    return SimpleNamespace(content=content, agent_id=agent, metadata=meta)


PARENT = "the whole runbook " * 200


def test_a_chunk_or_fact_names_its_parent_and_a_forged_source_does_not():
    assert rrv.parent_of({"parent_memory_id": "p1", "source": "auto_chunk"}) == "p1"
    assert (
        rrv.parent_of(
            {"_system": {"parent_memory_id": "p1", "source": "atomic_fact_fanout"}}
        )
        == "p1"
    )
    # ``source`` is caller-writable; without the reserved parent key it proves nothing.
    assert rrv.parent_of({"source": "auto_chunk"}) is None
    assert rrv.parent_of({"parent_memory_id": "p1", "source": "ingest"}) is None
    assert rrv.parent_of(None) is None


def test_whole_memories_return_tokens_but_claim_no_saving():
    measures, parents = rrv.summarize(
        [_row("short fact one"), _row("short fact two")], "writer"
    )
    assert parents == set()
    assert measures["recall.derived_returned_tokens"] == 0
    assert measures["recall.returned_tokens"] == doc_token_count(
        "short fact one"
    ) + doc_token_count("short fact two")
    assert measures["recall.count"] == 1 and measures["recall.results"] == 2


def test_chunks_of_one_parent_count_the_parent_once():
    rows = [
        _row("chunk a", parent="p1", source="auto_chunk"),
        _row("chunk b", parent="p1", source="auto_chunk"),
    ]
    measures, parents = rrv.summarize(rows, "writer")
    assert parents == {"p1"}
    assert measures["recall.derived_returned_tokens"] == doc_token_count(
        "chunk a"
    ) + doc_token_count("chunk b")


def test_results_another_agent_wrote_count_as_shared_and_need_a_known_caller():
    rows = [_row("x", agent="writer"), _row("y", agent="other"), _row("z", agent=None)]
    assert rrv.summarize(rows, "writer")[0]["recall.cross_agent_results"] == 1
    assert rrv.summarize(rows, None)[0]["recall.cross_agent_results"] == 0


async def test_the_parents_full_size_is_read_and_a_deleted_parent_claims_nothing(
    sink, monkeypatch
):
    async def bulk_get(ids, tenant_id):
        assert tenant_id == "t1"
        return [{"content": PARENT} if i == "p1" else None for i in ids]

    monkeypatch.setattr(
        rrv, "get_storage_client", lambda: SimpleNamespace(bulk_get_memories=bulk_get)
    )
    measures, parents = rrv.summarize(
        [
            _row("chunk a", parent="p1", source="auto_chunk"),
            _row("gone", parent="p2", source="auto_chunk"),
        ],
        "writer",
    )
    with usage_scope(tenant_id="t1", agent_id="writer"):
        await rrv._record("t1", "search", measures, parents)
    totals = sink.totals()
    assert totals["recall.derived_source_tokens"] == doc_token_count(PARENT)
    assert {v.scope.operation for v in sink.values} == {"search"}


async def test_a_diagnostic_search_records_nothing(sink, monkeypatch):
    scheduled = []
    monkeypatch.setattr(rrv, "track_task", lambda coro: scheduled.append(coro))
    ctx = SimpleNamespace(
        data={"diagnostic": True, "results": [_row("x")], "tenant_id": "t1"}
    )
    await rrv.RecordRecallValue().execute(ctx)
    assert scheduled == []
    ctx.data["diagnostic"] = False
    await rrv.RecordRecallValue().execute(ctx)
    assert len(scheduled) == 1
    scheduled[0].close()


async def test_a_near_duplicate_merge_records_what_it_retired(sink):
    from core_api.pipeline.steps.write.schedule_background_tasks import _record_retired

    async def get_memory(memory_id, tenant_id, read=True):
        return {"content": "the old duplicate claim"}

    sc = SimpleNamespace(get_memory=get_memory)
    await _record_retired(sc, "c1", "t1", 42)
    await _record_retired(sc, "c2", "t1", None)
    totals = sink.totals()
    assert totals["hygiene.retired_memories"] == 2
    assert totals["hygiene.retired_tokens"] == 42 + doc_token_count(
        "the old duplicate claim"
    )
    assert {v.scope.tenant_id for v in sink.values} == {"t1"}


def test_the_search_pipeline_ends_with_the_savings_step():
    from core_api.pipeline.compositions.search import build_search_pipeline

    names = [s.name for s in build_search_pipeline()._steps]
    assert names[-1] == "record_recall_value"
