"""Which session each service method opens, and why the answer is not mechanical.

``postgres_service`` has two session helpers. ``get_session()`` is the writer:
primary engine, wrapped in an explicit transaction. ``get_read_session()`` is
the reader: routes to the replica when ``read_database_url`` is set, and opens
no transaction.

oss-0902-l-52 observes that dozens of pure read paths open the writer. They do.
What this file records is why converting them is not a find-and-replace, and it
is worth stating before anyone tries:

1. **A method can be all-SELECT and still write.** ``agent_update_fleet``
   issues one ``select`` and then assigns ``agent.fleet_id = fleet_id``. The
   flush at transaction exit is the write; there is no INSERT, UPDATE or
   ``session.add`` anywhere in it. On the reader — no transaction, and a
   read-only connection against a replica — that update silently does not
   happen.

2. **A method can read its own writes.** ``entity_resolve_duplicates`` says so
   in its own docstring: the merge loop re-reads rows it mutates, inside
   SAVEPOINTs an HTTP boundary cannot express.

3. **Read-after-write is a property of the CALLER, not the method.** A method
   that only selects is still wrong on a replica if a request writes and then
   calls it, because replica lag is not visible from inside the method. That
   question cannot be answered by looking at ``postgres_service`` at all.

So this pins the population rather than draining it: the counts below move only
when someone deliberately changes them, and the classifier is here for whoever
does the per-method caller analysis that an actual conversion needs.

In OSS the conversion is a no-op either way — ``get_read_engine()`` returns the
writer engine unless ``read_database_url`` is set — which is precisely why a
wrong conversion would pass every test here and fail only in a replica
deployment.
"""

from __future__ import annotations

import ast
import pathlib
import re

import pytest

_SERVICE = (
    pathlib.Path(__file__).resolve().parents[2]
    / "core-storage-api/src/core_storage_api/services/postgres_service.py"
)

_WRITE_CALLS = {"sql_update", "pg_insert", "insert", "update", "delete"}
_SESSION_MUTATORS = {"add", "add_all", "flush", "merge", "delete", "commit"}

# No trailing ``\b`` on the alternation. It used to be there, and it applied to
# the whole group — so ``UPDATE\s+\w`` ended mid-word and that branch could
# never match. Six raw-SQL writers classified as pure reads until this was
# found, including ``memory_archive_expired``.
_WRITE_SQL = re.compile(
    r"\b(INSERT\s+INTO|UPDATE\s+\w|DELETE\s+FROM|CREATE\s+|DROP\s+|ALTER\s+|TRUNCATE\s+)",
    re.I,
)

# Documents in its own docstring why it must stay on the writer.
_DOCUMENTED_READ_YOUR_WRITES = {"entity_resolve_duplicates"}

# The per-method caller analysis this file was built to receive, done
# 2026-09-20 against ``main`` at c02e5783. Each entry is a method that only
# selects and must STILL open the writer, with the caller that makes it so.
#
# Method: callers were traced in two passes, because either alone gives the
# wrong answer. Intra-request ordering inside the storage-api handlers finds
# the first two below; it clears ``document_get_by_pk``, which the module
# docstring above already records as unsafe. The second pass follows
# core-api's storage client across the HTTP boundary and finds the rest.
_MUST_STAY_ON_THE_WRITER = {
    "entity_resolve_duplicates": (
        "Its own docstring: the merge loop re-reads rows it mutates, inside "
        "SAVEPOINTs an HTTP boundary cannot express."
    ),
    "document_get_by_pk": (
        "routes/documents.py::upsert_document re-fetches by the returned id "
        "immediately after the upsert, and passes read=False for exactly this "
        "reason. Added because an agent that POSTed a document and read it "
        "back got a 404; a replica read reintroduces that bug."
    ),
    "document_get_by_doc_id": (
        "Same re-fetch. The two document lookups must not disagree about "
        "staleness, or the id path becomes unreliable while doc_id is not."
    ),
    "idempotency_get": (
        "routers/idempotency.py::claim_idempotency reads the conflicting row "
        "ONLY when idempotency_claim lost the race. Under lag the row it "
        "collided with reads as absent, and the route reports 'row vanished "
        "between conflict and SELECT' — the one signal the response's `found` "
        "field exists to distinguish."
    ),
    "memory_conflict_get": (
        "routers/memories.py::resolve_memory_conflict reads it twice after "
        "memory_conflict_resolve: once to tell 'gone' from 'already reviewed' "
        "(lag turns a 409 into a 404), and once to return the row it just "
        "wrote (lag returns it unresolved)."
    ),
    "agent_get_by_id": (
        "routes/agents.py::patch_agent_tune and services/agent_service.py::"
        "update_trust_level both re-fetch after writing. Unlike the document "
        "pair these had NO opt-out — get_agent took no `read` argument at all "
        "— which is its own fix, not this file's."
    ),
}

# Private helpers. They open a session but are called only from other methods
# in this module, so they are not independently convertible: whichever caller
# they serve decides, and converting one in isolation would give a single
# request two sessions with different views of the same rows.
_INTERNAL_HELPERS = {"_describe_content_hash_winner", "_guard_document_shrink"}


