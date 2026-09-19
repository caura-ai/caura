# Caura — governed shared memory for AI agent fleets

Caura (formerly MemClaw) is an Agent DB — governed shared memory for AI agent fleets. Agents commit what they learn once; every agent in the fleet recalls it through MCP tools, REST, or Caura Rail (preview), subject to tenant isolation, visibility scope (scope_agent / scope_team / scope_org) and caller trust level. <!-- legacy-name-floor: taught as the former name -->

`caura` is the short install name for [`caura-client`](https://pypi.org/project/caura-client/),
the official Python client. Both names install the same package; `caura-client`
is the canonical one.

## Install

```bash
pip install caura
```

## Quickstart

```python
from caura import Caura

# Get an API key at https://caura.ai, or point base_url at a self-hosted server.
with Caura("mc_xxx", tenant_id="my-team", agent_id="my-agent") as mc:
    # Commit a memory once. The server enriches it with type, title, tags and importance.
    mc.write("Q3 revenue target is $4M, set on 2026-04-15.")

    # Search: ranked raw results.
    for m in mc.search("Q3 revenue target", top_k=5):
        print(m.title, "—", m.content)

    # Recall: an LLM-synthesized context brief.
    print(mc.recall("Q3 revenue target").summary)
```

Self-hosted? Pass `base_url="http://localhost:8000"`. The full client API is on the
[caura-client](https://pypi.org/project/caura-client/) page.

## Connect an MCP client

Agents that speak MCP (Claude Code, Cursor, OpenClaw and others) reach the same
memory without any SDK. Copy an API key from the [caura.ai](https://caura.ai)
dashboard and add:

```json
{
  "mcpServers": {
    "caura": {
      "url": "https://caura.ai/mcp",
      "headers": { "X-API-Key": "mc_your_api_key_here" }
    }
  }
}
```

For a production fleet, provision one agent-scoped credential per agent; see
[per-agent keys](https://caura.ai/docs/integrations/per-agent-keys). With a
tenant-scoped dashboard key, pass an explicit `agent_id` on every tool call.

## Three ways agents use memory

- **Deterministic: [Caura Rail](https://github.com/caura-ai/caura-rail) (preview).**
  Your code runs recall before every agent turn and commit after it, so the
  model cannot skip the memory step.
- **Agentic: MCP tools or REST.** The agent decides when to call `caura_write`
  and `caura_recall`, through the MCP config above or this client.
- **Reflective: the [Interviewer](https://caura.ai/docs/interviewer).** Caura
  reads the agent's own session transcript after the fact and stores the
  decisions and preferences the agent never stopped to record.

## Links

- Documentation: https://caura.ai/docs
- Source: https://github.com/caura-ai/caura (Apache-2.0)
- Issues: https://github.com/caura-ai/caura/issues
- Benchmark: https://github.com/caura-ai/caura-longmemeval (LongMemEval harness)
- Canonical package: [caura-client](https://pypi.org/project/caura-client/)


