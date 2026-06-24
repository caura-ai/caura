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
        ("2.4", "2.4.1", True),  # zero-pad
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
        # Pure-separator strings with no digits filter to an empty list
        # via `[int(x) for x in s.split(".") if x]`. Without the
        # empty-list guard they'd zero-pad to [0,0,0] and falsely
        # compare as older than any real version, triggering a spurious
        # auto-upgrade. Locked here.
        ("...", "2.4.0", False),
        (".", "2.4.0", False),
        ("....", "2.4.0", False),
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

    async def _fake(_tid):
        return {}  # no override

    monkeypatch.setattr("core_api.routes.fleet.get_raw_settings", _fake)
    assert await fleet_mod._auto_upgrade_enabled_for_tenant("tenant-1") is True


@pytest.mark.asyncio
async def test_auto_upgrade_enabled_override_false(monkeypatch):
    """Tenant override `memclaw.auto_upgrade_enabled = false` → disabled."""

    async def _fake(_tid):
        return {"memclaw": {"auto_upgrade_enabled": False}}

    monkeypatch.setattr("core_api.routes.fleet.get_raw_settings", _fake)
    assert await fleet_mod._auto_upgrade_enabled_for_tenant("tenant-1") is False


@pytest.mark.asyncio
async def test_auto_upgrade_enabled_override_true(monkeypatch):
    """Tenant override `memclaw.auto_upgrade_enabled = true` → enabled."""

    async def _fake(_tid):
        return {"memclaw": {"auto_upgrade_enabled": True}}

    monkeypatch.setattr("core_api.routes.fleet.get_raw_settings", _fake)
    assert await fleet_mod._auto_upgrade_enabled_for_tenant("tenant-1") is True


@pytest.mark.asyncio
async def test_auto_upgrade_fail_open_on_settings_error(monkeypatch):
    """If settings resolve raises, default to enabled (cooldown machinery
    on the plugin side prevents loops in the worst case).
    """

    async def _fake(_tid):
        raise RuntimeError("settings backend down")

    monkeypatch.setattr("core_api.routes.fleet.get_raw_settings", _fake)
    assert await fleet_mod._auto_upgrade_enabled_for_tenant("tenant-1") is True


# ---------------------------------------------------------------------------
# _maybe_queue_auto_upgrade
# ---------------------------------------------------------------------------


class _FakeStorage:
    """Captures create_command + get_pending_commands calls.

    The auto-upgrade gate now reaches the in-flight / attempt-budget checks
    through the storage client (``sc.fleet_in_flight_deploy`` /
    ``sc.fleet_deploy_attempt_count``) instead of ``fleet_repo``. Tests drive
    those branches by assigning async callables to ``in_flight_fn`` /
    ``count_fn`` (signature ``(*, node_id, since[, target_version])``).
    Defaults: no deploy in flight, zero recent attempts.
    """

    def __init__(self, pending=None):
        self.pending_commands = pending or []
        self.created_commands: list[dict] = []
        self.in_flight_fn = None
        self.count_fn = None

    async def get_pending_commands(self, _tenant_id, _node_name):
        return list(self.pending_commands)

    async def create_command(self, data):
        self.created_commands.append(data)
        return {"id": "fake-cmd-1"}

    async def fleet_in_flight_deploy(self, *, node_id, since):
        if self.in_flight_fn is not None:
            return await self.in_flight_fn(node_id=node_id, since=since)
        return False

    async def fleet_deploy_attempt_count(self, *, node_id, target_version, since):
        if self.count_fn is not None:
            return await self.count_fn(
                node_id=node_id, target_version=target_version, since=since
            )
        return 0


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
    monkeypatch.setattr(fleet_mod, "MIN_RECOMMENDED_PLUGIN_VERSION", "2.4.0")

    async def _enabled(_tid):
        return True

    monkeypatch.setattr(fleet_mod, "_auto_upgrade_enabled_for_tenant", _enabled)
    sc = _FakeStorage()
    await fleet_mod._maybe_queue_auto_upgrade(        sc=sc,
        body=_body("2.3.0"),
        pending_commands=sc.pending_commands,
        node_id="node-uuid-1",
    )
    assert sc.created_commands == []


