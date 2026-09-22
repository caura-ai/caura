"""Guard migrations whose DDL failures are deliberately non-fatal.

Soft failure is sometimes the right portability choice, but a green Alembic
revision then proves only that the exception was swallowed. Every such
migration needs a runtime predicate that proves its intended effect exists.

Like ``test_green_means_applied.py``, this scan follows module-level helpers
called by ``upgrade()`` and resolves SQL passed to ``op.execute`` instead of
grepping the whole file. That keeps prose, downgrade-only SQL and unrelated
helpers from creating false positives that would train maintainers to ignore
the guard.
"""

from __future__ import annotations

import ast
import pathlib
import re
from dataclasses import dataclass

import pytest

from core_storage_api.database.migration_postconditions import MIGRATION_POSTCONDITIONS

_REPO = pathlib.Path(__file__).resolve().parents[2]
_VERSIONS = _REPO / "core-storage-api/src/core_storage_api/database/migrations/versions"

_DO_BLOCK = re.compile(
    r"\bDO\s+\$(?P<tag>[A-Za-z0-9_]*)\$(?P<body>.*?)\$(?P=tag)\$",
    re.I | re.S,
)
_EXCEPTION_SECTION = re.compile(r"\bEXCEPTION\b(?P<body>.*?)(?=\bEND(?:\s*;|\s*\Z))", re.I | re.S)
_EXCEPTION_HANDLER = re.compile(
    r"\bWHEN\b.*?\bTHEN\b(?P<body>.*?)(?=\bWHEN\b|\Z)",
    re.I | re.S,
)
_FAIL_HARD_HANDLER = re.compile(
    r"\s*RAISE(?:\s*;|\s+(?!(?:DEBUG|LOG|INFO|NOTICE|WARNING)\b).*?;)\s*",
    re.I | re.S,
)


@dataclass(frozen=True)
class SoftFailingMigration:
    filename: str
    revision: str
    mechanisms: tuple[str, ...]


def _module_functions(tree: ast.Module) -> dict[str, ast.FunctionDef]:
    return {node.name: node for node in tree.body if isinstance(node, ast.FunctionDef)}


def _reachable_functions(
    root: ast.FunctionDef, functions: dict[str, ast.FunctionDef]
) -> list[ast.FunctionDef]:
    """Return *root* and every module-level helper reachable from it."""
    reachable: list[ast.FunctionDef] = []
    pending = [root]
    seen: set[str] = set()
    while pending:
        function = pending.pop()
        if function.name in seen:
            continue
        seen.add(function.name)
        reachable.append(function)
        called = {
            node.func.id
            for node in ast.walk(function)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
        }
        pending.extend(functions[name] for name in sorted(called) if name in functions)
    return reachable


def _module_strings(tree: ast.Module) -> dict[str, str]:
    strings: dict[str, str] = {}
    for node in tree.body:
        if not isinstance(node, (ast.Assign, ast.AnnAssign)):
            continue
        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        if isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
            for target in targets:
                if isinstance(target, ast.Name):
                    strings[target.id] = node.value.value
    return strings


def _sql_text(expression: ast.AST, module_strings: dict[str, str]) -> str:
    """Resolve the static SQL shapes migrations pass to ``op.execute``."""
    if isinstance(expression, ast.Constant) and isinstance(expression.value, str):
        return expression.value
    if isinstance(expression, ast.Name):
        return module_strings.get(expression.id, "")
    if (
        isinstance(expression, ast.Call)
        and isinstance(expression.func, ast.Attribute)
        and expression.func.attr == "format"
    ):
        return _sql_text(expression.func.value, module_strings)
    if isinstance(expression, ast.BinOp) and isinstance(expression.op, ast.Add):
        return _sql_text(expression.left, module_strings) + _sql_text(expression.right, module_strings)
    if isinstance(expression, ast.JoinedStr):
        return "".join(
            value.value
            for value in expression.values
            if isinstance(value, ast.Constant) and isinstance(value.value, str)
        )
    return ""


def _is_op_execute(node: ast.AST) -> bool:
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "execute"
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "op"
    )


def _block_always_raises(statements: list[ast.stmt]) -> bool:
    """Whether every path through a handler re-raises its exception."""
    if any(
        isinstance(node, (ast.Return, ast.Break, ast.Continue))
        for statement in statements
        for node in ast.walk(statement)
    ):
        return False
    for statement in statements:
        if isinstance(statement, ast.Raise):
            return True
        if (
            isinstance(statement, ast.If)
            and statement.orelse
            and _block_always_raises(statement.body)
            and _block_always_raises(statement.orelse)
        ):
            return True
    return False


