"""Document Store — structured JSONB records for agents."""

import logging
import re
from datetime import datetime

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from core_api.auth import AuthContext, get_auth_context
from core_api.clients.storage_client import get_storage_client
from core_api.db.session import get_db
from core_api.middleware.idempotency import IDEMPOTENCY_HEADER, idempotency_for
from core_api.middleware.rate_limit import write_limit
from core_api.services.audit_service import log_action, log_cross_tenant_read
from core_api.services.usage_service import check_and_increment_by_tenant as check_and_increment

logger = logging.getLogger(__name__)

router = APIRouter(tags=["Document Store"])


# ``skills`` is the agent-to-agent skill catalog (replaces the dropped
# memclaw_share_skill / memclaw_unshare_skill MCP tools). Slugs become
# directory names on plugin-side reconciliation, so doc_id is constrained
# to a filesystem-safe identifier; data["summary"] is embedded so other
# agents can semantic-search the catalog (with a back-compat fallback to
# data["description"] for the skills collection only — see
# core_api.services.doc_indexing).
SKILLS_COLLECTION = "skills"
_SKILL_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,99}$")


# ── Schemas ──


class DocWriteRequest(BaseModel):
    tenant_id: str
    fleet_id: str | None = None
    collection: str = Field(min_length=1, max_length=200)
    doc_id: str = Field(min_length=1, max_length=500)
    data: dict
    # Embed source is no longer caller-chosen. Server reads data["summary"]
    # (and, for collection="skills", falls back to data["description"] for
    # back-compat). See core_api.services.doc_indexing.


class DocQueryRequest(BaseModel):
    tenant_id: str
    fleet_id: str | None = None
    collection: str = Field(min_length=1, max_length=200)
    where: dict = Field(default_factory=dict)
    order_by: str | None = None
    order: str = Field(default="asc", pattern=r"^(asc|desc)$")
    limit: int = Field(default=20, ge=1, le=1000)
    offset: int = Field(default=0, ge=0)


class DocSearchRequest(BaseModel):
    """Vector search over indexed documents.

    Mirrors MCP ``memclaw_doc op=search``: when ``collection`` is omitted,
    search spans every collection in the tenant (broad strategy); when
    supplied, search is restricted to that collection (narrow strategy).
    Only documents written with a ``data["summary"]`` (i.e. with a
    non-NULL embedding column) are considered.
    """

    tenant_id: str
    fleet_id: str | None = None
    collection: str | None = Field(default=None, min_length=1, max_length=200)
    query: str = Field(min_length=1)
    top_k: int = Field(default=5, ge=1, le=50)


class DocOut(BaseModel):
    id: str
    tenant_id: str
    fleet_id: str | None
    collection: str
    doc_id: str
    data: dict
    created_at: datetime
    updated_at: datetime


# ── Helpers ──


def _dict_to_out(d: dict) -> DocOut:
    return DocOut(
        id=str(d.get("id", "")),
        tenant_id=d.get("tenant_id", ""),
        fleet_id=d.get("fleet_id"),
        collection=d.get("collection", ""),
        doc_id=d.get("doc_id", ""),
        data=d.get("data", {}),
        created_at=d.get("created_at", datetime.min),
        updated_at=d.get("updated_at", datetime.min),
    )


# ── Routes ──


