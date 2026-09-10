"""reg-a36 — a paraphrased predicate hid a real contradiction.

``SINGLE_VALUE_PREDICATES`` declares WHICH attributes hold only one value at a
time. It never said which of its own members name the SAME attribute, and
``memory_find_rdf_conflicts`` matched predicates with exact equality::

    func.lower(Memory.predicate) == predicate.lower()

So a subject whose deploy target was recorded once as ``deployed_to: staging``
and later as ``deployed_on: production`` was never compared: no conflict
raised, both rows left live and unpenalised, and a reader got whichever ranked
higher. The predicate is now expanded to its alias cluster.

The failure direction is the OPPOSITE of A35's, and that governs the whole
design. On the object side a wrong equivalence SUPPRESSES a real contradiction,
so normalising broadly was safe. Here a wrong alias MANUFACTURES a contradiction
between two facts that were never competing, and the loser takes a 0.5 ranking
penalty. Hence a narrow, enumerated map instead of a general rule — and hence
the refusals below, which are as much the fix as the clusters are.
"""

import inspect

import pytest

from common.constants import (
    _PREDICATE_ALIAS_CLUSTERS,
    PREDICATE_ALIASES,
    SINGLE_VALUE_PREDICATES,
    predicate_cluster,
)

pytestmark = pytest.mark.unit


# ── the map's own invariants ──────────────────────────────────────────────


def test_every_clustered_predicate_is_a_single_value_predicate():
    """A cluster member outside ``SINGLE_VALUE_PREDICATES`` is unreachable: the
    detector gates on membership before it ever calls the query, so the alias
    would sit in the map looking effective and never fire."""
    stray = sorted(
        m
        for cluster in _PREDICATE_ALIAS_CLUSTERS
        for m in cluster
        if m not in SINGLE_VALUE_PREDICATES
    )
    assert stray == []


def test_no_predicate_belongs_to_two_clusters():
    """Two clusters sharing a member would make the attribute's identity depend
    on which spelling the writer happened to use."""
    seen: dict[str, tuple[str, ...]] = {}
    collisions = []
    for cluster in _PREDICATE_ALIAS_CLUSTERS:
        for m in cluster:
            if m in seen:
                collisions.append((m, seen[m], cluster))
            seen[m] = cluster
    assert collisions == []


def test_no_alias_chains():
    """Every canonical must be a terminal. If a canonical were itself an alias,
    resolving once would land on a form that resolves again, and two callers
    doing different numbers of passes would disagree."""
    canonicals = set(PREDICATE_ALIASES.values())
    assert [a for a in PREDICATE_ALIASES if a in canonicals] == []


def test_canonical_is_the_first_member_and_is_itself_in_its_cluster():
    for cluster in _PREDICATE_ALIAS_CLUSTERS:
        canonical = cluster[0]
        assert canonical in predicate_cluster(canonical)
        for alias in cluster[1:]:
            assert PREDICATE_ALIASES[alias] == canonical


# ── what now compares ─────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "a,b",
    [
        ("status", "current_status"),
        ("state", "status"),
        ("deployed_to", "deployed_on"),
        ("hosted_on", "hosted_at"),
        ("runs_on", "running_on"),
        ("located_in", "based_in"),
        ("headquartered_in", "hq_in"),
        ("lives_in", "resides_in"),
        ("reports_to", "supervisor"),
        ("assigned_to", "assignee"),
        ("email", "email_address"),
        ("phone", "phone_number"),
        ("spouse", "married_to"),
        ("employer", "employed_by"),
        ("birthdate", "date_of_birth"),
        ("due_date", "deadline"),
        ("version", "current_version"),
    ],
)
def test_paraphrases_of_one_attribute_now_meet(a, b):
    assert predicate_cluster(a) == predicate_cluster(b)


# ── what deliberately still does NOT compare ──────────────────────────────