def _reaches_op_execute(statements: list[ast.stmt], functions: dict[str, ast.FunctionDef]) -> bool:
    pending = {
        node.func.id
        for statement in statements
        for node in ast.walk(statement)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in functions
    }
    if any(_is_op_execute(node) for statement in statements for node in ast.walk(statement)):
        return True
    seen: set[str] = set()
    while pending:
        name = pending.pop()
        if name in seen:
            continue
        seen.add(name)
        helper = functions[name]
        if any(_is_op_execute(node) for node in ast.walk(helper)):
            return True
        pending.update(
            node.func.id
            for node in ast.walk(helper)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id in functions
            and node.func.id not in seen
        )
    return False


def _python_swallow(function: ast.FunctionDef, functions: dict[str, ast.FunctionDef]) -> bool:
    for node in ast.walk(function):
        if not isinstance(node, ast.Try) or not node.handlers:
            continue
        if not _reaches_op_execute(node.body, functions):
            continue
        if any(not _block_always_raises(handler.body) for handler in node.handlers):
            return True
    return False


def _sql_swallow(executed_sql: str) -> bool:
    for block in _DO_BLOCK.finditer(executed_sql):
        for section in _EXCEPTION_SECTION.finditer(block.group("body")):
            handlers = [match.group("body") for match in _EXCEPTION_HANDLER.finditer(section.group("body"))]
            if any(_FAIL_HARD_HANDLER.fullmatch(handler) is None for handler in handlers):
                return True
    return False


def _revision(tree: ast.Module, filename: str) -> str:
    for node in tree.body:
        if not isinstance(node, (ast.Assign, ast.AnnAssign)):
            continue
        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        if not any(isinstance(target, ast.Name) and target.id == "revision" for target in targets):
            continue
        if isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
            return node.value.value
    raise AssertionError(f"{filename} has no static revision")


def _soft_failure_mechanisms(tree: ast.Module) -> tuple[str, ...]:
    functions = _module_functions(tree)
    upgrade = functions.get("upgrade")
    if upgrade is None:
        return ()
    reachable = _reachable_functions(upgrade, functions)
    module_strings = _module_strings(tree)
    executed_sql = "\n".join(
        _sql_text(node.args[0], module_strings)
        for function in reachable
        for node in ast.walk(function)
        if _is_op_execute(node) and node.args
    )
    mechanisms: list[str] = []
    if _sql_swallow(executed_sql):
        mechanisms.append("DO block EXCEPTION WHEN")
    if any(_python_swallow(function, functions) for function in reachable):
        mechanisms.append("Python try/except around op.execute")
    return tuple(mechanisms)


def _scan_migrations() -> tuple[list[str], list[SoftFailingMigration]]:
    scanned: list[str] = []
    soft_failing: list[SoftFailingMigration] = []
    for path in sorted(_VERSIONS.glob("[0-9]*.py")):
        source = path.read_text()
        tree = ast.parse(source)
        if "upgrade" not in _module_functions(tree):
            continue
        scanned.append(path.name)
        mechanisms = _soft_failure_mechanisms(tree)
        if mechanisms:
            soft_failing.append(
                SoftFailingMigration(
                    filename=path.name,
                    revision=_revision(tree, path.name),
                    mechanisms=mechanisms,
                )
            )
    return scanned, soft_failing


_SCANNED, _SOFT_FAILING = _scan_migrations()


def test_the_scan_detects_a_do_block_exception_handler() -> None:
    tree = ast.parse(
        '''
_DDL = """DO $$ BEGIN ALTER TABLE example ADD COLUMN guarded int;
EXCEPTION WHEN insufficient_privilege THEN RAISE NOTICE 'skipped'; END $$;"""

def upgrade():
    op.execute(_DDL.format(column="guarded"))
'''
    )
    assert _soft_failure_mechanisms(tree) == ("DO block EXCEPTION WHEN",)


def test_the_scan_does_not_flag_a_do_block_handler_that_reraises() -> None:
    tree = ast.parse(
        '''
_DDL = """DO $$ BEGIN ALTER TABLE example ADD COLUMN guarded int;
EXCEPTION WHEN insufficient_privilege THEN RAISE; END $$;"""

def upgrade():
    op.execute(_DDL)
'''
    )
    assert _soft_failure_mechanisms(tree) == ()


@pytest.mark.parametrize(
    "raise_statement",
    (
        "RAISE EXCEPTION 'migration failed';",
        "RAISE SQLSTATE 'P0001';",
        "RAISE 'migration failed';",
    ),
)
def test_the_scan_does_not_flag_a_do_block_handler_that_raises_an_exception(
    raise_statement: str,
) -> None:
    tree = ast.parse(
        f'''
_DDL = """DO $$ BEGIN ALTER TABLE example ADD COLUMN guarded int;
EXCEPTION WHEN insufficient_privilege THEN {raise_statement} END $$;"""

def upgrade():
    op.execute(_DDL)
'''
    )
    assert _soft_failure_mechanisms(tree) == ()


