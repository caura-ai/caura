"""Centralised constants for the MemClaw API."""

import importlib.metadata
import os
from pathlib import Path

# Re-export DB-query constants from common (shared with core-storage-api).
from common.constants import (  # noqa: F401
    CONTRADICTION_CANDIDATE_MAX,
    CONTRADICTION_SIMILARITY_THRESHOLD,
    CRYSTALLIZER_SHORT_CONTENT_CHARS,
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
    SQL_SCORING_PARAM_KEYS,
    TYPE_DECAY_DAYS,
    VECTOR_DIM,
)

# The embedding concurrency-gate timeout, which the bulk strong-embed budget
# below must stay ordered above — see BULK_STRONG_EMBED_TIMEOUT_SECONDS.
from common.embedding.constants import EMBEDDING_GATE_TIMEOUT_SECONDS

# Re-export memory-vocabulary constants from common.enrichment (CAURA-595).
# common is the source of truth so core-api and core-worker stay in sync.
from common.enrichment.constants import (  # noqa: F401
    CLASSIFIER_DEPRECATED_MEMORY_TYPES,
    DEFAULT_MEMORY_TYPE,
    DEFAULT_MEMORY_WEIGHT,
    MEMORY_STATUSES,
    MEMORY_TYPE_DESCRIPTIONS,
    MEMORY_TYPES,
    SERVER_RESERVED_MEMORY_TYPES,
    MemoryType,
)
from common.env_utils import read_float_env

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
def _resolve_version() -> str:
    """Resolve the running service version.

    Precedence (most to least authoritative):

    1. ``MEMCLAW_VERSION`` env — explicit deploy/ad-hoc override.
    2. ``VERSION`` file baked into the image at build time from
       ``pyproject.toml`` (see ``core-api/Dockerfile``). Deterministic and
       independent of installed-package metadata — the prod Dockerfile
       installs deps via ``uv export --no-emit-project`` (the project
       itself is never installed), so ``importlib.metadata`` finds no
       ``core-api`` dist and the endpoint silently served ``"dev"``.
    3. Installed package metadata — editable dev installs (``pip install -e``).
    4. ``"dev"`` — source-only checkout with none of the above.
    """
    env = os.environ.get("MEMCLAW_VERSION")
    if env and env.strip():
        return env.strip()
    # constants.py → core_api → src → core-api → repo root (image: /app).
    version_file = Path(__file__).resolve().parents[3] / "VERSION"
    try:
        stamped = version_file.read_text().strip()
        if stamped:
            return stamped
    except OSError:
        pass
    try:
        return importlib.metadata.version("core-api")
    except importlib.metadata.PackageNotFoundError:
        return "dev"


VERSION = _resolve_version()
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

