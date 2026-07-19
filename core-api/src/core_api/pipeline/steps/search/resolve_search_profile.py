"""ResolveSearchProfile — resolve per-agent search profile with fallback to constants."""

from __future__ import annotations

from core_api.constants import (
    CANDIDATE_POOL_SIZE,
    FRESHNESS_DECAY_DAYS,
    FRESHNESS_FLOOR,
    GRAPH_MAX_HOPS,
    MIN_SEARCH_SIMILARITY,
    RECALL_BOOST_CAP,
    RECALL_DECAY_WINDOW_DAYS,
    SIMILARITY_BLEND,
)
from core_api.pipeline.context import PipelineContext
from core_api.pipeline.step import StepResult
from core_api.services.memory_service import _adaptive_fts_weight


class ResolveSearchProfile:
    @property
    def name(self) -> str:
        return "resolve_search_profile"

    async def execute(self, ctx: PipelineContext) -> StepResult | None:
        from core_api.services.organization_settings import validate_search_profile

        search_profile = ctx.data.get("search_profile")
        sp = validate_search_profile(search_profile) if search_profile else {}

        # Tenant-wide default profile (A47) sits BELOW the agent profile and
        # ABOVE the global constants: a per-agent tuned knob wins, the tenant
        # default fills the gaps, and the constant is the final fallback.
        # ``tenant_config`` is a ResolvedConfig on the primary search/recall
        # paths (routes resolve it before calling); guard for callers that
        # don't pass one so behaviour is unchanged when it's absent.
        tenant_config = ctx.tenant_config
        if tenant_config is not None:
            tenant_default = getattr(tenant_config, "default_search_profile", {}) or {}
            if tenant_default:
                sp = {**tenant_default, **sp}

        query = ctx.data["query"]
        top_k = ctx.data["top_k"]

        ctx.data["search_params"] = {
            "top_k": sp.get("top_k", top_k),
            "min_similarity": sp.get("min_similarity", MIN_SEARCH_SIMILARITY),
            "fts_weight": (sp["fts_weight"] if "fts_weight" in sp else _adaptive_fts_weight(query)),
            "freshness_floor": sp.get("freshness_floor", FRESHNESS_FLOOR),
            "freshness_decay_days": sp.get("freshness_decay_days", FRESHNESS_DECAY_DAYS),
            "recall_boost_cap": sp.get("recall_boost_cap", RECALL_BOOST_CAP),
            "recall_decay_window_days": sp.get("recall_decay_window_days", RECALL_DECAY_WINDOW_DAYS),
            "graph_max_hops": sp.get("graph_max_hops", GRAPH_MAX_HOPS),
            "similarity_blend": sp.get("similarity_blend", SIMILARITY_BLEND),
            # A49: 0 = off; >0 = storage selects a cosine-dominant candidate pool of this size.
            "candidate_pool_size": sp.get("candidate_pool_size", CANDIDATE_POOL_SIZE),
        }
        return None