@pytest.mark.asyncio
async def test_maybe_queue_auto_upgrade_skips_when_current(monkeypatch):
    """plugin_version == VERSION → no deploy."""
    monkeypatch.setattr(fleet_mod, "MIN_RECOMMENDED_PLUGIN_VERSION", "2.4.0")

    async def _enabled(_tid):
        return True

    monkeypatch.setattr(fleet_mod, "_auto_upgrade_enabled_for_tenant", _enabled)
    sc = _FakeStorage()
    await fleet_mod._maybe_queue_auto_upgrade(        sc=sc,
        body=_body("2.4.0"),
        pending_commands=sc.pending_commands,
        node_id="node-uuid-1",
    )
    assert sc.created_commands == []


@pytest.mark.asyncio
async def test_maybe_queue_auto_upgrade_skips_when_newer(monkeypatch):
    """plugin_version > VERSION (dev install) → no downgrade."""
    monkeypatch.setattr(fleet_mod, "MIN_RECOMMENDED_PLUGIN_VERSION", "2.4.0")

    async def _enabled(_tid):
        return True

    monkeypatch.setattr(fleet_mod, "_auto_upgrade_enabled_for_tenant", _enabled)
    sc = _FakeStorage()
    await fleet_mod._maybe_queue_auto_upgrade(        sc=sc,
        body=_body("2.5.0-dev"),
        pending_commands=sc.pending_commands,
        node_id="node-uuid-1",
    )
    assert sc.created_commands == []


@pytest.mark.asyncio
async def test_maybe_queue_auto_upgrade_skips_when_blocked(monkeypatch):
    """deploy_blocked_until in the future → skip (cooldown signal)."""
    monkeypatch.setattr(fleet_mod, "MIN_RECOMMENDED_PLUGIN_VERSION", "2.4.0")

    async def _enabled(_tid):
        return True

    monkeypatch.setattr(fleet_mod, "_auto_upgrade_enabled_for_tenant", _enabled)
    sc = _FakeStorage()
    # 1 hour in the future — well within the 7-day MAX_BLOCK_MS cap.
    from datetime import UTC, datetime

    near_future_ms = int(datetime.now(UTC).timestamp() * 1000) + 3600_000
    await fleet_mod._maybe_queue_auto_upgrade(        sc=sc,
        body=_body("2.3.5", deploy_blocked_until=near_future_ms),
        pending_commands=sc.pending_commands,
        node_id="node-uuid-1",
    )
    assert sc.created_commands == []


@pytest.mark.asyncio
async def test_maybe_queue_auto_upgrade_ignores_absurd_block_cap(monkeypatch):
    """deploy_blocked_until > MAX_BLOCK_MS (7 days) is NOT honored.

    A misbehaving / malicious plugin could pin
    deploy_blocked_until = Number.MAX_SAFE_INTEGER to DoS its own
    upgrade path. The cap forces such absurd values to fall through
    to the normal "queue deploy" branch so the next heartbeat retries
    the upgrade.
    """
    monkeypatch.setattr(fleet_mod, "MIN_RECOMMENDED_PLUGIN_VERSION", "2.4.0")
    # Disable the pre-manifest-aware floor for this test — we're
    # exercising the absurd-block-cap branch, not the new gate.
    monkeypatch.setattr(fleet_mod, "MIN_AUTO_DEPLOY_PLUGIN_VERSION", "0.0.0")

    async def _enabled(_tid):
        return True

    monkeypatch.setattr(fleet_mod, "_auto_upgrade_enabled_for_tenant", _enabled)
    sc = _FakeStorage()
    absurd_future_ms = 99999999999999  # year 5138
    await fleet_mod._maybe_queue_auto_upgrade(        sc=sc,
        body=_body("2.3.5", deploy_blocked_until=absurd_future_ms),
        pending_commands=sc.pending_commands,
        node_id="node-uuid-1",
    )
    # Absurd value ignored → deploy IS queued (normal path).
    assert len(sc.created_commands) == 1
    assert sc.created_commands[0]["command"] == "deploy"


