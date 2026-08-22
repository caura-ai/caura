"""The tenant prefix a test mints and the prefix the sweep deletes must agree.

Nothing removes rows written through the service layer mid-run — most tests here
write via a storage client that commits on its own connection, so the per-test
session rollback never sees them. The only thing that ever reclaims them is the
end-of-run ``DELETE ... WHERE tenant_id LIKE 'test-tenant-%'`` in
``_setup_schema``.

That made the prefix load-bearing while leaving it a convention in a docstring,
and #858 is what it cost: two interview files minted ``t-`` tenants, so every job
document they ever wrote survived every run. A local database reached 300 of them
across 228 tenants, and because the endpoint under test sweeps *across* tenants,
it began reading other runs' residue — failing in a way that looks nothing like
accumulated state, which is the same trap the conftest comment already describes
for keystones.

These tests pin the coupling itself rather than any one call site.
"""

from __future__ import annotations

import ast
import inspect
import re

import pytest

from tests import conftest

pytestmark = [pytest.mark.unit]


def test_the_minted_prefix_is_the_one_the_sweep_deletes() -> None:
    assert conftest.new_tenant_id().startswith(conftest.SWEEP_TENANT_PREFIX)


def test_the_tenant_id_fixture_mints_a_sweepable_id() -> None:
    """The fixture and the plain function are two doors to the same decision;
    a test taking the fixture must not get a tenant the sweep cannot see."""
    fn = conftest.tenant_id.__wrapped__ if hasattr(conftest.tenant_id, "__wrapped__") else None
    assert fn is not None, "tenant_id is expected to be a pytest fixture wrapping a function"
    assert fn().startswith(conftest.SWEEP_TENANT_PREFIX)


def test_the_sweep_has_no_hardcoded_prefix_left() -> None:
    """The constant only helps while both sides actually use it.

    A literal creeping back into either the DELETE or the minter re-opens exactly
    the gap #858 came through, and it would read as harmless in review.

    Parsed rather than grepped, so that only *code* is checked: docstrings and
    comments here explain the prefix and quote it, which is the opposite of a
    problem, and a plain text search would fail on the documentation this change
    adds.
    """
    tree = ast.parse(inspect.getsource(conftest))

    docstrings = {
        id(node.body[0].value)
        for node in ast.walk(tree)
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
        and node.body
        and isinstance(node.body[0], ast.Expr)
        and isinstance(node.body[0].value, ast.Constant)
        and isinstance(node.body[0].value.value, str)
    }
    literals = [
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and id(node) not in docstrings
        and conftest.SWEEP_TENANT_PREFIX in node.value
    ]

    assert literals == [conftest.SWEEP_TENANT_PREFIX], (
        "the prefix should appear in code exactly once, as the SWEEP_TENANT_PREFIX "
        f"definition; found {literals}"
    )


def test_every_swept_statement_binds_the_prefix() -> None:
    """Each ``DELETE`` in the sweep must be parameterised on the constant.

    Counted rather than inspected one by one: a table added to the sweep list
    with its own inline pattern is the realistic regression, and it would leave
    that table leaking silently while the others stayed clean.
    """
    source = inspect.getsource(conftest)
    deletes = re.findall(r"DELETE FROM", source)
    bindings = re.findall(r"LIKE :prefix", source)

    assert len(deletes) == len(bindings), (
        f"{len(deletes)} DELETE statement(s) but {len(bindings)} bound to the prefix — "
        "one of them is not using SWEEP_TENANT_PREFIX"
    )
