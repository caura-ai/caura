"""Rows the OpenClaw plugin captured on its own, rather than an agent chose to write.

The plugin writes user messages, turn summaries and compaction summaries
without the agent deciding to (``plugin/src/context-engine.ts``), and tags each
write with one of :data:`AUTO_CAPTURE_TAGS`. A turn summary is whatever the
model said, so a page or file it read can put words there, and a user message
is whatever a person in that chat typed. Neither is a fact anyone chose to
share.

So a write carrying one of those tags is, whatever else the caller asked for:

- stored ``scope_agent``: the capturing agent's alone (and its organization's
  people, who read every scope);
- masked by the deterministic scan when the organization has not turned the
  PII gate on (``GovernanceScanContent``);
- held for review when its agent is below trust 1, with no hold level set
  (``write_hold.hold_for``);
- stamped ``_system.capture``: who captured it, which trigger, and the
  conversation it came from, as a hash of ``X-Caura-Session-Id``. ``_system`` is
  stripped from every caller's metadata (``system_metadata``), so no caller can
  write that record, and an update cannot remove it.

Installed plugins send the tags and no new field, so recognising the tags is
what covers them. A caller that tags a deliberate write this way only narrows it.

The broker (caura-daemon) captures too: each tool call's output, redacted on
the machine, through POST /memories/bulk with ``source: hook.post_tool_use``
(``queueCapture``). An install credential's item carrying that source is
stored ``scope_agent``, masked and stamped the same way, with its own
``session_id`` as the conversation (``stamp_broker_capture``). It is not held:
a batch's hold is decided once for the batch, which also carries the agent's
deliberate saves.
"""

from __future__ import annotations

import hashlib

from core_api.services.system_metadata import SYSTEM_NAMESPACE

# The plugin's automatic writes (ingest, afterTurn, compact). The plugin keeps
# the same set in ``AUTO_CAPTURE_TAGS``; its pre-compaction flush turn is the
# model's own ``caura_write`` and is not one of these.
AUTO_CAPTURE_TAGS: tuple[str, ...] = ("auto-ingest", "auto-turn-summary", "auto-compaction")

CAPTURE_KEY = "capture"
CAPTURED_VISIBILITY = "scope_agent"
# The trust level below which a captured write is held when the organization
# sets no level of its own (``quarantine.below_trust``).
CAPTURED_HOLD_BELOW_TRUST = 1


def capture_trigger(metadata: dict | None) -> str | None:
    """The auto-capture tag a write's caller metadata carries, or ``None``."""
    tags = (metadata or {}).get("tags")
    if not isinstance(tags, list):
        return None
    return next((tag for tag in AUTO_CAPTURE_TAGS if tag in tags), None)


def conversation_of(session_id: str | None) -> str | None:
    """The conversation a captured row came from: the first 32 hex characters
    of the SHA-256 of its session label. A hash, so the row does not carry the
    chat or person an OpenClaw session key names; the plugin computes the same
    value to recall a row only into its own conversation (``conversationOf``).
    """
    if not session_id:
        return None
    return hashlib.sha256(session_id.encode("utf-8")).hexdigest()[:32]


def capture_of(metadata: dict | None) -> dict | None:
    """The capture record on a row's (or a write's) metadata, or ``None``.

    Reads ``_system`` only, which callers cannot write, so a ``None`` here is a
    deliberate write.
    """
    record = ((metadata or {}).get(SYSTEM_NAMESPACE) or {}).get(CAPTURE_KEY)
    return record if isinstance(record, dict) else None


def stamp_capture(data, session_id: str | None) -> bool:
    """Mark ``data`` (a ``MemoryCreate`` whose metadata is already sanitised) if
    it is an automatic capture, and make it private. Returns whether it is one.
    """
    trigger = capture_trigger(data.metadata)
    if trigger is None:
        return False
    data.visibility = CAPTURED_VISIBILITY
    data.metadata.setdefault(SYSTEM_NAMESPACE, {})[CAPTURE_KEY] = {
        "source": "plugin",
        "trigger": trigger,
        "conversation": conversation_of(session_id),
    }
    return True


# The source the broker's PostToolUse hook tags a capture with, and the session
# key it names it under (caura-daemon ``queueCapture``, ``FormatMemoryMetadata``).
BROKER_CAPTURE_SOURCE = "hook.post_tool_use"
_BROKER_SESSION_KEY = "session_id"


def stamp_broker_capture(metadata: dict | None) -> bool:
    """Mark a broker item's ``metadata`` (already sanitised) if it is a capture.
    Returns whether it is one; the caller stores it ``CAPTURED_VISIBILITY``,
    since a bulk item has no visibility of its own.

    Only for an install credential's batch: anyone else's metadata is theirs.
    """
    if not metadata or metadata.get("source") != BROKER_CAPTURE_SOURCE:
        return False
    session = metadata.get(_BROKER_SESSION_KEY)
    metadata.setdefault(SYSTEM_NAMESPACE, {})[CAPTURE_KEY] = {
        "source": "broker",
        "trigger": "post_tool_use",
        "conversation": conversation_of(session if isinstance(session, str) else None),
    }
    return True
