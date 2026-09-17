"""reg-a70 step 1 — the async worker stopped throwing atomic facts away.

Atomize-on-write is largely built already: the enrichment prompt asks for
``atomic_facts`` (field 9), ``AtomicFact`` and ``EnrichmentResult.atomic_facts``
exist with a validator, and ``memory_service`` fans them out into child memories
on the SYNCHRONOUS path — with live-hash dedup, visibility and weight
inheritance, and non-fatal failure.

The async worker did none of that. Worse, it did not merely skip the fan-out: it
listed ``atomic_facts`` in ``_ENRICHMENT_UNROUTED_FIELDS`` and DISCARDED them,
so the LLM's answer was gone and the only way to recover it was to pay for the
call again. A fast-mode write of multi-claim content produced fewer memories
than the same content in strong mode, permanently.

This step persists them. Fan-out stays in core-api, which already consumes
``Topics.Memory.ENRICHED`` and already owns the rules a child must inherit —
and the worker's storage client has no create-memory call at all, so putting it
here would mean both a new write capability and a second copy of a subtle path.
"""

import pytest

from common.enrichment import EnrichmentResult
from core_worker import consumer as wc

pytestmark = pytest.mark.unit


def test_atomic_facts_are_no_longer_discarded():
    """The one-line regression: the field used to sit in the drop list."""
    assert "atomic_facts" not in wc._ENRICHMENT_UNROUTED_FIELDS


def test_atomic_facts_route_to_metadata():
    assert "atomic_facts" in wc._ENRICHMENT_METADATA_FIELDS


def test_nothing_is_unrouted_any_more():
    """The drop list is now empty, so the import-time drift guard below covers
    every field rather than every field except one."""
    assert not wc._ENRICHMENT_UNROUTED_FIELDS


def test_every_enrichment_field_still_has_a_home():
    """Mirrors the module's own import-time guard. If someone adds a field to
    ``EnrichmentResult`` and wires it nowhere, this fails here rather than the
    value silently landing on the floor."""
    unrouted = (
        set(EnrichmentResult.model_fields)
        - wc._ENRICHMENT_ORM_FIELDS
        - wc._ENRICHMENT_METADATA_FIELDS
        - wc._ENRICHMENT_UNROUTED_FIELDS
    )
    assert unrouted == set(), f"unrouted enrichment fields: {unrouted}"


def test_a_re_enrichment_can_clear_stale_facts():
    """Always-write, for the same reason ``tags`` is. A re-run that finds ONE
    claim where a previous run found three must be able to clear the stale two,
    or the fan-out would later create children for claims the current content no
    longer makes."""
    assert "atomic_facts" in wc._ENRICHMENT_ALWAYS_WRITE_METADATA


def test_a_heuristic_fallback_cannot_clear_real_facts():
    """Always-write is paired with the ``llm_ms > 0`` guard everywhere it is
    used; without it a fallback redelivery during an LLM outage would wipe facts
    a real run produced."""
    import inspect

    # Comments stripped and the window sized off the CODE: the always-write
    # branch carries a long explanatory comment, so a byte-window over the raw
    # source measures prose rather than the guard's placement.
    code = [
        ln
        for ln in inspect.getsource(wc._build_patch).splitlines()
        if not ln.lstrip().startswith("#")
    ]
    i = next(
        n for n, ln in enumerate(code) if "_ENRICHMENT_ALWAYS_WRITE_METADATA" in ln
    )
    assert any("result.llm_ms > 0" in ln for ln in code[i : i + 4]), (
        "the always-write branch no longer guards on a real LLM call"
    )


def test_the_gap_log_no_longer_claims_the_facts_are_lost():
    """The WARNING is the operator's only signal here. It said the facts would
    not appear as children — true — but implied they were gone, which is no
    longer the case, and the difference decides whether anyone has to re-run the
    LLM to recover them."""
    import inspect

    src = inspect.getsource(wc.handle_enrich_request)
    assert "persisted to" in src
    assert "not yet implemented" in src


def test_the_gap_warning_stays_greppable_and_counted():
    """The count is what sizes the exposure for the A75 proof gate."""
    import inspect

    src = inspect.getsource(wc.handle_enrich_request)
    assert "len(result.atomic_facts)" in src
    assert "logger.warning" in src[src.index("if result.atomic_facts:") - 400 :]
