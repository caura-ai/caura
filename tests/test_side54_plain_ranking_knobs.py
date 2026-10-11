"""lme-0929-h-01 (SIDE-54) — per-request "plain hybrid ranking" on REST /search.

Every row ``/search`` returns gets ``recall_count + 1``, and ``recall_boost``
ranks it higher on the next query; the entity boost reorders on top of that. A
benchmark re-running the same query on the same store therefore gets a
different order each run, and the only way out used to be a tenant setting or
a search profile — store-wide, for a per-call need.

``SearchRequest.recall_boost`` / ``entity_boost`` are opt-out tri-states:

* ``None`` (omitted) — the tenant's behaviour, unchanged for every existing
  caller;
* ``false`` — that factor is neutralised for THIS call only. ``recall_boost``
  false also skips the ``recall_count`` bump, so a plain read does not mutate
  the state that ranks the next one;
* ``true`` — same as omitted: a request can subtract a factor, never add one
  back over a tenant that disabled it.

Three layers are pinned here: the pure fold (``_request_ranking_knobs``), the
wire (what reaches the storage ``scored_search`` payload), and the ranking
itself on a crafted fixture where both boosts would otherwise flip the order —
on both search implementations (pipeline and the deprecated legacy path).
"""

from __future__ import annotations

import hashlib
import math
import uuid
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from core_api.routes.memories import _request_ranking_knobs
from core_api.schemas import SearchRequest

# ---------------------------------------------------------------------------
# (a) the fold — defaults unchanged, false neutralises, true never widens
# ---------------------------------------------------------------------------


def _body(**kw) -> SearchRequest:
    return SearchRequest(tenant_id="t1", query="q", **kw)


def _config(*, recall_boost: bool = True, entity_retrieval: bool = True):
    return SimpleNamespace(recall_boost=recall_boost, entity_retrieval=entity_retrieval)


@pytest.mark.unit
def test_the_fields_default_to_none_and_are_declared():
    body = _body()
    assert body.recall_boost is None
    assert body.entity_boost is None
    # Declared fields, so they never surface as ax-0917-h-05
    # ``unrecognized_parameters`` warnings.
    sent = _body(recall_boost=False, entity_boost=False)
    assert not (sent.model_extra or {})


@pytest.mark.unit
@pytest.mark.parametrize("tenant_recall", [True, False])
@pytest.mark.parametrize("tenant_entity", [True, False])
@pytest.mark.parametrize("route_bump", [True, False])
def test_omitting_both_fields_passes_the_tenant_values_through(
    tenant_recall, tenant_entity, route_bump
):
    knobs = _request_ranking_knobs(
        _body(),
        _config(recall_boost=tenant_recall, entity_retrieval=tenant_entity),
        allow_recall_bump=route_bump,
    )
    assert knobs == {
        "recall_boost": tenant_recall,
        "entity_retrieval": tenant_entity,
        "allow_recall_bump": route_bump,
    }


@pytest.mark.unit
def test_false_neutralises_both_factors_and_skips_the_bump():
    knobs = _request_ranking_knobs(
        _body(recall_boost=False, entity_boost=False), _config(), allow_recall_bump=True
    )
    assert knobs == {
        "recall_boost": False,
        "entity_retrieval": False,
        "allow_recall_bump": False,
    }


@pytest.mark.unit
def test_each_flag_only_touches_its_own_factor():
    only_recall = _request_ranking_knobs(
        _body(recall_boost=False), _config(), allow_recall_bump=True
    )
    assert only_recall == {
        "recall_boost": False,
        "entity_retrieval": True,
        "allow_recall_bump": False,
    }
    only_entity = _request_ranking_knobs(
        _body(entity_boost=False), _config(), allow_recall_bump=True
    )
    # entity_boost=false is a ranking opt-out only: the read is still a use,
    # so it still reinforces (only recall_boost=false withholds the bump).
    assert only_entity == {
        "recall_boost": True,
        "entity_retrieval": False,
        "allow_recall_bump": True,
    }


@pytest.mark.unit
def test_true_cannot_re_enable_what_the_tenant_or_route_disabled():
    knobs = _request_ranking_knobs(
        _body(recall_boost=True, entity_boost=True),
        _config(recall_boost=False, entity_retrieval=False),
        allow_recall_bump=False,  # e.g. #1197 asserted identity
    )
    assert knobs == {
        "recall_boost": False,
        "entity_retrieval": False,
        "allow_recall_bump": False,
    }


# ---------------------------------------------------------------------------
# Fixtures for the DB-backed tests
# ---------------------------------------------------------------------------