@router.post("/documents", response_model=DocOut)
@write_limit
async def upsert_document(
    request: Request,
    body: DocWriteRequest,
    auth: AuthContext = Depends(get_auth_context),
    db: AsyncSession = Depends(get_db),
    idempotency_key: str | None = Header(None, alias=IDEMPOTENCY_HEADER),
):
    """Upsert a document. If collection+doc_id exists, data is replaced."""
    auth.enforce_tenant(body.tenant_id)
    auth.enforce_read_only()
    auth.enforce_usage_limits()
    _idem = await idempotency_for(request, body.tenant_id, idempotency_key)
    if _idem and (_replay := _idem.cached_replay):
        _body, _status = _replay
        return JSONResponse(content=_body, status_code=_status)

    # Skills slug rule — doc_id becomes a directory name on plugin-side
    # reconciliation, so it must be filesystem-safe.
    if body.collection == SKILLS_COLLECTION and not _SKILL_SLUG_RE.fullmatch(body.doc_id):
        raise HTTPException(
            status_code=422,
            detail=(
                f"collection='skills' requires doc_id matching "
                f"{_SKILL_SLUG_RE.pattern} — got {body.doc_id!r}. "
                "Slugs become directory names on each plugin node."
            ),
        )

    if auth.tenant_id:
        await check_and_increment(db, body.tenant_id, "write")

    # Resolve which string in `data` gets embedded. Only data["summary"]
    # is embeddable; skills writes also accept data["description"] for
    # back-compat. See core_api.services.doc_indexing for the contract.
    from core_api.services.doc_indexing import (
        InvalidDocIndexingError,
        resolve_embed_source,
    )

    try:
        source = resolve_embed_source(body.collection, body.data)
    except InvalidDocIndexingError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    embedding: list[float] | None = None
    if source is not None:
        from common.embedding import get_embedding

        embedding = await get_embedding(source)
        if embedding is None:
            raise HTTPException(
                status_code=502,
                detail=(
                    "Embedding provider returned no vector (check provider config / quota). Write aborted."
                ),
            )

    if embedding is not None:
        from core_api.repositories import document_repo

        row = await document_repo.upsert_returning_xmax(
            db,
            tenant_id=body.tenant_id,
            fleet_id=body.fleet_id,
            collection=body.collection,
            doc_id=body.doc_id,
            data=body.data,
            embedding=embedding,
        )
        if row is None:
            raise HTTPException(status_code=500, detail="Document upsert returned no rows")
        # Commit so the storage-api process (which reads from the same
        # Postgres but holds its own connections) sees the row when we
        # re-fetch below. ``log_action`` further down also requires an
        # explicit commit, so we'll commit again at the end — that's
        # fine, the second commit is a no-op for the doc row.
        await db.commit()
        # Re-fetch to build the response shape (upsert_returning_xmax
        # returns a tuple, not a full doc dict).
        sc = get_storage_client()
        doc = await sc.get_document(
            tenant_id=body.tenant_id,
            collection=body.collection,
            doc_id=body.doc_id,
        )
    else:
        sc = get_storage_client()
        doc = await sc.upsert_document(
            {
                "tenant_id": body.tenant_id,
                "fleet_id": body.fleet_id,
                "collection": body.collection,
                "doc_id": body.doc_id,
                "data": body.data,
            }
        )
    if doc is None:
        raise HTTPException(status_code=500, detail="Document upsert returned no rows")
    await log_action(
        db,
        tenant_id=body.tenant_id,
        action="doc_upsert",
        resource_type="document",
        resource_id=doc.get("id"),
        detail={
            "collection": body.collection,
            "doc_id": body.doc_id,
            "indexed": embedding is not None,
        },
    )
    await db.commit()
    out = _dict_to_out(doc)
    if _idem:
        await _idem.record(out.model_dump(mode="json"), 200)
    return out


# NOTE: /documents/collections must be registered BEFORE /documents/{doc_id}
# because FastAPI matches in declaration order — without this ordering,
# `GET /documents/collections` would match `/documents/{doc_id}` with
# doc_id="collections" and require the `collection=` query param, returning 422.
@router.get("/documents/collections")
async def list_collections(
    tenant_id: str = Query(...),
    fleet_id: str | None = Query(default=None),
    auth: AuthContext = Depends(get_auth_context),
    db: AsyncSession = Depends(get_db),
):
    """Enumerate document collections in the tenant. Mirror of MCP
    ``memclaw_doc op=list_collections``. Returns one row per collection
    with the per-collection document count.

    Cross-tenant credentials see collections across every tenant in their
    readable set; counts merge by collection name. Pinning ``tenant_id``
    to a single tenant in the readable set scopes the result to that
    tenant's collections.
    """
    auth.enforce_readable_tenant(tenant_id)
    from core_api.repositories import document_repo

    rows = await document_repo.list_collections(
        db,
        tenant_id=tenant_id,
        fleet_id=fleet_id,
        readable_tenant_ids=(auth.readable_tenant_ids if auth.is_cross_tenant_read else None),
    )
    return JSONResponse(
        {
            "collections": [{"name": name, "count": count} for name, count in rows],
            "count": len(rows),
        }
    )


@router.get("/documents/{doc_id}")
async def get_document(
    doc_id: str,
    tenant_id: str = Query(...),
    collection: str = Query(...),
    auth: AuthContext = Depends(get_auth_context),
):
    """Get a single document by collection + doc_id.

    Cross-tenant credentials may pass any ``tenant_id`` in their readable
    set; the gate widens via ``enforce_readable_tenant``. Single-tenant
    behavior unchanged.
    """
    auth.enforce_readable_tenant(tenant_id)
    sc = get_storage_client()
    doc = await sc.get_document(tenant_id=tenant_id, collection=collection, doc_id=doc_id)
    if not doc:
        raise HTTPException(status_code=404, detail="Document not found")
    return _dict_to_out(doc)


