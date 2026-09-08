"""``AgentIdentity`` is only worth having if a raw string cannot become one.

The type marks a caller identity that has been through authentication or
resolution. What makes that more than documentation is the pair of invariants
below, and they cover each other:

* **Construction is confined to the resolvers.** Without this, every call site
  could write ``AgentIdentity(whatever)`` and the type would be a cast ritual
  that reads as a guarantee.

* **The sinks still ask for it.** An allow-list alone passes trivially if
  someone widens a sink's parameter back to ``str`` — nothing would be
  constructed, nothing would be violated, and the guarantee would be gone with
  every test still green. (Same mutual-coverage shape as the two error-code
  invariants in ``tests/test_tool_error_codes_inventory.py``: a narrowing scan
  satisfies one invariant silently and is caught only by the other.)

The property being protected is concrete. ``memory_access_allowed_for_agent``
decides ``scope_agent`` access with ``owner_agent_id == caller_agent_id``, so a
stored row's own ``agent_id`` passed as the CALLER makes that comparison
trivially true and self-authorizes every access. Before this type both
parameters were ``str | None`` and the swap type-checked; the third test below
pins the asymmetry that keeps them distinguishable.
"""

from __future__ import annotations

import ast
import pathlib

import core_api

SRC = pathlib.Path(core_api.__file__).parent

# Every place a raw string may legitimately become an identity: the gateway /
# header boundary, and the resolvers that apply a precedence rule. Adding an
# entry here is a deliberate act — it is a new place where an unvalidated
# string is asserted to be an authenticated identity, which is exactly the
# decision that should not be made silently in review.
ALLOWED_CONSTRUCTORS: frozenset[tuple[str, str]] = frozenset(
    {
        # Write-side precedence (verified id wins unless reserved).
        ("agent_ids.py", "effective_write_agent_id"),
        # Read-side precedence (verified id wins, asserted id falls back).
        ("agent_ids.py", "effective_read_agent_id"),
        # REST: the authenticated identity, or a caller's assertion when the
        # credential authenticates none.
        ("auth.py", "AuthContext.effective_agent_id"),
        # REST: the gateway-verified X-Agent-ID header boundary. ``core_api.auth``
        # is still mypy-exempt, so this construction is what makes the boundary
        # explicit rather than an unchecked bare string.
        ("auth.py", "get_auth_context"),
        # MCP: the gateway-verified X-Agent-ID header boundary — the MCP plane's
        # twin of AuthContext.agent_id.
        ("mcp_server.py", "MCPAuthMiddleware.__call__"),
        # REST read identity (auth > body.caller_agent_id > body.filter_agent_id).
        ("routes/memories.py", "_resolve_read_identity"),
        # Broker ownership boundary: degrades a foreign id to this install's own.
        ("services/agent_service.py", "broker_owned_agent_id"),
        # Write attribution, after the ownership boundary.
        ("services/agent_service.py", "resolve_write_agent"),
        # Evolve/insights caller resolution + trust gate.
        ("services/caller_identity.py", "resolve_caller_and_gate"),
    }
)

# The authorization sinks, and the parameter that must be an identity rather
# than any string. ``owner_agent_id`` is deliberately absent — see
# ``test_owner_agent_id_is_not_an_identity``.
IDENTITY_SINKS: dict[str, str] = {
    "enforce_fleet_read": "agent_id",
    "enforce_fleet_read_many": "agent_id",
    "enforce_delete": "agent_id",
    "enforce_memory_read": "caller_agent_id",
    "authorize_memory_access": "caller_agent_id",
    "memory_access_allowed_for_agent": "caller_agent_id",
}


