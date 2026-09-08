"""The MCP tool surface, enumerated — the inventory ``test_authz_gate_inventory``
says it does not cover.

That file opens by disclaiming this surface: ``app.mount("/mcp", ...)`` is a
Starlette ``Mount`` with no ``.routes``, so the route walk cannot see it and
``app.openapi()`` does not list it. Its closing note — "that surface wants its
own inventory and does not have one" — is what this file answers.

WHAT IS ALREADY COVERED, so this file does not restate it. The write-scope gate
is asserted BEHAVIOURALLY in ``tests/test_mcp_write_scope_gate.py``: twelve
tests drive a write tool or op with a read-only credential and require a
FORBIDDEN envelope, and three more assert the gate does NOT fire on a read.
That is a stronger claim than any static check here can make — it proves the
gate REFUSES, not merely that the call appears — and it exists because the 2026
audit found ``_is_write_allowed()`` defined and never invoked.

What those twelve cannot do is NOTICE A THIRTEENTH TOOL. They are hand-written,
one per tool and op, so a new tool or a new op on an existing tool arrives with
no test and nothing fails. That is the same gap the REST inventory was written
for, and the same argument: manual sweeps have not converged. So this file
enumerates from the REGISTRY and reads the HANDLERS, and everything it checks is
a fact about one or the other.

FOUR INVARIANTS.

    1. The ops a tool DECLARES are the ops its handler ACCEPTS.
    2. Every accepted op that mutates passes through the write-scope gate.
    3. A declared ``trust_required`` above 0 is corroborated by a handler that
       can actually reach a trust gate.
    4. Every caller-supplied agent identity is bound to the verified one.

The fourth is the self plane, the axis #1364/#1365 named and #1367 enumerated
for REST. It reads differently here because the binding is spelled differently:
REST hands handlers an ``AuthContext`` and asks them to call
``enforce_self_agent``, while MCP has no ``AuthContext`` at all — identity
arrives in module-level ``contextvars`` set by ``MCPAuthMiddleware`` and a
handler binds by REASSIGNING its own parameter, ``agent_id = _get_agent_id() or
agent_id``. That rebinding is a stronger fact than the REST precedence idiom
was: it overwrites the name the caller supplied, so no later line in the
handler can read the caller's value under that name. It is therefore checked by
a rule here, where REST needed fourteen allowlist lines — the note above
``SELF_ID_PARAMS`` in the REST file explains why an ``any()`` over a body
could not do the same job there.

Two axes the REST file has are deliberately ABSENT rather than forgotten.
``enforce_not_agent_credential`` has no analogue because ``_check_auth``
refuses admin keys outright — there is no admin plane on this surface for an
agent credential to escalate into. ``is_demo`` has none because the MCP
middleware never reads a demo header; that divergence is already recorded in
prose at ``tests/test_route_authz_gaps.py``, and this file does not restate it.

The third exists because ``trust_required`` is a PUBLISHED CLAIM WITH NO
ENFORCEMENT BEHIND IT. ``required_trust()`` in ``tools/_registry.py`` is
exported and, measured across this repo, called by nothing: the field is
range-checked by ``test_tools_registry.py`` and serialised into
``plugin/tools.json``, which ``test_tools_export_in_sync.py`` holds in lockstep
with the registry and which is the manifest clients read. The gates that
actually refuse are elsewhere and take their thresholds from somewhere else
entirely — ``_require_trust(..., min_level=...)`` is called at six sites in
``mcp_server.py`` and every ``min_level`` comes from ``_resolve_read_fleet_gate``,
a scope literal (``1 if scope == "agent" else 2``) or ``keystone_min_trust``.
So the number a client is told and the number the server enforces are two
independent systems that happen to agree today. Nothing checked that they keep
agreeing; this does.

WHAT THIS IS NOT. It does not decide whether a threshold is the RIGHT one, and
it cannot: that ``caura_doc``'s ``delete`` needs no trust while
``caura_manage``'s needs 3 is a product judgement about two different stores,
not a defect this file can see. It checks that a declared number has a gate
under it and that the surface is enumerated, nothing more.
"""

from __future__ import annotations

import ast
import functools
import inspect
import pathlib
import re
import textwrap
from collections.abc import Callable
from typing import NamedTuple

import core_api.tools as tools

# The gate every mutating tool routes through. A module-local helper in
# ``mcp_server``, so ``_classify`` below reaches it by name.
WRITE_SCOPE_GATE = "_check_write_scope"

