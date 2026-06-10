"""Repository for documents table queries."""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import delete, func, select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from common.models.document import Document


class DocumentRepository:
    """Single point of DB access for Document rows."""

    async def upsert(
        self,
        db: AsyncSession,
        *,
        tenant_id: str,
        collection: str,
        doc_id: str,
        data: dict,
        fleet_id: str | None = None,
    ) -> Document:
        """INSERT ... ON CONFLICT DO UPDATE. Returns the upserted Document."""
        stmt = (
            pg_insert(Document)
            .values(
                tenant_id=tenant_id,
                fleet_id=fleet_id,
                collection=collection,
                doc_id=doc_id,
                data=data,
            )
            .on_conflict_do_update(
                constraint="uq_documents_tenant_collection_doc",
                set_={
                    "data": data,
                    "fleet_id": fleet_id,
                    "updated_at": datetime.now(UTC),
                },
            )
            .returning(Document)
        )
        result = await db.execute(stmt)
        return result.scalar_one()

    async def upsert_returning_xmax(
        self,
        db: AsyncSession,
        *,
        tenant_id: str,
        collection: str,
        doc_id: str,
        data: dict,
        fleet_id: str | None = None,
        embedding: list[float] | None = None,
    ):
        """Upsert and return (id, created_at, updated_at, xmax) for MCP callers.

        ``embedding`` is opt-in — callers that skip it leave the column
        ``NULL`` and the doc won't participate in semantic search. Upsert
        always writes the embedding column, so passing ``None`` on a
        re-write will clear a previously-indexed doc (intentional — the
        caller chose not to index this version).
        """
        stmt = (
            pg_insert(Document)
            .values(
                tenant_id=tenant_id,
                fleet_id=fleet_id,
                collection=collection,
                doc_id=doc_id,
                data=data,
                embedding=embedding,
            )
            .on_conflict_do_update(
                constraint="uq_documents_tenant_collection_doc",
                set_={
                    "data": data,
                    "fleet_id": fleet_id,
                    "embedding": embedding,
                    "updated_at": text("now()"),
                },
            )
            .returning(Document.id, Document.created_at, Document.updated_at, text("xmax"))
        )
        result = await db.execute(stmt)
        return result.one()

    async def get_by_doc_id(
        self,
        db: AsyncSession,
        *,
        tenant_id: str,
        collection: str,
        doc_id: str,
        readable_tenant_ids: list[str] | None = None,
    ) -> Document | None:
        """Get a single document by (tenant_id, collection, doc_id).

        ``readable_tenant_ids`` widens the tenant predicate to
        ``ANY($readable)`` so cross-tenant credentials can fetch docs
        from sibling tenants. ``tenant_id`` is still required as the
        binding/home tenant.
        """
        if readable_tenant_ids:
            tenant_pred = Document.tenant_id.in_(readable_tenant_ids)
        else:
            tenant_pred = Document.tenant_id == tenant_id
        stmt = select(Document).where(
            tenant_pred,
            Document.collection == collection,
            Document.doc_id == doc_id,
        )
        result = await db.execute(stmt)
        return result.scalar_one_or_none()

    async def query(
        self,
        db: AsyncSession,
        *,
        tenant_id: str,
        collection: str,
        fleet_id: str | None = None,
        where: dict | None = None,
        order_by: str | None = None,
        order: str = "asc",
        limit: int = 20,
        offset: int = 0,
        readable_tenant_ids: list[str] | None = None,
    ) -> list[Document]:
        """Query documents with optional JSONB field-equality filters.

        ``readable_tenant_ids`` widens ``tenant_id`` to ``ANY($readable)``
        for cross-tenant credentials.
        """
        if readable_tenant_ids:
            tenant_pred = Document.tenant_id.in_(readable_tenant_ids)
        else:
            tenant_pred = Document.tenant_id == tenant_id
        stmt = select(Document).where(
            tenant_pred,
            Document.collection == collection,
        )
        if fleet_id:
            stmt = stmt.where(Document.fleet_id == fleet_id)

        for key, value in (where or {}).items():
            if isinstance(value, bool):
                stmt = stmt.where(Document.data[key].as_boolean() == value)
            elif isinstance(value, (int, float)):
                stmt = stmt.where(Document.data[key].as_float() == value)
            else:
                stmt = stmt.where(Document.data[key].astext == str(value))

        if order_by:
            col = Document.data[order_by].astext
            stmt = stmt.order_by(col.desc() if order == "desc" else col.asc())
        else:
            stmt = stmt.order_by(Document.updated_at.desc())

        stmt = stmt.offset(offset).limit(limit)
        result = await db.execute(stmt)
        return list(result.scalars().all())

    async def list_by_collection(
        self,
        db: AsyncSession,
        *,
        tenant_id: str,
        collection: str,
        fleet_id: str | None = None,
        limit: int = 50,
        offset: int = 0,
        readable_tenant_ids: list[str] | None = None,
    ) -> list[Document]:
        if readable_tenant_ids:
            tenant_pred = Document.tenant_id.in_(readable_tenant_ids)
        else:
            tenant_pred = Document.tenant_id == tenant_id
        stmt = select(Document).where(
            tenant_pred,
            Document.collection == collection,
        )
        if fleet_id:
            stmt = stmt.where(Document.fleet_id == fleet_id)
        stmt = stmt.order_by(Document.updated_at.desc()).offset(offset).limit(limit)
        result = await db.execute(stmt)
        return list(result.scalars().all())

    async def list_collections(
        self,
        db: AsyncSession,
        *,
        tenant_id: str,
        fleet_id: str | None = None,
        readable_tenant_ids: list[str] | None = None,
    ) -> list[tuple[str, int]]:
        """Enumerate collections a tenant has written to, with per-collection
        document counts.

        Returns rows of ``(collection, count)`` sorted alphabetically by
        collection name. If ``fleet_id`` is supplied, only documents matching
        that fleet are counted; otherwise counts span every fleet within the
        tenant.

        ``readable_tenant_ids`` widens to ``ANY($readable)`` — counts then
        span every collection across the readable set (collections with the
        same name across multiple tenants merge into one row).

        This is the discovery primitive for ``memclaw_doc op=list_collections``
        — clients use it when they do not yet know which collections exist.
        """
        if readable_tenant_ids:
            tenant_pred = Document.tenant_id.in_(readable_tenant_ids)
        else:
            tenant_pred = Document.tenant_id == tenant_id
        stmt = (
            select(Document.collection, func.count().label("count"))
            .where(tenant_pred)
            .group_by(Document.collection)
            .order_by(Document.collection)
        )
        if fleet_id:
            stmt = stmt.where(Document.fleet_id == fleet_id)
        result = await db.execute(stmt)
        return [(row.collection, int(row.count)) for row in result.all()]

    async def search(
        self,
        db: AsyncSession,
        *,
        tenant_id: str,
        query_embedding: list[float],
        collection: str | None = None,
        top_k: int = 5,
        fleet_id: str | None = None,
        readable_tenant_ids: list[str] | None = None,
        status: str | None = None,
    ) -> list[tuple[Document, float]]:
        """Semantic search over docs — scoped or cross-collection.

        If ``collection`` is supplied, search is restricted to that
        collection (narrow / strategy 1). If ``collection`` is ``None``,
        search spans every collection in the tenant (broad / strategy 2).
        Only rows with ``embedding IS NOT NULL`` are considered — a doc
        written without ``data["summary"]`` is invisible either way.

        ``readable_tenant_ids`` widens to ``ANY($readable)`` — semantic
        search then spans every document across the readable set, sorted
        by global cosine distance.

        ``status`` (optional) adds a ``data->>'status' = :status``
        equality filter — used by the MCP agent surface to restrict
        skill discovery to ``status='active'``. Pure mechanism: the
        caller decides the policy (which collection, when to apply).
        Left ``None`` by the REST search path, so that path is
        unaffected.

        Orders by cosine distance against ``query_embedding`` using the
        partial HNSW index from migration 003. Returns ``(Document,
        similarity)`` pairs where ``similarity = 1 - cosine_distance``
        (1.0 = identical, 0.0 = orthogonal, slightly negative values
        are legal for near-orthogonal high-dim vectors).
        """
        if readable_tenant_ids:
            tenant_pred = Document.tenant_id.in_(readable_tenant_ids)
        else:
            tenant_pred = Document.tenant_id == tenant_id
        distance = Document.embedding.cosine_distance(query_embedding)
        stmt = (
            select(Document, distance.label("distance"))
            .where(
                tenant_pred,
                Document.embedding.is_not(None),
            )
            .order_by(distance)
            .limit(max(top_k, 1))
        )
        if collection is not None:
            stmt = stmt.where(Document.collection == collection)
        if fleet_id:
            stmt = stmt.where(Document.fleet_id == fleet_id)
        if status is not None:
            stmt = stmt.where(Document.data["status"].astext == status)
        result = await db.execute(stmt)
        return [(row.Document, 1.0 - float(row.distance)) for row in result.all()]

    async def delete_by_doc_id(
        self,
        db: AsyncSession,
        *,
        tenant_id: str,
        collection: str,
        doc_id: str,
    ):
        """Delete by (tenant_id, collection, doc_id). Returns the deleted id or None."""
        stmt = (
            delete(Document)
            .where(
                Document.tenant_id == tenant_id,
                Document.collection == collection,
                Document.doc_id == doc_id,
            )
            .returning(Document.id)
        )
        result = await db.execute(stmt)
        return result.scalar_one_or_none()
