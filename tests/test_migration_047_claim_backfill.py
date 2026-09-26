"""Migration 047 must not leave in-flight rows readable as unclaimed.

047 adds ``claimed_at`` / ``claim_token`` to support the single-winner
compare-and-swap in ``lifecycle_audit_finalize``. That CAS treats a NULL
``claimed_at`` on an ``in_progress`` row as "free to claim", which is the
correct reading for a row that has never been claimed and the wrong reading
for a row whose operation is running right now.

Adding the column alone produces exactly the second case for every row that is
mid-flight when the migration lands, so the one window in which a concurrent
delivery could steal a live claim would be the deploy that ships the guard.
The migration therefore backfills those rows with ``now()``, which starts the
ordinary lease rather than exempting them from it.

Follows ``test_migration_012_safety_gate``: the module is imported by path and
``alembic.op`` is stubbed, so ``upgrade()`` runs end-to-end and we assert on
the SQL it issues without opening a database connection.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

MIGRATION = (
    Path(__file__).resolve().parent.parent
    / "core-storage-api"
    / "src"
    / "core_storage_api"
    / "database"
    / "migrations"
    / "versions"
    / "047_lifecycle_audit_claimed_at.py"
)


def _load_migration():
    """Load 047 by path — ``047_...`` is not a valid module identifier."""
    name = "_test_alembic_047_lifecycle_audit_claimed_at"
    spec = importlib.util.spec_from_file_location(name, MIGRATION)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _run_upgrade(monkeypatch: pytest.MonkeyPatch) -> tuple[list, list]:
    """Run ``upgrade()`` against a stubbed ``alembic.op``.

    Returns the recorded ``add_column`` and ``execute`` calls.
    """
    from alembic import op as alembic_op

    added: list = []
    executed: list = []
    monkeypatch.setattr(alembic_op, "add_column", lambda *a, **k: added.append((a, k)))
    monkeypatch.setattr(
        alembic_op, "execute", lambda *a, **k: executed.append(str(a[0]))
    )
    monkeypatch.setattr(alembic_op, "get_bind", MagicMock())

    _load_migration().upgrade()
    return added, executed


@pytest.mark.unit
def test_upgrade_backfills_rows_that_are_already_in_progress(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The load-bearing assertion.

    Without the backfill ``upgrade()`` issues no UPDATE at all and this
    fails on an empty ``executed`` list.
    """
    _added, executed = _run_upgrade(monkeypatch)

    updates = [sql for sql in executed if "UPDATE lifecycle_audit" in sql]
    assert updates, (
        "047 adds claimed_at but never backfills it. Every row that is "
        "in_progress when this migration lands then reads as unclaimed, and "
        "the CAS will hand a live operation to a competing delivery for the "
        "whole deploy window."
    )

    sql = " ".join(" ".join(updates).split())
    assert "claimed_at = now()" in sql, f"backfill must stamp a claim: {sql}"
    assert "status = 'in_progress'" in sql, (
        f"backfill must be restricted to rows that are mid-flight: {sql}"
    )


@pytest.mark.unit
def test_backfill_does_not_disturb_rows_that_already_carry_a_claim(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Re-running the migration must not extend a live lease.

    ``claimed_at IS NULL`` in the predicate keeps the backfill a one-shot
    repair. Without it, a re-run would reset the lease on every in-flight
    row and could keep an abandoned claim alive indefinitely.
    """
    _added, executed = _run_upgrade(monkeypatch)
    sql = " ".join(" ".join(executed).split())
    assert "claimed_at IS NULL" in sql, (
        f"backfill must skip rows that already hold a claim: {sql}"
    )


@pytest.mark.unit
def test_both_columns_are_still_added_nullable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The backfill must not have turned this into a table rewrite.

    Nullable with no server default keeps the column addition metadata-only
    in Postgres; only the bounded UPDATE touches rows.
    """
    added, _executed = _run_upgrade(monkeypatch)
    assert len(added) == 2, f"expected two add_column calls, got {len(added)}"
    for args, _kwargs in added:
        table, column = args[0], args[1]
        assert table == "lifecycle_audit"
        assert column.nullable is True, f"{column.name} must be nullable"
        assert column.server_default is None, (
            f"{column.name} must have no server default, or the addition "
            "stops being metadata-only"
        )