def _qualname_stack(tree: ast.AST) -> list[tuple[str, ast.Call]]:
    """Every ``AgentIdentity(...)`` call with its dotted enclosing scope."""
    found: list[tuple[str, ast.Call]] = []
    stack: list[str] = []

    class Visitor(ast.NodeVisitor):
        def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
            stack.append(node.name)
            self.generic_visit(node)
            stack.pop()

        visit_AsyncFunctionDef = visit_FunctionDef  # type: ignore[assignment]

        def visit_ClassDef(self, node: ast.ClassDef) -> None:
            stack.append(node.name)
            self.generic_visit(node)
            stack.pop()

        def visit_Call(self, node: ast.Call) -> None:
            func = node.func
            if isinstance(func, ast.Name) and func.id == "AgentIdentity":
                found.append((".".join(stack) or "<module>", node))
            self.generic_visit(node)

    Visitor().visit(tree)
    return found


def _sources() -> list[tuple[str, ast.Module]]:
    out = []
    for path in sorted(SRC.rglob("*.py")):
        try:
            out.append((str(path.relative_to(SRC)), ast.parse(path.read_text())))
        except SyntaxError:  # pragma: no cover - a syntax error fails elsewhere
            continue
    return out


def test_agent_identity_is_constructed_only_by_the_resolvers() -> None:
    actual = {
        (rel, scope) for rel, tree in _sources() for scope, _ in _qualname_stack(tree)
    }
    unexpected = actual - ALLOWED_CONSTRUCTORS
    assert not unexpected, (
        "AgentIdentity is constructed outside the identity resolvers:\n  "
        + "\n  ".join(f"{rel}::{scope}" for rel, scope in sorted(unexpected))
        + "\n\nA raw string became an authenticated identity here. Either route the "
        "value through an existing resolver, or — if this really is a new identity "
        "boundary — add it to ALLOWED_CONSTRUCTORS with a comment saying why."
    )


def test_the_authorization_sinks_still_require_an_identity() -> None:
    """Without this, widening a sink back to ``str`` would pass silently."""
    seen: dict[str, str] = {}
    for _rel, tree in _sources():
        for node in ast.walk(tree):
            if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
                continue
            if node.name not in IDENTITY_SINKS:
                continue
            want = IDENTITY_SINKS[node.name]
            args = node.args.posonlyargs + node.args.args + node.args.kwonlyargs
            for arg in args:
                if arg.arg == want:
                    seen[node.name] = (
                        ast.unparse(arg.annotation) if arg.annotation else "<none>"
                    )

    missing = set(IDENTITY_SINKS) - set(seen)
    assert not missing, f"sink(s) not found — renamed or removed? {sorted(missing)}"

    widened = {name: anno for name, anno in seen.items() if "AgentIdentity" not in anno}
    assert not widened, (
        "authorization sink(s) no longer require an AgentIdentity:\n  "
        + "\n  ".join(
            f"{n}({IDENTITY_SINKS[n]}: {a})" for n, a in sorted(widened.items())
        )
        + "\n\nWidening these to ``str`` removes the guarantee without failing "
        "anything else: nothing would be constructed, so the allow-list test "
        "above would still pass."
    )


def test_owner_agent_id_is_not_an_identity() -> None:
    """The asymmetry IS the security property, so it is pinned explicitly.

    ``owner_agent_id`` is read off a stored row: it is data. Giving it the same
    type as the caller would make the two interchangeable again — and
    ``scope_agent`` access is decided by comparing them.
    """
    checked = 0
    for _rel, tree in _sources():
        for node in ast.walk(tree):
            if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
                continue
            args = node.args.posonlyargs + node.args.args + node.args.kwonlyargs
            for arg in args:
                if arg.arg != "owner_agent_id":
                    continue
                anno = ast.unparse(arg.annotation) if arg.annotation else "<none>"
                checked += 1
                assert "AgentIdentity" not in anno, (
                    f"{node.name}(owner_agent_id: {anno}) — a stored row's owner is "
                    "data, not an authenticated identity. Typing it the same as "
                    "caller_agent_id makes them interchangeable, and scope_agent "
                    "access is decided by comparing the two."
                )
    assert checked >= 2, (
        f"expected to check at least the two authorize/predicate parameters, saw {checked}"
    )
