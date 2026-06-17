"""Centralised constants for the MemClaw API."""

import importlib.metadata
import os

# Re-export DB-query constants from common (shared with core-storage-api).
from common.constants import (  # noqa: F401
    CONTRADICTION_CANDIDATE_MAX,
    CONTRADICTION_SIMILARITY_THRESHOLD,
    DEFAULT_RELATION_TYPE_WEIGHT,
    ENTITY_RESOLUTION_CANDIDATE_LIMIT,
    GRAPH_MAX_EXPANDED_ENTITIES,
    GRAPH_MAX_HOPS,
    LIFECYCLE_STALE_ARCHIVE_WEIGHT,
    RECALL_BOOST_SCALE,
    RELATION_TYPE_WEIGHTS,
    SEMANTIC_DEDUP_CANDIDATE_LIMIT,
    SEMANTIC_DEDUP_THRESHOLD,
    SINGLE_VALUE_PREDICATES,
    TYPE_DECAY_DAYS,
    VECTOR_DIM,
)

# Re-export memory-vocabulary constants from common.enrichment (CAURA-595).
# common is the source of truth so core-api and core-worker stay in sync.
from common.enrichment.constants import (  # noqa: F401
    DEFAULT_MEMORY_TYPE,
    DEFAULT_MEMORY_WEIGHT,
    MEMORY_STATUSES,
    MEMORY_TYPE_DESCRIPTIONS,
    MEMORY_TYPES,
    MemoryType,
)

# Re-export LLM provider constants from common.llm (CAURA-595).
from common.llm.constants import (  # noqa: F401
    ANTHROPIC_CHAT_BASE_URL,
    ANTHROPIC_DEFAULT_MODEL,
    GEMINI_DEFAULT_MODEL,
    LLM_FALLBACK_MODEL_OPENAI,
    LLM_RETRY_ATTEMPTS,
    LLM_RETRY_DELAY_S,
    OPENAI_CHAT_BASE_URL,
    OPENAI_REQUEST_TIMEOUT_SECONDS,
    OPENROUTER_CHAT_BASE_URL,
    OPENROUTER_DEFAULT_MODEL,
    VERTEX_LLM_DEFAULT_MODEL,
)


def is_mcp_path(path: str) -> bool:
    """True for requests routed to the MCP mount (/mcp, /mcp/*).

    Middlewares that need to opt out of MCP's long-lived streaming
    semantics (security headers, request-wide timeouts) gate on this.
    """
    return path == "/mcp" or path.startswith("/mcp/")


# ── Version ──
# Source-only dev (PYTHONPATH set but no `pip install -e`) has no
# package metadata; fall back to "dev" rather than crashing import.
try:
    VERSION = importlib.metadata.version("core-api")
except importlib.metadata.PackageNotFoundError:
    VERSION = "dev"
OPENAI_EMBEDDING_MODEL = os.environ.get("OPENAI_EMBEDDING_MODEL", "text-embedding-3-small")
EMBEDDING_RETRY_ATTEMPTS = 2
EMBEDDING_RETRY_DELAY_S = 1.0
EMBEDDING_REEMBED_DELAY_S = 30.0
EMBEDDING_REEMBED_BATCH_SIZE = 50
EMBEDDING_CACHE_TTL = 259200  # 3 days — embeddings are deterministic per model+query

# ── Memory ──
# Derived from MEMORY_TYPES (the SoT) so adding a new type in
# common/enrichment/constants.py propagates here without hand-editing.
MEMORY_TYPES_PATTERN = "^(" + "|".join(MEMORY_TYPES) + ")$"
MEMORY_TYPES_DESCRIPTION = (
    "Memory type. Auto-classified by LLM if omitted. Valid values: " + ", ".join(MEMORY_TYPES) + "."
)

# ── Memory status lifecycle ──
MEMORY_STATUSES_PATTERN = (
    r"^(active|pending|confirmed|cancelled"
    r"|outdated|conflicted|archived|deleted)$"
)

# ── Health / status probe timeouts ──
# Upper bound on a single dependency probe (storage / redis / event_bus).
# Cloud Run typically gives health checks 10-30s before marking a revision
# unhealthy; we want to fail-fast well before that so a stalled backend
# can't hang the whole probe. Shared between ``/health`` (binary 503 deploy
# gate) and ``/stats`` / ``/status`` (public endpoints with the same posture
# — return ``0`` / "unreachable" rather than block landing-page hits).
PROBE_TIMEOUT_SECONDS = 5.0

