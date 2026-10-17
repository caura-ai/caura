"""Unit tests for the Envelope wire schema."""

from __future__ import annotations

import json

import pytest
from caura_bus_core import Envelope, new_msg_id, new_thread_id, now_ms
from pydantic import ValidationError


def test_factory_populates_required_fields() -> None:
    env = Envelope.new(
        from_="agent-a",
        to=["agent-b"],
        body="hello",
    )
    assert env.id.startswith("msg_")
    assert env.thread_id.startswith("t_")
    assert env.from_ == "agent-a"
    assert env.to == ["agent-b"]
    assert env.kind == "info"
    assert env.parts == []
    assert env.correlation_id is None
    assert env.tenant_id is None
    assert env.fleet_id is None
    assert isinstance(env.ts, int) and env.ts > 0


def test_from_alias_serializes_as_from_on_the_wire() -> None:
    env = Envelope.new(from_="agent-a", to=["agent-b"], body="x")
    wire = json.loads(env.model_dump_json(by_alias=True))
    assert wire["from"] == "agent-a"
    assert "from_" not in wire


def test_envelope_round_trip_through_json() -> None:
    original = Envelope.new(
        from_="agent-a",
        to=["agent-b", "agent-c"],
        body="hello",
        correlation_id="msg_prev_xyz",
        tenant_id="tnt_1",
        fleet_id="flt_1",
    )
    payload = original.model_dump_json(by_alias=True)
    revived = Envelope.model_validate_json(payload)
    assert revived == original


def test_invalid_kind_rejected() -> None:
    with pytest.raises(ValidationError):
        Envelope(
            id=new_msg_id(),
            **{"from": "agent-a"},
            to=["agent-b"],
            kind="shout",  # not in the V1 enum
            thread_id=new_thread_id(),
            ts=now_ms(),
            body="x",
        )


def test_accepts_construction_by_attribute_name_or_alias() -> None:
    # populate_by_name=True means callers may pass `from_` or `from`.
    by_attr = Envelope(
        id=new_msg_id(),
        from_="agent-a",
        to=["agent-b"],
        thread_id=new_thread_id(),
        ts=now_ms(),
        body="x",
    )
    by_alias = Envelope(
        id=new_msg_id(),
        **{"from": "agent-a"},
        to=["agent-b"],
        thread_id=new_thread_id(),
        ts=now_ms(),
        body="x",
    )
    assert by_attr.from_ == by_alias.from_ == "agent-a"


def test_broadcast_recipient_list_allowed() -> None:
    # `to=["*"]` is how broadcast is encoded; the schema must allow it.
    env = Envelope.new(from_="agent-a", to=["*"], body="hi everyone")
    assert env.to == ["*"]


def test_ids_are_unique() -> None:
    assert new_msg_id() != new_msg_id()
    assert new_thread_id() != new_thread_id()