# Names that can refuse on trust. Deliberately a set of NAMES rather than a
# check that trust is enforced: whether ``enforce_delete`` really refuses at 3
# is ``test_memory_byid_authz``'s job, and duplicating it here would be the
# gate-attribution this file's REST sibling avoids. The claim made is only
# "a handler declaring a threshold reaches something that can apply one".
TRUST_GATES = frozenset(
    {
        "_require_trust",
        "require_trust",
        "enforce_delete",
        "enforce_update",
        "enforce_fleet_write",
        "enforce_fleet_read",
        "enforce_fleet_read_many",
        "enforce_memory_read",
        "keystone_min_trust",
        "effective_keystone_min_trust",
        "_resolve_read_fleet_gate",
    }
)

# Op names that change state. A judgement, and the membership is the whole
# content of invariant 2, so ``test_every_accepted_op_is_classified`` fails if
# an op appears in neither set — a new op must not join the surface unexamined.
MUTATING_OPS = frozenset(
    {
        "write",
        "set",
        "create",
        "bulk_create",
        "update",
        "transition",
        "delete",
        "bulk_delete",
        "redistribute",
    }
)
READING_OPS = frozenset({"read", "query", "search", "list_collections", "lineage"})

# Tools that declare NO ops, split the same way. ``caura_tune`` is the one worth
# naming: it reads like a settings getter, and it writes.
MUTATING_TOOLS = frozenset(
    {"caura_write", "caura_tune", "caura_evolve", "caura_insights"}
)
READONLY_TOOLS = frozenset(
    {"caura_recall", "caura_list", "caura_stats", "caura_keystones", "caura_entity_get"}
)

# Parameter names carrying a caller's ASSERTION about which agent it is.
# MEASURED over every registered handler's signature rather than guessed: these
# two are the only agent-shaped names on the whole MCP surface — there is no
# ``x_agent_id``, ``caller_agent_id`` or ``target_agent_id`` here, unlike REST.
SELF_ID_PARAMS = frozenset({"agent_id", "filter_agent_id"})

# The verified identity, and the binders that consume it. A handler binds by
# reassigning the parameter from one of these, so a caller-supplied value
# cannot survive under that name.
VERIFIED_IDENTITY = "_get_agent_id"
IDENTITY_BINDERS = frozenset({VERIFIED_IDENTITY, "effective_write_agent_id"})

# ``(tool, param)`` pairs where the name is NOT a claim about who the caller is.
# ``test_the_excluded_identities_are_still_live`` fails if one stops appearing,
# so a rename cannot turn an exclusion into silence.
SELF_ID_EXCLUDED: dict[tuple[str, str], str] = {
    ("caura_keystones_set", "agent_id"): (
        "the TARGET agent a rule binds to, not the caller — the handler's own "
        "Field description says so in as many words, and it derives the caller "
        "separately as caller_agent_id = _get_agent_id() or 'mcp-agent', never "
        "falling back to this parameter"
    ),
}

KNOWN_GAP_PREFIX = "KNOWN GAP:"


# Invariant 1. Empty — every tool's declared ops match what its handler
# accepts. (Held one KNOWN GAP until caura_manage declared bulk_delete and
# lineage, which plugin/tools.json had been under-publishing.)
DECLARED_OPS_ALLOWLIST: dict[str, str] = {}

# Invariant 2. Empty, and that is the point: every mutating op is gated today,
# so the list records no exceptions and a new one has to be argued for here.
WRITE_SCOPE_ALLOWLIST: dict[str, str] = {}

# Invariant 3. Also empty — every declared threshold reaches a gate.
TRUST_ALLOWLIST: dict[str, str] = {}

# Invariant 4. One entry, and it is deliberately NOT called a gap.
SELF_BIND_ALLOWLIST: dict[str, str] = {
    "caura_recall.filter_agent_id": (
        "author filter, not a visibility identity. The REST twin DOES gate the "
        "same name — routes/memories.py calls enforce_self_agent on "
        "body.filter_agent_id — and the reason is a fallback that exists only "
        "there: eff_agent_id = auth.agent_id or body.caller_agent_id or "
        "body.filter_agent_id, so on REST the filter can BECOME the identity. "
        "On this path caller_agent_id is bound to the rebound agent_id and the "
        "filter reaches search_memories as a pure author narrowing, so the "
        "mechanism that made the gate necessary is absent. Recorded as a "
        "SURFACE DIVERGENCE rather than a leak because that is what was "
        "verified: whether the narrowing alone can disclose a peer's rows "
        "depends on the storage visibility predicate, which is asserted in "
        "tests/test_h06_m30_recall_identity.py and was not traced to SQL here"
    ),
}


# ---------------------------------------------------------------------------
# Handler inspection
# ---------------------------------------------------------------------------


@functools.cache
def _parse(fn) -> ast.AST | None:
    try:
        return ast.parse(textwrap.dedent(inspect.getsource(fn)))
    except (OSError, TypeError, SyntaxError):
        return None


