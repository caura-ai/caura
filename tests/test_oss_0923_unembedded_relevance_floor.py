"""oss-0923 — the relevance floor's un-embedded branch, and what makes it safe.

``passes_relevance_filter`` returns True unconditionally when ``has_embedding``
is False. Read on its own that is an unbounded hole in the similarity floor, and
it has now been independently reported as one twice. It is not a hole, but the
reason is not in this file's neighbourhood and nothing pinned it:

  THE CORE-API ``True`` IS LOAD-BEARING, NOT PERMISSIVE. The floor compares
  ``min_similarity`` against ``vec_sim``, and storage represents a missing cosine
  as the sentinel ``0.0`` (``postgres_service``: ``case((embedding IS NOT NULL,
  1 - cosine_distance), else_=0.0)``). Applying the floor to an un-embedded row
  would therefore drop *every* such row at any positive floor — reverting
  CAURA-594, CAURA-679 and #687 in one line. The branch is the only thing that
  keeps a row whose vector has not landed discoverable at all.

  WHAT BOUNDS IT IS IN THE OTHER SERVICE. ``search_memories_scored`` carries the
  CAURA-594 admission guard in ``row_filters`` — ``or_(Memory.embedding.is_not(
  None), _fts_guard)`` — applied to the ingredients CTE *and* to every ANN pool
  arm. An un-embedded row that does not match the full-text query is never
  returned to core-api at all, so the core-api branch never sees one. That
  precondition is what makes the unconditional True correct, and it lives in a
  separately deployed package that cannot import this one.

So the two cases below pin the two halves. The first states the core-api
contract including the asymmetry it has never admitted to; the second is the
one that fails if anyone relaxes the storage guard, which is the only change
that would turn the branch into the hole it is repeatedly mistaken for.

HARNESS FACT, same as ``test_pm_c02_search_content_is_never_blank``: the test
database is built by ``Base.metadata.create_all``, so migration 001's
``search_vector`` trigger does not exist and the column is NULL on every row.
Full-text match is dead suite-wide — and since it is the ONLY admission route an
un-embedded row has, none of this is otherwise reachable by a test at all
(tracked as oss-0923-m-02). ``_index_for_fts`` rebuilds the vector for one
tenant using migration 034's own expression.
"""

from __future__ import annotations

import uuid

import pytest
import sqlalchemy as sa

from core_api.config import settings
from core_api.search_trim import passes_relevance_filter
from core_api.services import memory_service
from core_storage_api.services.postgres_service import get_session
from tests.conftest import close_scheduled_coro, get_test_auth

# Migration 034's expression, verbatim — see the note in the c-02 file.
_SEARCH_VECTOR_SQL = (
    "to_tsvector('english', coalesce(title, '') || ' ' || coalesce(content, ''))"
)


def _tenant() -> str:
    return f"t-unemb-{uuid.uuid4().hex[:10]}"


@pytest.fixture
def pipeline_search(monkeypatch):
    """Pin the production (pipeline) search path.

    ``tests/pipeline/test_search_pipeline.py`` sets ``_USE_PIPELINE_SEARCH =
    False`` without restoring it, so without this a case that happens to run
    after it asserts against the deprecated legacy path instead.
    """
    monkeypatch.setattr(memory_service, "_USE_PIPELINE_SEARCH", True)


async def _index_for_fts(tenant_id: str) -> None:
    async with get_session() as session:
        await session.execute(
            sa.text(
                f"UPDATE memories SET search_vector = {_SEARCH_VECTOR_SQL} WHERE tenant_id = :t"
            ),
            {"t": tenant_id},
        )
        await session.commit()


# ── the core-api contract ────────────────────────────────────────────────────


