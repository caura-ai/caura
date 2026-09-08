# Documentation index

Operator and integrator guides that ship with the repo. Start at the root
[`README.md`](../README.md) for what Caura is and a first run;
[`AGENT-INSTALL.md`](../AGENT-INSTALL.md) is the shortest path to a connected
agent.

## Operating a deployment

- [`self-hosting.md`](self-hosting.md) — the complete self-hosted guide:
  provider configuration, authentication modes, service topology, security,
  offline operation, and tests.
- [`local-embedder.md`](local-embedder.md) — serve embeddings from a TEI
  sidecar instead of a hosted API, for semantic search with no cloud calls.
- [`performance.md`](performance.md) — the benchmark numbers at operator scale:
  what to expect in your own system, and what the numbers cannot tell you.
- [`operator-forge-cron.md`](operator-forge-cron.md) — driving the Skill
  Factory's Forge and promoter ticks from an external scheduler.

## Building against the API

- [`api-reference.md`](api-reference.md) — curated map of the REST and MCP
  routes; a running deployment's own OpenAPI schema stays authoritative.
- [`api-surfaces.md`](api-surfaces.md) — which operations belong on REST, MCP,
  or the OpenClaw plugin, and who owns each surface.
- [`public-api-stability.md`](public-api-stability.md) — the SemVer contract:
  which surfaces are stable, and which are internal and free to change.
- [`integration-without-plugin.md`](integration-without-plugin.md) — for
  developers building an SDK client against a deployment with no plugin
  runtime installed.
- [`skills-inbox-api.md`](skills-inbox-api.md) — the operator-facing REST API
  behind the human-in-the-loop review of Skill Factory candidates.

## Upgrading

- [`upgrading-from-v1.md`](upgrading-from-v1.md) — the v1.x to v2.x server
  upgrade, including the destructive 768 → 1024 embedding migration and the
  re-embed paths after it.
- [`plugin-upgrade.md`](plugin-upgrade.md) — for operators upgrading the plugin
  on OpenClaw nodes: when heartbeat-driven auto-upgrade runs, and the manual
  re-install for the cases where it does not.

## Skills over MCP

- [`mcp-skill-delivery.md`](mcp-skill-delivery.md) — how a skill reaches an MCP
  client (a read, not a push), and the rule that gates it.

## Also in this directory

- [`component-registry.yaml`](component-registry.yaml) — checked-in inventory of
  the shipped cron ticks; `tests/test_component_registry.py` turns drift between
  it and the scheduler into a test failure.
- `contradiction-verification/`, `observability/`, `plans/` — findings,
  measurement contracts, and decision records. Working notes, not guides.
