from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field, model_validator

from core_api.constants import (
    BULK_MAX_ITEMS,
    DEFAULT_MEMORY_TYPE,
    DEFAULT_SEARCH_TOP_K,
    MAX_CONTENT_LENGTH,
    MAX_QUERY_LENGTH,
    MAX_SEARCH_TOP_K,
    MAX_TRUST_LEVEL,
    MEMORY_STATUSES_PATTERN,
    MEMORY_TYPES_DESCRIPTION,
    MEMORY_VISIBILITIES_PATTERN,
    MIN_TRUST_LEVEL,
    MemoryType,
)

# --- Memory ---


class EntityLinkIn(BaseModel):
    entity_id: UUID
    role: str


class MemoryCreate(BaseModel):
    tenant_id: str
    fleet_id: str | None = None
    agent_id: str
    memory_type: MemoryType | None = Field(default=None, description=MEMORY_TYPES_DESCRIPTION)
    content: str = Field(min_length=1, max_length=MAX_CONTENT_LENGTH)
    weight: float | None = Field(default=None, ge=0.0, le=1.0)
    source_uri: str | None = None
    run_id: str | None = None
    metadata: dict | None = None
    entity_links: list[EntityLinkIn] = []
    expires_at: datetime | None = None
    # RDF triple
    subject_entity_id: UUID | None = None
    predicate: str | None = None
    object_value: str | None = None
    # Temporal validity
    ts_valid_start: datetime | None = None
    ts_valid_end: datetime | None = None
    # Reference datetime for LLM enrichment (resolves relative dates like "last week")
    reference_datetime: datetime | None = None
    # Status lifecycle
    status: str | None = Field(default=None, pattern=MEMORY_STATUSES_PATTERN)
    # Visibility scope
    visibility: str | None = Field(default=None, pattern=MEMORY_VISIBILITIES_PATTERN)
    # Extract-only mode: run enrichment + embedding but skip DB insert
    persist: bool = True
    # Write-mode dial: "fast" (embed-only → background enrich), "strong" (full pipeline), "auto" (system picks)
    write_mode: Literal["fast", "strong", "auto", "stm"] | None = None


class BulkMemoryItem(BaseModel):
    """Single item in a bulk write request. tenant_id/fleet_id/agent_id inherited from parent."""

    memory_type: MemoryType | None = Field(default=None, description=MEMORY_TYPES_DESCRIPTION)
    content: str = Field(min_length=1, max_length=MAX_CONTENT_LENGTH)
    weight: float | None = Field(default=None, ge=0.0, le=1.0)
    source_uri: str | None = None
    run_id: str | None = None
    metadata: dict | None = None
    entity_links: list[EntityLinkIn] = []
    expires_at: datetime | None = None
    subject_entity_id: UUID | None = None
    predicate: str | None = None
    object_value: str | None = None
    ts_valid_start: datetime | None = None
    ts_valid_end: datetime | None = None
    reference_datetime: datetime | None = None
    status: str | None = Field(default=None, pattern=MEMORY_STATUSES_PATTERN)


class BulkMemoryCreate(BaseModel):
    tenant_id: str
    fleet_id: str | None = None
    agent_id: str
    items: list[BulkMemoryItem] = Field(min_length=1, max_length=BULK_MAX_ITEMS)
    visibility: str | None = Field(default=None, pattern=MEMORY_VISIBILITIES_PATTERN)


