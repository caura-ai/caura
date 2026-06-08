#!/usr/bin/env python3
"""Dump the SoT `REGISTRY` to `plugin/tools.json` for consumption by the TS plugin.

Run from the memclaw repo root:

    PYTHONPATH=core-api/src:core-storage-api/src:. python scripts/export_tool_specs.py

CI check: `tests/test_tools_export_in_sync.py` runs this and diffs the
output against the committed `plugin/tools.json`. Drift fails the PR.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path


OUTPUT = Path("plugin/tools.json")


def build_payload() -> list[dict]:
    """Compose the JSON payload exported to the plugin."""
    from core_api.tools import REGISTRY, to_descriptor_json

    specs = sorted(REGISTRY.values(), key=lambda s: s.name)
    return [to_descriptor_json(s) for s in specs]


def main() -> int:
    payload = build_payload()
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    print(f"wrote {OUTPUT} ({len(payload)} tools)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
