"""Auto-upgrade trigger in the heartbeat handler (CAURA-444).

Pure unit-tests for the helpers — no DB / storage roundtrip needed.
The heartbeat handler stitches them together; integration coverage
lives in the existing fleet route tests.
"""

from __future__ import annotations

import pytest

from core_api.routes import fleet as fleet_mod


# ---------------------------------------------------------------------------
# _semver_lt
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "a,b,expected",
    [
        ("2.3.0", "2.4.0", True),
        ("2.4.0", "2.3.0", False),
        ("2.4.0", "2.4.0", False),
        ("2.4", "2.4.1", True),    # zero-pad
        ("2.4.1", "2.4", False),
        ("1.10.0", "1.9.0", False),  # int compare, not lex
        ("1.9.0", "1.10.0", True),
        # Falsy / unparseable inputs never produce a "newer" verdict.
        ("", "2.4.0", False),
        ("2.4.0", "", False),
        (None, "2.4.0", False),
        ("2.4.0", None, False),
        ("dev", "2.4.0", False),
        ("not.a.version", "2.4.0", False),
    ],
)
def test_semver_lt(a, b, expected):
    assert fleet_mod._semver_lt(a, b) == expected


# ---------------------------------------------------------------------------
# Known-broken denylist (transition guard for v2.3.0)
# ---------------------------------------------------------------------------


def test_v2_3_0_is_in_known_broken_set():
    """The 2.3.0 → 2.4.0 transition is broken (drift-1 + drift-2 in
    the deploy machinery). The denylist must contain it; auto-upgrade
    on these nodes would loop. Operators must manually upgrade.
    """
    assert "2.3.0" in fleet_mod.KNOWN_BROKEN_DEPLOY_VERSIONS


def test_known_broken_set_is_frozen():
    """A frozenset means a runtime mistake (.add()) raises rather than
    silently breaking the safety net.
    """
    assert isinstance(fleet_mod.KNOWN_BROKEN_DEPLOY_VERSIONS, frozenset)


# ---------------------------------------------------------------------------
# _auto_upgrade_enabled_for_tenant
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_auto_upgrade_enabled_default_true(monkeypatch):
    """No tenant override → enabled (the global default)."""
    async def _fake(_db, _tid):
        return {}  # no override

    monkeypatch.setattr(
        "core_api.services.organization_settings.get_raw_settings", _fake
    )
    assert (
        await fleet_mod._auto_upgrade_enabled_for_tenant(None, "tenant-1")
        is True
    )


@pytest.mark.asyncio
async def test_auto_upgrade_enabled_override_false(monkeypatch):
    """Tenant override `memclaw.auto_upgrade_enabled = false` → disabled."""
    async def _fake(_db, _tid):
        return {"memclaw": {"auto_upgrade_enabled": False}}

    monkeypatch.setattr(
        "core_api.services.organization_settings.get_raw_settings", _fake
    )
    assert (
        await fleet_mod._auto_upgrade_enabled_for_tenant(None, "tenant-1")
        is False
    )


@pytest.mark.asyncio
async def test_auto_upgrade_enabled_override_true(monkeypatch):
    """Tenant override `memclaw.auto_upgrade_enabled = true` → enabled."""
    async def _fake(_db, _tid):
        return {"memclaw": {"auto_upgrade_enabled": True}}

    monkeypatch.setattr(
        "core_api.services.organization_settings.get_raw_settings", _fake
    )
    assert (
        await fleet_mod._auto_upgrade_enabled_for_tenant(None, "tenant-1")
        is True
    )


@pytest.mark.asyncio
async def test_auto_upgrade_fail_open_on_settings_error(monkeypatch):
    """If settings resolve raises, default to enabled (cooldown machinery
    on the plugin side prevents loops in the worst case).
    """
    async def _fake(_db, _tid):
        raise RuntimeError("settings backend down")

    monkeypatch.setattr(
        "core_api.services.organization_settings.get_raw_settings", _fake
    )
    assert (
        await fleet_mod._auto_upgrade_enabled_for_tenant(None, "tenant-1")
        is True
    )


# ---------------------------------------------------------------------------
# _maybe_queue_auto_upgrade
# ---------------------------------------------------------------------------


class _FakeStorage:
    """Captures create_command + get_pending_commands calls."""

    def __init__(self, pending=None):
        self.pending_commands = pending or []
        self.created_commands: list[dict] = []

    async def get_pending_commands(self, _tenant_id, _node_name):
        return list(self.pending_commands)

    async def create_command(self, data):
        self.created_commands.append(data)
        return {"id": "fake-cmd-1"}


def _body(plugin_version="2.3.0", deploy_blocked_until=None):
    """Mint a HeartbeatIn-shaped Pydantic model with overrides."""
    return fleet_mod.HeartbeatIn(
        tenant_id="tenant-1",
        node_name="node-a",
        plugin_version=plugin_version,
        deploy_blocked_until=deploy_blocked_until,
    )


