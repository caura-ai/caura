"""Constants shared between core-api and core-storage-api.

Mostly DB-query values both services need to agree on. Also ``SEARCH_KNOBS``
and the wire contract derived from it: storage reads only the derived key
tuples, but the knob table is one declaration on purpose — splitting the
bounds into core-api and the flags into here would put the knob NAME in two
files, which is the drift it exists to remove.
"""

from datetime import timedelta
from typing import NamedTuple

from common.env_utils import read_float_env, read_int_env

# ── Memory liveness ──
# The statuses that mean "this memory is live". Broader than the literal
# ``active`` value: enrichment promotes writes past ``active`` on the normal
# path (the classifier assigns ``confirmed`` to outcome-shaped content and
# ``pending`` to task/plan/commitment content, and the crystallizer writes
# ``confirmed`` directly), so a liveness check that tests ``status ==
# "active"`` silently drops enriched rows. Excludes the terminal/shelved
# states (``cancelled``, ``outdated``, ``conflicted``, ``archived``,
# ``deleted``). Callers that genuinely want one exact status filter on it
# explicitly instead.
LIVE_MEMORY_STATUSES = ("active", "confirmed", "pending")

# ── Embeddings ──
# Native dim of the default embedder (BAAI/bge-m3, see local-embedder docs).
# Schema upgrade lives in alembic migration 012_vector_dim_1024.py — keep
# this constant in lock-step with that migration.
VECTOR_DIM = 1024

# ── Write-time semantic dedup ──
SEMANTIC_DEDUP_THRESHOLD = 0.95  # cosine similarity above this -> near-duplicate
SEMANTIC_DEDUP_CANDIDATE_LIMIT = 1  # only need to know if any match exists

# Two-tier dedup constants (A1 #15). The dispatch that consumes them
# lives in A1 #16. Today's single-tier code keeps using
# ``SEMANTIC_DEDUP_THRESHOLD`` above — this PR adds the band cutoffs
# without changing any call site.
#
# Decision band (cosine similarity to nearest existing memory):
#   ≥ AUTO          → auto-reject (clear duplicate; no LLM)
#   JUDGE ≤ s < AUTO → LLM judge decides (A1 #16; A4 #12 verdict+confidence)
#   < JUDGE         → accept (not a dup)
SEMANTIC_DEDUP_AUTO_THRESHOLD = 0.97  # auto-reject band — tighter than the
# legacy 0.95 so refinements (same first sentence + extra detail) drop into
# the judge band rather than getting silently rejected.
SEMANTIC_DEDUP_JUDGE_THRESHOLD = 0.85  # broad enough to catch refinements,
# tight enough that the LLM judge isn't swamped with unrelated memories that
# happen to share vocabulary.

# ── Contradiction detection ──
# A63 — cosine floor for semantic contradiction CANDIDATES (the rows the
# LLM judge gets to look at). Lowered 0.70 -> 0.45 on measured evidence:
# four genuine, deliberately paraphrased updates written through the real
# local pipeline scored 0.483 / 0.503 / 0.568 / 0.607 against the facts
# they supersede — EVERY ONE below the old 0.70 floor, so Path A returned
# ZERO candidates for all four (logs: candidates_initial=0) and the judge
# never saw the contradiction it exists to catch. That is the STALE
# Type-II miss class: an update phrased in different words than the fact
# it replaces.
#
# Why this is close to free rather than a cost increase: the candidate
# query is ``WHERE (1 - distance) >= threshold ORDER BY distance LIMIT
# <max>``, so the LIMIT — not the threshold — bounds how many rows the
# judge sees. Lowering the floor cannot add a 21st candidate; it changes
# WHICH rows fill the window (and takes the sparse case from 0 to a few).
# Judge output per clean candidate is ~1.5 tokens post-E4 (#1011), so
# even the sparse-case delta is negligible.
#
# Precision is the judge's job, and it now does it: #1023 removed the
# Gate-2 veto that was overriding true update verdicts, so a wider net
# gets adjudicated rather than rubber-stamped. Env-tunable for
# incident-time adjustment without a deploy.
CONTRADICTION_SIMILARITY_THRESHOLD = read_float_env(
    "CONTRADICTION_SIMILARITY_THRESHOLD", 0.45
)
CONTRADICTION_CANDIDATE_MAX = (
    8  # max similar memories to check (LLM is the quality gate)
)
# A63 — the window the /similar-candidates ROUTE applies, which is what prod
# actually runs: the route's caller (the contradiction detector) passes no
# ``limit``, and the route's own default has always been 20, not
# ``CONTRADICTION_CANDIDATE_MAX``'s 8. Named here so the value prod uses is
# declared rather than buried as a literal in the route, and so the
# cost-bounding argument on the threshold above points at something real.
# Deliberately left at 20: E3/E4 established this window's cost, and A63
# widens the FLOOR, not the window.
CONTRADICTION_CANDIDATE_WINDOW = read_int_env("CONTRADICTION_CANDIDATE_WINDOW", 20)

