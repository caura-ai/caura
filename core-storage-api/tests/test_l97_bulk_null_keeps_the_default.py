"""A bulk item's explicit null ``weight`` or ``created_at`` gets the column's default (L-97).

Migration 001 created both columns nullable, and the models now say so (caura
PR #2033). Eldad chose not to tighten the database (2026-10-11) and to close the
one path that stores a NULL instead. That path is the storage bulk insert: the
route hands ``request.json()`` items to ``memory_add_all``, and its multi-values
INSERT renders a ``None`` as an explicit NULL, which the nullable columns keep.
A NULL weight makes the row's search score NULL, and a NULL ``created_at``
drops it out of every ordering and window on time.

The single-row write never did this: the ORM leaves a ``None`` out of the
INSERT, so the server default applies. It is the control.
"""

from __future__ import annotations

import uuid

import pytest
from httpx import AsyncClient
from sqlalchemy import text

from core_storage_api.database.init import get_engine
from tests.test_integration import PREFIX

pytestmark = pytest.mark.asyncio


async def _rows(tenant_id: str) -> dict:
    async with get_engine().connect() as conn:
        result = await conn.execute(
            text("SELECT content, weight, created_at FROM memories WHERE tenant_id = :t"),
            {"t": tenant_id},
        )
        return {row.content: row for row in result}


def _item(tenant_id: str, content: str, **fields) -> dict:
    return {
        "tenant_id": tenant_id,
        "agent_id": "l97-agent",
        "content": content,
        "memory_type": "fact",
        "client_request_id": f"l97-{uuid.uuid4()}",
        **fields,
    }


async def test_a_bulk_item_with_a_null_weight_and_created_at_gets_the_defaults(client: AsyncClient) -> None:
    tenant_id = f"l97-{uuid.uuid4().hex[:8]}"
    items = [
        _item(tenant_id, "unweighted", weight=None, created_at=None),
        _item(tenant_id, "weighted", weight=0.7, created_at=None),
    ]

    resp = await client.post(f"{PREFIX}/memories/bulk", json=items)

    assert resp.status_code == 200, resp.text
    rows = await _rows(tenant_id)
    assert rows["unweighted"].weight == pytest.approx(0.5)
    assert rows["weighted"].weight == pytest.approx(0.7)
    assert rows["unweighted"].created_at is not None
    # Stamped in batch order, as a batch that sends no created_at is.
    assert rows["unweighted"].created_at < rows["weighted"].created_at


async def test_a_single_write_with_a_null_weight_gets_the_default(client: AsyncClient) -> None:
    """The control: the ORM already leaves the None out of the INSERT."""
    tenant_id = f"l97-{uuid.uuid4().hex[:8]}"
    body = _item(tenant_id, "single", weight=None, created_at=None)

    resp = await client.post(f"{PREFIX}/memories", json=body)

    assert resp.status_code == 200, resp.text
    row = (await _rows(tenant_id))["single"]
    assert row.weight == pytest.approx(0.5)
    assert row.created_at is not None
