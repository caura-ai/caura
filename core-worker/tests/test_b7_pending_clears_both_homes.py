"""B7 x C25 — the worker's enrichment PATCH must clear the pending flag in
BOTH metadata homes.

C25 gave the ``_system`` namespace read-side precedence
(``extract_system_metadata``: nested wins over legacy), so a patch that
clears only the legacy top-level ``enrichment_pending`` leaves
``system_metadata.enrichment_pending`` True forever — polling would never
observe completion on fast-mode rows. The patch also mirrors the enrichment
metadata fields into ``_system`` so async-enriched rows carry the same
namespaced view strong-mode rows get at write time.
"""

from common.enrichment.schema import EnrichmentResult
from core_worker.consumer import _build_patch


def _result(**over):
    base = {
        "memory_type": "fact",
        "weight": 0.5,
        "title": "t",
        "summary": "platform summary",
        "tags": ["a"],
        "contains_pii": False,
        "pii_types": [],
        "retrieval_hint": "hint",
        "llm_ms": 42,
    }
    base.update(over)
    return EnrichmentResult(**base)


def test_pending_cleared_in_both_homes():
    patch = _build_patch(_result())
    mp = patch["metadata_patch"]
    assert mp["enrichment_pending"] is False
    assert mp["_system"]["enrichment_pending"] is False


def test_metadata_fields_mirrored_into_system():
    patch = _build_patch(_result())
    mp = patch["metadata_patch"]
    assert mp["summary"] == "platform summary"
    assert mp["_system"]["summary"] == "platform summary"
    assert mp["llm_ms"] == 42
    assert mp["_system"]["llm_ms"] == 42


def test_heuristic_fallback_still_clears_both_homes():
    # llm_ms=0 → every metadata field skips, but the pending clear must
    # still go out in both homes (the original regression, extended to C25).
    heuristic = _result(summary="", tags=[], retrieval_hint="", llm_ms=0, title="")
    patch = _build_patch(heuristic)
    mp = patch["metadata_patch"]
    assert mp["enrichment_pending"] is False
    assert mp["_system"] == {"enrichment_pending": False}
