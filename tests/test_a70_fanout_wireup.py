"""reg-a70 step 2b — the deferred path finally creates its child memories.

Step 1 (#1424) stopped the worker discarding ``atomic_facts``; step 2a (#1428)
lifted the fan-out into ``fan_out_atomic_facts`` so it could be called from
somewhere other than the synchronous write. This joins them: the enriched
consumer reads the persisted facts and creates the children, closing the gap
where a fast-mode write of multi-claim content produced fewer memories than the
same content in strong mode.

The placement is the load-bearing part and is what these tests mostly pin.
"""

import inspect

import pytest

from core_api import consumer as c

pytestmark = pytest.mark.unit


def test_the_consumer_fans_out():
    assert callable(c._fan_out_persisted_atomic_facts)
    assert (
        "_fan_out_persisted_atomic_facts(sc, memory, payload, outcome)"
        in inspect.getsource(c.handle_memory_enriched)
    )


def test_it_reuses_the_shared_fan_out_rather_than_reimplementing_it():
    """A second copy of that block is the one outcome worth avoiding — every
    guarantee in it was paid for by an incident."""
    src = inspect.getsource(c._fan_out_persisted_atomic_facts)
    assert "await fan_out_atomic_facts(" in src
    assert "create_memory" not in src, "the consumer is writing rows itself again"


def test_children_inherit_the_post_policy_visibility():
    """#808. ``memory`` was read BEFORE the governance PATCH, so its visibility
    is the pre-policy value; ``outcome.visibility`` is the only correct source,
    and it must win."""
    src = inspect.getsource(c._fan_out_persisted_atomic_facts)
    assert 'outcome.visibility or memory.get("visibility")' in src


def test_fan_out_runs_after_remediation_and_before_the_embedding_check():
    """Order is the design. After remediation: a dropped row must spawn nothing,
    and children need the post-policy visibility. Before the embedding check: a
    parent whose vector has not landed still has valid facts, and each child
    embeds itself — gating on the parent would strand them behind an unrelated
    race."""
    src = inspect.getsource(c.handle_memory_enriched)
    drop = src.index("if outcome.dropped:")
    fan = src.index("_fan_out_persisted_atomic_facts(")
    embed = src.index('embedding = memory.get("embedding")')
    assert drop < fan < embed


def test_a_dropped_row_never_reaches_the_fan_out():
    """The governance branch returns, so this is structural rather than a check
    inside the fan-out."""
    src = inspect.getsource(c.handle_memory_enriched)
    drop_block = src[
        src.index("if outcome.dropped:") : src.index("_fan_out_persisted_atomic_facts(")
    ]
    assert "return" in drop_block


def test_no_facts_costs_nothing():
    """Every enriched row enters here and most carry no facts."""
    src = inspect.getsource(c._fan_out_persisted_atomic_facts)
    head = src[: src.index("try:")]
    assert "if not raw_facts:" in head and "return" in head


def test_the_marker_is_cleared_by_merge_not_overwrite():
    """``metadata_patch`` merges with JSONB ``||``; writing ``metadata_`` whole
    would drop the summary/tags/pii keys the worker set in the same row."""
    src = inspect.getsource(c._fan_out_persisted_atomic_facts)
    assert '"metadata_patch": {"atomic_facts": None}' in src


def test_a_failed_fan_out_keeps_the_facts_for_retry():
    """The facts are the expensive part — they cost an LLM call. Clearing the
    marker after a failure would discard them for good."""
    src = inspect.getsource(c._fan_out_persisted_atomic_facts)
    fail = src[src.index("atomic-fact fan-out failed") :]
    assert "return" in fail[:400], "it falls through and clears the marker anyway"


def test_it_never_raises_into_the_handler():
    """A fan-out failure must not nack the event: contradiction detection runs
    after it and is this handler's primary job."""
    src = inspect.getsource(c._fan_out_persisted_atomic_facts)
    assert src.count("except Exception:") >= 2


def test_an_unparseable_payload_is_dropped_not_retried_forever():
    """We wrote it, so a shape we cannot parse means the schema moved under an
    older row. No redelivery re-parses it."""
    src = inspect.getsource(c._fan_out_persisted_atomic_facts)
    assert "except (ValidationError, TypeError):" in src
    assert "facts = []" in src


def test_counts_are_logged_so_the_deferred_path_is_observable():
    """The synchronous path logs created/deduped/unembedded; without the same
    three numbers here, nobody can tell whether the deferred path is working."""
    src = inspect.getsource(c._fan_out_persisted_atomic_facts)
    for k in ("created", "deduped", "unembedded"):
        assert f'counts["{k}"]' in src
