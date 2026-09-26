"""Declarations that describe a contract the code does not have.

Two removals share this shape and neither could fail a test, because a
declaration nothing reads has no behaviour to get wrong:

* ``Settings.host`` / ``Settings.port`` in core-storage-api and core-worker.
  Both services take their address from the Dockerfile CMD — ``common.serve
  --host --port`` for storage, a literal ``uvicorn --port`` for the worker —
  and neither ever read the field. Cloud Run sets ``PORT`` in the environment,
  so the worker's field silently absorbed it while uvicorn bound something
  else: a value that looked authoritative and answered no question.

* Seven pydantic request schemas in ``core_storage_api.schemas``. Unreferenced,
  and stale in ways that would have bitten whoever adopted them — see
  ``test_scoping_keys_the_dead_schemas_omitted_are_still_read``.

These assertions are static: they read source, never a database.
"""

from __future__ import annotations

import ast
import pathlib

import pytest

_REPO = pathlib.Path(__file__).resolve().parents[1]

_STORAGE_ROUTERS = _REPO / "core-storage-api/src/core_storage_api/routers"


def _settings_fields(path: pathlib.Path) -> set[str]:
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.ClassDef) and node.name == "Settings":
            return {
                stmt.target.id
                for stmt in node.body
                if isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name)
            }
    raise AssertionError(f"no Settings class found in {path}")


def _handler_body_keys(path: pathlib.Path, route: str) -> set[str]:
    """Keys the handler decorated with *route* reads off its JSON body."""
    tree = ast.parse(path.read_text())
    for fn in ast.walk(tree):
        if not isinstance(fn, (ast.AsyncFunctionDef, ast.FunctionDef)):
            continue
        for dec in fn.decorator_list:
            if not (isinstance(dec, ast.Call) and dec.args):
                continue
            first = dec.args[0]
            if not (isinstance(first, ast.Constant) and first.value == route):
                continue
            keys: set[str] = set()
            for node in ast.walk(fn):
                if (
                    isinstance(node, ast.Subscript)
                    and isinstance(node.value, ast.Name)
                    and node.value.id == "body"
                    and isinstance(node.slice, ast.Constant)
                ):
                    keys.add(node.slice.value)
                if (
                    isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and node.func.attr == "get"
                    and isinstance(node.func.value, ast.Name)
                    and node.func.value.id == "body"
                    and node.args
                    and isinstance(node.args[0], ast.Constant)
                ):
                    keys.add(node.args[0].value)
            return keys
    raise AssertionError(f"no handler for {route!r} in {path}")


def _model_names(path: pathlib.Path) -> set[str]:
    """Classes in *path* that inherit from ``BaseModel``."""
    return {
        node.name
        for node in ast.walk(ast.parse(path.read_text()))
        if isinstance(node, ast.ClassDef)
        and any(isinstance(b, ast.Name) and b.id == "BaseModel" for b in node.bases)
    }


@pytest.mark.parametrize(
    "config_path",
    [
        "core-storage-api/src/core_storage_api/config.py",
        "core-worker/src/core_worker/config.py",
    ],
)
def test_no_service_declares_an_address_it_does_not_bind(config_path: str) -> None:
    fields = _settings_fields(_REPO / config_path)
    assert len(fields) > 5, (
        f"{config_path}: only {len(fields)} fields parsed — the walk missed the class"
    )
    assert not {"host", "port"} & fields, (
        f"{config_path} declares host/port again; the Dockerfile CMD is what binds the address"
    )


def test_the_dockerfiles_are_what_carry_the_port() -> None:
    """The other half of the claim above, so it cannot rot one-sided."""
    storage = (_REPO / "core-storage-api/Dockerfile").read_text()
    assert "--port" in storage and "8002" in storage
    worker = (_REPO / "core-worker/Dockerfile").read_text()
    assert "--port" in worker and "8080" in worker


def test_the_schemas_module_declares_no_unreferenced_request_model() -> None:
    """A request model nothing references is a contract nothing enforces."""
    schemas = _REPO / "core-storage-api/src/core_storage_api/schemas.py"

    # Vacuity: prove the detector detects. A module known to carry referenced
    # BaseModel subclasses must come back non-empty, or an empty result from
    # ``schemas.py`` would mean nothing.
    control = _model_names(
        _REPO / "core-storage-api/src/core_storage_api/routers/tenants.py"
    )
    assert control, "the BaseModel detector found none in a module that has them"

    declared = _model_names(schemas)
    referenced = set()
    for path in (_REPO / "core-storage-api/src").rglob("*.py"):
        if path == schemas:
            continue
        text = path.read_text(errors="ignore")
        referenced |= {name for name in declared if name in text}
    assert declared - referenced == set(), (
        f"unreferenced request models in schemas.py: {sorted(declared - referenced)}"
    )


@pytest.mark.parametrize(
    ("router", "route", "key"),
    [
        # Each key was ABSENT from the schema that named its endpoint, so
        # annotating the handler with that schema would have dropped it —
        # silently, since pydantic ignores unknown keys by default. These are
        # the three where dropping it changes who can read what.
        ("memories.py", "/scored-search", "readable_tenant_ids"),
        ("entities.py", "/fts-search", "strict_fleet_scoping"),
        ("memories.py", "/entity-links", "memory_ids"),
    ],
)
def test_scoping_keys_the_dead_schemas_omitted_are_still_read(
    router: str, route: str, key: str
) -> None:
    keys = _handler_body_keys(_STORAGE_ROUTERS / router, route)
    assert keys, f"{route}: parsed no body keys at all"
    assert key in keys, f"{route} no longer reads {key!r}"