@pytest.mark.asyncio
async def test_maybe_queue_auto_upgrade_skips_when_disabled(monkeypatch):
    """tenant has auto_upgrade_enabled = false → skip."""
    monkeypatch.setattr(fleet_mod, "MIN_RECOMMENDED_PLUGIN_VERSION", "2.4.0")

    async def _disabled(_tid):
        return False

    monkeypatch.setattr(fleet_mod, "_auto_upgrade_enabled_for_tenant", _disabled)
    sc = _FakeStorage()
    await fleet_mod._maybe_queue_auto_upgrade(        sc=sc,
        body=_body("2.3.5"),
        pending_commands=sc.pending_commands,
        node_id="node-uuid-1",
    )
    assert sc.created_commands == []


@pytest.mark.asyncio
async def test_maybe_queue_auto_upgrade_skips_when_already_pending(monkeypatch):
    """An existing pending deploy → skip (don't double-queue)."""
    monkeypatch.setattr(fleet_mod, "MIN_RECOMMENDED_PLUGIN_VERSION", "2.4.0")

    async def _enabled(_tid):
        return True

    monkeypatch.setattr(fleet_mod, "_auto_upgrade_enabled_for_tenant", _enabled)
    sc = _FakeStorage(pending=[{"command": "deploy", "payload": {}}])
    await fleet_mod._maybe_queue_auto_upgrade(        sc=sc,
        body=_body("2.3.5"),
        pending_commands=sc.pending_commands,
        node_id="node-uuid-1",
    )
    assert sc.created_commands == []


# ---------------------------------------------------------------------------
# MIN_AUTO_DEPLOY_PLUGIN_VERSION — pre-manifest-aware floor (CAURA-000)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "stale_version", ["0.98.4", "2.0.0", "2.3.0", "2.4.0", "2.5.0"]
)
async def test_maybe_queue_auto_upgrade_skips_pre_manifest_aware(
    monkeypatch, stale_version
):
    """Pre-manifest-aware floor blocks every released plugin tag.

    The hardcoded fallback file list in those releases doesn't include
    files added in later releases (e.g. keystones.ts), so auto-deploy
    leaves the plugin unable to load. Same recovery path as
    KNOWN_BROKEN_DEPLOY_VERSIONS — operator runs the install one-liner.
    """
    monkeypatch.setattr(fleet_mod, "MIN_RECOMMENDED_PLUGIN_VERSION", "2.6.0")
    monkeypatch.setattr(fleet_mod, "MIN_AUTO_DEPLOY_PLUGIN_VERSION", "2.6.0")

    async def _enabled(_tid):
        return True

    monkeypatch.setattr(fleet_mod, "_auto_upgrade_enabled_for_tenant", _enabled)
    sc = _FakeStorage()
    await fleet_mod._maybe_queue_auto_upgrade(        sc=sc,
        body=_body(stale_version),
        pending_commands=sc.pending_commands,
        node_id="node-uuid-1",
    )
    assert sc.created_commands == [], (
        f"Expected no deploy queued for pre-manifest-aware {stale_version!r}; "
        f"got {sc.created_commands!r}"
    )


@pytest.mark.asyncio
async def test_maybe_queue_auto_upgrade_allowed_at_or_above_floor(monkeypatch):
    """A plugin at the floor exact version OR above is eligible for
    auto-deploy (subject to other gates). Asserts the floor is < rather
    than <=, so floor versions can still be pushed forward."""
    # Target is higher than floor — there's a real upgrade to issue.
    monkeypatch.setattr(fleet_mod, "MIN_RECOMMENDED_PLUGIN_VERSION", "2.6.1")
    monkeypatch.setattr(fleet_mod, "MIN_AUTO_DEPLOY_PLUGIN_VERSION", "2.6.0")

    async def _enabled(_tid):
        return True

    monkeypatch.setattr(fleet_mod, "_auto_upgrade_enabled_for_tenant", _enabled)
    sc = _FakeStorage()
    await fleet_mod._maybe_queue_auto_upgrade(        sc=sc,
        body=_body("2.6.0"),
        pending_commands=sc.pending_commands,
        node_id="node-uuid-1",
    )
    assert len(sc.created_commands) == 1, (
        f"Expected deploy queued for floor version 2.6.0; got {sc.created_commands!r}"
    )
    assert sc.created_commands[0]["command"] == "deploy"