def _set_literal(node) -> frozenset[str] | None:
    """String constants of a set/list/tuple literal, or None if it is neither."""
    if not isinstance(node, ast.Set | ast.List | ast.Tuple):
        return None
    values = {
        e.value
        for e in node.elts
        if isinstance(e, ast.Constant) and isinstance(e.value, str)
    }
    return frozenset(values) or None


def _accepted_ops(fn) -> frozenset[str] | None:
    """Op names the handler validates ``op`` against, or None if it takes no op.

    TWO SPELLINGS ARE LIVE and neither is the house style, so both are read:
    ``caura_manage`` and ``caura_doc`` assign ``_valid_ops = {...}`` and then
    test ``op not in _valid_ops``; ``caura_keystones_set`` inlines the set at
    the comparison. Reading only one of them would return None for the other,
    and None is "this tool has no ops" — which passes invariant 1 silently.
    ``test_the_op_scan_reads_both_spellings`` is what stops that.
    """
    tree = _parse(fn)
    if tree is None:
        return None
    assigned: dict[str, frozenset[str]] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            literal = _set_literal(node.value)
            if literal:
                for target in node.targets:
                    if isinstance(target, ast.Name):
                        assigned[target.id] = literal
    for node in ast.walk(tree):
        if (
            not isinstance(node, ast.Compare)
            or not isinstance(node.left, ast.Name)
            or node.left.id != "op"
            or len(node.ops) != 1
            or not isinstance(node.ops[0], ast.NotIn)
        ):
            continue
        comparator = node.comparators[0]
        literal = _set_literal(comparator)
        if literal:
            return literal
        if isinstance(comparator, ast.Name) and comparator.id in assigned:
            return assigned[comparator.id]
    return None


def _calls_named(node, name: str) -> bool:
    return any(
        isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == name
        for n in ast.walk(node)
    )


def _write_scope_coverage(fn) -> tuple[frozenset[str], bool]:
    """``(ops named in a guarded write-scope check, gate called unconditionally)``.

    The two shapes, both live::

        if err := _check_write_scope():                       -> covers every op
        if op in {"a", "b"} and (err := _check_write_scope()): -> covers a and b

    An ``if`` whose test calls the gate without naming ops is read as covering
    the whole tool, which is what the op-less tools do.
    """
    tree = _parse(fn)
    if tree is None:
        return frozenset(), False
    named: set[str] = set()
    unconditional = False
    for node in ast.walk(tree):
        if not isinstance(node, ast.If) or not _calls_named(
            node.test, WRITE_SCOPE_GATE
        ):
            continue
        ops = None
        for sub in ast.walk(node.test):
            if (
                isinstance(sub, ast.Compare)
                and isinstance(sub.left, ast.Name)
                and sub.left.id == "op"
                and len(sub.ops) == 1
                and isinstance(sub.ops[0], ast.In)
            ):
                ops = _set_literal(sub.comparators[0])
                break
        if ops is None:
            unconditional = True
        else:
            named |= ops
    return frozenset(named), unconditional


@functools.cache
def _reachable_calls(fn, _depth: int = 0) -> frozenset[str]:
    """Called names, following module-local helpers two levels down.

    Same depth and same ``__module__`` filter as the REST inventory's
    ``_classify``, and for the same reason: an imported service function would
    credit a handler with a gate that runs three layers away, which is exactly
    the attribution these files avoid. ``ast.walk`` ignores control flow, so a
    gate inside an ``if`` counts — ordering is a behavioural question and
    ``test_mcp_write_scope_gate.py`` is where it is asked.
    """
    tree = _parse(fn)
    if tree is None:
        return frozenset()
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            if isinstance(node.func, ast.Name):
                found.add(node.func.id)
            elif isinstance(node.func, ast.Attribute):
                found.add(node.func.attr)
    if _depth >= 2:
        return frozenset(found)
    module_globals = getattr(inspect.getmodule(fn), "__dict__", {})
    for name in sorted(found):
        target = module_globals.get(name)
        if not inspect.isfunction(target):
            continue
        if getattr(target, "__module__", None) != fn.__module__:
            continue
        found |= _reachable_calls(target, _depth + 1)
    return frozenset(found)


def _identity_params(fn) -> frozenset[str]:
    """``SELF_ID_PARAMS`` this handler declares, from its own signature.

    ``inspect.signature`` is safe here where it was not for FastAPI routes:
    these are plain functions with no request model, and ``mcp_server`` has no
    ``from __future__ import annotations``, so nothing is a forward-reference
    string. ``test_the_identity_scan_sees_both_names`` pins that.
    """
    try:
        params = inspect.signature(fn).parameters
    except (TypeError, ValueError):
        return frozenset()
    return frozenset(params) & SELF_ID_PARAMS


