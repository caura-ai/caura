"""Unit tests for broker (install-credential) bulk-write agent attribution.

memclawd Layer 1 stamps each capture's ``metadata.agent_id`` with the agent that
produced it. Layer 2 (``_broker_write_agent_id``) attributes a broker bulk write
to that agent when the batch unambiguously names one — so the memory view shows
the agent instead of the bare install. A mixed batch, or a pre-Layer-1 broker
that sends no per-item agent_id, falls back to the install identity so a write
is never mis-attributed.
"""

from __future__ import annotations

from core_api.routes.memories import _broker_write_agent_id
from core_api.schemas import BulkMemoryItem

_INSTALL = "8fd60b3e-7369-4938-bee9-c55d07be3c67"
_AGENT = "ea41b380-fdf6-5f08-acc2-420c45acb670"
_AGENT2 = "93ca3edd-1219-518a-9360-2d1c00b22532"


def _item(agent_id: str | None) -> BulkMemoryItem:
    meta = {"agent_id": agent_id} if agent_id is not None else None
    return BulkMemoryItem(content="x", metadata=meta)


def test_unanimous_agent_is_used():
    items = [_item(_AGENT), _item(_AGENT)]
    assert _broker_write_agent_id(items, _INSTALL) == _AGENT


def test_mixed_agents_fall_back_to_install():
    items = [_item(_AGENT), _item(_AGENT2)]
    assert _broker_write_agent_id(items, _INSTALL) == f"broker:{_INSTALL}"


def test_no_metadata_falls_back_to_install():
    items = [_item(None), _item(None)]
    assert _broker_write_agent_id(items, _INSTALL) == f"broker:{_INSTALL}"


def test_empty_agent_id_is_ignored():
    items = [_item(""), _item("")]
    assert _broker_write_agent_id(items, _INSTALL) == f"broker:{_INSTALL}"


def test_single_named_agent_among_unnamed_items_wins():
    # A pre-Layer-1 item (no agent_id) mixed with Layer-1 items that all name
    # the same agent: the one distinct agent still attributes the write.
    items = [_item(None), _item(_AGENT), _item(_AGENT)]
    assert _broker_write_agent_id(items, _INSTALL) == _AGENT


def test_missing_install_uuid_falls_back_to_unknown():
    assert _broker_write_agent_id([_item(None)], None) == "broker:unknown"
