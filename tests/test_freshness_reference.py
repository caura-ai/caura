"""``freshness_reference`` — freshness and the temporal window measured from
``valid_at`` instead of ``now()`` — end-to-end against Postgres.

The scenario is a backfilled corpus: every row is CREATED in the same sitting
but carries the time its event happened in ``ts_valid_start``. Under the
default anchor, ``greatest(created_at, ts_valid_start)`` resolves to the ingest
time for all of them, so freshness is a constant and "last month" is a window
ending at the ingest date. The knob retargets both to the request's ``valid_at``.

Invariants pinned here:

* knob OFF + valid_at   ⇒ freshness is flat across the backfilled rows (the
  behaviour being made optional — recorded so the fix is visible as a delta);
* knob ON  + valid_at   ⇒ the row whose event is nearer the as-of time ranks
  first, on freshness alone (identical embedding, weight, lexical score);
* knob ON  + NO valid_at ⇒ ids AND scores identical to knob OFF — the switch
  is inert without an as-of time, so no caller that exists today moves;
* knob ON  + temporal_window ⇒ the window ends at valid_at, not now();
* a NAIVE valid_at is read as UTC rather than 500ing on the timestamptz bind.

Two rows, same embedding vector, same weight, a query that matches neither
lexically — so ``base_score`` is identical and any ordering comes from the
factor under test. Requires a database like the rest of this tree.
"""

import contextlib
import hashlib
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.dialects import postgresql

import core_storage_api.services.postgres_service as ps
from common.embedding import fake_embedding
from core_api.clients.storage_client import get_storage_client

_SP = {
    "fts_weight": 0.3,
    "freshness_floor": 0.7,
    "freshness_decay_days": 90,
    "recall_boost_cap": 1.1,
    "recall_decay_window_days": 14,
    "similarity_blend": 0.85,
    "fts_rank_scale": 6.0,
}

# The as-of time of the question. Well in the past relative to any test run,
# so ``created_at`` (now) is always LATER than both event times below — the
# exact shape that makes the default ``greatest`` anchor collapse to ingest.
AS_OF = datetime(2024, 3, 15, 12, 0, tzinfo=UTC)
RECENT_EVENT = AS_OF - timedelta(
    days=20
)  # inside a 60-day window, well inside 90-day decay
OLD_EVENT = AS_OF - timedelta(days=400)  # past the decay → floor

# One vector for both rows: the probe is this same text, so vec_sim ties.
_SHARED = fake_embedding("shared vector so only time can separate the rows")
# Matches nothing in either row → fts_score 0 for both.
_NEUTRAL_QUERY = "zzznomatchzzz"


async def _insert(tenant_id: str, content: str, *, event_at: datetime):
    sc = get_storage_client()
    return await sc.create_memory(
        {
            "tenant_id": tenant_id,
            "fleet_id": None,
            "agent_id": "test-agent",
            "memory_type": "fact",
            "content": content,
            "weight": 0.5,
            "embedding": _SHARED,
            "content_hash": hashlib.sha256(
                f"{tenant_id}:{content}".encode()
            ).hexdigest(),
            "status": "active",
            "visibility": "scope_team",
            "ts_valid_start": event_at.isoformat(),
        }
    )


async def _search(tenant_id: str, *, freshness_reference: int, valid_at=None, **kw):
    sp = dict(_SP)
    sp["freshness_reference"] = freshness_reference
    rows = await ps.PostgresService().memory_scored_search(
        tenant_id=tenant_id,
        embedding=_SHARED,
        query=_NEUTRAL_QUERY,
        search_params=sp,
        top_k=10,
        valid_at=valid_at,
        **kw,
    )
    return {str(r.Memory.id): float(r.score) for r in rows}, [
        str(r.Memory.id) for r in rows
    ]


@pytest.fixture
async def backfilled(tenant_id):
    recent = await _insert(
        tenant_id, "the irrigation valve was replaced", event_at=RECENT_EVENT
    )
    old = await _insert(
        tenant_id, "the irrigation schedule was first drafted", event_at=OLD_EVENT
    )
    return tenant_id, str(recent["id"]), str(old["id"])