# ── Memory visibility levels ──
# Named constants are the SoT — ``MEMORY_VISIBILITIES`` and the regex below
# derive from them so a rename here propagates to membership checks and
# the Pydantic pattern automatically. SQL filters and any other call
# site should import the named constant rather than the bare string so
# a typo turns into a NameError at import time, not a silent miss.
MEMORY_VISIBILITY_SCOPE_AGENT = "scope_agent"
MEMORY_VISIBILITY_SCOPE_TEAM = "scope_team"
MEMORY_VISIBILITY_SCOPE_ORG = "scope_org"
MEMORY_VISIBILITIES = (
    MEMORY_VISIBILITY_SCOPE_AGENT,
    MEMORY_VISIBILITY_SCOPE_TEAM,
    MEMORY_VISIBILITY_SCOPE_ORG,
)
MEMORY_VISIBILITIES_PATTERN = (
    f"^({MEMORY_VISIBILITY_SCOPE_AGENT}|{MEMORY_VISIBILITY_SCOPE_TEAM}|{MEMORY_VISIBILITY_SCOPE_ORG})$"
)

MAX_CONTENT_LENGTH = 10000
CHUNKING_THRESHOLD_CHARS = 2000  # content above this triggers auto-chunking
MAX_QUERY_LENGTH = 5000

# ── Tool surface bookkeeping ──
# Tool descriptions live inline in `core_api/tools/memclaw_*.py` spec
# modules (the SoT). Nothing else should hold a copy.
# STM tools were dropped in 6fea229; STM_ONLY_TOOLS constant removed.

