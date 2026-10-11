"""core-api's storage client addresses a document by the exact collection and
doc_id it was given (M-14, M-81, M-117).

The client put both into the storage URL unescaped, so a ``/`` in the
collection, or a ``?``, ``#``, ``%XX`` or dot segment in either, addressed a
different document or a different route:

- M-14: reads and deletes missed the row, and a collection named
  ``collections`` was listed as the collection index.
- M-81: the overwrite gate's lookup found no document under such a name, so a
  trust-1 agent replaced another fleet's document as though creating it.
- M-117: a ``?`` or ``#`` in the collection sent that lookup to the list
  route, and the gate crashed on every agent write to the collection.

Real in-process storage through the conftest bridge, so storage's own routing
decides what each request reaches.
"""

from __future__ import annotations

import pytest
from fastapi import HTTPException

from core_api.services.agent_service import enforce_document_overwrite
from tests.conftest import new_tenant_id

pytestmark = pytest.mark.asyncio

# (collection, doc_id). The first is a control: a ``/`` in a doc_id always
# worked, because storage matches the rest of the path into it.
NAMES = [
    ("notes", "forge/x"),
    ("team/notes", "a"),
    ("notes", "faq?v=2"),
    ("notes", "issue#123"),
    ("notes", "a%2Fb"),
    ("notes", "a/../b"),
    ("notes?draft", "a"),
    ("notes#x", "a"),
    ("collections", "a"),
]


@pytest.mark.parametrize(("collection", "doc_id"), NAMES)
async def test_a_document_is_read_listed_and_deleted_by_its_exact_name(
    sc, collection, doc_id
):
    tenant = new_tenant_id()
    await sc.upsert_document(
        {
            "tenant_id": tenant,
            "collection": collection,
            "doc_id": doc_id,
            "data": {"v": 1},
        }
    )

    got = await sc.get_document(tenant, collection, doc_id, read=False)
    assert got is not None
    assert (got["collection"], got["doc_id"]) == (collection, doc_id)
    listed = await sc.list_documents(tenant, collection)
    assert [d["doc_id"] for d in listed] == [doc_id]
    assert await sc.delete_document(tenant, collection, doc_id) is True
    assert await sc.get_document(tenant, collection, doc_id, read=False) is None


@pytest.mark.parametrize(("collection", "doc_id"), NAMES)
async def test_the_overwrite_gate_finds_another_fleets_document(sc, collection, doc_id):
    tenant = new_tenant_id()
    await sc.create_or_update_agent(
        {"tenant_id": tenant, "agent_id": "low", "trust_level": 1, "fleet_id": "f2"}
    )
    await sc.upsert_document(
        {
            "tenant_id": tenant,
            "collection": collection,
            "doc_id": doc_id,
            "data": {"steps": "the real runbook"},
            "agent_id": "author",
            "fleet_id": "f1",
        }
    )

    with pytest.raises(HTTPException) as exc:
        await enforce_document_overwrite(
            tenant, "low", collection=collection, doc_id=doc_id, force=False
        )

    assert exc.value.status_code == 403


@pytest.mark.parametrize(("collection", "doc_id"), NAMES)
async def test_the_overwrite_gate_lets_a_create_through(sc, collection, doc_id):
    tenant = new_tenant_id()
    await sc.create_or_update_agent(
        {"tenant_id": tenant, "agent_id": "low", "trust_level": 1, "fleet_id": "f2"}
    )

    await enforce_document_overwrite(
        tenant, "low", collection=collection, doc_id=doc_id, force=False
    )
