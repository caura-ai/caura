"""Regression tests for write/query embedding-surface symmetry (CAURA-222).

Pre-CAURA-222, the write pipeline embedded
``compose_embedding_text(content, retrieval_hint)`` —
``"[Retrieval hint]: <hint>\\n\\n<content>"`` — while the search path
embedded raw query text. Identical content↔query produced cosine ~0.69
instead of ~1.0, capping recall across dedup, entity-lookup, and search
ranking (algo-stress run 20260507T153129Z-76c172).

The fix aligned both sides on raw text. These tests are the load-
bearing assertion that prevents the asymmetry from coming back: any
future change that prepends, appends, or otherwise transforms the text
on one side without doing the same on the other will trip these tests.

Three surfaces are pinned:

1. Hot-path write (``ParallelEmbedEnrich``) — embed input must equal
   the raw memory content even when enrichment yields a retrieval_hint.
2. Atomic-fact fan-out (``_enrich_memory_background``) — child embed
   input must equal the raw ``fact.content``.
3. End-to-end symmetry — for byte-identical content/query, the write
   and search paths feed the embedding model the same string, so a
   deterministic embedder produces the same vector (cosine == 1.0).
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from core_api.constants import VECTOR_DIM
from core_api.pipeline.context import PipelineContext
from core_api.pipeline.steps.write.parallel_embed_enrich import ParallelEmbedEnrich
from core_api.schemas import MemoryCreate
from tests._contradiction_batch_compat import install_batch_status_replay_shim

pytestmark = pytest.mark.asyncio


TENANT_ID = f"test-embed-stability-{uuid.uuid4().hex[:8]}"
CONTENT = "Algo-stress embedding-stability probe — fixed string."


def _input(content: str = CONTENT) -> MemoryCreate:
    return MemoryCreate(
        tenant_id=TENANT_ID,
        fleet_id="f",
        agent_id="a",
        content=content,
        persist=True,
        entity_links=[],
    )


def _ctx(*, enrichment: bool = True) -> PipelineContext:
    tenant_config = SimpleNamespace(
        enrichment_enabled=enrichment,
        enrichment_provider="fake" if enrichment else "none",
    )
    return PipelineContext(
        db=AsyncMock(),
        data={"input": _input(), "content_hash": "f" * 64},
        tenant_config=tenant_config,
    )


# ---------------------------------------------------------------------------
# 1. Hot-path write embeds raw content even when enrichment yields a hint
# ---------------------------------------------------------------------------


async def test_hot_path_embeds_raw_content_when_enrichment_yields_hint() -> None:
    """The write pipeline must NOT prefix or otherwise transform the text
    fed to the embedding model, regardless of what enrichment returns.

    A pre-CAURA-222 regression would re-embed
    ``"[Retrieval hint]: <hint>\\n\\n<content>"`` here and store the
    hint-prefixed vector. Search-side embeds raw query text, so the
    surfaces would diverge → cosine collapse → recall ceiling.
    """
    embed_inputs: list[str] = []

    async def _spy_embed(text: str, *_a, **_k):
        embed_inputs.append(text)
        return [0.1] * VECTOR_DIM

    enrichment_result = SimpleNamespace(
        retrieval_hint="business milestone: signed first client",
    )

    async def _enrich(*_a, **_k):
        return enrichment_result

    with (
        patch(
            "core_api.pipeline.steps.write.parallel_embed_enrich.settings.deployment_mode",
            "inline",
        ),
        patch(
            "core_api.pipeline.steps.write.parallel_embed_enrich.get_embedding",
            new=_spy_embed,
        ),
        patch("core_api.services.memory_enrichment.enrich_memory", new=_enrich),
    ):
        await ParallelEmbedEnrich().execute(_ctx(enrichment=True))

    # Exactly one embed call, with raw content. Any prefix marker
    # (e.g. "[Retrieval hint]:") or the hint string itself appearing in
    # the embed input is the asymmetry returning.
    assert len(embed_inputs) == 1, (
        f"expected one embed call, got {len(embed_inputs)} — a second call "
        f"likely indicates a hint re-embed has been re-introduced"
    )
    assert embed_inputs[0] == CONTENT
    assert "[Retrieval hint]" not in embed_inputs[0]
    assert "business milestone" not in embed_inputs[0]


# ---------------------------------------------------------------------------
# 2. Atomic-fact children embed raw fact_content
# ---------------------------------------------------------------------------


async def test_atomic_fact_children_embed_raw_fact_content() -> None:
    """Atomic-fact fan-out children must embed raw ``fact.content``, not
    a hint-prefixed composition. Same rationale as the hot path: the
    search side embeds raw query text, so children that get persisted
    on a different surface lose recall on queries targeting their
    specific claim.
    """
    from core_api.services import memory_service

    embed_inputs: list[str] = []

    async def _spy_embed(text: str, *_a, **_k):
        embed_inputs.append(text)
        return [0.2] * VECTOR_DIM

    fact_a = SimpleNamespace(
        content="Anniversary is July 22.",
        retrieval_hint="anniversary date",
        suggested_type="fact",
    )
    fact_b = SimpleNamespace(
        content="Kitchen faucet started leaking.",
        retrieval_hint="home maintenance issue",
        suggested_type="episode",
    )
    enrichment = SimpleNamespace(
        memory_type="fact",
        weight=None,
        title=None,
        summary=None,
        tags=None,
        llm_ms=None,
        contains_pii=False,
        pii_types=None,
        retrieval_hint="",
        ts_valid_start=None,
        ts_valid_end=None,
        status=None,
        atomic_facts=[fact_a, fact_b],
    )

    mem_row = {
        "id": "m1",
        "deleted_at": None,
        "fleet_id": "f1",
        "embedding": [0.0] * VECTOR_DIM,
        "memory_type": "fact",
        "weight": 0.5,
        "status": "active",
        "ts_valid_start": None,
        "ts_valid_end": None,
        "metadata_": {},
        "visibility": "scope_team",
    }
    sc = MagicMock()
    sc.get_memory = AsyncMock(return_value=mem_row)
    sc.update_embedding = AsyncMock()
    sc.update_memory_status = AsyncMock()
    install_batch_status_replay_shim(sc)
    sc.create_memory = AsyncMock()
    sc.update_memory = AsyncMock()

    def _stub_tracked_task(coro, _name, *_a, **_k):
        coro.close()
        return None

    with (
        patch.object(memory_service.settings, "deployment_mode", "inline"),
        patch.object(memory_service, "get_embedding", new=_spy_embed),
        patch.object(memory_service, "get_storage_client", return_value=sc),
        patch(
            "core_api.services.memory_enrichment.enrich_memory",
            new=AsyncMock(return_value=enrichment),
        ),
        patch.object(memory_service, "track_task"),
        patch(
            "core_api.services.task_tracker.tracked_task",
            new=MagicMock(side_effect=_stub_tracked_task),
        ),
        patch(
            "core_api.services.organization_settings.resolve_config",
            new=AsyncMock(
                return_value=SimpleNamespace(
                    enrichment_enabled=True,
                    enrichment_provider="fake",
                    entity_extraction_enabled=False,
                )
            ),
        ),
    ):
        await memory_service._enrich_memory_background(
            uuid.uuid4(), "parent content", TENANT_ID, "f1", "a"
        )

    # One embed call per fact, each with the fact's raw content.
    assert embed_inputs == [fact_a.content, fact_b.content], (
        f"atomic-fact embed inputs {embed_inputs!r} — expected raw "
        f"fact.content with no hint prefix"
    )
    for inp in embed_inputs:
        assert "[Retrieval hint]" not in inp
        assert "anniversary date" not in inp
        assert "home maintenance issue" not in inp


# ---------------------------------------------------------------------------
# 3. End-to-end symmetry: write surface == query surface for identical text
# ---------------------------------------------------------------------------


async def test_write_and_query_embed_identical_input_for_identical_text() -> None:
    """The load-bearing CAURA-222 assertion: for byte-identical content
    and query, the embed inputs on the write and search paths must be
    equal. With a deterministic embedder, equal inputs → equal vectors
    → cosine == 1.0.

    A new write transformation (e.g. a re-introduced hint prefix, a new
    document-side instruction prefix, normalization) without a matching
    query-side change will diverge the two recorded inputs and trip
    this test.
    """
    from core_api.services import memory_service

    write_embed_inputs: list[str] = []
    query_embed_inputs: list[str] = []

    async def _spy_write_embed(text: str, *_a, **_k):
        write_embed_inputs.append(text)
        return [0.3] * VECTOR_DIM

    async def _spy_query_embed(text: str, *_a, **_k):
        query_embed_inputs.append(text)
        return [0.3] * VECTOR_DIM

    enrichment_result = SimpleNamespace(
        retrieval_hint="business milestone: signed first client",
    )

    async def _enrich(*_a, **_k):
        return enrichment_result

    # Bypass the redis-backed query-embedding cache so the spy fires.
    async def _cache_miss(*_a, **_k):
        return None

    async def _cache_set(*_a, **_k):
        return None

    with (
        patch(
            "core_api.pipeline.steps.write.parallel_embed_enrich.settings.deployment_mode",
            "inline",
        ),
        patch(
            "core_api.pipeline.steps.write.parallel_embed_enrich.get_embedding",
            new=_spy_write_embed,
        ),
        patch("core_api.services.memory_enrichment.enrich_memory", new=_enrich),
        patch.object(memory_service, "get_query_embedding", new=_spy_query_embed),
        patch("core_api.cache.cache_get", new=_cache_miss),
        patch("core_api.cache.cache_set", new=_cache_set),
    ):
        # Write side
        await ParallelEmbedEnrich().execute(_ctx(enrichment=True))
        # Search side — same text
        await memory_service._get_or_cache_embedding(
            CONTENT,
            TENANT_ID,
            tenant_config=None,
        )

    assert len(write_embed_inputs) == 1
    assert len(query_embed_inputs) == 1
    assert write_embed_inputs[0] == query_embed_inputs[0], (
        f"write embed input {write_embed_inputs[0]!r} != "
        f"query embed input {query_embed_inputs[0]!r} — surfaces have "
        f"diverged; CAURA-222 asymmetry is back"
    )
    # And both equal the raw input — no transformation on either side.
    assert write_embed_inputs[0] == CONTENT