# Types a caller may CREATE: the full vocabulary minus server-reserved types
# (outcome/rule/insight — authored only by internal flows and rejected at the
# write boundary) and classifier-deprecated types (semantic — folded into
# ``fact``; CAURA-701/702). Kept in ``MEMORY_TYPES`` order.
MEMORY_TYPES_WRITE = tuple(
    t
    for t in MEMORY_TYPES
    if t not in SERVER_RESERVED_MEMORY_TYPES and t not in CLASSIFIER_DEPRECATED_MEMORY_TYPES
)
# Shown on WRITE fields (create/update): only the types a caller may set. Keeps
# deprecated/reserved values out of the schema so agents stop picking them.
MEMORY_TYPES_WRITE_DESCRIPTION = (
    "Memory type. Auto-classified by LLM if omitted. Valid values: " + ", ".join(MEMORY_TYPES_WRITE) + "."
)
# Shown on the recall/list FILTER field: any type that may EXIST in storage,
# including historical rows written before a type was reserved/deprecated
# (legacy ``semantic``/``insight``/… rows stay queryable).
MEMORY_TYPES_FILTER_DESCRIPTION = (
    "Filter results to a single memory type. Valid values: " + ", ".join(MEMORY_TYPES) + "."
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

# ── Doc-derived memories ──
# ``memclaw_doc(op="write")`` mints a memory carrying the document body so the
# body becomes reachable by MEANING (recall / recall-brief read
# ``memories.content`` directly). Docs themselves are only searchable via their
# ``data["summary"]`` embedding, and ``memclaw_recall`` never returns documents.
#
# The memory content is the doc body VERBATIM, so the cutoff is simply the
# memory schema ceiling: mint whenever the body fits in a memory at all.
# Over-cutoff bodies are SKIPPED rather than truncated — a truncated body reads
# as complete and would produce confidently wrong downstream conclusions.
DOC_MEMORY_MAX_CHARS = MAX_CONTENT_LENGTH

# NOTE: there is deliberately no ``DOC_MEMORY_TYPE``. Doc-derived memories pass
# no ``memory_type`` at all, so the enrichment classifier assigns one per
# document — a decision record becomes ``decision``, a runbook ``rule``, an
# incident writeup ``episode``. Pinning every document to a single type would
# throw that away. (And ``reference`` was never an option: it is not a
# MemoryType, and the ``memories_memory_type_check`` CHECK constraint from
# migration 013 would reject it.)

# Provenance only — written to ``memories.source_uri`` as
# ``memclaw-doc://<collection>/<doc_id>``. Nothing queries it yet (that would
# need a storage-side filter + index); it exists so a later reconciliation
# mechanism has a stable key to match a doc to the memories minted from it.
DOC_MEMORY_URI_SCHEME = "memclaw-doc"

assert DOC_MEMORY_MAX_CHARS <= MAX_CONTENT_LENGTH, (
    "DOC_MEMORY_MAX_CHARS must not exceed MemoryCreate.content max_length "
    f"({MAX_CONTENT_LENGTH}) — an over-cap spec would fail schema validation."
)

# ── Tool surface bookkeeping ──
# Tool descriptions live inline in `core_api/tools/memclaw_*.py` spec
# modules (the SoT). Nothing else should hold a copy.
# STM tools were dropped in 6fea229; STM_ONLY_TOOLS constant removed.

# ── Search / ranking ──
DEFAULT_SEARCH_TOP_K = 5
MAX_SEARCH_TOP_K = 20
MIN_SEARCH_SIMILARITY = 0.3
# A49: cosine-dominant candidate selection. 0 = OFF (the first-stage candidate pool
# is selected by the boost-distorted ``score`` — current behaviour). >0 = select the
# candidate pool by semantic relevance (``similarity``, boost-free) at this size, so
# boost-demoted-but-strong matches survive the LIMIT into the ranking/rerank (A50)
# stage instead of being evicted by the boost stack. Enable per-tenant via
# ``default_search_profile.candidate_pool_size``; keep 0 globally until the A49/A50
# A/B validates it. Offline ceiling (LongMemEval): pool=50 recovers 95% of the gold
# production dropped from top-20; >70 adds nothing. See benchmark/a49_ceiling_check.py.
CANDIDATE_POOL_SIZE = 0
# Ranking formula selector (A50 unified). 0 = LEGACY multiplicative boost stack
# (score = base_score x freshness x recall x temporal x date x currency x status --
# current behaviour). 1 = UNIFIED relevance-dominant formula: score = similarity plus
# BOUNDED, query-gated ADDITIVE nudges (freshness gated to temporal queries, date/entity
# gated to their triggers), minus a staleness penalty, times status_penalty; drops the
# 0.15·weight floor (A46) and popularity recall_boost (A41). Off by default; flip per
# tenant via default_search_profile.score_formula to A/B old vs new on the benchmark.
# Rationale + offline calibration: docs/ranking/unified-ranking-formula.md.
SCORE_FORMULA = 0
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
# Scale applied to ts_rank_cd BEFORE the saturating map, putting fts_score on the
# same scale as cosine so FTS_WEIGHT above bites in effect and not just in name
# (#687). Division of labour: FTS_WEIGHT is the *preference* between the two
# signals; this is the *unit conversion* that makes the preference meaningful.
#
# The input to the derivation: migration 001 builds ``search_vector`` as a bare
# ``to_tsvector('english', content)`` with no ``setweight``, so every lexeme is
# weight D = 0.1 and a modal single-occurrence match scores ts_rank_cd = 0.1 →
# 0.0909, against measured cosine of 0.35-0.39. Re-derive if that trigger starts
# weighting lexemes; changing FTS_WEIGHT alone does NOT require it.
#
# Why 6: derived, not tuned. k*0.1/(1+k*0.1) ≈ 0.37 lands a modal match in the
# cosine range. Do not treat it as a free dial.
#
# Calibrated at the MODE, not across the distribution: ts_rank_cd's cover-density
# penalty is not a constant factor, so a spread-out multi-term match still lands
# well below the band. That residual is arguably right — spread terms are a worse
# match — but it means "effective ≈ nominal weight" describes the typical query,
# not a global property. A corpus of spread-term queries will measure lower; that
# is not the scale being broken.
#
# Measured before shipping — see BENCHMARKS.md § First-stage retrieval, and note
# there that the effect rises monotonically across the whole range swept, so the
# benchmark does NOT locate an optimum. 6 is the derivation's answer, not the
# sweep's. 1.0 reproduces the pre-#687 formula exactly; per-tenant override lives
# at ``search.default_profile.fts_rank_scale``.
FTS_RANK_SCALE = 6.0
FTS_WEIGHT_BOOSTED = 0.6  # for short specific queries (1-3 proper nouns / identifiers)
FTS_BOOST_MAX_TOKENS = 3  # queries with more meaningful tokens than this stay at FTS_WEIGHT
FTS_BOOST_SPECIFICITY_RATIO = 0.4  # strict >; at N=2 this means >=1 specific token triggers boost
SIMILARITY_BLEND = 0.85  # base_score = SIMILARITY_BLEND * similarity + (1 - SIMILARITY_BLEND) * weight (raised from 0.75 — LoCoMo sweep showed +13pp recall)
SEARCH_OVERFETCH_FACTOR = 2  # fetch top_k * N candidates from storage, trim to top_k after min_similarity filter — gives post-filter headroom
FTS_ONLY_RESERVED_RESULTS = 1  # #687: result slots held for rows that FTS-match but are not embedded yet (transient deferred-embed window); 0 restores the pre-#687 head-slice behaviour
# ``SQL_SCORING_PARAM_KEYS`` — the set both search-path builders project through
# before sending ``search_params`` — is re-exported from ``common.constants``
# above, because storage reads the same set and the drift that matters is
# set-against-SQL.
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
# Hard cap on recall-summary generation. The recall LLM call is non-streaming and
# output-bound, so the full generation time is exposed to the caller; this ceiling
# bounds the worst-case p95+ tail (prod /recall was routinely 5-15s, traced to long
# summary generations on the recall model). Halved from 1000 to 500: kept at 500
# rather than lower because RECALL_PROMPT still emits step-by-step reasoning BEFORE
# the answer, so too tight a cap would truncate the answer itself. If that
# chain-of-thought is later trimmed from the prompt, this can drop toward ~400.
MEMORY_RECALL_SUMMARY_MAX_TOKENS = 500

# ── Insights ──
INSIGHTS_MAX_MEMORIES = 50  # max memories per analysis pass (token budget ~10k)
INSIGHTS_TEMPERATURE = 0.3  # analytical, not creative
INSIGHTS_DISCOVER_SAMPLE_SIZE = 200  # memories to sample for vector clustering
# Trailing window the discover sample is spread over. Without it the sample is
# "newest N", which on a busy tenant spans hours — discover then clusters
# yesterday's activity instead of the corpus, and consecutive nightly runs
# re-find the same shapes in near-identical slices.
INSIGHTS_DISCOVER_WINDOW_DAYS = 30
INSIGHTS_DISCOVER_CLUSTERS = 6  # k-means cluster count for discover mode
# Trailing window for the patterns read. Semantically aligned with the mode
# (the prompt analyzes "recent" memories) — but primarily a cost bound: the
# title-dedup subquery computes window functions over every row matching the
# filters, so an unbounded patterns read would scan the tenant's whole active
# history to return 50 deduped rows (pre-dedup it early-terminated on the
# (tenant_id, created_at DESC) index). Separate from the discover window so
# the two modes stay independently tunable.
INSIGHTS_PATTERNS_WINDOW_DAYS = 30
# Same cost bound for the failures and stale reads (their weight/recall
# predicates prune less and less as the corpus grows; the dedup subquery has
# no inner LIMIT). 90 days — deliberately wider than the stale mode's own
# 30/14-day age thresholds, which rows must EXCEED to qualify: stale now
# surfaces the 30-90-day "recently became stale" band instead of perpetually
# re-reporting the same ancient tail (which is the archive-stale lifecycle
# job's business, not nightly reporting's).
INSIGHTS_FAILURES_WINDOW_DAYS = 90
INSIGHTS_STALE_WINDOW_DAYS = 90
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
# CRYSTALLIZER_SHORT_CONTENT_CHARS now lives in common/ (re-exported above) —
# core-storage-api needs the same bound to list short-content candidates.
CRYSTALLIZER_LOW_EMBEDDING_COVERAGE_PCT = 90
CRYSTALLIZER_HIGH_PENDING_PCT = 20
CRYSTALLIZER_HIGH_PII_COUNT = 10
CRYSTALLIZER_MAX_BATCH_SIZE = 50  # max memories per crystallization batch
CRYSTALLIZER_MIN_CLUSTER_SIZE = 3  # min memories in a cluster to trigger crystallization
CRYSTALLIZER_DEDUP_BATCH_SIZE = 500  # memories per ANN batch during dedup scan
CRYSTALLIZER_DEDUP_NEIGHBORS = 5  # top-K neighbors to check per memory
CRYSTALLIZER_MAX_DEDUP_PAIRS = 1000  # safety valve: cap total near-dup pairs per run

# ── Interviewer (Phase 1) ──
# Scheduled reflective work-reports (docs/plans/interviewer-phase1-decisions.md).
INTERVIEW_MAX_EVENTS_PER_SUBMIT = (
    500  # plugin-side submit cap; the cursor-driven catch-up loop drains any backlog
)
INTERVIEW_EVENT_MAX_CHARS = 8_000  # per-event content truncation before masking/prompting
INTERVIEW_CHUNK_MAX_CHARS = 96_000  # ~24k tokens per map-phase chunk
INTERVIEW_MAX_ITEMS_PER_SECTION = 15  # 6 sections x 15 = 90, safely under BULK_MAX_ITEMS
INTERVIEW_MAX_KEYSTONES_IN_PROMPT = 8
INTERVIEW_TEMPERATURE = 0.2

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

# Margin over the concurrency gate for the opportunistic embed's default budget,
# and the floor that margin can't take it below.
_STRONG_EMBED_GATE_MARGIN_SECONDS = 3.0
_STRONG_EMBED_MIN_SECONDS = 8.0


def _default_strong_embed_timeout(gate_seconds: float, required_seconds: float) -> float:
    """Default budget for the opportunistic bulk embed.

    Derived from the gate rather than fixed, so raising
    ``EMBEDDING_GATE_TIMEOUT_SECONDS`` — an operator's env var — can never leave a
    deployment unable to start. A fixed literal here would do exactly that: any
    install that had already raised the gate past it would fail the startup
    ordering check on upgrade, fixable only by a code change.

    Must land STRICTLY between the two, since that is what the startup validator
    enforces: ``gate < budget < required``. Clamping to ``required_seconds`` is not
    enough — for a gate within the margin of the cap that clamp lands exactly ON
    the cap, and the validator then refuses to start for an operator who only
    raised the gate to a legitimate value below it. So when the full margin doesn't
    fit, take the midpoint of the remaining room, which is strictly inside the
    bound for any ``gate < required``.

    A gate at or above the cap has no such room, and is incoherent on its own terms
    — it would outlive the embed it gates. That returns ``required_seconds`` and
    lets the validator say so rather than papering over it.
    """
    preferred = max(_STRONG_EMBED_MIN_SECONDS, gate_seconds + _STRONG_EMBED_GATE_MARGIN_SECONDS)
    if preferred < required_seconds:
        return preferred
    if gate_seconds < required_seconds:
        return (gate_seconds + required_seconds) / 2.0
    return required_seconds


# Cap on the *opportunistic* bulk embed — the one a ``write_mode="strong"`` item
# triggers on a deployment that otherwise defers. Much tighter than the required
# cap above, because this branch's documented fallback is "defer to the backfill
# anyway": spending 30s to reach an outcome that was free is latency charged to
# every other item in the batch, none of which asked for inline embedding.
#
# Must stay ABOVE ``EMBEDDING_GATE_TIMEOUT_SECONDS``, which is deliberately set
# below callers' deadlines so a saturated concurrency gate surfaces as
# attributable backpressure rather than an anonymous caller timeout. Drop below it
# and every gate-saturated strong write is reported as a generic embed failure.
#
# The default is derived from that gate so the ordering holds however the operator
# tunes it, and is env-overridable so a deployment that needs a specific value has
# a config-only lever rather than a code change. ``_validate_timeout_ordering`` in
# config.py still rejects an explicit override that conflicts with the gate.
BULK_STRONG_EMBED_TIMEOUT_SECONDS = read_float_env(
    "BULK_STRONG_EMBED_TIMEOUT_SECONDS",
    _default_strong_embed_timeout(EMBEDDING_GATE_TIMEOUT_SECONDS, BULK_EMBEDDING_TIMEOUT_SECONDS),
)

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


def _relation_weight(relation_type: str, row_weight: float) -> float:
    """Compute effective weight for a relation edge.

    Combines the per-type semantic weight (from RELATION_TYPE_WEIGHTS) with the
    per-row weight stored in the DB (default 1.0). Relocated here from the
    deleted ``repositories`` package (Fix 2 final cleanup).
    """
    type_w = RELATION_TYPE_WEIGHTS.get(relation_type.lower(), DEFAULT_RELATION_TYPE_WEIGHT)
    return type_w * row_weight