def test_the_scan_detects_python_exception_swallowing_around_op_execute() -> None:
    tree = ast.parse(
        """
def upgrade():
    try:
        op.execute("ALTER TABLE example ADD COLUMN guarded int")
    except PermissionError:
        log_warning()
"""
    )
    assert _soft_failure_mechanisms(tree) == ("Python try/except around op.execute",)


def test_the_scan_treats_return_from_an_exception_handler_as_swallowing() -> None:
    tree = ast.parse(
        """
def upgrade():
    try:
        op.execute("ALTER TABLE example ADD COLUMN guarded int")
    except PermissionError:
        return
"""
    )
    assert _soft_failure_mechanisms(tree) == ("Python try/except around op.execute",)


def test_the_scan_treats_a_conditional_return_before_raise_as_swallowing() -> None:
    tree = ast.parse(
        """
def upgrade():
    try:
        op.execute("ALTER TABLE example ADD COLUMN guarded int")
    except PermissionError:
        if can_tolerate_failure():
            return
        raise
"""
    )
    assert _soft_failure_mechanisms(tree) == ("Python try/except around op.execute",)


def test_the_scan_follows_helpers_called_inside_a_swallowing_try() -> None:
    tree = ast.parse(
        """
def apply_ddl():
    op.execute("ALTER TABLE example ADD COLUMN guarded int")

def upgrade():
    try:
        apply_ddl()
    except PermissionError:
        log_warning()
"""
    )
    assert _soft_failure_mechanisms(tree) == ("Python try/except around op.execute",)


def test_the_scan_does_not_flag_bare_if_not_exists() -> None:
    tree = ast.parse(
        """
def upgrade():
    op.execute("CREATE INDEX IF NOT EXISTS example_idx ON example (id)")
"""
    )
    assert _soft_failure_mechanisms(tree) == ()


def test_the_scan_does_not_flag_an_exception_handler_that_always_reraises() -> None:
    tree = ast.parse(
        """
def upgrade():
    try:
        op.execute("ALTER TABLE example ADD COLUMN guarded int")
    except PermissionError:
        raise
"""
    )
    assert _soft_failure_mechanisms(tree) == ()


def test_the_scan_sees_the_migrations_and_known_soft_failure() -> None:
    assert len(_SCANNED) >= 45, f"only {len(_SCANNED)} migrations found — the scan is broken"
    assert any(item.revision == "044" for item in _SOFT_FAILING), (
        "migration 044's DO block was not detected — the soft-failure scan is broken"
    )


@pytest.mark.parametrize("migration", _SOFT_FAILING, ids=lambda item: item.filename)
def test_every_soft_failing_migration_has_a_postcondition(migration: SoftFailingMigration) -> None:
    guarded_revisions = {postcondition.revision for postcondition in MIGRATION_POSTCONDITIONS}
    assert migration.revision in guarded_revisions, (
        f"{migration.filename} can swallow DDL failure via {', '.join(migration.mechanisms)} but revision "
        f"{migration.revision} has no MIGRATION_POSTCONDITIONS entry. Add a predicate that returns true "
        "only when the migration's effect is present."
    )


def test_migration_044_postcondition_preserves_the_repair_contract() -> None:
    condition = next(item for item in MIGRATION_POSTCONDITIONS if item.revision == "044")
    assert condition.name == "cosine_distance_procost"
    assert condition.severity == "warning"
    assert "to_regprocedure('cosine_distance(vector, vector)')" in condition.predicate
    assert "procost <> 1" in condition.predicate
    assert "extension owner" in condition.message
    assert "ALTER FUNCTION cosine_distance(vector, vector) COST 100;" in condition.message
    assert "re-running the migration will never fix" in condition.message


def test_migration_044_postcondition_does_not_assert_live_harm() -> None:
    """044's message is read on every boot of deployments that can never apply it.

    It once asserted the planner was "under-pricing <=> by ~100x", which reads as an
    incident in exactly the deployments where the app user cannot own the pgvector
    extension and the migration therefore cannot apply. Measured 2026-09-22 against a
    2.1M-row production table still at procost 1, the filtered ANN arm was already
    served by the HNSW index, so the mispricing was latent there. The message must
    calibrate the claim and say how a reader checks it against their own deployment.
    """
    condition = next(item for item in MIGRATION_POSTCONDITIONS if item.revision == "044")
    assert "under-pricing" not in condition.message
    assert "latent rather than a live defect" in condition.message
    assert "pg_stat_user_indexes" in condition.message