# ── Recall boost ──
RECALL_BOOST_SCALE = 10  # recalls needed to reach half of max boost

# ── Per-type decay windows ──
TYPE_DECAY_DAYS: dict[str, int] = {
    "preference": 365,
    "decision": 180,
    "fact": 120,
    "semantic": 120,
    "commitment": 120,
    "outcome": 90,
    "plan": 60,
    "intention": 60,
    "episode": 45,
    "task": 30,
    "action": 30,
    "cancellation": 14,
    "rule": 365,
    "insight": 90,
}

# ── Entity resolution (embedding-based) ──
ENTITY_RESOLUTION_CANDIDATE_LIMIT = 3  # max candidates to evaluate

# ── Knowledge graph ──
GRAPH_MAX_HOPS = 2  # max relation hops to expand during search
GRAPH_MAX_EXPANDED_ENTITIES = (
    200  # cap on entity IDs in the IN clause after graph expansion
)

# Relation-type weights: strong semantic relations boost more than structural ones.
RELATION_TYPE_WEIGHTS: dict[str, float] = {
    # Strong -- direct actionable connections
    "manages": 1.0,
    "works_on": 1.0,
    "created_by": 1.0,
    "authored_by": 1.0,
    "owns": 1.0,
    "leads": 1.0,
    "reports_to": 1.0,
    # Medium -- useful but weaker signal
    "uses": 0.7,
    "depends_on": 0.7,
    "belongs_to": 0.7,
    "part_of": 0.7,
    "related_to": 0.5,
    "mentions": 0.5,
    # Weak -- structural/geographic, rarely query-relevant
    "located_in": 0.3,
    "contains": 0.3,
    "instance_of": 0.3,
}
DEFAULT_RELATION_TYPE_WEIGHT = 0.5  # unknown relation types get neutral weight

