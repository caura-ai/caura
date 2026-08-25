"""D14 follow-up: every rate-limited route must inject a Response.

With ``headers_enabled=True`` (D14, #976), slowapi injects
X-RateLimit-*/Retry-After into the endpoint's ``Response``. When the handler
signature has no ``Response``-annotated parameter, slowapi raises inside
``_inject_headers`` and the route 500s ON EVERY CALL — not just when
throttled. That shipped: POST /documents, POST /ingest/commit and
POST /recall all 500'd from #976 until this guard's fix, unnoticed because
the bench and wet tests only exercised POST /memories and POST /search
(both of which already carried the parameter).

The guard is AST-based so it catches the mistake at unit-test time, without
booting the app or hitting a limiter.
"""

from __future__ import annotations

import ast
import pathlib

import pytest

pytestmark = pytest.mark.unit

_ROUTES_DIR = (
    pathlib.Path(__file__).resolve().parents[1] / "core-api" / "src" / "core_api"
)
_LIMIT_DECORATORS = {"write_limit", "search_limit", "write_bulk_limit"}


def _decorator_names(fn: ast.AsyncFunctionDef | ast.FunctionDef) -> set[str]:
    names = set()
    for dec in fn.decorator_list:
        node = dec.func if isinstance(dec, ast.Call) else dec
        if isinstance(node, ast.Name):
            names.add(node.id)
        elif isinstance(node, ast.Attribute):
            names.add(node.attr)
    return names


def _has_response_param(fn: ast.AsyncFunctionDef | ast.FunctionDef) -> bool:
    for arg in [*fn.args.posonlyargs, *fn.args.args, *fn.args.kwonlyargs]:
        ann = arg.annotation
        if isinstance(ann, ast.Name) and ann.id == "Response":
            return True
        if isinstance(ann, ast.Attribute) and ann.attr == "Response":
            return True
    return False


def test_every_rate_limited_route_declares_a_response_param():
    offenders = []
    for path in sorted(_ROUTES_DIR.rglob("*.py")):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if not isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef)):
                continue
            if not (_decorator_names(node) & _LIMIT_DECORATORS):
                continue
            if not _has_response_param(node):
                offenders.append(f"{path.name}:{node.lineno} {node.name}")
    assert not offenders, (
        "rate-limited route(s) without a Response-annotated parameter — "
        "slowapi headers_enabled 500s these on every call: " + ", ".join(offenders)
    )


def test_guard_actually_sees_the_limited_routes():
    """The guard above passes vacuously if the decorator names drift —
    pin the number of rate-limited handlers it inspects."""
    seen = 0
    for path in sorted(_ROUTES_DIR.rglob("*.py")):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef)) and (
                _decorator_names(node) & _LIMIT_DECORATORS
            ):
                seen += 1
    assert seen >= 4, f"expected at least 4 rate-limited handlers, saw {seen}"
