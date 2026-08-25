"""C31 — MCP/REST parameter parity, per the ratified wire contract (D1/D2).

D2: short names are canonical (`memory_type`, `status`, `top_k`); the REST
`*_filter` forms are permanent accepted aliases. Before this, `memory_type=
fact` (the MCP spelling) was silently DROPPED by REST's extra="ignore"
contract — MemoryImpact's "C1+C2 together are a trap".

D1: `items` is the canonical list key; MCP recall dual-emits `results` +
`items` + `count`.

The parity test is the CI enforcement the contract calls for: every shared
search concept must be accepted by BOTH surfaces under the canonical short
spelling.
"""

import inspect

import pytest

from core_api.schemas import SearchRequest

pytestmark = pytest.mark.unit


# --- D2: REST accepts the canonical short spellings ---------------------------


def _base(**kw):
    return SearchRequest(tenant_id="t", query="q", **kw)


def test_rest_accepts_short_memory_type():
    assert _base(memory_type="fact").memory_type_filter == "fact"


def test_rest_accepts_short_status():
    assert _base(status="confirmed").status_filter == "confirmed"


def test_rest_long_forms_still_work():
    r = _base(memory_type_filter="fact", status_filter="confirmed")
    assert r.memory_type_filter == "fact"
    assert r.status_filter == "confirmed"


def test_conflict_long_form_wins():
    # When both spellings arrive, the long form wins (first in AliasChoices) —
    # deterministic, documented behavior rather than silent last-write.
    r = SearchRequest.model_validate(
        {"tenant_id": "t", "query": "q", "memory_type_filter": "fact", "memory_type": "episode"}
    )
    assert r.memory_type_filter == "fact"


def test_short_spelling_actually_filters_not_ignored():
    """The C1+C2 trap regression guard: the short spelling must land in the
    model, not vanish into extra='ignore'."""
    r = SearchRequest.model_validate({"tenant_id": "t", "query": "q", "memory_type": "fact"})
    assert r.memory_type_filter == "fact"


# --- D2: parity — every shared concept accepted on both surfaces ---------------


# Shared search concepts and their canonical (short) spelling. REST acceptance
# is proven by model validation above/below; MCP acceptance by signature.
SHARED_CANONICAL = ["memory_type", "status", "top_k", "fleet_ids", "filter_agent_id",
                    "valid_at", "min_similarity", "diagnostic"]


def test_mcp_recall_exposes_all_canonical_names():
    from core_api import mcp_server

    sig = inspect.signature(mcp_server.caura_recall)
    for name in SHARED_CANONICAL:
        assert name in sig.parameters, f"caura_recall missing canonical param {name!r}"


def test_rest_accepts_all_canonical_names():
    payload = {
        "tenant_id": "t",
        "query": "q",
        "memory_type": "fact",
        "status": "confirmed",
        "top_k": 3,
        "fleet_ids": ["f1"],
        "filter_agent_id": "a1",
        "valid_at": "2026-08-25T00:00:00Z",
        "min_similarity": 0.4,
        "diagnostic": True,
    }
    r = SearchRequest.model_validate(payload)
    assert r.memory_type_filter == "fact"
    assert r.status_filter == "confirmed"
    assert r.top_k == 3
    assert r.min_similarity == 0.4
    assert r.diagnostic is True


# --- D1: MCP recall payload dual-emits items + count ---------------------------


def test_mcp_recall_source_dual_emits_items():
    """Grep-guard on the payload build: results + items + count must all be
    emitted (dual-emit per D1; removal is a future announced wave)."""
    import pathlib

    src = (
        pathlib.Path(__file__).resolve().parents[1] / "core-api/src/core_api/mcp_server.py"
    ).read_text()
    recall_section = src.split("async def caura_recall(")[1].split("async def caura_")[0]
    assert '"results": _rows' in recall_section
    assert '"items": _rows' in recall_section
    assert '"count": len(_rows)' in recall_section
