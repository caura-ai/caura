"""Spec-only response models for C33 (OpenAPI completeness).

These models document success-response shapes in the generated OpenAPI spec
via ``responses={200: {"model": ...}}`` on the route decorators. They are
deliberately NOT passed as ``response_model=``: attaching them there would
route every response through Pydantic serialization (coercing values and
dropping undeclared keys), which is a runtime behavior change this task must
not make. Spec-only attachment documents the shape and changes nothing on
the wire.

Consequence: nothing enforces these models at runtime. When a handler's
return shape changes, the matching model here must change in the same PR —
``tests/test_c33_openapi_completeness.py`` ratchets the count of
undocumented success responses so new routes can't ship blank, and the
wet-test checklist for schema-touching PRs includes diffing a live response
against its model here.

Conditional keys (present only under a documented condition) are declared
Optional with a ``description`` saying when they appear; JSON-mapping values
use ``dict[str, int]``-style types rather than named models.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from core_api.schemas import MemoryOut

# --------------------------------------------------------------------------
# memories / recall / health / version
# --------------------------------------------------------------------------


class MemoryStatsResponse(BaseModel):
    total: int = Field(description="Live (non-deleted) memory count in scope.")
    by_type: dict[str, int]
    by_agent: dict[str, int]
    by_status: dict[str, int]
    by_tenant: dict[str, int] | None = Field(
        default=None,
        description="Only for cross-tenant reads spanning more than one tenant.",
    )
    deleted: int | None = Field(default=None, description="Only when include_deleted=true.")
    total_including_deleted: int | None = Field(default=None, description="Only when include_deleted=true.")


class MemoryCountResponse(BaseModel):
    count: int


class BulkDeleteResponse(BaseModel):
    deleted: int = Field(description="Number of memories soft-deleted.")


class SupersessionPeer(BaseModel):
    id: str
    content_preview: str = Field(description="First 200 characters of content.")
    status: str
    created_at: str


class ContradictionEntry(BaseModel):
    memory_id: str
    status: str
    reason: str = Field(description="One of rdf_conflict, semantic_conflict, unknown.")
    content_preview: str
    direction: str = Field(description="superseded_by or supersedes.")
    created_at: str


class MemoryContradictionsResponse(BaseModel):
    memory_id: str
    status: str | None
    superseded_by: SupersessionPeer | None = Field(
        description="The newer memory that superseded this one; null when none is live."
    )
    superseded_memories: list[SupersessionPeer]
    detection_status: str = Field(description="completed or pending.")
    contradictions: list[ContradictionEntry]


class MemoryStatusPatchResponse(BaseModel):
    memory_id: str
    old_status: str | None
    new_status: str


class RecallDiagnostic(BaseModel):
    recall_prompt: str | None
    recall_model: str | None
    recall_provider: str | None
    all_candidates: list
    top_k_used: int | None
    retrieval_strategy: str | None
    search_params: dict


class RecallResponse(BaseModel):
    query: str
    summary: str
    memory_count: int
    memories: list[MemoryOut]
    items: list[MemoryOut] = Field(
        description="Alias of memories (canonical list key per the wire contract)."
    )
    recall_ms: int
    diagnostic: RecallDiagnostic | None = Field(
        default=None, description="Only when the request sets diagnostic=true."
    )


class VersionResponse(BaseModel):
    version: str


# --------------------------------------------------------------------------
# documents / keystones / evolve / entities / graph
# --------------------------------------------------------------------------


class DocumentSearchItem(BaseModel):
    collection: str
    doc_id: str
    data: dict = Field(description="Arbitrary caller-supplied document body.")
    similarity: float


class DocumentSearchResponse(BaseModel):
    collection: str | None = Field(
        description="Echo of the requested collection; null for cross-collection search."
    )
    count: int
    results: list[DocumentSearchItem] = Field(description="Deprecated alias of items (wire contract D1).")
    items: list[DocumentSearchItem]


class DocumentCollectionInfo(BaseModel):
    name: str
    count: int = Field(description="Documents in this collection.")


class DocumentCollectionsResponse(BaseModel):
    collections: list[DocumentCollectionInfo]
    count: int = Field(description="Number of collections, not total documents.")


class KeystoneData(BaseModel):
    title: str
    content: str
    weight: int = Field(
        description="Priority bucket 25, 50, or 100 (request labels low/med/high are converted)."
    )
    scope: str = Field(description="tenant, fleet, or agent.")
    agent_id: str | None = Field(default=None, description="Only when scope=agent.")
    author_user_id: str | None = Field(default=None, description="Only when supplied at write time.")


class KeystoneDoc(BaseModel):
    id: str
    tenant_id: str
    fleet_id: str | None = Field(description="Null for tenant-scope rules.")
    collection: str = Field(description='Always "_keystones".')
    doc_id: str
    data: KeystoneData
    created_at: str
    updated_at: str


class KeystonesEnvelope(BaseModel):
    count: int
    items: list[KeystoneDoc]


class KeystoneDeleteResponse(BaseModel):
    deleted: bool
    doc_id: str


class WeightAdjustment(BaseModel):
    memory_id: str
    old_weight: float
    new_weight: float
    delta: float


class GeneratedRule(BaseModel):
    rule_memory_id: str
    condition: str
    action: str
    confidence: float


class EvolveReportResponse(BaseModel):
    outcome_id: str
    outcome_type: str = Field(description="success, failure, or partial.")
    scope: str = Field(description="agent, fleet, or all.")
    weight_adjustments: list[WeightAdjustment]
    rules_generated: list[GeneratedRule] = Field(
        description="At most one element; empty unless a rule persisted."
    )
    rule_skipped_reason: str | None = Field(
        description="Slug explaining why no rule was generated; null when one was."
    )
    weight_adjustment_skipped_reason: str | None = Field(
        description="Slug explaining why no weights moved; null when at least one did."
    )
    out_of_scope_count: int
    evolve_ms: int


class EntityListItem(BaseModel):
    id: str
    tenant_id: str | None
    fleet_id: str | None
    entity_type: str | None
    canonical_name: str | None
    attributes: dict | None
    memory_count: int


class GraphNode(BaseModel):
    id: str
    label: str | None = Field(description="Entity canonical_name.")
    type: str | None = Field(description="Entity entity_type.")
    fleet_id: str | None
    attributes: dict | None
    memory_count: int


class GraphEdge(BaseModel):
    id: str
    source: str
    target: str
    relation_type: str | None
    weight: float
    evidence_memory_id: str | None


class GraphResponse(BaseModel):
    nodes: list[GraphNode]
    edges: list[GraphEdge]


# --------------------------------------------------------------------------
# settings / tenants / fleets / tool-descriptions
# --------------------------------------------------------------------------


class ProviderModelChoice(BaseModel):
    provider: str | None
    model: str | None


class ProviderModelToggle(ProviderModelChoice):
    enabled: bool | None


class SearchSettings(BaseModel):
    recall_boost: bool | None
    graph_retrieval: bool | None
    entity_retrieval: bool | None
    default_profile: dict = Field(
        description="Open knob map (min_similarity, top_k, freshness_floor, score_formula, ...)."
    )


class WriteSettings(BaseModel):
    default_write_mode: str | None = Field(description="fast or strong; null means fast.")
    triple_emission_enabled: bool | None
    retraction_enabled: bool | None


class SettingsResponse(BaseModel):
    """Full effective settings tree: DEFAULT_SETTINGS deep-merged with tenant
    overrides — every section always present. Deep operational blobs are left
    as open objects here; their authoritative key sets live in
    ``services/organization_settings.py::DEFAULT_SETTINGS``."""

    enrichment: ProviderModelToggle
    recall: ProviderModelToggle
    embedding: ProviderModelChoice
    entity_extraction: ProviderModelToggle
    fallback_llm: ProviderModelChoice
    agent_digest: dict
    search: SearchSettings
    crystallizer: dict
    dedup: dict
    lifecycle: dict
    entity_linking: dict
    insights: dict
    observability: dict
    chunking: dict
    write: WriteSettings
    agents: dict
    memclaw: dict  # legacy-name-ok: documents the existing settings wire key
    security_audit: dict
    skills_factory: dict
    interviewer: dict
    entity_blocklist: list[str]
    governance: dict
    api_keys: dict = Field(description="Per-tenant provider key overrides (open object).")


class FleetDistributionItem(BaseModel):
    fleet_id: str
    memory_count: int
    agent_count: int


class ToolDescriptionEnriched(BaseModel):
    description: str
    stm_only: bool


# --------------------------------------------------------------------------
# fleet / agents
# --------------------------------------------------------------------------


class FleetCreateResponse(BaseModel):
    ok: bool
    fleet_id: str
    tenant_id: str


class FleetListItem(BaseModel):
    fleet_id: str | None
    node_count: int
    last_heartbeat: str | None
    status: str = Field(description="online or offline.")


class FleetPurgeResponse(BaseModel):
    ok: bool
    tenant_id: str
    fleet_id: str
    deleted: dict[str, int] = Field(
        description="Rows deleted per table (fleet_commands, memories, entities, ...)."
    )


class HeartbeatCommand(BaseModel):
    id: str
    command: str | None
    payload: dict | None


class HeartbeatResponse(BaseModel):
    ok: bool
    node_id: str
    commands: list[HeartbeatCommand] = Field(description="Pending commands, acked on delivery.")


class CommandCreateResponse(BaseModel):
    id: str
    status: str


class FleetCommand(BaseModel):
    id: str
    node_id: str
    command: str | None
    payload: dict | None
    status: str = Field(description="pending, acked, done, or failed.")
    result: dict | None
    created_at: str
    acked_at: str | None
    completed_at: str | None


class OkResponse(BaseModel):
    ok: bool


class FleetNode(BaseModel):
    node_id: str
    node_name: str
    fleet_id: str | None
    hostname: str | None
    ip: str | None
    openclaw_version: str | None
    plugin_version: str | None
    plugin_hash: str | None
    os_info: str | None
    status: str = Field(description="online, stale, or offline.")
    agents: list | None
    tools: list | None
    channels: list | None
    metadata: dict | None = Field(
        description="Open object; may carry recall_metrics, reconcile, deploy_blocked_until, sentinel markers."
    )
    last_heartbeat: str | None
    created_at: str | None


class FleetAgentStats(BaseModel):
    agent_id: str
    trust_level: int
    total_memories: int
    last_write_at: str | None
    total_recalls: int
    last_recall_at: str | None
    active_24h: bool
    stale: bool


class FleetSummary(BaseModel):
    total_agents: int
    active_agents_24h: int
    memories_24h: int
    stale_agents: int
    total_memories: int
    conflicted_memories: int
    outdated_memories: int
    deleted_memories: int
    recalled_memories_24h: int


class FleetStatsResponse(BaseModel):
    agents: list[FleetAgentStats]
    fleet_summary: FleetSummary


class AgentFleetPatchResponse(BaseModel):
    agent_id: str
    old_fleet_id: str | None
    new_fleet_id: str


# --------------------------------------------------------------------------
# ingest / crystallize / insights
# --------------------------------------------------------------------------


class IngestFact(BaseModel):
    content: str
    suggested_type: str
    source_uri: str | None = None
    salience: float | None = Field(default=None, description="Only when the extractor emitted one.")


class IngestPreviewResponse(BaseModel):
    """Covers all four branches: cache hit (cached+run_id), too-short
    (skipped_reason), zero-section, and the normal LLM path."""

    url: str | None
    content_length: int
    facts: list[IngestFact]
    chunk_ms: int
    doc_hash: str | None = Field(default=None, description="Absent on cache-hit and too-short branches.")
    sections: int | None = Field(default=None, description="Absent on cache-hit and too-short branches.")
    cached: bool | None = Field(default=None, description="Only on a doc-hash cache hit.")
    run_id: str | None = Field(default=None, description="Only on a cache hit: the prior run's id.")
    skipped_reason: str | None = Field(default=None, description="Only when skipped (content_too_short).")


class IngestCommitResponse(BaseModel):
    url: str | None
    facts_extracted: int
    memories_created: int
    skipped_duplicates: int
    errored: int
    run_id: str
    ingest_ms: int


class IngestUndoResponse(BaseModel):
    deleted: int
    run_id: str


class CrystallizeReport(BaseModel):
    """Nested blobs are open objects; authoritative sub-shapes live in
    ``services/crystallizer_service.py`` (each may be ``{"error": true}``
    when its check failed)."""

    id: str
    tenant_id: str | None
    fleet_id: str | None
    trigger: str | None
    status: str | None = Field(description="running, completed, or failed.")
    started_at: str | None
    completed_at: str | None
    duration_ms: int | None
    summary: dict = Field(description="overall_score / critical / warning / info counts.")
    hygiene: dict = Field(
        description="Seven checks (orphaned_entities, near_duplicates, ...), each with count and affected ids."
    )
    health: dict
    usage_data: dict
    issues: list[dict] = Field(
        description="Each: severity, category, code, title, description, count, affected_ids."
    )
    crystallization: dict


class InsightFinding(BaseModel):
    type: str
    headline: str
    what_happened: str
    why_it_matters: str
    recommended_action: str
    confidence: float
    related_memory_ids: list[str]
    title: str = Field(description="Legacy mirror of headline.")
    description: str = Field(description="Legacy mirror of what_happened + why_it_matters.")
    recommendation: str = Field(description="Legacy mirror of recommended_action.")
    insight_memory_id: str | None = Field(description="Null when persisting this finding failed.")


class InsightsResponse(BaseModel):
    focus: str
    scope: str
    memories_analyzed: int
    findings: list[InsightFinding]
    summary: str
    insight_memory_ids: list[str]
    gate_rejected: int
    insights_ms: int


class HealthResponse(BaseModel):
    status: str = Field(description="ok, degraded, or unhealthy (unhealthy is served as 503).")
    storage: str = Field(description="connected or unreachable.")
    redis: str = Field(description="connected, unavailable, or not configured.")
    event_bus: str = Field(description="ok, unhealthy, or error.")
    platform_init_errors: list[str] | None = Field(
        default=None,
        description="Only when platform init recorded errors and all dependencies are up (status=degraded).",
    )
    unhealthy_dependencies: list[str] | None = Field(
        default=None,
        description="Only on 503: names of failing dependencies.",
    )