def _rebound_params(fn) -> frozenset[str]:
    """Parameters reassigned from the verified identity.

    Matching the ASSIGNMENT TARGET is what makes this decidable, and it is why
    a rule works here where the REST sibling had to delete its equivalent. That
    one matched ``auth.agent_id or <param>`` anywhere in a body, which also
    matched an audit-log line assigning a DIFFERENT variable — an ``any()``
    over a body cannot tell the deciding expression from a bystander. Here the
    caller's own name is the thing being overwritten, so a match means the
    supplied value is gone, not merely that a similar expression exists.

    FOUR SPELLINGS ARE LIVE, and each later one is why this is not a one-liner::

        agent_id = _get_agent_id() or agent_id                  # 6 tools
        agent_id = effective_write_agent_id(_get_agent_id(), …) # caura_write

        caller_agent_id = _get_agent_id()                       # manage, doc
        agent_id = caller_agent_id or agent_id

        agent_id_effective = _get_agent_id() or agent_id        # keystones

    The third binds through an intermediate local, so a check that only looked
    for a binder CALL in the assignment's value reports those two tools as
    unbound. The fourth never assigns to the parameter at all — it folds the
    parameter into a NEW name and uses that name from then on.

    The fourth is the shape the REST sibling's deleted rule got wrong, so it is
    admitted on a stricter condition than "an assignment of this shape exists":
    every LOAD of the parameter must sit inside such an assignment. If the raw
    parameter is read anywhere else, the caller's value is still reachable and
    the tool is reported unbound. That is a property of the whole function
    body, not of one line, which is exactly what the REST version lacked.

    Both later shapes were found by ``test_the_identity_scan_sees_both_names``
    and by the invariant itself failing, not by reading ahead.

    The claim is bounded and worth stating plainly: it proves the caller's
    value cannot be read under the parameter's own name, not that no other
    variable in the handler carries a caller-supplied identity.
    """
    tree = _parse(fn)
    if tree is None:
        return frozenset()

    def _called_names(node) -> set[str]:
        return {
            n.func.id
            for n in ast.walk(node)
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
        }

    # Pass 1: locals that hold the verified identity, e.g. ``caller_agent_id``.
    derived: set[str] = set()
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Assign)
            and _called_names(node.value) & IDENTITY_BINDERS
        ):
            derived |= {t.id for t in node.targets if isinstance(t, ast.Name)}

    # Pass 2: the binding assignments, and which Name nodes they consume.
    rebound: set[str] = set()
    consumed: set[int] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign):
            continue
        referenced = {n.id for n in ast.walk(node.value) if isinstance(n, ast.Name)}
        if not (_called_names(node.value) & IDENTITY_BINDERS or referenced & derived):
            continue
        rebound |= {
            t.id for t in node.targets if isinstance(t, ast.Name)
        } & SELF_ID_PARAMS
        # Only a DIRECT binder call folds a parameter into another name. The
        # ``referenced & derived`` path above must not: once ``agent_id`` is
        # itself rebound it joins ``derived``, and every later assignment
        # mentioning it would otherwise be read as consuming its own arguments
        # — which reported caura_recall's filter_agent_id as bound because the
        # search call that carries it also carries the bound agent_id.
        if _called_names(node.value) & IDENTITY_BINDERS:
            consumed |= {id(n) for n in ast.walk(node.value) if isinstance(n, ast.Name)}

    # Pass 3: a parameter folded into another name counts only if its raw value
    # is never read anywhere else.
    for param in SELF_ID_PARAMS - rebound:
        loads = [
            n
            for n in ast.walk(tree)
            if isinstance(n, ast.Name) and n.id == param and isinstance(n.ctx, ast.Load)
        ]
        if loads and all(id(n) in consumed for n in loads):
            rebound.add(param)
    return frozenset(rebound)


class _Tool(NamedTuple):
    name: str
    spec: object
    declared_ops: frozenset[str]
    accepted_ops: frozenset[str] | None
    guarded_ops: frozenset[str]
    gate_unconditional: bool
    calls: frozenset[str]
    identity_params: frozenset[str]
    rebound_params: frozenset[str]

    @property
    def ops(self) -> frozenset[str]:
        """What the handler really takes, falling back to what it declares."""
        return self.accepted_ops if self.accepted_ops is not None else self.declared_ops


def _tools() -> list[_Tool]:
    rows = []
    for name, spec in sorted(tools.REGISTRY.items()):
        handler = spec.handler
        guarded, unconditional = _write_scope_coverage(handler)
        rows.append(
            _Tool(
                name=name,
                spec=spec,
                declared_ops=frozenset(op.name for op in (spec.ops or ())),
                accepted_ops=_accepted_ops(handler),
                guarded_ops=guarded,
                gate_unconditional=unconditional,
                calls=_reachable_calls(handler),
                identity_params=_identity_params(handler),
                rebound_params=_rebound_params(handler),
            )
        )
    return rows


def _ops_disagree(tool: _Tool) -> bool:
    """Invariant 1: the handler accepts a different op set than it declares."""
    return tool.accepted_ops is not None and tool.accepted_ops != tool.declared_ops


