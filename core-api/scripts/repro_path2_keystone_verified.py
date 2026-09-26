"""Measure the Path-2 keystone privilege inversion described in
``docs/plans/rest-mcp-agent-identity-asymmetry.md`` section 2.

Two byte-identical requests, differing only in which credential they carry,
are resolved through the real ``_resolve_auth_context`` and then through the
real ``keystones._resolve_caller_identity`` / ``_effective_min_for_caller``.

The shared ``CAURA_API_KEY`` holder comes out at trust floor 1; the admin-key
holder, making the same claim about the same victim, comes out at 2. The floor
bump exists to stop exactly this claim, and it fires only for the *stronger*
credential.

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

SHARED_KEY = "shared-caura-key"
ADMIN_KEY = "admin-key"
VICTIM = "victim-agent"


class _Req:
    """The two attributes ``_resolve_auth_context`` touches."""

    def __init__(self, headers: dict[str, str]) -> None:
        self.headers = headers
        self.state = types.SimpleNamespace()


async def _measure(key: str) -> tuple[object, str, bool, int]:
    req = _Req({"x-tenant-id": "t1", "x-agent-id": VICTIM})
    ctx = await _resolve_auth_context(req, key)
    caller_agent_id, verified = _resolve_caller_identity(ctx, VICTIM)
    # 1 is the self-author floor: ``scope=agent`` naming the caller itself.
    return ctx.agent_id, caller_agent_id, verified, _effective_min_for_caller(1, verified)


async def main() -> None:
    async def _noop(*_args: object, **_kwargs: object) -> None:
        return None

    with (
        patch.object(auth_mod.settings, "memclaw_api_key", SHARED_KEY),
        patch.object(auth_mod.settings, "is_standalone", False),
        patch.object(auth_mod, "_block_if_suppressed", new=_noop),
        patch.object(auth_mod, "_block_if_any_readable_suppressed", new=_noop),
        patch.object(auth_mod, "get_admin_key", lambda: ADMIN_KEY),
        patch.object(auth_mod, "set_current_tenant", lambda *_: None),
    ):
        for label, key in (("Path 2 — shared CAURA_API_KEY", SHARED_KEY), ("Path 1 — admin key", ADMIN_KEY)):
            auth_agent, caller, verified, floor = await _measure(key)
            print(f"{label}:")
            print(f"    AuthContext.agent_id   = {auth_agent!r}")
            print(f"    caller_agent_id        = {caller!r}")
            print(f"    caller_verified        = {verified}")
            print(f"    effective trust floor  = {floor}")


if __name__ == "__main__":
    asyncio.run(main())
