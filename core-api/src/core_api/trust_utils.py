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

    Self-author tier (≥ 1) covers exactly ``scope=agent`` carrying an
    **explicit** ``agent_id`` that equals the caller — an agent shaping
    its own private policy. "Self-scoped" is therefore a property of the
    request, not of the caller: ``scope=agent`` with ``agent_id``
    OMITTED is **not** self-authored and stays at ≥ 2, because nothing
    in the payload names the agent the rule binds to. Everything else —
    ``scope=fleet``, ``scope=tenant``, or ``scope=agent`` targeting
    another agent (admin-on-behalf) — keeps the cross-agent governance
    bar at ≥ 2 as well.

    Read paths do not call this; reads remain ungated.
    """
    # ``target_agent_id is not None`` is defence-in-depth: without it,
    # a rule stored without an ``agent_id`` (malformed row) could land
    # in the self-author tier when ``caller_agent_id`` is somehow also
    # None — a state today's REST handlers don't reach (caller has a
    # ``"rest-admin"`` fallback) but the explicit check keeps the
    # guard robust if a future caller can produce ``None`` here.
    #
    # Its visible consequence on the write path is the ≥ 2 floor for
    # ``scope=agent`` with no ``agent_id``. That is correct and stays:
    # such a payload names no target, storage rejects it anyway
    # ("scope=agent requires agent_id"), and inferring the caller here
    # would silently rewrite an under-specified governance rule into a
    # narrower one. ``keystone_trust_hint`` exists so the resulting 403
    # says which of the two problems the caller actually has.
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


def keystone_trust_hint(
    scope: str | None,
    target_agent_id: str | None,
    caller_agent_id: str,
) -> str:
    """Remedial suffix for a keystone 403, or ``""`` when none applies.

    A wet test surfaced the one refusal whose message is actively
    misleading: ``scope=agent`` with ``agent_id`` omitted is refused at
    the ≥ 2 bar, and the bare ``trust_level=1 < required 2`` text reads
    as "the docs are wrong about self-scope needing trust 1" rather
    than "you left the target out". Both surfaces append this so the
    caller learns which knob to turn.

    Deliberately additive: callers concatenate it onto the existing
    trust-error string rather than replacing it, so the pinned
    ``Agent '…' (trust_level=N) < required M.`` shape is untouched and
    anything parsing that prefix keeps working.

    Returns ``""`` for every other refusal (``scope=fleet``,
    ``scope=tenant``, cross-agent ``scope=agent``, the stored-shape
    escalation guard) — those messages are already accurate and the
    self-author tier genuinely does not apply to them.

    The wording states what ``agent_id`` *means* rather than promising
    the retry will succeed: on REST, an identity asserted through the
    ``X-Agent-ID`` header alone is unverified and
    ``_effective_min_for_caller`` holds the floor at ≥ 2 even for a
    correctly-shaped self-authored rule. Naming the
    gateway-verified-identity precondition keeps the hint true on both
    paths instead of sending an admin-key caller round a loop that
    cannot close.
    """
    if scope == "agent" and target_agent_id is None:
        return (
            " Note: scope=agent with agent_id omitted is not a self-authored rule — "
            "the self-author tier (trust >= 1) covers only scope=agent carrying an "
            "explicit agent_id equal to the caller, asserted through a "
            f"gateway-verified agent identity. Set agent_id='{caller_agent_id}' to "
            "target yourself; every other shape needs trust >= 2."
        )
    return ""
