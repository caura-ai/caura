"""Shared agent-id constants.

A tiny, dependency-free module so both the MCP server (``mcp_server``) and the
REST routes (``routes.memories``) can reference the reserved default identity
without importing each other — avoids the heavy/cross-private import that would
otherwise couple the route module to the whole MCP tool surface.
"""

from typing import NewType

# A caller identity that has been through authentication or resolution — not
# merely a string that happens to hold an agent id. The distinction is
# load-bearing rather than cosmetic: ``memory_access_allowed_for_agent`` decides
# ``scope_agent`` access with ``owner_agent_id == caller_agent_id``, so a stored
# row's own ``agent_id`` passed as the CALLER makes that comparison trivially
# true and self-authorizes every access. Before this type both parameters were
# ``str``, so nothing but review stopped it.
#
# Only the resolvers construct it, and ``tests/test_agent_identity_construction.py``
# pins that list — a raw string cannot become an identity anywhere else. An
# ``owner_agent_id`` read off a row deliberately stays ``str``: it is data, not
# an identity, and giving the two the same type is what this prevents.
#
# NOT for audit attribution. ``AuthContext.effective_agent_id``'s docstring
# draws that line and ``test_the_audit_attribution_is_not_bound_by_the_helper``
# pins it: an attribution must record what the request carried even when
# authorization ignored it, which is a different value from the identity
# authorization used.
AgentIdentity = NewType("AgentIdentity", str)

# Reserved fallback identity used when a caller omits ``agent_id`` on the
# single-tenant / standalone path. On the enterprise gateway this value is
# explicitly refused (see ``mcp_server._refuse_default_agent_on_gateway``) so
# anonymous writes are never silently attributed to one shared identity.
DEFAULT_AGENT_ID = "mcp-agent"

# Dedicated system identity for the automated nightly insights run
# (``lifecycle_audit._CoreApiLifecycleAdapter.insights`` → ``generate_insights``).
# Distinct from ``DEFAULT_AGENT_ID`` above: that is the generic "caller specified
# nothing" fallback, whereas the scheduled insights pass has no human/agent
# caller and must write under a stable, registerable identity rather than
# collapsing onto the anonymous default (which is unregistered, so its writes
# never surface in Prism or the per-agent report). Registered per-tenant with
# ``belonging_type='service'`` the first time the job runs for that tenant.
INSIGHTER_AGENT_ID = "memclaw-insighter"  # legacy-name-floor: floor

# Trust tier the insighter self-registers at: service level, granting cross-fleet
# read (>=2, needed for scope='all') and write (>=3, it persists scope_org
# insights). The automated path calls the service directly and bypasses the
# route trust gate, so this is future-proofing for any gated re-route.
INSIGHTER_TRUST_LEVEL = 3

# Fallback identity for the memory minted from a document write, used ONLY when
# the caller carries no agent identity at all (the REST ``POST /documents`` path
# without a gateway-stamped ``X-Agent-ID``; the MCP path always has one). Same
# reasoning as ``INSIGHTER_AGENT_ID``: a stable registerable service identity
# rather than the anonymous default, and self-registered per tenant on first use
# via ``get_or_create_agent``.
#
# Prefer the real doc writer's agent_id whenever there is one. Attribution is not
# cosmetic here: ``caura_insights`` defaults to ``scope="agent"``, which filters
# ``Memory.agent_id == agent_id``, so rows attributed to this service identity are
# invisible to every real agent's default insights run.
DOC_INDEXER_AGENT_ID = "memclaw-doc-indexer"  # legacy-name-floor: floor

# Bare ``"main"`` is the OpenClaw plugin's *unset* default agent_id: when an
# operator never sets ``CAURA_AGENT_ID`` every install collapses onto this one
# shared identity (the eToro "firehose"). Unlike ``DEFAULT_AGENT_ID``
# ("mcp-agent", legitimate on the standalone path), bare ``"main"`` is never a
# valid *persisted* write identity and is refused on every write path.
RESERVED_MAIN_AGENT_ID = "main"

