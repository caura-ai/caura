"""/memories/scored-search caps its own transaction with ``statement_timeout``.

On 2026-10-15 a prod search ran 129s on the read replica after every caller
had given up (core-api answered 504 at 45s), and the retries stacked up beside
it. ``Settings.scored_search_statement_timeout_ms`` now sets SET LOCAL
``statement_timeout`` on the search's read transaction, and the route answers
504 when it fires.

Requires a database like the rest of this tree.
"""

import contextlib

from sqlalchemy import text

import core_storage_api.services.postgres_service as ps
from common.embedding import fake_embedding
from core_storage_api.config import settings as storage_settings

_SP = {
    "fts_weight": 0.3,
    "freshness_floor": 0.7,
    "freshness_decay_days": 90,
    "recall_boost_cap": 1.1,
    "recall_decay_window_days": 14,
    "similarity_blend": 0.85,
}


def _wrap_read_session(monkeypatch, before_query):
    """Run ``before_query(session)`` ahead of each query the search issues on
    its read session, skipping its own ``set_config`` calls."""
    real_get_read_session = ps.get_read_session

    @contextlib.asynccontextmanager
    async def _wrapped():
        async with real_get_read_session() as session:

            class _Spy:
                async def execute(self, stmt, *args, **kwargs):
                    if "set_config" not in str(stmt):
                        await before_query(session)
                    return await session.execute(stmt, *args, **kwargs)

            yield _Spy()

    monkeypatch.setattr(ps, "get_read_session", _wrapped)


async def _search(tenant_id: str):
    return await ps.PostgresService().memory_scored_search(
        tenant_id=tenant_id,
        embedding=fake_embedding("statement timeout probe"),
        query="statement timeout probe",
        search_params=_SP,
        top_k=5,
    )


async def test_statement_timeout_is_live_when_the_search_runs(
    tenant_id, monkeypatch
) -> None:
    monkeypatch.setattr(storage_settings, "scored_search_statement_timeout_ms", 1234)
    observed: list[str] = []

    async def _record(session):
        observed.append(
            (await session.execute(text("SHOW statement_timeout"))).scalar()
        )

    _wrap_read_session(monkeypatch, _record)
    assert await _search(tenant_id) == []  # empty tenant; the assertion is the GUC
    assert observed and set(observed) == {"1234ms"}, (
        f"statement_timeout was not live when the search query ran: {observed!r}"
    )


async def test_zero_leaves_statement_timeout_unset(tenant_id, monkeypatch) -> None:
    monkeypatch.setattr(storage_settings, "scored_search_statement_timeout_ms", 0)
    observed: list[str] = []

    async def _record(session):
        observed.append(
            (await session.execute(text("SHOW statement_timeout"))).scalar()
        )

    _wrap_read_session(monkeypatch, _record)
    await _search(tenant_id)
    assert observed and set(observed) == {"0"}, observed


async def test_a_search_past_its_timeout_answers_504(
    storage_http, tenant_id, monkeypatch
) -> None:
    monkeypatch.setattr(storage_settings, "scored_search_statement_timeout_ms", 100)

    async def _stall(session):
        # Stands in for a scan slower than the cap.
        await session.execute(text("SELECT pg_sleep(1)"))

    _wrap_read_session(monkeypatch, _stall)
    r = await storage_http.post(
        "/api/v1/storage/memories/scored-search",
        json={
            "tenant_id": tenant_id,
            "embedding": fake_embedding("statement timeout probe"),
            "query": "statement timeout probe",
            "search_params": _SP,
            "top_k": 5,
        },
    )
    assert r.status_code == 504, r.text
    assert "statement timeout" in r.json()["detail"]