def test_min_auto_deploy_constant_shape():
    """The floor is a string-shaped semver and stays a module-level
    constant so operators can find / bump it without searching the file.
    """
    assert isinstance(fleet_mod.MIN_AUTO_DEPLOY_PLUGIN_VERSION, str)
    parts = fleet_mod.MIN_AUTO_DEPLOY_PLUGIN_VERSION.split(".")
    assert len(parts) >= 2
    for p in parts:
        assert p.isdigit(), f"Non-numeric segment in floor: {p!r}"


def test_has_recent_deploy_from_list_handles_non_dict_entries():
    """Pre-fix ``(c or {}).get(...)`` raised AttributeError on a truthy
    non-dict element (string, number, list). The isinstance guard
    treats unexpected types as "not a deploy" rather than crashing
    the heartbeat.
    """
    # Garbage from a misbehaving storage backend — strings, numbers,
    # bare lists. All must be silently skipped, not crash.
    assert (
        fleet_mod._has_recent_deploy_command_from_list(
            ["not-a-dict", 42, ["nested"], None, {}]
        )
        is False
    )
    # Mixed: garbage + a valid deploy → still True (we don't miss the
    # legitimate entry just because adjacent ones are malformed).
    assert (
        fleet_mod._has_recent_deploy_command_from_list(
            ["garbage", {"command": "deploy"}, 99]
        )
        is True
    )


@pytest.mark.asyncio
async def test_maybe_queue_auto_upgrade_queues_deploy_for_old_version(monkeypatch):
    """Happy path: enabled, not blocked, no pending, valid old version → queue."""
    monkeypatch.setattr(fleet_mod, "MIN_RECOMMENDED_PLUGIN_VERSION", "2.4.0")
    # Disable the pre-manifest-aware floor for this test — happy-path coverage
    # is for an already-manifest-aware client; the new floor has its own tests.
    monkeypatch.setattr(fleet_mod, "MIN_AUTO_DEPLOY_PLUGIN_VERSION", "0.0.0")

    async def _enabled(_tid):
        return True

    monkeypatch.setattr(fleet_mod, "_auto_upgrade_enabled_for_tenant", _enabled)
    sc = _FakeStorage()
    await fleet_mod._maybe_queue_auto_upgrade(        sc=sc,
        body=_body("2.3.5"),
        pending_commands=sc.pending_commands,
        node_id="node-uuid-1",
    )
    assert len(sc.created_commands) == 1
    cmd = sc.created_commands[0]
    assert cmd["command"] == "deploy"
    assert cmd["payload"]["target_version"] == "2.4.0"
    assert cmd["tenant_id"] == "tenant-1"
    # Storage expects ``node_id`` (UUID FK to fleet_nodes.id), not ``node_name``.
    # Pre-fix the queue silently 500'd because ``_filter_fields`` dropped the
    # ``node_name`` key and ``node_id`` was NULL — this assertion locks the fix.
    assert cmd["node_id"] == "node-uuid-1"
    assert "node_name" not in cmd


@pytest.mark.asyncio
async def test_maybe_queue_auto_upgrade_skips_when_plugin_version_missing(monkeypatch):
    """No plugin_version on payload (very old plugin) → skip."""
    monkeypatch.setattr(fleet_mod, "MIN_RECOMMENDED_PLUGIN_VERSION", "2.4.0")

    async def _enabled(_tid):
        return True

    monkeypatch.setattr(fleet_mod, "_auto_upgrade_enabled_for_tenant", _enabled)
    sc = _FakeStorage()
    await fleet_mod._maybe_queue_auto_upgrade(        sc=sc,
        body=fleet_mod.HeartbeatIn(tenant_id="tenant-1", node_name="node-a"),
        pending_commands=sc.pending_commands,
        node_id="node-uuid-1",
    )
    assert sc.created_commands == []


