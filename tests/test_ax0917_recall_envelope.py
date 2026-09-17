"""ax-0917-h-03 / h-04 / h-05 — the recall response envelope and its params.

Three findings against one surface, all of them byte counts:

h-03  ``/recall`` serialised the whole result set TWICE, under ``memories``
      and its C4 ``items`` alias. The Python list is shared in memory, so the
      "materialise once" comment was true and irrelevant: JSON writes it out
      both times. Measured here at ~50% of the payload.

h-04  every row carried the write-time telemetry block THREE times —
      ``metadata.<key>`` (C25's legacy mirror), ``metadata._system`` (the
      namespace it is mirrored into) and ``system_metadata`` (the derived read
      view). ~1.9 KB per row against 60-82 B of actual content.

h-05  ``limit`` was dropped in silence on a body whose real parameter is
      ``top_k``, so an agent asking for 2 rows got the default 5 — and
      ``bogus_param_xyz`` behaved identically, which is what proved the
      mechanism was "unknown keys vanish", not anything about ``limit``.

The fixes and why they point the way they do are argued at their sites:
``recall_service._row_keys`` / ``_dump_rows``, ``system_metadata.strip_platform_metadata``,
``schemas.SearchRequest.top_k`` and ``routes.memories._unknown_param_warnings``.
Back-compat for the default REST shape stays pinned in
``tests/test_c4_recall_items_alias.py``.

Pure logic + route-level; no LLM, no embedding provider.
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from core_api.schemas import MemoryOut, RecallRequest, SearchRequest
from core_api.services.recall_service import summarize_memories
from core_api.services.system_metadata import (
    extract_system_metadata,
    strip_platform_metadata,
)
from tests.conftest import get_test_auth

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Fixtures — a row shaped like one the write pipeline actually leaves behind
# ---------------------------------------------------------------------------

# Exactly what ``set_system_value`` produces: every platform value written to
# BOTH the legacy top level and ``_system``. Invented telemetry would make the
# byte assertions meaningless, so this mirrors the dual-write rather than
# guessing at it.
_TELEMETRY = {
    "llm_ms": 1843,
    "write_latency_ms": 2104,
    "semantic_dedup_ms": 37,
    "weight_source": "llm",
    "write_mode": "fast",
    "enrichment_pending": False,
    "embedding_pending": False,
    "contains_pii": False,
    "retrieval_hint": "fleet operations preference",
}
# The caller's own bag, which must survive the projection untouched.
_CALLER_META = {"source": "openclaw-plugin", "ticket": "CAURA-999"}


def _stored_metadata() -> dict:
    md: dict = {"_system": dict(_TELEMETRY), **_TELEMETRY, **_CALLER_META}
    return md


def _memory_out(
    content: str = "Ran prefers the CLI over the dashboard for fleet ops.",
) -> MemoryOut:
    metadata = _stored_metadata()
    return MemoryOut(
        id=uuid.uuid4(),
        tenant_id="tenant-A",
        agent_id="agent-A",
        memory_type="fact",
        title="Fleet operations note",
        content=content,
        weight=0.62,
        source_uri=None,
        run_id=None,
        metadata=metadata,
        system_metadata=extract_system_metadata(metadata),
        created_at=datetime.now(UTC),
        expires_at=None,
    )


def _minimal_config(recall_enabled: bool = False) -> SimpleNamespace:
    """Cheapest config ``summarize_memories`` accepts. ``recall_enabled=False``
    takes the no-LLM branch, which serialises rows identically to the brief
    branch — the envelope is what is under test, not the summary."""
    return SimpleNamespace(
        recall_enabled=recall_enabled,
        recall_provider="fake",
        recall_model="fake-model",
        recall_boost=False,
        graph_expand=False,
        entity_retrieval=True,
    )


def _wire_bytes(obj) -> int:
    return len(json.dumps(obj, default=str).encode("utf-8"))


# ---------------------------------------------------------------------------
# h-03 — the result set is no longer serialised twice unless asked
# ---------------------------------------------------------------------------


async def test_h03_opting_out_of_the_alias_drops_the_duplicate_list():
    """FAILS PRE-FIX: ``summarize_memories`` had no ``items_alias`` parameter
    and emitted both keys unconditionally."""
    rows = [_memory_out() for _ in range(5)]
    lean = await summarize_memories(rows, "q", _minimal_config(), items_alias=False)

    assert "items" not in lean, "the alias must be absent when the caller opts out"
    assert len(lean["memories"]) == 5
    assert lean["memory_count"] == 5


async def test_h03_opting_out_halves_the_payload():
    """The whole finding is a byte count, so the byte count is the assertion.

    Compared against the SAME rows with the alias on, so this measures the
    duplication and nothing else."""
    rows = [_memory_out() for _ in range(5)]
    fat = await summarize_memories(rows, "q", _minimal_config(), items_alias=True)
    lean = await summarize_memories(rows, "q", _minimal_config(), items_alias=False)

    saved = 1 - _wire_bytes(lean) / _wire_bytes(fat)
    assert 0.45 < saved < 0.55, (
        f"expected ~50% of the payload to be the duplicate, got {saved:.1%}"
    )


@pytest.mark.parametrize("recall_enabled", [True, False])
async def test_h03_opt_out_applies_to_the_empty_branch_too(recall_enabled, monkeypatch):
    """A consumer's ``"items" in body`` check must not flip on an empty result
    set — the three branches have to agree about the shape."""
    import core_api.services.recall_service as rs_mod

    monkeypatch.setattr(
        rs_mod, "call_with_fallback", AsyncMock(return_value="brief"), raising=False
    )
    empty = await summarize_memories(
        [], "q", _minimal_config(recall_enabled=recall_enabled), items_alias=False
    )
    assert "items" not in empty
    assert empty["memories"] == []


async def test_h03_rest_default_still_carries_the_alias(client, monkeypatch):
    """Back-compat pin, restated here next to the opt-out it guards.

    ``RecallResponse.items`` is published OpenAPI and
    ``docs/public-api-stability.md`` makes REST response shapes part of the
    SemVer contract, so the default must NOT change. A caller that sends
    ``items_alias: false`` is the one that gets the lean envelope."""
    monkeypatch.setattr(
        "core_api.services.memory_service.search_memories", AsyncMock(return_value=[])
    )
    tenant_id, headers = get_test_auth()

    default = await client.post(
        "/api/v1/recall",
        json={"tenant_id": tenant_id, "query": "fleet ops", "top_k": 5},
        headers=headers,
    )
    assert default.status_code == 200, default.text
    assert "items" in default.json()

    opted_out = await client.post(
        "/api/v1/recall",
        json={
            "tenant_id": tenant_id,
            "query": "fleet ops",
            "top_k": 5,
            "items_alias": False,
        },
        headers=headers,
    )
    assert opted_out.status_code == 200, opted_out.text
    assert "items" not in opted_out.json()
    assert "memories" in opted_out.json()


# ---------------------------------------------------------------------------
# h-04 — one metadata location on agent-facing reads
# ---------------------------------------------------------------------------


def test_h04_strip_keeps_caller_keys_and_drops_platform_ones():
    """FAILS PRE-FIX: ``strip_platform_metadata`` did not exist."""
    stripped = strip_platform_metadata(_stored_metadata())

    assert stripped == _CALLER_META, "the caller's own metadata must survive verbatim"
    assert "_system" not in stripped
    for key in _TELEMETRY:
        assert key not in stripped, f"platform key {key!r} still duplicated in metadata"


def test_h04_strip_preserves_the_none_vs_empty_distinction():
    """``null`` and ``{}`` are different answers on the wire — the falsy-``{}``
    trap ``_dict_to_memory_out`` documents. A row that HAS a metadata column
    must not report it absent just because every key in it was platform-written.
    """
    assert strip_platform_metadata(None) is None
    assert strip_platform_metadata({}) == {}
    assert strip_platform_metadata(dict(_TELEMETRY)) == {}


async def test_h04_recall_rows_carry_telemetry_exactly_once():
    """FAILS PRE-FIX: the same ``llm_ms`` appeared in all three locations."""
    resp = await summarize_memories([_memory_out()], "q", _minimal_config())
    row = resp["memories"][0]

    assert "_system" not in (row["metadata"] or {})
    assert "llm_ms" not in (row["metadata"] or {})
    # ...and the telemetry is still IN the response, at the documented C25
    # location. This is a projection, not a deletion: an internal consumer that
    # reads write-time telemetry off a recall row keeps working by reading the
    # key C25 named for it.
    assert row["system_metadata"]["llm_ms"] == 1843
    assert row["system_metadata"]["weight_source"] == "llm"
    assert row["system_metadata"]["embedding_pending"] is False
    # The caller's own bag is untouched.
    assert row["metadata"]["source"] == "openclaw-plugin"


async def test_h04_one_location_is_a_third_of_the_metadata_bytes():
    """87% overhead on short memories is the finding; this pins the direction
    and the order of magnitude without hard-coding a build-specific number."""
    row = _memory_out()
    dumped = row.model_dump(mode="json")
    md = dumped["metadata"]
    triplicated = (
        _wire_bytes({k: v for k, v in md.items() if k != "_system"})
        + _wire_bytes(md["_system"])
        + _wire_bytes(dumped["system_metadata"])
    )

    resp = await summarize_memories([row], "q", _minimal_config())
    projected = resp["memories"][0]
    single = _wire_bytes(projected["metadata"]) + _wire_bytes(
        projected["system_metadata"]
    )

    assert single < triplicated / 2, (
        f"metadata still duplicated: {single} B projected vs {triplicated} B stored"
    )


# ---------------------------------------------------------------------------
# h-05 — `limit` is honoured, and unknown params stop being silent
# ---------------------------------------------------------------------------


def test_h05_limit_is_honoured_as_top_k():
    """FAILS PRE-FIX: ``limit`` was not a declared alias, so ``extra="ignore"``
    dropped it and ``top_k`` fell back to ``DEFAULT_SEARCH_TOP_K`` (5)."""
    assert RecallRequest(tenant_id="t", query="q", limit=2).top_k == 2
    # /search shares this body and had the identical trap.
    assert SearchRequest(tenant_id="t", query="q", limit=2).top_k == 2


def test_h05_explicit_top_k_wins_over_limit_and_the_loser_is_reported():
    """``top_k`` is first in ``AliasChoices``, so it wins — and because the
    losing spelling then arrives as an extra, the caller is TOLD which of the
    two numbers the endpoint used, instead of guessing."""
    body = RecallRequest(tenant_id="t", query="q", top_k=3, limit=99)
    assert body.top_k == 3
    assert body.model_extra == {"limit": 99}


def test_h05_unknown_params_are_captured_not_discarded():
    """FAILS PRE-FIX: the model inherited ``extra="ignore"``, so ``model_extra``
    was empty and no layer could tell the caller anything."""
    body = RecallRequest(tenant_id="t", query="q", bogus_param_xyz=2)
    assert body.model_extra == {"bogus_param_xyz": 2}


async def test_h05_recall_warns_about_parameters_it_does_not_read(client, monkeypatch):
    """The response-level channel, which is the only one an autonomous agent
    can act on — it does not read our logs.

    FAILS PRE-FIX: the response carried no ``warnings`` key at all."""
    monkeypatch.setattr(
        "core_api.services.memory_service.search_memories", AsyncMock(return_value=[])
    )
    tenant_id, headers = get_test_auth()
    resp = await client.post(
        "/api/v1/recall",
        json={"tenant_id": tenant_id, "query": "q", "bogus_param_xyz": 2},
        headers=headers,
    )
    assert resp.status_code == 200, resp.text
    warnings = resp.json()["warnings"]
    assert warnings[0]["code"] == "unrecognized_parameters"
    assert warnings[0]["details"]["unknown_parameters"] == ["bogus_param_xyz"]
    assert "top_k" in warnings[0]["message"]


async def test_h05_a_clean_recall_carries_no_warnings_key(client, monkeypatch):
    """The notice costs nothing when there is nothing to say — this PR is about
    bytes, so an always-present ``"warnings": null`` would be self-defeating."""
    monkeypatch.setattr(
        "core_api.services.memory_service.search_memories", AsyncMock(return_value=[])
    )
    tenant_id, headers = get_test_auth()
    resp = await client.post(
        "/api/v1/recall",
        json={"tenant_id": tenant_id, "query": "q", "top_k": 2},
        headers=headers,
    )
    assert resp.status_code == 200, resp.text
    assert "warnings" not in resp.json()


async def test_h05_unknown_params_are_still_accepted_not_rejected(client, monkeypatch):
    """The SAFE-01 asymmetry stands: search-shaped bodies accept unknown fields.

    Making them strict would be the honest fix and is a breaking change for any
    integrator already sending junk — so this pins that the PR did NOT do it,
    the same way ``tests/test_unknown_field_rejection.py`` pins it for /search.
    """
    monkeypatch.setattr(
        "core_api.services.memory_service.search_memories", AsyncMock(return_value=[])
    )
    tenant_id, headers = get_test_auth()
    resp = await client.post(
        "/api/v1/recall",
        json={"tenant_id": tenant_id, "query": "q", "not_a_real_param": True},
        headers=headers,
    )
    assert resp.status_code == 200, resp.text


async def test_h05_search_reports_unknown_params_in_its_existing_warnings_list(
    client, monkeypatch
):
    """/search shares the body and had the same silence; the A28 ``warnings``
    field it already publishes is where the notice belongs."""
    # /search binds the name at import time (``routes.memories`` line 102),
    # unlike /recall which imports it inside the handler.
    monkeypatch.setattr(
        "core_api.routes.memories.search_memories", AsyncMock(return_value=[])
    )
    tenant_id, headers = get_test_auth()
    resp = await client.post(
        "/api/v1/search",
        json={"tenant_id": tenant_id, "query": "q", "bogus_param_xyz": 2},
        headers=headers,
    )
    assert resp.status_code == 200, resp.text
    codes = [w["code"] for w in (resp.json()["warnings"] or [])]
    assert "unrecognized_parameters" in codes


# ---------------------------------------------------------------------------
# Combined — what the MCP brief stopped shipping
# ---------------------------------------------------------------------------


async def test_mcp_brief_no_longer_ships_a_fourth_copy(mcp_env, monkeypatch):
    """``caura_recall(include_brief=True)`` carried the identical rows FOUR
    times: ``results`` + ``items`` on the payload (C31/D1's permanent alias)
    and ``memories`` + ``items`` inside the brief.

    FAILS PRE-FIX: ``brief["items"]`` was present."""
    from tests._mcp_test_helpers import parse_envelope

    mcp_env["service"]("search_memories").return_value = []
    monkeypatch.setattr(
        "core_api.services.organization_settings.resolve_config",
        AsyncMock(return_value=_minimal_config(recall_enabled=True)),
    )
    monkeypatch.setattr(
        "core_api.clients.storage_client.CoreStorageClient.get_agent",
        AsyncMock(return_value=None),
    )

    from core_api import mcp_server

    payload = parse_envelope(
        await mcp_server.caura_recall(query="q", include_brief=True)
    )
    assert "items" not in payload["brief"]
    assert "memories" in payload["brief"]
    # The payload's own C31/D1 dual-emit is a separate, documented contract —
    # untouched, and deliberately still pinned here so a later sweep that
    # removes it has to argue that case on its own.
    assert "results" in payload and "items" in payload
