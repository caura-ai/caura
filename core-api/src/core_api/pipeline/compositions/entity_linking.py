"""Entity linking pipeline compositions — modular profiles for different schedules."""

from core_api.pipeline.runner import Pipeline
from core_api.pipeline.steps.entity_linking import (
    BackfillEntityEmbeddings,
    DiscoverCrossLinks,
    InferRelations,
    ResolveEntities,
)


def build_full_entity_linking_pipeline() -> Pipeline:
    """Nightly: all 4 steps in dependency order."""
    return Pipeline(
        "entity_linking_full",
        [
            BackfillEntityEmbeddings(),
            ResolveEntities(),
            DiscoverCrossLinks(),
            InferRelations(),
        ],
    )


def build_quick_entity_linking_pipeline() -> Pipeline:
    """Hourly: just resolution (assumes embeddings are mostly populated)."""
    return Pipeline(
        "entity_linking_quick",
        [
            ResolveEntities(),
        ],
    )


def build_link_discovery_pipeline() -> Pipeline:
    """On-demand: backfill embeddings then discover new cross-links."""
    return Pipeline(
        "entity_linking_discovery",
        [
            BackfillEntityEmbeddings(),
            DiscoverCrossLinks(),
        ],
    )


def build_relation_inference_pipeline() -> Pipeline:
    """On-demand: just co-occurrence relation inference."""
    return Pipeline(
        "entity_linking_relations",
        [
            InferRelations(),
        ],
    )