# ---------------------------------------------------------------------------
# HeartbeatIn.recall_metrics size cap
# ---------------------------------------------------------------------------


def test_recall_metrics_under_cap_accepted():
    """Normal-sized blob from the plugin passes through unchanged."""
    body = fleet_mod.HeartbeatIn(
        tenant_id="tenant-1",
        node_name="node-a",
        recall_metrics={
            "calls_total": 142,
            "skipped_total": 89,
            "skipped_by_reason": {
                "trivial-ping": 41,
                "below-threshold": 33,
                "slash-command": 8,
                "explicit-recall-trigger": 7,
            },
        },
    )
    assert body.recall_metrics is not None
    assert body.recall_metrics["calls_total"] == 142


def test_recall_metrics_over_cap_rejected():
    """A misbehaving plugin sending a huge counter blob is rejected at
    the API boundary — protects nodes.metadata from unbounded growth.
    """
    import pytest as _pt
    from pydantic import ValidationError

    huge = {
        "calls_total": 1,
        "skipped_by_reason": {f"reason-{i}": i for i in range(500)},
    }
    # 500 keys × ~20 bytes ≈ 10 KB → well over the 4 KB cap.
    with _pt.raises(ValidationError, match="exceeds 4 KB limit"):
        fleet_mod.HeartbeatIn(
            tenant_id="tenant-1",
            node_name="node-a",
            recall_metrics=huge,
        )


def test_recall_metrics_none_accepted():
    """Omitted field is fine (back-compat with older plugins)."""
    body = fleet_mod.HeartbeatIn(tenant_id="tenant-1", node_name="node-a")
    assert body.recall_metrics is None


# ---------------------------------------------------------------------------
# HeartbeatIn.deploy_blocked_until validation
# ---------------------------------------------------------------------------


def test_deploy_blocked_until_positive_accepted():
    """Positive epoch-ms (future) is the normal case."""
    body = fleet_mod.HeartbeatIn(
        tenant_id="tenant-1",
        node_name="node-a",
        deploy_blocked_until=1_700_000_000_000,
    )
    assert body.deploy_blocked_until == 1_700_000_000_000


def test_deploy_blocked_until_none_accepted():
    """Omitted field is fine (older plugins, or no cooldown)."""
    body = fleet_mod.HeartbeatIn(tenant_id="tenant-1", node_name="node-a")
    assert body.deploy_blocked_until is None


def test_deploy_blocked_until_zero_rejected():
    """Zero is not a real cooldown timestamp. Reject at the API boundary
    so it doesn't land in nodes.metadata and mislead operators.
    """
    import pytest as _pt
    from pydantic import ValidationError

    with _pt.raises(ValidationError):
        fleet_mod.HeartbeatIn(
            tenant_id="tenant-1",
            node_name="node-a",
            deploy_blocked_until=0,
        )


def test_deploy_blocked_until_negative_rejected():
    """Negative values are nonsensical — reject."""
    import pytest as _pt
    from pydantic import ValidationError

    with _pt.raises(ValidationError):
        fleet_mod.HeartbeatIn(
            tenant_id="tenant-1",
            node_name="node-a",
            deploy_blocked_until=-1,
        )


# ---------------------------------------------------------------------------
# CAURA-000: in-flight deploy gate (acked-but-not-completed protection)
# ---------------------------------------------------------------------------
#
# The pending-only gate (``_has_recent_deploy_command_from_list``) above
# is blind to ``acked`` deploy commands — once the heartbeat handler
# ships a deploy to the plugin (transitioning ``pending`` → ``acked``),
# the next heartbeat sees an empty pending list and queues another
# deploy, even if the previous one is still building or got killed
# mid-result-POST by its own systemctl restart. Customer prod data
# (2026-06-08): 1,381 acked-stuck deploys on a single node, one new
# per 60s heartbeat — the "SIGTERM every 60 seconds" cycle.
#
# The fix adds a second gate that consults the repository for any
# ``deploy`` row with status IN (pending, acked) inside a 10-minute
# window. These tests pin both the skip-when-in-flight and the
# fail-open-on-DB-error behaviors.