# ── Search / ranking ──
DEFAULT_SEARCH_TOP_K = 5
MAX_SEARCH_TOP_K = 20
MIN_SEARCH_SIMILARITY = 0.3
FRESHNESS_DECAY_DAYS = 90
FRESHNESS_FLOOR = 0.7
ENTITY_BOOST_FACTOR = 1.3
ENTITY_TOKEN_MIN_LENGTH = 2  # A7: was 3; lowered to retain 2-char acronym
# entities (``AI`` / ``ML`` / ``PR`` / ``UI`` / ``QA`` / ``HR`` /
# ``UK`` / ``US``…). 2-char English fillers (``in`` / ``on`` / ``to``
# / ``be`` / ``is``…) are already in ENTITY_STOPWORDS so the noise
# floor is unchanged; single-letter tokens still drop via the >=2
# check.
ENTITY_STOPWORDS: frozenset[str] = frozenset(
    {
        # ── Determiners / articles ──
        "the",
        "a",
        "an",
        "this",
        "that",
        "these",
        "those",
        "my",
        "your",
        "his",
        "her",
        "its",
        "our",
        "their",
        "some",
        "any",
        "each",
        "every",
        "all",
        "both",
        # ── Pronouns ──
        "i",
        "me",
        "we",
        "us",
        "you",
        "he",
        "him",
        "she",
        "they",
        "them",
        "it",
        "who",
        "whom",
        "what",
        "which",
        "whose",
        "myself",
        "yourself",
        "itself",
        # Indefinite pronouns (common in queries, never entity names)
        "something",
        "anything",
        "everything",
        "nothing",
        "someone",
        "anyone",
        "everyone",
        "nobody",
        "somebody",
        "whatever",
        "whoever",
        "whenever",
        "wherever",
        "however",
        # ── Prepositions ──
        "in",
        "on",
        "at",
        "to",
        "for",
        "of",
        "with",
        "by",
        "from",
        "about",
        "into",
        "through",
        "during",
        "before",
        "after",
        "above",
        "below",
        "between",
        "under",
        "over",
        "against",
        "along",
        "around",
        "among",
        "within",
        "without",
        # ── Conjunctions / connectors ──
        "and",
        "but",
        "or",
        "nor",
        "so",
        "yet",
        "because",
        "although",
        "while",
        "whether",
        "unless",
        "if",
        "than",
        # ── Auxiliary / modal verbs ──
        "is",
        "are",
        "was",
        "were",
        "be",
        "been",
        "being",
        "am",
        "have",
        "has",
        "had",
        "do",
        "does",
        "did",
        "done",
        "will",
        "would",
        "shall",
        "should",
        "may",
        "might",
        "can",
        "could",
        "must",
        # ── Common verbs (query fillers, never entity names) ──
        "tell",
        "show",
        "give",
        "get",
        "let",
        "make",
        "know",
        "think",
        "want",
        "need",
        "find",
        "say",
        "said",
        "look",
        "see",
        "use",
        "help",
        "explain",
        "describe",
        "list",
        "summarize",
        "go",
        "going",
        "went",
        "gone",
        "come",
        "came",
        "keep",
        "kept",
        "got",
        "took",
        "taken",
        "try",
        "tried",
        "happen",
        "happened",
        "seem",
        "seems",
        "feel",
        "mean",
        "means",
        "become",
        "became",
        "bring",
        "brought",
        "put",
        "set",
        "sent",
        "run",
        "left",
        "ask",
        "asked",
        "call",
        "called",
        # ── Adverbs / fillers ──
        "not",
        "no",
        "very",
        "just",
        "also",
        "too",
        "how",
        "when",
        "where",
        "why",
        "here",
        "there",
        "then",
        "now",
        "well",
        "still",
        "already",
        "please",
        "really",
        "actually",
        "basically",
        "probably",
        "maybe",
        "perhaps",
        "definitely",
        "certainly",
        "simply",
        "usually",
        "often",
        "sometimes",
        "always",
        "never",
        "ever",
        "likely",
        "enough",
        "rather",
        "quite",
        "pretty",
        "even",
        "only",
        "almost",
        "nearly",
        # ── Quantifiers / degree ──
        "more",
        "less",
        "much",
        "many",
        "most",
        "least",
        "few",
        "several",
        "lot",
        "lots",
        # ── Temporal (query modifiers, never entity names) ──
        "today",
        "yesterday",
        "tomorrow",
        "week",
        "month",
        "year",
        "ago",
        "recently",
        "soon",
        "later",
        "earlier",
        "currently",
        "previous",
        "recent",
        "last",
        "next",
        # ── Generic nouns (too vague for entity names) ──
        # Mirrors ENTITY_NAME_BLOCKLIST — these words are blocked from
        # becoming entity names, so searching for them is wasted work.
        "team",
        "meeting",
        "project",
        "system",
        "process",
        "approach",
        "update",
        "issue",
        "change",
        "result",
        "group",
        "company",
        "person",
        "user",
        "client",
        "thing",
        "stuff",
        "idea",
        "work",
        "code",
        # Additional vague nouns common in queries
        "way",
        "bit",
        "kind",
        "type",
        "sort",
        "information",
        "question",
        "answer",
        "detail",
        "details",
        "example",
        "problem",
        "point",
        "case",
        "status",
        "report",
        "data",
        "overview",
        "summary",
        "topic",
        # ── Generic adjectives (query modifiers, not entity names) ──
        "different",
        "same",
        "other",
        "another",
        "good",
        "bad",
        "best",
        "worst",
        "new",
        "old",
    }
)
GRAPH_HOP_BOOST = {
    0: 1.3,
    1: 1.2,
    2: 1.1,
}  # boost factor per hop distance (0 = direct match)
GRAPH_MAX_BOOSTED_MEMORIES = 50  # cap on memories receiving graph boost (prevents popular-entity fan-out)
# CAURA-698: cap on entity FTS matches that triggers the ENTITY_LOOKUP
# short-circuit. Above this, the query almost certainly did not name a
# specific entity (it matched broadly against a dense entity index); the
# precision argument for entity_lookup breaks down at high match counts,
# so fall through to keyword/semantic search instead.
#
# Calibrated against etoro prod data (2026-06-02, 6h, 98 user_or_assistant
# queries). The distribution is bimodal with a wide empty gap between
# ~75 and ~500 matches, so any threshold in that range is equivalent on
# this dataset. Sampling the actual queries in each band revealed they
# are all multi-keyword topical searches (e.g., "DeFi web3 smart contract",
# "docker caddy dns", "marketing campaign email creative") — none name a
# specific entity. T=20 lands the entity_lookup rate in the 20-30% target
# band on user traffic while still allowing low-count cases that might
# represent legitimate "name a thing" queries on other tenants' data.
# Tune via prod measurement, not bench alone.
ENTITY_LOOKUP_MAX_MATCHES = 20

