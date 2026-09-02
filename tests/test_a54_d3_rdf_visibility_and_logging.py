"""A54 / D3 — the RDF contradiction path could reach across a privacy boundary,
and detection was invisible unless it found something.

A54: ``memory_find_rdf_conflicts`` had no visibility predicate while its sibling
     ``memory_find_similar_candidates`` did, so the RDF route could select
     another agent's ``scope_agent`` row as a conflict candidate and then mark
     it outdated/conflicted — a status write into a row the writer cannot read.
D3:  the completion log fired only when conflicts were found, so "detection ran
     and found nothing" and "detection never ran" looked identical.

The A54 leak was REPRODUCED live before the fix (agent B's write turned agent
A's private row ``conflicted``) and is gone after it, with a positive control
proving same-owner detection still fires:

    2) agent B private write (contradicts A) -> A: active   sup=None   [untouched]
    3) agent A contradicts ITSELF            -> A: conflicted
                                                C: active   sup=<A.id>

Note the task filed only the RDF query. The live leak actually came through the
SEMANTIC query, so the fix covers all three contradiction candidate paths.
"""

import inspect

import pytest

from core_api.services import contradiction_detector as cd

pytestmark = pytest.mark.unit


# ── A54 ───────────────────────────────────────────────────────────────────


def test_storage_query_filters_on_visibility():
    from core_storage_api.services.postgres_service import PostgresService

    src = inspect.getsource(PostgresService.memory_find_rdf_conflicts)
    assert (
        "visibility"
        in inspect.signature(PostgresService.memory_find_rdf_conflicts).parameters
    )
    assert "Memory.visibility == visibility" in src


def test_scope_agent_also_pins_the_owning_agent():
    """``visibility == 'scope_agent'`` means "private to SOME agent", not "private
    to THIS agent" — so filtering on the tier alone still matches a different
    agent's private rows. The owner predicate is the part that actually isolates."""
    from core_storage_api.services.postgres_service import PostgresService

    src = inspect.getsource(PostgresService.memory_find_rdf_conflicts)
    assert 'visibility == "scope_agent"' in src
    assert "Memory.agent_id == agent_id" in src


def test_visibility_is_optional_at_the_storage_boundary():
    """core-api and core-storage-api deploy independently. A storage instance
    running ahead of core-api must keep serving callers that don't send the new
    params rather than 422-ing every RDF lookup."""
    from core_storage_api.services.postgres_service import PostgresService

    sig = inspect.signature(PostgresService.memory_find_rdf_conflicts)
    assert sig.parameters["visibility"].default is None
    assert sig.parameters["agent_id"].default is None


def test_client_forwards_visibility_and_agent():
    from core_api.clients.storage_client import CoreStorageClient

    src = inspect.getsource(CoreStorageClient.find_rdf_conflicts)
    assert 'params["visibility"] = visibility' in src
    assert 'params["agent_id"] = agent_id' in src


def test_detector_always_sends_the_writers_scope():
    """The whole fix is defeated if the one caller forgets to pass it, so pin
    the call site — not just the plumbing underneath it."""
    src = inspect.getsource(cd)
    call = src[src.index("rdf_conflicts = await sc.find_rdf_conflicts(") :][:2200]
    assert 'visibility=new_memory.get("visibility", "scope_team")' in call
    assert 'agent_id=new_memory.get("agent_id")' in call


def test_every_contradiction_candidate_query_pins_the_owner():
    """The leak reproduced through the SEMANTIC path even after the RDF path was
    fixed — scoping one query is not scoping the boundary. All three candidate
    routes must carry the owner predicate or the hole simply moves."""
    from core_storage_api.services.postgres_service import PostgresService

    for fn in (
        "memory_find_rdf_conflicts",
        "memory_find_similar_candidates",
        "memory_find_entity_overlap_candidates",
    ):
        src = inspect.getsource(getattr(PostgresService, fn))
        assert "Memory.agent_id == agent_id" in src, f"{fn} does not pin the owner"
        assert 'visibility == "scope_agent"' in src, (
            f"{fn} pins the owner unconditionally"
        )


def test_rdf_scoping_matches_the_semantic_path():
    """The two candidate routes must agree on the privacy boundary. A54 existed
    precisely because they diverged; this fails if they diverge again."""
    src = inspect.getsource(cd)
    sem = src.count('"visibility": new_memory.get("visibility", "scope_team")')
    rdf = src.count('visibility=new_memory.get("visibility", "scope_team")')
    assert sem >= 1 and rdf >= 1


# ── D3 ────────────────────────────────────────────────────────────────────


def test_completion_is_logged_even_when_nothing_is_found():
    src = inspect.getsource(cd)
    i = src.index("Async contradiction detection completed for memory")
    # Walk back to the nearest enclosing statement: it must NOT be guarded by
    # `if contradictions:` — that guard is exactly the bug.
    preceding = src[:i]
    last_if = preceding.rfind("if contradictions:")
    last_log_block = preceding.rfind("logger.info(")
    assert last_if < last_log_block, (
        "completion log is still inside `if contradictions:`"
    )


def test_completion_log_carries_the_conflict_count():
    src = inspect.getsource(cd)
    block = src[src.index("Async contradiction detection completed for memory") :][:600]
    assert "n_conflicts" in block
    assert '"conflicts_found"' in block  # structured, greppable in log search
    assert '"memory_id"' in block
