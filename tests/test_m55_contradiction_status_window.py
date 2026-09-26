"""09/02 M-55 — contradiction evidence was dropped for memories older than the window.

A contradiction is a status FLIP on an existing memory. That event had no
timestamp anywhere on the row: `memories` carries `created_at`, `deleted_at`,
`expires_at`, `last_recalled_at` and `last_dedup_checked_at` — and no
`updated_at`.

So `outcome_contradiction_signals` windowed on `created_at`, and a memory
written weeks ago but contradicted TODAY fell outside the current scan window.
Its failure evidence was dropped **silently**: "no rows" and "no
contradictions" are the same answer to the caller, so the signal under-reported
without ever erroring.

The extractor's own docstring always described the intended behaviour — "a
memory that's been around for weeks but only just got contradicted SHOULD still
register a firing inside the current scan window" — and the storage docstring
named the missing column as a "CAURA-future". Migration 045 adds it.

Supersession is deliberately NOT changed: there the event IS the creation of
the superseding memory, so `new_mem.created_at` is already the correct
timestamp. Only the flip needed one.
"""

import inspect

import pytest

pytestmark = pytest.mark.unit


def _signal_sql() -> str:
    from core_storage_api.services.postgres_service import PostgresService

    return inspect.getsource(PostgresService.outcome_contradiction_signals)


# ── the column exists and is writable ────────────────────────────────────


def test_the_model_carries_the_transition_timestamp():
    from common.models.memory import Memory

    assert hasattr(Memory, "status_changed_at")


def test_the_column_is_nullable_because_history_cannot_be_backfilled():
    """There is no source of truth for when a historical row's status changed.
    Inventing one — created_at, or the migration time — would fabricate
    evidence and shift old failures into whatever window ran next."""
    from common.models.memory import Memory

    assert Memory.__table__.c.status_changed_at.nullable is True


# ── both write paths stamp it ────────────────────────────────────────────


def test_the_dedicated_status_writer_stamps_the_transition():
    from core_storage_api.services.postgres_service import PostgresService

    src = inspect.getsource(PostgresService.memory_update_status)
    assert "status_changed_at" in src


def test_the_generic_patch_path_stamps_it_too():
    """``memory_update`` also accepts ``status`` (its docstring lists it), so
    it is a SECOND way a status can change. A contradiction applied through
    here would otherwise stay invisible to the window."""
    from core_storage_api.services.postgres_service import PostgresService

    src = inspect.getsource(PostgresService.memory_update)
    assert "status_changed_at" in src
    assert '"status" in values' in src


def test_an_explicit_timestamp_in_the_patch_wins():
    """So a caller replaying a known transition can supply the real time
    instead of having it overwritten with 'now'."""
    from core_storage_api.services.postgres_service import PostgresService

    src = inspect.getsource(PostgresService.memory_update)
    assert '"status_changed_at" not in values' in src


# ── the query uses it, with a safe fallback ──────────────────────────────


def test_the_window_is_on_the_transition_not_the_creation():
    sql = _signal_sql()
    assert "COALESCE(m.status_changed_at, m.created_at) >= :w_start" in sql
    assert "COALESCE(m.status_changed_at, m.created_at) <  :w_end" in sql


def test_the_bare_created_at_window_is_gone():
    """The actual defect. Left in place, old contradictions stay invisible."""
    sql = _signal_sql()
    assert "AND m.created_at >= :w_start" not in sql
    assert "AND m.created_at <  :w_end" not in sql


def test_observed_at_reports_the_transition_too():
    """The evidence carries this through to the trace; reporting creation time
    would date the failure to when the memory was written, not when it was
    found wrong."""
    assert "COALESCE(m.status_changed_at, m.created_at) AS observed_at" in _signal_sql()


def test_pre_existing_rows_keep_todays_behaviour():
    """The COALESCE is what makes 045 safe without a backfill: every row
    written before it has NULL and therefore windows on ``created_at``,
    exactly as before."""
    sql = _signal_sql()
    assert sql.count("COALESCE(m.status_changed_at, m.created_at)") >= 3


# ── supersession is deliberately untouched ───────────────────────────────


def test_the_supersession_signal_still_windows_on_the_new_memory():
    """Not an oversight. For a supersession the event IS the creation of the
    superseding memory, so ``new_mem.created_at`` is already the event time —
    adding a transition column there would describe the wrong moment."""
    from core_storage_api.services.postgres_service import PostgresService

    sql = inspect.getsource(PostgresService.outcome_supersession_signals)
    assert "new_mem.created_at >= :w_start" in sql
    assert "status_changed_at" not in sql


# ── the migration ────────────────────────────────────────────────────────


def test_the_index_is_partial_and_built_concurrently():
    """Only contradicted rows are ever read through this column, and they are a
    small fraction of the table — a full index would cost write throughput on
    every status change for rows no reader looks at. CONCURRENTLY (in an
    autocommit block, matching 040/041) keeps the build off the write path."""
    from pathlib import Path

    import core_storage_api

    mig = (
        Path(core_storage_api.__file__).parent
        / "database/migrations/versions/045_memories_status_changed_at.py"
    )
    src = mig.read_text()
    assert "autocommit_block" in src
    assert "CREATE INDEX CONCURRENTLY" in src
    assert "WHERE status IN ('outdated', 'conflicted')" in src
    # An interrupted CONCURRENTLY build leaves an INVALID index that
    # ``IF NOT EXISTS`` would skip forever — 040 documents this.
    assert "DROP INDEX CONCURRENTLY IF EXISTS" in src
