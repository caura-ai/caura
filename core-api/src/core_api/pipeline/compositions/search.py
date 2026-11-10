"""Search pipeline composition — single linear pipeline for search_memories()."""

from core_api.pipeline.runner import Pipeline
from core_api.pipeline.steps.search import (
    ClassifyQuery,
    ExecuteScoredSearch,
    ExplainMemoryFilters,
    ExtractTemporalHint,
    InjectSTMContext,
    LoadAndSerialize,
    LogRecallEvent,
    ParallelEmbedAndEntityBoost,
    PostFilterResults,
    RecordExposure,
    RecordRecallValue,
    RerankResults,
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
            ExecuteScoredSearch(),
            RerankResults(),
            PostFilterResults(),
            LoadAndSerialize(),
            InjectSTMContext(),
            TrackRecalls(),
            LogRecallEvent(),
            RecordExposure(),
            RecordRecallValue(),
        ],
    )


def build_search_explain_pipeline() -> Pipeline:
    """The search pipeline as EC-06's explain runs it.

    The same steps in the same order up to the result set, plus
    ``ExplainMemoryFilters`` after the scored search. Left out: the steps that
    only act on a finished result (STM context injection, which adds rather
    than removes) or record it (recall tracking, recall-event logging,
    recall-value metering, exposure) — an explanation is not a recall and must not
    count as one.
    """
    return Pipeline(
        "search_explain",
        [
            ResolveSearchProfile(),
            ExtractTemporalHint(),
            ClassifyQuery(),
            ParallelEmbedAndEntityBoost(),
            ExecuteScoredSearch(),
            ExplainMemoryFilters(),
            RerankResults(),
            PostFilterResults(),
            LoadAndSerialize(),
        ],
    )
