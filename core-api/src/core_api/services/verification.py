"""Evidence contract v1, EC-14: the verify operation.

A person, an admin credential or an agent at ``trust_level >= 3`` marks a
memory ``human_verified``, or withdraws that. Reached over REST
(``POST /memories/{id}/verify`` and ``/unverify``) and MCP
(``caura_manage`` ops ``verify`` / ``unverify``); each surface authorizes the
caller its own way and then calls :func:`change_verification`, which writes the
row and the audit entry. See ``docs/plans/evidence-contract-v1.md``.

What unverify leaves behind is decided by storage in the same UPDATE
(``postgres_service._verification_writes``): ``disputed`` stays ``disputed``,
anything else returns to what the row's own evidence says (``tool_observed`` or
``unverified``).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from core_api.clients.storage_client import get_storage_client
from core_api.services.audit_service import log_action

VERIFY_ACTION = "memory_verify"
UNVERIFY_ACTION = "memory_unverify"


def verifier_id(*, agent_id: str | None, user_id: str | None, credential_kind: str) -> str:
    """Who ``verified_by`` names: ``agent:<id>``, a person's user id, else the credential kind."""
    if agent_id:
        return f"agent:{agent_id}"
    return user_id or credential_kind


async def change_verification(
    *,
    tenant_id: str,
    memory: dict,
    verify: bool,
    verified_by: str,
    audit_agent_id: str | None,
    audit_detail: dict[str, Any],
) -> dict[str, Any] | None:
    """Verify (or unverify) ``memory``, an authorized caller's live row, and audit it.

    Returns the row's verification fields after the write, or None when the
    row went away in between (the caller answers 404).
    """
    sc = get_storage_client()
    memory_id = str(memory["id"])
    if verify:
        patch: dict[str, Any] = {
            "verification_status": "human_verified",
            "verified_by": verified_by,
            "verified_at": datetime.now(UTC).isoformat(),
        }
    else:
        patch = {"verified_by": None, "verified_at": None}
    if await sc.update_memory(memory_id, tenant_id, patch) is None:
        return None
    after = await sc.get_memory(memory_id, tenant_id) or {}
    old_status = memory.get("verification_status") or "unverified"
    result = {
        "memory_id": memory_id,
        "old_verification_status": old_status,
        "verification_status": after.get("verification_status"),
        "verified_by": after.get("verified_by"),
        "verified_at": after.get("verified_at"),
    }
    await log_action(
        tenant_id=tenant_id,
        agent_id=audit_agent_id,
        action=VERIFY_ACTION if verify else UNVERIFY_ACTION,
        resource_type="memory",
        resource_id=memory_id,
        detail={
            "old_verification_status": old_status,
            "new_verification_status": result["verification_status"],
            "actor": verified_by,
            "owner_agent_id": memory.get("agent_id"),
            **audit_detail,
        },
    )
    return result
