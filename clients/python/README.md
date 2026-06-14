# memclaw-client

Official Python client for [MemClaw](https://memclaw.net) — governed shared
memory for AI agent fleets (multi-agent, multi-tenant, MCP-native).

A thin wrapper over the MemClaw REST API. Point it at a managed
(`https://memclaw.net`) or self-hosted (`http://localhost:8000`) deployment.

## Install

```bash
pip install memclaw-client
```

## Quickstart

```python
from memclaw_client import MemClaw

mc = MemClaw("mc_xxx", tenant_id="my-team", agent_id="my-agent")

# Write a memory — enriched server-side with type, title, tags, importance.
mc.write("Q3 revenue target is $4M, set on 2026-04-15.")

# Search (ranked raw results)
for m in mc.search("Q3 revenue target", top_k=5):
    print(m.title, "—", m.content)

# Recall (LLM-synthesized context brief)
print(mc.recall("Q3 revenue target").summary)
```

Self-hosted? Pass `base_url`:

```python
mc = MemClaw("standalone", tenant_id="default", base_url="http://localhost:8000")
```

## API

| Method | Endpoint | Returns |
|---|---|---|
| `write(content, ...)` | `POST /api/v1/memories` | `Memory` |
| `search(query, top_k=5, ...)` | `POST /api/v1/search` | `list[Memory]` |
| `recall(query, top_k=5, ...)` | `POST /api/v1/recall` | `RecallResult` |
| `health()` | `GET /api/v1/health` | `dict` |

The client is a context manager (`with MemClaw(...) as mc:`) and raises
`AuthError` (401/403), `NotFoundError` (404), or `MemClawAPIError` on failures.
Every result also exposes the full API payload on `.raw`.

For credentials, scopes, and the full API surface, see the
[MemClaw docs](https://memclaw.net/docs). Production fleets should use
[per-agent keys](https://memclaw.net/docs/integrations/per-agent-keys).

## License

Apache-2.0
