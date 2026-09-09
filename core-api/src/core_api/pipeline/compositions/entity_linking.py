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


# ``build_quick_entity_linking_pipeline``, ``build_link_discovery_pipeline`` and
# ``build_relation_inference_pipeline`` were removed 2026-09-09 (audit
# oss-0814-l-48): three profiles for schedules that were never wired up, with no
# reference anywhere in the repo — not a caller, not an export, not a test. Only
# the nightly full pipeline is used (``lifecycle_audit``). They are recoverable
# from git if an hourly or on-demand schedule is ever built; keeping unreachable
# profiles around implied a choice of pipeline that callers did not actually have.