@pytest.mark.asyncio
async def test_maybe_queue_auto_upgrade_skips_when_acked_deploy_in_flight(monkeypatch):
    """CAURA-000: an acked-but-not-completed deploy within the window
    blocks queueing another one (the customer's 60s-SIGTERM-loop fix)."""
    monkeypatch.setattr(fleet_mod, "MIN_RECOMMENDED_PLUGIN_VERSION", "2.4.0")
    monkeypatch.setattr(fleet_mod, "MIN_AUTO_DEPLOY_PLUGIN_VERSION", "0.0.0")

    async def _enabled(_tid):
        return True

    monkeypatch.setattr(fleet_mod, "_auto_upgrade_enabled_for_tenant", _enabled)

    # Storage reports "yes, a deploy is in flight" — gate must skip.
    async def _has_in_flight(*, node_id, since):
        return True

    sc = _FakeStorage()
    sc.in_flight_fn = _has_in_flight
    queued = await fleet_mod._maybe_queue_auto_upgrade(        sc=sc,
        body=_body("2.3.5"),
        pending_commands=sc.pending_commands,
        node_id="00000000-0000-0000-0000-000000000001",
    )
    assert queued is False
    assert sc.created_commands == [], (
        f"in-flight acked deploy must block queueing another; got {sc.created_commands}"
    )


@pytest.mark.asyncio
async def test_maybe_queue_auto_upgrade_queues_when_no_in_flight(monkeypatch):
    """Happy path: no in-flight deploy → queue as before. Pins that the
    new gate doesn't break the regular auto-upgrade flow when the repo
    reports no recent in-flight commands."""
    monkeypatch.setattr(fleet_mod, "MIN_RECOMMENDED_PLUGIN_VERSION", "2.4.0")
    monkeypatch.setattr(fleet_mod, "MIN_AUTO_DEPLOY_PLUGIN_VERSION", "0.0.0")

    async def _enabled(_tid):
        return True

    monkeypatch.setattr(fleet_mod, "_auto_upgrade_enabled_for_tenant", _enabled)

    async def _no_in_flight(*, node_id, since):
        return False

    sc = _FakeStorage()
    sc.in_flight_fn = _no_in_flight
    queued = await fleet_mod._maybe_queue_auto_upgrade(        sc=sc,
        body=_body("2.3.5"),
        pending_commands=sc.pending_commands,
        node_id="00000000-0000-0000-0000-000000000001",
    )
    assert queued is True
    assert len(sc.created_commands) == 1
    assert sc.created_commands[0]["command"] == "deploy"


@pytest.mark.asyncio
async def test_maybe_queue_auto_upgrade_fails_open_on_repo_error(monkeypatch):
    """DB hiccup on the new gate must NOT break auto-upgrade. The
    existing pending-only gate above is still a safety net, and an
    operator clearing the queue manually shouldn't require the new
    gate to be available. Logs a warning so the issue is observable."""
    monkeypatch.setattr(fleet_mod, "MIN_RECOMMENDED_PLUGIN_VERSION", "2.4.0")
    monkeypatch.setattr(fleet_mod, "MIN_AUTO_DEPLOY_PLUGIN_VERSION", "0.0.0")

    async def _enabled(_tid):
        return True

    monkeypatch.setattr(fleet_mod, "_auto_upgrade_enabled_for_tenant", _enabled)

    async def _throws(*, node_id, since):
        raise RuntimeError("DB connection lost")

    sc = _FakeStorage()
    sc.in_flight_fn = _throws
    queued = await fleet_mod._maybe_queue_auto_upgrade(        sc=sc,
        body=_body("2.3.5"),
        pending_commands=sc.pending_commands,
        node_id="00000000-0000-0000-0000-000000000001",
    )
    # Fail-open: queue still happens. (Pre-fix this path had no DB
    # call at all, so we preserve that behavior on storage error.)
    assert queued is True
    assert len(sc.created_commands) == 1