class BulkItemResult(BaseModel):
    """Per-item outcome of a bulk write (CAURA-602).

    Status semantics:

    - ``"created"``: this attempt newly inserted the row; ``id`` is the
      new row's id.
    - ``"duplicate_attempt"``: same ``X-Bulk-Attempt-Id``+index already
      committed in a prior call. ``id`` is the canonical row from that
      first attempt. Returned when a retry hits the per-item unique
      constraint — what eliminates the silent-create class.
    - ``"duplicate_content"``: a different attempt's row with the same
      ``content_hash`` already exists. ``id`` and ``duplicate_of`` both
      point at the existing row; emitted in place of an insert.
    - ``"error"``: the row could not be processed (validation,
      enrichment timeout, missing storage id). ``error`` describes.

    The legacy ``"duplicate"`` status is gone — callers must read
    ``duplicate_attempt`` vs ``duplicate_content`` because they imply
    different client-side actions (an idempotent retry succeeded vs
    "you already wrote this content earlier").
    """

    index: int
    client_request_id: str | None = None
    status: Literal["created", "duplicate_attempt", "duplicate_content", "error"]
    id: UUID | None = None
    duplicate_of: UUID | None = None
    error: str | None = None


class BulkMemoryResponse(BaseModel):
    """Aggregate response from the bulk-write endpoint.

    ``duplicates`` rolls up both ``duplicate_attempt`` and
    ``duplicate_content`` for top-level metric continuity; per-item
    detail lives in ``results``. The route returns 200 when everything
    succeeded and 207 Multi-Status when at least one item is in error —
    callers must read per-item ``status`` and never infer success from
    a 2xx alone.
    """

    created: int
    duplicates: int
    errors: int
    results: list[BulkItemResult]
    bulk_ms: int


class RedistributeRequest(BaseModel):
    memory_ids: list[UUID] = Field(..., min_length=1, max_length=500)
    target_agent_id: str = Field(..., min_length=1, max_length=256)


class RedistributeResponse(BaseModel):
    moved: int
    promoted: int  # scope_agent → scope_team auto-promotions
    skipped: int  # already owned by target
    errors: list[str]
    redistribute_ms: int


class MemoryUpdate(BaseModel):
    content: str | None = Field(default=None, min_length=1, max_length=MAX_CONTENT_LENGTH)
    memory_type: MemoryType | None = Field(default=None, description=MEMORY_TYPES_DESCRIPTION)
    weight: float | None = Field(default=None, ge=0.0, le=1.0)
    title: str | None = None
    status: str | None = Field(default=None, pattern=MEMORY_STATUSES_PATTERN)
    visibility: str | None = Field(default=None, pattern=MEMORY_VISIBILITIES_PATTERN)
    metadata: dict | None = None
    metadata_mode: str | None = Field(
        default=None,
        pattern="^(merge|replace)$",
        description=(
            "How to apply ``metadata``: ``merge`` (default when omitted "
            "or ``null``) does a top-level JSONB ``||`` merge, preserving "
            "keys not present in the patch; ``replace`` overwrites the "
            "column wholesale."
        ),
    )
    source_uri: str | None = None
    subject_entity_id: UUID | None = None
    predicate: str | None = None
    object_value: str | None = None
    ts_valid_start: datetime | None = None
    ts_valid_end: datetime | None = None
    expires_at: datetime | None = None
    entity_links: list[EntityLinkIn] | None = None

    @model_validator(mode="after")
    def metadata_mode_requires_metadata(self) -> "MemoryUpdate":
        """Reject ``{"metadata_mode": "<merge|replace>"}`` (a real
        value, not None) without a matching ``metadata`` field.
        Pre-fix, sending only the mode flag was a silent 200 no-op
        (the request bypassed the "no fields to update" guard
        because ``metadata_mode`` is set, but produced no patch and
        no changes). Surface as 422 so the client knows the intent
        didn't land — and pair-fix prevents the phantom audit-record
        path entirely.

        The ``is not None`` guard matters for SDK clients that
        serialise the full schema with ``exclude_none=True``: the
        explicit-default-None lets them drop the field silently
        rather than always sending ``"merge"`` and tripping the
        validator on every non-metadata PATCH.
        """
        if (
            "metadata_mode" in self.model_fields_set
            and self.metadata_mode is not None
            and "metadata" not in self.model_fields_set
        ):
            raise ValueError("metadata_mode is only valid when metadata is also provided")
        return self


class EntityLinkOut(BaseModel):
    entity_id: UUID
    role: str