# Predicates where only one value can be true at a time for a given subject.
SINGLE_VALUE_PREDICATES: frozenset[str] = frozenset(
    {
        # -- Identity & status --
        "named",
        "name",
        "renamed_to",
        "status",
        "has_status",
        "current_status",
        "phase",
        "current_phase",
        "state",
        "current_state",
        "role",
        "has_role",
        "current_role",
        "title",
        "has_title",
        "job_title",
        "type",
        "has_type",
        "category",
        "classified_as",
        "identified_as",
        "labeled_as",
        # -- Location & position --
        "lives_in",
        "located_in",
        "is_located_in",
        "location",
        "current_location",
        "headquartered_in",
        "hq_in",
        "based_in",
        "is_based_in",
        "resides_in",
        "resides_at",
        "stationed_at",
        "hosted_on",
        "hosted_at",
        "is_hosted_on",
        "deployed_to",
        "deployed_at",
        "deployed_on",
        "is_deployed_to",
        "runs_on",
        "running_on",
        "stored_in",
        "stored_at",
        "registered_in",
        "registered_at",
        "country",
        "city",
        "region",
        "address",
        # -- Hierarchy & singular roles --
        "reports_to",
        "reporting_to",
        "led_by",
        "owned_by",
        "managed_by",
        "manager",
        "manager_of",
        "headed_by",
        "head_of",
        "ceo_of",
        "ceo",
        "cto_of",
        "cto",
        "cfo_of",
        "cfo",
        "assigned_to",
        "assignee",
        "is_assigned_to",
        "maintained_by",
        "maintainer",
        "maintainer_of",
        "supervised_by",
        "supervisor",
        # -- Metrics, scores & measurements --
        "score",
        "scored",
        "has_score",
        "rating",
        "rated",
        "has_rating",
        "price",
        "priced_at",
        "has_price",
        "current_price",
        "cost",
        "costs",
        "has_cost",
        "value",
        "valued_at",
        "has_value",
        "net_worth",
        "weight",
        "weighs",
        "has_weight",
        "market_cap",
        "has_market_cap",
        "market_cap_rank",
        "revenue",
        "has_revenue",
        "annual_revenue",
        "monthly_revenue",
        "salary",
        "has_salary",
        "compensation",
        "budget",
        "has_budget",
        "funding",
        "total_funding",
        "valuation",
        "has_valuation",
        "count",
        "count_of",
        "has_count",
        "total",
        "size",
        "has_size",
        "percentage",
        "percentage_of",
        "estimated_at",
        "estimate",
        "measured_at",
        "measurement",
        "ranked",
        "rank",
        "ranking",
        "has_rank",
        "potential_score",
        "risk_score",
        "quality_score",
        "health_score",
        "sentiment",
        "sentiment_score",
        "confidence",
        "confidence_score",
        "probability",
        "capacity",
        "has_capacity",
        "limit",
        "has_limit",
        "quota",
        "has_quota",
        "balance",
        "has_balance",
        "supply",
        "total_supply",
        "circulating_supply",
        "volume",
        "trading_volume",
        "liquidity",
        "all_time_high",
        "all_time_low",
        "burn_rate",
        "runway",
        "latency",
        "uptime",
        "availability",
        "accuracy",
        "precision",
        "recall_metric",
        "f1_score",
        # -- Versioning & current state --
        "version",
        "has_version",
        "latest_version",
        "current_version",
        "running_version",
        "released_as",
        "replaced_by",
        "replaces",
        "succeeded_by",
        "succeeds",
        "upgraded_to",
        "upgraded_from",
        "migrated_to",
        "migrated_from",
        "chosen_over",
        "selected",
        "preferred",
        "switched_to",
        "switched_from",
        "deprecated_by",
        "deprecated",
        # -- Temporal & scheduling --
        "scheduled_for",
        "scheduled_at",
        "rescheduled_to",
        "due_by",
        "due_date",
        "due_on",
        "starts_on",
        "start_date",
        "started_on",
        "started_at",
        "ends_on",
        "end_date",
        "ended_on",
        "ended_at",
        "expires_on",
        "expiry_date",
        "expiration",
        "deadline",
        "has_deadline",
        "eta",
        "has_eta",
        "expected_by",
        "next_review",
        "next_meeting",
        "next_milestone",
        "last_updated",
        "last_modified",
        "last_seen",
        "last_active",
        "created_on",
        "created_at",
        "target_date",
        "release_date",
        "go_live_date",
        "launch_date",
        # -- Configuration & settings --
        "configured_as",
        "configuration",
        "set_to",
        "setting",
        "limited_to",
        "capped_at",
        "defaults_to",
        "default_value",
        "backed_by",
        "backend",
        "powered_by",
        "styled_with",
        "licensed_under",
        "license",
        "instance_of",
        "environment",
        "env",
        "tier",
        "subscription",
        "subscription_plan",
        "pricing_plan",
        "mode",
        "has_mode",
        # -- Project & task management --
        "priority",
        "has_priority",
        "severity",
        "has_severity",
        "milestone",
        "has_milestone",
        "sprint",
        "has_sprint",
        "epic",
        "has_epic",
        "depends_on_completion_of",
        # -- Infrastructure & networking --
        "hostname",
        "cluster",
        "namespace",
        "zone",
        "availability_zone",
        # -- Contact & personal --
        "email",
        "has_email",
        "email_address",
        "phone",
        "has_phone",
        "phone_number",
        "website",
        "has_website",
        "age",
        "has_age",
        "born_in",
        "birthdate",
        "date_of_birth",
        "married_to",
        "spouse",
        "employed_by",
        "employer",
        "work_location",
        "office",
    }
)

