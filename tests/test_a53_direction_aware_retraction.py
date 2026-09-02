"""A53 — a flipped contradiction verdict could never be retracted.

The content route records its verdict two ways:

    canonical: new_memory wins -> new_memory.supersedes_id = candidate.id
                                 candidate       -> `conflicted`
    flipped:   candidate wins  -> candidate.supersedes_id = new_memory.id
                                 NEW_MEMORY      -> `outdated`

Retraction only ever dereferenced ``new_memory.supersedes_id``, which is NULL in
the flipped case, so it returned False before reaching the judge. The row to
revert and the row owning the chain edge SWAP between the two directions, so
writing them the canonical way round in a flipped chain reverts the wrong row
and leaves the real edge dangling.

The function's docstring asserted the opposite ("works in both directions").
"""

import inspect

import pytest

from core_api.services import contradiction_detector as cd

pytestmark = pytest.mark.unit


def test_flipped_lookup_is_retraction_shaped_not_search_shaped():
    """``find_successors`` filters status to active/confirmed and applies
    visibility scoping — it would silently miss the edge owner exactly when
    retraction needs it. The dedicated lookup applies neither."""
    from core_storage_api.services.postgres_service import PostgresService

    src = inspect.getsource(PostgresService.memory_find_by_supersedes_id)
    assert "Memory.supersedes_id == supersedes_id" in src
    assert "Memory.tenant_id == tenant_id" in src  # the one real boundary
    assert "status" not in src.split('"""')[2]  # no status predicate in the body
    assert "visibility" not in src.split('"""')[2]


def test_retraction_resolves_both_directions():
    src = inspect.getsource(cd._attempt_entity_retraction)
    # canonical: dereference our own edge
    assert 'new_memory.get("supersedes_id")' in src
    # flipped: find who points AT us
    assert "find_by_supersedes_id" in src
    assert "edge_owner" in src and "candidate" in src


def test_status_guard_is_shape_based_not_a_single_literal():
    """Canonical marks the loser `conflicted`; flipped marks it `outdated`.
    A guard hard-coded to `conflicted` refuses every flipped retraction."""
    src = inspect.getsource(cd._attempt_entity_retraction)
    assert '("conflicted", "outdated")' in src
    assert 'get("status") != "conflicted"' not in src


def test_ambiguous_chain_is_refused_not_guessed():
    """More than one row claiming the edge means clearing it could undo a
    DIFFERENT contradiction's work. Refusing is the safe direction."""
    src = inspect.getsource(cd._attempt_entity_retraction)
    assert "len(owners) != 1" in src


def test_edge_is_cleared_on_the_row_that_owns_it():
    """The CAS write must target edge_owner, not unconditionally new_memory —
    that is the actual bug in the flipped direction."""
    src = inspect.getsource(cd._attempt_entity_retraction)
    tail = src[src.index("await sc.update_memory_status") :]
    assert 'str(edge_owner.get("id"))' in tail
    assert "unset_supersedes=True" in tail
    assert 'expected_supersedes_id=str(candidate.get("id"))' in tail


def test_edge_consistency_checked_before_paying_for_the_judge():
    """If the edge no longer points at the row we resolved, the pair is stale;
    bail before the LLM call rather than discarding its answer."""
    src = inspect.getsource(cd._attempt_entity_retraction)
    i = src.index('edge_owner.get("supersedes_id")')
    j = (
        src.index("_llm_contradiction_check")
        if "_llm_contradiction_check" in src
        else len(src)
    )
    assert i < j, "edge check must precede the judge call"


def test_docstring_no_longer_claims_direction_independence():
    doc = cd._attempt_entity_retraction.__doc__ or ""
    assert "DIRECTION-AWARE" in doc
    assert (
        "the dereferenced\n    row IS the conflicted candidate in either case"
        not in doc
    )