async def test_default_anchor_is_flat_on_a_backfilled_corpus(backfilled):
    """Knob off: both rows were ingested now, so both are 'fresh' — a tie."""
    tenant, recent, old = backfilled
    scores, _ = await _search(tenant, freshness_reference=0, valid_at=AS_OF)
    assert set(scores) == {recent, old}
    assert scores[recent] == pytest.approx(scores[old]), (
        "with freshness anchored to ingest time a backfilled corpus cannot be told apart by age"
    )


async def test_valid_at_anchor_ranks_the_nearer_event_first(backfilled):
    """Knob on: 20 days before the as-of time beats 400 days before it."""
    tenant, recent, old = backfilled
    scores, order = await _search(tenant, freshness_reference=1, valid_at=AS_OF)
    assert set(scores) == {recent, old}
    assert order[0] == recent
    assert scores[recent] > scores[old]
    # Floor is 0.7 and the recent row is at ~20/90 of the decay: the ratio must
    # be a real gap, not a rounding artefact.
    assert scores[recent] / scores[old] > 1.15


async def test_knob_is_inert_without_valid_at(backfilled):
    """The switch alone must not move anyone: identical ids and scores."""
    tenant, _, _ = backfilled
    off_scores, off_order = await _search(tenant, freshness_reference=0)
    on_scores, on_order = await _search(tenant, freshness_reference=1)
    assert on_order == off_order
    assert on_scores == pytest.approx(off_scores)


async def test_temporal_window_ends_at_valid_at(backfilled):
    """A 60-day window: under the knob only the 20-day-old event is inside it."""
    tenant, recent, old = backfilled
    window = timedelta(days=60)

    off_scores, _ = await _search(
        tenant, freshness_reference=0, valid_at=AS_OF, temporal_window=window
    )
    # Default: window ends now(), both rows were CREATED now → both inside → tie.
    assert off_scores[recent] == pytest.approx(off_scores[old])

    on_scores, on_order = await _search(
        tenant, freshness_reference=1, valid_at=AS_OF, temporal_window=window
    )
    assert on_order[0] == recent
    # Freshness already favours the recent row; the window adds the 1.3x on top,
    # so the gap widens compared to the no-window case.
    no_window_scores, _ = await _search(tenant, freshness_reference=1, valid_at=AS_OF)
    assert (on_scores[recent] / on_scores[old]) > (
        no_window_scores[recent] / no_window_scores[old]
    )


async def test_naive_valid_at_is_read_as_utc(backfilled):
    """A naive datetime must bind to timestamptz, not 500 — and mean UTC."""
    tenant, recent, old = backfilled
    aware, aware_order = await _search(tenant, freshness_reference=1, valid_at=AS_OF)
    naive, naive_order = await _search(
        tenant, freshness_reference=1, valid_at=AS_OF.replace(tzinfo=None)
    )
    assert naive_order == aware_order == [recent, old]
    assert naive == pytest.approx(aware)


async def test_naive_valid_at_is_normalized_for_every_timestamp_bind(monkeypatch):
    """The currency-factor bind must share the freshness clock's UTC coercion."""
    captured = []

    class _Stop(Exception):
        pass

    class _Session:
        async def execute(self, stmt, *args, **kwargs):
            captured.append(stmt)
            raise _Stop

    @contextlib.asynccontextmanager
    async def _fake_session():
        yield _Session()

    monkeypatch.setattr(ps, "get_read_session", _fake_session)

    with contextlib.suppress(_Stop):
        await ps.PostgresService().memory_scored_search(
            tenant_id="t",
            embedding=_SHARED,
            query=_NEUTRAL_QUERY,
            # Keep the new freshness clock off: any datetime bind in this
            # statement comes from the pre-existing currency-factor path.
            search_params={**_SP, "freshness_reference": 0},
            valid_at=AS_OF.replace(tzinfo=None),
        )

    assert captured, "no statement reached session.execute"
    params = captured[0].compile(dialect=postgresql.dialect()).params.values()
    datetime_params = [value for value in params if isinstance(value, datetime)]
    assert datetime_params
    assert all(
        value == AS_OF and value.utcoffset() == timedelta(0)
        for value in datetime_params
    )
