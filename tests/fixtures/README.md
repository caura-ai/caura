# Tool-surface baselines

Fixtures that lock the current MCP tool surface against silent regressions.

| File | What | Used by |
|---|---|---|
| `tools_list_baseline_v1.json` | `mcp.list_tools()` snapshot | `test_mcp_token_budget.py` (token ceiling + live-match) |
| `tool_descriptions_baseline_v1.json` | `GET /tool-descriptions` default shape | `test_tool_descriptions_regression.py` |
| `tool_descriptions_enriched_baseline_v1.json` | `GET /tool-descriptions?enriched=true` | `test_tool_descriptions_regression.py` |

## Token-budget gate

The `tools/list` MCP response must encode to ≤ **5000 cl100k tokens** (see
`CEILING_TOKENS` in `test_mcp_token_budget.py`). Tool-surface tokens are
paid on every agent call, so raise the ceiling only when a feature
genuinely needs it.

## Reproducing

```bash
cd memclaw
PYTHONPATH=core-api/src:core-storage-api/src:. python capture_baselines.py
```

The capture script imports `core_api.mcp_server` (which registers every
`@mcp.tool` as a side-effect) and dumps the in-process `mcp.list_tools()`
plus the `/tool-descriptions` JSON shapes.
