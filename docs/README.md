# Docs index

Short map of the markdown guides under `docs/`. Grouped the way the repo's
`area/*` labels already group the work.

## Operators

| Doc | What it's for | Who it's for |
|---|---|---|
| [`self-hosting.md`](./self-hosting.md) | Docker Compose self-host path: providers, auth, topology | Operators standing up or running Caura on their own infra |
| [`local-embedder.md`](./local-embedder.md) | TEI sidecar for self-hosted `BAAI/bge-m3` embeddings | Operators who want local semantic search instead of hosted OpenAI |
| [`performance.md`](./performance.md) | Operator-scale reading of the public benchmarks | Operators sizing latency/throughput expectations |
| [`operator-forge-cron.md`](./operator-forge-cron.md) | Scheduling the Skill Factory Forge + promoter cron tick | Operators enabling automated skill mining |

## Integrators

| Doc | What it's for | Who it's for |
|---|---|---|
| [`api-reference.md`](./api-reference.md) | Curated REST/MCP endpoint groups, auth, config | Integrators calling Caura over HTTP |
| [`api-surfaces.md`](./api-surfaces.md) | Ownership charter: what belongs on REST vs MCP vs plugin | Integrators choosing which surface to use |
| [`public-api-stability.md`](./public-api-stability.md) | SemVer stability contract for public surfaces | Integrators depending on forward-compatible APIs |
| [`integration-without-plugin.md`](./integration-without-plugin.md) | SDK/client integration without the OpenClaw plugin | Developers building Python/Node (or other) clients |
| [`skills-inbox-api.md`](./skills-inbox-api.md) | Skills Inbox REST API (human-in-the-loop Skill Factory) | Operators/integrators driving skill review via API |

## Upgrades

| Doc | What it's for | Who it's for |
|---|---|---|
| [`upgrading-from-v1.md`](./upgrading-from-v1.md) | Destructive v1.x → v2.x schema/embedding migration | Operators upgrading a stored-memory install to v2 |
| [`plugin-upgrade.md`](./plugin-upgrade.md) | OpenClaw plugin auto-upgrade (heartbeat) and manual `/api/v1/install-plugin` re-install | Operators running the memclaw plugin against a Caura server |

## Plugin / skills

| Doc | What it's for | Who it's for |
|---|---|---|
| [`mcp-skill-delivery.md`](./mcp-skill-delivery.md) | How a Forge-produced (or human-authored) skill reaches an agent over MCP | Integrators and operators wiring MCP skill delivery |
