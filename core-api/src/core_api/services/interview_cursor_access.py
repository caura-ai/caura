"""Credential authority for interview cursors, shared by REST and MCP reads."""

from dataclasses import dataclass
from typing import Any

from fastapi import HTTPException

from core_api.agent_ids import canonical_service_agent_id
from core_api.auth import CAURA_KEY_CREDENTIAL, AuthContext
from core_api.services.interview_service import (
    JOBS_COLLECTION,
    SKILL_DELIVERY_COLLECTION,
    WATERMARK_COLLECTION,
)


def _field(row: Any, name: str) -> Any:
    return row.get(name) if isinstance(row, dict) else getattr(row, name, None)


@dataclass(frozen=True)
class CursorReadAccess:
    tenant_id: str
    # None is explicit tenant-admin authority, never an unverified identity.
    owner_agent_id: str | None

    def allows(self, row: Any) -> bool:
        if _field(row, "tenant_id") != self.tenant_id:
            return False
        if self.owner_agent_id is None:
            return True
        data = _field(row, "data")
        owner = data.get("agent_id") if isinstance(data, dict) else None
        return isinstance(owner, str) and canonical_service_agent_id(owner) == self.owner_agent_id

    def require(self, row: Any) -> None:
        if not self.allows(row):
            raise HTTPException(403, "This credential cannot read this interview stream")

    def query_where(self, where: dict | None) -> dict | None:
        """None means a contradictory caller filter; ownership always wins."""
        effective = dict(where or {})
        if self.owner_agent_id is not None:
            if "agent_id" in effective and effective["agent_id"] != self.owner_agent_id:
                return None
            effective["agent_id"] = self.owner_agent_id
        return effective


def cursor_read_access(auth: AuthContext | None, tenant_id: str | None) -> CursorReadAccess:
    if auth is None:
        raise HTTPException(403, "Authenticated cursor authority is required")
    if tenant_id is None:
        tenant_id = auth.tenant_id
    if not tenant_id:
        raise HTTPException(400, "tenant_id required")
    # Cross-tenant read grants do not widen interview ownership.
    AuthContext.enforce_tenant(auth, tenant_id)
    if auth.is_admin:
        return CursorReadAccess(tenant_id, None)
    # The configured shared deployment key is an explicit tenant-admin path.
    # Its caller-supplied agent label does not narrow or establish authority.
    if auth.credential_id == CAURA_KEY_CREDENTIAL:
        return CursorReadAccess(tenant_id, None)
    if not auth.is_install_credential and auth.agent_id_verified and auth.agent_id:
        return CursorReadAccess(tenant_id, canonical_service_agent_id(auth.agent_id))
    if (
        not auth.is_install_credential
        and auth.agent_id is None
        and auth.credential_kind != "agent"
        and auth.is_org_admin
    ):
        return CursorReadAccess(tenant_id, None)
    raise HTTPException(403, "An owning agent or tenant-admin credential is required")


def visible_cursor_search_rows(rows: list, auth: AuthContext | None, tenant_id: str) -> list:
    """Filter a ranked page; storage already applied top_k before ownership.

    Owned watermarks can be crowded out of that page. This protects reads but
    does not provide complete stream enumeration; use the query/read paths.
    """
    try:
        access = cursor_read_access(auth, tenant_id)
    except HTTPException:
        access = None
    return [
        row
        for row in rows
        if _field(row, "collection") != WATERMARK_COLLECTION or (access is not None and access.allows(row))
    ]


def visible_document_collections(rows: list[dict]) -> list[dict]:
    return [
        row
        for row in rows
        if row.get("name") not in {JOBS_COLLECTION, WATERMARK_COLLECTION, SKILL_DELIVERY_COLLECTION}
    ]
