"""09/02 M-07 — scripts/forge_dry_run.py crashed on every real invocation.

It did `from core_api.db.session import async_session`. #491 deleted that
module when it routed all core-api DB access through core-storage-api, and
`core-api/src/core_api/db/` has held nothing but `__pycache__` since.

The import is LAZY (inside `_run`, for `--help` ergonomics), which is why this
survived: the file parsed, `--help` worked, ruff passed, and only an operator
actually running a Forge dry-run ever saw the ImportError.

Storage had already grown the two forge-shaped endpoints the script needs
(`forge_memory_content_by_ids`, `forge_is_fingerprint_poisoned`) in that same
migration — so this was a move that was simply never made, not a redesign.
"""

import ast
import inspect
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "forge_dry_run.py"


def _source() -> str:
    return _SCRIPT.read_text()


def _code_only() -> str:
    """Source with comments and docstrings stripped.

    The fix is *described* in comments that name the very symbols it removes,
    so a plain substring search would pass on the prose alone. These assertions
    must read the code.
    """
    tree = ast.parse(_source())
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Expr)
            and isinstance(node.value, ast.Constant)
            and isinstance(node.value.value, str)
        ):
            node.value.value = ""
    return ast.unparse(tree)


def test_the_deleted_module_is_no_longer_imported():
    """The actual crash."""
    assert "core_api.db.session" not in _code_only()


def test_no_session_is_opened_and_nothing_is_committed():
    """`async_session()` and `db.commit()` both belonged to a pool core-api no
    longer has; each storage call commits server-side."""
    code = _code_only()
    assert "async_session" not in code
    assert "db.commit" not in code


def test_core_api_really_has_no_session_factory_left():
    """Pins WHY the script had to move rather than re-import from elsewhere.
    If core-api ever regains a pool this fails and the approach is revisited."""
    import core_api

    db_pkg = Path(core_api.__file__).parent / "db"
    leftovers = (
        [p.name for p in db_pkg.iterdir() if p.name != "__pycache__"]
        if db_pkg.exists()
        else []
    )
    assert not leftovers, f"core_api/db has real modules again: {leftovers}"


def test_the_fetcher_and_poison_checker_go_through_storage():
    code = _code_only()
    assert "forge_memory_content_by_ids" in code
    assert "forge_is_fingerprint_poisoned" in code


def test_those_storage_methods_exist_with_the_expected_keywords():
    """Guards against the script calling an endpoint that drifted. Both are
    keyword-only, so a renamed parameter would fail at runtime, not import."""
    from core_api.clients.storage_client import CoreStorageClient

    fetch = inspect.signature(CoreStorageClient.forge_memory_content_by_ids).parameters
    assert {"tenant_id", "memory_ids"} <= set(fetch)

    poison = inspect.signature(
        CoreStorageClient.forge_is_fingerprint_poisoned
    ).parameters
    assert {"tenant_id", "fleet_id", "cluster_fingerprint"} <= set(poison)


def test_helpers_that_never_used_the_session_no_longer_take_one():
    """`_wire_candidate_writer` and `_wire_status_checker` accepted `db` and
    ignored it — a signature implying a dependency the body did not have."""
    tree = ast.parse(_source())
    fns = {
        n.name: n
        for n in ast.walk(tree)
        if isinstance(n, ast.AsyncFunctionDef | ast.FunctionDef)
    }
    for name in ("_wire_candidate_writer", "_wire_status_checker"):
        args = [a.arg for a in fns[name].args.args]
        assert "db" not in args, f"{name} still takes an unused db: {args}"


def test_the_script_still_parses_and_exposes_its_entry_point():
    tree = ast.parse(_source())
    names = {
        n.name
        for n in ast.walk(tree)
        if isinstance(n, ast.AsyncFunctionDef | ast.FunctionDef)
    }
    assert "_run" in names
