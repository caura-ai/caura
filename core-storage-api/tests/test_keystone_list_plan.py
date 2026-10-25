"""#112: the keystone list is served by an index it already has.

``list_keystones`` carried a NOTE that its ``data["weight"]`` sort "requires a
functional index" and was "a sequential scan" without one, tracked in #112.
Measured, it is neither. Its statement filters on ``tenant_id`` and
``collection`` before anything else, both equalities that
``ix_documents_tenant_collection`` (migration 001) answers, so a list reads
only that tenant's keystones and the ``LIMIT`` turns the sort into a top-N
heapsort. Against 20,300 documents, 300 of them one tenant's keystones, a
generic plan was a bitmap index scan on that index feeding a 32kB top-N sort.

The proposed ``((data->>'weight')::int)`` index could not have served the sort
in any case: the statement binds each JSON key as a parameter and casts the
weight to FLOAT. So what the list depends on is this one index, and that is
what this pins, on the statement the service actually issues.
"""

from __future__ import annotations

import pytest

from core_storage_api.services.keystones import _list_statement
from tests.conftest import plan_with_only_index

pytestmark = pytest.mark.asyncio


@pytest.mark.parametrize(
    ("fleet_id", "agent_id"),
    [(None, None), ("fleet-a", None), ("fleet-a", "agent-1")],
    ids=["tenant-rules", "with-fleet-rules", "with-agent-rules"],
)
async def test_the_keystone_list_is_served_by_the_tenant_collection_index(
    _ensure_schema, fleet_id, agent_id
) -> None:
    stmt = _list_statement(tenant_id="t-112", fleet_id=fleet_id, agent_id=agent_id)
    plan = await plan_with_only_index(stmt, "ix_documents_tenant_collection", table="documents")
    assert "ix_documents_tenant_collection" in plan, plan
