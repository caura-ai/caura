"""Shared caller-identity resolution for the outcome/insight REST routes.

``routes/evolve.py`` and ``routes/insights.py`` resolved the calling agent with
byte-identical, copy-pasted logic — verified gateway identity > ``body.agent_id``
> the reserved ``DEFAULT_AGENT_ID`` — then gated the write on trust. This is the
single source for that logic so the two routes can't drift (the standalone-operator
bypass below was previously only on keystones, leaving evolve/insights to 403 the
bare standalone operator).

NOTE: ``routes/keystones.py`` deliberately does NOT use this. Governance writes
must not fall back to a *registerable* identity — its ``rest-admin`` sentinel has
no agent row so the fallback always 403s on the gateway, whereas ``DEFAULT_AGENT_ID``
auto-registers — and keystones has stricter anti-spoof handling (X-Agent-ID
mismatch rejection, verified-floor bump). Keep that path separate.
"""

from __future__ import annotations

import logging

from fastapi import HTTPException

from core_api.agent_ids import DEFAULT_AGENT_ID
from core_api.auth import AuthContext
from core_api.config import settings as app_settings
from core_api.services.agent_service import broker_owned_agent_id
from core_api.services.trust_service import parse_trust_error, require_trust

logger = logging.getLogger(__name__)


async def resolve_caller_and_gate(
    auth: AuthContext,
    *,
    tenant_id: str,
    body_agent_id: str | None,
    scope: str,
    action: str,
) -> str:
    """Resolve the caller's ``agent_id`` and gate the write on trust.

    Precedence: gateway-verified ``auth.agent_id`` > ``body_agent_id`` >
    ``DEFAULT_AGENT_ID`` (so standalone callers still show up in audit logs).
    Returns the resolved ``caller_agent_id``; raises 403 when the resolved agent
    isn't registered or lacks the required trust (``min_level`` is 1 for
    ``scope=agent``, else 2).

    For an install-credential (broker) caller the resolved id first passes
    through the broker ownership gate (``broker_owned_agent_id``): a foreign or
    reserved ``broker:`` id degrades to the caller's own ``broker:<install>``
    fallback BEFORE the trust check, so a broker can't attribute an outcome /
    insight memory to an agent owned by another install.

    The gate is skipped for (a) admins — tenant-free system callers — and (b)
    the unidentified standalone operator: ``IS_STANDALONE`` with no asserted
    identity is the single-tenant local admin (mirrors
    ``keystones._is_standalone_admin`` and ``routes/memories``). ``action``
    (``"evolve"`` / ``"insights"``) only labels the mismatch-warning log line.
    """
    if auth.agent_id and body_agent_id is not None and auth.agent_id != body_agent_id:
        # Warn only on an explicit body.agent_id that disagrees with the
        # verified header; the Pydantic default would fire on every gateway call.
        logger.warning(
            "%s: verified agent_id=%s overrides body.agent_id=%s",
            action,
            auth.agent_id,
            body_agent_id,
        )
    caller_agent_id = auth.agent_id or body_agent_id or DEFAULT_AGENT_ID

    if auth.is_admin:
        return caller_agent_id
    if app_settings.is_standalone and not auth.agent_id and not body_agent_id:
        return caller_agent_id

    # Broker ownership boundary: an install-credential caller may only attribute
    # a write to an agent it owns. Degrade a foreign / reserved-namespace agent
    # id to this install's own ``broker:<install>`` fallback BEFORE the trust
    # gate, so trust is evaluated on the identity actually written (parity with
    # the data-plane ``resolve_write_agent``). Non-broker callers are unaffected.
    # No owner stamp here: evolve/insights never first-touch an agent (require_trust
    # 403s an unregistered id), so this only redirects attribution, it doesn't
    # create/claim rows.
    if auth.is_install_credential:
        caller_agent_id = await broker_owned_agent_id(caller_agent_id, auth.install_uuid, tenant_id)

    min_level = 1 if scope == "agent" else 2
    # Write paths must pin identity: outcome/insight memories + an audit-log row
    # are keyed to caller_agent_id, so an unregistered name corrupts the audit
    # trail. require_trust soft-passes a missing row (read ergonomics), so
    # re-block not_found explicitly here.
    _, not_found, terr = await require_trust(tenant_id, caller_agent_id, min_level=min_level)
    if not_found:
        raise HTTPException(
            status_code=403,
            detail=f"Agent '{caller_agent_id}' is not registered in tenant '{tenant_id}'.",
        )
    if terr:
        raise HTTPException(status_code=403, detail=parse_trust_error(terr))
    return caller_agent_id
