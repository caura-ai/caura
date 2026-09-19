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


def _writer_session_methods() -> dict[str, set[str]]:
    _, tree, lines = _source()
    out: dict[str, set[str]] = {}
    for node in ast.walk(tree):
        if not isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef)):
            continue
        if node.name == "get_session" or "get_session()" not in _segment(lines, node):
            continue
        out[node.name] = _write_markers(node)
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
    """
    methods = _writer_session_methods()
    pure = {name for name, marks in methods.items() if not marks}

    assert len(methods) == 136, f"{len(methods)} methods open a writer session"
    assert len(pure) == 63, f"{len(pure)} of them show no write marker"


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