def _declared_trust(tool: _Tool) -> int:
    """The threshold the manifest publishes: the tool's, else its highest op's."""
    return tool.spec.trust_required or max(
        (op.trust_required for op in (tool.spec.ops or ())), default=0
    )


def _trust_uncorroborated(tool: _Tool) -> bool:
    """Invariant 3: publishes a threshold, reaches no gate that could refuse."""
    return _declared_trust(tool) > 0 and not (tool.calls & TRUST_GATES)


def _identity_unbound(tool: _Tool, param: str) -> bool:
    """Invariant 4: a caller-supplied identity that nothing rebinds."""
    if (tool.name, param) in SELF_ID_EXCLUDED:
        return False
    return param not in tool.rebound_params


def _mutating(tool: _Tool) -> frozenset[str]:
    """Accepted ops that change state; empty for a read-only op-less tool."""
    if not tool.ops:
        return (
            frozenset({"<whole tool>"}) if tool.name in MUTATING_TOOLS else frozenset()
        )
    return frozenset(tool.ops & MUTATING_OPS)


def _write_scope_ungated(tool: _Tool) -> frozenset[str]:
    """Invariant 2: mutating ops that never reach the write-scope gate."""
    if tool.gate_unconditional:
        return frozenset()
    return _mutating(tool) - tool.guarded_ops


def _report(tool: _Tool) -> str:
    return (
        f"    {tool.name}\n"
        f"        declared ops: {sorted(tool.declared_ops) or 'none'}\n"
        f"        accepted ops: {sorted(tool.accepted_ops) if tool.accepted_ops is not None else 'n/a'}\n"
        f"        write-scope:  {sorted(tool.guarded_ops) or ('ALL' if tool.gate_unconditional else 'NONE')}\n"
        f"        trust gates:  {sorted(tool.calls & TRUST_GATES) or 'none'}\n"
        f"        identity params: {sorted(tool.identity_params) or 'none'}\n"
        f"        rebound from verified: {sorted(tool.rebound_params) or 'none'}"
    )


# ---------------------------------------------------------------------------
# The invariants
# ---------------------------------------------------------------------------


def test_declared_ops_match_accepted_ops() -> None:
    """What a tool publishes must be what it takes.

    ``plugin/tools.json`` is generated from ``REGISTRY`` and CI-locked to it by
    ``test_tools_export_in_sync.py``, so a tool's ``ops`` list is the contract
    clients read. The handler's own validation set is what actually runs. When
    they drift, the manifest is wrong in a way no existing test can see: the
    export check compares the manifest to the registry, and both would agree
    while both were incomplete.
    """
    offenders = []
    for tool in _tools():
        if not _ops_disagree(tool):
            continue
        if tool.name in DECLARED_OPS_ALLOWLIST:
            continue
        undeclared = sorted(tool.accepted_ops - tool.declared_ops)
        phantom = sorted(tool.declared_ops - tool.accepted_ops)
        offenders.append(
            f"{_report(tool)}\n"
            f"        undeclared (handler takes, registry omits): {undeclared or 'none'}\n"
            f"        phantom (registry declares, handler rejects): {phantom or 'none'}"
        )
    assert not offenders, (
        f"{len(offenders)} tool(s) declare a different op set than they accept:\n"
        + "\n".join(offenders)
        + "\n\nAdd the missing OpSpec entries and regenerate plugin/tools.json, "
        f"or add a line to DECLARED_OPS_ALLOWLIST in {__file__}."
    )


def test_every_mutating_op_passes_the_write_scope_gate() -> None:
    """A read-only credential must not reach a mutating op.

    Enumerated from the handler's accepted ops rather than from the registry,
    because the handler is what runs. The two agree today, but only because
    invariant 1 holds them there, and they did not when this was written:
    ``caura_manage`` accepted ``bulk_delete`` without declaring it, so a
    registry-driven scan would have skipped a real, correctly gated op. Reading
    the handler keeps this check independent of that one rather than downstream
    of it.
    """
    offenders = []
    for tool in _tools():
        if tool.name in WRITE_SCOPE_ALLOWLIST:
            continue
        ungated = sorted(_write_scope_ungated(tool))
        if ungated:
            offenders.append(f"{_report(tool)}\n        UNGATED: {ungated}")
    assert not offenders, (
        f"{len(offenders)} tool(s) accept a mutating op that never reaches "
        f"{WRITE_SCOPE_GATE}:\n"
        + "\n".join(offenders)
        + f"\n\nGate the op, or add a line to WRITE_SCOPE_ALLOWLIST in {__file__} "
        "saying why this op does not mutate."
    )


