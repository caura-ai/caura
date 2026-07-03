"""Search pipeline composition — single linear pipeline for search_memories()."""

from core_api.pipeline.runner import Pipeline
from core_api.pipeline.steps.search import (
    ClassifyQuery,
    CrossContextEnrich,
    ExecuteScoredSearch,
    ExtractTemporalHint,
    GraphReasoningBoost,
    InjectSTMContext,
    LoadAndSerialize,
    LogRecallEvent,
    ParallelEmbedAndEntityBoost,
    PostFilterResults,
    ResolveSearchProfile,
    TrackRecalls,
)


def build_search_pipeline() -> Pipeline:
    return Pipeline(
        "search",
        [
            ResolveSearchProfile(),
            ExtractTemporalHint(),
            ClassifyQuery(),
            ParallelEmbedAndEntityBoost(),
            GraphReasoningBoost(),
            ExecuteScoredSearch(),
            PostFilterResults(),
            CrossContextEnrich(),
            LoadAndSerialize(),
            InjectSTMContext(),
            TrackRecalls(),
            LogRecallEvent(),
        ],
    )
