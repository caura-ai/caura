"""The tool-latency section of the integration guide states checkable facts.

``docs/integration-without-plugin.md`` tells integrators to raise their MCP
client's tool timeout to ~30 s because ``caura_insights`` runs 6.8–8.7 s with
"no ``depth``/quick mode and no partial results — a timeout loses the entire
call". That is advice a reader acts on, and it stops being true the moment
someone adds a cheap mode.

A doc claim about behaviour is worth exactly as much as the thing that notices
when it goes stale, so the two checkable halves are pinned here: the parameter
that would make the claim wrong, and the cross-reference that would silently
break.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from core_api import mcp_server

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]

DOC = Path(__file__).parents[1] / "docs" / "integration-without-plugin.md"


async def test_insights_still_has_no_cheap_mode():
    """If a ``depth``/``quick`` parameter lands, the timeout advice is stale.

    Not a vote against adding one — it is the report's recommended real fix.
    This just makes adding it update the doc in the same change instead of
    leaving integrators sizing timeouts for a constraint that no longer exists.
    """
    tools = await mcp_server.mcp.list_tools()
    insights = next(t for t in tools if t.name == "caura_insights")
    params = set((insights.inputSchema.get("properties") or {}).keys())

    assert not params & {"depth", "quick", "mode"}, (
        "caura_insights gained a cheap-mode parameter. Update the Tool latency "
        f"section of {DOC.name}, which tells integrators there is none."
    )


def test_tool_latency_section_is_linked_and_present():
    """The pitfall entry links to the section by anchor; keep them together."""
    text = DOC.read_text()

    assert "## Tool latency" in text
    assert "(#tool-latency)" in text, "the caura_insights pitfall links to this anchor"


def test_latency_table_names_the_slow_tool():
    """Guards against the table being trimmed to the point of losing its subject."""
    text = DOC.read_text()
    section = text.split("## Tool latency", 1)[1]

    assert "caura_insights" in section
    # The advice is only actionable with a concrete number to size against.
    assert re.search(r"\d+(\.\d+)?\s*s", section), (
        "the table must keep at least one timing"
    )
