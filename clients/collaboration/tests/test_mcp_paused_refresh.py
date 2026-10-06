"""A paused local claim is refreshed from Caura once a human resolves the pause."""

import asyncio

import pytest
from caura_bus_core.bus import PlatformError
from caura_bus_mcp.server import dispatch
from collab_fake import Platform, close, session


@pytest.fixture
async def paused():
    """A delivery claimed by this MCP session, then paused and observed as paused."""
    platform = Platform()
    app = session(platform)
    await dispatch(app, "wait", {"timeout": 0})
    first_token = platform.token
    platform.pause()
    with pytest.raises(PlatformError) as exc:
        await dispatch(app, "progress", {"delivery_id": "d1", "summary": "working", "idempotency_key": "p1"})
    assert exc.value.status == 409 and exc.value.detail["state"] == "paused"
    assert platform.token is None  # Caura-side stop confirmed; the old token is gone.
    try:
        yield platform, app, first_token
    finally:
        await close(app)


def progress(key="p2"):
    return {"delivery_id": "d1", "summary": "continuing", "idempotency_key": key}


def waits(platform):
    return sum(1 for path, _ in platform.calls if path == "/inbox/wait")


async def test_all_operations_recover_after_approval(paused):
    platform, app, first_token = paused
    platform.decide("approve")

    assert (await dispatch(app, "progress", progress()))["status"] == "leased"
    assert platform.state == "leased" and platform.token not in {None, first_token}
    assert platform.actions("progress")[-1]["lease_token"] == platform.token
    assert (await dispatch(app, "discover", {}))["agents"]
    receipt = await dispatch(app, "reply", {"delivery_id": "d1", "body": "done", "idempotency_key": "r1"})
    assert receipt["message_id"] == "reply-1" and platform.state == "acked"
    assert app.delivery.current is None


async def test_unrelated_operations_recover_after_approval(paused):
    platform, app, _ = paused
    platform.decide("approve")
    assert (await dispatch(app, "discover", {}))["agents"] == [{"agent_id": "architect"}]
    # The resumed claim belongs to this session; the model sees it on its next wait.
    assert (await dispatch(app, "wait", {"timeout": 0}))["delivery"]["state"] == "leased"


async def test_still_paused_work_stays_fenced(paused):
    platform, app, _ = paused
    for op, args in (
        ("progress", progress()),
        ("reply", {"delivery_id": "d1", "body": "late", "idempotency_key": "r1"}),
        ("discover", {}),
    ):
        with pytest.raises(PlatformError) as exc:
            await dispatch(app, op, args)
        assert exc.value.detail["state"] == "paused"
    assert platform.actions("progress") == [] and platform.actions("reply") == []
    assert platform.state == "paused" and platform.token is None


async def test_rejected_delivery_is_never_resurrected(paused):
    platform, app, _ = paused
    platform.decide("reject")
    with pytest.raises(PlatformError) as exc:
        await dispatch(app, "reply", {"delivery_id": "d1", "body": "late", "idempotency_key": "r1"})
    assert exc.value.detail["state"] == "unavailable"
    assert platform.actions("reply") == [] and platform.state == "cancelled"
    assert app.delivery.current is None
    # Work unrelated to the withdrawn delivery continues; nothing re-leases it.
    assert (await dispatch(app, "discover", {}))["agents"]
    assert (await dispatch(app, "wait", {"timeout": 0}))["delivery"] is None


async def test_redirect_shows_new_instructions_before_work_continues(paused):
    platform, app, _ = paused
    platform.decide("redirect", "Only review the API module")
    args = {"delivery_id": "d1", "body": "Full review", "idempotency_key": "r1"}
    with pytest.raises(PlatformError) as exc:
        await dispatch(app, "reply", args)
    assert exc.value.detail["state"] == "resumed"
    assert exc.value.detail["delivery"]["resume_context"]["instructions"] == "Only review the API module"
    assert platform.actions("reply") == []
    args["body"] = "API module review"
    assert (await dispatch(app, "reply", args))["message_id"] == "reply-1"


async def test_errors_never_carry_lease_tokens(paused):
    platform, app, _ = paused
    platform.decide("redirect", "Narrow scope")
    with pytest.raises(PlatformError) as exc:
        await dispatch(app, "progress", progress())
    assert platform.token and platform.token not in str(exc.value)
    assert "lease_token" not in exc.value.detail["delivery"]


async def test_concurrent_refreshes_reclaim_once(paused):
    platform, app, _ = paused
    platform.decide("approve")
    before = waits(platform)
    results = await asyncio.gather(
        dispatch(app, "progress", progress("a")), dispatch(app, "progress", progress("b"))
    )
    assert [r["status"] for r in results] == ["leased", "leased"]
    assert waits(platform) - before == 1 and platform.attempt == 1


async def test_refresh_cannot_steal_a_live_lease_from_another_session(paused):
    platform, app, _ = paused
    platform.decide("approve")
    other = session(platform)
    try:
        await dispatch(other, "wait", {"timeout": 0})
        owner_token = platform.token
        with pytest.raises(PlatformError) as exc:
            await dispatch(app, "progress", progress())
        assert exc.value.detail["state"] == "unavailable"
        assert platform.token == owner_token and platform.session == other.delivery.session_id
        assert (await dispatch(other, "progress", progress("owner")))["status"] == "leased"
    finally:
        await close(other)


async def test_notices_seen_during_refresh_reach_the_next_wait(paused):
    platform, app, _ = paused
    platform.notices = [{"type": "request_overdue", "message_id": "m9"}]
    with pytest.raises(PlatformError):
        await dispatch(app, "discover", {})
    result = await dispatch(app, "wait", {"timeout": 50})
    assert result["notices"] == [{"type": "request_overdue", "message_id": "m9"}]