@router.post("/documents/query")
async def query_documents(
    body: DocQueryRequest,
    auth: AuthContext = Depends(get_auth_context),
):
    """Query documents by field equality filters on JSONB data.

    Cross-tenant credentials may pass any tenant in their readable set
    as ``body.tenant_id`` (one-tenant-at-a-time scope; aggregate-across
    widening lives on the direct-DB ``memclaw_doc`` MCP path).
    """
    auth.enforce_readable_tenant(body.tenant_id)

    sc = get_storage_client()
    docs = await sc.query_documents(
        {
            "tenant_id": body.tenant_id,
            "collection": body.collection,
            "fleet_id": body.fleet_id,
            "where": body.where,
            "order_by": body.order_by,
            "order": body.order,
            "limit": body.limit,
            "offset": body.offset,
        }
    )

    return [_dict_to_out(d) for d in docs]


@router.get("/documents")
async def list_documents(
    tenant_id: str = Query(...),
    collection: str = Query(...),
    fleet_id: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=1000),
    offset: int = Query(default=0, ge=0),
    auth: AuthContext = Depends(get_auth_context),
):
    """List all documents in a collection.

    Cross-tenant credentials may pass any tenant in their readable set
    (one-tenant-at-a-time; the aggregate ``list_collections`` view widens).
    """
    auth.enforce_readable_tenant(tenant_id)
    sc = get_storage_client()
    docs = await sc.list_documents(
        tenant_id=tenant_id, collection=collection, fleet_id=fleet_id, limit=limit, offset=offset
    )
    return [_dict_to_out(d) for d in docs]


@router.delete("/documents/{doc_id}", status_code=204)
async def delete_document(
    doc_id: str,
    tenant_id: str = Query(...),
    collection: str = Query(...),
    auth: AuthContext = Depends(get_auth_context),
    db: AsyncSession = Depends(get_db),
):
    """Delete a document by collection + doc_id."""
    auth.enforce_tenant(tenant_id)
    auth.enforce_read_only()
    sc = get_storage_client()
    deleted = await sc.delete_document(tenant_id=tenant_id, collection=collection, doc_id=doc_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Document not found")
    await log_action(
        db,
        tenant_id=tenant_id,
        action="doc_delete",
        resource_type="document",
        detail={"collection": collection, "doc_id": doc_id},
    )
    await db.commit()


# ── Vector search + collections enumeration ──
#
# These two endpoints mirror MCP ``memclaw_doc op=search`` and
# ``op=list_collections``. They go DIRECTLY through ``document_repo``
# (bypassing the storage-api HTTP hop) for two reasons:
#   1. The MCP path already does, so behaviour is identical.
#   2. The corresponding storage-api routes don't exist — adding them
#      would just be an extra hop with no functional value.
# See docs/api-surfaces.md for surface ownership rationale.


@router.post("/documents/search")
async def search_documents(
    body: DocSearchRequest,
    auth: AuthContext = Depends(get_auth_context),
    db: AsyncSession = Depends(get_db),
):
    """Vector search over indexed documents. Mirror of MCP ``memclaw_doc op=search``.

    Embeds ``body.query`` via the configured embedding provider, then ranks
    documents by cosine similarity. ``collection=None`` searches across all
    collections in the tenant; supplying ``collection`` scopes the search.
    """
    auth.enforce_readable_tenant(body.tenant_id)
    if auth.tenant_id:
        # Rate-limit against the home tenant (not every tenant in the
        # readable set) — mirrors recall's pattern. The home tenant pays
        # the search-budget cost for the widened query.
        await check_and_increment(db, auth.tenant_id, "search")

    from common.embedding import get_embedding
    from core_api.repositories import document_repo

    query_embedding = await get_embedding(body.query)
    if query_embedding is None:
        raise HTTPException(
            status_code=503,
            detail=("embedding provider returned no vector (check provider config / quota); search aborted"),
        )
    pairs = await document_repo.search(
        db,
        tenant_id=body.tenant_id,
        collection=body.collection,
        query_embedding=query_embedding,
        top_k=body.top_k,
        fleet_id=body.fleet_id,
        readable_tenant_ids=(auth.readable_tenant_ids if auth.is_cross_tenant_read else None),
    )
    items = [
        {
            "collection": d.collection,
            "doc_id": d.doc_id,
            "data": d.data,
            "similarity": round(sim, 4),
        }
        for d, sim in pairs
    ]
    source_tenants = auth.source_tenants_for_audit()
    if source_tenants and auth.is_cross_tenant_read:
        counts: dict[str, int] = {}
        for d, _sim in pairs:
            rt = getattr(d, "tenant_id", None)
            if rt:
                counts[rt] = counts.get(rt, 0) + 1
        await log_cross_tenant_read(
            db,
            home_tenant_id=auth.tenant_id,
            home_agent_id=auth.agent_id,
            source_tenants=source_tenants,
            surface="rest_documents_search",
            result_count_by_tenant=counts,
            query_summary=(body.query or "")[:200],
        )
    return JSONResponse(
        {
            "collection": body.collection,
            "count": len(items),
            "results": items,
        }
    )


# /documents/collections is registered earlier in the file (before
# /documents/{doc_id}) to avoid the path-parameter collision.
