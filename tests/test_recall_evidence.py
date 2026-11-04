"""Evidence contract v1 on the read side: EC-10, EC-11, EC-12.

EC-10 — every search/recall hit carries a compact ``evidence`` object (source,
trace, verification, why) with empty keys left off; ``diagnostic=true`` widens
it to the full detail. The six evidence columns are not repeated at the top
level of a hit, so a row pays for them once.

EC-11 — ``verified_only`` is a storage filter, applied by the scored search, the
entity short-circuit and the successor lookup alike. Not a ranking input.

EC-12 — ``why`` is a short deterministic phrase from the score factors and the
retrieval strategy. No LLM.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy import update
from sqlalchemy.dialects import postgresql

from common.embedding import fake_embedding
from common.models.memory import VERIFIED_STATUSES, Memory
from core_api.pipeline.context import PipelineContext
from core_api.pipeline.steps.search import load_and_serialize as las
from core_api.pipeline.steps.search.retrieval_types import (
    RetrievalPlan,
    RetrievalStrategy,
)
from core_api.schemas import MemoryOut, ScoreParts, SearchHitOut, SearchRequest
from core_api.services.evidence import (
    WHY_MAX_CHARS,
    recall_evidence,
    to_search_hit,
    why_phrase,
)
from core_storage_api.services import postgres_service as ps

# The compact evidence object of a typical tool-observed row, serialized as the
# MCP surface sends it. Raising this is a decision, not a drift: the object rides
# on every row of every recall.
COMPACT_EVIDENCE_MAX_BYTES = 140
# What a hit may grow by over the pre-contract row (MemoryOut without the six
# evidence columns): the ``"evidence":{...}`` key itself, nothing else.
HIT_GROWTH_MAX_BYTES = COMPACT_EVIDENCE_MAX_BYTES + len('"evidence":,')

EVIDENCE_COLUMNS = (
    "observed_at",
    "trace_id",
    "tool_call_id",
    "verification_status",
    "verified_by",
    "verified_at",
)


def _memory(**over):
    base = {
        "id": uuid.uuid4(),
        "tenant_id": "t",
        "fleet_id": "f",
        "agent_id": "agent-1",
        "agent_display_name": None,
        "memory_type": "fact",
        "title": "Deploy target",
        "content": "The staging deploy target is caura.dev.",
        "weight": 0.5,
        "source_uri": None,
        "run_id": None,
        "observed_at": None,
        "trace_id": "mcp-" + "5f2e" * 8,
        "tool_call_id": "call_abc123",
        "verification_status": "tool_observed",
        "verified_by": None,
        "verified_at": None,
        "metadata_": None,
        "created_at": "2026-11-04T10:00:00+00:00",
        "expires_at": None,
        "subject_entity_id": None,
        "predicate": None,
        "object_value": None,
        "ts_valid_start": None,
        "ts_valid_end": None,
        "status": "active",
        "visibility": "scope_team",
        "recall_count": 0,
        "last_recalled_at": None,
        "supersedes_id": None,
    }
    base.update(over)
    return SimpleNamespace(**base)


def _row(memory=None, **factors):
    parts = {
        "vec_sim": 0.82,
        "fts_score": 0.4,
        "freshness": 0.97,
        "entity_boost": 1.0,
        "recall_boost": 1.0,
        "temporal_boost": 1.0,
        "status_penalty": 1.0,
    }
    parts.update(factors)
    return SimpleNamespace(
        Memory=memory or _memory(),
        score=0.9,
        similarity=None,
        has_embedding=True,
        entity_links=[],
        **parts,
    )


async def _serialize(
    rows, *, diagnostic=False, strategy=None, monkeypatch, successors=()
):
    sc = MagicMock()
    sc.find_successors = AsyncMock(return_value=list(successors))
    monkeypatch.setattr(las, "get_storage_client", lambda: sc)
    data = {"filtered_rows": rows, "tenant_id": "t", "diagnostic": diagnostic}
    if strategy is not None:
        data["retrieval_plan"] = RetrievalPlan(strategy=strategy)
    ctx = PipelineContext(data=data)
    await las.LoadAndSerialize().execute(ctx)
    return ctx, sc


# ── EC-12: why ──────────────────────────────────────────────────────────────


@pytest.mark.unit
@pytest.mark.parametrize(
    ("parts", "strategy", "injected", "expected"),
    [
        ({"vec_sim": 0.8, "fts_score": 0.3}, None, False, "vector + keyword match"),
        ({"vec_sim": 0.8, "fts_score": 0.0}, None, False, "vector match"),
        ({"vec_sim": None, "fts_score": 0.5}, None, False, "keyword match"),
        ({"vec_sim": 0.8, "entity_boost": 1.2}, None, False, "vector + entity match"),
        (
            {"vec_sim": 0.8, "temporal_boost": 1.3},
            None,
            False,
            "vector match, in time range",
        ),
        ({"vec_sim": 0.8}, "recent_context", False, "vector match, recent"),
        (
            {"vec_sim": 0.8, "recall_boost": 1.1},
            None,
            False,
            "vector match, often recalled",
        ),
        (None, "entity_lookup", False, "entity match"),
        (None, "recent_context", False, "recent"),
        (None, None, False, "matched"),
        (None, None, True, "newest version of a recalled fact"),
    ],
)
def test_why_phrase(parts, strategy, injected, expected):
    sp = ScoreParts(**parts) if parts is not None else None
    assert why_phrase(sp, strategy=strategy, injected=injected) == expected


@pytest.mark.unit
def test_why_never_exceeds_its_bound():
    """Every signal and every qualifier at once still fits."""
    sp = ScoreParts(
        vec_sim=0.9,
        fts_score=0.9,
        entity_boost=1.5,
        recall_boost=1.5,
        temporal_boost=1.5,
        rerank=0.7,
    )
    for strategy in (None, *(s.value for s in RetrievalStrategy)):
        phrase = why_phrase(sp, strategy=strategy)
        assert len(phrase) <= WHY_MAX_CHARS, phrase


# ── EC-10: the evidence object ──────────────────────────────────────────────


@pytest.mark.unit
def test_compact_evidence_has_four_keys_and_drops_empty_ones():
    ev = recall_evidence(_memory(), score_parts=ScoreParts(vec_sim=0.8, fts_score=0.2))
    assert ev.model_dump(mode="json") == {
        "source": "tool:call_abc123",
        "trace": "mcp-" + "5f2e" * 8,
        "verification": "tool_observed",
        "why": "vector + keyword match",
    }

    bare = recall_evidence(
        _memory(trace_id=None, tool_call_id=None, verification_status="unverified"),
        score_parts=ScoreParts(vec_sim=0.8),
    )
    # No null keys on the wire: absent means "none".
    assert bare.model_dump(mode="json") == {
        "verification": "unverified",
        "why": "vector match",
    }


@pytest.mark.unit
def test_source_prefers_the_tool_call_then_a_real_source_uri():
    assert (
        recall_evidence(_memory(source_uri="https://x.test/a")).source
        == "tool:call_abc123"
    )
    uri = recall_evidence(_memory(tool_call_id=None, source_uri="https://x.test/a"))
    assert uri.source == "https://x.test/a"
    # Ingest's pasted-content marker is not a source.
    placeholder = recall_evidence(_memory(tool_call_id=None, source_uri="text-input"))
    assert placeholder.source is None


@pytest.mark.unit
def test_detail_adds_the_full_set_only_when_asked():
    mem = _memory(
        run_id="run-7", source_uri="https://x.test/a", verified_by="agent:boss"
    )
    compact = recall_evidence(mem).model_dump(mode="json")
    assert set(compact) == {"source", "trace", "verification", "why"}

    full = recall_evidence(mem, strategy="semantic_search", detail=True).model_dump(
        mode="json"
    )
    assert full["run_id"] == "run-7"
    assert full["tool_call_id"] == "call_abc123"
    assert full["source_uri"] == "https://x.test/a"
    assert full["verified_by"] == "agent:boss"
    assert full["strategy"] == "semantic_search"


@pytest.mark.unit
def test_compact_evidence_payload_size_is_pinned():
    """The per-row cost of EC-10, in bytes, on the MCP wire format."""
    ev = recall_evidence(_memory(), score_parts=ScoreParts(vec_sim=0.8, fts_score=0.2))
    body = json.dumps(
        ev.model_dump(mode="json"), separators=(",", ":"), ensure_ascii=False
    )
    assert len(body.encode()) <= COMPACT_EVIDENCE_MAX_BYTES, (len(body.encode()), body)


@pytest.mark.unit
async def test_a_hit_grows_only_by_its_evidence_and_does_not_repeat_the_columns(
    monkeypatch,
):
    ctx, _ = await _serialize([_row()], monkeypatch=monkeypatch)
    (hit,) = ctx.data["results"]
    assert isinstance(hit, SearchHitOut)
    dumped = hit.model_dump(mode="json")
    for column in EVIDENCE_COLUMNS:
        assert column not in dumped, column
    # Still readable in-process (the brief, recall logging).
    assert hit.verification_status == "tool_observed"

    # The same row as a pre-contract hit: MemoryOut without the six columns.
    pre_contract = MemoryOut(
        **{f: getattr(hit, f) for f in MemoryOut.model_fields}
    ).model_dump(mode="json", exclude=set(EVIDENCE_COLUMNS))
    compact = (",", ":")
    growth = len(json.dumps(dumped, separators=compact, default=str)) - len(
        json.dumps(pre_contract, separators=compact, default=str)
    )
    assert 0 < growth <= HIT_GROWTH_MAX_BYTES, growth


@pytest.mark.unit
async def test_diagnostic_widens_evidence_and_names_the_strategy(monkeypatch):
    ctx, _ = await _serialize(
        [_row()],
        diagnostic=True,
        strategy=RetrievalStrategy.SEMANTIC_SEARCH,
        monkeypatch=monkeypatch,
    )
    ev = ctx.data["results"][0].evidence.model_dump(mode="json")
    assert ev["strategy"] == "semantic_search"
    assert ev["tool_call_id"] == "call_abc123"


@pytest.mark.unit
async def test_injected_successor_says_why(monkeypatch):
    stale_id = uuid.uuid4()
    stale = _row(_memory(id=stale_id, status="outdated"))
    successor = vars(_memory(id=str(uuid.uuid4()), supersedes_id=str(stale_id))) | {
        "id": str(uuid.uuid4()),
        "supersedes_id": str(stale_id),
    }
    successor["metadata_"] = None
    ctx, _ = await _serialize([stale], successors=[successor], monkeypatch=monkeypatch)
    injected = [h for h in ctx.data["results"] if h.injected]
    assert len(injected) == 1
    assert injected[0].evidence.why == "newest version of a recalled fact"


@pytest.mark.unit
def test_entity_lookup_rows_say_entity_match():
    out = to_search_hit(
        MemoryOut.model_validate(
            {**vars(_memory()), "metadata": None, "entity_links": []},
            from_attributes=False,
        ),
        strategy="entity_lookup",
    )
    assert out.evidence is not None
    assert out.evidence.why == "entity match"


# ── EC-11: verified_only ────────────────────────────────────────────────────


@pytest.mark.unit
def test_verified_statuses_are_the_three_checked_ones():
    assert VERIFIED_STATUSES == ("tool_observed", "corroborated", "human_verified")


@pytest.mark.unit
def test_search_request_defaults_verified_only_off():
    assert SearchRequest(tenant_id="t", query="q").verified_only is False
    assert (
        SearchRequest(tenant_id="t", query="q", verified_only=True).verified_only
        is True
    )


@pytest.mark.unit
async def test_successor_lookup_carries_the_filter(monkeypatch):
    stale = _row(_memory(status="outdated"))
    sc = MagicMock()
    sc.find_successors = AsyncMock(return_value=[])
    monkeypatch.setattr(las, "get_storage_client", lambda: sc)
    ctx = PipelineContext(
        data={"filtered_rows": [stale], "tenant_id": "t", "verified_only": True}
    )
    await las.LoadAndSerialize().execute(ctx)
    assert sc.find_successors.await_args.args[0]["verified_only"] is True


def _record_statements(monkeypatch) -> list:
    statements: list = []

    @contextlib.asynccontextmanager
    async def _session():
        session = AsyncMock()

        async def _execute(stmt, *a, **k):
            statements.append(stmt)
            result = MagicMock()
            result.all.return_value = []
            return result

        session.execute.side_effect = _execute
        yield session

    monkeypatch.setattr(ps, "get_session", _session)
    monkeypatch.setattr(ps, "get_read_session", _session)
    return statements


def _sql(stmt) -> str:
    return str(
        stmt.compile(
            dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}
        )
    )


VERIFIED_SQL = "memories.verification_status IN ('tool_observed', 'corroborated', 'human_verified')"


@pytest.mark.unit
@pytest.mark.parametrize("verified_only", [False, True])
async def test_by_id_load_applies_the_filter_only_when_asked(
    monkeypatch, verified_only
):
    statements = _record_statements(monkeypatch)
    await ps.PostgresService().memory_load_by_ids(
        [uuid.uuid4()], "t", verified_only=verified_only
    )
    assert (VERIFIED_SQL in _sql(statements[0])) is verified_only


@pytest.mark.unit
@pytest.mark.parametrize("verified_only", [False, True])
async def test_successor_lookup_applies_the_filter_only_when_asked(
    monkeypatch, verified_only
):
    statements = _record_statements(monkeypatch)
    await ps.PostgresService().memory_find_successors(
        [uuid.uuid4()], "t", verified_only=verified_only
    )
    assert statements
    assert all((VERIFIED_SQL in _sql(s)) is verified_only for s in statements)


async def _insert(tenant_id: str, content: str, status: str) -> str:
    from core_api.clients.storage_client import get_storage_client

    mem = await get_storage_client().create_memory(
        {
            "tenant_id": tenant_id,
            "fleet_id": None,
            "agent_id": "test-agent",
            "memory_type": "fact",
            "content": content,
            "weight": 0.5,
            "embedding": fake_embedding(content),
            "content_hash": hashlib.sha256(
                f"{tenant_id}:{content}".encode()
            ).hexdigest(),
            "status": "active",
            "visibility": "scope_team",
        }
    )
    # Server-owned: no write sets it, so the test moves it the way the verify
    # operation and EC-13 will — directly on the row.
    async with ps.get_session() as session:
        await session.execute(
            update(Memory)
            .where(Memory.id == mem["id"])
            .values(verification_status=status)
        )
    return str(mem["id"])


@pytest.mark.integration
@pytest.mark.parametrize("use_pipeline", [True, False], ids=["pipeline", "legacy"])
async def test_verified_only_filters_without_reordering(
    tenant_id, use_pipeline, monkeypatch
):
    from core_api.services import memory_service

    monkeypatch.setattr(memory_service, "_USE_PIPELINE_SEARCH", use_pipeline)
    ids = {
        status: await _insert(
            tenant_id, f"deploy target note {status} caura staging", status
        )
        for status in ("unverified", "tool_observed", "human_verified", "disputed")
    }

    everything = await memory_service.search_memories(
        tenant_id, "deploy target caura staging", top_k=10
    )
    verified = await memory_service.search_memories(
        tenant_id, "deploy target caura staging", top_k=10, verified_only=True
    )

    all_ids = [str(h.id) for h in everything]
    kept = [str(h.id) for h in verified]
    assert set(all_ids) == set(ids.values())
    assert set(kept) == {ids["tool_observed"], ids["human_verified"]}
    # A filter, not a ranking input: the survivors keep their relative order.
    assert kept == [i for i in all_ids if i in kept]
    assert all(
        h.evidence is not None and h.evidence.verification in VERIFIED_STATUSES
        for h in verified
    )


@pytest.mark.unit
@pytest.mark.parametrize("verified_only", [False, True])
def test_explain_and_search_share_the_named_filter(verified_only):
    """EC-06 explains with the search's own filter list, so it names this one."""
    from sqlalchemy import false

    named = ps._scored_search_row_filters(
        tenant_id="t",
        readable_tenant_ids=None,
        fts_guard=false(),
        fleet_ids=None,
        strict_fleet_scoping=False,
        caller_agent_id=None,
        caller_tenant_id=None,
        filter_agent_id=None,
        memory_type_filter=None,
        status_filter=None,
        history_query=False,
        valid_at=None,
        verified_only=verified_only,
    )
    assert ("verified_only" in [name for name, _ in named]) is verified_only


@pytest.mark.unit
def test_explain_payload_and_reason_carry_verified_only():
    from core_api.pipeline.steps.search.execute_scored_search import (
        build_scored_search_payload,
    )
    from core_api.services.recall_explain import FILTER_REASONS

    data = {
        "tenant_id": "t",
        "query": "q",
        "verified_only": True,
        "embedding": None,
        "temporal_window": None,
        "boosted_memory_ids": set(),
        "memory_boost_factor": {},
    }
    payload = build_scored_search_payload(data, {"top_k": 5}, 5)
    assert payload["verified_only"] is True
    assert "verified_only" in FILTER_REASONS
