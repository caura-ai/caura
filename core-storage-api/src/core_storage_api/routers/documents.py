"""Document store CRUD and query endpoints."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query, Request

from core_storage_api.schemas import DOCUMENT_FIELDS, orm_to_dict
from core_storage_api.services.postgres_service import PostgresService

router = APIRouter(prefix="/documents", tags=["Documents"])
_svc = PostgresService()


@router.post("")
async def upsert_document(request: Request) -> dict:
    body: dict = await request.json()
    try:
        doc = await _svc.document_upsert(
            tenant_id=body["tenant_id"],
            collection=body["collection"],
            doc_id=body["doc_id"],
            data=body["data"],
            fleet_id=body.get("fleet_id"),
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return orm_to_dict(doc, DOCUMENT_FIELDS)


@router.post("/upsert-xmax")
async def upsert_document_xmax(request: Request) -> dict:
    """Upsert returning (id, created_at, updated_at, xmax).

    ``xmax == 0`` → INSERT (new row); ``xmax != 0`` → UPDATE (the
    on-conflict path fired). ``embedding`` is opt-in; passing ``None``
    on a re-write clears a previously-indexed doc's vector (intentional).
    """
    body: dict = await request.json()
    try:
        row = await _svc.document_upsert_returning_xmax(
            tenant_id=body["tenant_id"],
            collection=body["collection"],
            doc_id=body["doc_id"],
            data=body["data"],
            fleet_id=body.get("fleet_id"),
            embedding=body.get("embedding"),
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    # ``.returning(Document.id, Document.created_at, Document.updated_at, text("xmax"))``
    # — index positionally (the Row is typed as a plain ``tuple``).
    doc_id_, created_at, updated_at, xmax = row
    return {
        "id": str(doc_id_),
        "created_at": created_at.isoformat(),
        "updated_at": updated_at.isoformat(),
        "xmax": int(xmax),
    }


@router.post("/search")
async def search_documents(request: Request) -> list[dict]:
    body: dict = await request.json()
    pairs = await _svc.document_search(
        tenant_id=body["tenant_id"],
        query_embedding=body["query_embedding"],
        collection=body.get("collection"),
        top_k=body.get("top_k", 5),
        fleet_id=body.get("fleet_id"),
        readable_tenant_ids=body.get("readable_tenant_ids"),
        status=body.get("status"),
    )
    results: list[dict] = []
    for d, sim in pairs:
        row = orm_to_dict(d, DOCUMENT_FIELDS)
        row["similarity"] = sim
        results.append(row)
    return results


@router.post("/update-status")
async def update_document_status(request: Request) -> dict:
    """Conditional (CAS) status flip on a document's ``data`` jsonb.

    Backs ``skill_promoter.make_db_status_updater`` (Fix 2 Ph5a). The UPDATE
    narrows on the EXPECTED source status; a zero-row match → 404 so core-api
    raises ``AlreadyTransitionedError`` (mirrors the bec8229 "404 not
    silent-200" decision). Returns ``{updated: true, doc_id}`` on a match.
    """
    body: dict = await request.json()
    tenant_id = body.get("tenant_id")
    if not tenant_id:
        raise HTTPException(status_code=422, detail="tenant_id is required")
    for key in ("collection", "doc_id", "new_status", "expected_status"):
        if not body.get(key):
            raise HTTPException(status_code=422, detail=f"{key} is required")
    updated = await _svc.document_update_status(
        tenant_id=tenant_id,
        collection=body["collection"],
        doc_id=body["doc_id"],
        new_status=body["new_status"],
        expected_status=body["expected_status"],
    )
    if not updated:
        # CAS miss: the row no longer carries ``expected_status`` (a
        # concurrent writer transitioned it, or it never existed). 404 so the
        # client returns None and the promoter raises AlreadyTransitionedError.
        raise HTTPException(status_code=404, detail="No document matched the expected status")
    return {"updated": True, "doc_id": body["doc_id"]}


@router.post("/query")
async def query_documents(request: Request) -> list[dict]:
    body: dict = await request.json()
    docs = await _svc.document_query(
        tenant_id=body["tenant_id"],
        collection=body["collection"],
        fleet_id=body.get("fleet_id"),
        where=body.get("where"),
        order_by=body.get("order_by"),
        order=body.get("order", "asc"),
        limit=body.get("limit", 20),
        offset=body.get("offset", 0),
        readable_tenant_ids=body.get("readable_tenant_ids"),
    )
    return [orm_to_dict(d, DOCUMENT_FIELDS) for d in docs]


# NOTE: /documents/collections MUST be registered BEFORE /documents/{collection}
# and /documents/{collection}/{doc_id} — FastAPI matches in declaration order, so
# otherwise ``GET /documents/collections`` would bind ``collection="collections"``.
@router.get("/collections")
async def list_collections(
    tenant_id: str,
    fleet_id: str | None = None,
    readable_tenant_ids: list[str] | None = Query(default=None),
) -> dict:
    rows = await _svc.document_list_collections(
        tenant_id=tenant_id,
        fleet_id=fleet_id,
        readable_tenant_ids=readable_tenant_ids,
    )
    return {
        "collections": [{"name": name, "count": count} for name, count in rows],
        "count": len(rows),
    }


# NOTE: registered BEFORE /{collection}/{doc_id} and /{collection} so the
# literal "collection-count" path doesn't bind ``collection="collection-count"``.
@router.get("/collection-count")
async def collection_count(
    tenant_id: str,
    collection: str,
    status: str | None = None,
    fleet_id: str | None = None,
    readable_tenant_ids: list[str] | None = Query(default=None),
) -> dict:
    """Count documents in ``collection``, optionally filtered by
    ``data->>'status'``. Backs the MCP skills active-only count correction."""
    count = await _svc.document_count_in_collection(
        tenant_id=tenant_id,
        collection=collection,
        status=status,
        fleet_id=fleet_id,
        readable_tenant_ids=readable_tenant_ids,
    )
    return {"count": count}


# ``doc_id:path`` so by-id access works for doc_ids that contain a slash —
# forge-distilled skills use ``forge/<slug>`` (see distill_prompt). The doc_id
# is only ever a bound SQL param here, never a filesystem path, so matching the
# rest of the URL into it is safe.
@router.get("/{collection}/{doc_id:path}")
async def get_document(
    collection: str,
    doc_id: str,
    tenant_id: str,
    readable_tenant_ids: list[str] | None = Query(default=None),
) -> dict:
    doc = await _svc.document_get_by_doc_id(
        tenant_id=tenant_id,
        collection=collection,
        doc_id=doc_id,
        readable_tenant_ids=readable_tenant_ids,
    )
    if doc is None:
        raise HTTPException(status_code=404, detail="Document not found")
    return orm_to_dict(doc, DOCUMENT_FIELDS)


@router.get("/{collection}")
async def list_documents(
    collection: str,
    tenant_id: str,
    fleet_id: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> list[dict]:
    docs = await _svc.document_list_by_collection(
        tenant_id=tenant_id,
        collection=collection,
        fleet_id=fleet_id,
        limit=limit,
        offset=offset,
    )
    return [orm_to_dict(d, DOCUMENT_FIELDS) for d in docs]


@router.delete("/{collection}/{doc_id:path}")
async def delete_document(
    collection: str,
    doc_id: str,
    tenant_id: str,
    require_status: str | None = None,
) -> dict:
    """Delete one document. ``require_status`` (optional) folds a
    ``data->>'status' = :status`` guard into the DELETE atomically — a
    non-matching/missing row deletes zero rows and 404s, indistinguishable
    from a missing one (the MCP skills active-only delete gate)."""
    try:
        deleted_id = await _svc.document_delete_by_doc_id(
            tenant_id=tenant_id,
            collection=collection,
            doc_id=doc_id,
            require_status=require_status,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if deleted_id is None:
        raise HTTPException(status_code=404, detail="Document not found")
    return {"deleted_id": str(deleted_id)}
