"""A write reports where its ``weight`` came from (``weight_source``).

When the caller omits ``weight``, it is filled by the enrichment LLM. That is
not cosmetic: ``SIMILARITY_BLEND = 0.85`` makes weight 15% of the base rank
score, so identical content can rank differently run to run. Two throwaway
``fact`` memories written seconds apart were observed getting 0.30 and 0.50 —
and nothing in the response said either number came from a model, so a team
chasing non-reproducible ranking had no way to tell an LLM guess from a
deliberate value.

Three values rather than a boolean, because "not the caller" hides the
distinction that matters for reproducibility:

    caller   — the write supplied it; fully deterministic
    llm      — the enrichment model chose it; NOT reproducible
    default  — ``DEFAULT_MEMORY_WEIGHT``; deterministic

Mirrors ``memory_type_agent_set`` (CAURA-703), which records the same
provenance question for ``memory_type``, and is registered in the same
``PLATFORM_ONLY_KEYS`` set so a caller cannot forge it.
"""

from __future__ import annotations

import uuid

import pytest

from core_api.constants import DEFAULT_MEMORY_WEIGHT
from core_api.pipeline.context import PipelineContext
from core_api.pipeline.steps.write.merge_enrichment_fields import MergeEnrichmentFields
from core_api.services.system_metadata import PLATFORM_ONLY_KEYS, sanitize_caller_metadata

pytestmark = [pytest.mark.unit]


class _Input:
    def __init__(self, weight=None):
        self.memory_type = None
        self.weight = weight
        self.metadata = None
        self.ts_valid_start = None
        self.ts_valid_end = None
        self.status = None


class _Enrichment:
    def __init__(self, weight=None):
        self.memory_type = "fact"
        self.weight = weight
        self.title = None
        self.summary = None
        self.tags = None
        self.llm_ms = None
        self.ts_valid_start = None
        self.ts_valid_end = None
        self.contains_pii = False
        self.pii_types = None
        self.business_relevance = None


async def _run(*, caller_weight=None, enrichment=None):
    ctx = PipelineContext(data={"input": _Input(caller_weight), "enrichment": enrichment})
    await MergeEnrichmentFields().execute(ctx)
    return ctx.data["memory_fields"]


def _source(fields) -> str:
    return fields["metadata"]["_system"]["weight_source"]


async def test_caller_supplied_weight_is_reported_as_caller():
    fields = await _run(caller_weight=0.7, enrichment=_Enrichment(weight=0.3))

    assert fields["weight"] == 0.7, "a caller-supplied weight always wins"
    assert _source(fields) == "caller"


async def test_llm_supplied_weight_is_reported_as_llm():
    """The case the finding is about — a value no one chose and nothing flagged."""
    fields = await _run(caller_weight=None, enrichment=_Enrichment(weight=0.3))

    assert fields["weight"] == 0.3
    assert _source(fields) == "llm"


async def test_default_weight_is_reported_as_default():
    """Enrichment off or failed — deterministic, and distinguishable from ``llm``.

    A boolean would collapse this with the LLM case, losing exactly the fact a
    team wanting reproducible ranking needs.
    """
    fields = await _run(caller_weight=None, enrichment=None)

    assert fields["weight"] == DEFAULT_MEMORY_WEIGHT
    assert _source(fields) == "default"


async def test_enrichment_without_a_weight_is_default_not_llm():
    """Enrichment ran but returned no weight — the LLM chose nothing.

    Reporting ``llm`` here would claim a model picked a value it did not, and
    would mark a deterministic default as irreproducible.
    """
    fields = await _run(caller_weight=None, enrichment=_Enrichment(weight=None))

    assert fields["weight"] == DEFAULT_MEMORY_WEIGHT
    assert _source(fields) == "default"


async def test_zero_weight_from_the_caller_still_counts_as_caller():
    """0.0 is a choice, not an absence — the check is ``is not None``.

    A truthiness test here would silently reattribute a deliberate 0.0 to the
    LLM and overwrite it.
    """
    fields = await _run(caller_weight=0.0, enrichment=_Enrichment(weight=0.9))

    assert fields["weight"] == 0.0
    assert _source(fields) == "caller"


def test_weight_source_cannot_be_forged_by_a_caller():
    """Platform-only, like ``memory_type_agent_set``.

    Without this a caller could stamp ``weight_source: "caller"`` on a row whose
    weight a model actually chose — the provenance would be worse than absent,
    because it would look authoritative.
    """
    assert "weight_source" in PLATFORM_ONLY_KEYS
    assert sanitize_caller_metadata({"weight_source": "caller", "mine": 1}) == {"mine": 1}