def _source() -> tuple[str, ast.Module, list[str]]:
    text = _SERVICE.read_text()
    return text, ast.parse(text), text.splitlines()


def _segment(lines: list[str], node: ast.AST) -> str:
    return "\n".join(lines[node.lineno - 1 : node.end_lineno])


def _strings(node: ast.AST):
    """Every string literal under *node*, f-strings included.

    ``ast.Constant`` alone misses ``text(f"UPDATE ...")`` — that is a
    ``JoinedStr`` — and it is how the archive/apply/restore methods write.
    """
    for sub in ast.walk(node):
        if isinstance(sub, ast.Constant) and isinstance(sub.value, str):
            yield sub.value
        elif isinstance(sub, ast.JoinedStr):
            yield "".join(
                part.value
                for part in sub.values
                if isinstance(part, ast.Constant) and isinstance(part.value, str)
            )


def _write_markers(fn: ast.AST) -> set[str]:
    marks: set[str] = set()
    for node in ast.walk(fn):
        if isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Name) and func.id in _WRITE_CALLS:
                marks.add(f"{func.id}()")
            if isinstance(func, ast.Attribute):
                if (
                    func.attr in _SESSION_MUTATORS
                    and isinstance(func.value, ast.Name)
                    and func.value.id == "session"
                ):
                    marks.add(f"session.{func.attr}")
                if func.attr in {"delete", "update", "insert"}:
                    marks.add(f".{func.attr}()")
                if func.attr.startswith("on_conflict") or func.attr == "with_for_update":
                    marks.add(func.attr)
        if isinstance(node, (ast.Assign, ast.AugAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for target in targets:
                if (
                    isinstance(target, ast.Attribute)
                    and isinstance(target.value, ast.Name)
                    and target.value.id not in {"self", "cls"}
                ):
                    marks.add("orm-attr-assign")
    for literal in _strings(fn):
        if _WRITE_SQL.search(literal):
            marks.add("raw-write-sql")
            break
    return marks


def _delegated_markers(fn: ast.AST, writers: set[str]) -> set[str]:
    """Writes this method performs through a helper in the same module.

    The fifth thing the classifier was blind to. It reads a method's OWN body,
    so a method that opens the session and hands it to a sibling holding the
    ``pg_insert`` shows no marker at all and lands in the "pure read" bucket —
    the same silent misclassification ``agent_update_fleet`` is pinned for,
    arriving by delegation rather than by an invisible statement. Converting
    such a method to the read replica would drop its writes without a word.

    Only ``self.``/``cls.`` calls to functions that this module itself
    classifies as writers count, so it cannot be fooled by a name.
    """
    marks: set[str] = set()
    for node in ast.walk(fn):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            continue
        target = node.func.value
        if isinstance(target, ast.Name) and target.id in {"self", "cls"} and node.func.attr in writers:
            marks.add(f"delegated:{node.func.attr}")
    return marks


def _writer_session_methods() -> dict[str, set[str]]:
    _, tree, lines = _source()
    functions = [node for node in ast.walk(tree) if isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef))]
    # Every writer in the module, INCLUDING the ones that take a session rather
    # than opening one — those are exactly the helpers the delegation check
    # needs to recognise, and they never appear in the result below.
    direct = {node.name: _write_markers(node) for node in functions}
    writers = {name for name, marks in direct.items() if marks}

    out: dict[str, set[str]] = {}
    for node in functions:
        if node.name == "get_session" or "get_session()" not in _segment(lines, node):
            continue
        out[node.name] = direct[node.name] | _delegated_markers(node, writers)
    return out


def test_the_classifier_agrees_with_methods_that_announce_themselves() -> None:
    """Cross-check from an independent signal: the method's own name.

    Every count below rests on the classifier, and the classifier was wrong
    four times while this was written — missing ``sql_update``, missing
    f-string SQL, missing ORM attribute assignment, and carrying a regex whose
    trailing word boundary disabled one branch. Each version looked right and
    reported a plausible number. A name that says "update" or "archive"
    classifying as a pure read is the cheapest way to notice.
    """
    verbs = (
        "update",
        "archive",
        "apply",
        "restore",
        "supersede",
        "redistribute",
        "mark_",
        "increment",
        "_set_",
        "delete",
        "purge",
        "upsert",
        "create",
        "claim",
        "release",
        "record",
    )
    pure = {name for name, marks in _writer_session_methods().items() if not marks}
    assert pure, "classifier found no pure reads at all — it is broken"

    mislabelled = sorted(
        name
        for name in pure
        if any(verb in name for verb in verbs)
        # ``find_by_supersedes_id`` reads BY a supersedes id; it supersedes nothing.
        and "find_by_supersedes" not in name
        and name not in _DOCUMENTED_READ_YOUR_WRITES
    )
    assert not mislabelled, f"named like writes, classified as reads: {mislabelled}"