# ── Predicate aliasing under paraphrase (A36) ──
# ``SINGLE_VALUE_PREDICATES`` says WHICH attributes can hold only one value.
# It does not say which of its own members are the SAME attribute — so
# ``status`` and ``current_status`` are two keys, and a subject carrying one of
# each is never compared. The RDF contradiction path keys on
# ``(subject_entity_id, predicate)`` with an exact predicate match, so
# "deploy target is staging" written as ``deployed_to`` and later contradicted
# as ``deployed_on: production`` produces no conflict at all: both rows stay
# live, both stay unpenalised, and a reader gets whichever ranks higher.
#
# Each key below is an alias; each value is the canonical member of its cluster
# (itself always a member of ``SINGLE_VALUE_PREDICATES``). Consumers should go
# through ``predicate_cluster()`` rather than reading this map directly.
#
# THE INCLUSION RULE, and why it is deliberately narrow. On the OBJECT side of
# this query (A35) a wrong equivalence SUPPRESSES a real contradiction, so
# normalising aggressively was safe. Here the failure runs the other way: a
# wrong alias MANUFACTURES a contradiction between two facts that were never in
# competition, and the loser takes a 0.5 ranking penalty. So a cluster is
# admitted only when its members differ by a function word, an abbreviation, or
# a form of the same word — never by a content word.
#
# What that rule REFUSES, and why each refusal is load-bearing:
#
#   * INVERSES. The set contains ``manager``/``manager_of``,
#     ``head_of``/``headed_by``, ``ceo``/``ceo_of``,
#     ``maintainer``/``maintainer_of``. These are not paraphrases — they point
#     the other way. Aliasing "X's manager is Y" to "X is the manager of Y"
#     would read an org chart as self-contradictory. Only the side that makes
#     the value an attribute OF the subject joins a cluster.
#   * ``title`` → ``job_title``. ``title`` is also a document's title, and the
#     predicate carries no subject type to tell the two apart.
#   * ``cost`` → ``price``. What a thing costs to make and what it sells for
#     are two numbers that are SUPPOSED to differ.
#   * ``latest_version`` / ``running_version`` → ``version``. Latest-released
#     and currently-deployed differ on every system mid-rollout; that gap is
#     the fact, not a conflict.
#   * ``start_date`` / ``started_at`` and the ``end_date`` family. Planned vs
#     actual. A slipped schedule would read as a contradiction.
#   * ``resides_at`` / ``stationed_at`` / ``address`` / ``city`` / ``country``
#     / ``region``. These differ from the location cluster in GRANULARITY, and
#     "Boston" vs "12 Main St" is not a disagreement.
#
# Granularity note: this exposure is not new and is not created here —
# ``located_in: Boston`` already conflicts with ``located_in: Massachusetts``
# under a single predicate. The rule above keeps aliasing from WIDENING it.
_PREDICATE_ALIAS_CLUSTERS: tuple[tuple[str, ...], ...] = (
    # Identity & status. ``state``/``current_state`` sit in the set's own
    # "Identity & status" section — geography is carried by ``region`` /
    # ``country`` / ``city``, which stay out of every cluster.
    ("status", "has_status", "current_status", "state", "current_state"),
    ("phase", "current_phase"),
    ("role", "has_role", "current_role"),
    ("type", "has_type"),
    # Where a thing is. One cluster on purpose: for a person "lives in" and
    # "is located in" answer the same question, and the predicate does not
    # know whether its subject is a person or a company.
    (
        "located_in",
        "is_located_in",
        "based_in",
        "is_based_in",
        "headquartered_in",
        "hq_in",
        "lives_in",
        "resides_in",
        "location",
        "current_location",
    ),
    # Where a workload runs. Preposition-only differences.
    ("deployed_to", "is_deployed_to", "deployed_at", "deployed_on"),
    ("hosted_on", "is_hosted_on", "hosted_at"),
    ("runs_on", "running_on"),
    ("stored_in", "stored_at"),
    ("registered_in", "registered_at"),
    # Hierarchy — attribute-of-subject side only (see INVERSES above).
    ("reports_to", "reporting_to", "supervisor", "supervised_by", "manager"),
    ("led_by", "headed_by"),
    ("assigned_to", "is_assigned_to", "assignee"),
    ("maintained_by", "maintainer"),
    # Metrics. ``price`` and ``cost`` are separate clusters, not one.
    ("score", "has_score", "scored"),
    ("rating", "has_rating", "rated"),
    ("price", "has_price", "priced_at", "current_price"),
    ("cost", "has_cost", "costs"),
    ("rank", "has_rank", "ranked", "ranking"),
    ("value", "has_value"),
    ("version", "has_version", "current_version"),
    ("due_date", "due_by", "due_on", "deadline", "has_deadline"),
    # Contact & personal.
    ("email", "has_email", "email_address"),
    ("phone", "has_phone", "phone_number"),
    ("birthdate", "date_of_birth"),
    ("spouse", "married_to"),
    ("employer", "employed_by"),
)

