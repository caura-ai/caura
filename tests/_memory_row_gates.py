"""Names the two authz inventories share for the memory row-gate axis.

``tests/test_authz_gate_inventory.py`` (REST) and
``tests/test_mcp_authz_gate_inventory.py`` (MCP) each check that a mutation of
memory rows reaches a gate that decides whether THIS caller may change THIS
row. They read different handlers the same way, so the vocabulary lives here
once and the two files cannot drift apart on it.
"""

from __future__ import annotations

import ast
import functools
import importlib
import inspect
import textwrap
from collections.abc import Iterable

# The gates that decide row authority for an agent credential. Each is an
# ``agent_service`` function: ``enforce_update`` (its own row, a peer's at
# trust 3, never trust 0), ``enforce_delete`` (trust 3), and
# ``enforce_transition`` (a status change: the first, plus the second when the
# new status takes the row out of the live set).
ROW_GATES = frozenset({"enforce_update", "enforce_delete", "enforce_transition"})

# Imported service functions that apply a row gate themselves, so a handler
# calling one is gated one layer down. The inventories do not follow imported
# calls, so each name here is a claim, and
# ``test_authz_gate_inventory.test_row_gate_carriers_apply_a_row_gate`` checks
# it against the function's own body.
#
# A carrier is credited only through ``carriers_called``: a bare-name call
# that resolves to this very function. Its name is also a storage method
# (``sc.update_memory``), which writes the row with no gate at all, so a match
# on the name alone would let the sink that brings a handler into scope also
# satisfy the check.
ROW_GATE_CARRIERS: dict[str, str] = {
    "update_memory": "core_api.services.memory_service",
}

# Calls that change or retire memory rows. A handler reaching one directly, or
# through a helper in its own module, is in scope whatever its parameters are
# called, so a new route or op that retires rows cannot leave the axis by
# naming its ids differently. The service and storage spellings share names,
# and both count.
MEMORY_ROW_SINKS = frozenset(
    {
        "update_memory",
        "update_memory_status",
        "batch_update_status",
        "soft_delete_memory",
        "soft_delete_by_ids",
        "soft_delete_by_filter",
        "soft_delete_by_run",
        "redistribute_memories",
    }
)

# Request parameters naming the memory rows a call acts on. A mutating route or
# op taking one is in scope even if the scan cannot see the sink it reaches.
MEMORY_ID_PARAMS = frozenset({"memory_id", "memory_ids"})


def _is_carrier(name: str, target: object) -> bool:
    module = ROW_GATE_CARRIERS.get(name)
    return module is not None and target is getattr(
        importlib.import_module(module), name, None
    )


@functools.cache
def _tree(fn) -> ast.AST | None:
    try:
        return ast.parse(textwrap.dedent(inspect.getsource(fn)))
    except (OSError, TypeError, SyntaxError):
        return None


def carriers_called(nodes: Iterable[ast.AST], fn, _depth: int = 0) -> frozenset[str]:
    """The ``ROW_GATE_CARRIERS`` that ``nodes``, code of ``fn``, call.

    Only a bare-name call counts, and only when the name resolves in ``fn``'s
    module to the carrier itself, so ``sc.update_memory(...)`` never does.
    Module-local helpers are followed two levels down, as both inventories
    follow them for everything else.
    """
    module_globals = getattr(inspect.getmodule(fn), "__dict__", {})
    found: set[str] = set()
    for root in nodes:
        for node in ast.walk(root):
            if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)):
                continue
            name = node.func.id
            target = module_globals.get(name)
            if _is_carrier(name, target):
                found.add(name)
            elif (
                _depth < 2
                and target is not fn
                and inspect.isfunction(target)
                and getattr(target, "__module__", None) == fn.__module__
                and (tree := _tree(target)) is not None
            ):
                found |= carriers_called((tree,), target, _depth + 1)
    return frozenset(found)
