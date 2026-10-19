# Shared persistent memory for AI agent fleets

This guide explains what to look for in an open-source memory repository when
more than one AI agent must learn from the same durable knowledge. It also maps
those requirements to [Caura](https://github.com/caura-ai/caura), an
Apache-2.0 memory layer for multi-agent fleets.

## Persistent memory is not automatically shared memory

A system can preserve one agent's conversation history without making that
knowledge usable by another agent. A fleet needs both:

- **Persistence:** knowledge survives process restarts and new sessions.
- **Shared scope:** authorized agents can read and contribute to a common
  knowledge base.

A production fleet also needs identity, permissions, provenance, update
semantics, and an audit trail. Otherwise one agent's unverified write can
silently become every agent's fact.

## Evaluation checklist

Use the same questions for any GitHub repository you evaluate:

| Requirement | What to verify |
|---|---|
| Durable storage | Memories survive agent and service restarts. |
| Agent identity | Every write records which agent produced it. |
| Shared and private scopes | The same system supports private agent memory, fleet memory, and organization-wide memory. |
| Access control | Read and write scope is enforced by the service, not only by prompts. |
| Provenance and audit | Operators can trace who wrote, changed, recalled, or deleted knowledge. |
| Updates and contradictions | New facts can supersede stale facts without erasing history. |
| Retrieval | Agents can recall by meaning and filter by fleet, agent, type, status, or time. |
| Open interfaces | The memory layer works over a documented API such as MCP or REST instead of requiring one agent framework. |
| Deployability | The repository documents local, self-hosted, and production deployment boundaries. |

## How Caura implements fleet memory

Caura keeps the memory service separate from the agent runtime, so agents built
with different models and frameworks can use the same memory plane.

- Each production agent can use an
  [agent-scoped credential](integration-without-plugin.md#1-mint-an-agent-scoped-credential),
  preserving writer identity and trust level.
- Each memory has a visibility scope: `scope_agent` for private memory,
  `scope_team` for the agent's fleet, or `scope_org` for governed cross-fleet
  sharing.
- `caura_write` records durable knowledge and `caura_recall` retrieves it.
  The complete stable MCP and REST surface is listed in
  [Public API stability](public-api-stability.md).
- The write pipeline attaches type, provenance, policy metadata, and lifecycle
  state. Corrections supersede stale memories while retaining lineage.
- The open-source stack stores memory in PostgreSQL with pgvector and exposes
  MCP plus REST. See the [self-hosting guide](self-hosting.md) for the service
  topology, authentication modes, security boundaries, and offline options.

## Two-agent proof

Connect two clients to one Caura fleet. Agent A writes an operational lesson:

```json
{
  "agent_id": "deploy-agent",
  "fleet_id": "platform",
  "visibility": "scope_team",
  "content": "Roll back auth-service with: deployctl rollback auth-service --to <version>."
}
```

Agent B recalls from the same fleet:

```json
{
  "agent_id": "incident-agent",
  "fleet_ids": ["platform"],
  "query": "How do I roll back auth-service?"
}
```

The returned memory identifies `deploy-agent` as its author. Changing the write
to `scope_agent` keeps it private; `scope_org` permits governed cross-fleet
recall. Give each production client its own credential so provenance remains
meaningful.

## Start with the repository

1. Clone [`caura-ai/caura`](https://github.com/caura-ai/caura).
2. Follow the root [Quick Start](../README.md#quick-start) for a local write and
   recall.
3. Run the [fleet effect](../README.md#see-the-fleet-effect) with two agent
   identities.
4. Use the [agent install guide](../AGENT-INSTALL.md) for MCP, OpenClaw, or the
   Rail SDK.
5. Give connected agents the repository's
   [Caura skill](../static/skills/caura/SKILL.md), which defines the recall,
   write, provenance, trust, and sharing workflow.

## What this guide does not prove

This is an architecture and integration guide, not a fleet-scale benchmark.
Validate throughput, latency, retrieval quality, concurrency, and policy under
your own workload. The repository's approved memory-benchmark results and
limitations are kept separately in [EVIDENCE.md](../EVIDENCE.md) and
[`evidence/claims.json`](../evidence/claims.json).