def test_declared_trust_is_corroborated_by_a_gate() -> None:
    """A published threshold must have something under it that can refuse.

    ``required_trust()`` is called by nothing, so the number in the manifest
    and the number the server applies are independent. This does not check they
    are EQUAL — the thresholds come from scope, not from the registry, so there
    is no single number to compare against — but a tool that advertises a trust
    requirement while reaching no trust gate at all is advertising nothing.
    """
    offenders = []
    for tool in _tools():
        if tool.name in TRUST_ALLOWLIST:
            continue
        if _trust_uncorroborated(tool):
            offenders.append(
                f"{_report(tool)}\n        DECLARES trust "
                f"{_declared_trust(tool)}, reaches no gate"
            )
    assert not offenders, (
        f"{len(offenders)} tool(s) publish a trust_required that no reachable "
        "gate could enforce:\n"
        + "\n".join(offenders)
        + "\n\nEither the threshold is wrong and should be 0, or the gate is "
        f"missing. If neither, add a line to TRUST_ALLOWLIST in {__file__}."
    )


def test_every_caller_supplied_identity_is_bound() -> None:
    """A caller must not act as an agent it merely named.

    The MCP half of the axis #1367 enumerated for REST. A parameter satisfies
    it three ways: the handler REBINDS it from the gateway-verified identity;
    it is excluded because the name means something other than "who I am"; or
    it carries a line in ``SELF_BIND_ALLOWLIST`` saying what it does mean.
    """
    offenders = []
    for tool in _tools():
        for param in sorted(tool.identity_params):
            if not _identity_unbound(tool, param):
                continue
            if f"{tool.name}.{param}" in SELF_BIND_ALLOWLIST:
                continue
            offenders.append(f"{_report(tool)}\n        UNBOUND: {param}")
    assert not offenders, (
        f"{len(offenders)} caller-supplied agent identit(ies) are never bound "
        f"to {VERIFIED_IDENTITY}():\n"
        + "\n".join(offenders)
        + f"\n\nRebind the parameter, or add a line to SELF_BIND_ALLOWLIST in "
        f"{__file__} saying what it means if not 'act as this agent'."
    )


def test_the_identity_scan_sees_both_names() -> None:
    """``_identity_params`` returning less passes invariant 4 silently.

    Pinned on the two tools that matter: ``caura_recall`` is the only handler
    taking both names, and ``caura_write`` binds through
    ``effective_write_agent_id`` rather than the bare ``_get_agent_id() or``
    shape, so it is the one a narrower binder set would drop.
    """
    by_name = {tool.name: tool for tool in _tools()}
    expected = {
        "caura_recall": ({"agent_id", "filter_agent_id"}, {"agent_id"}),
        "caura_write": ({"agent_id"}, {"agent_id"}),
        "caura_manage": ({"agent_id"}, {"agent_id"}),
    }
    wrong = {}
    for name, (want_params, want_bound) in expected.items():
        tool = by_name.get(name)
        if tool is None:
            wrong[name] = "tool absent from the registry"
        elif tool.identity_params != frozenset(want_params):
            wrong[name] = (
                f"params {sorted(tool.identity_params)}, want {sorted(want_params)}"
            )
        elif tool.rebound_params != frozenset(want_bound):
            wrong[name] = (
                f"rebound {sorted(tool.rebound_params)}, want {sorted(want_bound)}"
            )
    assert not wrong, (
        "the identity scan stopped seeing what it was written for:\n"
        + "".join(f"    {k}: {v}\n" for k, v in sorted(wrong.items()))
        + "Fix _identity_params / _rebound_params before trusting a green run."
    )


def test_the_excluded_identities_are_still_live() -> None:
    """An exclusion for a parameter no tool takes is a decision about nothing,
    and the next reader takes it as evidence the question was asked recently."""
    live = {(tool.name, param) for tool in _tools() for param in tool.identity_params}
    dead = sorted(set(SELF_ID_EXCLUDED) - live)
    assert not dead, (
        f"SELF_ID_EXCLUDED names (tool, param) pairs that do not exist: {dead}\n"
        f"Live agent-shaped params: {sorted(live)}\n"
        "Delete the entries, or correct them to the name that replaced them."
    )


# ---------------------------------------------------------------------------
# Guards on the guards
# ---------------------------------------------------------------------------


