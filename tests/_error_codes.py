"""Which error codes an MCP tool handler can actually put on the wire.

Derived from source rather than declared, so ``ToolSpec.error_codes`` can be
compared against something independent of itself. Lives in ``tests/_*.py``
because this scan — not the assertions built on it — is the part that can be
wrong, and that shape of file is the one the review pipeline reads.

THREE PATHS REACH A CALLER, and a scan that follows fewer than all three
under-reports, which is the failure mode that passes:

1. ``_error_response("CODE", …)`` called inside the handler.
2. A dict literal carrying ``"code": "CODE"`` — ``caura_manage`` builds its
   unknown-op envelope that way rather than through the helper.
3. A module-level PRE-BAKED ``CallToolResult``. ``_check_auth`` does not
   format an error; it returns ``_AUTH_ERROR`` / ``_ADMIN_ERROR``, both
   assigned at import time, and ``_check_write_scope`` returns
   ``_READ_ONLY_ERROR``. Every handler calls ``_check_auth``, so these are
   the codes MOST widely emitted and the ones a function-scoped walk misses
   entirely — the first version of this scan missed 19 of 52 that way, and
   reported ``UNAUTHORIZED`` as emitted by no tool at all.

Helper calls are followed transitively within ``mcp_server`` only. Codes
raised deeper in the service layer are out of scope on purpose: those surface
as HTTP failures from the storage client, not as this envelope, and following
them would make the scan a whole-program analysis.
"""

from __future__ import annotations

import ast
import functools
import inspect

# Callables that format an error envelope from a code as their first argument.
_FORMATTERS = frozenset({"_error_response", "make_error_payload"})


def _literal_codes(node: ast.AST) -> set[str]:
    """Codes written as literals anywhere under ``node`` (paths 1 and 2)."""
    found: set[str] = set()
    for sub in ast.walk(node):
        if isinstance(sub, ast.Call):
            name = getattr(sub.func, "id", None) or getattr(sub.func, "attr", None)
            if name in _FORMATTERS and sub.args:
                first = sub.args[0]
                if isinstance(first, ast.Constant) and isinstance(first.value, str):
                    found.add(first.value)
        elif isinstance(sub, ast.Dict):
            # Always parallel: a ``**expr`` unpacking yields a None KEY, not a
            # missing one, so a length mismatch would mean this assumption is wrong.
            for key, value in zip(sub.keys, sub.values, strict=True):
                if (
                    isinstance(key, ast.Constant)
                    and key.value == "code"
                    and isinstance(value, ast.Constant)
                    and isinstance(value.value, str)
                    and value.value.isupper()
                ):
                    found.add(value.value)
    return found


@functools.cache
def _module() -> tuple[dict[str, ast.AST], dict[str, frozenset[str]]]:
    """``(functions by name, pre-baked error constants by name)``."""
    from core_api import mcp_server

    tree = ast.parse(inspect.getsource(mcp_server))
    functions = {
        node.name: node
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    # Path 3. Top-level assignments only: a pre-baked result is built once at
    # import, which is exactly why it is invisible to a per-function walk.
    constants: dict[str, frozenset[str]] = {}
    for stmt in tree.body:
        if not isinstance(stmt, ast.Assign):
            continue
        codes = _literal_codes(stmt.value)
        if not codes:
            continue
        for target in stmt.targets:
            if isinstance(target, ast.Name):
                constants[target.id] = frozenset(codes)
    return functions, constants


def prebaked_error_constants() -> dict[str, frozenset[str]]:
    """The path-3 constants, exposed so a test can pin that they are found."""
    return dict(_module()[1])


def _names(node: ast.AST) -> set[str]:
    return {sub.id for sub in ast.walk(node) if isinstance(sub, ast.Name)}


def _calls(node: ast.AST) -> set[str]:
    out: set[str] = set()
    for sub in ast.walk(node):
        if isinstance(sub, ast.Call):
            name = getattr(sub.func, "id", None) or getattr(sub.func, "attr", None)
            if name:
                out.add(name)
    return out


def emitted_codes(
    function_name: str, _seen: frozenset[str] = frozenset()
) -> frozenset[str]:
    """Every code reachable from ``function_name`` inside ``mcp_server``.

    ``_seen`` guards a cycle the graph does not currently have: measured, the
    only one is ``call_tool`` calling itself, and the ``callee !=
    function_name`` test below already covers that. It stays because mutual
    recursion would make this hang, and a depth limit would truncate quietly
    instead of terminating.
    """
    functions, constants = _module()
    if function_name in _seen or function_name not in functions:
        return frozenset()
    node = functions[function_name]
    codes = set(_literal_codes(node))
    for name in _names(node):
        codes |= constants.get(name, frozenset())
    for callee in _calls(node):
        if callee in functions and callee != function_name:
            codes |= emitted_codes(callee, _seen | {function_name})
    return frozenset(codes)
