"""reg-a65 groundwork — populate the predicate so the RDF path can fire at all.

A63 wrote back ``subject_entity_id`` from the extractor and stopped there. Its
own comment explains why the column was NULL to begin with: the write-time
triple path fires only on narrow phrase regexes. But the deterministic RDF
contradiction path keys on **(subject, predicate)**, so filling one of the three
columns left it exactly as dormant as before — every row had a subject and no
predicate.

This is the same write-back for the other two columns, plus the canonicalisation
that makes them comparable across wordings.

SCOPE, stated plainly: this does NOT fix A65's two measured probe cases.
"finished the Snowflake cutover for analytics" vs "the analytics warehouse is
BigQuery" needs the INFERENCE that a cutover changes the warehouse — semantic
work that no vocabulary mapping does. The other candidate fix (a tiered judge)
targets those and is parked under the no-new-LLM-calls hold. What this does is
remove the precondition that made the deterministic path unable to fire for
anyone: predicate NULL everywhere.
"""

import inspect

import pytest

from core_api.services.entity_extraction_worker import _canonical_predicate as canon

pytestmark = pytest.mark.unit


# ── canonicalisation ──────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "a,b",
    [
        ("is located in", "located in"),
        ("is_based_in", "based_in"),
        ("has_status", "status"),
        ("is assigned to", "assigned-to"),
        ("was_deployed_to", "deployed to"),
        ("Managed By", "managed_by"),
        ("reports-to", "REPORTS_TO"),
    ],
)
def test_two_wordings_of_one_attribute_converge(a, b):
    """The whole point. If these produced different predicates, two rows about
    the same attribute would still never compare equal."""
    assert canon(a) == canon(b) is not None


def test_the_bare_form_wins_when_both_spellings_are_canonical():
    """``SINGLE_VALUE_PREDICATES`` contains BOTH ``is_located_in`` and
    ``located_in``. Returning whichever matched first would split one attribute
    across two canonical names — reintroducing the bug at the layer meant to fix
    it."""
    assert canon("is_located_in") == "located_in"
    assert canon("has_status") == "status"


def test_a_multi_valued_relation_is_refused():
    """``works_on`` can be true of five projects at once, so it is not a
    single-value attribute and a second value is not a replacement."""
    assert canon("works_on") is None
    assert canon("uses") is None


def test_an_unrecognised_relation_is_refused_rather_than_guessed():
    """A wrong predicate is worse than none: the RDF path treats
    (subject, predicate) as authoritative and would compare two unrelated
    attributes as the same one."""
    assert canon("total gibberish") is None
    assert canon("") is None
    assert canon(None) is None


def test_it_does_not_invent_predicates_by_stripping():
    """Prefix-stripping only returns a form that is ITSELF canonical, so it can
    never manufacture a predicate the vocabulary does not contain."""
    from common.constants import SINGLE_VALUE_PREDICATES

    for probe in ("is_nonsense", "has_flurble", "was_widget"):
        out = canon(probe)
        assert out is None or out in SINGLE_VALUE_PREDICATES


# ── the write-back ────────────────────────────────────────────────────────


def _worker_src() -> str:
    from core_api.services import entity_extraction_worker as w

    return inspect.getsource(w)


def test_it_writes_back_only_on_exactly_one_canonical_relation():
    """A63's ambiguity rule, for A63's reason: two canonical relations about one
    subject means we cannot tell which attribute the row asserts."""
    src = _worker_src()
    assert "if len(canonical_rels) == 1:" in src
    assert "skipped_ambiguous" in src


def test_the_relation_must_start_at_the_subject():
    """A relation about some other entity in the same memory is not this row's
    claim."""
    src = _worker_src()
    assert "rel.from_entity == subject_name" in src


def test_it_does_not_reach_into_the_subject_writebacks_scope():
    """``subject_ids`` is computed inside a conditional block; referencing it
    from the outer level NameErrors on every path where that block did not run.
    Recomputed from ``filtered`` instead."""
    src = _worker_src()
    tail = src[src.index("A65: predicate write-back") :]
    assert "subject_names = {" in tail
    assert "subject_ids" not in tail


def test_both_columns_are_written_together():
    """A predicate with no object names an attribute with no value, which the
    RDF comparison reads as a claim nothing can conflict with."""
    from core_api.clients.storage_client import CoreStorageClient

    sig = inspect.signature(CoreStorageClient.set_predicate_if_null)
    assert "predicate" in sig.parameters and "object_value" in sig.parameters


def test_the_regex_paths_value_is_never_clobbered():
    """Storage guards on ``predicate IS NULL`` — when the write-time triple path
    fired, its value came from a deterministic match on the original text and
    outranks this async write-back."""
    from core_storage_api.services.postgres_service import PostgresService

    src = inspect.getsource(PostgresService.memory_set_predicate_if_null)
    assert "Memory.predicate.is_(None)" in src
    assert "Memory.tenant_id == tenant_id" in src
    assert "Memory.deleted_at.is_(None)" in src


def test_a_failure_is_non_fatal():
    """Same contract as the subject write-back: the row keeps a NULL predicate,
    which is today's behaviour for every row."""
    src = _worker_src()
    tail = src[src.index("A65: predicate write-back") :]
    assert "non-fatal" in tail
    assert "logger.warning" in tail


def test_every_outcome_is_logged():
    """set / kept_existing / ambiguous / no_canonical must be distinguishable —
    otherwise "no predicate found" and "never ran" look identical, which is the
    D3 ambiguity this codebase has already paid for twice."""
    src = _worker_src()
    for outcome in ("predicate_writeback", "skipped_ambiguous", "skipped_no_canonical"):
        assert outcome in src
