"""``caura_manage`` returns one shape per outcome, not prose for some ops.

The defect: ``op=read`` and ``op=update`` returned serialized objects, refusals
from every op returned a JSON error envelope, and ``op=transition`` and
``op=delete`` returned a human sentence. One tool, three return shapes — so a
caller had to sniff the type to tell success from failure, and then sniff the
*text*, because a string is the success case only if it does not happen to
contain "Error".

The parity smoke had to resort to exactly that::

    ok = (isinstance(mcp_out, dict) and mcp_out.get("status") == "outdated") or \\
         (isinstance(mcp_out, str) and "outdated" in mcp_out and "Error" not in mcp_out)

``"Error" not in mcp_out`` as a success predicate passes for years and then
silently inverts the day someone rewords a message.

Both ops now return objects. ``op=transition`` copies REST ``PATCH
/memories/{id}/status`` key-for-key (``memory_id`` / ``old_status`` /
``new_status``, see ``openapi_responses.MemoryStatusPatchResponse``) so one
client can parse both surfaces. REST DELETE returns 204 with no body, so
``op=delete`` has no shape to copy; it reuses ``memory_id`` rather than a bare
``id`` so one tool does not name the same thing two ways. The old prose is
preserved in ``message`` for chat rendering.
"""

from __future__ import annotations

import json
from uuid import uuid4

import pytest
from fastapi import HTTPException

from core_api import mcp_server
from tests._mcp_test_helpers import as_text, parse_envelope, stub_storage_client

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]

VALID_UID = str(uuid4())


def _memory_row(status="active"):
    return {
        "id": VALID_UID,
        "agent_id": "alice",
        "fleet_id": None,
        "visibility": "scope_team",
        "status": status,
        "content": "hello",
    }


def _async_return(value):
    async def _inner(*args, **kwargs):  # noqa: ARG001
        return value

    return _inner


def _wire_transition(monkeypatch, status="active"):
    sc = stub_storage_client(
        monkeypatch, get_memory=_memory_row(status), update_memory_status=None
    )
    monkeypatch.setattr(mcp_server, "authorize_memory_access", _async_return(True))
    monkeypatch.setattr(mcp_server, "log_action", _async_return(None))
    return sc


async def test_transition_returns_an_object_not_prose(mcp_env, monkeypatch):
    _wire_transition(monkeypatch)

    out = await mcp_server.caura_manage(
        op="transition", memory_id=VALID_UID, status="archived"
    )

    payload = parse_envelope(out)
    assert isinstance(payload, dict)
    assert payload["memory_id"] == VALID_UID
    assert payload["old_status"] == "active"
    assert payload["new_status"] == "archived"


async def test_delete_returns_an_object_not_prose(mcp_env):
    mcp_env["service"]("soft_delete_memory").return_value = None

    out = await mcp_server.caura_manage(op="delete", memory_id=VALID_UID)

    payload = parse_envelope(out)
    assert isinstance(payload, dict)
    assert payload["memory_id"] == VALID_UID
    assert payload["deleted"] is True


async def test_transition_keys_match_the_rest_status_route(mcp_env, monkeypatch):
    """Same key names as ``MemoryStatusPatchResponse``, so one parser covers both.

    Pinned against the model rather than a literal list, so renaming a field on
    the REST side fails here instead of silently reopening the divergence.
    """
    from core_api.openapi_responses import MemoryStatusPatchResponse

    _wire_transition(monkeypatch)

    payload = parse_envelope(
        await mcp_server.caura_manage(
            op="transition", memory_id=VALID_UID, status="archived"
        )
    )

    assert set(MemoryStatusPatchResponse.model_fields) <= payload.keys()


async def test_success_is_distinguishable_from_refusal_without_reading_text(
    mcp_env, monkeypatch
):
    """The point of the change: a structural predicate, not a substring one.

    Success and refusal are now both JSON objects, told apart by the presence
    of an ``error`` key — no ``isinstance`` sniffing, and nothing that inverts
    when a message is reworded.
    """
    mcp_env["service"]("soft_delete_memory").return_value = None
    ok = parse_envelope(await mcp_server.caura_manage(op="delete", memory_id=VALID_UID))

    mcp_env["service"]("soft_delete_memory").side_effect = HTTPException(
        status_code=403, detail="insufficient trust"
    )
    refused = parse_envelope(
        await mcp_server.caura_manage(op="delete", memory_id=VALID_UID)
    )

    assert "error" not in ok
    assert ok["deleted"] is True
    assert "error" in refused
    assert refused["error"]["code"] == "FORBIDDEN"


async def test_prose_survives_in_a_message_field(mcp_env, monkeypatch):
    """Kept for chat rendering — the sentence moved, it did not disappear."""
    _wire_transition(monkeypatch)
    transition = parse_envelope(
        await mcp_server.caura_manage(
            op="transition", memory_id=VALID_UID, status="archived"
        )
    )
    assert "active -> archived" in transition["message"]

    mcp_env["service"]("soft_delete_memory").return_value = None
    deleted = parse_envelope(
        await mcp_server.caura_manage(op="delete", memory_id=VALID_UID)
    )
    assert f"Memory {VALID_UID} deleted" in deleted["message"]


async def test_every_manage_success_op_parses_as_json(mcp_env, monkeypatch):
    """No op returns a bare string any more — the shape count drops to two.

    ``read``/``update`` already returned objects; ``transition``/``delete`` were
    the two that did not. Asserting over all four is what makes this a contract
    for the tool rather than two isolated fixes.
    """
    _wire_transition(monkeypatch)
    outs = [
        await mcp_server.caura_manage(op="read", memory_id=VALID_UID),
        await mcp_server.caura_manage(
            op="transition", memory_id=VALID_UID, status="archived"
        ),
    ]

    class _Out:
        def model_dump(self, mode="python"):  # noqa: ARG002
            return {"id": VALID_UID, "content": "new text"}

    mcp_env["service"]("update_memory").return_value = _Out()
    outs.append(
        await mcp_server.caura_manage(op="update", memory_id=VALID_UID, content="new text")
    )
    mcp_env["service"]("soft_delete_memory").return_value = None
    outs.append(await mcp_server.caura_manage(op="delete", memory_id=VALID_UID))

    for out in outs:
        assert isinstance(json.loads(as_text(out)), dict)