@pytest.mark.parametrize(
    "a,b,why",
    [
        ("manager", "manager_of", "inverse: X's manager vs X manages Y"),
        ("head_of", "headed_by", "inverse"),
        ("ceo", "ceo_of", "inverse"),
        ("maintainer", "maintainer_of", "inverse"),
        ("title", "job_title", "a title is also a document's title"),
        ("cost", "price", "what it costs to make vs what it sells for"),
        ("version", "latest_version", "latest-released vs currently-deployed"),
        ("version", "running_version", "latest-released vs currently-deployed"),
        ("start_date", "started_at", "planned vs actual"),
        ("end_date", "ended_at", "planned vs actual"),
        ("located_in", "city", "granularity, not disagreement"),
        ("located_in", "country", "granularity, not disagreement"),
        ("located_in", "region", "granularity, not disagreement"),
        ("resides_in", "resides_at", "a city is not a street address"),
        ("located_in", "address", "granularity, not disagreement"),
        ("status", "role", "two different attributes"),
        ("score", "rating", "two different metrics"),
    ],
)
def test_refusals_stay_refused(a, b, why):
    """The load-bearing half of A36.

    Each pair here would, if aliased, invent a contradiction out of two facts
    that were never in competition — and the losing row would take a 0.5
    ranking penalty for it. The inverse pairs are the sharpest case: aliasing
    "X's manager is Y" to "X is the manager of Y" would read an ordinary org
    chart as self-contradictory.
    """
    assert predicate_cluster(a) != predicate_cluster(b), why


def test_inverse_predicates_are_in_no_cluster_at_all():
    """Belt and braces on the whole ``*_of`` family, so a future cluster cannot
    pull one in without this failing."""
    clustered = {m for cluster in _PREDICATE_ALIAS_CLUSTERS for m in cluster}
    inverses = {p for p in SINGLE_VALUE_PREDICATES if p.endswith("_of")}
    assert inverses, "guard would be vacuous if the set had no *_of predicates"
    assert sorted(inverses & clustered) == []


# ── the resolver's contract ───────────────────────────────────────────────


def test_unclustered_predicate_passes_through_unchanged():
    """An unaliased predicate must keep exactly today's behaviour: a
    single-member IN is the same query as the equality it replaced."""
    assert predicate_cluster("works_on") == frozenset({"works_on"})
    assert predicate_cluster("depends_on_completion_of") == frozenset(
        {"depends_on_completion_of"}
    )


@pytest.mark.parametrize("raw", ["Status", "  status ", "STATUS", "status"])
def test_lookup_is_case_and_whitespace_insensitive(raw):
    assert predicate_cluster(raw) == predicate_cluster("status")


@pytest.mark.parametrize("raw", ["", "   ", None])
def test_empty_predicate_does_not_explode(raw):
    """The detector gates on a truthy predicate, but the resolver is public and
    must not raise if something else reaches it."""
    assert predicate_cluster(raw) == frozenset({""})


# ── the query actually uses it ────────────────────────────────────────────


def test_the_query_expands_the_cluster():
    from core_storage_api.services.postgres_service import PostgresService

    src = inspect.getsource(PostgresService.memory_find_rdf_conflicts)
    assert "predicate_cluster(predicate)" in src
    # the equality this replaced must be gone, or aliases would never widen
    # anything — the IN would sit beside a filter that already excluded them
    assert "func.lower(Memory.predicate) == predicate.lower()" not in src


def test_expansion_is_ordered_so_the_sql_is_stable():
    """``frozenset`` iteration order is not stable across processes. An IN list
    built straight from the set would produce a different SQL string per worker
    and defeat statement caching."""
    from core_storage_api.services.postgres_service import PostgresService

    src = inspect.getsource(PostgresService.memory_find_rdf_conflicts)
    assert "sorted(predicate_cluster(predicate))" in src


def test_object_side_normalisation_still_applies():
    """A35 and A36 touch adjacent lines of one WHERE clause. Neither may quietly
    drop the other."""
    from core_storage_api.services.postgres_service import PostgresService

    src = inspect.getsource(PostgresService.memory_find_rdf_conflicts)
    assert src.count("_normalized_object_sql(") == 2


def test_the_destructive_merge_path_was_left_alone():
    """A71's ``_same_claim`` compares predicates with the same exact equality —
    but a match there RETIRES a row, where a match in the contradiction path
    only flags one. Widening a destructive comparison is a separate decision
    with a separate risk profile, and it is deliberately not taken here.

    This test pins that choice so a later reader sees an intentional asymmetry
    rather than a missed call site.
    """
    from core_api.pipeline.steps.write.detect_near_duplicate import _same_claim

    src = inspect.getsource(_same_claim)
    assert "predicate_cluster" not in src
