"""Show the two credentials agreeing on the keystone trust floor — the
oss-0922-m-03 inversion, and its fix.

Two byte-identical requests, differing only in which credential they carry,
are resolved through the real ``_resolve_auth_context`` and then through the
real ``keystones._resolve_caller_identity`` / ``_effective_min_for_caller``.

**Pre-fix** the shared ``CAURA_API_KEY`` holder came out at trust floor 1 while
the admin-key holder, making the same claim about the same victim, came out at
2 — the floor bump firing only for the *stronger* credential, because
``_resolve_caller_identity`` read the PRESENCE of ``auth.agent_id`` and Path 2
builds that attribute from the caller's own ``X-Agent-ID``.

**Now** both print 2. ``AuthContext.agent_id_verified`` records provenance, and
only the gateway path (Path 4) sets it, so a self-asserted header no longer
buys the verified tier.

This script is the diagnostic, not the guard. It cannot see the defect on its
own — it hands the helper a context it built itself, which is exactly the step
that hid the problem in the first place. The regression test drives the real
route with the real credential:
``tests/test_keystone_identity_provenance.py``.

Run:

    PYTHONPATH=core-api/src:core-storage-api/src:. \\
        python core-api/scripts/repro_path2_keystone_verified.py

Nothing is written and no network call is made — the two suppression checks and
the tenant-context setter are stubbed because they are the only I/O on the path.
"""

from __future__ import annotations

import asyncio
import types
from unittest.mock import patch

from core_api import auth as auth_mod
from core_api.auth import _resolve_auth_context
from core_api.routes.keystones import _effective_min_for_caller, _resolve_caller_identity

# The settings attribute still carries the pre-rename name, so patch.object
# needs it spelled exactly. Hoisted to a constant because ruff format wraps
# the call and carries a trailing marker onto the closing paren, which the
# legacy-name ratchet then reads as the marker being deleted.
_SHARED_KEY_SETTING = "memclaw_api_key"  # legacy-name-floor: real settings attribute name

SHARED_KEY = "shared-caura-key"
ADMIN_KEY = "admin-key"
VICTIM = "victim-agent"


class _Req:
    """The two attributes ``_resolve_auth_context`` touches."""

    def __init__(self, headers: dict[str, str]) -> None:
        self.headers = headers
        self.state = types.SimpleNamespace()


async def _measure(key: str) -> tuple[object, bool, str, bool, int]:
    req = _Req({"x-tenant-id": "t1", "x-agent-id": VICTIM})
    ctx = await _resolve_auth_context(req, key)
    caller_agent_id, verified = _resolve_caller_identity(ctx, VICTIM)
    # 1 is the self-author floor: ``scope=agent`` naming the caller itself.
    return (
        ctx.agent_id,
        ctx.agent_id_verified,
        caller_agent_id,
        verified,
        _effective_min_for_caller(1, verified),
    )


async def main() -> None:
    async def _noop(*_args: object, **_kwargs: object) -> None:
        return None

    with (
        patch.object(auth_mod.settings, _SHARED_KEY_SETTING, SHARED_KEY),
        patch.object(auth_mod.settings, "is_standalone", False),
        patch.object(auth_mod, "_block_if_suppressed", new=_noop),
        patch.object(auth_mod, "_block_if_any_readable_suppressed", new=_noop),
        patch.object(auth_mod, "get_admin_key", lambda: ADMIN_KEY),
        patch.object(auth_mod, "set_current_tenant", lambda *_: None),
    ):
        floors = []
        for label, key in (("Path 2 — shared CAURA_API_KEY", SHARED_KEY), ("Path 1 — admin key", ADMIN_KEY)):
            auth_agent, provenance, caller, verified, floor = await _measure(key)
            floors.append(floor)
            print(f"{label}:")
            print(f"    AuthContext.agent_id          = {auth_agent!r}")
            print(f"    AuthContext.agent_id_verified = {provenance}")
            print(f"    caller_agent_id               = {caller!r}")
            print(f"    caller_verified               = {verified}")
            print(f"    effective trust floor         = {floor}")
        # The finding was the GAP, so the check is the equality, not a
        # constant: a retuned shared floor stays fine, a reopened gap does not.
        verdict = "agree" if len(set(floors)) == 1 else "DISAGREE — inversion is back"
        print(f"\nThe two credentials {verdict} (floors: {floors}).")


if __name__ == "__main__":
    asyncio.run(main())
