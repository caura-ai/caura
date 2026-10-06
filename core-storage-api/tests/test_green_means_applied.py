"""Two ways this service reports work it did not do.

A migration that stamps green over an index it did not build, and a bootstrap
that stamps the whole chain over migrations that never ran. Both fail in the
quiet direction: no error at the time, and none later either, because the
recorded state says there is nothing left to do.

The migration scan here resolves calls through the AST rather than grepping
source, and that is not fastidiousness. Grepping got the answer wrong three
times while this was being written: it counted ``CREATE INDEX CONCURRENTLY``
inside a COMMENT as a build, it read ``{_INDEX_NAME}`` f-strings as an index
literally named ``ON``, and it missed the cleanup in 035 and 040 entirely
because their helper is defined outside ``upgrade()``. Each wrong answer
looked plausible. The repo's other guard on these migrations
(``test_no_plain_create_index_on_large_tables``) is a regex, and migrations
035/040/048 carry comments explaining the contortions needed to stay inside
its coverage — 007 and 026 do the right thing and are invisible to it.
"""

from __future__ import annotations

import ast
import pathlib
import re

import pytest

_REPO = pathlib.Path(__file__).resolve().parents[2]
_VERSIONS = _REPO / "core-storage-api/src/core_storage_api/database/migrations/versions"

_CREATE_CONCURRENTLY = re.compile(r"CREATE\s+(?:UNIQUE\s+)?INDEX\s+CONCURRENTLY", re.I)
_DROP_CONCURRENTLY = re.compile(r"DROP\s+INDEX\s+CONCURRENTLY", re.I)


def _segment(source: str, node: ast.AST) -> str:
    return "\n".join(source.splitlines()[node.lineno - 1 : node.end_lineno])


def _executed_sql(fn: ast.FunctionDef, source: str) -> str:
    """SQL text reachable from *fn*: every string constant under an ``execute``
    call. Deliberately NOT the function's source — a comment mentioning
    ``CREATE INDEX CONCURRENTLY`` is prose, not a build."""
    out: list[str] = []
    for node in ast.walk(fn):
        if not (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "execute"
            and node.args
        ):
            continue
        out.extend(
            sub.value
            for sub in ast.walk(node.args[0])
            if isinstance(sub, ast.Constant) and isinstance(sub.value, str)
        )
    return "\n".join(out)


def _reachable_source(fn: ast.FunctionDef, funcs: dict[str, ast.FunctionDef], source: str) -> str:
    """*fn*'s source plus the source of every module-level function it calls."""
    called = {n.func.id for n in ast.walk(fn) if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}
    parts = [_segment(source, fn)]
    parts.extend(_segment(source, funcs[name]) for name in sorted(called) if name in funcs)
    return "\n".join(parts)


def _concurrent_index_migrations() -> list[tuple[str, bool]]:
    """``(filename, handles_an_interrupted_prior_build)`` for every migration
    whose ``upgrade()`` builds an index concurrently."""
    out: list[tuple[str, bool]] = []
    for path in sorted(_VERSIONS.glob("[0-9]*.py")):
        source = path.read_text()
        tree = ast.parse(source)
        funcs = {n.name: n for n in tree.body if isinstance(n, ast.FunctionDef)}
        upgrade = funcs.get("upgrade")
        if upgrade is None:
            continue
        sql = _executed_sql(upgrade, source)
        if not _CREATE_CONCURRENTLY.search(sql):
            continue
        reachable = _reachable_source(upgrade, funcs, source)
        handled = (
            # Queries pg_index for a half-built index, inline or via a helper.
            "indisvalid" in reachable
            or "drop_invalid_indexes" in reachable
            # Drops the index unconditionally before rebuilding it (012, 037,
            # 045): an INVALID leftover is dropped along with a valid one.
            or bool(_DROP_CONCURRENTLY.search(sql))
        )
        out.append((path.name, handled))
    return out


def test_the_scan_sees_the_migrations_it_is_asserting_about() -> None:
    found = _concurrent_index_migrations()
    assert len(found) >= 15, f"only {len(found)} concurrent-index migrations found — the scan is broken"


@pytest.mark.parametrize(
    "name,handled", _concurrent_index_migrations(), ids=lambda v: v if isinstance(v, str) else ""
)
def test_every_concurrent_index_build_survives_an_interrupted_prior_run(name: str, handled: bool) -> None:
    """``CREATE INDEX CONCURRENTLY IF NOT EXISTS`` alone is not idempotent.

    An interrupted build leaves the index in ``pg_index`` with
    ``indisvalid = false``; ``IF NOT EXISTS`` then skips it forever, and the
    planner will not use it. The migration reports success over an index that
    is useless and still charges write overhead on every insert.
    """
    assert handled, (
        f"{name} builds an index CONCURRENTLY without clearing a half-built one first. "
        "Call drop_invalid_indexes(...) from core_storage_api.database.migration_helpers "
        "inside the autocommit_block, before the CREATE."
    )