class UsageSummary(BaseModel):
    memories_stored: int | None = None
    memories_limit: int | None = None
    writes_remaining: int | None = None


class MemoryOut(BaseModel):
    id: UUID
    tenant_id: str
    fleet_id: str | None = None
    agent_id: str
    memory_type: str
    title: str | None = None
    content: str
    weight: float
    source_uri: str | None
    run_id: str | None
    metadata: dict | None
    created_at: datetime
    expires_at: datetime | None
    entity_links: list[EntityLinkOut] = []
    similarity: float | None = None
    # RDF triple
    subject_entity_id: UUID | None = None
    predicate: str | None = None
    object_value: str | None = None
    # Temporal validity
    ts_valid_start: datetime | None = None
    ts_valid_end: datetime | None = None
    # Status lifecycle
    status: str = "active"
    # Visibility scope
    visibility: str = "scope_team"
    # Recall tracking
    recall_count: int = 0
    last_recalled_at: datetime | None = None
    # Contradiction tracking
    supersedes_id: UUID | None = None
    superseded_by: list["ContradictionInfo"] | None = None
    # Usage info (populated on write responses)
    usage: UsageSummary | None = None

    model_config = {"from_attributes": True}


class ContradictionInfo(BaseModel):
    """Summary of a contradiction detected on write."""

    old_memory_id: UUID
    old_status: str
    reason: str  # "rdf_conflict" or "semantic_conflict"
    old_content_preview: str


# --- Search ---


class PaginatedMemoryResponse(BaseModel):
    items: list[MemoryOut]
    next_cursor: str | None = None


class SearchResponse(BaseModel):
    """Envelope for search results — matches PaginatedMemoryResponse shape."""

    items: list[MemoryOut]


class SearchRequest(BaseModel):
    tenant_id: str
    fleet_ids: list[str] | None = None
    query: str = Field(min_length=1, max_length=MAX_QUERY_LENGTH)
    filter_agent_id: str | None = None
    memory_type_filter: MemoryType | None = Field(
        default=None,
        description="Filter results to a single memory type. " + MEMORY_TYPES_DESCRIPTION,
    )
    status_filter: str | None = Field(default=None, pattern=MEMORY_STATUSES_PATTERN)
    valid_at: datetime | None = None
    top_k: int = Field(
        default=DEFAULT_SEARCH_TOP_K,
        ge=1,
        le=MAX_SEARCH_TOP_K,
        description=f"Maximum results to return (1-{MAX_SEARCH_TOP_K}, default {DEFAULT_SEARCH_TOP_K}).",
    )
    diagnostic: bool = False


# --- Entity ---


class EntityUpsert(BaseModel):
    tenant_id: str
    fleet_id: str | None = None
    entity_type: str
    canonical_name: str
    attributes: dict | None = None


class RelationOut(BaseModel):
    id: UUID
    relation_type: str
    to_entity_id: UUID
    to_entity_name: str | None = None
    weight: float
    evidence_memory_id: UUID | None

    model_config = {"from_attributes": True}


class EntityOut(BaseModel):
    id: UUID
    tenant_id: str
    fleet_id: str | None = None
    entity_type: str
    canonical_name: str
    attributes: dict | None
    linked_memories: list[MemoryOut] = []
    relations: list[RelationOut] = []

    model_config = {"from_attributes": True}


# --- Relation ---


class RelationUpsert(BaseModel):
    tenant_id: str
    fleet_id: str | None = None
    from_entity_id: UUID
    relation_type: str
    to_entity_id: UUID
    weight: float = Field(default=1.0, ge=0.0, le=1.0)
    evidence_memory_id: UUID | None = None


# --- Ingest ---


class IngestRequest(BaseModel):
    tenant_id: str
    fleet_id: str | None = None
    agent_id: str = "ingest-agent"
    url: str | None = None
    content: str | None = None
    focus: str | None = None
    # Optional caller-supplied source label. Used by the multipart upload
    # endpoint (``/ingest/file``) to thread ``upload:<filename>`` through
    # so the per-fact ``source_uri`` carries the original filename instead
    # of being stamped as the generic ``"text-input"`` marker. When
    # absent, ``ingest_preview`` falls back to ``url`` (URL ingest) or
    # ``"text-input"`` (pasted content) — unchanged behavior.
    source_uri: str | None = None


