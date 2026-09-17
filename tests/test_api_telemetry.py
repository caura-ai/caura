"""GET /api/v1/telemetry and POST /api/v1/telemetry/rotate through the HTTP API.

Runs against the real storage stack (the in-process core-storage-api bridge),
so the identity row, the count reads and the per-tenant fleet summary are all
exercised the way a live server exercises them.
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path

import pytest

from core_api.config import settings
from core_api.heartbeat import sender as sender_mod
from core_api.heartbeat.identity import DEPLOYMENT_ORG_ID
from core_api.heartbeat.policy import Enabled
from core_api.heartbeat.sender import HeartbeatSender
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
    assert body["next_send_at"] is None
    assert body["endpoint"] == settings.caura_telemetry_url
    assert body["disable"] == "CAURA_TELEMETRY=off or DO_NOT_TRACK=1"
    assert set(body) == {
        "enabled",
        "reason",
        "deployment_id",
        "endpoint",
        "last_sent_at",
        "last_status",
        "next_send_at",
        "payload_preview",
        "disable",
    }


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
