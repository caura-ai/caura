"""Versioned, administrator-approved delivery to credential-bound fleet nodes."""

from __future__ import annotations

import hashlib
import json
import logging
import re
from datetime import UTC, datetime
from typing import Literal
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator
from starlette.responses import JSONResponse

from common.constants import NODE_PRINCIPAL_TENANT
from core_api.auth import CAURA_KEY_CREDENTIAL, AuthContext, get_auth_context
from core_api.clients.storage_client import get_storage_client
from core_api.routes.fleet import _node_principal
from core_api.schemas import TenantScopedBody

logger = logging.getLogger(__name__)
router = APIRouter(tags=["Skills"])
GRANTS_COLLECTION = "_skill_delivery_grants"
# Wire normalization shared with the plugin. Runtime-default Unicode trimming
# differs between Python and JavaScript and must not alter artifact bytes.
METADATA_WHITESPACE = " \t\r\n"
FILE_MAX = 65_536
RESPONSE_MAX = 4_194_304
# Storage's single-page ceiling. A saturated page is an error, never a partial
# desired set. Avoid offset pagination across independently changing snapshots.
CATALOG_PAGE = 5000
APPROVAL_BATCH = 64
SLUG = re.compile(r"^(?:(?:forge|agent)/)?([a-z0-9][a-z0-9._-]{0,99})$")


class ApprovalRequest(TenantScopedBody):
    model_config = ConfigDict(extra="forbid")
    expected_artifact_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    node_ids: list[UUID] = Field(min_length=1, max_length=256)


class InstallableSkillsRequest(TenantScopedBody):
    node_id: UUID
    delivery_version: Literal[1]


class Recipient(BaseModel):
    node_id: UUID
    owner_principal: str


class DeliveryApproval(BaseModel):
    grant_id: UUID
    skill_doc_id: str
    artifact_hash: str
    source_hash: str
    bytes: int
    recipients: list[Recipient]
    approved_by: str
    approved_at: datetime
    delivery_version: Literal[1]


class ArtifactBinding(BaseModel):
    grant_id: UUID
    artifact_hash: str
    bytes: int


class DeliveryDocument(BaseModel):
    doc_id: str
    data: dict[str, str]
    delivery: ArtifactBinding


class HeldSkill(BaseModel):
    doc_id: str
    reason: Literal["approval_required"]


class DeliveryManifest(BaseModel):
    delivery_version: Literal[1]
    node_id: UUID
    complete: Literal[True]
    prune_allowed: bool
    documents: list[DeliveryDocument]
    held: list[HeldSkill]


class InstallationEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")
    event_id: UUID
    skill_doc_id: str = Field(max_length=106, pattern=SLUG.pattern)
    grant_id: UUID
    artifact_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    outcome: Literal["installed", "updated", "removed"]
    target_id: UUID
    occurred_at: AwareDatetime


class InstallationResults(TenantScopedBody):
    model_config = ConfigDict(extra="forbid")
    reporting_version: Literal[1]
    node_id: UUID
    events: list[InstallationEvent] = Field(min_length=1, max_length=64)

    @model_validator(mode="after")
    def _unique_events(self):
        if len({event.event_id for event in self.events}) != len(self.events):
            raise ValueError("Event IDs must be unique within a batch")
        return self


class InstallationAcceptance(BaseModel):
    reporting_version: Literal[1]
    node_id: UUID
    accepted_event_ids: list[UUID]


def _tenant(auth: AuthContext, requested: str | None) -> str:
    tenant = requested or auth.tenant_id
    if not tenant:
        raise HTTPException(422, "tenant_id required")
    auth.enforce_tenant(tenant)
    return tenant


def _hash(value: str) -> str:
    return "sha256:" + hashlib.sha256(value.encode("utf-8")).hexdigest()


