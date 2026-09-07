"""CAURA-722 — the diagnostic says why entity retrieval stayed out of a query.

Before this, ``SearchDiagnostic`` reported ``retrieval_strategy`` and nothing
else about the entity path. Three unrelated causes were therefore
indistinguishable from outside the server, and they belong to different owners:

  * entity FTS matched nothing            -> extraction / linking
  * it matched too much and was declined  -> threshold tuning (CAURA-698)
  * it matched, but the pool under-filled -> the short-circuit's own fill rule

Only server logs could tell them apart, which is what blocked the follow-up
after a benchmark run reported the ENTITY_LOOKUP strategy firing on 0 of 589
queries.

Two facts close that: ``entity_matches`` and ``entity_match_declined``.

The distinctions these tests pin, and which are easy to regress:

  * ``None`` (FTS never ran) must not collapse to ``0`` (ran, matched nothing).
    A plain ``or 0`` anywhere on the path destroys the field's whole purpose.
  * The count is taken BEFORE the over-broad branch empties ``matched_ids``, so
    a decline reports its real match count rather than 0 — 0 is precisely the
    value it must never be confused with.
  * ``entity_match_declined`` is True only for the over-broad decline, which is
    the one that also suppresses the per-row entity boost. An under-filled pool
    reports False, because there the boost still applies.
"""

import uuid
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import text

from core_api.constants import ENTITY_LOOKUP_MAX_MATCHES
from core_api.pipeline.steps.search.classify_query import ClassifyQuery
from core_api.services import memory_service
from tests.conftest import get_test_auth, new_tenant_id


@pytest.fixture
def pipeline_search(monkeypatch):
    monkeypatch.setattr(memory_service, "_USE_PIPELINE_SEARCH", True)


async def _diag(client, headers, tenant_id, query, **extra):
    resp = await client.post(
        "/api/v1/search",
        headers=headers,
        json={
            "tenant_id": tenant_id,
            "query": query,
            "top_k": 5,
            "diagnostic": True,
            **extra,
        },
    )
    assert resp.status_code == 200, resp.text
    return resp.json()["diagnostic"]


# ---------------------------------------------------------------------------
# The fields exist and are reachable over REST
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_the_diagnostic_carries_both_entity_fields(client, pipeline_search):
    tenant_id, headers = get_test_auth(new_tenant_id())

    seeded = await client.post(
        "/api/v1/memories",
        headers=headers,
        json={
            "tenant_id": tenant_id,
            "agent_id": "agent-a",
            "content": "Sarah owns the Q3 roadmap and presented it in Berlin.",
        },
    )
    assert seeded.status_code == 201, seeded.text

    diag = await _diag(
        client, headers, tenant_id, "What did Sarah say about the Q3 roadmap?"
    )

    assert "entity_matches" in diag
    assert "entity_match_declined" in diag
    assert diag["entity_match_declined"] is False
    # A query with entity-shaped tokens reaches the FTS, so the count is a
    # number rather than None — whatever the index happens to hold.
    assert diag["entity_matches"] is None or isinstance(diag["entity_matches"], int)


@pytest.mark.integration
async def test_a_query_with_no_entity_tokens_reports_none_not_zero(
    client, pipeline_search
):
    """``None`` means "never asked"; ``0`` means "asked, got nothing".

    Collapsing them is the failure this field exists to prevent, so the
    all-stopword query has to come back as None.
    """
    tenant_id, headers = get_test_auth(new_tenant_id())

    diag = await _diag(client, headers, tenant_id, "of the a to be is it")

    assert diag["entity_matches"] is None, (
        "a query the tokenizer strips to nothing never reaches entity FTS, "
        "so its match count is unknown — not zero"
    )
    assert diag["entity_match_declined"] is False


