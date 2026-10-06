"""``agent_digest.cadence`` decides which digest runs an org gets (M-113).

``DEFAULT_SETTINGS`` declares ``agent_digest.cadence: daily | weekly | both`` and
PUT /settings accepts it, but nothing read it. core-operations fires a ``day``
and a ``week`` run unconditionally, and ``run_agent_digest`` enumerated orgs by
``enabled`` alone, so every opted-in org got, and paid for, both: an org that set
``weekly`` to cut LLM spend still paid for a daily run every day.

A run now covers only the orgs whose cadence includes its period, and reports
how many it left out. PUT rejects a value outside the three. One stored before
that check runs on the declared default, ``daily``.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

import core_api.services.tenants as tenants_mod
from core_api.services import agent_digest
from core_api.services import organization_settings as os_svc

pytestmark = pytest.mark.asyncio

#: org -> its stored cadence; ``None`` leaves the key unset.
_ORGS = {"daily": "daily", "weekly": "weekly", "both": "both", "unset": None}


async def _run(monkeypatch, period: str, cadence_by_org: dict) -> tuple[dict, set]:
    ran: set[str] = set()

    async def list_orgs() -> list[str]:
        return list(cadence_by_org)

    async def settings(org_id: str) -> dict:
        config: dict = {"enabled": True}
        if cadence_by_org[org_id] is not None:
            config["cadence"] = cadence_by_org[org_id]
        return {"agent_digest": config}

    async def generate(org_id, period, config, *, now) -> dict:
        ran.add(org_id)
        return {"generated": 1}

    monkeypatch.setattr(tenants_mod, "list_tenants_with_agent_digest_enabled", list_orgs)
    monkeypatch.setattr(agent_digest, "get_settings_for_display", settings)
    monkeypatch.setattr(agent_digest, "generate_for_org", generate)
    return await agent_digest.run_agent_digest(period), ran


async def test_a_daily_run_covers_daily_both_and_the_default(monkeypatch):
    summary, ran = await _run(monkeypatch, "day", _ORGS)
    assert ran == {"daily", "both", "unset"}
    assert (summary["completed"], summary["off_cadence"]) == (3, 1)


async def test_a_weekly_run_covers_weekly_and_both(monkeypatch):
    summary, ran = await _run(monkeypatch, "week", _ORGS)
    assert ran == {"weekly", "both"}
    assert (summary["completed"], summary["off_cadence"]) == (2, 2)


async def test_an_unknown_stored_cadence_runs_on_the_default(monkeypatch):
    _, day = await _run(monkeypatch, "day", {"legacy": "hourly"})
    _, week = await _run(monkeypatch, "week", {"legacy": "hourly"})
    assert (day, week) == ({"legacy"}, set())


async def test_a_stored_cadence_that_is_not_a_string_runs_on_the_default(monkeypatch):
    """Review round 1: stored before PUT checked it, a list is unhashable, so the
    lookup raised and the org was counted failed instead of running daily."""
    summary, ran = await _run(monkeypatch, "day", {"legacy": ["weekly"]})
    assert ran == {"legacy"}
    assert summary["failed"] == 0


def _settings_storage(monkeypatch) -> AsyncMock:
    sc = MagicMock()
    sc.update_org_settings = AsyncMock(return_value={"settings": {}, "changed": False})
    monkeypatch.setattr(os_svc, "get_storage_client", lambda: sc)
    return sc.update_org_settings


@pytest.mark.parametrize(
    "cadence", ["hourly", "Daily", "", {}, ["daily"]], ids=["hourly", "Daily", "empty", "dict", "list"]
)
async def test_settings_reject_an_unknown_cadence(monkeypatch, cadence):
    """A dict passes the leaf-type check, which recurses into dicts, so it reached the
    membership test unhashable and raised ``TypeError``, a 500 (review round 1)."""
    write = _settings_storage(monkeypatch)
    with pytest.raises(ValueError, match=r"agent_digest\.cadence"):
        await os_svc.update_settings("t1", {"agent_digest": {"cadence": cadence}})
    write.assert_not_awaited()


@pytest.mark.parametrize("cadence", ["daily", "weekly", "both", None])
async def test_settings_accept_each_cadence_and_a_reset(monkeypatch, cadence):
    """Guard: the three documented values, and ``None``, which resets to the default."""
    write = _settings_storage(monkeypatch)
    await os_svc.update_settings("t1", {"agent_digest": {"cadence": cadence}})
    write.assert_awaited_once()
