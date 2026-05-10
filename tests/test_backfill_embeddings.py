"""Unit tests for the OSS embedding-backfill CLI.

The script lives at ``core-storage-api/scripts/backfill_embeddings.py``
and re-embeds rows whose embedding is NULL — the post-migration-010
recovery path for OSS docker-compose users.

These tests mock the engine + ``get_embedding`` so no real DB or
OpenAI account is required. Integration coverage (against a real
local Postgres + fake embedding provider) is covered by the staging
cutover runbook (Spec E), not this PR.
"""

from __future__ import annotations

import uuid
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock

import pytest


def _fake_engine(rows_by_query: dict[str, list[tuple]]) -> MagicMock:
    """Build a minimal AsyncEngine stand-in that returns canned rows for
    each ``conn.execute`` call, keyed by a fragment of the SQL.

    *rows_by_query* maps "memories" / "entities" → the full row list to
    yield in a single page. The first ``execute`` call for each table
    returns those rows; subsequent calls return an empty list (so the
    pagination loop terminates).
    """
    served: dict[str, bool] = {}

    async def _execute(statement, params=None):
        sql = str(statement).lower()
        # UPDATE statements: pretend they succeeded.
        if sql.startswith("update"):
            return MagicMock()
        # SELECT — first call per table returns rows; second returns [].
        for key, rows in rows_by_query.items():
            if key in sql and not served.get(key):
                served[key] = True
                result = MagicMock()
                result.all = MagicMock(return_value=rows)
                return result
        empty = MagicMock()
        empty.all = MagicMock(return_value=[])
        return empty

    conn = MagicMock()
    conn.execute = AsyncMock(side_effect=_execute)
    conn.commit = AsyncMock()

    @asynccontextmanager
    async def _connect():
        yield conn

    engine = MagicMock()
    engine.connect = _connect
    return engine