def test_the_writer_session_population_is_pinned() -> None:
    """Counts, so the population cannot grow quietly.

    Not an instruction to drain it — see this module's docstring for why a
    mechanical conversion is unsafe. Two things legitimately move these numbers
    DOWN, and both mean updating them here: converting a method to
    ``get_read_session`` after doing the caller analysis, and deleting one that
    turns out to have no callers at all.

    138 -> 136 on 2026-09-19 by the second route (OSS-0814-L-53): ``relation_list``
    and ``fleet_get_node_ids_for_fleet`` were removed as zero-caller surfaces.
    Both were in the ``pure`` subset — readers holding a writer session — so
    they were part of the backlog this file pins, and deleting them retires two
    entries without anyone having to do the per-caller analysis first. The
    cheapest way off this list is not to need the method.

    136 -> 137 on 2026-09-19 (ax-0917-h-07): ``document_get_by_pk`` is new, and
    it joins the ``pure`` subset — it only selects. The writer session is the
    RIGHT choice for it, not a default inherited by copy-paste, and this module's
    own point 3 is why: read-after-write is a property of the CALLER. This method
    exists precisely because an agent that POSTs a document and reads it back by
    the returned ``id`` got a 404; served from a replica it would lag and 404
    again, reintroducing the bug it was added to fix. Its sibling
    ``document_get_by_doc_id`` holds the writer for the same reason, and the two
    document lookups must not disagree about staleness — that would make the
    ``id`` path unreliable while the ``doc_id`` path was not.

    137 -> 138 on 2026-09-22 (OSS-0814-L-37): ``relation_bulk_add`` is new — the
    batch behind ``POST /entities/relations/bulk``. It is plainly a writer and
    is NOT in the ``pure`` subset, which is only true because the classifier
    grew ``_delegated_markers`` in the same change: the method opens the session
    and hands it to ``_relation_upsert_and_fetch``, which holds the
    ``pg_insert``. Without that rule it would have been counted as a pure read
    and offered up as a conversion candidate.
    """
    methods = _writer_session_methods()
    pure = {name for name, marks in methods.items() if not marks}

    assert len(methods) == 138, f"{len(methods)} methods open a writer session"
    assert len(pure) == 64, f"{len(pure)} of them show no write marker"


@pytest.mark.parametrize(
    "method,why",
    [
        ("agent_update_fleet", "orm-attr-assign"),
        ("memory_archive_expired", "raw-write-sql"),
        ("memory_update_embedding", "sql_update()"),
        ("fleet_delete_node", ".delete()"),
    ],
)
def test_the_hazards_that_defeat_a_find_and_replace_are_detected(method: str, why: str) -> None:
    """Each of these is all-but-invisible in one of the four ways the classifier
    was blind to. ``agent_update_fleet`` is the important one: its only query is
    a SELECT, and routing it to the reader would drop the write silently."""
    marks = _writer_session_methods().get(method)
    assert marks is not None, f"{method} no longer opens a writer session"
    assert why in marks, f"{method}: expected {why}, got {sorted(marks)}"


def test_every_method_that_must_stay_on_the_writer_still_looks_like_a_pure_read() -> None:
    """The analysis is only interesting while these still classify as reads.

    Each entry in ``_MUST_STAY_ON_THE_WRITER`` is a method the classifier calls
    pure — no INSERT, no UPDATE, no ``session.add`` — that a CALLER nonetheless
    makes unsafe on a replica. If one grows a write marker the reasoning is no
    longer the interesting part, and the entry should move out rather than sit
    here implying a subtlety that is now obvious.
    """
    methods = _writer_session_methods()
    for name in _MUST_STAY_ON_THE_WRITER:
        assert name in methods, f"{name} no longer opens a writer session"
        assert not methods[name], (
            f"{name} now has write markers {sorted(methods[name])} — it is "
            "plainly a writer, so its caller-analysis entry is redundant"
        )


def test_the_convertible_population_is_pinned() -> None:
    """What is left after the caller analysis, so a conversion has a target.

    64 methods show no write marker. Six of them must stay on the writer anyway
    because of what CALLS them, and two are private helpers that inherit their
    caller's session. The remaining 56 are the candidates — the number a
    conversion PR is allowed to move, and the only number in this file that
    SHOULD go down.

    One precondition applies to all 56 and is not visible from here: core-api's
    storage client already splits reads at the SERVICE level (``_read_prefix``)
    with a per-call ``read=False`` opt-out. Converting a method sends it to the
    DB replica regardless of which deployment served the request, so the
    deployment handling ``read=False`` traffic must leave ``read_database_url``
    unset — otherwise a converted method quietly ignores the caller's opt-out
    and the two layers disagree.
    """
    methods = _writer_session_methods()
    pure = {name for name, marks in methods.items() if not marks}

    assert _MUST_STAY_ON_THE_WRITER.keys() <= pure
    assert pure >= _INTERNAL_HELPERS

    convertible = pure - set(_MUST_STAY_ON_THE_WRITER) - _INTERNAL_HELPERS
    assert len(convertible) == 56, (
        f"{len(convertible)} convertible candidates, expected 56 — "
        "update this and say which way it moved and why"
    )
