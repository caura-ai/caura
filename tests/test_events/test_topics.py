"""Topics enum behaviour — string equivalence + format-transparent."""

from __future__ import annotations

from common.events import Topics


# Every family is contracted now — audit was the last, on 2026-09-10 — so both
# assertions carry the current literal. This used ``frozen_topic`` for audit
# while it was the one member still on the legacy namespace; that helper pins
# the LEGACY prefix independently of the enum, and pointing it at a contracted
# family would assert the wrong thing.
# Literals here rather than the enum on both sides, so the test still fails if
# the value moves — comparing the enum to itself would pass through any rename.
def test_members_compare_equal_to_their_string_value() -> None:
    assert Topics.Memory.ENRICHED == "caura.memory.enriched"
    assert Topics.Audit.EVENT_RECORDED == "caura.audit.event-recorded"


def test_members_format_as_their_string_value() -> None:
    # f-string format MUST produce the value, not "Memory.ENRICHED".
    # Pub/Sub's topic_path uses f-string interpolation; regressing here
    # breaks every publish.
    assert f"{Topics.Memory.ENRICHED}" == "caura.memory.enriched"
    assert str(Topics.Memory.EMBED_REQUESTED) == "caura.memory.embed-requested"


def test_members_hash_equal_to_their_string_value() -> None:
    # Dict lookup on a handler map keyed by topic name must find the
    # enum member when looked up by the string and vice versa.
    d = {Topics.Memory.ENRICHED: 1}
    assert d["caura.memory.enriched"] == 1
    d2 = {"caura.memory.enriched": 2}
    assert d2[Topics.Memory.ENRICHED] == 2
