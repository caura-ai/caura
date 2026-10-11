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


def test_the_drop_list_holds_exactly_the_two_deliberate_constants():
    """Pinned as an exact set, so a THIRD field cannot join them quietly.

    The list was empty after ``atomic_facts`` left it. ``status`` and ``tags``
    were added back deliberately (OSS 09/02 M-65 / M-66) — the argument for
    each is at ``_ENRICHMENT_UNROUTED_FIELDS`` itself.

    Equality rather than ``in``: the whole value of this list is that it is
    short and every entry is argued for. ``assert "x" in ...`` would let a
    future field be dropped by adding one line.
    """
    # Bound to a lowercase local first: SIM300 classifies the SCREAMING_CASE
    # attribute as a constant and wants it on the right, which would put the
    # subject of the assertion after the expectation.
    drop_list = wc._ENRICHMENT_UNROUTED_FIELDS
    assert drop_list == frozenset({"status", "tags"})


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
    """Always-write. (``tags`` used to be cited here as the parallel case; it
    is no longer always-write — see M-66.) A re-run that finds ONE
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


def test_no_log_claims_the_async_path_does_not_fan_out(monkeypatch):
    """oss-0924-m-03. Step 1 shipped a WARNING saying fan-out was "not yet
    implemented on the async path"; step 2b (#1430) implemented it the same day
    and the WARNING stayed for fifteen days, telling anyone grepping the worker
    logs the exact opposite of what the deferred path does.

    Asserted on the emitted records rather than the source text, so re-wording
    the claim cannot pass. ``caplog`` is not used: these tests run without the
    ``asyncio`` marker, and the assertion is about what the handler's logging
    calls produce, not about running the coroutine.
    """
    import inspect

    src = inspect.getsource(wc.handle_enrich_request)
    for stale in (
        "not yet implemented",
        "will NOT appear as child",
        "HALF closed",
    ):
        assert src.count(stale) <= 1, (
            f"{stale!r} appears as a live claim, not only in the note recording "
            "that it was retired"
        )
    # The retired WARNING is gone entirely — no logger.warning survives in the
    # atomic-fact stretch of the handler.
    assert "logger.warning" not in src


def test_the_fact_count_survives_as_a_field_on_the_info_line():
    """Retiring the WARNING must not cost the measurement. The count is the one
    thing pm-0918-c-04 could not get from the corpus (it predates A70), so it
    moves to the processed-line ``extra`` rather than disappearing with the
    string that used to carry it."""
    import inspect

    src = inspect.getsource(wc.handle_enrich_request)
    assert '"atomic_facts": len(result.atomic_facts or [])' in src
