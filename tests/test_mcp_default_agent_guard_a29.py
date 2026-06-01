"""A29: extend the A14 ``MISSING_AGENT_ID`` guard from the write surfaces
to the **read** surfaces of the MCP tool family.

After A29 the following tools refuse with ``MISSING_AGENT_ID`` on the
gateway path when the caller relies on the literal ``"mcp-agent"`` default
(i.e. the gateway resolved a tenant credential but did NOT inject an
``X-Agent-ID`` header):

    memclaw_recall, memclaw_list, memclaw_stats, memclaw_insights,
    memclaw_keystones, and every op of memclaw_doc (read, query,
    search, list_collections, write, delete).

What stays un-guarded (per the A14 scope decision):
    memclaw_manage op=lineage / op=read.

What was already guarded by A14 and MUST still refuse (regression guard):
    memclaw_write — kept here as a sanity ping that the broader edit
    on memclaw_doc didn't accidentally drop the existing write guard.
"""

from __future__ import annotations

import uuid

import pytest

from core_api import mcp_server
from tests._mcp_test_helpers import parse_envelope

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]


# ---------------------------------------------------------------------------
# 1. Simple read tools — single op axis, so we parametrize by tool name only.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "tool_name,kwargs",
    [
        # memclaw_recall: query is the only required kwarg.
        ("memclaw_recall", {"query": "anything"}),
        # memclaw_list: scope defaults to "agent" which passes pre-validation.
        ("memclaw_list", {}),
        # memclaw_stats: every param optional.
        ("memclaw_stats", {}),
        # memclaw_insights: focus is required and must be one of the allowed
        # slugs to clear pre-validation before the guard fires.
        ("memclaw_insights", {"focus": "contradictions"}),
        # memclaw_keystones: every param optional.
        ("memclaw_keystones", {}),
    ],
)
async def test_read_tool_refuses_default_agent_on_gateway(mcp_env, tool_name, kwargs):
    """Each of the 5 simple read tools must refuse the default identity on
    the gateway path. Boundary rejection — no downstream service hit.

    The guard fires *after* schema/scope pre-validation but *before* any
    service call, so the kwargs above are the minimum needed to reach it.
    """
    tool = getattr(mcp_server, tool_name)

    token = mcp_server._via_gateway_var.set(True)
    try:
        out = await tool(**kwargs)
    finally:
        mcp_server._via_gateway_var.reset(token)

    payload = parse_envelope(out)
    assert payload["error"]["code"] == "MISSING_AGENT_ID", (
        f"{tool_name} did not refuse the default identity on gateway path; "
        f"got {payload!r}"
    )


# ---------------------------------------------------------------------------
# 2. memclaw_doc — guard fires on every op (A29 closes the delete gap, and
#    write was already covered by A14; included as a regression guard).
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "op,extra_kwargs",
    [
        # Reads.
        ("read", {"collection": "skills", "doc_id": "rule-1"}),
        ("query", {"collection": "skills"}),
        ("search", {"collection": "skills", "query": "anything"}),
        ("list_collections", {}),
        # Writes (already A14-covered; kept as regression guards).
        ("write", {"collection": "skills", "doc_id": "rule-1", "data": {"k": "v"}}),
        ("delete", {"collection": "skills", "doc_id": "rule-1"}),
    ],
)
async def test_memclaw_doc_refuses_default_agent_on_gateway(mcp_env, op, extra_kwargs):
    """memclaw_doc must refuse the default identity on every op when
    gateway-routed. Storage helpers don't need to succeed — the guard
    is the first non-validation step, so we only need the kwargs to
    clear per-op argument validation."""
    token = mcp_server._via_gateway_var.set(True)
    try:
        out = await mcp_server.memclaw_doc(op=op, **extra_kwargs)
    finally:
        mcp_server._via_gateway_var.reset(token)

    payload = parse_envelope(out)
    assert payload["error"]["code"] == "MISSING_AGENT_ID", (
        f"memclaw_doc op={op!r} did not refuse the default identity on "
        f"gateway path; got {payload!r}"
    )


# ---------------------------------------------------------------------------
# 3. Standalone path keeps the default-identity ergonomics.
# ---------------------------------------------------------------------------