def _add_columns_in_migrations_with_an_autocommit_block() -> list[tuple[str, int, bool]]:
    """``(filename, line, idempotent)`` for every column add in an ``upgrade()``
    that also opens an ``autocommit_block``.

    Entering the block COMMITS the migration's transaction, while
    ``alembic_version`` moves only after ``upgrade()`` returns. A run killed in
    the block therefore leaves the column committed and the revision unrecorded,
    and the retry re-runs ``upgrade()`` from the top.
    """
    out: list[tuple[str, int, bool]] = []
    for path in sorted(_VERSIONS.glob("[0-9]*.py")):
        source = path.read_text()
        tree = ast.parse(source)
        upgrade = next((n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "upgrade"), None)
        if upgrade is None:
            continue
        calls = [
            n for n in ast.walk(upgrade) if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
        ]
        if not any(c.func.attr == "autocommit_block" for c in calls):  # type: ignore[attr-defined]
            continue
        for call in calls:
            if call.func.attr == "add_column":  # type: ignore[attr-defined]
                idempotent = any(
                    kw.arg == "if_not_exists"
                    and isinstance(kw.value, ast.Constant)
                    and kw.value.value is True
                    for kw in call.keywords
                )
                out.append((path.name, call.lineno, idempotent))
        for sql in _executed_sql(upgrade, source).split("\n"):
            if re.search(r"ADD\s+COLUMN", sql, re.I):
                out.append((path.name, 0, bool(re.search(r"ADD\s+COLUMN\s+IF\s+NOT\s+EXISTS", sql, re.I))))
    return out


def test_the_add_column_scan_sees_the_migrations_it_is_asserting_about() -> None:
    names = {name for name, _, _ in _add_columns_in_migrations_with_an_autocommit_block()}
    assert {"026_audit_client_event_id.py", "037_memories_embedded_content_hash.py"} <= names, (
        f"the scan found {sorted(names)} — it is no longer seeing column adds"
    )


@pytest.mark.parametrize(
    "name,line,idempotent",
    [
        pytest.param(*case, id=f"{case[0]}:{case[1]}")
        for case in _add_columns_in_migrations_with_an_autocommit_block()
    ],
)
def test_a_column_add_before_an_autocommit_block_survives_a_retry(
    name: str, line: int, idempotent: bool
) -> None:
    """A plain ``ADD COLUMN`` here turns one interrupted index build into a
    wedge: every retry fails on the column the first attempt already committed,
    before it reaches the index. ``test_migration_retry.py`` reproduces it."""
    assert idempotent, (
        f"{name}:{line} adds a column without IF NOT EXISTS in an upgrade() that opens an "
        "autocommit_block. Pass if_not_exists=True to op.add_column (or use "
        "'ADD COLUMN IF NOT EXISTS', as 026 does)."
    )


def test_the_stamp_sentinel_is_still_a_migration_only_table() -> None:
    """``init_database`` refuses to stamp unless this table is present.

    Its whole value is that the migration chain creates it and
    ``Base.metadata.create_all`` cannot — so if a model ever declares it, the
    discriminator silently stops discriminating and the refusal stops firing
    on exactly the databases it exists to catch.
    """
    import common.models  # noqa: F401
    from common.models.base import Base
    from core_storage_api.database.init import _CHAIN_SENTINEL_TABLE

    assert Base.metadata.tables, "no ORM tables registered — the import is not doing its job"
    assert _CHAIN_SENTINEL_TABLE not in Base.metadata.tables, (
        f"{_CHAIN_SENTINEL_TABLE} now has an ORM model, so create_all would create it too "
        "and it can no longer tell a chain-built schema from a create_all one"
    )
    creators = [p.name for p in _VERSIONS.glob("[0-9]*.py") if _CHAIN_SENTINEL_TABLE in p.read_text()]
    assert creators, f"no migration mentions {_CHAIN_SENTINEL_TABLE}"