FTS_WEIGHT = 0.3  # blend: (1 - FTS_WEIGHT) * vector + FTS_WEIGHT * keyword
FTS_WEIGHT_BOOSTED = 0.6  # for short specific queries (1-3 proper nouns / identifiers)
FTS_BOOST_MAX_TOKENS = 3  # queries with more meaningful tokens than this stay at FTS_WEIGHT
FTS_BOOST_SPECIFICITY_RATIO = 0.4  # strict >; at N=2 this means >=1 specific token triggers boost
SIMILARITY_BLEND = 0.85  # base_score = SIMILARITY_BLEND * similarity + (1 - SIMILARITY_BLEND) * weight (raised from 0.75 — LoCoMo sweep showed +13pp recall)
SEARCH_OVERFETCH_FACTOR = 2  # fetch top_k * N candidates from storage, trim to top_k after min_similarity filter — gives post-filter headroom
# A26: recall_count is bumped for every RETURNED row, used or not (see
# TrackRecalls + memory_increment_recall), and feeds recall_boost back into the
# rank score — a self-reinforcing "returned → boosted → returned" loop with no
# usefulness signal. Until a confirmation-gated bump lands (the real fix, tied to
# D5/D1), the cap is dialed down so the boost can no longer hijack rankings: at
# cap=1.1 a popular-but-useless row can only overtake a more-relevant one whose
# base score is <10% higher (was <50% at cap=1.5), and the shorter decay window
# lets stale popularity fade in ~2 weeks instead of a quarter.
RECALL_BOOST_CAP = 1.1  # max multiplier from frequent recall (A26: dialed down from 1.5)
RECALL_DECAY_WINDOW_DAYS = 14  # only recalls within this window contribute to boost (A26: from 90)

# ── Recall summary ──
MEMORY_RECALL_SUMMARY_TEMPERATURE = 0.3
MEMORY_RECALL_SUMMARY_MAX_TOKENS = 1000

# ── Insights ──
INSIGHTS_MAX_MEMORIES = 50  # max memories per analysis pass (token budget ~10k)
INSIGHTS_TEMPERATURE = 0.3  # analytical, not creative
INSIGHTS_DISCOVER_SAMPLE_SIZE = 200  # memories to sample for vector clustering
INSIGHTS_DISCOVER_CLUSTERS = 6  # k-means cluster count for discover mode
INSIGHTS_FOCUS_MODES = ("contradictions", "failures", "stale", "divergence", "patterns", "discover")

# Shared scope enum used by memclaw_list, memclaw_insights, memclaw_evolve and
# their REST counterparts. Trust-level gating per scope lives in the individual
# handlers (trust_service.require_trust); this tuple is the single source of
# truth for "what values are accepted".
VALID_SCOPES = ("agent", "fleet", "all")

# ── Evolve (Karpathy Loop) ──
EVOLVE_SUCCESS_DELTA = 0.1  # weight increase on success
EVOLVE_FAILURE_DELTA = -0.15  # weight decrease on failure (asymmetric — failures propagate faster)
EVOLVE_PARTIAL_DELTA = 0.03  # slight nudge for partial outcomes
EVOLVE_WEIGHT_FLOOR = 0.05  # never reduce weight below this (archival value)
EVOLVE_WEIGHT_CAP = 1.0  # never increase weight above this
EVOLVE_RULE_CONFIDENCE_THRESHOLD = 0.5  # min LLM confidence to persist a generated rule
EVOLVE_RULE_TEMPERATURE = 0.3  # analytical rule generation
EVOLVE_OUTCOME_TYPES = ("success", "failure", "partial")
EVOLVE_MAX_RELATED_IDS = 50  # cap on memories touched per evolve call

MEMORY_RECALL_SUMMARY_NUM_SENTENCES = 10

# ── Pagination ──
DEFAULT_LIST_LIMIT = 50
MAX_LIST_LIMIT = 500
DEFAULT_AUDIT_LIMIT = 50
MAX_AUDIT_LIMIT = 200
DEFAULT_ENTITY_LIMIT = 100

# ── Tier limits ──
TIER_LIMITS = {
    "free": {
        "max_memories": 10_000,
        "writes_per_month": 5_000,
        "searches_per_month": 5_000,
    },
    "pro": {
        "max_memories": 250_000,
        "writes_per_month": 25_000,
        "searches_per_month": 50_000,
    },
    "business": {
        "max_memories": 1_000_000,
        "writes_per_month": 100_000,
        "searches_per_month": 500_000,
    },
    "custom": {
        "max_memories": None,
        "writes_per_month": None,
        "searches_per_month": None,
    },
}

# ── Agent trust levels ──
TRUST_LEVELS = {
    0: "restricted",
    1: "standard",
    2: "cross_fleet",
    3: "admin",
}
DEFAULT_TRUST_LEVEL = 1
MIN_TRUST_LEVEL = 0
MAX_TRUST_LEVEL = 3

