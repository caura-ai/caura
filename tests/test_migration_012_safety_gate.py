"""Pre-flight safety gate on alembic migration 012_vector_dim_1024.

Verifies the gate's three behaviours:
- Empty DB (count = 0): proceeds without env opt-in (fresh-install path).
- Populated DB + env unset: raises RuntimeError with a clear message.
- Populated DB + env set: proceeds.

The gate is at the top of ``upgrade()``; we exercise it by importing
the migration module and patching ``op.get_bind`` + the env via
``monkeypatch``. No real DB connection is opened.
"""

from __future__ import annotations

import importlib
import sys
from unittest.mock import MagicMock

import pytest


def _load_migration():
    """Re-import the migration module fresh.

    Migration files have hyphenated filenames (``012_vector_dim_1024.py``)
    that aren't valid Python identifiers, so we use ``importlib.util`` to
    load by file path.
    """
    import importlib.util
    from pathlib import Path

    here = Path(__file__).resolve().parent
    repo = here.parent
    mig_path = (
        repo
        / "core-storage-api"
        / "src"
        / "core_storage_api"
        / "database"
        / "migrations"
        / "versions"
        / "012_vector_dim_1024.py"
    )
    name = "_test_alembic_012_vector_dim_1024"
    spec = importlib.util.spec_from_file_location(name, mig_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _patch_op(monkeypatch: pytest.MonkeyPatch, existing_count: int) -> None:
    """Make ``alembic.op`` look like a no-op shim that returns
    *existing_count* from the gate's count query, and silently accepts
    every other call (``op.execute``, ``op.get_context``, the
    autocommit_block context manager).

    This lets ``upgrade()`` run end-to-end without touching a real DB —
    we're only asserting on the gate's branching, not on the ALTER /
    CREATE INDEX side effects.
    """
    from alembic import op as alembic_op

    fake_bind = MagicMock()
    fake_result = MagicMock()
    fake_result.scalar_one.return_value = existing_count
    fake_bind.execute.return_value = fake_result
    monkeypatch.setattr(alembic_op, "get_bind", lambda: fake_bind)

    # Stub out the rest of upgrade()'s side effects so the post-gate
    # body doesn't crash on missing tables / context.
    monkeypatch.setattr(alembic_op, "execute", lambda *_a, **_k: None)
    fake_ctx = MagicMock()
    cm = MagicMock()
    cm.__enter__ = lambda *_: None
    cm.__exit__ = lambda *_: False
    fake_ctx.autocommit_block.return_value = cm
    monkeypatch.setattr(alembic_op, "get_context", lambda: fake_ctx)


@pytest.mark.unit
def test_gate_proceeds_on_empty_db_without_opt_in(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Fresh install: row count == 0, env unset → upgrade() runs to
    completion (no RuntimeError)."""
    monkeypatch.delenv("MEMCLAW_RUN_DESTRUCTIVE_MIGRATIONS", raising=False)
    _patch_op(monkeypatch, existing_count=0)
    mig = _load_migration()
    mig.upgrade()  # must not raise


@pytest.mark.unit
def test_gate_refuses_destructive_run_without_opt_in(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Populated DB, env unset → RuntimeError with the row count and
    the env var name in the message."""
    monkeypatch.delenv("MEMCLAW_RUN_DESTRUCTIVE_MIGRATIONS", raising=False)
    _patch_op(monkeypatch, existing_count=42_000)
    mig = _load_migration()
    with pytest.raises(RuntimeError) as ei:
        mig.upgrade()
    assert "42000" in str(ei.value)
    assert "MEMCLAW_RUN_DESTRUCTIVE_MIGRATIONS" in str(ei.value)


@pytest.mark.unit
def test_gate_proceeds_with_explicit_opt_in(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Populated DB, env set to 'true' → upgrade() proceeds."""
    monkeypatch.setenv("MEMCLAW_RUN_DESTRUCTIVE_MIGRATIONS", "true")
    _patch_op(monkeypatch, existing_count=42_000)
    mig = _load_migration()
    mig.upgrade()  # must not raise


@pytest.mark.unit
@pytest.mark.parametrize("val", ["", "yes", "1", "TRUE_BUT_TYPO", "no", "false"])
def test_gate_treats_non_true_values_as_opt_out(
    monkeypatch: pytest.MonkeyPatch,
    val: str,
) -> None:
    """Anything other than (case-insensitive) ``"true"`` is treated as
    opt-out, so an operator's typo doesn't accidentally green-light
    data destruction."""
    monkeypatch.setenv("MEMCLAW_RUN_DESTRUCTIVE_MIGRATIONS", val)
    _patch_op(monkeypatch, existing_count=10)
    mig = _load_migration()
    with pytest.raises(RuntimeError):
        mig.upgrade()


@pytest.mark.unit
@pytest.mark.parametrize("val", ["true", "TRUE", "True"])
def test_gate_accepts_case_insensitive_true(
    monkeypatch: pytest.MonkeyPatch,
    val: str,
) -> None:
    """``MEMCLAW_RUN_DESTRUCTIVE_MIGRATIONS=TRUE`` (or ``True``) opts
    in. Operators commonly capitalize bool envs."""
    monkeypatch.setenv("MEMCLAW_RUN_DESTRUCTIVE_MIGRATIONS", val)
    _patch_op(monkeypatch, existing_count=10)
    mig = _load_migration()
    mig.upgrade()  # must not raise
