# Tool-surface baselines

Fixtures that lock the current MCP tool surface against silent regressions.

| File | What | Used by |
|---|---|---|
| `tools_list_baseline_v1.json` | `mcp.list_tools()` snapshot | `test_mcp_token_budget.py` (token ceiling + live-match) |
| `tool_descriptions_baseline_v1.json` | `GET /tool-descriptions` default shape | `test_tool_descriptions_regression.py` |
| `tool_descriptions_enriched_baseline_v1.json` | `GET /tool-descriptions?enriched=true` | `test_tool_descriptions_regression.py` |

## Token-budget gate

The `tools/list` MCP response must encode within `CEILING_TOKENS` in
`test_mcp_token_budget.py`. Read the constant and the dated reasons beside it
rather than any number quoted here. Tool-surface tokens are paid on every agent
call, so raise the ceiling only when a feature genuinely needs it; trimming
something the inputSchema already says is usually cheaper than raising it.

## Reproducing

```bash
cd caura
PYTHONPATH=core-api/src:core-storage-api/src:. python capture_baselines.py
```

The capture script imports `core_api.mcp_server`, which registers every tool as
a side effect: importing it pulls in the SoT registry at the bottom of that
module, and each `caura_*` spec module calls `mcp_register` as it loads. It then
dumps the in-process `mcp.list_tools()`
plus the `/tool-descriptions` JSON shapes. It prints the resulting token count so
a budget regression shows up locally instead of in CI.

Regenerate whenever a `ToolSpec` description, a tool's parameter annotations, or
the registry contents change. `plugin/tools.json` is a separate artifact with its
own generator — `python scripts/export_tool_specs.py` — and
`test_tools_export_in_sync.py` will fail until you run that too.

## Rule-set hash vectors

`ruleset-hash-vectors.json` holds the shared cases for the rule-set hash
(`common/governance/ruleset_hash.py`, tested by `test_ruleset_hash.py`). It is
not generated from this code: the expected bytes come from an independent
RFC 8785 oracle in node (caura-ai/caura-daemon
`internal/ruleset/testdata/gen-vectors.mjs`), and the same file sits byte for
byte beside it, where the broker's Go suite runs it too. Both suites pin the
file's sha256, so edit both copies and both pins in the same pair of PRs.
