"""Turns the OpenClaw plugin captures on its own are private, scanned and marked.

The plugin writes three kinds of row without the agent deciding to: user
messages (``auto-ingest``), turn summaries (``auto-turn-summary``) and
compaction summaries (``auto-compaction``). Plugins already installed send these
tags and nothing else, so the server recognises the tags. (The pre-compaction
flush turn is the model's own ``caura_write``; its prompt asks for
``scope_agent``.)

Such a row is stored ``scope_agent`` whatever the caller asked, is masked by the
deterministic scan even when the organization has not turned the PII gate on,
is held when its agent is below trust 1 even when no hold level is set, and
carries ``_system.capture``: who captured it, which trigger, and a hash of the
conversation it came from (from ``X-Caura-Session-Id``). ``_system`` is the
platform's namespace, so no caller can write or remove that record.
"""

from __future__ import annotations

import pytest

from tests.conftest import get_test_auth, new_tenant_id, uid

# The plugin sends ``X-Caura-Session-Id: openclaw:<sessionKey>``; the stamp is
# the first 32 hex characters of its SHA-256. The plugin's
# capture-privacy.test.ts pins the same vector.
DM_SESSION = "openclaw:agent:main:telegram:dm:42"
DM_CONVERSATION = "ef394477dbfa5bfa49682d369231cb10"

TURN = "We agreed to ship the billing change on Friday after the load test passes. " * 2


