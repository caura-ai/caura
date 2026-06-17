"""Cross-worker settings-cache invalidation (CAURA-571).

The org-settings write path publishes ``Topics.Org.SETTINGS_CHANGED``; every
core-api process subscribes with ``broadcast=True`` and drops its cached
settings for that org, so a change made on one worker/instance takes effect
everywhere promptly instead of waiting out the per-process TTL.

These cover the handler, the payload schema, and the broadcast wiring. The
Pub/Sub-side per-process subscription mechanics live in
``tests/test_events/test_pubsub_bus.py``.
"""

from __future__ import annotations

import pydantic
import pytest

from common.events.base import Event
from common.events.org_settings_changed_event import OrgSettingsChangedEvent
from common.events.topics import Topics
from core_api import consumer as consumer_mod


def test_payload_model_validates_org_id() -> None:
    assert OrgSettingsChangedEvent(org_id="org-1").org_id == "org-1"


def test_payload_model_rejects_missing_org_id() -> None:
    with pytest.raises(pydantic.ValidationError):
        OrgSettingsChangedEvent.model_validate({})


@pytest.mark.asyncio
async def test_handler_invalidates_cache(monkeypatch) -> None:
    seen: list[str] = []
    monkeypatch.setattr(
        consumer_mod, "invalidate_cache", lambda org_id: seen.append(org_id)
    )
    event = Event(event_type=Topics.Org.SETTINGS_CHANGED, payload={"org_id": "org-7"})
    await consumer_mod.handle_org_settings_changed(event)
    assert seen == ["org-7"]


@pytest.mark.asyncio
async def test_handler_ack_drops_malformed_payload(monkeypatch) -> None:
    # Missing org_id → ValidationError → ack-drop (no invalidate, no raise), so
    # a poison message can't nack-loop the broadcast subscription.
    seen: list[str] = []
    monkeypatch.setattr(
        consumer_mod, "invalidate_cache", lambda org_id: seen.append(org_id)
    )
    event = Event(event_type=Topics.Org.SETTINGS_CHANGED, payload={})
    await consumer_mod.handle_org_settings_changed(event)
    assert seen == []


def test_register_consumers_subscribes_settings_as_broadcast(monkeypatch) -> None:
    # The invalidation consumer MUST be broadcast (every process), while the
    # existing enrich/embed consumers stay work-queue (one process per event).
    calls: list[tuple[str, bool]] = []

    class FakeBus:
        def subscribe(self, topic, handler, *, broadcast=False):
            calls.append((topic, broadcast))

    monkeypatch.setattr(consumer_mod, "get_event_bus", lambda: FakeBus())
    consumer_mod.register_consumers()

    assert (Topics.Org.SETTINGS_CHANGED, True) in calls
    assert (Topics.Memory.ENRICHED, False) in calls
    assert (Topics.Memory.EMBEDDED, False) in calls
