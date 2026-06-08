"""Agent trust-level enforcement.

Shared by the MCP handlers in ``core_api.mcp_server`` and the REST routes
under ``core_api.routes`` so both surfaces gate on the same rule without
reaching into each other's private symbols.

``require_trust`` returns ``(trust, not_found, None)`` on pass and
``(trust, not_found, error_str)`` on fail. ``not_found`` is a typed flag
for the "agent row is missing" case so callers can distinguish "unknown
id" from "known id with insufficient trust" without parsing the error
string. The error string carries the ``_MCP_ERROR_PREFIX`` constant so
the MCP handler convention stays in one place; ``parse_trust_error``
strips that prefix so REST routes can surface the bare detail through
an ``HTTPException``.

When the agent row is missing (``not_found=True``), the gate falls back
to ``DEFAULT_TRUST_LEVEL`` rather than treating the row's absence as a
permission failure: API-key admission already proved authorization, so
a fresh agent that hasn't yet been materialised by a write must still
be allowed to use any tool the tenant's default policy permits. Code
paths that need the row to exist (per-agent tuning, audit attribution)
should call ``get_or_create_agent`` explicitly — the soft-pass here
applies only to the gate decision.
"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from core_api.constants import DEFAULT_TRUST_LEVEL

# Keep the prefix in one place so ``parse_trust_error`` and ``require_trust``
# can't drift if someone edits one. ``str.removeprefix`` is a no-op when the
# prefix isn't present, so parse_trust_error stays robust against future
# error strings that happen not to start with it.
_MCP_ERROR_PREFIX = "Error (403): "


async def require_trust(
    db: AsyncSession,
    tenant_id: str,
    agent_id: str,
    min_level: int,
) -> tuple[int, bool, str | None]:
    """Look up the agent's trust level and gate on ``min_level``.

    Returns ``(trust, not_found, None)`` on pass, ``(trust, not_found,
    error_str)`` on fail. ``not_found`` is ``True`` when no agent row
    exists for ``(tenant_id, agent_id)``; in that case the effective
    trust used for the gate decision is ``DEFAULT_TRUST_LEVEL`` (the
    same level a freshly-registered agent receives). Error format
    matches the wider MCP handler convention of ``"Error (403): …"``.

    .. warning::

       **Write paths MUST check ``not_found`` independently of
       ``error_str``.** The soft-pass returns ``(DEFAULT_TRUST_LEVEL,
       True, None)`` for a missing agent when ``min_level <=
       DEFAULT_TRUST_LEVEL`` — ``error_str`` is ``None`` so a caller
       that only gates on ``terr`` will let an unregistered, fabricated
       ``agent_id`` through. The soft-pass exists for read-only
       ergonomics (``memclaw_list``, recall) where attribution is
       cosmetic. On any path that persists records keyed to the caller-
       supplied ``agent_id`` (memories, audit-log rows, evolve
       outcomes, insights), an unregistered name corrupts the audit
       trail because identity attribution becomes unverifiable.

       Pattern to follow — see ``core_api/routes/evolve.py``::

           _, not_found, terr = await require_trust(...)
           if not_found:
               raise HTTPException(
                   status_code=403,
                   detail=f"Agent '{agent_id}' is not registered ...",
               )
           if terr:
               raise HTTPException(status_code=403, detail=parse_trust_error(terr))

       The same pattern is in ``routes/insights.py`` and the MCP
       handlers ``memclaw_evolve`` / ``memclaw_insights`` in
       ``mcp_server.py``. ``memclaw_list`` (read-only) intentionally
       does NOT check ``not_found`` and is the canonical "soft-pass
       OK" call site.
    """
    from core_api.services.agent_service import lookup_agent

    agent = await lookup_agent(db, tenant_id, agent_id)
    if agent is None:
        # Soft-pass: a missing row is not a permission failure. Use the
        # platform default trust so the gate matches what the agent would
        # receive on its first write (when get_or_create_agent runs).
        if min_level <= DEFAULT_TRUST_LEVEL:
            return DEFAULT_TRUST_LEVEL, True, None
        # Distinguish "no row" from a real ``trust_level`` denial so an
        # operator debugging the 403 doesn't go looking for a row to
        # upgrade that doesn't exist. The ``register the agent by
        # writing one memory first`` hint matches the recovery path
        # used elsewhere in the system (writes auto-create via
        # ``get_or_create_agent``).
        return (
            DEFAULT_TRUST_LEVEL,
            True,
            f"{_MCP_ERROR_PREFIX}Agent '{agent_id}' is not registered "
            f"(no row; effective trust {DEFAULT_TRUST_LEVEL} < required {min_level}). "
            f"Register the agent by writing one memory first.",
        )
    trust = agent.get("trust_level", 0) if isinstance(agent, dict) else getattr(agent, "trust_level", 0)
    if trust < min_level:
        return (
            trust,
            False,
            f"{_MCP_ERROR_PREFIX}Agent '{agent_id}' (trust_level={trust}) < required {min_level}.",
        )
    return trust, False, None


def parse_trust_error(terr: str) -> str:
    """Strip the ``_MCP_ERROR_PREFIX`` from an error produced by ``require_trust``.

    REST routes use this to surface the underlying reason as the detail
    of an ``HTTPException`` without leaking the MCP-style prefix. Uses
    ``str.removeprefix`` which is a no-op when the prefix isn't present —
    keeps the helper safe against future callers that feed in arbitrary
    error strings.
    """
    return terr.removeprefix(_MCP_ERROR_PREFIX)
