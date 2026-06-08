"""Topics enum behaviour — string equivalence + format-transparent."""

from __future__ import annotations

from common.events import Topics


def test_members_compare_equal_to_their_string_value() -> None:
    assert Topics.Memory.CREATED == "memclaw.memory.created"
    assert Topics.Audit.EVENT_RECORDED == "memclaw.audit.event-recorded"


def test_members_format_as_their_string_value() -> None:
    # f-string format MUST produce the value, not "Memory.CREATED".
    # Pub/Sub's topic_path uses f-string interpolation; regressing here
    # breaks every publish.
    assert f"{Topics.Memory.CREATED}" == "memclaw.memory.created"
    assert str(Topics.Memory.EMBED_REQUESTED) == "memclaw.memory.embed-requested"


def test_members_hash_equal_to_their_string_value() -> None:
    # Dict lookup on a handler map keyed by topic name must find the
    # enum member when looked up by the string and vice versa.
    d = {Topics.Memory.CREATED: 1}
    assert d["memclaw.memory.created"] == 1
    d2 = {"memclaw.memory.created": 2}
    assert d2[Topics.Memory.CREATED] == 2