def _artifact(doc: dict) -> tuple[dict, str, str, int]:
    data = doc.get("data") or {}
    content, name, description = (data.get(k) for k in ("content", "name", "description"))
    match = SLUG.fullmatch(doc.get("doc_id", ""))
    if not match or not isinstance(content, str) or not content or not isinstance(description, str):
        raise HTTPException(422, "Skill needs a safe catalog ID, content and description")
    name = (
        name.strip(METADATA_WHITESPACE)
        if isinstance(name, str) and name.strip(METADATA_WHITESPACE)
        else match[1]
    )
    description = description.strip(METADATA_WHITESPACE)
    if not description:
        raise HTTPException(422, "Skill description required")
    # Bound inputs before producing an additional rendered copy. Metadata is
    # bound independently too, including when content supplies its own YAML.
    try:
        if any(len(v.encode("utf-8")) > FILE_MAX for v in (content, name, description)):
            raise HTTPException(413, "Rendered skill exceeds 64 KiB")
        rendered = (
            content
            if re.match(r"^---\r?\n", content)
            else (
                "---\nname: "
                + json.dumps(name, ensure_ascii=False)
                + "\ndescription: "
                + json.dumps(description, ensure_ascii=False)
                + "\n---\n\n"
                + content
            )
        )
        size = len(rendered.encode("utf-8"))
        if size > FILE_MAX:
            raise HTTPException(413, "Rendered skill exceeds 64 KiB")
        source_hash = _hash(
            json.dumps(
                {k: data.get(k) for k in ("content", "name", "description")},
                ensure_ascii=False,
                sort_keys=True,
            )
        )
        artifact_hash = _hash(rendered)
    except UnicodeError as exc:
        raise HTTPException(422, "Skill must be valid UTF-8") from exc
    return {"name": name, "description": description, "content": content}, artifact_hash, source_hash, size


def _pointer(doc_id: str) -> str:
    return "current/" + hashlib.sha256(doc_id.encode()).hexdigest()


async def _node(sc, tenant: str, node_id: str) -> dict:
    node = await sc.get_node_binding(tenant, node_id)
    if not node or str(node.get("tenant_id")) != tenant:
        raise HTTPException(404, "Receiver not found")
    if not node.get("owner_principal"):
        raise HTTPException(409, "Receiver needs a credential binding")
    return node


def _receiver(auth: AuthContext, node: dict) -> None:
    principal = _node_principal(auth)
    if principal is None:
        # Explicit tenant-wide authority, not a fallback from an asserted agent
        # label or a cross-tenant read grant. Narrow credentials match exactly.
        tenant_admin = auth.is_org_admin or auth.credential_id == CAURA_KEY_CREDENTIAL
        tenant_key = (
            not auth.is_person
            and not auth.agent_id
            and auth.credential_kind in (None, "tenant", "tenant_key")
        )
        if (
            auth.is_cross_tenant_read
            or auth.credential_kind == "cross_tenant"
            or not (tenant_admin or tenant_key)
        ):
            raise HTTPException(403, "Receiver credential required")
        principal = NODE_PRINCIPAL_TENANT
    if principal != node["owner_principal"]:
        raise HTTPException(403, "Receiver is bound to another credential")


