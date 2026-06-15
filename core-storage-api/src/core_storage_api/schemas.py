"""Request schemas and ORM-to-dict helpers for core-storage-api."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel

# ---------------------------------------------------------------------------
# ORM → dict helper
# ---------------------------------------------------------------------------


def _serialise_value(val: Any) -> Any:
    """Recursively convert non-JSON-native types in nested structures."""
    if val is None:
        return None
    if isinstance(val, uuid.UUID):
        return str(val)
    if isinstance(val, datetime):
        return val.isoformat()
    if hasattr(val, "tolist"):
        return val.tolist()
    if isinstance(val, dict):
        return {k: _serialise_value(v) for k, v in val.items()}
    if isinstance(val, (list, tuple)):
        return [_serialise_value(v) for v in val]
    return val


def orm_to_dict(obj: Any, fields: list[str]) -> dict[str, Any]:
    """Convert a SQLAlchemy ORM instance to a plain dict.

    Type coercions applied per-value (recursively for dicts/lists):
    - UUID          → str
    - datetime      → isoformat string
    - pgvector      → list[float]
    - None          → None (passthrough)
    - everything else passes through unchanged
    """
    result: dict[str, Any] = {}
    for f in fields:
        result[f] = _serialise_value(getattr(obj, f, None))
    return result


# ---------------------------------------------------------------------------
# Field lists — one per model, consumed by orm_to_dict callers
# ---------------------------------------------------------------------------

MEMORY_FIELDS: list[str] = [
    "id",
    "tenant_id",
    "fleet_id",
    "agent_id",
    "memory_type",
    "content",
    "embedding",
    "weight",
    "source_uri",
    "run_id",
    "metadata_",
    "created_at",
    "title",
    "content_hash",
    "client_request_id",
    "expires_at",
    "deleted_at",
    "search_vector",
    "subject_entity_id",
    "predicate",
    "object_value",
    "ts_valid_start",
    "ts_valid_end",
    "status",
    "visibility",
    "recall_count",
    "last_recalled_at",
    "last_dedup_checked_at",
    "supersedes_id",
]

ENTITY_FIELDS: list[str] = [
    "id",
    "tenant_id",
    "fleet_id",
    "canonical_name",
    "entity_type",
    "attributes",
]

RELATION_FIELDS: list[str] = [
    "id",
    "tenant_id",
    "fleet_id",
    "from_entity_id",
    "to_entity_id",
    "relation_type",
    "weight",
    "evidence_memory_id",
]

AGENT_FIELDS: list[str] = [
    "id",
    "tenant_id",
    "fleet_id",
    "agent_id",
    "display_name",
    "install_id",
    "trust_level",
    "search_profile",
    "created_at",
    "updated_at",
]

DOCUMENT_FIELDS: list[str] = [
    "id",
    "tenant_id",
    "fleet_id",
    "collection",
    "doc_id",
    "data",
    "created_at",
    "updated_at",
]

IDEMPOTENCY_RESPONSE_FIELDS: list[str] = [
    "tenant_id",
    "idempotency_key",
    "request_hash",
    "response_body",
    "status_code",
    "created_at",
    "expires_at",
    "is_pending",
]

FLEET_NODE_FIELDS: list[str] = [
    "id",
    "tenant_id",
    "fleet_id",
    "node_name",
    "hostname",
    "ip",
    "openclaw_version",
    "plugin_version",
    "plugin_hash",
    "os_info",
    "agents_json",
    "tools_json",
    "channels_json",
    "extra",
    "last_heartbeat",
    "created_at",
]

FLEET_COMMAND_FIELDS: list[str] = [
    "id",
    "tenant_id",
    "node_id",
    "command",
    "payload",
    "status",
    "result",
    "created_at",
    "acked_at",
    "completed_at",
]

MEMORY_ENTITY_LINK_FIELDS: list[str] = [
    "memory_id",
    "entity_id",
    "role",
]

AUDIT_LOG_FIELDS: list[str] = [
    "id",
    "tenant_id",
    "fleet_id",
    "agent_id",
    "action",
    "resource_type",
    "resource_id",
    "detail",
    "created_at",
    # Chain seq surfaces in list responses (JSON-safe int); the raw
    # ``prev_hash``/``event_hash`` bytes are intentionally NOT listed
    # here — the /verify endpoint hex-encodes them in its own response.
    "seq",
]

REPORT_FIELDS: list[str] = [
    "id",
    "tenant_id",
    "fleet_id",
    "trigger",
    "status",
    "started_at",
    "completed_at",
    "duration_ms",
    "summary",
    "hygiene",
    "health",
    "usage_data",
    "issues",
    "crystallization",
]

BACKGROUND_TASK_FIELDS: list[str] = [
    "id",
    "tenant_id",
    "task_type",
    "error_message",
    "created_at",
]


# ---------------------------------------------------------------------------
# Pydantic request schemas for complex query endpoints
# ---------------------------------------------------------------------------


class ScoredSearchRequest(BaseModel):
    tenant_id: str
    embedding: list[float]
    query: str
    fleet_ids: list[str] | None = None
    caller_agent_id: str | None = None
    filter_agent_id: str | None = None
    memory_type_filter: str | None = None
    status_filter: str | None = None
    valid_at: str | None = None
    boosted_memory_ids: list[str] | None = None
    memory_boost_factor: dict[str, float] | None = None
    search_params: dict
    temporal_window_seconds: float | None = None
    recall_boost_enabled: bool = True
    top_k: int = 10


class SemanticDuplicateRequest(BaseModel):
    tenant_id: str
    embedding: list[float]
    content_hash: str | None = None
    fleet_id: str | None = None
    threshold: float = 0.95


class ContradictionCandidatesRequest(BaseModel):
    tenant_id: str
    embedding: list[float]
    memory_type: str
    fleet_id: str | None = None


class GraphExpandRequest(BaseModel):
    seed_ids: list[str]
    tenant_id: str
    fleet_id: str | None = None
    max_hops: int = 2
    use_union: bool = True


class NearDuplicatesRequest(BaseModel):
    tenant_id: str
    fleet_id: str | None = None


class MemoryEntityLinksRequest(BaseModel):
    entity_ids: list[str]


class EntityFTSRequest(BaseModel):
    tokens: list[str]
    tenant_id: str
    fleet_ids: list[str] | None = None
