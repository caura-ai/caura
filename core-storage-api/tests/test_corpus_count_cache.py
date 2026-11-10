"""The report's corpus counts, kept for five minutes — no database.

Prism App Review v2 N035. ``memory_quality_metrics(recalled_after=...)`` counts
the whole corpus search can return (``corpus_total``) and how much of it was
recalled in the window (``recalled_in_window``). It is the only quality query
bounded by neither ``created_at`` nor ``memory_type``, so every report read
made one pass over all of a tenant's live rows, and a dashboard that reads the
report on each load paid it every time.

The counts are kept per scope and window for five minutes: a share of the
corpus recalled "this period" moves slowly, and a few minutes' lag is the
price of not rescanning. The key is every input the corpus filters read, so a
tenant, a fleet, an agent or an exclusion list never shares another's count.

Same capture technique as ``test_ann_pool_statement``: the statements are
intercepted on their way to ``session.execute`` and answered by shape.
"""

from __future__ import annotations

import contextlib
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.dialects import postgresql

import core_storage_api.services.postgres_service as ps
from core_storage_api.services.corpus_counts import CorpusCountCache

pytestmark = pytest.mark.asyncio

BASE = {
    "tenant_id": "tenant-a",
    "exclude_memory_types": ["working"],
    "exclude_agent_ids": ["firehose"],
    "exclude_title_regex": "^tmp",
    "include_scope_agent": True,
}
WINDOW = datetime(2026, 11, 5, 12, 0, 30, tzinfo=UTC)


class _Result:
    def __init__(self, rows: list) -> None:
        self._rows = rows

    def all(self) -> list:
        return self._rows

    def one(self):
        return self._rows[0]


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    now = [1_000.0]
    monkeypatch.setattr(ps, "_CORPUS_COUNTS", CorpusCountCache(clock=lambda: now[0]))
    return now


@pytest.fixture
def corpus_reads(monkeypatch: pytest.MonkeyPatch) -> list[dict]:
    """The corpus statement's bound parameters, one entry per time it ran."""
    reads: list[dict] = []

    class _Session:
        async def execute(self, stmt, *args, **kwargs):
            compiled = stmt.compile(dialect=postgresql.dialect())
            sql = str(compiled)
            if "last_recalled_at" in sql:
                reads.append(compiled.params)
                return _Result([(10, 4)])
            if "GROUP BY" in sql or "LIMIT" in sql:
                return _Result([])
            return _Result([(0, 0, 0)])

    @contextlib.asynccontextmanager
    async def _fake_session():
        yield _Session()

    monkeypatch.setattr(ps, "get_read_session", _fake_session)
    return reads


def _counts(result: dict) -> tuple[int, int]:
    return result["corpus_total"], result["recalled_in_window"]


async def test_one_count_serves_the_same_scope_and_window_for_five_minutes(clock, corpus_reads):
    svc = ps.PostgresService()
    first = await svc.memory_quality_metrics(**BASE, recalled_after=WINDOW)
    clock[0] += 120
    again = await svc.memory_quality_metrics(**BASE, recalled_after=WINDOW + timedelta(minutes=2))

    assert len(corpus_reads) == 1
    assert _counts(first) == _counts(again) == (10, 4)


async def test_counts_again_once_five_minutes_have_passed(clock, corpus_reads):
    svc = ps.PostgresService()
    await svc.memory_quality_metrics(**BASE, recalled_after=WINDOW)
    clock[0] += 300
    await svc.memory_quality_metrics(**BASE, recalled_after=WINDOW)

    assert len(corpus_reads) == 2


async def test_every_report_in_the_same_five_minutes_counts_from_the_same_start(clock, corpus_reads):
    await ps.PostgresService().memory_quality_metrics(**BASE, recalled_after=WINDOW)

    (bound,) = [v for v in corpus_reads[0].values() if isinstance(v, datetime)]
    assert bound == datetime(2026, 11, 5, 12, 0, tzinfo=UTC)


async def test_another_window_is_counted_on_its_own(clock, corpus_reads):
    svc = ps.PostgresService()
    await svc.memory_quality_metrics(**BASE, recalled_after=WINDOW)
    await svc.memory_quality_metrics(**BASE, recalled_after=WINDOW - timedelta(days=6))

    assert len(corpus_reads) == 2


@pytest.mark.parametrize(
    "change",
    [
        {"tenant_id": "tenant-b"},
        {"readable_tenant_ids": ["tenant-a", "tenant-b"]},
        {"fleet_id": "fleet-1"},
        {"agent_id": "agent-1"},
        {"exclude_memory_types": ["working", "episode"]},
        {"exclude_agent_ids": []},
        {"exclude_title_regex": None},
        {"include_scope_agent": False},
    ],
    ids=lambda change: next(iter(change)),
)
async def test_any_other_scope_is_counted_on_its_own(change, clock, corpus_reads):
    svc = ps.PostgresService()
    await svc.memory_quality_metrics(**BASE, recalled_after=WINDOW)
    await svc.memory_quality_metrics(**{**BASE, **change}, recalled_after=WINDOW)

    assert len(corpus_reads) == 2


async def test_a_report_without_a_window_reads_no_corpus(clock, corpus_reads):
    result = await ps.PostgresService().memory_quality_metrics(**BASE)

    assert corpus_reads == []
    assert "corpus_total" not in result