@router.post("/skills/installations", response_model=InstallationAcceptance)
async def record_installations(
    body: InstallationResults,
    auth: AuthContext = Depends(get_auth_context),
):
    """Persist authenticated node reports; this is not filesystem attestation."""
    tenant = _tenant(auth, body.tenant_id)
    auth.enforce_read_only()
    auth.enforce_usage_limits()
    sc = get_storage_client()
    node_id = str(body.node_id)
    node = await _node(sc, tenant, node_id)
    _receiver(auth, node)
    owner = node["owner_principal"]
    recipient = {"node_id": node_id, "owner_principal": owner}
    # A delayed report remains meaningful after the current pointer changes.
    # Resolve only the exact immutable versions, on the primary; never infer
    # provenance from the current skill, fleet labels or a supplied principal.
    versions: dict[str, dict] = {}
    for event in body.events:
        grant_id = str(event.grant_id)
        if grant_id not in versions:
            try:
                row = await sc.get_document(tenant, GRANTS_COLLECTION, grant_id, read=False)
            except Exception as exc:
                raise HTTPException(503, "Approval storage unavailable; retry the same event IDs") from exc
            data = row.get("data") if isinstance(row, dict) else None
            if (
                not isinstance(row, dict)
                or row.get("tenant_id") != tenant
                or row.get("collection") != GRANTS_COLLECTION
                or row.get("doc_id") != grant_id
                or not isinstance(data, dict)
                or data.get("record_type") == "current"
            ):
                raise HTTPException(409, "An immutable delivery approval is required")
            versions[grant_id] = data
        grant = versions[grant_id]
        recipients = grant.get("recipients")
        if (
            grant.get("delivery_version") != 1
            or grant.get("grant_id") != grant_id
            or grant.get("skill_doc_id") != event.skill_doc_id
            or grant.get("artifact_hash") != event.artifact_hash
            or not isinstance(recipients, list)
            or recipient not in recipients
        ):
            raise HTTPException(409, "Installation result does not match the approved receiver and artifact")

    actor = _node_principal(auth)
    if actor is None:
        actor = f"user:{auth.user_id}" if auth.user_id else f"credential:{auth.credential_id or 'tenant'}"
    received_at = datetime.now(UTC).isoformat()
    events = [
        {
            "tenant_id": tenant,
            "agent_id": str(auth.agent_id) if auth.agent_id_verified and auth.agent_id else None,
            "action": "skill.installation",
            "resource_type": "skill",
            "resource_id": str(event.grant_id),
            # The caller cannot collide with another node's reports or with
            # IDs from other writers of the existing per-tenant event chain.
            "client_event_id": "skill-installation:"
            + _hash(json.dumps([tenant, node_id, owner, str(event.event_id)])).removeprefix("sha256:"),
            "detail": {
                **event.model_dump(mode="json"),
                "node_id": node_id,
                "owner_principal": owner,
                "reported_by": actor,
                "received_at": received_at,
                "source": "node_reported",
            },
        }
        for event in body.events
    ]
    try:
        # Synchronous storage acceptance, with the storage chain's existing
        # stable-ID deduplication. Queue acceptance is not an acknowledgment.
        result = await sc.create_audit_logs_bulk(events)
        if not isinstance(result, dict) or result.get("ok") is not True or result.get("count") != len(events):
            raise RuntimeError("Installation storage did not acknowledge the complete batch")
    except Exception as exc:
        raise HTTPException(503, "Installation storage unavailable; retry the same event IDs") from exc
    return {
        "reporting_version": 1,
        "node_id": node_id,
        "accepted_event_ids": [str(event.event_id) for event in body.events],
    }


@router.post("/skills/{doc_id:path}/delivery", responses={200: {"model": DeliveryApproval}})
async def approve_delivery(
    doc_id: str,
    body: ApprovalRequest,
    auth: AuthContext = Depends(get_auth_context),
):
    tenant = _tenant(auth, body.tenant_id)
    auth.enforce_not_agent_credential("approve skill delivery")
    auth.enforce_read_only()
    auth.enforce_tenant(tenant)
    auth.enforce_tenant_admin_credential("approve skill delivery")
    if (
        not auth.is_org_admin
        or auth.is_install_credential
        or auth.credential_kind == "agent"
        or auth.agent_id
    ):
        raise HTTPException(403, "An organization or system administrator must approve delivery")
    approver = auth.user_id or (auth.credential_id if auth.is_admin else None)
    if not approver:
        raise HTTPException(403, "An attributable administrator is required")
    sc = get_storage_client()
    doc = await sc.get_document(tenant, "skills", doc_id, read=False)
    if not doc or doc.get("tenant_id") != tenant or doc.get("data", {}).get("status") != "active":
        raise HTTPException(409, "An active skill is required")
    _, artifact_hash, source_hash, size = _artifact(doc)
    if artifact_hash != body.expected_artifact_hash:
        raise HTTPException(409, "Skill version changed; review the current artifact")
    recipients = []
    for node_id in sorted({str(n) for n in body.node_ids}):
        node = await _node(sc, tenant, node_id)
        recipients.append({"node_id": node_id, "owner_principal": node["owner_principal"]})
    grant = {
        "grant_id": str(uuid4()),
        "skill_doc_id": doc_id,
        "artifact_hash": artifact_hash,
        "source_hash": source_hash,
        "bytes": size,
        "recipients": recipients,
        "approved_by": approver,
        "approved_at": datetime.now(UTC).isoformat(),
        "delivery_version": 1,
    }
    # Keep immutable versions for installation provenance. Publish the pointer
    # last: a failed publication cannot expose a partly written approval.
    await sc.upsert_skill_delivery_record(
        {"tenant_id": tenant, "collection": GRANTS_COLLECTION, "doc_id": grant["grant_id"], "data": grant}
    )
    await sc.upsert_skill_delivery_record(
        {
            "tenant_id": tenant,
            "collection": GRANTS_COLLECTION,
            "doc_id": _pointer(doc_id),
            "data": {**grant, "record_type": "current"},
        }
    )
    return grant


