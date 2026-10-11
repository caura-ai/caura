"""The per-tenant lifecycle automation switch is honoured (M-115).

``lifecycle.lifecycle_automation_enabled`` is declared in the settings, type
checked on PUT, exposed as ``ResolvedConfig.lifecycle_automation_enabled``, and
rendered by the dashboard as "Auto-transition expired and stale memories". The
integration guide says lifecycle automation is togglable per tenant by it. But
nothing read it: the nightly fanout picked every active tenant for
archive-expired and archive-stale, so a tenant that turned it off still had its
expired rows flipped to ``outdated`` and its stale rows archived.

The fanout now skips those two actions for a tenant with the switch off and
reports how many it skipped. A tenant whose settings cannot be read is counted
as failed, not run. Purge is retention, not a transition, and the pipeline ops
have their own switches, so neither reads this one.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from core_api.routes import lifecycle
from core_api.services.organization_settings import ResolvedConfig

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]


async def _fanout(action: str, switch_by_org: dict) -> tuple[dict, set]:
    """Run the fanout over ``switch_by_org``: org -> True, False, None (unset),
    or an exception its settings read raises. Returns (response, triggered orgs).
    """
    triggered: set[str] = set()

    async def trigger(**kwargs) -> int:
        triggered.add(kwargs["org_id"])
        return 1

    async def config_for(org_id: str) -> ResolvedConfig:
        value = switch_by_org[org_id]
        if isinstance(value, BaseException):
            raise value
        if value is None:
            return ResolvedConfig({})
        return ResolvedConfig({"lifecycle": {"lifecycle_automation_enabled": value}})

    orgs = list(switch_by_org)
    cache = getattr(lifecycle, "_FANOUT_SEMAPHORES", None)
    if cache is not None:
        cache.clear()
    try:
        with (
            patch.object(lifecycle, "_trigger_one", trigger),
            patch.object(
                lifecycle, "_list_tenants_for_action", AsyncMock(return_value=orgs)
            ),
            patch.object(
                lifecycle, "resolve_publisher_kwargs", AsyncMock(return_value=None)
            ),
            patch.object(
                lifecycle,
                "resolve_config",
                AsyncMock(side_effect=config_for),
                create=True,
            ),
        ):
            # Called directly, so the ``Query(...)`` default would arrive as
            # itself, not as ``None``, and 422 every non-pipeline action.
            out = await lifecycle.fanout_lifecycle_action(
                action, auth=MagicMock(), dedup_window_hours=None
            )
    finally:
        if cache is not None:
            cache.clear()
    return out, triggered


@pytest.mark.parametrize("action", ["archive-expired", "archive-stale"])
async def test_a_tenant_that_turned_lifecycle_automation_off_is_skipped(action):
    out, triggered = await _fanout(action, {"on": True, "off": False, "unset": None})
    assert triggered == {"on", "unset"}
    assert (out["published"], out["failed"], out.get("skipped")) == (2, 0, 1)


async def test_a_tenant_whose_settings_cannot_be_read_is_not_archived():
    out, triggered = await _fanout(
        "archive-expired", {"on": True, "unreadable": RuntimeError("storage down")}
    )
    assert triggered == {"on"}
    assert (out["published"], out["failed"]) == (1, 1)


@pytest.mark.parametrize("action", ["purge-soft-deleted", "crystallize"])
async def test_actions_the_switch_does_not_name_still_run(action):
    """Guard: purge is retention, and the pipeline ops have their own switches."""
    out, triggered = await _fanout(action, {"off": False})
    assert triggered == {"off"}
    assert out["published"] == 1