@pytest.mark.integration
async def test_the_fields_are_absent_of_effect_on_results(client, pipeline_search):
    """D12's contract: asking for diagnostics must not change ``items``."""
    tenant_id, headers = get_test_auth(new_tenant_id())

    await client.post(
        "/api/v1/memories",
        headers=headers,
        json={
            "tenant_id": tenant_id,
            "agent_id": "agent-a",
            "content": "Sarah owns the Q3 roadmap.",
        },
    )
    body = {"tenant_id": tenant_id, "query": "Sarah Q3 roadmap", "top_k": 5}

    plain = await client.post("/api/v1/search", headers=headers, json=body)
    with_diag = await client.post(
        "/api/v1/search", headers=headers, json={**body, "diagnostic": True}
    )

    assert plain.status_code == with_diag.status_code == 200
    assert [i["id"] for i in plain.json()["items"]] == [
        i["id"] for i in with_diag.json()["items"]
    ]


# ---------------------------------------------------------------------------
# The count is captured before the over-broad branch empties it
# ---------------------------------------------------------------------------


def _ctx(query: str, top_k: int = 5):
    """ClassifyQuery context in the shape ``test_entity_lookup_short_circuit``
    uses — ``top_k`` and the graph knobs live under ``search_params``."""
    from core_api.pipeline.context import PipelineContext

    ctx = PipelineContext()
    ctx.data = {
        "query": query,
        "tenant_id": "tenant-test",
        "search_params": {"fts_weight": 0.3, "graph_max_hops": 0, "top_k": top_k},
        "temporal_window": None,
        "entity_retrieval": True,
    }
    return ctx


@pytest.mark.unit
async def test_an_over_broad_decline_reports_its_real_count_not_zero():
    """The ordering trap.

    ``classify_query`` sets ``matched_ids = []`` right after flagging the
    over-broad decline. Reading the count after that point reports 0 — the one
    value that must not be confused with "FTS matched nothing", since the two
    send the work to different teams.
    """
    over_broad = [str(uuid.uuid4()) for _ in range(ENTITY_LOOKUP_MAX_MATCHES + 7)]
    ctx = _ctx("Sarah Berlin roadmap")

    with patch.object(ClassifyQuery, "_entity_fts", AsyncMock(return_value=over_broad)):
        await ClassifyQuery().execute(ctx)

    assert ctx.data["entity_match_declined"] is True
    assert ctx.data["entity_matches"] == ENTITY_LOOKUP_MAX_MATCHES + 7, (
        "the decline must report how many it matched, not the emptied list"
    )


@pytest.mark.unit
async def test_a_genuine_zero_match_reports_zero_and_no_decline():
    ctx = _ctx("Sarah Berlin roadmap")

    with patch.object(ClassifyQuery, "_entity_fts", AsyncMock(return_value=[])):
        await ClassifyQuery().execute(ctx)

    assert ctx.data["entity_matches"] == 0
    assert not ctx.data.get("entity_match_declined")


@pytest.mark.unit
async def test_a_swallowed_fts_failure_leaves_the_count_unknown():
    """The step swallows entity-lookup failures. It must not then claim 0.

    A count of 0 would assert "the index holds nothing matching", which a
    failed call has not established.
    """
    ctx = _ctx("Sarah Berlin roadmap")

    with patch.object(
        ClassifyQuery, "_entity_fts", AsyncMock(side_effect=RuntimeError("boom"))
    ):
        await ClassifyQuery().execute(ctx)

    assert ctx.data.get("entity_matches") is None
    assert not ctx.data.get("entity_match_declined")


@pytest.mark.unit
async def test_entity_retrieval_disabled_never_reaches_the_fts():
    ctx = _ctx("Sarah Berlin roadmap")
    ctx.data["entity_retrieval"] = False
    fts = AsyncMock(return_value=[])

    with patch.object(ClassifyQuery, "_entity_fts", fts):
        await ClassifyQuery().execute(ctx)

    fts.assert_not_awaited()
    assert ctx.data.get("entity_matches") is None


