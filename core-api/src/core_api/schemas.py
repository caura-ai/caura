from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import AliasChoices, BaseModel, Field, create_model, model_validator

from common.constants import AGENT_TUNABLE_KEYS, SEARCH_KNOBS
from core_api.constants import (
    BULK_MAX_ITEMS,
    DEFAULT_MEMORY_TYPE,
    DEFAULT_SEARCH_TOP_K,
    MAX_CONTENT_LENGTH,
    MAX_QUERY_LENGTH,
    MAX_SEARCH_TOP_K,
    MAX_TRUST_LEVEL,
    MEMORY_STATUSES_PATTERN,
    MEMORY_TYPES_FILTER_DESCRIPTION,
    MEMORY_TYPES_WRITE_DESCRIPTION,
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
    # Optional like ``BulkMemoryCreate.agent_id``: omitting it is allowed only
    # on the standalone single-tenant path, where ``write_memory`` fills the
    # reserved ``"mcp-agent"`` identity. Tenant-scoped/gateway callers must
    # still pass an explicit agent_id (enforced in the route) so writes are
    # never silently attributed to one shared identity. min_length=1 rejects an
    # empty string at the schema layer (None still means "unset").
    agent_id: str | None = Field(default=None, min_length=1)
    memory_type: MemoryType | None = Field(default=None, description=MEMORY_TYPES_WRITE_DESCRIPTION)
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
    write_mode: Literal["fast", "strong", "auto", "stm"] | None = Field(
        default=None,
        description=(
            "Write-mode dial. 'fast' = embed-only, LLM enrichment deferred to the "
            "background; 'strong' = full pipeline inline; 'auto' = the system picks. "
            "'strong' also embeds inline regardless of the deployment's embedding "
            "mode, which makes it the supported opt-out when a caller must search "
            "for what it just wrote — see `MemoryOut.metadata.embedding_pending`. "
            "It costs the embedding provider call on the request path, which is "
            "exactly what fast mode's sub-2s p99 visibility SLA exists to avoid, so "
            "it is a per-write choice rather than a default to flip."
        ),
    )


class BulkMemoryItem(BaseModel):
    """Single item in a bulk write request. tenant_id/fleet_id/agent_id inherited from parent."""

    # Additive-tolerance policy (broker↔cloud API-versioning RFC): a bulk write
    # must NOT 422 the whole batch because one item has a bad field — the valid
    # items must still be written. So schema-level constraints that would reject
    # the ENTIRE request are deliberately dropped here (unlike single-write
    # ``MemoryCreate`` / ``MemoryUpdate``, which keep them) and re-enforced PER
    # ITEM in ``create_memories_bulk``, aggregated into one ``status="error"``
    # 207 row per item:
    #   - memory_type ∉ MEMORY_TYPES → memory_type_errors. The field is ``str``
    #     here (not the typed ``MemoryType`` enum used on single-write), so an
    #     unknown type is a per-item error, not a whole-batch 422.
    #   - content length → short_content_errors
    #     (``< CRYSTALLIZER_SHORT_CONTENT_CHARS`` = 10, subsumes the old
    #     ``min_length=1``) + oversized_content_errors (``> MAX_CONTENT_LENGTH``).
    #   - weight range [0.0, 1.0] → weight_errors.
    #   - status enum → status_errors.
    memory_type: str | None = Field(default=None, description=MEMORY_TYPES_WRITE_DESCRIPTION)
    content: str
    weight: float | None = Field(default=None)
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
    status: str | None = Field(default=None)  # validated per-item (status_errors); see note above
    # Per ITEM, not per batch, deliberately: callers that funnel through the bulk
    # endpoint may coalesce writes from unrelated sources into one request (the
    # broker's durable queue does exactly this), so one item asking for 'strong'
    # must not force inline embedding on everything batched alongside it.
    #
    # Untyped ``str`` rather than a Literal, per this model's additive-tolerance
    # policy above: a Literal would 422 the WHOLE batch over one item's unknown
    # value (e.g. 'stm' copied from a single-write payload). Only 'strong' has an
    # effect; every other value, known or not, behaves as 'fast'.
    write_mode: str | None = Field(
        default=None,
        description=(
            "Per-item write mode. 'strong' embeds this item inline, so it is "
            "searchable as soon as the write is persisted rather than after the "
            "background backfill — at the cost of an embedding provider call on "
            "the request path. Any other value behaves as 'fast': the item follows "
            "the deployment's embedding mode. On a deployment that already embeds "
            "inline this has no effect, since every item is embedded inline anyway. "
            "Narrower than MemoryCreate.write_mode: on the bulk path 'strong' "
            "affects only the embedding — LLM enrichment defers either way — and "
            "the tenant's default_write_mode is not consulted, so only an explicit "
            "per-item opt-in embeds inline."
        ),
    )


class BulkMemoryCreate(BaseModel):
    tenant_id: str
    fleet_id: str | None = None
    # Optional on the wire so memclawd broker calls (cloud-data-plane.md
    # §2.4) can omit it — the route handler defaults to
    # ``broker:<install_uuid>`` when the caller authenticates with an
    # install credential. Non-broker callers (dashboard / SDK) still
    # must populate it; the route's relaxation branch keys off the
    # credential kind, not the body.
    agent_id: str | None = None
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
    memory_type: MemoryType | None = Field(default=None, description=MEMORY_TYPES_WRITE_DESCRIPTION)
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


class ScoreParts(BaseModel):
    """D12 — per-factor breakdown of the ranking composite ``MemoryOut.score``.

    Every factor mirrors a column the scored-search SQL already computes and
    returns per row; this model only surfaces them. All fields are nullable:
    FTS-only rows have no ``vec_sim``, entity-lookup short-circuit rows have no
    FTS rank, and successor-injected rows were never scored at all.
    """

    vec_sim: float | None = None
    fts_score: float | None = None
    freshness: float | None = None
    entity_boost: float | None = None
    recall_boost: float | None = None
    temporal_boost: float | None = None
    status_penalty: float | None = None


class MemoryOut(BaseModel):
    id: UUID
    tenant_id: str
    fleet_id: str | None = None
    agent_id: str
    # Human-readable agent label (agents.display_name), NULL-safe: null when the
    # agent has no agents row yet (e.g. broker:<install> ids) — clients fall back
    # to agent_id. Populated via a LEFT JOIN in the storage query methods, not
    # stored on the memory row.
    agent_display_name: str | None = None
    memory_type: str
    title: str | None = None
    content: str
    weight: float
    source_uri: str | None
    run_id: str | None
    # Two flags here tell a caller the write is not fully settled yet:
    #
    #   embedding_pending: true   — the row was stored without an embedding and a
    #     background backfill is scheduled. Until it lands the memory is reachable
    #     by keyword (FTS) and by the non-semantic `GET /memories` list, but it
    #     does not compete on semantic similarity, so a `search` for a paraphrase
    #     of it can come back empty. Observed time-to-searchable: ~15-20s on
    #     production, and over 10 minutes on staging, whose backfill is slower —
    #     so do not treat it as "a moment".
    #   enrichment_pending: true  — title/memory_type/weight/status/ts_valid_* are
    #     still the caller-supplied or default values; the LLM pass will PATCH them.
    #
    # Absent (or false) means that stage ran inline. On the single-write path
    # `write_mode="strong"` embeds and enriches inline, so neither flag appears —
    # that is the supported read-your-own-write opt-out, at the cost of the
    # provider calls on the request path. A deployment running embedding inline
    # (OSS local default) never sets embedding_pending, even in fast mode.
    #
    # `BulkMemoryItem.write_mode` is narrower: there 'strong' governs the
    # embedding only, enrichment defers either way, and the bulk path sets neither
    # flag — so a bulk caller cannot read pendingness off its own write response.
    metadata: dict | None
    # C25 — platform-written telemetry/enrichment (llm_ms, write_latency_ms,
    # semantic_dedup_ms, summary, tags, pii flags, write-mode flags …) exposed
    # under their own namespace. For one release the same keys ALSO remain in
    # ``metadata`` (dual-emit) — reading them from ``metadata`` is deprecated.
    # Derived for historical rows too; None when nothing platform-written
    # exists. A caller's own ``metadata.summary`` / ``metadata.tags`` are no
    # longer overwritten by enrichment (the platform's copy lives here).
    system_metadata: dict | None = None
    created_at: datetime
    expires_at: datetime | None
    entity_links: list[EntityLinkOut] = []
    similarity: float | None = None
    # D12 — the multiplicative ranking composite (similarity * freshness *
    # entity/recall/temporal boosts * status_penalty) that actually ordered
    # this row, plus its factors. ``similarity`` above stays the raw 0..1
    # cosine (the ``min_similarity``-comparable value); ``score`` routinely
    # exceeds 1.0 and is for explaining rank, not for threshold gating.
    # Populated only on scored-search hits; None on list/get reads and on
    # successor-injected rows, which were never scored.
    score: float | None = None
    score_parts: ScoreParts | None = None
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
    # Unified contradiction model (A55) — system-populated, read-only on the API.
    # confidence: confidence in this memory's claim (None = unknown/legacy).
    # is_inferred: True when the system materialised this memory by inference.
    # scope: structured validity qualifiers (role/task/location).
    confidence: float | None = None
    is_inferred: bool = False
    scope: dict | None = None
    # Usage info (populated on write responses)
    usage: UsageSummary | None = None

    model_config = {"from_attributes": True}


# Kept to ONE line, because a model docstring becomes the schema's public
# ``description`` in ``openapi.broker.json`` — maintainer rationale belongs here, in a
# comment, not in the frozen contract. Three things about this model are load-bearing:
#
# FIELD ORDER IS THE CONTRACT. FastAPI serialises in model-field order, not dict
# order, and this endpoint has always emitted these 29 keys in this order. Reordering
# them changes the response bytes for every caller;
# ``tests/test_memory_get_serialization_contract.py`` pins it.
#
# THE TIMESTAMPS ARE ``str``, NOT ``datetime``, ON PURPOSE. They arrive as ISO strings
# — the route reads them out of core-storage-api's JSON and passes them through — and
# ``datetime`` would not merely annotate, it re-serialises: pydantic v2 renders
# ``2026-08-13T18:52:48.040997+00:00`` as ``...040997Z``. Measured, not assumed. That
# is a wire change on a live endpoint, not a side effect to take on while adding a
# schema. The fleet is already inconsistent here: ``MemoryOut`` (the POST/PATCH model)
# types them as ``datetime`` and emits the ``Z`` form for the same row. Aligning the
# two is an API decision with its own migration story; ``str`` documents what ships.
#
# NULLABILITY MIRRORS THE TABLE, plus the two computed fields that are ``None`` for an
# un-embedded row. A field marked required here that is NULL in practice turns a 200
# into a 500 via ``ResponseValidationError``, so this errs toward optional wherever the
# column is nullable.
class MemoryDetailResponse(BaseModel):
    """Full detail for one memory: row fields, entity links, embedding stats."""

    id: str
    tenant_id: str
    fleet_id: str | None = None
    agent_id: str
    agent_display_name: str | None = None
    memory_type: str
    title: str | None = None
    content: str
    weight: float | None = None
    source_uri: str | None = None
    run_id: str | None = None
    # Exposed as ``metadata``; storage serialises the JSONB column as ``metadata_``
    # and the route renames it on the way out.
    metadata: dict | None = None
    # C25 — platform-written view; same contract as ``MemoryOut.system_metadata``.
    system_metadata: dict | None = None
    content_hash: str | None = None
    created_at: str | None = None
    expires_at: str | None = None
    deleted_at: str | None = None
    subject_entity_id: str | None = None
    predicate: str | None = None
    object_value: str | None = None
    ts_valid_start: str | None = None
    ts_valid_end: str | None = None
    status: str
    visibility: str
    recall_count: int
    last_recalled_at: str | None = None
    supersedes_id: str | None = None
    entity_links: list[dict] = []
    # Both None unless the row has a non-empty embedding: the raw pgvector never
    # crosses the wire, only a first-20 preview and the server-computed stats.
    embedding_preview: list[float] | None = None
    embedding_stats: dict | None = None


class ContradictionInfo(BaseModel):
    """Summary of a contradiction detected on write.

    ``old_memory_id`` always refers to the **pre-existing candidate**
    (never to ``new_memory``), regardless of which row ended up being
    the older one in the supersession chain. The ``direction`` field
    disambiguates the two cases:

      - ``"canonical"`` — the candidate is older than ``new_memory``;
        the candidate became outdated/conflicted, ``new_memory``
        carries ``supersedes_id`` pointing at it. (Historical behaviour.)
      - ``"flipped"`` — the candidate is newer than ``new_memory``;
        ``new_memory`` is the row that became outdated/conflicted, and
        the candidate now carries ``supersedes_id`` pointing back at
        ``new_memory``. This branch was previously unreachable
        (CAURA-125; gap A6) and is now exercised by deferred-embedding
        races and ``created_at`` ties.
    """

    old_memory_id: UUID
    old_status: str
    reason: str  # "rdf_conflict" or "semantic_conflict"
    old_content_preview: str
    # CAURA-125 — defaults to "canonical" so any existing caller that
    # constructs ``ContradictionInfo`` without supplying ``direction``
    # keeps producing the same shape it did before this PR.
    direction: Literal["canonical", "flipped"] = "canonical"


# --- Search ---


class PaginatedMemoryResponse(BaseModel):
    items: list[MemoryOut]
    next_cursor: str | None = None


class SearchDiagnostic(BaseModel):
    """D12 — retrieval trace returned when ``SearchRequest.diagnostic`` is true.

    Answers "why did each result appear (and what got cut)": the full widened
    candidate set with per-row score factors and exclusion reasons, the applied
    knobs, and the strategy the classifier picked. Requesting it does NOT change
    the ``items`` a caller gets back, and a diagnostic call never bumps
    ``recall_count`` — it is inspection, not use.
    """

    retrieval_strategy: str | None = None
    top_k_requested: int | None = None
    min_similarity_applied: float | None = None
    candidates_considered: int = 0
    returned: int = 0
    excluded_below_min_similarity: int = 0
    excluded_by_top_k_trim: int = 0
    # The resolved knob set the scoring SQL ran with (profile → tenant → constant,
    # after any per-request ``min_similarity`` override).
    search_params: dict = {}
    # One entry per widened candidate: id/title/type/status + score + factors +
    # ``excluded`` (None | "below_min_similarity" | "trimmed_by_top_k").
    all_candidates: list[dict] = []


class SearchResponse(BaseModel):
    """Envelope for search results — matches PaginatedMemoryResponse shape."""

    items: list[MemoryOut]
    # D12 — present only when the request set ``diagnostic=true``.
    diagnostic: SearchDiagnostic | None = None


class SearchRequest(BaseModel):
    tenant_id: str
    fleet_ids: list[str] | None = None
    query: str = Field(min_length=1, max_length=MAX_QUERY_LENGTH)
    filter_agent_id: str | None = None
    # C31/D2 — the short names are canonical (the spellings the MCP tools use);
    # the historical `*_filter` forms stay accepted forever as aliases. Before
    # this, `memory_type=fact` (the MCP spelling) was silently DROPPED by the
    # extra="ignore" contract — the C1+C2 trap. When both spellings arrive,
    # the long form wins (first in AliasChoices).
    memory_type_filter: MemoryType | None = Field(
        default=None,
        validation_alias=AliasChoices("memory_type_filter", "memory_type"),
        description=MEMORY_TYPES_FILTER_DESCRIPTION,
    )
    status_filter: str | None = Field(
        default=None,
        pattern=MEMORY_STATUSES_PATTERN,
        validation_alias=AliasChoices("status_filter", "status"),
    )
    valid_at: datetime | None = None
    top_k: int = Field(
        default=DEFAULT_SEARCH_TOP_K,
        ge=1,
        le=MAX_SEARCH_TOP_K,
        description=f"Maximum results to return (1-{MAX_SEARCH_TOP_K}, default {DEFAULT_SEARCH_TOP_K}).",
    )
    # D12 — per-request cosine floor. Overrides the resolved profile/tenant
    # default for THIS call only (request beats profile beats tenant beats
    # constant). Gates on the raw ``vec_sim`` — the same value returned in
    # ``MemoryOut.similarity`` — never on the boosted composite ``score``.
    # FTS-only rows (no embedding yet) bypass the floor by contract.
    min_similarity: float | None = Field(
        default=None,
        ge=0.0,
        le=1.0,
        description=(
            "Per-request similarity floor (0.0-1.0) applied to the raw cosine; "
            "overrides the agent/tenant search-profile value for this call."
        ),
    )
    # D12 — when true, the response carries a ``diagnostic`` retrieval trace
    # (full candidate set, score factors, exclusion reasons, applied knobs).
    # Results are unchanged and no recall_count is bumped on a diagnostic call.
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


# Derived from ``SEARCH_KNOBS`` rather than written out. The fields and their
# bounds were hand-maintained here, in the MCP ``caura_tune`` signature and in
# the knob table, and they had already drifted: ``graph_max_hops`` was capped at 3
# here while the table allowed 5, so a tenant-wide default could hold a depth no
# agent profile could set (#730). One declaration removes the class of drift
# rather than testing for it.
#
# ``agent_tunable`` is what selects the nine; the three A/B knobs
# (``fts_rank_scale``, ``candidate_pool_size``, ``score_formula``) stay off this
# surface deliberately — they are tenant-level A/B levers, not per-agent tuning.
# ``dict[str, Any]`` explicitly: ``create_model``'s overloads take
# ``**field_definitions: Any | tuple[Any, Any]``, and mypy will not match a
# comprehension it infers as ``dict[Any, tuple[Any, None]]`` against them.
_PROFILE_FIELDS: dict[str, Any] = {
    name: (
        SEARCH_KNOBS[name].value_type | None,
        Field(default=None, ge=SEARCH_KNOBS[name].bounds[0], le=SEARCH_KNOBS[name].bounds[1]),
    )
    for name in AGENT_TUNABLE_KEYS
}

SearchProfileUpdate = create_model(
    "SearchProfileUpdate",
    __doc__="Per-agent search tuning knobs. All fields optional — only override what you set.",
    **_PROFILE_FIELDS,
)


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