@pytest.mark.unit
@pytest.mark.asyncio
async def test_backfill_re_embeds_memories_and_entities(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Happy path: each NULL-embedding row gets a re-embed call and a
    corresponding UPDATE. Reports the right scanned/embedded counts."""
    from core_storage_api.scripts.backfill_embeddings import run_backfill

    rows = {
        "from memories": [
            (uuid.uuid4(), "memory content one"),
            (uuid.uuid4(), "memory content two"),
        ],
        "from entities": [
            (uuid.uuid4(), "Acme Corp"),
        ],
    }
    monkeypatch.setattr(
        "core_storage_api.scripts.backfill_embeddings.get_engine"
        if False
        else "core_storage_api.database.init.get_engine",
        lambda: _fake_engine(rows),
    )

    embed = AsyncMock(return_value=[0.1] * 1024)
    monkeypatch.setattr("common.embedding.get_embedding", embed)

    reports = await run_backfill(
        tenant_id=None,
        batch_size=500,
        max_inflight=10,
        dry_run=False,
    )

    by_table = {r.table: r for r in reports}
    assert by_table["memories"].scanned == 2
    assert by_table["memories"].embedded == 2
    assert by_table["entities"].scanned == 1
    assert by_table["entities"].embedded == 1
    # 3 rows → 3 embed calls.
    assert embed.await_count == 3


@pytest.mark.unit
@pytest.mark.asyncio
async def test_backfill_dry_run_skips_provider_and_writes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``--dry-run`` counts what would have been done but doesn't call
    the embedding provider or issue UPDATEs."""
    from core_storage_api.scripts.backfill_embeddings import run_backfill

    rows = {
        "from memories": [(uuid.uuid4(), "x"), (uuid.uuid4(), "y")],
        "from entities": [],
    }
    monkeypatch.setattr(
        "core_storage_api.database.init.get_engine",
        lambda: _fake_engine(rows),
    )
    embed = AsyncMock(return_value=[0.1] * 1024)
    monkeypatch.setattr("common.embedding.get_embedding", embed)

    reports = await run_backfill(
        tenant_id=None, batch_size=500, max_inflight=10, dry_run=True
    )

    by_table = {r.table: r for r in reports}
    assert by_table["memories"].scanned == 2
    assert by_table["memories"].embedded == 2  # counted as "would have"
    assert embed.await_count == 0  # but never actually called


@pytest.mark.unit
@pytest.mark.asyncio
async def test_backfill_skips_empty_content_rows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A row with empty / None content is skipped (not re-embedded with
    a degenerate empty-string vector). Reported under
    ``skipped_empty_content``."""
    from core_storage_api.scripts.backfill_embeddings import run_backfill

    rows = {
        "from memories": [
            (uuid.uuid4(), ""),
            (uuid.uuid4(), "real content"),
            (uuid.uuid4(), None),
        ],
        "from entities": [],
    }
    monkeypatch.setattr(
        "core_storage_api.database.init.get_engine",
        lambda: _fake_engine(rows),
    )
    embed = AsyncMock(return_value=[0.1] * 1024)
    monkeypatch.setattr("common.embedding.get_embedding", embed)

    reports = await run_backfill(
        tenant_id=None, batch_size=500, max_inflight=10, dry_run=False
    )

    mems = next(r for r in reports if r.table == "memories")
    assert mems.scanned == 3
    assert mems.embedded == 1
    assert mems.skipped_empty_content == 2
    assert embed.await_count == 1


@pytest.mark.unit
@pytest.mark.asyncio
async def test_backfill_aborts_on_consecutive_provider_failures(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If ``get_embedding`` returns None on too many consecutive rows,
    the backfill raises a RuntimeError so the operator can investigate
    rather than spending the next hour writing nothing."""
    from core_storage_api.scripts import backfill_embeddings

    n_rows = backfill_embeddings._MAX_CONSECUTIVE_NONES + 5
    rows = {
        "from memories": [(uuid.uuid4(), f"content {i}") for i in range(n_rows)],
        "from entities": [],
    }
    monkeypatch.setattr(
        "core_storage_api.database.init.get_engine",
        lambda: _fake_engine(rows),
    )
    embed = AsyncMock(return_value=None)
    monkeypatch.setattr("common.embedding.get_embedding", embed)

    with pytest.raises(RuntimeError, match="consecutive rows"):
        await backfill_embeddings.run_backfill(
            tenant_id=None, batch_size=500, max_inflight=2, dry_run=False
        )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_backfill_only_table_filter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``--only-table memories`` skips entities entirely (no scan, no
    report)."""
    from core_storage_api.scripts.backfill_embeddings import run_backfill

    rows = {
        "from memories": [(uuid.uuid4(), "m1")],
        "from entities": [(uuid.uuid4(), "ENTITY-SHOULD-NOT-BE-PROCESSED")],
    }
    monkeypatch.setattr(
        "core_storage_api.database.init.get_engine",
        lambda: _fake_engine(rows),
    )
    embed = AsyncMock(return_value=[0.1] * 1024)
    monkeypatch.setattr("common.embedding.get_embedding", embed)

    reports = await run_backfill(
        tenant_id=None,
        batch_size=500,
        max_inflight=10,
        dry_run=False,
        only_table="memories",
    )

    assert len(reports) == 1
    assert reports[0].table == "memories"


# ---------------------------------------------------------------------------
# CLI exit-code coverage
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.asyncio
async def test_amain_returns_2_on_runtime_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``RuntimeError`` from ``run_backfill`` (the "consecutive Nones"
    abort path) maps to exit code 2 — distinguishable for monitoring as
    "embedding provider degraded" rather than "config / unexpected"."""
    from core_storage_api.scripts.backfill_embeddings import _amain

    async def _runtime_explode(**_kw):
        raise RuntimeError("provider returned None on 20 consecutive rows; stopping")

    monkeypatch.setattr(
        "core_storage_api.scripts.backfill_embeddings.run_backfill",
        _runtime_explode,
    )
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")

    code = await _amain([])
    assert code == 2


@pytest.mark.unit
@pytest.mark.asyncio
async def test_amain_returns_1_on_unexpected_exception(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Anything that isn't ``RuntimeError`` (DB unreachable,
    registry-level ``ValueError`` surfacing here, asyncio cancellation,
    etc.) maps to exit code 1 with a stack trace logged. Exit-code 1 vs
    2 lets ops monitoring distinguish 'something else is broken' from
    'provider degraded'."""
    import logging

    from core_storage_api.scripts.backfill_embeddings import _amain

    async def _value_explode(**_kw):
        raise ValueError("OPENAI_EMBEDDING_BASE_URL/SEND_DIMENSIONS conflict")

    monkeypatch.setattr(
        "core_storage_api.scripts.backfill_embeddings.run_backfill",
        _value_explode,
    )
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")

    with caplog.at_level(
        logging.ERROR, logger="core_storage_api.scripts.backfill_embeddings"
    ):
        code = await _amain([])

    assert code == 1
    assert any(
        "configuration or unexpected error" in rec.getMessage()
        for rec in caplog.records
    ), "expected an ERROR log naming the broader error class"


# ---------------------------------------------------------------------------
# CAURA-222: --rewrite-hint-prefixed mode
# ---------------------------------------------------------------------------


def _capturing_engine(
    rows_by_query: dict[str, list[tuple]],
) -> tuple[MagicMock, list[str]]:
    """Like ``_fake_engine`` but also records every SQL string executed,
    so tests can assert on the WHERE clause shape under different modes."""
    captured_sql: list[str] = []
    served: dict[str, bool] = {}

    async def _execute(statement, params=None):
        sql = str(statement)
        captured_sql.append(sql)
        sql_lower = sql.lower()
        if sql_lower.startswith("update"):
            return MagicMock()
        for key, rows in rows_by_query.items():
            if key in sql_lower and not served.get(key):
                served[key] = True
                result = MagicMock()
                result.all = MagicMock(return_value=rows)
                return result
        empty = MagicMock()
        empty.all = MagicMock(return_value=[])
        return empty

    conn = MagicMock()
    conn.execute = AsyncMock(side_effect=_execute)
    conn.commit = AsyncMock()

    @asynccontextmanager
    async def _connect():
        yield conn

    engine = MagicMock()
    engine.connect = _connect
    return engine, captured_sql


@pytest.mark.unit
@pytest.mark.asyncio
async def test_rewrite_hint_prefixed_targets_memories_with_hint_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """--rewrite-hint-prefixed scans rows where embedding IS NOT NULL
    and metadata.retrieval_hint is non-empty. The default mode's
    embedding-IS-NULL filter must NOT appear in this scan."""
    from core_storage_api.scripts.backfill_embeddings import run_backfill

    rows = {
        "from memories": [
            (uuid.uuid4(), "memory previously embedded with a hint prefix"),
        ],
    }
    engine, captured_sql = _capturing_engine(rows)
    monkeypatch.setattr("core_storage_api.database.init.get_engine", lambda: engine)
    embed = AsyncMock(return_value=[0.4] * 1024)
    monkeypatch.setattr("common.embedding.get_embedding", embed)

    reports = await run_backfill(
        tenant_id=None,
        batch_size=500,
        max_inflight=10,
        dry_run=False,
        rewrite_hint_prefixed=True,
    )

    by_table = {r.table: r for r in reports}
    assert "memories" in by_table
    assert by_table["memories"].scanned == 1
    assert by_table["memories"].embedded == 1
    assert embed.await_count == 1

    # Verify the SQL filter shape on the SELECT against memories.
    select_against_memories = [
        s
        for s in captured_sql
        if "select" in s.lower() and "from memories" in s.lower()
    ]
    assert select_against_memories, "no SELECT against memories was emitted"
    sql = select_against_memories[0].lower()
    assert "embedding is not null" in sql
    assert "metadata_ ? 'retrieval_hint'" in sql
    assert "metadata_->>'retrieval_hint'" in sql
    # Default-mode selector must NOT be present.
    assert "embedding is null" not in sql


@pytest.mark.unit
@pytest.mark.asyncio
async def test_rewrite_hint_prefixed_skips_entities(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Entities don't carry retrieval_hint metadata, so the hint-rewrite
    mode skips them — even if rows exist on that table, they shouldn't
    produce a report or burn embed calls."""
    from core_storage_api.scripts.backfill_embeddings import run_backfill

    rows = {
        "from memories": [(uuid.uuid4(), "memory with hint")],
        "from entities": [(uuid.uuid4(), "Acme Corp")],
    }
    engine, captured_sql = _capturing_engine(rows)
    monkeypatch.setattr("core_storage_api.database.init.get_engine", lambda: engine)
    embed = AsyncMock(return_value=[0.5] * 1024)
    monkeypatch.setattr("common.embedding.get_embedding", embed)

    reports = await run_backfill(
        tenant_id=None,
        batch_size=500,
        max_inflight=10,
        dry_run=False,
        rewrite_hint_prefixed=True,
    )

    by_table = {r.table: r for r in reports}
    assert set(by_table.keys()) == {"memories"}
    # The entities table should not have been queried at all in this mode.
    assert not any("from entities" in s.lower() for s in captured_sql)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_default_mode_uses_null_embedding_filter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Belt-and-braces: the existing IS-NULL scan path is unchanged when
    --rewrite-hint-prefixed is not set. Pinned here so the new mode's
    branching can't silently break the default."""
    from core_storage_api.scripts.backfill_embeddings import run_backfill

    rows = {
        "from memories": [(uuid.uuid4(), "x")],
        "from entities": [],
    }
    engine, captured_sql = _capturing_engine(rows)
    monkeypatch.setattr("core_storage_api.database.init.get_engine", lambda: engine)
    monkeypatch.setattr(
        "common.embedding.get_embedding", AsyncMock(return_value=[0.1] * 1024)
    )

    await run_backfill(
        tenant_id=None,
        batch_size=500,
        max_inflight=10,
        dry_run=False,
    )

    select_against_memories = [
        s
        for s in captured_sql
        if "select" in s.lower() and "from memories" in s.lower()
    ]
    assert select_against_memories
    sql = select_against_memories[0].lower()
    assert "embedding is null" in sql
    # Hint-rewrite mode markers must be absent.
    assert "retrieval_hint" not in sql


@pytest.mark.unit
@pytest.mark.asyncio
async def test_amain_warns_on_non_idempotent_rewrite(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Live --rewrite-hint-prefixed runs must surface the non-idempotent
    nature loudly on stderr AND give the operator a 5s grace window to
    Ctrl-C before the run starts. retrieval_hint metadata is preserved
    by the rewrite, so every re-run re-matches and re-embeds the same
    rows — silent on that fact would let an operator burn provider
    quota on no-op repeats."""
    from core_storage_api.scripts.backfill_embeddings import _amain

    async def _noop_run(**_kw):
        return []

    monkeypatch.setattr(
        "core_storage_api.scripts.backfill_embeddings.run_backfill", _noop_run
    )
    # Mock the grace-period sleep so the test doesn't actually wait 5s.
    sleeps: list[float] = []

    async def _record_sleep(secs: float) -> None:
        sleeps.append(secs)

    monkeypatch.setattr(
        "core_storage_api.scripts.backfill_embeddings.asyncio.sleep",
        _record_sleep,
    )
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")

    code = await _amain(["--rewrite-hint-prefixed"])
    captured = capsys.readouterr()

    assert code == 0
    assert "NOT idempotent" in captured.err
    assert "metadata.retrieval_hint" in captured.err
    # Operator grace window: warning fires, then a 5s pause before
    # any provider call, so a fat-fingered re-invocation can be
    # caught with Ctrl-C.
    assert "Starting in 5 s" in captured.err
    assert sleeps == [5]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_amain_no_warning_on_dry_run_rewrite(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """--dry-run + --rewrite-hint-prefixed does not call the provider
    or write anything, so the non-idempotency warning + grace pause
    would just be noise — suppress both for the scope-estimation use
    case."""
    from core_storage_api.scripts.backfill_embeddings import _amain

    async def _noop_run(**_kw):
        return []

    monkeypatch.setattr(
        "core_storage_api.scripts.backfill_embeddings.run_backfill", _noop_run
    )
    sleeps: list[float] = []

    async def _record_sleep(secs: float) -> None:
        sleeps.append(secs)

    monkeypatch.setattr(
        "core_storage_api.scripts.backfill_embeddings.asyncio.sleep",
        _record_sleep,
    )
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")

    code = await _amain(["--rewrite-hint-prefixed", "--dry-run"])
    captured = capsys.readouterr()

    assert code == 0
    assert "NOT idempotent" not in captured.err
    assert "Starting in 5 s" not in captured.err
    assert sleeps == []


@pytest.mark.unit
@pytest.mark.asyncio
async def test_amain_no_warning_on_default_mode(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Default null-embedding scan IS idempotent (writes flip rows from
    NULL to non-NULL, so re-runs see strictly fewer rows). The warning
    and the grace pause must be scoped to --rewrite-hint-prefixed only."""
    from core_storage_api.scripts.backfill_embeddings import _amain

    async def _noop_run(**_kw):
        return []

    monkeypatch.setattr(
        "core_storage_api.scripts.backfill_embeddings.run_backfill", _noop_run
    )
    sleeps: list[float] = []

    async def _record_sleep(secs: float) -> None:
        sleeps.append(secs)

    monkeypatch.setattr(
        "core_storage_api.scripts.backfill_embeddings.asyncio.sleep",
        _record_sleep,
    )
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")

    code = await _amain([])
    captured = capsys.readouterr()

    assert code == 0
    assert "NOT idempotent" not in captured.err
    assert sleeps == []
