"""CI guard — `plugin/tools.json` must be in sync with the Python SoT.

Fails the PR if a developer changes a ToolSpec in Python without
regenerating the committed plugin snapshot.

Fix a failure by running:

    PYTHONPATH=core-api/src:core-storage-api/src:. python scripts/export_tool_specs.py
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).parent.parent
PLUGIN_SNAPSHOT = REPO_ROOT / "plugin" / "tools.json"


def test_plugin_tools_json_matches_registry():
    from scripts.export_tool_specs import build_payload  # type: ignore[attr-defined]

    expected = build_payload()
    actual = json.loads(PLUGIN_SNAPSHOT.read_text())
    assert actual == expected, (
        "plugin/tools.json is out of sync with the SoT registry. "
        "Regenerate via:\n"
        "  PYTHONPATH=core-api/src:core-storage-api/src:. "
        "python scripts/export_tool_specs.py"
    )