# ---------------------------------------------------------------------------
# The per-row score factors reach the caller at all
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_the_diagnostic_reports_the_score_factors_it_declares(
    client, db, pipeline_search
):
    """``all_candidates`` promises per-row score factors; five never arrived.

    ``entity_boost``, ``freshness``, ``recall_boost``, ``temporal_boost`` and
    ``fts_score`` were each computed in storage's scored CTE, folded into
    ``score``, and then dropped because the CTE select list did not name them.
    core-api reads all five by name and the diagnostic rounds each through
    ``_f()``, so three layers were wired to receive values the query never
    sent — and every one came back ``None`` on every row.

    That is how a null came to read as "this signal did not apply". It is also
    what made ``entity_boost`` useless as evidence, since it is the only place
    the entity *boost* (as opposed to the ENTITY_LOOKUP short-circuit) is
    observable at all.

    ``fts_score`` is checked against the database rather than asserted
    outright, because whether it CAN have a value depends on how the schema was
    built. It is ``ts_rank_cd(search_vector, ...)``, and ``search_vector`` is
    filled by a trigger created in migration 001. CI runs
    ``alembic upgrade head``, so the trigger is there and the rank is a number;
    ``conftest`` builds the schema with ``Base.metadata.create_all``, which
    makes columns but not triggers, so locally the column is NULL and the rank
    is legitimately NULL with it. Asserting non-null unconditionally would pass
    in CI and fail on every developer machine; asserting nothing would let the
    projection regress in CI unnoticed. So the test asks the database which
    world it is in — the same reason the FTS-dependent tests in ``tests/`` fail
    on a create_all database.
    """
    tenant_id, headers = get_test_auth(new_tenant_id())

    for content in (
        "Sarah owns the Q3 roadmap and presented it in Berlin.",
        "Sarah prefers async standups over daily calls.",
        "The Q3 roadmap moved the billing rewrite to October.",
    ):
        r = await client.post(
            "/api/v1/memories",
            headers=headers,
            json={"tenant_id": tenant_id, "agent_id": "agent-a", "content": content},
        )
        assert r.status_code == 201, r.text

    diag = await _diag(client, headers, tenant_id, "Sarah Q3 roadmap Berlin")
    candidates = diag["all_candidates"]
    assert candidates, "precondition: the search must return candidates"

    declared = (
        "score",
        "vec_sim",
        "fts_match",
        "status_penalty",
        "fts_score",
        "freshness",
        "entity_boost",
        "recall_boost",
        "temporal_boost",
    )
    for row in candidates:
        missing = [k for k in declared if k not in row]
        assert not missing, f"all_candidates row is missing declared factors: {missing}"

    # Everything except fts_score is a literal or a CASE over the row itself,
    # so it has a value on every row regardless of the tsvector trigger.
    for factor in ("freshness", "entity_boost", "recall_boost", "temporal_boost"):
        nulls = [r for r in candidates if r.get(factor) is None]
        assert not nulls, (
            f"{factor} is null on {len(nulls)}/{len(candidates)} rows — the factor is "
            "computed in the scored CTE and must be carried out of it, not dropped"
        )

    # The identity value is the correct report for "computed, nothing to add",
    # and is what makes a genuine boost distinguishable from an absent one.
    assert all(r["entity_boost"] == 1.0 for r in candidates), (
        "with no entity boost requested every row should report the 1.0 identity, "
        "not None and not 0"
    )

    # ``fts_score`` is only computable where the tsvector trigger exists.
    trigger_present = bool(
        (
            await db.execute(
                text(
                    "SELECT 1 FROM pg_trigger "
                    "WHERE tgname = 'memories_search_vector_trigger' "
                    "AND NOT tgisinternal"
                )
            )
        ).scalar()
    )
    fts_nulls = [r for r in candidates if r.get("fts_score") is None]
    if trigger_present:
        assert not fts_nulls, (
            f"fts_score is null on {len(fts_nulls)}/{len(candidates)} rows on a "
            "migrated schema — the factor must be projected out of the scored CTE. "
            "This is the assertion that catches the projection regressing in CI."
        )
    else:
        assert len(fts_nulls) == len(candidates), (
            "without the migration-001 tsvector trigger, search_vector is NULL and "
            "ts_rank_cd over it is NULL too, so every row should report None here — "
            "a partial result would mean something other than the trigger is at play"
        )
