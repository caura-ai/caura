#!/usr/bin/env python3
"""Regenerate the MCP tool-surface baselines in ``tests/fixtures/``.

``tests/fixtures/README.md`` has documented this entry point for a while, but the
script itself was missing, so the only way to refresh a baseline was to hand-roll
the capture and match its serialisation by trial. Run it after any change to a
ToolSpec description, a tool's parameter annotations, or the registry contents:

    PYTHONPATH=core-api/src:core-storage-api/src:. python capture_baselines.py

Writes the three fixtures the surface tests compare against:

* ``tools_list_baseline_v1.json``                  — ``mcp.list_tools()``
* ``tool_descriptions_baseline_v1.json``           — ``/tool-descriptions``
* ``tool_descriptions_enriched_baseline_v1.json``  — ``…?enriched=true``

Importing ``core_api.mcp_server`` is what registers every tool, as a side effect.
Then check the token gate: tool-surface tokens are paid on every agent call, so
``test_mcp_token_budget.py`` fails if the surface grows past its ceiling. This
script prints the resulting count so a regression is visible here rather than in
CI.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

FIXTURES = Path(__file__).parent / "tests" / "fixtures"


def _write(name: str, payload: object) -> None:
    path = FIXTURES / name
    # Must match how the tests read these back byte-for-byte, or every
    # regeneration shows up as a whole-file diff: indent=2, real UTF-8 (the
    # descriptions contain characters like "≤"), trailing newline.
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"wrote {path.relative_to(Path(__file__).parent)}")


async def _capture() -> list[dict]:
    from core_api import mcp_server
    from core_api.routes.health import tool_descriptions

    tools = await mcp_server.mcp.list_tools()
    tools_list = [
        t.model_dump(mode="json") if hasattr(t, "model_dump") else dict(t.__dict__) for t in tools
    ]
    # The tests sort by name before comparing, because spec modules are
    # auto-loaded in `pkgutil.iter_modules` order; store it sorted so the file
    # is stable regardless.
    tools_list.sort(key=lambda x: x["name"])

    _write("tools_list_baseline_v1.json", tools_list)
    _write("tool_descriptions_baseline_v1.json", await tool_descriptions(enriched=False))
    _write("tool_descriptions_enriched_baseline_v1.json", await tool_descriptions(enriched=True))
    return tools_list


def main() -> None:
    tools_list = asyncio.run(_capture())
    try:
        import tiktoken
    except ImportError:
        print(f"\n{len(tools_list)} tools captured. Install tiktoken to see the token count.")
        return
    encoded = tiktoken.get_encoding("cl100k_base").encode(
        json.dumps(tools_list, separators=(",", ":"))
    )
    print(f"\n{len(tools_list)} tools, {len(encoded)} cl100k tokens in tools/list.")
    print("Compare against CEILING_TOKENS in tests/test_mcp_token_budget.py.")


if __name__ == "__main__":
    main()
