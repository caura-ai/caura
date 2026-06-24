"""Unit tests for ``_schema_is_at_head`` (core_storage_api.database.init).

Guards the predicate that lets a non-leader replica start serving without
acquiring the migration advisory lock: True only when the DB's Alembic revision
equals the precomputed script head, and False on a fresh DB (no
``alembic_version`` row → ``current is None``) so the bootstrap/stamp path still
runs under the lock. Stubs alembic's ``MigrationContext`` so the logic is tested
without a DB; ``head`` is passed in directly (the caller precomputes it once).
"""

from __future__ import annotations

import pytest

from core_storage_api.database import init as init_mod


def _patch_current(monkeypatch: pytest.MonkeyPatch, *, current: str | None) -> None:
    class _Ctx:
        @staticmethod
        def configure(connection: object) -> _Ctx:
            return _Ctx()

        def get_current_revision(self) -> str | None:
            return current

    monkeypatch.setattr("alembic.runtime.migration.MigrationContext", _Ctx)


@pytest.mark.parametrize(
    ("current", "head", "expected"),
    [
        ("rev_head", "rev_head", True),  # at head → start serving, skip the lock
        ("rev_old", "rev_head", False),  # migration pending → must wait for the leader
        (None, "rev_head", False),  # fresh DB (no alembic_version) → run under the lock
    ],
)
def test_schema_is_at_head(
    monkeypatch: pytest.MonkeyPatch, current: str | None, head: str, expected: bool
) -> None:
    _patch_current(monkeypatch, current=current)
    assert init_mod._schema_is_at_head(None, head) is expected  # type: ignore[arg-type]