# Ids rejected on EVERY write path regardless of context (never legitimate,
# including standalone), enforced by the write-path guard
# (``services.agent_identity``). ``DEFAULT_AGENT_ID`` ("mcp-agent") is
# intentionally excluded — standalone uses it and it is guarded *conditionally*
# by ``mcp_server._refuse_default_agent_on_gateway`` instead.
ALWAYS_RESERVED_AGENT_IDS = frozenset({RESERVED_MAIN_AGENT_ID})

# Ids that must NOT be accepted as a *body-supplied* (client self-declared)
# write identity: the unset plugin default ("main") and the MCP tool-param
# default ("mcp-agent", which means "caller specified nothing"). Gates the BODY
# only — see ``effective_write_agent_id``. The VERIFIED id is checked against
# ``ALWAYS_RESERVED_AGENT_IDS`` ({main}) so a real (if poorly-named)
# ``home_agent_id="mcp-agent"`` credential still wins and cannot be overridden
# by the body (closing the spoof vector that treating verified "mcp-agent" as a
# placeholder would open).
_PLACEHOLDER_BODY_AGENT_IDS = frozenset({RESERVED_MAIN_AGENT_ID, DEFAULT_AGENT_ID})


def effective_write_agent_id(verified_id: str | None, body_id: str | None) -> AgentIdentity | None:
    """Resolve the agent_id a WRITE is attributed to from the verified
    credential identity (gateway ``X-Agent-ID`` = ``home_agent_id``) and the
    client-supplied body id.

    Rule: the verified id wins — **unless** it is the reserved ``main``
    placeholder (a misconfigured ``home_agent_id="main"`` cred), in which case a
    *non-default* body id is honored so the install can self-identify instead of
    collapsing onto "main". A verified ``mcp-agent`` home is a real (if
    poorly-named) identity and still wins — it is NOT overridable by the body. If
    no real identity is available, the reserved id falls through and the
    write-path guard (policy=reject) refuses it.

    Asymmetric on purpose: the *verified* check uses ``ALWAYS_RESERVED_AGENT_IDS``
    ({main}) so a real verified id (incl. "mcp-agent") can't be body-spoofed; the
    *body* check uses ``_PLACEHOLDER_BODY_AGENT_IDS`` ({main, mcp-agent}) so a
    defaulted body can't become the identity. Strictly *tighter* than the REST
    write path, which already trusts the body unconditionally.
    """
    if verified_id and verified_id not in ALWAYS_RESERVED_AGENT_IDS:
        return AgentIdentity(verified_id)
    if body_id and body_id not in _PLACEHOLDER_BODY_AGENT_IDS:
        return AgentIdentity(body_id)
    # Reserved/placeholder fallthrough: still the resolved identity, and the
    # write-path guard (policy=reject) is what refuses it downstream.
    reserved = verified_id or body_id
    # ``is not None`` so this stays a passthrough of the original
    # ``return verified_id or body_id`` — truthiness here would turn an
    # empty-string id into None and change what the write-path guard sees.
    return AgentIdentity(reserved) if reserved is not None else None


def effective_read_agent_id(verified_id: str | None, asserted_id: str) -> AgentIdentity:
    """Resolve the identity a READ is authorized as, verified-id-first.

    The read-side sibling of :func:`effective_write_agent_id`, and far simpler:
    a read has no attribution to protect, so there is no reserved-placeholder
    asymmetry — the gateway-verified id wins and the caller's asserted id is the
    fallback. Total, because every MCP read tool defaults ``agent_id`` to
    ``DEFAULT_AGENT_ID`` rather than accepting None.

    Exists so the read path has ONE place that turns two strings into an
    ``AgentIdentity``. It was written as ``_get_agent_id() or agent_id`` inline
    at eight MCP call sites; the one that reaches ``enforce_fleet_read_many``
    now routes through here instead. The remaining seven feed no authorization
    sink and are deliberately left alone rather than swept.
    """
    return AgentIdentity(verified_id or asserted_id)