@pytest.mark.asyncio
async def test_maybe_queue_auto_upgrade_skips_pending_before_querying_repo(monkeypatch):
    """The cheaper pending-list check must run BEFORE the DB query.
    Pins the gate ordering: a deploy already in the pending list short-
    circuits without paying for a DB roundtrip. Avoids unnecessary
    storage load on every heartbeat under normal operation."""
    monkeypatch.setattr(fleet_mod, "MIN_RECOMMENDED_PLUGIN_VERSION", "2.4.0")
    monkeypatch.setattr(fleet_mod, "MIN_AUTO_DEPLOY_PLUGIN_VERSION", "0.0.0")

    async def _enabled(_tid):
        return True

    monkeypatch.setattr(fleet_mod, "_auto_upgrade_enabled_for_tenant", _enabled)

    # If the gate calls storage, this records it — we assert it was NOT
    # called because the pending check fires first.
    call_count = {"n": 0}

    async def _has_in_flight(*, node_id, since):
        call_count["n"] += 1
        return False

    sc = _FakeStorage(pending=[{"id": "x", "command": "deploy"}])
    sc.in_flight_fn = _has_in_flight
    queued = await fleet_mod._maybe_queue_auto_upgrade(        sc=sc,
        body=_body("2.3.5"),
        pending_commands=sc.pending_commands,
        node_id="00000000-0000-0000-0000-000000000001",
    )
    assert queued is False
    assert sc.created_commands == []
    assert call_count["n"] == 0, (
        "pending check must short-circuit before the storage call; "
        f"storage was called {call_count['n']} time(s)"
    )


def test_deploy_in_flight_window_is_sensible():
    """Lock the chosen window (10 min). Too short → races a slow build.
    Too long → manual recovery needed for genuinely-abandoned acks."""
    from datetime import timedelta

    assert fleet_mod.DEPLOY_IN_FLIGHT_WINDOW >= timedelta(minutes=2), (
        "window must exceed typical deploy wall-clock (build ~30s + "
        "restart ~10s + re-init ~5s + safety margin)"
    )
    assert fleet_mod.DEPLOY_IN_FLIGHT_WINDOW <= timedelta(minutes=30), (
        "window must be short enough that genuinely-abandoned acks "
        "auto-recover without operator intervention"
    )


# ---------------------------------------------------------------------------
# CAURA-000: auto-upgrade attempt budget (per node, per target_version)
# ---------------------------------------------------------------------------
#
# The in-flight window only stops concurrent storms. A node that never
# converges to the target — fetch failures, unsafe-filename aborts, or a
# "succeeds but version never advances" skew between MIN_RECOMMENDED and
# the served manifest version — would otherwise be re-queued every
# heartbeat (~60s) forever. The budget caps that at
# AUTO_UPGRADE_MAX_ATTEMPTS per target per AUTO_UPGRADE_ATTEMPT_WINDOW.
#
# Each test relies on the storage in-flight check defaulting to "no deploy
# in flight" (``_FakeStorage.fleet_in_flight_deploy`` → False) so execution
# reaches the budget check, then sets ``sc.count_fn`` to drive the branch
# under test. (These mirror the in-flight tests above.)


def _enable_upgrade(monkeypatch):
    monkeypatch.setattr(fleet_mod, "MIN_RECOMMENDED_PLUGIN_VERSION", "2.4.0")
    monkeypatch.setattr(fleet_mod, "MIN_AUTO_DEPLOY_PLUGIN_VERSION", "0.0.0")

    async def _enabled(_tid):
        return True

    monkeypatch.setattr(fleet_mod, "_auto_upgrade_enabled_for_tenant", _enabled)