def test_the_op_scan_reads_both_spellings() -> None:
    """``_accepted_ops`` returns None for a tool it cannot parse, and None
    means "no ops" — which passes invariant 1 without checking anything.

    So the two live spellings are pinned by name. ``caura_manage`` uses the
    ``_valid_ops = {...}`` assignment form and ``caura_keystones_set`` inlines
    the set; a change that broke either would otherwise turn its tool into a
    silent pass.
    """
    by_name = {tool.name: tool for tool in _tools()}
    expected = {
        "caura_manage": {
            "read",
            "update",
            "transition",
            "delete",
            "bulk_delete",
            "lineage",
        },
        "caura_doc": {"write", "read", "query", "delete", "list_collections", "search"},
        "caura_keystones_set": {"set", "delete"},
    }
    wrong = {
        name: f"found {sorted(by_name[name].accepted_ops) if by_name[name].accepted_ops else None}"
        for name, want in expected.items()
        if name in by_name and by_name[name].accepted_ops != frozenset(want)
    }
    missing = sorted(set(expected) - set(by_name))
    assert not wrong and not missing, (
        "the op scan stopped reading a live spelling:\n"
        + "".join(f"    {k}: {v}\n" for k, v in sorted(wrong.items()))
        + (f"    tools absent from the registry: {missing}\n" if missing else "")
        + "Fix _accepted_ops before trusting a green run — a tool it cannot "
        "read is indistinguishable from a tool with no ops."
    )


def test_every_accepted_op_is_classified() -> None:
    """An op in neither ``MUTATING_OPS`` nor ``READING_OPS`` is unexamined.

    Invariant 2 only looks at ops it knows mutate, so an unclassified name is
    not a failure there — it is an absence, which is the failure mode these
    files exist to end.
    """
    seen: set[str] = set()
    for tool in _tools():
        seen |= set(tool.ops)
    unclassified = sorted(seen - MUTATING_OPS - READING_OPS)
    assert not unclassified, (
        f"op name(s) in neither MUTATING_OPS nor READING_OPS: {unclassified}\n"
        "Add each to the set that describes it. An op nobody classified is an "
        "op invariant 2 silently skips."
    )


def test_every_op_less_tool_is_classified() -> None:
    """Same question for tools that take no op at all."""
    unclassified = sorted(
        tool.name
        for tool in _tools()
        if not tool.ops and tool.name not in MUTATING_TOOLS | READONLY_TOOLS
    )
    assert not unclassified, (
        f"op-less tool(s) in neither MUTATING_TOOLS nor READONLY_TOOLS: {unclassified}\n"
        "Add each. An unclassified tool is treated as read-only by _mutating, "
        "so a new mutating tool would pass invariant 2 by being forgotten."
    )


def test_the_rest_inventorys_tool_list_is_still_accurate() -> None:
    """The sibling file names the mutating tools in prose. Prose goes stale.

    It did: an earlier revision named three (``caura_write``, ``caura_manage``,
    ``caura_keystones_set``) when seven call the gate, and nothing failed —
    which is the same unfalsifiable-reason problem that file's own
    ``test_allowlist_reasons_that_name_a_mechanism_are_corroborated`` exists to
    catch, one level up in the docstring instead of in an allowlist.

    Checked here rather than there because this is the file that can compute
    the answer. A tool added or a gate removed fails this with both sets shown.
    """
    sibling = pathlib.Path(__file__).with_name("test_authz_gate_inventory.py")
    doc = ast.get_docstring(ast.parse(sibling.read_text())) or ""
    span = re.search(
        r"guard themselves with(.*?)That surface now has its own inventory",
        doc,
        re.S,
    )
    assert span, (
        "the paragraph this test reads has been reworded in "
        f"{sibling.name}. Re-anchor the search, or drop this test with the "
        "paragraph — do not leave it matching nothing, which passes."
    )
    claimed = set(re.findall(r"caura_\w+", span.group(1)))
    actual = {tool.name for tool in _tools() if WRITE_SCOPE_GATE in tool.calls}
    assert claimed == actual, (
        f"{sibling.name}'s docstring names the mutating MCP tools as "
        f"{sorted(claimed)}, but {sorted(actual)} call {WRITE_SCOPE_GATE}.\n"
        f"    only in the docstring: {sorted(claimed - actual) or 'none'}\n"
        f"    only in the code:      {sorted(actual - claimed) or 'none'}\n"
        "Update that paragraph. It is the scope statement a reader trusts "
        "before deciding this surface is someone else's problem."
    )


def test_the_registry_scan_is_not_silently_empty() -> None:
    """Guards the guard: every invariant above iterates ``_tools()``.

    Asserted loosely — the point is "the registry loaded", not a number to
    update whenever a tool is added. ``_autoload_specs`` imports every
    ``caura_*`` module at import time, so a failure here is that mechanism, not
    a deleted tool.
    """
    rows = _tools()
    assert len(rows) >= 10, (
        f"only {len(rows)} tools in the registry; there were 12 when this was "
        "written, so the autoload has probably stopped finding tool modules."
    )
    handlerless = sorted(tool.name for tool in rows if tool.spec.handler is None)
    assert not handlerless, (
        f"tool(s) with no handler: {handlerless}\nEvery invariant here reads the "
        "handler, so a None handler is a tool nothing checks."
    )


# ---------------------------------------------------------------------------
# Allowlist hygiene
# ---------------------------------------------------------------------------


