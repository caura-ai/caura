"""Guards on the two fixtures that decide whether a test is really isolated.

``db`` (see ``conftest.db``) is a per-test session inside a transaction that is
rolled back at teardown. Requesting it reads as "this test is transactionally
isolated" — but almost everything here writes through the service layer, which
commits on its own connections, so that rollback never touches those rows. A
test that takes ``db`` and never uses it therefore advertises isolation it does
not have, and pays a connection + transaction + rollback for nothing.

107 signatures had drifted into that state before this guard existed. Two
assertions keep it from coming back, and together they also close the
indirect case:

  1. no HELPER may take ``db`` without using it, and
  2. no TEST may request ``db`` without using it.

(1) is what makes (2) sufficient. Without it, ``helper(db, ...)`` counts as a
use of the name while the helper quietly drops the parameter — which is exactly
how 34 of those 107 stayed hidden from the first sweep.
"""

from __future__ import annotations

import ast
import pathlib

from tests import conftest

FIXTURE = "db"
TESTS_ROOT = pathlib.Path(__file__).parent

# Tests that genuinely need the fixture's SIDE EFFECT (an open transaction) but
# never name it. Empty today; add an entry with a reason rather than deleting
# the assertion, so the exception is visible in review.
SIDE_EFFECT_ONLY: frozenset[str] = frozenset()


def _params(fn: ast.FunctionDef | ast.AsyncFunctionDef) -> list[str]:
    a = fn.args
    return [p.arg for p in (a.posonlyargs + a.args + a.kwonlyargs)]


def _names_loaded(fn: ast.FunctionDef | ast.AsyncFunctionDef, name: str) -> bool:
    """Is ``name`` read anywhere in the body — including nested scopes?"""
    return any(
        isinstance(n, ast.Name) and n.id == name and isinstance(n.ctx, ast.Load)
        for n in ast.walk(fn)
    )


def _functions() -> list[tuple[pathlib.Path, ast.FunctionDef | ast.AsyncFunctionDef]]:
    found = []
    for path in sorted(TESTS_ROOT.rglob("*.py")):
        try:
            tree = ast.parse(path.read_text(), filename=str(path))
        except SyntaxError:  # pragma: no cover - a broken file fails its own tests
            continue
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                found.append((path, node))
    return found


def _offenders(*, tests: bool) -> list[str]:
    out = []
    for path, fn in _functions():
        is_test = fn.name.startswith("test_")
        if is_test is not tests:
            continue
        if FIXTURE not in _params(fn) or _names_loaded(fn, FIXTURE):
            continue
        if is_test and fn.name in SIDE_EFFECT_ONLY:
            continue
        out.append(f"{path.relative_to(TESTS_ROOT)}:{fn.lineno} {fn.name}")
    return out


def test_no_helper_takes_the_db_fixture_without_using_it() -> None:
    """A helper with a dead ``db`` param forces its callers to request one too.

    This is the assertion that makes the test-level one below meaningful: it is
    why passing ``db`` to a helper can be trusted as a real use.
    """
    offenders = _offenders(tests=False)
    assert not offenders, (
        f"{len(offenders)} helper(s) take the `{FIXTURE}` fixture and never use it. "
        f"Drop the parameter and the argument at each call site — otherwise every "
        f"caller must keep requesting a session that goes nowhere:\n  "
        + "\n  ".join(offenders)
    )


def test_no_test_requests_the_db_fixture_without_using_it() -> None:
    offenders = _offenders(tests=True)
    assert not offenders, (
        f"{len(offenders)} test(s) request the `{FIXTURE}` fixture and never use it. "
        f"Remove it from the signature: it implies transactional isolation these "
        f"tests do not have, since service-layer writes commit on their own "
        f"connections. If one truly needs the open transaction as a side effect, "
        f"add it to SIDE_EFFECT_ONLY with a reason:\n  " + "\n  ".join(offenders)
    )


# ---------------------------------------------------------------------------
# The other half of the same problem: rows that survive because the TENANT is
# shared. The ``db``-fixture guards above stop a test from *claiming* isolation
# it lacks; these stop the tenant id from silently handing one test's committed
# rows to the next.
# ---------------------------------------------------------------------------


# The fixture's underlying function — the guards assert on what one CALL returns, so
# they call it directly rather than comparing ids across two test invocations. That
# alternative needs module-level state, which would be per-worker if this suite ever
# runs parallel, letting both cases pass vacuously.
_mint_tenant_id = conftest.tenant_id.__wrapped__


def test_tenant_id_fixture_mints_a_fresh_id_per_call() -> None:
    first, second = _mint_tenant_id(), _mint_tenant_id()
    assert first != second, (
        f"the `tenant_id` fixture returned {first!r} twice, so every test sharing "
        "it also shares committed rows: service-layer writes commit on their own "
        "connections and nothing removes them until the session-end sweep, which "
        "lets one test satisfy another's `len(results) >= 1`. Mint a fresh id "
        "per call."
    )


def test_tenant_id_fixture_keeps_the_swept_prefix() -> None:
    minted = _mint_tenant_id()
    assert minted.startswith("test-tenant-"), (
        f"tenant_id {minted!r} would not be matched by the session-end sweep in "
        "conftest._setup_schema, so its rows would outlive the run"
    )