# alias → canonical. The first member of each tuple is the canonical form.
PREDICATE_ALIASES: dict[str, str] = {
    alias: cluster[0] for cluster in _PREDICATE_ALIAS_CLUSTERS for alias in cluster[1:]
}

# Any member → every member of its cluster, canonical included. This is the
# lookup the query path wants: it turns one predicate into the set of spellings
# a stored row might legitimately have used for the same attribute.
_PREDICATE_CLUSTER_BY_MEMBER: dict[str, frozenset[str]] = {
    member: frozenset(cluster)
    for cluster in _PREDICATE_ALIAS_CLUSTERS
    for member in cluster
}


def predicate_cluster(predicate: str) -> frozenset[str]:
    """Every spelling that means the same attribute as ``predicate``.

    Returns ``{predicate}`` (lowercased) for a predicate in no cluster, so a
    caller can use the result unconditionally and an unaliased predicate keeps
    exactly today's behaviour — a single-member IN is the same query as an
    equality.
    """
    normalized = (predicate or "").strip().lower()
    return _PREDICATE_CLUSTER_BY_MEMBER.get(normalized, frozenset({normalized}))


# ── Lifecycle automation (CAURA-655) ──
# Weight threshold for archive-stale: memories below this with zero
# recalls are eligible for archival. Lives in common/ so the threshold
# is shared between core-api's adapter (synchronous OSS standalone path)
# and core-worker's storage helper (SaaS Pub/Sub consumer path).
# Diverging values would silently produce different archive footprints
# across the two deployment modes.
LIFECYCLE_STALE_ARCHIVE_WEIGHT: float = 0.3

# Minimum content length for a memory to be considered worth keeping. Two
# services must agree on it: core-api rejects shorter writes at the quality
# gate, and core-storage-api uses the same bound when listing existing rows
# that fall below it for the crystallizer's short-content hygiene check. A
# divergence would let the hygiene report flag rows the write path would have
# accepted, or miss rows it would have rejected.
CRYSTALLIZER_SHORT_CONTENT_CHARS: int = 10