class _Axis(NamedTuple):
    """One invariant, and everything the hygiene checks need to police it.

    ``needed`` returns every key the axis could legitimately hold, mapped to
    whether its invariant would STILL flag it. That one callable is what lets
    the two checks below be written once: a key the map does not contain is
    stale, and a key it maps to ``False`` is unnecessary.

    It also means "still needed" has ONE definition per axis, shared with the
    invariant test itself rather than restated beside it — the REST sibling's
    ``_Axis`` docstring records that as the original point, and this file
    learned it the slow way. An earlier revision policed only the axes a
    hand-kept ``_NECESSITY_CHECKED`` set happened to name, plus a meta-test to
    guard the set; giving the axis a predicate it cannot be constructed
    without deletes both, and the SELF_BIND key shape stops being a special
    case that three separate call sites had to know about.
    """

    name: str
    allowlist: dict[str, str]
    needed: Callable[[], dict[str, bool]]
    gap_ceiling: int


_ALLOWLISTS = (
    _Axis(
        "DECLARED_OPS_ALLOWLIST",
        DECLARED_OPS_ALLOWLIST,
        lambda: {t.name: _ops_disagree(t) for t in _tools()},
        0,
    ),
    _Axis(
        "WRITE_SCOPE_ALLOWLIST",
        WRITE_SCOPE_ALLOWLIST,
        lambda: {t.name: bool(_write_scope_ungated(t)) for t in _tools()},
        0,
    ),
    _Axis(
        "TRUST_ALLOWLIST",
        TRUST_ALLOWLIST,
        lambda: {t.name: _trust_uncorroborated(t) for t in _tools()},
        0,
    ),
    _Axis(
        "SELF_BIND_ALLOWLIST",
        SELF_BIND_ALLOWLIST,
        lambda: {
            f"{t.name}.{p}": _identity_unbound(t, p)
            for t in _tools()
            for p in t.identity_params
        },
        0,
    ),
)


def test_allowlists_have_no_stale_entries() -> None:
    """An entry naming something the registry no longer serves must fail rather
    than sit there looking like a considered decision."""
    stale = {}
    for axis in _ALLOWLISTS:
        gone = sorted(set(axis.allowlist) - set(axis.needed()))
        if gone:
            stale[axis.name] = gone
    assert not stale, (
        f"allowlist entries name things this registry does not serve:\n{stale}\n"
        "Delete the entries; do not update them to match something you have not "
        "re-examined."
    )


def test_allowlists_have_no_unnecessary_entries() -> None:
    """An entry excusing something that now passes on its own is dead.

    ``test_allowlists_have_no_stale_entries`` only catches an entry naming
    something that no longer exists. It does not catch the commoner case: the
    gap gets FIXED and the line stays, still reading as though someone decided
    the excuse was warranted. A later offender can then arrive under the same
    key and be excused by a reason written about something else.

    Every axis is checked, including the empty ones — emptying an allowlist is
    exactly when its check stops being exercised and starts rotting unnoticed.
    That is structural rather than remembered: ``_Axis.needed`` cannot be
    omitted when an axis is declared.
    """
    unnecessary = []
    for axis in _ALLOWLISTS:
        needed = axis.needed()
        for key in sorted(axis.allowlist):
            if needed.get(key) is False:
                unnecessary.append(
                    f"{axis.name}[{key}]: the invariant would pass without it"
                )
    assert not unnecessary, (
        "allowlist entries excuse things that no longer need excusing:\n"
        + "\n".join(f"    {u}" for u in unnecessary)
        + "\n\nDelete the entries. If one was a KNOWN GAP, lower that axis's "
        "gap_ceiling in _ALLOWLISTS to match."
    )


def test_known_gaps_do_not_grow() -> None:
    """A ratchet, one ceiling per axis so headroom is not fungible between them.

    Fixing a gap means deleting its line and lowering the ceiling; a new gap
    must not borrow the headroom a fix created.
    """
    for axis in _ALLOWLISTS:
        gaps = sorted(
            f"{key}  ({reason})"
            for key, reason in axis.allowlist.items()
            if reason.startswith(KNOWN_GAP_PREFIX)
        )
        assert len(gaps) <= axis.gap_ceiling, (
            f"{axis.name}: {len(gaps)} known gaps, ceiling is {axis.gap_ceiling}:\n    "
            + "\n    ".join(gaps)
            + "\n\nClose the gap rather than raising the ceiling. The ceiling "
            "exists to be lowered."
        )
        assert len(gaps) == axis.gap_ceiling, (
            f"{axis.name}: {len(gaps)} known gaps but the ceiling is still "
            f"{axis.gap_ceiling} — a gap was fixed without lowering it, so the "
            "ratchet has slack a new gap could take up silently. Set this "
            f"axis's gap_ceiling to {len(gaps)}."
        )