async def test_read_default_agent_in_standalone_does_not_trigger_guard(mcp_env):
    """Standalone (no ``_via_gateway_var`` flip) must NOT raise
    ``MISSING_AGENT_ID`` for the default identity — that's the whole
    point of the standalone vs. gateway split. We don't care whether
    the call ultimately succeeds (a missing storage mock may surface a
    different error, or even bubble a raw exception out of an un-mocked
    downstream); we only assert that *if* an envelope comes back, the
    error code is not ``MISSING_AGENT_ID``. An exception bubbling out
    is also acceptable proof that the guard did not fire (the guard
    short-circuits to an envelope, never raises)."""
    # Don't flip _via_gateway_var — standalone is the default in tests.
    try:
        out = await mcp_server.memclaw_recall(query="anything")
    except Exception:
        # The call reached past the guard and crashed downstream
        # (no storage mock) — that itself proves the guard did not fire.
        return

    payload = parse_envelope(out)
    err = payload.get("error") if isinstance(payload, dict) else None
    if err is not None:
        assert err.get("code") != "MISSING_AGENT_ID", (
            "Standalone path must not trip the gateway-default-identity guard; "
            f"got {payload!r}"
        )


# ---------------------------------------------------------------------------
# 4. Explicit agent_id bypasses the guard on a read tool.
# ---------------------------------------------------------------------------


async def test_read_explicit_agent_on_gateway_bypasses_guard(mcp_env):
    """Same gateway-routed path, but caller passed an explicit agent_id —
    the guard MUST NOT fire. The call may still error downstream (we
    haven't fully mocked the storage path), but the error code, if any,
    must not be ``MISSING_AGENT_ID``. An exception bubbling out of an
    un-mocked downstream is acceptable proof — the guard short-circuits
    to an envelope, never raises."""
    token = mcp_server._via_gateway_var.set(True)
    try:
        try:
            out = await mcp_server.memclaw_recall(
                query="anything", agent_id="real-agent"
            )
        except Exception:
            # Past the guard, crashed downstream — guard did not fire.
            return
    finally:
        mcp_server._via_gateway_var.reset(token)

    payload = parse_envelope(out)
    err = payload.get("error") if isinstance(payload, dict) else None
    if err is not None:
        assert err.get("code") != "MISSING_AGENT_ID", (
            "Explicit agent_id must bypass the gateway-default-identity guard; "
            f"got {payload!r}"
        )


# ---------------------------------------------------------------------------
# 5. A14 contract still holds — memclaw_write regression guard.
# ---------------------------------------------------------------------------


async def test_a14_write_guard_still_holds(mcp_env):
    """Sanity ping: the A29 extension did not regress the A14 write guard
    on memclaw_write itself. If this fails, the broader edit on the read
    surfaces likely also broke the write surface."""
    token = mcp_server._via_gateway_var.set(True)
    try:
        out = await mcp_server.memclaw_write(content="anything")
    finally:
        mcp_server._via_gateway_var.reset(token)

    payload = parse_envelope(out)
    assert payload["error"]["code"] == "MISSING_AGENT_ID"


# ---------------------------------------------------------------------------
# 6. Out-of-scope guard — memclaw_manage op=lineage stays un-guarded.
# ---------------------------------------------------------------------------


async def test_manage_lineage_is_not_guarded_by_a29(mcp_env):
    """memclaw_manage op=lineage is a *read* on memclaw_manage. The A14
    PR deliberately scoped manage's guard to its write ops (delete /
    update / transition / bulk_delete), and A29 did NOT extend the guard
    to manage's read ops. This test pins that intentional gap so a
    future refactor doesn't silently start refusing manage reads.

    Acceptable outcomes: either the handler returns a payload whose
    error code is NOT ``MISSING_AGENT_ID`` (e.g. NOT_FOUND for the
    fake UUID), or it raises a Python exception trying to talk to an
    un-mocked downstream — both prove the guard did not fire."""
    token = mcp_server._via_gateway_var.set(True)
    try:
        try:
            out = await mcp_server.memclaw_manage(
                op="lineage", memory_id=str(uuid.uuid4())
            )
        except Exception:
            # Past the guard, crashed downstream — guard did not fire.
            return
    finally:
        mcp_server._via_gateway_var.reset(token)

    payload = parse_envelope(out)
    err = payload.get("error") if isinstance(payload, dict) else None
    if err is not None:
        assert err.get("code") != "MISSING_AGENT_ID", (
            "memclaw_manage op=lineage MUST NOT be guarded by the A14/A29 "
            "default-identity refusal — manage's read ops are out of scope; "
            f"got {payload!r}"
        )
