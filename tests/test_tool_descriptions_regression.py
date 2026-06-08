"""Phase 4 regression — `/tool-descriptions` and MCP `tools/list` derived
from the SoT registry must produce the captured v1.0 baselines.

Locks the post-consolidation 13-tool surface (10 user-facing LTM tools —
of which 7 are live and 3 are knowledge-layer placeholders — plus 3
STM tools). The pre-consolidation `*_16tools.json` baselines are kept
in `tests/fixtures/` for token-budget delta measurement.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

FIXTURES = Path(__file__).parent / "fixtures"
EXPECTED_TOOL_COUNT = 12


@pytest.mark.asyncio
async def test_tool_descriptions_default_matches_baseline():
    """Default shape `{name: description}` matches the v1 baseline."""
    from core_api.routes.health import tool_descriptions

    actual = await tool_descriptions(enriched=False)
    expected = json.loads((FIXTURES / "tool_descriptions_baseline_v1.json").read_text())
    assert actual == expected, (
        "Default /tool-descriptions output drifted from baseline. "
        "If intentional, regenerate the v1 baselines."
    )


@pytest.mark.asyncio
async def test_tool_descriptions_enriched_matches_baseline():
    """Enriched shape `{name: {description, stm_only}}` matches baseline."""
    from core_api.routes.health import tool_descriptions

    actual = await tool_descriptions(enriched=True)
    expected = json.loads(
        (FIXTURES / "tool_descriptions_enriched_baseline_v1.json").read_text()
    )
    assert actual == expected


def test_registry_has_v1_spec_count():
    """Surface registers exactly the expected number of specs."""
    from core_api.tools import REGISTRY

    assert len(REGISTRY) == EXPECTED_TOOL_COUNT


def test_no_placeholders_remain():
    """All v1.0 reserved slots have been removed or promoted."""
    from core_api.tools import REGISTRY

    for name, spec in REGISTRY.items():
        assert spec.impl_status != "reserved", f"{name} is still a placeholder"


@pytest.mark.asyncio
async def test_mcp_tools_list_content_matches_baseline():
    """MCP `tools/list` content (sorted by name) matches the v1 baseline.

    Order is normalized because `pkgutil.iter_modules` auto-loads spec
    modules alphabetically; the MCP spec doesn't mandate order.
    """
    from core_api import mcp_server

    tools = await mcp_server.mcp.list_tools()
    actual = []
    for t in tools:
        d = t.model_dump(mode="json") if hasattr(t, "model_dump") else dict(t.__dict__)
        actual.append(d)
    actual.sort(key=lambda x: x["name"])

    expected = json.loads((FIXTURES / "tools_list_baseline_v1.json").read_text())
    expected.sort(key=lambda x: x["name"])

    assert actual == expected, (
        "MCP tools/list content drifted from v1 baseline. "
        "If intentional, regenerate via the capture snippet in tests/fixtures/README.md."
    )
