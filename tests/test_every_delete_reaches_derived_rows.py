"""B25 (M-52, M-53): every delete reaches the rows derived from what it deletes.

#1775 made a single delete cascade to the auto-chunk and atomic-fact children
of the deleted row (``metadata.parent_memory_id``), gated on markers the parent
carries because the child lookup had no index. Three gaps still left children
live, carrying the deleted text:

* the bulk delete by id (REST and MCP) and the filter delete never looked;
* ``/ingest/undo/{run_id}`` deletes the run's ``source = "ingest"`` rows, and
  an ingested fact's atomic-fact children carry ``source = "atomic_fact_fanout"``
  (and, written before #1775, no run_id at all);
* a parent written before #1775 on the inline path carries no marker, so even a
  single delete skipped the lookup.

Storage now soft-deletes the derived rows of whatever a delete removes, in the
same transaction, on every delete path. An index on
``metadata ->> 'parent_memory_id'`` (migration 058) serves the lookup, so the
marker gate is gone.
"""

from __future__ import annotations

import pytest

from tests.conftest import get_test_auth
from tests.conftest import uid as _uid

pytestmark = pytest.mark.integration


async def _row(sc, tenant_id, agent, content, **fields):
    return await sc.create_memory(
        {
            "tenant_id": tenant_id,
            "agent_id": agent,
            "memory_type": "fact",
            "content": f"{content} {_uid()}",
            "status": "active",
            "visibility": "scope_team",
            **fields,
        }
    )


async def _child(sc, tenant_id, agent, parent):
    metadata = {"parent_memory_id": str(parent["id"]), "source": "auto_chunk"}
    return await _row(sc, tenant_id, agent, "child slice", metadata_=metadata)


async def _live(sc, tenant_id, row) -> bool:
    return await sc.get_memory(str(row["id"]), tenant_id, read=False) is not None


async def test_a_single_delete_reaches_the_children_of_an_unmarked_parent(
    client, tenant_id, sc
):
    """A parent written before #1775 carries no marker; the lookup needs none."""
    _, headers = get_test_auth(tenant_id)
    agent = f"derived-{_uid()}"
    parent = await _row(sc, tenant_id, agent, "parent document", metadata_={})
    child = await _child(sc, tenant_id, agent, parent)

    resp = await client.delete(
        f"/api/v1/memories/{parent['id']}?tenant_id={tenant_id}", headers=headers
    )

    assert resp.status_code == 204, resp.text
    assert not await _live(sc, tenant_id, child)


async def test_a_bulk_delete_by_id_reaches_the_children(client, tenant_id, sc):
    _, headers = get_test_auth(tenant_id)
    agent = f"derived-{_uid()}"
    parent = await _row(sc, tenant_id, agent, "parent document", metadata_={})
    child = await _child(sc, tenant_id, agent, parent)
    bystander = await _row(sc, tenant_id, agent, "unrelated")

    resp = await client.post(
        "/api/v1/memories/bulk-delete",
        json={"tenant_id": tenant_id, "ids": [str(parent["id"])]},
        headers=headers,
    )

    assert resp.status_code == 200, resp.text
    # The count is every row that left recall: the parent and its child.
    assert resp.json()["deleted"] == 2
    assert not await _live(sc, tenant_id, child)
    assert await _live(sc, tenant_id, bystander)


async def test_a_filter_delete_reaches_the_children_but_not_an_excluded_one(
    client, tenant_id, sc
):
    """The children do not match the filter themselves; they follow the parent.
    An id the caller excluded is still kept, child or not."""
    _, headers = get_test_auth(tenant_id)
    agent = f"derived-{_uid()}"
    tag = f"tag-{_uid()}"
    parent = await _row(
        sc, tenant_id, agent, "parent document", metadata_={"cleanup_tag": tag}
    )
    child = await _child(sc, tenant_id, agent, parent)
    kept = await _child(sc, tenant_id, agent, parent)

    resp = await client.request(
        "DELETE",
        f"/api/v1/memories?tenant_id={tenant_id}",
        json={
            "metadata_filter": {"cleanup_tag": tag},
            "exclude_ids": [str(kept["id"])],
        },
        headers=headers,
    )

    assert resp.status_code == 204, resp.text
    assert not await _live(sc, tenant_id, parent)
    assert not await _live(sc, tenant_id, child)
    assert await _live(sc, tenant_id, kept)


async def test_ingest_undo_reaches_the_derived_rows_of_the_batch(client, tenant_id, sc):
    """M-52: the fan-out children of an ingested fact, with or without the run."""
    _, headers = get_test_auth(tenant_id)
    agent = f"derived-{_uid()}"
    run_id = f"run-{_uid()}"
    ingest = {"source": "ingest"}
    fact = await _row(
        sc, tenant_id, agent, "ingested fact", run_id=run_id, metadata_=ingest
    )
    fanout = {"parent_memory_id": str(fact["id"]), "source": "atomic_fact_fanout"}
    child = await _row(
        sc, tenant_id, agent, "atomic fact", run_id=run_id, metadata_=fanout
    )
    old_child = await _row(sc, tenant_id, agent, "older atomic fact", metadata_=fanout)
    # Shares the run but is not ingest output and derives from nothing deleted.
    manual = {"source": "manual"}
    bystander = await _row(
        sc, tenant_id, agent, "manual note", run_id=run_id, metadata_=manual
    )

    resp = await client.post(
        f"/api/v1/ingest/undo/{run_id}?tenant_id={tenant_id}", headers=headers
    )

    assert resp.status_code == 200, resp.text
    assert resp.json()["deleted"] == 3
    for row in (fact, child, old_child):
        assert not await _live(sc, tenant_id, row)
    assert await _live(sc, tenant_id, bystander)
