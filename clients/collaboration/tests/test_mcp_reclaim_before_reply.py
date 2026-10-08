"""An answer in hand survives a lost lease only through a fresh, still-permitted claim."""

import httpx
import pytest
from caura_bus_core.bus import PlatformError
from caura_bus_mcp.server import dispatch
from collab_fake import Platform, close, session

ANSWER = {"delivery_id": "d1", "body": "Review findings", "idempotency_key": "result-1"}


@pytest.fixture
async def lost():
    """This session claimed d1, worked on it, then lost the lease (for example, a rebuild)."""
    platform = Platform()
    app = session(platform)
    await dispatch(app, "wait", {"timeout": 0})
    stale_token = platform.token
    platform.expire()
    try:
        yield platform, app, stale_token
    finally:
        await close(app)


async def test_reply_reclaims_the_same_work_then_answers_once(lost):
    platform, app, stale_token = lost
    receipt = await dispatch(app, "reply", ANSWER)
    assert receipt["message_id"] == "reply-1" and platform.state == "acked"
    sent = platform.actions("reply")
    assert len(sent) == 1 and sent[0]["lease_token"] not in {None, stale_token}
    assert platform.attempt == 2  # The same delivery, reclaimed by this session.
    # Retrying the same logical reply keeps its idempotency key and receipt.
    again = await dispatch(app, "reply", ANSWER)
    assert again["message_id"] == "reply-1" and again["duplicate"] is True


async def test_stale_token_is_rejected_by_caura(lost):
    platform, app, stale_token = lost
    with pytest.raises(PlatformError, match="stale delivery lease"):
        await app.bus.reply("d1", token=stale_token, idempotency_key="probe", body="x")
    with pytest.raises(PlatformError, match="stale delivery lease"):
        await app.bus.delivery_action("d1", "progress", stale_token, idempotency_key="p", summary="s")
    assert platform.replies == {}


async def test_cancellation_never_replays_stale_work(lost):
    platform, app, _ = lost
    platform.state = "cancelled"
    with pytest.raises(PlatformError) as exc:
        await dispatch(app, "reply", ANSWER)
    assert exc.value.detail["state"] == "unavailable"
    assert platform.actions("reply") == [] and app.delivery.current is None


async def test_changed_instructions_never_replay_stale_work(lost):
    platform, app, _ = lost
    platform.decide("redirect", "Only review the API module")
    with pytest.raises(PlatformError) as exc:
        await dispatch(app, "reply", ANSWER)
    assert exc.value.detail["state"] == "resumed"
    assert exc.value.detail["delivery"]["resume_context"]["instructions"] == "Only review the API module"
    assert platform.actions("reply") == []
    # A new answer for the new instructions uses a new key and the fresh claim.
    revised = {**ANSWER, "body": "API module findings", "idempotency_key": "result-2"}
    assert (await dispatch(app, "reply", revised))["message_id"] == "reply-1"


async def test_another_completion_never_replays_stale_work(lost):
    platform, app, _ = lost
    platform.state = "acked"  # Completed by a different attempt of this agent.
    with pytest.raises(PlatformError) as exc:
        await dispatch(app, "reply", ANSWER)
    assert exc.value.detail["state"] == "unavailable"
    assert platform.actions("reply") == []


async def test_another_session_keeps_its_live_claim(lost):
    platform, app, _ = lost
    other = session(platform)
    try:
        await dispatch(other, "wait", {"timeout": 0})
        owner_token = platform.token
        with pytest.raises(PlatformError) as exc:
            await dispatch(app, "reply", ANSWER)
        assert exc.value.detail["state"] == "unavailable"
        assert platform.token == owner_token and platform.actions("reply") == []
        assert (await dispatch(other, "reply", {**ANSWER, "idempotency_key": "owner"}))["message_id"]
    finally:
        await close(other)


async def test_lost_reply_response_is_replayed_by_key_not_reworked():
    platform = Platform()
    app = session(platform)
    try:
        await dispatch(app, "wait", {"timeout": 0})
        platform.lost_reply_responses = 3  # Commit, then lose every transport retry.
        with pytest.raises(httpx.ReadError):
            await dispatch(app, "reply", ANSWER)
        assert platform.state == "acked" and len(platform.replies) == 1
        # The local claim still holds the dead token; the same key returns the stored receipt.
        receipt = await dispatch(app, "reply", ANSWER)
        assert receipt["duplicate"] is True and receipt["message_id"] == "reply-1"
        assert len(platform.replies) == 1
    finally:
        await close(app)