class IngestFact(BaseModel):
    content: str
    suggested_type: str = DEFAULT_MEMORY_TYPE
    # Provenance: ``ingest_preview`` stamps this on every fact it returns
    # (the URL it fetched from, or "text-input" for a pasted body). When
    # the caller round-trips the preview output straight to commit without
    # explicitly re-passing ``url``, this is the only thing that lets us
    # persist the right ``source_uri``. ``IngestCommitRequest.url`` still
    # wins if provided (dashboard back-compat).
    source_uri: str | None = None
    # A1 (PR #5): LLM-emitted salience score, 0.0-1.0. Preview's validator
    # already dropped sub-0.5 facts before returning, so any value seen
    # here passed the floor at preview time. Persisted on the memory so
    # an A2-cache-hit preview can restore it; not used for filtering at
    # commit time.
    salience: float | None = None


class IngestCommitRequest(BaseModel):
    tenant_id: str
    fleet_id: str | None = None
    agent_id: str = "ingest-agent"
    url: str | None = None
    facts: list[IngestFact]
    run_id: str | None = None
    # A2: optional. When the caller echoes the ``doc_hash`` from a prior
    # preview, commit stamps it on every persisted memory's metadata so
    # the *next* preview of the same content can short-circuit the LLM
    # call (cache-hit). Backward-compatible: omitting it just disables
    # the cache for future previews of this content.
    doc_hash: str | None = None


class RelationUpsertOut(BaseModel):
    id: UUID
    tenant_id: str
    fleet_id: str | None = None
    from_entity_id: UUID
    relation_type: str
    to_entity_id: UUID
    weight: float
    evidence_memory_id: UUID | None

    model_config = {"from_attributes": True}


# --- Agent ---


class AgentOut(BaseModel):
    id: UUID
    tenant_id: str
    fleet_id: str | None = None
    agent_id: str
    trust_level: int
    search_profile: dict | None = None
    created_at: datetime
    updated_at: datetime | None = None

    model_config = {"from_attributes": True}


class AgentTrustUpdate(BaseModel):
    trust_level: int = Field(ge=MIN_TRUST_LEVEL, le=MAX_TRUST_LEVEL)
    fleet_id: str | None = None


class SearchProfileUpdate(BaseModel):
    """Per-agent search tuning knobs. All fields optional — only override what you set."""

    top_k: int | None = Field(default=None, ge=1, le=20)
    min_similarity: float | None = Field(default=None, ge=0.1, le=0.9)
    fts_weight: float | None = Field(default=None, ge=0.0, le=1.0)
    freshness_floor: float | None = Field(default=None, ge=0.0, le=1.0)
    freshness_decay_days: int | None = Field(default=None, ge=7, le=730)
    recall_boost_cap: float | None = Field(default=None, ge=1.0, le=3.0)
    recall_decay_window_days: int | None = Field(default=None, ge=7, le=365)
    graph_max_hops: int | None = Field(default=None, ge=0, le=3)
    similarity_blend: float | None = Field(default=None, ge=0.0, le=1.0)


# --- Background Task ---


class STMWriteResponse(BaseModel):
    """Response for STM writes — different shape from MemoryOut."""

    id: str
    write_mode: str = "stm"
    target: str  # "notes" | "bulletin"
    tenant_id: str
    agent_id: str
    content: str
    ttl: int
    posted_at: datetime
    latency_ms: int = 0


class BackgroundTaskOut(BaseModel):
    id: UUID
    task_name: str
    memory_id: UUID | None = None
    tenant_id: str
    status: str
    error_message: str | None = None
    error_traceback: str | None = None
    created_at: datetime
    completed_at: datetime | None = None

    model_config = {"from_attributes": True}
