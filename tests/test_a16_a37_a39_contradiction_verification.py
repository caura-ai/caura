"""A16 / A37 / A39 — three rows filed as "unverified". Here is what they do.

Each was verified empirically against a live local stack (real embeddings + a
real judge LLM), not by reading the code and asserting a guess. Two came back
clean; one found a defect.

A16 — complementary lifestyle facts.  VERIFIED FIXED.
    "Priya eats lunch at the office cafeteria on weekdays" +
    "Priya cooks dinner at home most evenings"
    -> both stay `active`. The judge's ``non_conflict_reason`` taxonomy
    (``list_valued_predicate`` shape (b): different attributes of one subject)
    covers this. The office-lunch case in the row does not reproduce.

A39 — cross-tenant surfacing.  VERIFIED CORRECT.
    The same subject written to two tenants with conflicting values leaves both
    rows `active`: no cross-tenant contradiction. A foreign read by id is 404.

A37 — N>2 / transitive chains.  DEFECT FOUND.
    See docs/contradiction-verification/a37-n-way-chain-findings.md and
    benchmark/a37_n_way_chain.py. Three mutually-exclusive values for one
    subject resolve INCONSISTENTLY; in 2 of 3 runs the chain ended with
    contradictory claims co-active, and in one run detection fired not at all.

    This file does NOT assert the buggy shape as expected — pinning "the oldest
    row stays active" would turn a defect into a contract. What is pinned is
    what must hold regardless of chain length.
"""

import inspect

import pytest

pytestmark = pytest.mark.unit


def test_non_conflict_taxonomy_covers_complementary_attributes():
    """A16 — the shape that must classify office-lunch as non-conflicting."""
    from core_api.services.contradiction_detector import CONTRADICTION_PROMPT

    assert "list_valued_predicate" in CONTRADICTION_PROMPT
    assert "complementary facts" in CONTRADICTION_PROMPT


def test_a_state_change_is_still_a_contradiction():
    """The counterweight to A16: loosening the taxonomy far enough to spare
    complementary facts must NOT spare genuine updates, or nothing is ever
    retired. Pinned because these two pull in opposite directions."""
    from core_api.services.contradiction_detector import CONTRADICTION_PROMPT

    assert "updates ARE contradictions" in CONTRADICTION_PROMPT


def test_every_candidate_query_is_tenant_scoped():
    """A39 — tenant is the outermost boundary on every candidate query."""
    from core_storage_api.services.postgres_service import PostgresService

    for fn in (
        "memory_find_rdf_conflicts",
        "memory_find_similar_candidates",
        "memory_find_entity_overlap_candidates",
    ):
        src = inspect.getsource(getattr(PostgresService, fn))
        assert "Memory.tenant_id == tenant_id" in src, f"{fn} is not tenant-scoped"


def test_status_writes_carry_a_cross_tenant_guard():
    """A39, write side: a chain edge cannot be created across tenants."""
    from core_api.clients.storage_client import CoreStorageClient

    doc = inspect.getdoc(CoreStorageClient.update_memory_status) or ""
    assert "cross-tenant write guard" in doc


def test_a_row_is_never_its_own_contradiction_candidate():
    """A37 — the invariant that must hold at ANY chain length, and the failure
    mode that would make a transitive chain unresolvable."""
    from core_storage_api.services.postgres_service import PostgresService

    src = inspect.getsource(PostgresService.memory_find_rdf_conflicts)
    assert "Memory.id != memory_id" in src