async def installable_skills(body: InstallableSkillsRequest, auth: AuthContext):
    tenant = _tenant(auth, body.tenant_id)
    sc = get_storage_client()
    node_id = str(body.node_id)
    node = await _node(sc, tenant, node_id)
    _receiver(auth, node)
    rows = await sc.query_documents(
        {"tenant_id": tenant, "collection": "skills", "where": {"status": "active"}, "limit": CATALOG_PAGE},
        read=False,
    )
    if len(rows) >= CATALOG_PAGE:
        raise HTTPException(413, "Catalog capacity exceeded; no complete manifest available")
    valid_rows = []
    prune_allowed = True
    for doc in rows:
        if doc.get("tenant_id") != tenant:
            raise HTTPException(409, "Catalog contains a foreign skill")
        doc_id = doc.get("doc_id")
        if not isinstance(doc_id, str) or not SLUG.fullmatch(doc_id):
            logger.warning("Skipping unsafe skill identity tenant=%s doc_id=%r", tenant, doc_id)
            # Its legacy disk identity is unknown. Continue approved delivery,
            # but do not infer removal of any installed copy from this catalog.
            prune_allowed = False
            continue
        valid_rows.append(doc)
    pointers = list(dict.fromkeys(_pointer(doc["doc_id"]) for doc in valid_rows))
    current: dict[str, dict] = {}
    for start in range(0, len(pointers), APPROVAL_BATCH):
        batch = pointers[start : start + APPROVAL_BATCH]
        approvals = await sc.read_skill_delivery_records(
            tenant_id=tenant,
            doc_ids=batch,
            node_id=node_id,
            owner_principal=node["owner_principal"],
        )
        for record in approvals:
            if record.get("tenant_id") != tenant or record.get("doc_id") not in batch:
                raise HTTPException(409, "Approval lookup returned an unexpected record")
            current[record["doc_id"]] = record.get("data", {})
    result = {
        "delivery_version": 1,
        "node_id": node_id,
        "complete": True,
        "prune_allowed": prune_allowed,
        "documents": [],
        "held": [],
    }
    documents: list[dict] = []
    held: list[dict] = []
    # Count the exact wire encoding incrementally; never return a successful
    # prefix when either the storage page or response budget is exhausted.
    size = len(JSONResponse(result).body)
    for doc in valid_rows:
        doc_id = doc["doc_id"]
        entry = None
        grant = current.get(_pointer(doc_id), {})
        grant_id = grant.get("grant_id")
        if isinstance(grant_id, str):
            try:
                data, artifact_hash, source_hash, file_size = _artifact(doc)
            except HTTPException:
                pass  # Invalid/oversized legacy bytes are held, never shipped.
            else:
                recipient = {"node_id": node_id, "owner_principal": node["owner_principal"]}
                if (
                    grant.get("delivery_version") == 1
                    and grant.get("skill_doc_id") == doc_id
                    and grant.get("grant_id") == grant_id
                    and grant.get("artifact_hash") == artifact_hash
                    and grant.get("source_hash") == source_hash
                    and recipient in grant.get("recipients", [])
                ):
                    entry = {
                        "doc_id": doc_id,
                        "data": data,
                        "delivery": {
                            "grant_id": grant_id,
                            "artifact_hash": artifact_hash,
                            "bytes": file_size,
                        },
                    }
        value = entry or {"doc_id": doc_id, "reason": "approval_required"}
        bucket = documents if entry else held
        size += len(JSONResponse(value).body) + (1 if bucket else 0)
        if size > RESPONSE_MAX:
            raise HTTPException(413, "Complete skill manifest exceeds 4 MiB")
        bucket.append(value)
    result["documents"], result["held"] = documents, held
    return JSONResponse(result)