# ── Auth ──
API_KEY_HEADER = "X-API-Key"
API_KEY_PREFIX = "mc_"
API_KEY_HASH_DISPLAY_LENGTH = 12

# ── Fleet heartbeat ──
HEARTBEAT_INTERVAL_SECONDS = 60
NODE_STALE_SECONDS = 90
NODE_OFFLINE_SECONDS = 300

# ── Memory Crystallizer ──
CRYSTALLIZER_STALE_DAYS = 180
CRYSTALLIZER_STALE_MAX_WEIGHT = 0.3
CRYSTALLIZER_DEDUP_THRESHOLD = 0.95
CRYSTALLIZER_SHORT_CONTENT_CHARS = 10
CRYSTALLIZER_LOW_EMBEDDING_COVERAGE_PCT = 90
CRYSTALLIZER_HIGH_PENDING_PCT = 20
CRYSTALLIZER_HIGH_PII_COUNT = 10
CRYSTALLIZER_MAX_BATCH_SIZE = 50  # max memories per crystallization batch
CRYSTALLIZER_MIN_CLUSTER_SIZE = 3  # min memories in a cluster to trigger crystallization
CRYSTALLIZER_DEDUP_BATCH_SIZE = 500  # memories per ANN batch during dedup scan
CRYSTALLIZER_DEDUP_NEIGHBORS = 5  # top-K neighbors to check per memory
CRYSTALLIZER_MAX_DEDUP_PAIRS = 1000  # safety valve: cap total near-dup pairs per run

# ── Bulk write ──
BULK_MAX_ITEMS = 100  # max memories per bulk request
BULK_EMBEDDING_CONCURRENCY = 10  # max parallel embedding calls in bulk mode
BULK_ENRICHMENT_CONCURRENCY = 10  # max parallel enrichment calls in bulk mode
# Outer cap on the whole enrichment gather. One hung provider call would
# otherwise stall the batch; on timeout, completed slots keep their values
# and pending ones stay None (same as a per-item provider error). Should
# stay below `settings.request_timeout_seconds` in config.py so this fires
# before the outer request budget.
BULK_ENRICHMENT_TOTAL_TIMEOUT_SECONDS = 30.0
# Outer cap on the embedding-batch call in the bulk path. Embed runs
# *before* enrichment in ``create_memories_bulk`` (CAURA-595 sequencing),
# so the worst-case time-to-storage is ``embed + enrich``. The validator
# in config.py uses both this and the enrichment cap to prove the
# ``storage_bulk_timeout_seconds`` per-phase deadline can fire before the
# umbrella ``bulk_request_timeout_seconds``.
BULK_EMBEDDING_TIMEOUT_SECONDS = 30.0

# ── Lifecycle automation ──
LIFECYCLE_INTERVAL_HOURS = 24  # run lifecycle cycle every N hours
LIFECYCLE_BATCH_SIZE = 500  # max memories per status transition batch
# ``LIFECYCLE_STALE_ARCHIVE_WEIGHT`` is re-exported from
# ``common.constants`` (see top of this file) — canonical location is
# ``common`` so core-worker can read the same value without depending
# on core-api.

# ── Entity extraction quality filter ──
MIN_ENTITY_NAME_LENGTH = 2  # single-char "entities" are never meaningful
ENTITY_NAME_BLOCKLIST: frozenset[str] = frozenset(
    {
        "team",
        "meeting",
        "project",
        "system",
        "process",
        "approach",
        "update",
        "issue",
        "change",
        "result",
        "group",
        "company",
        "person",
        "user",
        "client",
        "thing",
        "stuff",
        "idea",
        "work",
        "code",
    }
)

# ── Entity resolution (embedding-based) ──
ENTITY_RESOLUTION_THRESHOLD = 0.85  # cosine similarity above this → same entity

# ── Cross-memory entity linking ──
ENTITY_EMBEDDING_BACKFILL_BATCH_SIZE = 100
ENTITY_RESOLUTION_BATCH_SIZE = 100
CROSS_LINK_SIMILARITY_THRESHOLD = 0.75
CROSS_LINK_TEXT_VERIFY = True
CROSS_LINK_MEMORY_BATCH_SIZE = 200
MIN_COOCCURRENCE_FOR_RELATION = 2
RELATION_REINFORCE_DELTA = 0.1
MAX_RELATION_WEIGHT = 1.0
RELATION_INFERENCE_BATCH_SIZE = 500
