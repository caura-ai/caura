# Performance & Benchmarks

Operator-grade companion to the public benchmarks write-up. The blog answers *"is this credible?"*; this document answers *"what should I expect in my system, and what can these numbers not tell me?"*

## Public benchmark results

|  | LoCoMo | LongMemEval | Search latency |
|---|---|---|---|
| Accuracy (LLM-judge) | **77.6%** | **72.5%** | — |
| Token savings vs full context | **96.6%** | **98.2%** | — |
| Latency | — | — | **23 ms p50 · 27 ms p95** (warm) |

LoCoMo and LongMemEval are the two most-cited public agent-memory benchmarks. Both measure one agent, one user, one long conversation — the single-chatbot shape. Accuracy across the leading systems (MemClaw, Mem0, Zep) clusters in a narrow band.

**Source:** [Fast, Token-Efficient, and Built for Fleets](https://memclaw.net/blog/memclaw-benchmarks) (2026-04-19).

**Last updated:** 2026-04-19. These numbers move when we re-run; check the blog for the current canonical version.

## What we optimize for

Accuracy sits inside the leading cluster. That's not the axis we push hardest along.

- **Latency** — a few hundred ms of search disappears behind one LLM call when you run one agent. The same overhead, multiplied across thousands of agents making millions of recall calls a day, decides whether a deployment is viable.
- **Token efficiency** — recall returns the relevant slice, not the full transcript. Token savings vs sending the full context to the LLM: 96–98% on the two benchmarks. That ratio is the bill at fleet scale.
- **Governance correctness** — write a memory at the wrong scope and you've leaked data across teams. The retrieval surface enforces scope filtering by default; the audit log records every cross-scope read.

## What we measure

- **Accuracy** — LLM-judge over benchmark-defined questions, not `recall@k` over a fixed gold set. The retrieval-then-answer pipeline as a whole gets the credit; this is the metric that maps to product behavior.
- **Token efficiency** — total tokens sent to the answering LLM, divided by the same prompt + full prior context (the "no memory system" baseline).
- **Search latency** — p50 / p95 of `POST /search` against a warm cache, single-tenant load. Cold-cache p50 is higher; we publish warm because that's the steady-state condition under real load.

## What these benchmarks can't measure

Single-agent benchmarks can't ask:

- Did agent #17's mistake this morning prevent agents #1–#40 from repeating it this afternoon?
- Does a new agent joining the fleet inherit what the fleet already knows, or start from zero?
- Is a memory written by the sales fleet visible — or correctly invisible — to an agent in support?
- Does cross-tenant data ever leak when the recall query is ambiguous?

These are the questions that decide whether a memory system is deployable inside a company. MemClaw was designed around them — scoped memory (agent / fleet / cross-fleet), per-agent trust tiers, PII quarantine before cross-fleet exposure, full audit log, the `memclaw_evolve` → `memclaw_insights` outcome-propagation loop. **None of this moves a recall@k number. All of it moves whether you can deploy.**

The field needs a benchmark for the fleet-shaped problem. We're working toward one. If you're thinking about this too, the [Discord](https://discord.com/invite/aNfpgfpj) is open.

## How MemClaw compares

For a single chatbot, the public-benchmark leaders (MemClaw, Mem0, Zep) cluster in a narrow accuracy band — the choice usually comes down to stack fit, latency, and token budget.

MemClaw differentiates on the dimensions a single-agent benchmark can't see:

| Dimension | Why it matters at fleet scale |
|---|---|
| Scoped memory (agent / fleet / cross-fleet) | A write at the wrong scope is a data leak across teams |
| Per-agent trust tiers | Lets you trust some agents more than others without rewriting the recall path |
| Cross-agent outcome propagation (`memclaw_evolve` → `memclaw_insights`) | One agent's mistake becomes a preventive rule the rest of the fleet sees before repeating it |
| Latency at fleet load | 23 ms p50 search × millions of calls/day stays affordable; 250 ms doesn't |
| Token efficiency | 96–98% savings vs full context is the bill, not a microbenchmark curiosity |

## What to verify in your own deployment

The published numbers are warm-cache, single-tenant, on our reference hardware. Before relying on them in capacity planning:

- Run [`/whoami`](integration-without-plugin.md#2-verify-your-identity-whoami) round-trips against your deployment to anchor a baseline.
- Hit `POST /search` under your expected concurrency to confirm latency holds — the search-path optimizer assumes a warm pgvector cache.
- Audit `tenant_id` and `fleet_id` filtering on every recall path you care about; the test suite covers scope correctness, but your tenancy model is yours to validate.

For the results table, the methodology, and step-by-step reproduction against the public LoCoMo and LongMemEval datasets, see [`BENCHMARKS.md`](../BENCHMARKS.md).

## Sources

- **Blog write-up:** [Fast, Token-Efficient, and Built for Fleets](https://memclaw.net/blog/memclaw-benchmarks) (2026-04-19)
- **Public benchmarks:** [LoCoMo](https://arxiv.org/abs/2402.17753), [LongMemEval](https://arxiv.org/abs/2410.10813)
