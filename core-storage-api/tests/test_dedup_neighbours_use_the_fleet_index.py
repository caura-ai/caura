"""The dedup sweep's neighbour lookup is narrowed to one fleet by an index.

A near-duplicate pair never spans fleets, and the nightly sweep passes no fleet,
so ``memory_find_near_duplicate_pairs`` matches each neighbour to its
candidate's fleet inside the ``LATERAL``. That match was spelled
``n.fleet_id IS NOT DISTINCT FROM c.fleet_id``, which Postgres cannot use as an
index condition. On a 231k-row prod tenant whose shared rows sit in 8 fleets,
each candidate's lookup read all 166,508 of the tenant's live shared rows and
dropped 165,033 of them for being in another fleet: ~0.57s per candidate, so
every 500-row batch outran its statement timeout and the sweep fell behind.

``ix_memories_tenant_fleet`` (migration 001) answers the match once each kind
of candidate fleet gets its own branch: ``n.fleet_id = c.fleet_id`` for a
fleet, ``n.fleet_id IS NULL`` for none. An ``OR`` of those two arms is not
enough; under a plain index scan it stays a filter, which is the plan this test
saw for it. So this pins, on the statement the service actually issues, that
with that index as the table's only one both fleet matches are index
conditions. ``tests/test_lifecycle_visibility_and_entity_identity.py`` pins
what the match means (pairs stay in one fleet, NULL pairs only with NULL).
"""

from __future__ import annotations

import contextlib
import re

import pytest
from sqlalchemy import Text, bindparam
from sqlalchemy.dialects.postgresql import ARRAY

import core_storage_api.services.postgres_service as ps
from tests.conftest import plan_with_only_index

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


def _capture_pairs_statement(monkeypatch) -> list:
    """Collect the pairs statement, bound to its parameters, as the sweep issues it."""
    captured: list = []
    real_get_session = ps.get_session

    @contextlib.asynccontextmanager
    async def _wrapped():
        async with real_get_session() as session:

            class _Spy:
                async def execute(self, stmt, params=None, *args, **kwargs):
                    if "LATERAL" in str(stmt):
                        # ``plan_with_only_index`` renders binds as literals,
                        # which a list can only be once it is typed.
                        binds = [
                            bindparam(k, v, type_=ARRAY(Text)) if isinstance(v, list) else bindparam(k, v)
                            for k, v in params.items()
                        ]
                        captured.append(stmt.bindparams(*binds))
                    return await session.execute(stmt, params, *args, **kwargs)

            yield _Spy()

    monkeypatch.setattr(ps, "get_session", _wrapped)
    return captured


async def test_the_nightly_sweep_finds_neighbours_through_the_fleet_index(
    _ensure_schema, tenant_id, monkeypatch
) -> None:
    captured = _capture_pairs_statement(monkeypatch)
    await ps.PostgresService().memory_find_near_duplicate_pairs(
        tenant_id=tenant_id, fleet_id=None, batch_size=10
    )
    [stmt] = captured

    plan = await plan_with_only_index(stmt, "ix_memories_tenant_fleet")
    # The candidates are not narrowed by fleet (the sweep passed none), so every
    # index condition on ``fleet_id`` is a neighbour lookup's.
    index_conds = [line for line in plan.splitlines() if "Index Cond:" in line]
    assert any(re.search(r"\(fleet_id = \w+\.fleet_id\)", line) for line in index_conds), (
        f"a candidate's fleet does not narrow its neighbour lookup by index:\n{plan}"
    )
    assert any("(fleet_id IS NULL)" in line for line in index_conds), (
        f"a candidate with no fleet does not narrow its neighbour lookup by index:\n{plan}"
    )