_AGENT = "agent-side54"
_QUERY = "orbital launch window"
_DIM = 1024
# B's cosine to the query. Close enough to A (1.0) that a saturated
# recall_boost (<= RECALL_BOOST_CAP) or an entity boost flips the order, far
# enough that the plain hybrid score keeps A strictly first.
_B_COS = 0.99

_PIPE_EMBED = (
    "core_api.pipeline.steps.search.parallel_embed_entity_boost._get_or_cache_embedding"
)
_PIPE_ENTITY = "core_api.pipeline.steps.search.parallel_embed_entity_boost._entity_boost_via_storage"
_LEGACY_EMBED = "core_api.services.memory_service._get_or_cache_embedding"
_LEGACY_ENTITY = "core_api.services.memory_service._entity_boost_pipeline"


def _unit(i: int) -> list[float]:
    v = [0.0] * _DIM
    v[i] = 1.0
    return v


_Q_EMB = _unit(0)
_A_EMB = _unit(0)
_B_EMB = [0.0] * _DIM
_B_EMB[0] = _B_COS
_B_EMB[1] = math.sqrt(1.0 - _B_COS**2)


@pytest.fixture
def as_agent(monkeypatch):
    """Authenticated agent identity, so the default call DOES bump recall_count."""
    from core_api.app import app
    from core_api.auth import AuthContext, get_auth_context
    from core_api.tenant_context import set_current_tenant

    def _install(tenant_id: str):
        async def _dep():
            set_current_tenant(tenant_id)
            return AuthContext(
                tenant_id=tenant_id,
                agent_id=_AGENT,
                readable_tenant_ids=[tenant_id],
            )

        app.dependency_overrides[get_auth_context] = _dep

    yield _install
    app.dependency_overrides.pop(get_auth_context, None)


async def _seed(tenant_id: str, content: str, embedding: list[float], **extra) -> str:
    from core_api.clients.storage_client import get_storage_client

    payload = {
        "tenant_id": tenant_id,
        "fleet_id": None,
        "agent_id": _AGENT,
        "memory_type": "fact",
        "content": content,
        "weight": 0.5,
        "embedding": embedding,
        "content_hash": hashlib.sha256(f"{tenant_id}::{content}".encode()).hexdigest(),
        "status": "active",
        "recall_count": 0,
        "visibility": "scope_team",
        **extra,
    }
    mem = await get_storage_client().create_memory(payload)
    return str(mem["id"])


async def _seed_fixture(tenant_id: str) -> tuple[str, str]:
    """A = the plainly better match; B = slightly worse but heavily recalled."""
    a = await _seed(tenant_id, f"{_QUERY} alpha", _A_EMB)
    b = await _seed(
        tenant_id,
        f"{_QUERY} bravo",
        _B_EMB,
        recall_count=1_000_000,
        last_recalled_at=datetime.now(UTC).isoformat(),
    )
    return a, b


def _patches(use_pipeline: bool, boosted_id: str):
    """Pin the query vector and make the entity signal favour ``boosted_id``.

    The entity patch stands in for an entity-FTS hit: if the request skips
    entity retrieval, the patched function is never called and B gets no boost
    — which is exactly the behaviour under test.
    """
    boost = ({uuid.UUID(boosted_id)}, {uuid.UUID(boosted_id): 1.3})

    async def _embed(*_a, **_k):
        return list(_Q_EMB)

    async def _entity(*_a, **_k):
        return boost

    if use_pipeline:
        return patch(_PIPE_EMBED, _embed), patch(_PIPE_ENTITY, _entity)
    return patch(_LEGACY_EMBED, _embed), patch(_LEGACY_ENTITY, _entity)


async def _search(client, tenant_id: str, **body) -> dict:
    resp = await client.post(
        "/api/v1/search",
        json={"tenant_id": tenant_id, "query": _QUERY, "top_k": 5, **body},
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


async def _recall_count(memory_id: str) -> int:
    from sqlalchemy import text

    from core_storage_api.services.postgres_service import get_session

    async with get_session() as session:
        row = (
            await session.execute(
                text("SELECT recall_count FROM memories WHERE id = :id"),
                {"id": memory_id},
            )
        ).first()
    return int(row[0])


async def _settle_recall_bumps() -> None:
    """Await the fire-and-forget TrackRecalls bumps already dispatched.

    The pipeline bumps ``recall_count`` in a background task, so reading the
    counter right after a response races it. Waiting here makes "the plain
    call did not bump" a real assertion rather than a timing accident.
    """
    import asyncio

    from core_api.tasks import _background_tasks

    pending = [t for t in _background_tasks if not t.done()]
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)


_PATHS = pytest.mark.parametrize(
    "use_pipeline", [True, False], ids=["pipeline", "legacy"]
)


