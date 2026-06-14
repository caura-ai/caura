# MemClaw Benchmarks

How MemClaw performs on the two most-cited public agent-memory benchmarks —
**LoCoMo** and **LongMemEval** — plus the fleet-shaped dimensions those
single-agent benchmarks can't measure.

> **TL;DR** — On accuracy, MemClaw sits inside the leading cluster (MemClaw,
> Mem0, Zep land in a narrow band). Where we push hardest, and where it
> compounds at fleet scale, is **latency, token efficiency, and governance
> correctness**.

## Results

|  | LoCoMo | LongMemEval | Search latency |
|---|---|---|---|
| Accuracy (LLM-judge) | **77.6%** | **72.5%** | — |
| Token savings vs full context | **96.6%** | **98.2%** | — |
| Latency | — | — | **23 ms p50 · 27 ms p95** (warm) |

LoCoMo and LongMemEval both measure one agent, one user, one long
conversation — the single-chatbot shape. Accuracy across the leading systems
clusters in a narrow band, so the meaningful differences show up on the other
axes.

**Numbers are point-in-time (last run 2026-04-19) and move when we re-run.** The
canonical, current version lives in the blog write-up linked below.

## What we measure, and how

- **Accuracy** — an LLM judge scores the answer the retrieval-then-answer
  pipeline produces against each benchmark's question, rather than `recall@k`
  over a fixed gold set. The whole pipeline gets the credit, because that's what
  maps to product behavior.
- **Token efficiency** — total tokens sent to the answering LLM divided by the
  same prompt with the full prior conversation inlined (the "no memory system"
  baseline). The ratio, not the absolute count, is what scales into your bill.
- **Search latency** — p50 / p95 of `POST /api/v1/search` against a warm
  pgvector cache, single-tenant. Cold-cache p50 is higher; we publish warm
  because that's the steady state under real load.

## What these benchmarks can't measure

Single-agent benchmarks can't ask the questions that decide whether a memory
system is deployable inside a company:

- Did agent #17's mistake this morning stop agents #1–#40 from repeating it this
  afternoon? (cross-agent outcome propagation)
- Does a new agent joining the fleet inherit what the fleet already knows?
- Is a memory written by the sales fleet correctly **invisible** to a support
  agent? (scoped visibility)
- Does cross-tenant data ever leak when the recall query is ambiguous?

None of this moves a `recall@k` number; all of it moves whether you can deploy.
MemClaw is built around these — scoped memory (agent / fleet / cross-fleet),
per-agent trust tiers, keystone policies, PII quarantine before cross-fleet
exposure, a full audit log, and the `memclaw_evolve` → `memclaw_insights`
outcome-propagation loop. The field still needs a benchmark for the
fleet-shaped problem; we're working toward one.

## Reproduce it yourself

The accuracy and token-efficiency numbers run against the **public** datasets,
so you can reproduce the methodology against your own MemClaw instance:

1. **Stand up MemClaw locally** — follow the [Quick Start](README.md#quick-start)
   (`docker compose up -d`). Set an embedding/LLM provider key so memories are
   embedded and enriched (standalone dummy-embedding mode runs but won't produce
   meaningful recall).
2. **Get the datasets** — [LoCoMo](https://arxiv.org/abs/2402.17753) and
   [LongMemEval](https://arxiv.org/abs/2410.10813) are publicly available.
3. **Ingest** — for each conversation, write its turns with
   `POST /api/v1/memories` (one memory per turn / fact).
4. **Query** — for each benchmark question, call `POST /api/v1/search` and pass
   the retrieved memories to your answering LLM.
5. **Score** — judge each answer against the benchmark's expected answer with an
   LLM judge, and compute token usage against the full-context baseline.
6. **Latency** — measure p50/p95 of `POST /api/v1/search` under your expected
   concurrency against a warm cache.

> A turnkey harness isn't bundled in this repo yet — the datasets are large and
> publicly hosted, and the runner is being prepared for open release. Until
> then the steps above describe the exact methodology. Operator-scale guidance
> and caveats live in [`docs/performance.md`](docs/performance.md).

## How MemClaw compares

For a single chatbot, the public-benchmark leaders (MemClaw, Mem0, Zep) cluster
in a narrow accuracy band — the choice usually comes down to stack fit, latency,
and token budget. MemClaw differentiates on the dimensions a single-agent
benchmark can't see; see [`docs/performance.md`](docs/performance.md#how-memclaw-compares)
for the fleet-scale breakdown and the [feature comparison](README.md#how-memclaw-compares)
in the README.

## Sources

- **Blog write-up (canonical, current):** [Fast, Token-Efficient, and Built for Fleets](https://memclaw.net/blog/memclaw-benchmarks) (2026-04-19)
- **Operator companion:** [`docs/performance.md`](docs/performance.md)
- **Public benchmarks:** [LoCoMo](https://arxiv.org/abs/2402.17753) · [LongMemEval](https://arxiv.org/abs/2410.10813)