# ``(has_tables, has_alembic_version, has_chain_evidence, has_head_evidence)
# -> action`` over every combination, because the old code's mistake was in a
# combination nobody had written down. The rows that matter are the last four:
# same database shape, and the only thing separating "record what the schema
# shows" from "guess" is whether the chain left its fingerprint — at 019 AND at
# head. The sentinel alone used to be enough, and stamped head over a database
# that had stopped anywhere after 019.
_BOOTSTRAP_CASES = [
    (False, False, False, False, "upgrade"),  # empty database — run the whole chain
    (False, False, True, False, "upgrade"),  # sentinel without memories: still not tracked
    (False, False, True, True, "upgrade"),
    (False, False, False, True, "upgrade"),
    (False, True, False, False, "upgrade"),  # tracked, tables dropped — chain decides
    (False, True, True, True, "upgrade"),
    (True, True, False, False, "upgrade"),  # the steady state: tracked, run what's pending
    (True, True, True, False, "upgrade"),  # tracked and behind head: the ordinary upgrade
    (True, True, True, True, "upgrade"),
    (True, True, False, True, "upgrade"),
    (True, False, True, True, "stamp"),  # chain-built through head, version row lost
    (True, False, True, False, "refuse"),  # chain-built, revision unknown: stamping head skips the gap
    (True, False, False, False, "refuse"),  # create_all-shaped: the silent wedge
    (True, False, False, True, "refuse"),  # head's object without the chain's: still create_all-shaped
]


@pytest.mark.parametrize("tables,version,evidence,head,expected", _BOOTSTRAP_CASES)
def test_the_bootstrap_decision_is_the_same_for_every_probe_combination(
    tables: bool, version: bool, evidence: bool, head: bool, expected: str
) -> None:
    from core_storage_api.database.init import schema_bootstrap_action

    assert (
        schema_bootstrap_action(
            has_tables=tables,
            has_alembic_version=version,
            has_chain_evidence=evidence,
            has_head_evidence=head,
        )
        == expected
    )


def test_a_chain_built_database_short_of_head_is_refused_rather_than_stamped() -> None:
    """The sentinel proves migration 019 ran, nothing later. A database that
    stopped after 019 and lost its version row was stamped at head, and every
    migration in between was skipped for good."""
    from core_storage_api.database.init import schema_bootstrap_action

    assert (
        schema_bootstrap_action(
            has_tables=True, has_alembic_version=False, has_chain_evidence=True, has_head_evidence=False
        )
        == "refuse"
    )


def test_the_head_fingerprint_names_the_current_head() -> None:
    """``init_database`` stamps head only over the head migration's fingerprint,
    and a fingerprint written for an older head proves nothing about this one.
    Boot treats a stale entry as no evidence, which is safe but turns a
    legitimate stamp into a refusal — so a new migration must move it."""
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    from core_storage_api.database.init import _HEAD_FINGERPRINT_REVISION

    cfg = Config()
    cfg.set_main_option("script_location", str(_VERSIONS.parent))
    head = ScriptDirectory.from_config(cfg).get_current_head()
    assert head == _HEAD_FINGERPRINT_REVISION, (
        f"the newest migration is {head}, but init.py's head fingerprint still describes "
        f"{_HEAD_FINGERPRINT_REVISION}. Point _HEAD_FINGERPRINT_REVISION/_HEAD_FINGERPRINT_SQL at an "
        "object the new head creates (or set the SQL to None if it creates none)."
    )


def test_a_create_all_shaped_database_is_refused_rather_than_stamped() -> None:
    """The case the old code got wrong, stated on its own.

    ``tests/conftest.py`` builds its schema with ``Base.metadata.create_all``,
    which leaves ``memories`` present, ``alembic_version`` absent and every
    migration-only object missing. Pointing this service at that database used
    to stamp it at head — CONTRIBUTING.md documents the consequence, and
    documentation was the only thing standing between an operator and a
    permanently under-provisioned database.
    """
    from core_storage_api.database.init import schema_bootstrap_action

    assert (
        schema_bootstrap_action(
            has_tables=True, has_alembic_version=False, has_chain_evidence=False, has_head_evidence=False
        )
        == "refuse"
    )


def test_the_bootstrap_docstring_names_every_action_it_can_take() -> None:
    """``init_database``'s docstring is the only place the three-way decision
    is described in prose, and prose is what drifted here before: it went on
    saying "stamps the current revision" for the whole life of this change,
    describing a branch that had become one of three.

    Coupled mechanically rather than by review: every action
    ``schema_bootstrap_action`` can return must appear in the docstring, so
    adding or renaming one forces the description to be revisited.
    """
    from core_storage_api.database.init import init_database, schema_bootstrap_action

    actions = {
        schema_bootstrap_action(
            has_tables=t, has_alembic_version=v, has_chain_evidence=e, has_head_evidence=h
        )
        for t in (True, False)
        for v in (True, False)
        for e in (True, False)
        for h in (True, False)
    }
    assert actions == {"upgrade", "stamp", "refuse"}, f"decision returns {actions}"

    doc = init_database.__doc__ or ""
    assert doc, "init_database has no docstring"
    missing = sorted(a for a in actions if a not in doc)
    assert not missing, f"init_database's docstring does not mention: {missing}"
