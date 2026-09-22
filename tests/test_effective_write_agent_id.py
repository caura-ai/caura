"""Tests for shared `agent_ids.effective_write_agent_id` write-path resolution.

Rule: the verified gateway id wins UNLESS it's a reserved placeholder, in which
case a non-placeholder body id is honored (so a `home_agent_id="main"` cred can
self-identify instead of collapsing onto "main"). A real verified id is never
overridden by the body (no spoofing of a properly-provisioned cred).
"""

import pytest

from core_api.agent_ids import (
    DOC_INDEXER_AGENT_ID,
    INSIGHTER_AGENT_ID,
    LEGACY_DOC_INDEXER_AGENT_ID,
    LEGACY_INSIGHTER_AGENT_ID,
    effective_write_agent_id,
    service_agent_read_ids,
)


@pytest.mark.parametrize(
    "verified,body,expected",
    [
        # Real verified id always wins — body cannot override (anti-spoof).
        ("webclaw", "evilclaw", "webclaw"),
        ("webclaw", None, "webclaw"),
        ("webclaw", "main", "webclaw"),
        # A verified "mcp-agent" home is a real (if poorly-named) identity and
        # MUST win — not be overridable by the body (closes the spoof vector).
        ("mcp-agent", "evilclaw", "mcp-agent"),
        ("mcp-agent", None, "mcp-agent"),
        # Reserved-placeholder verified id ("main") -> honor a real body id.
        ("main", "webclaw", "webclaw"),
        ("main", "main-7765cca229ab", "main-7765cca229ab"),
        # Reserved verified + reserved/empty body -> stays reserved (PR-1 rejects).
        ("main", None, "main"),
        ("main", "main", "main"),
        ("main", "mcp-agent", "main"),  # body placeholder too -> no mcp-agent collapse
        # No verified id (tenant-key / standalone) -> body used, unchanged behavior.
        (None, "webclaw", "webclaw"),
        (
            None,
            "mcp-agent",
            "mcp-agent",
        ),  # preserved for _refuse_default_agent_on_gateway
        (None, None, None),
        (LEGACY_INSIGHTER_AGENT_ID, None, INSIGHTER_AGENT_ID),
        (None, LEGACY_DOC_INDEXER_AGENT_ID, DOC_INDEXER_AGENT_ID),
    ],
)
def test_effective_write_agent_id(verified, body, expected):
    assert effective_write_agent_id(verified, body) == expected


@pytest.mark.parametrize(
    "supplied,expected",
    [
        (INSIGHTER_AGENT_ID, (INSIGHTER_AGENT_ID, LEGACY_INSIGHTER_AGENT_ID)),
        (LEGACY_INSIGHTER_AGENT_ID, (INSIGHTER_AGENT_ID, LEGACY_INSIGHTER_AGENT_ID)),
        (DOC_INDEXER_AGENT_ID, (DOC_INDEXER_AGENT_ID, LEGACY_DOC_INDEXER_AGENT_ID)),
        ("customer-agent", ("customer-agent",)),
    ],
)
def test_service_agent_read_ids_are_canonical_first(supplied, expected):
    assert service_agent_read_ids(supplied) == expected