# ── Search tuning knobs (#723 / #725 / #727) ──
# The declaration of each search knob's type, accepted range, and whether it
# crosses the wire to core-storage-api. Ranges and types drive validation on
# both the agent-profile and tenant-default write paths; the ``sql`` flags
# derive the wire contract below.
#
# NOT yet the declaration the request SCHEMAS derive from: ``SearchProfileUpdate``
# and the ``caura_tune`` MCP signature still enumerate their own subset (9 of
# these 12 — the three A/B knobs are deliberately not agent-tunable) with their
# own bounds. Those bounds now AGREE with this table, and
# ``test_agent_tunable_bounds_match_the_knob_table`` fails if they drift again —
# but they are still written out by hand in three places. Deriving them from
# here is the remaining step.
#
# One table because the same knob used to be registered in four places — the
# validation rules, both search-path builders, and the storage route's key list —
# and every omission was silent in a different way. Keys the SQL needed went
# missing on one path (``candidate_pool_size`` / ``score_formula``, #723). Keys
# the SQL never reads travelled anyway, one of which — ``top_k`` — collided with
# a same-named request parameter and became the candidate-window LIMIT, defeating
# the overfetch on the active path (#725). And a knob absent from the rules was
# accepted UNVALIDATED and UNCLAMPED on the agent path while the tenant-default
# path rejected it as unknown.
#
# It lives here, not beside core-api's default VALUES, because what drifts is the
# key set against storage's SQL — a two-service concern, which is this module's
# subject. The defaults stay in ``core_api.constants``: those are core-api policy,
# and three of them are not constants at all (``fts_weight`` is query-adaptive,
# ``top_k`` and ``min_similarity`` fall back to the caller's request).


class SearchKnob(NamedTuple):
    """Type, bounds, and wire disposition for one search tuning knob."""

    value_type: type
    bounds: tuple[float, float]
    # Crosses the wire in ``search_params``; storage reads it in the scoring SQL.
    sql: bool = False
    # Storage reads it with INDEXED access, i.e. no server-side default, so a
    # payload omitting it is malformed and the route rejects it rather than
    # letting it surface as a KeyError 500 from inside the session.
    sql_required: bool = False
    # Exposed on the agent-facing tuning surface (``SearchProfileUpdate``, and the
    # ``caura_tune`` MCP tool). False for the A/B knobs, which are held at their
    # global defaults until the offline comparison validates them and are flipped
    # per TENANT via ``search.default_profile``, not per agent.
    agent_tunable: bool = False


# Ceiling on ``top_k`` for every search surface — REST ``/search`` (422 above it),
# MCP ``caura_recall`` (clamped, with a warning) and the tunable profile below.
# One constant so the three cannot drift: it used to be a literal 20 here AND a
# ``MAX_SEARCH_TOP_K = 20`` in ``core_api.constants``. Raised 20 -> 200 on
# 2026-09-09: 20 was the only slot count on the AMB leaderboard set by the
# server rather than the caller (hybrid-search and cognee retrieve 50; Hindsight
# uses a token budget), and it forced the harness into 8k-character chunks to
# reach comparable context — the most tokens of any entry with the coarsest
# selection. This is a CEILING, not a value: the default stays
# DEFAULT_SEARCH_TOP_K (5) and every Caura surface asks for 5 unless told
# otherwise. Larger requests are intentionally opt-in: they increase the scored
# query's overfetch, local result processing, and response payload. Recall
# tracking is already one fire-and-forget batched update, and successor
# enrichment is already one batched lookup rather than one call per result.
MAX_SEARCH_TOP_K = 200

