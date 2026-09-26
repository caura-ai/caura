"""oss-0902-l-30 and oss-0814-l-09 — two crystallizer defects on the same sweep.

l-30: the source-archive step ran UNCONDITIONALLY after the fact-creation loop,
so a cluster whose every crystallized fact was rejected as a duplicate ended
with all its sources archived and nothing standing in their place. The code's
own comment explains why that is the routine case, not an edge one: a
crystallized fact is a near-verbatim merge of cluster members that are >=0.95
similar and STILL ACTIVE at that point, so ``create_memory``'s dedup gate 409s
against a cluster member — the very row about to be archived. Knowledge left the
live corpus and none entered it.

l-09: ``POST /crystallize/all`` answered 500 on every multi-tenant deployment.
``get_standalone_tenant_id()`` raises when standalone was never initialised and
nothing caught it. The endpoint is standalone-only by construction — there is no
tenant enumeration to fan out over — so the condition is a deployment mode, not
a fault, and 500 sends whoever is on call hunting a crystallizer outage.
"""

import inspect

import pytest

pytestmark = pytest.mark.unit


# ── l-30 ──────────────────────────────────────────────────────────────────


def _archive_block() -> str:
    from core_api.services import crystallizer_service as cs

    src = inspect.getsource(cs)
    i = src.index("archived_ids: list[str] = []")
    return src[i : src.index("batch_result", i) + 600]


def test_archive_is_gated_on_a_replacement_existing():
    b = _archive_block()
    assert "if not new_ids:" in b, "the archive step is unconditional again"


def test_gate_is_new_ids_not_absence_of_duplicates():
    """What licenses retiring the sources is that a replacement EXISTS — not
    that nothing was rejected. A cluster that created one fact and skipped two
    duplicates is still safe to archive; one that created none never is.

    Comments are stripped first: the code comment names the rejected condition
    in prose to explain why it is wrong, and matching that would fail on the
    explanation rather than on the behaviour."""
    code = "\n".join(
        line
        for line in _archive_block().splitlines()
        if not line.lstrip().startswith("#")
    )
    assert "duplicate_facts == 0" not in code
    assert "failed_facts == 0" not in code
    assert "if not new_ids:" in code


def test_nothing_created_means_nothing_is_queued_for_archive():
    b = _archive_block()
    i = b.index("if not new_ids:")
    assert "cluster_ids_to_archive = []" in b[i : b.index("else:", i)]


def test_keeping_sources_live_is_logged_with_the_reason():
    """Silence here would look identical to a cluster that had no sources."""
    b = _archive_block()
    assert "kept" in b and "duplicates=%d failed=%d" in b


def test_empty_cluster_skips_the_storage_round_trip():
    """No replacement means nothing to retire — and no reason to spend a call
    telling storage so."""
    b = _archive_block()
    assert "if cluster_ids_to_archive" in b and "else {}" in b


# ── l-09 ──────────────────────────────────────────────────────────────────


def test_fanout_answers_501_not_500_outside_standalone():
    from core_api.routes import crystallizer as cr

    src = inspect.getsource(cr.trigger_crystallization_all)
    assert "except RuntimeError" in src, "the RuntimeError still escapes as a 500"
    assert "status_code=501" in src


def test_the_501_names_the_route_that_does_work():
    """An error that only says 'no' costs the caller another round trip to find
    out what to do instead."""
    from core_api.routes import crystallizer as cr

    src = inspect.getsource(cr.trigger_crystallization_all)
    assert "POST /crystallize?tenant_id=" in src


def test_standalone_path_is_untouched():
    """The fan-out must still work where it is meaningful — the guard is only
    reached when the tenant id cannot be resolved at all."""
    from core_api.routes import crystallizer as cr

    src = inspect.getsource(cr.trigger_crystallization_all)
    body = src[src.index("try:") :]
    assert "tenant_ids = [get_standalone_tenant_id()]" in body
    assert body.index("tenant_ids = [get_standalone_tenant_id()]") < body.index(
        "except RuntimeError"
    )