async def _write(
    client, tenant_id, headers, *, session: str | None = DM_SESSION, **body
):
    payload = {
        "tenant_id": tenant_id,
        "agent_id": f"capture-{uid()}",
        "memory_type": "episode",
        "content": TURN,
        "metadata": {"tags": ["auto-turn-summary"]},
        **body,
    }
    extra = {"X-Caura-Session-Id": session} if session else {}
    resp = await client.post(
        "/api/v1/memories", json=payload, headers={**headers, **extra}
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


@pytest.mark.parametrize(
    "trigger", ["auto-turn-summary", "auto-compaction", "auto-ingest"]
)
async def test_a_captured_row_is_private_and_names_its_conversation(client, trigger):
    tenant_id, headers = get_test_auth(new_tenant_id())
    row = await _write(
        client, tenant_id, headers, metadata={"tags": [trigger, "user-message"]}
    )

    assert row["visibility"] == "scope_agent"
    assert (row.get("system_metadata") or {}).get("capture") == {
        "source": "plugin",
        "trigger": trigger,
        "conversation": DM_CONVERSATION,
    }


async def test_a_caller_cannot_widen_a_captured_row_at_write_time(client):
    tenant_id, headers = get_test_auth(new_tenant_id())
    row = await _write(client, tenant_id, headers, visibility="scope_team")
    assert row["visibility"] == "scope_agent"


async def test_a_capture_without_a_session_header_is_still_marked(client):
    """An installed plugin's user-message write sends no session header: the
    row is captured all the same, with no conversation to name."""
    tenant_id, headers = get_test_auth(new_tenant_id())
    row = await _write(
        client, tenant_id, headers, session=None, metadata={"tags": ["auto-ingest"]}
    )
    assert row["visibility"] == "scope_agent"
    assert row["system_metadata"]["capture"] == {
        "source": "plugin",
        "trigger": "auto-ingest",
        "conversation": None,
    }


async def test_a_deliberate_write_is_not_marked(client):
    tenant_id, headers = get_test_auth(new_tenant_id())
    row = await _write(
        client,
        tenant_id,
        headers,
        metadata={"tags": ["deploy"], "_system": {"capture": {"source": "plugin"}}},
    )
    assert row["visibility"] == "scope_team"
    assert "capture" not in (row.get("system_metadata") or {})


async def test_a_secret_in_a_captured_turn_is_masked_with_the_gate_off(client):
    """The organization has not enabled the PII gate (the default)."""
    tenant_id, headers = get_test_auth(new_tenant_id())
    key = "AKIA" + "Q" * 16
    row = await _write(
        client, tenant_id, headers, content=f"{TURN} The deploy key is {key}."
    )
    assert key not in row["content"]

    # A deliberate write is governed by the organization's settings, as before.
    deliberate = await _write(
        client,
        tenant_id,
        headers,
        content=f"{TURN} key {key}",
        metadata={"tags": ["note"]},
    )
    assert key in deliberate["content"]


async def test_the_capture_record_survives_metadata_updates(client):
    """A caller can neither write ``_system`` nor drop it: a merge keeps the
    stored namespace, and a replace keeps the capture record."""
    tenant_id, headers = get_test_auth(new_tenant_id())
    row = await _write(client, tenant_id, headers)

    for body in (
        {"metadata": {"tags": ["edited"], "_system": {"capture": None}}},
        {"metadata": {"tags": ["edited"]}, "metadata_mode": "replace"},
        {"metadata": {"_system": {}}, "metadata_mode": "replace"},
    ):
        resp = await client.patch(
            f"/api/v1/memories/{row['id']}?tenant_id={tenant_id}",
            json=body,
            headers=headers,
        )
        assert resp.status_code == 200, resp.text
        got = await client.get(
            f"/api/v1/memories/{row['id']}?tenant_id={tenant_id}", headers=headers
        )
        assert got.status_code == 200, got.text
        assert (
            got.json()["system_metadata"]["capture"]["conversation"] == DM_CONVERSATION
        ), body


async def test_a_captured_row_is_held_below_trust_one_with_no_hold_level(monkeypatch):
    """No hold level set (the default): a captured write from an agent below
    trust 1 is held anyway; a deliberate one is not."""
    from types import SimpleNamespace

    from common.constants import HOLD_KEY, QUARANTINED_MEMORY_STATUS
    from core_api.pipeline.context import PipelineContext
    from core_api.pipeline.steps.write.hold_low_trust_write import HoldLowTrustWrite
    from core_api.schemas import MemoryCreate
    from core_api.services import write_hold

    class _Storage:
        async def get_agent(self, agent_id, tenant_id, read=True):
            return {"agent_id": agent_id, "trust_level": 0}

    monkeypatch.setattr(write_hold, "get_storage_client", lambda: _Storage())
    config = SimpleNamespace(quarantine_below_trust=lambda fleet_id: 0)

    async def _status(metadata: dict) -> str:
        data = MemoryCreate(
            tenant_id="t", agent_id="a", content=TURN, metadata={"tags": ["x"]}
        )
        data.metadata = metadata
        ctx = PipelineContext(
            data={
                "input": data,
                "memory_fields": {"status": "active", "metadata": dict(metadata)},
            },
            tenant_config=config,
        )
        await HoldLowTrustWrite().execute(ctx)
        fields = ctx.data["memory_fields"]
        if fields["status"] == QUARANTINED_MEMORY_STATUS:
            assert fields["metadata"]["_system"][HOLD_KEY]["below_trust"] == 1
        return fields["status"]

    captured = {
        "tags": ["auto-turn-summary"],
        "_system": {"capture": {"source": "plugin"}},
    }
    assert await _status(captured) == QUARANTINED_MEMORY_STATUS
    assert await _status({"tags": ["note"]}) == "active"


def test_the_recall_brief_marks_captured_rows():
    import json
    from types import SimpleNamespace

    from core_api.services.recall_service import _format_memories_for_prompt

    def _m(content, system_metadata=None):
        return SimpleNamespace(
            memory_type="episode",
            title=None,
            status="active",
            content=content,
            ts_valid_start=None,
            system_metadata=system_metadata,
        )

    items = json.loads(
        _format_memories_for_prompt(
            [
                _m("deliberate"),
                _m(
                    "captured",
                    {"capture": {"source": "plugin", "trigger": "auto-ingest"}},
                ),
            ]
        )
    )
    assert "auto_captured" not in items[0]
    assert items[1]["auto_captured"] is True


def test_conversation_hash_matches_the_plugin_vector():
    from core_api.services.auto_capture import conversation_of

    assert conversation_of(DM_SESSION) == DM_CONVERSATION
    assert conversation_of(None) is None