@pytest.mark.asyncio
async def test_maybe_queue_auto_upgrade_skips_for_known_broken(monkeypatch):
    """Plugin v2.3.0 → no deploy command (loop-prevention denylist)."""
    monkeypatch.setattr(fleet_mod, "VERSION", "2.4.0")

    async def _enabled(_db, _tid):
        return True

    monkeypatch.setattr(fleet_mod, "_auto_upgrade_enabled_for_tenant", _enabled)
    sc = _FakeStorage()
    await fleet_mod._maybe_queue_auto_upgrade(db=None, sc=sc, body=_body("2.3.0"))
    assert sc.created_commands == []


@pytest.mark.asyncio
async def test_maybe_queue_auto_upgrade_skips_when_current(monkeypatch):
    """plugin_version == VERSION → no deploy."""
    monkeypatch.setattr(fleet_mod, "VERSION", "2.4.0")

    async def _enabled(_db, _tid):
        return True

    monkeypatch.setattr(fleet_mod, "_auto_upgrade_enabled_for_tenant", _enabled)
    sc = _FakeStorage()
    await fleet_mod._maybe_queue_auto_upgrade(db=None, sc=sc, body=_body("2.4.0"))
    assert sc.created_commands == []


@pytest.mark.asyncio
async def test_maybe_queue_auto_upgrade_skips_when_newer(monkeypatch):
    """plugin_version > VERSION (dev install) → no downgrade."""
    monkeypatch.setattr(fleet_mod, "VERSION", "2.4.0")

    async def _enabled(_db, _tid):
        return True

    monkeypatch.setattr(fleet_mod, "_auto_upgrade_enabled_for_tenant", _enabled)
    sc = _FakeStorage()
    await fleet_mod._maybe_queue_auto_upgrade(db=None, sc=sc, body=_body("2.5.0-dev"))
    assert sc.created_commands == []


@pytest.mark.asyncio
async def test_maybe_queue_auto_upgrade_skips_when_blocked(monkeypatch):
    """deploy_blocked_until in the future → skip (cooldown signal)."""
    monkeypatch.setattr(fleet_mod, "VERSION", "2.4.0")

    async def _enabled(_db, _tid):
        return True

    monkeypatch.setattr(fleet_mod, "_auto_upgrade_enabled_for_tenant", _enabled)
    sc = _FakeStorage()
    future_ms = 99999999999999  # year 5138
    await fleet_mod._maybe_queue_auto_upgrade(
        db=None, sc=sc, body=_body("2.4.1", deploy_blocked_until=future_ms)
    )
    assert sc.created_commands == []


@pytest.mark.asyncio
async def test_maybe_queue_auto_upgrade_skips_when_disabled(monkeypatch):
    """tenant has auto_upgrade_enabled = false → skip."""
    monkeypatch.setattr(fleet_mod, "VERSION", "2.4.0")

    async def _disabled(_db, _tid):
        return False

    monkeypatch.setattr(fleet_mod, "_auto_upgrade_enabled_for_tenant", _disabled)
    sc = _FakeStorage()
    await fleet_mod._maybe_queue_auto_upgrade(db=None, sc=sc, body=_body("2.3.5"))
    assert sc.created_commands == []


@pytest.mark.asyncio
async def test_maybe_queue_auto_upgrade_skips_when_already_pending(monkeypatch):
    """An existing pending deploy → skip (don't double-queue)."""
    monkeypatch.setattr(fleet_mod, "VERSION", "2.4.0")

    async def _enabled(_db, _tid):
        return True

    monkeypatch.setattr(fleet_mod, "_auto_upgrade_enabled_for_tenant", _enabled)
    sc = _FakeStorage(pending=[{"command": "deploy", "payload": {}}])
    await fleet_mod._maybe_queue_auto_upgrade(db=None, sc=sc, body=_body("2.3.5"))
    assert sc.created_commands == []


@pytest.mark.asyncio
async def test_maybe_queue_auto_upgrade_queues_deploy_for_old_version(monkeypatch):
    """Happy path: enabled, not blocked, no pending, valid old version → queue."""
    monkeypatch.setattr(fleet_mod, "VERSION", "2.4.0")

    async def _enabled(_db, _tid):
        return True

    monkeypatch.setattr(fleet_mod, "_auto_upgrade_enabled_for_tenant", _enabled)
    sc = _FakeStorage()
    await fleet_mod._maybe_queue_auto_upgrade(db=None, sc=sc, body=_body("2.3.5"))
    assert len(sc.created_commands) == 1
    cmd = sc.created_commands[0]
    assert cmd["command"] == "deploy"
    assert cmd["payload"]["target_version"] == "2.4.0"
    assert cmd["tenant_id"] == "tenant-1"
    assert cmd["node_name"] == "node-a"


@pytest.mark.asyncio
async def test_maybe_queue_auto_upgrade_skips_when_plugin_version_missing(monkeypatch):
    """No plugin_version on payload (very old plugin) → skip."""
    monkeypatch.setattr(fleet_mod, "VERSION", "2.4.0")

    async def _enabled(_db, _tid):
        return True

    monkeypatch.setattr(fleet_mod, "_auto_upgrade_enabled_for_tenant", _enabled)
    sc = _FakeStorage()
    await fleet_mod._maybe_queue_auto_upgrade(
        db=None,
        sc=sc,
        body=fleet_mod.HeartbeatIn(tenant_id="tenant-1", node_name="node-a"),
    )
    assert sc.created_commands == []
