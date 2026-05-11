"""Document store CRUD and query endpoints."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request

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
    )
    return [orm_to_dict(d, DOCUMENT_FIELDS) for d in docs]


@router.get("/{collection}/{doc_id}")
async def get_document(
    collection: str,
    doc_id: str,
    tenant_id: str,
) -> dict:
    doc = await _svc.document_get_by_doc_id(
        tenant_id=tenant_id,
        collection=collection,
        doc_id=doc_id,
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


@router.delete("/{collection}/{doc_id}")
async def delete_document(
    collection: str,
    doc_id: str,
    tenant_id: str,
) -> dict:
    try:
        deleted_id = await _svc.document_delete_by_doc_id(
            tenant_id=tenant_id,
            collection=collection,
            doc_id=doc_id,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if deleted_id is None:
        raise HTTPException(status_code=404, detail="Document not found")
    return {"deleted_id": str(deleted_id)}