SEARCH_KNOBS: dict[str, SearchKnob] = {
    # ── core-api-local: resolved here, never sent to storage ──
    "top_k": SearchKnob(int, (1, MAX_SEARCH_TOP_K), agent_tunable=True),
    "min_similarity": SearchKnob(float, (0.1, 0.9), agent_tunable=True),
    # Ceiling 3, matching the agent-facing ingress (``SearchProfileUpdate`` and
    # the ``caura_tune`` MCP signature). It read 5 here until 2026-08-07 while
    # both of those said 3, so a tenant-wide default could hold a depth no agent
    # profile could ever set. Depth drives graph expansion cost, so 3 is the
    # deliberate ceiling rather than the widest of the three.
    "graph_max_hops": SearchKnob(int, (0, 3), agent_tunable=True),
    # ── scoring knobs storage reads positionally ──
    "fts_weight": SearchKnob(
        float, (0.0, 1.0), sql=True, sql_required=True, agent_tunable=True
    ),
    "freshness_floor": SearchKnob(
        float, (0.0, 1.0), sql=True, sql_required=True, agent_tunable=True
    ),
    "freshness_decay_days": SearchKnob(
        int, (7, 730), sql=True, sql_required=True, agent_tunable=True
    ),
    "recall_boost_cap": SearchKnob(
        float, (1.0, 3.0), sql=True, sql_required=True, agent_tunable=True
    ),
    "recall_decay_window_days": SearchKnob(
        int, (7, 365), sql=True, sql_required=True, agent_tunable=True
    ),
    "similarity_blend": SearchKnob(
        float, (0.0, 1.0), sql=True, sql_required=True, agent_tunable=True
    ),
    # ── scoring knobs with a server-side default, so optional on the wire ──
    # #687: scale on ts_rank_cd before saturation. Floor is 1.0, not 0 — that is
    # the pre-#687 formula, so a tenant can revert but cannot weaken keyword
    # relevance below where it has always been. Ceiling is the largest value the
    # LoCoMo sweep actually measured; above it is untested territory.
    "fts_rank_scale": SearchKnob(float, (1.0, 20.0), sql=True),
    # A49: 0 = off (candidate pool by boosted score); >0 = cosine-dominant pool of this size.
    "candidate_pool_size": SearchKnob(int, (0, 200), sql=True),
    # A50 unified: 0 = legacy multiplicative score; 1 = unified relevance-dominant formula.
    "score_formula": SearchKnob(int, (0, 1), sql=True),
    # HNSW two-stage retrieval (PR2 of docs/plans/hnsw-two-stage-retrieval.md):
    # 0 = off (full-scan candidate window, unchanged); >0 = storage admits
    # candidates through index-served pool arms (ANN top-N by cosine via the
    # memories HNSW index, FTS, recency, date window, entity-boosted ids) and
    # runs the scoring formula over that pool only. Needs pgvector >= 0.8 at
    # runtime (iterative scans); storage probes once and silently keeps the
    # full scan below that. Mutually exclusive with ``candidate_pool_size`` —
    # storage lets ann win if both arrive, but don't set both.
    "ann_pool_size": SearchKnob(int, (0, 1000), sql=True),
    # Shadow-compare mode for the ANN pool (PR3): when 1 AND ann_pool_size > 0,
    # core-api SERVES the legacy full-scan result and runs the pooled query in
    # the background, logging rank overlap / score deltas / latency — the
    # rollout gate's evidence on real traffic. Core-api-side only (sql=False:
    # storage never reads it; the primary call crosses the wire with
    # ann_pool_size forced to 0 and the shadow call with the configured size).
    # Inert when ann_pool_size is 0.
    "ann_pool_shadow": SearchKnob(int, (0, 1)),
}

# The wire contract, derived. Core-api's two search-path builders project
# ``search_params`` through the first; the storage route rejects a payload
# missing any of the second.
SQL_SCORING_PARAM_KEYS: tuple[str, ...] = tuple(
    k for k, v in SEARCH_KNOBS.items() if v.sql
)
SQL_SCORING_REQUIRED_KEYS: tuple[str, ...] = tuple(
    k for k, v in SEARCH_KNOBS.items() if v.sql_required
)
# The agent-facing tuning surface, derived the same way: ``SearchProfileUpdate``
# and the ``caura_tune`` MCP tool expose exactly these.
AGENT_TUNABLE_KEYS: tuple[str, ...] = tuple(
    k for k, v in SEARCH_KNOBS.items() if v.agent_tunable
)


# ---------------------------------------------------------------------------
# Analysis reports
# ---------------------------------------------------------------------------

# H-07: how long a report row may sit in ``status='running'`` before
# ``report_find_running`` stops treating it as in flight.
#
# NOT a timeout — nothing is cancelled, and no run is shortened. It bounds how
# long an ORPHANED row (one whose run died without writing a terminal status) can
# suppress future runs, which used to be forever: ``run_crystallization``
# short-circuits on whatever that lookup returns, so a single crashed run
# disabled crystallization for the tenant until someone edited the row by hand.
#
# One hour is a ceiling on a plausible run, not a typical one: a run does an LLM
# call per selected cluster, so minutes is normal and an hour is far outside it.
# Raising this lengthens the outage a crash causes; lowering it risks two
# concurrent runs, whose only consequence is a second report row.
REPORT_RUNNING_STALE_AFTER = timedelta(hours=1)
