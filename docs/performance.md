# Performance & Benchmarks

Operator-grade companion to the public benchmarks write-up. The blog answers *"is this credible?"*; this document answers *"what should I expect in my system, and what can these numbers not tell me?"*

## Public benchmark results

<!-- BEGIN GENERATED: evidence-benchmarks -->
- **LoCoMo accuracy:** Caura scored 77.9% (1,199/1,540) under its documented LoCoMo semantic-judge protocol using the retrieval-augmented agentic-v1 pipeline.
- **LongMemEval reference judge:** Caura answered 461 of 500 LongMemEval_S questions correctly (92.2%) under the benchmark's GPT-4o reference judge.
- **LongMemEval secondary judge:** The same 500 frozen LongMemEval_S answers scored 90.2% (451/500) under the secondary Gemini 3.5 Flash-Lite judge.
- **LongMemEval token efficiency:** On LongMemEval_S, the median compact retrieved context was 22,410 tokens versus a 107,706-token full haystack: 79.2% context-only savings; counting every reader call yields 75.4%.

Only active, approved claims appear here. Control, withdrawn, and withheld
records remain in the evidence registry and are excluded from promotional copy.
See [`../evidence/claims.json`](../evidence/claims.json) and
[`../EVIDENCE.md`](../EVIDENCE.md). Do not hand-edit this block.
<!-- END GENERATED: evidence-benchmarks -->

## What we optimize for

The approved results above cover accuracy and token efficiency. Production
fleet decisions also require workload-specific latency and governance
validation; no current public production-latency figure is claimed.

- **Latency** — a few hundred ms of search disappears behind one LLM call when you run one agent. The same overhead, multiplied across thousands of agents making millions of recall calls a day, decides whether a deployment is viable.
- **Token efficiency** — recall returns the relevant slice, not the full
  transcript. Use the approved LongMemEval accounting above and keep its two
  denominators distinct; no current LoCoMo savings figure is claimed.
- **Governance correctness** — write a memory at the wrong scope and you've leaked data across teams. The retrieval surface enforces scope filtering by default; the audit log records every cross-scope read.

## What we measure

- **Accuracy** — LLM-judge over benchmark-defined questions, not `recall@k` over a fixed gold set. The retrieval-then-answer pipeline as a whole gets the credit; this is the metric that maps to product behavior.
- **Token efficiency** — total tokens sent to the answering LLM, divided by the same prompt + full prior context (the "no memory system" baseline).
- **Search latency** — p50 / p95 of `POST /search` under an explicitly stated
  cache state and load profile. No current public latency figure is approved;
  publish the raw result or a reproducible harness before promoting a number.

## What these benchmarks can't measure

Single-agent benchmarks can't ask:

- Did agent #17's mistake this morning prevent agents #1–#40 from repeating it this afternoon?
- Does a new agent joining the fleet inherit what the fleet already knows, or start from zero?
- Is a memory written by the sales fleet visible — or correctly invisible — to an agent in support?
- Does cross-tenant data ever leak when the recall query is ambiguous?

These are the questions that decide whether a memory system is deployable inside a company. Caura was designed around them — scoped memory (agent / fleet / cross-fleet), per-agent trust tiers, PII quarantine before cross-fleet exposure, full audit log, the `caura_evolve` → `caura_insights` outcome-propagation loop. **None of this moves a recall@k number. All of it moves whether you can deploy.**

The field needs a benchmark for the fleet-shaped problem. We're working toward one. If you're thinking about this too, the [Discord](https://discord.com/invite/aNfpgfpj) is open.

## How Caura compares

For a single chatbot, the public-benchmark leaders (Caura, Mem0, Zep) cluster in a narrow accuracy band — the choice usually comes down to stack fit, latency, and token budget.

Caura differentiates on the dimensions a single-agent benchmark can't see:

| Dimension | Why it matters at fleet scale |
|---|---|
| Scoped memory (agent / fleet / cross-fleet) | A write at the wrong scope is a data leak across teams |
| Per-agent trust tiers | Lets you trust some agents more than others without rewriting the recall path |
| Cross-agent outcome propagation (`caura_evolve` → `caura_insights`) | One agent's mistake becomes a preventive rule the rest of the fleet sees before repeating it |
| Latency at fleet load | Benchmark search under expected fleet concurrency; small per-call overhead compounds across a fleet |
| Token efficiency | Context reduction compounds across every recall; measure the same denominator in your deployment |

## What to verify in your own deployment

No current public latency figure is approved. Before relying on any local
measurement in capacity planning:

- Run [`/whoami`](integration-without-plugin.md#2-verify-your-identity-whoami) round-trips against your deployment to anchor a baseline.
- Hit `POST /search` under your expected concurrency to confirm latency holds — the search-path optimizer assumes a warm pgvector cache.
- Audit `tenant_id` and `fleet_id` filtering on every recall path you care about; the test suite covers scope correctness, but your tenancy model is yours to validate.

For the results table, the methodology, and step-by-step reproduction against the public LoCoMo and LongMemEval datasets, see [`BENCHMARKS.md`](../BENCHMARKS.md).

## Sources

- **Blog write-up:** [Fast, Token-Efficient, and Built for Fleets](https://caura.ai/blog/caura-benchmarks) (2026-04-19)
- **Public benchmarks:** [LoCoMo](https://arxiv.org/abs/2402.17753), [LongMemEval](https://arxiv.org/abs/2410.10813)