def test_unembedded_rows_bypass_every_floor_including_an_explicit_request_one():
    """The branch ignores the request, agent and tenant floors alike.

    Worth stating because the docstring's other sentence — "request, agent, and
    tenant floors remain strict" — describes the ``allow_fts_global_floor_bypass``
    clause only, and a reader can carry it over to the whole function. They are
    not strict here: a caller who asks for ``min_similarity=0.99`` still receives
    un-embedded rows, and no floor any layer can set changes that.

    It is also the reason the value cannot simply be "made strict". A strict
    branch drops every un-embedded row at any positive floor, because the
    ``vec_sim`` it would be compared against is storage's 0.0 sentinel, not a
    measurement. That is the assertion in the middle of this case.
    """
    # Every floor a caller, agent or tenant can express — all bypassed.
    for floor in (0.0, 0.3, 0.75, 0.99, 1.0):
        assert passes_relevance_filter(
            has_embedding=False,
            vec_sim=0.0,
            min_similarity=floor,
            fts_match=True,
        ), f"un-embedded row rejected at min_similarity={floor}"

    # Why it cannot be tightened instead: the same row judged on its sentinel.
    assert not (0.0 >= 0.3), (
        "if this ever holds the sentinel has changed and the branch could be "
        "narrowed; while it does not, a strict branch means no un-embedded row "
        "is ever discoverable"
    )

    # The embedded arm is genuinely strict, for contrast — this is the asymmetry.
    assert not passes_relevance_filter(
        has_embedding=True,
        vec_sim=0.2,
        min_similarity=0.3,
        fts_match=True,
    ), "an embedded row must not bypass the floor without the global-fallback flag"
    assert passes_relevance_filter(
        has_embedding=True,
        vec_sim=0.2,
        min_similarity=0.3,
        fts_match=True,
        allow_fts_global_floor_bypass=True,
    ), "the global-fallback bypass is the embedded row's only exemption"

    # Unknown provenance must fall through to the strict path, never the bypass:
    # ``has_embedding is False`` is an identity check precisely so None does not
    # inherit the exemption.
    assert not passes_relevance_filter(
        has_embedding=None,
        vec_sim=0.2,
        min_similarity=0.3,
    ), "None provenance must be judged strictly, not treated as un-embedded"


# ── the storage precondition the contract above rests on ─────────────────────


@pytest.mark.integration
async def test_unembedded_rows_reach_search_only_through_a_lexical_match(
    client, monkeypatch, pipeline_search
):
    """CAURA-594's admission guard, asserted from core-api's side of the wire.

    Both groups below are written in the same bulk, in the same tenant, and both
    are left un-embedded. The query names one group. If the storage guard were
    relaxed, the other group would arrive as candidates scored on ``weight *
    freshness`` alone — the exact failure CAURA-594 describes — and the floor
    would wave them through, because the floor cannot judge a row with no vector.

    The assertion is against ``all_candidates`` rather than ``items``: that is
    the full set storage returned, before core-api's trim, so it fails on
    admission rather than on ranking. A ``top_k`` large enough to hold both
    groups means nothing here is hidden by the trim.
    """
    monkeypatch.setattr(settings, "deployment_mode", "deferred")
    monkeypatch.setattr(memory_service, "track_task", close_scheduled_coro)

    tenant_id = _tenant()
    headers = get_test_auth(tenant_id)[1]
    tag = uuid.uuid4().hex[:8]

    # Deliberately disjoint vocabularies: no stem is shared between the two, so
    # 'english' stemming cannot make a group-B row match the group-A query.
    named = [f"Quarterly infrastructure runbook {tag} chunk {i}" for i in range(8)]
    unnamed = [f"Dessert pastry glossary {tag}x melon sorbet {i}" for i in range(8)]

    resp = await client.post(
        "/api/v1/memories/bulk",
        headers={**headers, "X-Bulk-Attempt-Id": f"unemb-{uuid.uuid4().hex}"},
        json={
            "tenant_id": tenant_id,
            "agent_id": "bulk-agent",
            "items": [{"content": c} for c in named + unnamed],
        },
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["errors"] == 0, resp.text

    await _index_for_fts(tenant_id)

    resp = await client.post(
        "/api/v1/search",
        headers=headers,
        json={
            "tenant_id": tenant_id,
            "query": f"quarterly infrastructure runbook {tag}",
            "top_k": 50,
            "diagnostic": True,
        },
    )
    assert resp.status_code == 200, resp.text
    candidates = resp.json()["diagnostic"]["all_candidates"]

    unembedded = [c for c in candidates if c["has_embedding"] is False]
    assert unembedded, (
        "no un-embedded candidate was admitted — the deferred window went "
        "unexercised and this case asserted nothing about the guard"
    )

    # The guard, stated positively and negatively.
    assert all(c["fts_match"] for c in unembedded), (
        "an un-embedded candidate arrived WITHOUT a full-text match: storage's "
        "CAURA-594 row filter no longer holds, and because the relevance floor "
        "cannot judge a row with no vector, nothing downstream will stop it"
    )
    titles = " ".join((c.get("title") or "") for c in candidates).lower()
    assert "sorbet" not in titles and "pastry" not in titles, (
        "un-embedded rows unrelated to the query were admitted as candidates — "
        "they can only have entered on weight/freshness, which is precisely the "
        "top_k displacement CAURA-594's guard exists to prevent"
    )
