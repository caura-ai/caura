"""GET /api/v1/telemetry and POST /api/v1/telemetry/rotate through the HTTP API.

Runs against the real storage stack (the in-process core-storage-api bridge),
so the identity row, the count reads and the per-tenant fleet summary are all
exercised the way a live server exercises them.
"""

from __future__ import annotations

import json
import os
import uuid
from pathlib import Path

import pytest

from core_api.config import settings
from core_api.heartbeat import sender as sender_mod
from core_api.heartbeat.identity import DEPLOYMENT_ORG_ID
from core_api.heartbeat.policy import REASON_INVALID_ENDPOINT_URL, Disabled, Enabled
from core_api.heartbeat.sender import HeartbeatSender
from core_api.heartbeat.state import SharedState
from tests.conftest import get_admin_headers
from tests.test_heartbeat_payload import assert_valid

SCHEMA = json.loads(
    (
        Path(__file__).resolve().parents[1] / "docs" / "telemetry-schema-v1.json"
    ).read_text()
)
REASONS = {
    "caura_telemetry_off",
    "do_not_track",
    "ci",
    "enterprise_gateway",
    "managed_platform",
    "pytest",
    "invalid_endpoint_url",
}


@pytest.fixture(autouse=True)
def _clean_sender_state():
    yield
    sender_mod._reset_for_tests()


async def test_telemetry_is_off_under_pytest(client):
    resp = await client.get("/api/v1/telemetry", headers=get_admin_headers())
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["enabled"] is False
    assert body["reason"] in REASONS
    assert body["deployment_id"] is None
    assert body["payload_preview"] is None
    assert body["last_sent_at"] is None
    assert body["last_status"] is None
    assert body["last_error"] is None
    assert body["last_attempt_at"] is None
    assert body["next_send_at"] is None
    assert body["endpoint"] == settings.caura_telemetry_url
    assert body["disable"] == "CAURA_TELEMETRY=off or DO_NOT_TRACK=1"
    assert set(body) == {
        "enabled",
        "reason",
        "deployment_id",
        "endpoint",
        "last_attempt_at",
        "last_sent_at",
        "last_status",
        "last_error",
        "next_send_at",
        "payload_preview",
        "disable",
    }


async def test_telemetry_reports_a_bad_collector_url(client, monkeypatch):
    """OFF with the reason and the offending endpoint, nothing previewed."""
    monkeypatch.setattr(settings, "caura_telemetry_url", "http://example.com/x")
    sender_mod._decision = Disabled(REASON_INVALID_ENDPOINT_URL)
    resp = await client.get("/api/v1/telemetry", headers=get_admin_headers())
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["enabled"] is False
    assert body["reason"] == "invalid_endpoint_url"
    assert body["endpoint"] == "http://example.com/x"
    assert body["payload_preview"] is None


async def test_telemetry_preview_when_enabled(client, sc):
    """With a sender installed, the preview is a valid schema-1 payload built from storage."""
    sender_mod._sender = HeartbeatSender(settings, version="3.16.0")
    sender_mod._decision = Enabled()

    resp = await client.get("/api/v1/telemetry", headers=get_admin_headers())
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["enabled"] is True
    assert body["reason"] is None
    uuid.UUID(body["deployment_id"])
    preview = body["payload_preview"]
    assert_valid(preview)
    assert preview["deployment_id"] == body["deployment_id"]
    assert preview["version"] == "3.16.0"
    assert preview["mode"]["standalone"] is True

    # The identity landed in the reserved org-settings row.
    stored = await sc.get_org_settings(DEPLOYMENT_ORG_ID)
    assert stored["deployment_id"] == body["deployment_id"]
    assert len(stored["deployment_token"]) == 64

    # A second read is stable.
    resp = await client.get("/api/v1/telemetry", headers=get_admin_headers())
    assert resp.json()["deployment_id"] == body["deployment_id"]


async def test_follower_worker_reports_the_leaders_values_and_summed_counts(
    client, tmp_path
):
    """A worker that does not hold the lock answers from state.json and sums counters."""
    leader = SharedState(tmp_path)
    assert leader.try_acquire_leader()
    try:
        leader.write_status(
            {
                "deployment_id": "6f0a3c2e-1b4d-4e8f-9a7c-2d5e8b1f4a90",
                "last_attempt_at": "2026-09-19T16:23:52Z",
                "last_sent_at": "2026-09-19T16:23:52Z",
                "last_status": 202,
                "last_error": None,
                "next_send_at": "2026-09-20T16:20:00Z",
            }
        )
        # The leader flushed 3 python-client requests; this worker saw none.
        # (The file carries the leader's pid, a live process; dead pids are pruned.)
        (tmp_path / f"clients-{leader.pid}.json").write_text(
            json.dumps(
                {
                    "pid": leader.pid,
                    "flushed_at": 4102444800.0,
                    "counts": {"caura-client-python": 3},
                }
            )
        )
        follower = SharedState(tmp_path, pid=os.getppid())
        assert not follower.is_leader
        sender_mod._sender = HeartbeatSender(
            settings, shared=follower, version="3.17.0"
        )
        sender_mod._decision = Enabled()

        resp = await client.get("/api/v1/telemetry", headers=get_admin_headers())
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["enabled"] is True
        assert body["last_sent_at"] == "2026-09-19T16:23:52Z"
        assert body["last_attempt_at"] == "2026-09-19T16:23:52Z"
        assert body["last_status"] == 202
        assert body["last_error"] is None
        assert body["next_send_at"] == "2026-09-20T16:20:00Z"
        assert body["deployment_id"] == "6f0a3c2e-1b4d-4e8f-9a7c-2d5e8b1f4a90"
        preview = body["payload_preview"]
        assert_valid(preview)
        assert preview["clients_24h"]["caura-client-python"] == "2-5"
    finally:
        leader.release_leader()


async def test_rotate_replaces_the_identity(client, sc):
    sender_mod._sender = HeartbeatSender(settings)
    sender_mod._decision = Enabled()
    before = (await client.get("/api/v1/telemetry", headers=get_admin_headers())).json()

    resp = await client.post("/api/v1/telemetry/rotate", headers=get_admin_headers())
    assert resp.status_code == 200, resp.text
    rotated = resp.json()
    assert rotated["rotated"] is True
    uuid.UUID(rotated["deployment_id"])
    assert rotated["deployment_id"] != before["deployment_id"]

    after = (await client.get("/api/v1/telemetry", headers=get_admin_headers())).json()
    assert after["deployment_id"] == rotated["deployment_id"]
    assert after["payload_preview"]["deployment_id"] == rotated["deployment_id"]
    stored = await sc.get_org_settings(DEPLOYMENT_ORG_ID)
    assert stored["deployment_id"] == rotated["deployment_id"]


async def test_rotate_works_while_disabled(client, sc):
    """Rotation is an explicit operator action; it does not need the loop."""
    resp = await client.post("/api/v1/telemetry/rotate", headers=get_admin_headers())
    assert resp.status_code == 200, resp.text
    stored = await sc.get_org_settings(DEPLOYMENT_ORG_ID)
    assert stored["deployment_id"] == resp.json()["deployment_id"]
