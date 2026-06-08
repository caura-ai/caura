"""Shared trust-floor helpers for keystone write/delete paths.

Lives in its own module so the REST surface (``routes/keystones.py``)
and the MCP surface (``mcp_server.py``) can both import it without
a ``routes → mcp_server`` (or vice-versa) cross-import. Keeping the
policy in one place is what guarantees both surfaces enforce the same
matrix — earlier iterations duplicated the conditional and accumulated
drift commentary instead.
"""

from __future__ import annotations


def keystone_min_trust(
    scope: str,
    target_agent_id: str | None,
    caller_agent_id: str,
) -> int:
    """Trust floor for a keystone write or delete.

    Self-author tier (≥ 1) covers exactly ``scope=agent`` where the
    rule's ``agent_id`` is the caller — an agent shaping its own
    private policy. Everything else — ``scope=fleet``, ``scope=tenant``,
    or ``scope=agent`` targeting another agent (admin-on-behalf) —
    keeps the cross-agent governance bar at ≥ 2.

    Read paths do not call this; reads remain ungated.
    """
    # ``target_agent_id is not None`` is defence-in-depth: without it,
    # a rule stored without an ``agent_id`` (malformed row) could land
    # in the self-author tier when ``caller_agent_id`` is somehow also
    # None — a state today's REST handlers don't reach (caller has a
    # ``"rest-admin"`` fallback) but the explicit check keeps the
    # guard robust if a future caller can produce ``None`` here.
    if scope == "agent" and target_agent_id is not None and target_agent_id == caller_agent_id:
        return 1
    return 2


def effective_keystone_min_trust(
    new_scope: str,
    new_target_agent_id: str | None,
    stored_scope: str | None,
    stored_target_agent_id: str | None,
    caller_agent_id: str,
) -> int:
    """Trust floor for an upsert against a (possibly existing) rule.

    Without this, a trust-1 agent who knows the ``doc_id`` of a
    ``scope=fleet`` rule could overwrite it by submitting
    ``scope=agent`` + ``agent_id=<self>`` in the body — the new-shape
    floor (1) passes the gate and storage upserts unconditionally,
    silently dropping a tenant-wide rule and replacing it with one
    only the attacker controls.

    The fix takes the max of two floors:

      * The floor required to author the NEW shape supplied in the
        request body.
      * The floor required to author the STORED shape currently
        persisted under ``(tenant_id, doc_id)``, if any.

    For a fresh create (no stored row), only the new-shape floor
    applies — the caller picks a scope they're authorized for.
    """
    new_floor = keystone_min_trust(new_scope, new_target_agent_id, caller_agent_id)
    if stored_scope is None:
        return new_floor
    stored_floor = keystone_min_trust(stored_scope, stored_target_agent_id, caller_agent_id)
    return max(new_floor, stored_floor)