@pytest.mark.asyncio
async def test_attempt_budget_skips_when_exhausted(monkeypatch):
    """At/above AUTO_UPGRADE_MAX_ATTEMPTS for this (node, target) in the
    window → gate stops re-queuing (the indefinite-churn fix)."""
    _enable_upgrade(monkeypatch)

    seen = {}

    async def _count(*, node_id, target_version, since):
        seen["target_version"] = target_version
        return fleet_mod.AUTO_UPGRADE_MAX_ATTEMPTS  # exactly at the cap

    sc = _FakeStorage()
    sc.count_fn = _count
    queued = await fleet_mod._maybe_queue_auto_upgrade(        sc=sc,
        body=_body("2.3.5"),
        pending_commands=sc.pending_commands,
        node_id="00000000-0000-0000-0000-000000000001",
    )
    assert queued is False
    assert sc.created_commands == [], (
        f"budget-exhausted node must not be re-queued; got {sc.created_commands}"
    )
    # Budget must be keyed on the target version, not the node's current
    # version — so a future release (new target) gets a fresh budget.
    assert seen["target_version"] == "2.4.0"


@pytest.mark.asyncio
async def test_attempt_budget_allows_under_cap(monkeypatch):
    """Below the cap → still queues. A node mid-upgrade (transient retry)
    must not be braked early."""
    _enable_upgrade(monkeypatch)

    async def _count(*, node_id, target_version, since):
        return fleet_mod.AUTO_UPGRADE_MAX_ATTEMPTS - 1  # one below the cap

    sc = _FakeStorage()
    sc.count_fn = _count
    queued = await fleet_mod._maybe_queue_auto_upgrade(        sc=sc,
        body=_body("2.3.5"),
        pending_commands=sc.pending_commands,
        node_id="00000000-0000-0000-0000-000000000001",
    )
    assert queued is True
    assert len(sc.created_commands) == 1
    assert sc.created_commands[0]["command"] == "deploy"


@pytest.mark.asyncio
async def test_attempt_budget_fresh_target_gets_fresh_budget(monkeypatch):
    """A brand-new target version (count=0 for it) queues even if the node
    burned its budget on a PRIOR target. Keying on target_version is what
    makes a new release recoverable."""
    _enable_upgrade(monkeypatch)

    async def _count(*, node_id, target_version, since):
        return 0  # no attempts yet for THIS target

    sc = _FakeStorage()
    sc.count_fn = _count
    queued = await fleet_mod._maybe_queue_auto_upgrade(        sc=sc,
        body=_body("2.3.5"),
        pending_commands=sc.pending_commands,
        node_id="00000000-0000-0000-0000-000000000001",
    )
    assert queued is True
    assert len(sc.created_commands) == 1


@pytest.mark.asyncio
async def test_attempt_budget_fails_open_on_repo_error(monkeypatch):
    """A DB error on the count lookup must NOT wedge a legitimate
    upgrade — fail open and queue (mirrors the in-flight check's
    fail-open posture). The in-flight check is the prior safety net."""
    _enable_upgrade(monkeypatch)

    async def _boom(*, node_id, target_version, since):
        raise RuntimeError("db down")

    sc = _FakeStorage()
    sc.count_fn = _boom
    queued = await fleet_mod._maybe_queue_auto_upgrade(        sc=sc,
        body=_body("2.3.5"),
        pending_commands=sc.pending_commands,
        node_id="00000000-0000-0000-0000-000000000001",
    )
    assert queued is True
    assert len(sc.created_commands) == 1


def test_attempt_budget_constants_are_sensible():
    """Lock the budget knobs. The window must comfortably exceed a
    deploy cycle so a real upgrade isn't counted against itself across
    retries; the cap must be > 1 (a converging upgrade is 1 attempt) and
    small enough to brake a wedge quickly."""
    from datetime import timedelta

    assert fleet_mod.AUTO_UPGRADE_ATTEMPT_WINDOW >= timedelta(hours=1)
    assert fleet_mod.AUTO_UPGRADE_ATTEMPT_WINDOW <= timedelta(days=2)
    assert fleet_mod.AUTO_UPGRADE_MAX_ATTEMPTS >= 2, (
        "must allow at least one retry beyond the single healthy attempt"
    )
    assert fleet_mod.AUTO_UPGRADE_MAX_ATTEMPTS <= 20, (
        "must brake a true wedge well below the ~1,440/day un-budgeted rate"
    )