# ---------------------------------------------------------------------------
# (b) the ranking — both boosts off == plain hybrid score order
# ---------------------------------------------------------------------------


@pytest.mark.integration
@_PATHS
async def test_both_flags_false_rank_by_the_plain_hybrid_score(
    client, as_agent, tenant_id, monkeypatch, use_pipeline
):
    from core_api.services import memory_service

    monkeypatch.setattr(memory_service, "_USE_PIPELINE_SEARCH", use_pipeline)
    as_agent(tenant_id)
    a, b = await _seed_fixture(tenant_id)

    p_embed, p_entity = _patches(use_pipeline, boosted_id=b)
    with p_embed, p_entity:
        default = await _search(client, tenant_id)
        await _settle_recall_bumps()
        before = await _recall_count(b)
        plain = await _search(client, tenant_id, recall_boost=False, entity_boost=False)
        await _settle_recall_bumps()
        after = await _recall_count(b)

    default_ids = [m["id"] for m in default["items"]]
    plain_ids = [m["id"] for m in plain["items"]]

    # The fixture is only meaningful if the boosts really do reorder it.
    assert default_ids[:2] == [b, a], (
        "fixture broken: recall_boost + entity boost should lift B over A by default"
    )
    # Plain: A first, and the order is exactly the unboosted relevance order.
    assert plain_ids[:2] == [a, b]
    sims = [m["similarity"] for m in plain["items"]]
    assert sims == sorted(sims, reverse=True)

    # The default call reinforced; the plain call did not.
    assert default["recall_tracked"] is True
    assert plain["recall_tracked"] is False
    assert before == 1_000_001  # the default call's bump landed
    assert after == before


@pytest.mark.integration
@_PATHS
async def test_default_request_is_unchanged_and_still_bumps(
    client, as_agent, tenant_id, monkeypatch, use_pipeline
):
    """Omitting the fields (and sending true) keeps today's ranking + bump."""
    from core_api.services import memory_service

    monkeypatch.setattr(memory_service, "_USE_PIPELINE_SEARCH", use_pipeline)
    as_agent(tenant_id)
    a, b = await _seed_fixture(tenant_id)

    p_embed, p_entity = _patches(use_pipeline, boosted_id=b)
    with p_embed, p_entity:
        omitted = await _search(client, tenant_id)
        explicit_true = await _search(
            client, tenant_id, recall_boost=True, entity_boost=True
        )

    for resp in (omitted, explicit_true):
        assert [m["id"] for m in resp["items"]][:2] == [b, a]
        assert resp["recall_tracked"] is True
        assert not resp.get("warnings")


# ---------------------------------------------------------------------------
# (c) the wire — the request field reaches the storage scoring payload
# ---------------------------------------------------------------------------


@pytest.mark.integration
@_PATHS
@pytest.mark.parametrize(
    ("body", "expect_recall_enabled", "expect_entity_factor"),
    [
        ({}, True, True),
        ({"recall_boost": False}, False, True),
        ({"entity_boost": False}, True, False),
        ({"recall_boost": False, "entity_boost": False}, False, False),
    ],
    ids=["default", "recall-off", "entity-off", "both-off"],
)
async def test_the_request_fields_reach_the_storage_scored_search_payload(
    client,
    as_agent,
    tenant_id,
    monkeypatch,
    use_pipeline,
    body,
    expect_recall_enabled,
    expect_entity_factor,
):
    from core_api.clients.storage_client import CoreStorageClient
    from core_api.services import memory_service

    monkeypatch.setattr(memory_service, "_USE_PIPELINE_SEARCH", use_pipeline)
    as_agent(tenant_id)
    _a, b = await _seed_fixture(tenant_id)

    payloads: list[dict] = []
    real = CoreStorageClient.scored_search

    async def _spy(self, data, *args, **kwargs):
        payloads.append(data)
        return await real(self, data, *args, **kwargs)

    monkeypatch.setattr(CoreStorageClient, "scored_search", _spy)

    p_embed, p_entity = _patches(use_pipeline, boosted_id=b)
    with p_embed, p_entity:
        await _search(client, tenant_id, **body)

    assert len(payloads) == 1
    sent = payloads[0]
    assert sent["recall_boost_enabled"] is expect_recall_enabled
    if expect_entity_factor:
        assert sent.get("memory_boost_factor") == {b: 1.3}
    else:
        assert not sent.get("memory_boost_factor")
        assert not sent.get("boosted_memory_ids")
    # The scoring knobs themselves are untouched: the opt-out gates the factor,
    # it does not rewrite the tenant's search_params.
    assert "recall_boost_cap" in sent["search_params"]
