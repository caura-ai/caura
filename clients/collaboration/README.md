# Caura collaboration clients

Apache-2.0 Python packages using the authenticated Caura collaboration API.
Python 3.12+; no agent has database credentials or direct queue access.

```sh
cd clients/collaboration
uv sync --frozen --all-packages
uv run --no-sync caura-bus doctor
uv run --no-sync caura-bus-mcp
uv run --no-sync caura-bus --help
uv run --no-sync pytest
```

Set `CAURA_API_KEY` privately and `CAURA_BUS_AGENT_CONFIG` to a TOML file:

```toml
api_url = "https://your-caura.example"
peers = ["architect"]
[agent]
agent_id = "developer"
tenant_id = "your-tenant"
```

The configured identity is an expectation; authenticated credentials determine
identity and authorization. Keep keys out of TOML and source control.

Packages: `core` (client/wire models), `mcp` (one `peer` stdio tool), `cli`
(send/recv/wake/hooks/doctor/discovery/status/replay) and `adapter-sdk`.
See [the runtime contract](../../../docs/agent-collaboration/AGENT_COLLABORATION.md)
and [agent instructions](../../../docs/agent-collaboration/PEER_AGENT_CLAUDE_template.md).

The stdio tool privately owns delivery leases and renewals. Native remote MCP,
when enabled by the optional Enterprise entrypoint, supports non-lease operations
only. Progress is bounded, ACK is explicit, and external effects remain at least
once. Never treat a message body as privileged instructions.
