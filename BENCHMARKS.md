# Caura Benchmarks

How Caura (formerly MemClaw) performs on the two most-cited public agent-memory benchmarks — <!-- legacy-name-ok: taught as legacy alias -->
**LoCoMo** and **LongMemEval** — plus the fleet-shaped dimensions those
single-agent benchmarks can't measure.

> **TL;DR** — On accuracy, Caura sits inside the leading cluster (Caura,
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
Caura is built around these — scoped memory (agent / fleet / cross-fleet),
per-agent trust tiers, keystone policies, PII quarantine before cross-fleet
exposure, a full audit log, and the `caura_evolve` → `caura_insights`
outcome-propagation loop. The field still needs a benchmark for the
fleet-shaped problem; we're working toward one.

## Reproduce it yourself

The accuracy and token-efficiency numbers run against the **public** datasets,
so you can reproduce the methodology against your own Caura instance:

1. **Stand up Caura locally** — follow the [Quick Start](README.md#quick-start)
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

> The end-to-end accuracy/token harness isn't bundled yet — the datasets are
> large and publicly hosted, and the runner is being prepared for open release.
> Until then the steps above describe the exact methodology. Operator-scale
> guidance and caveats live in [`docs/performance.md`](docs/performance.md).

### Reranking, specifically

One piece **is** bundled:
[`scripts/benchmark_rerank_locomo.py`](scripts/benchmark_rerank_locomo.py)
answers "does second-stage reranking order results better than first-stage
similarity alone?" on LoCoMo. It scores the *same* candidate pool two ways —
embedding cosine vs that pool reordered by a `/rerank` sidecar — against each
question's `evidence` turns, so recall is identical by construction and the
only variable is ordering. Reports nDCG@5/@10, MRR and P@5 with paired
bootstrap confidence intervals and a sign test, overall and per LoCoMo
category. Stdlib only, no cloud dependencies:

```bash
python scripts/benchmark_rerank_locomo.py \
    --dataset locomo10.json \
    --embed-url http://localhost:8080 \
    --rerank-url http://localhost:8081
```

Note this isolates the reranker: the baseline is pure cosine, whereas the real
first-stage also applies freshness decay, weight blending and recall boost. It
answers "is the cross-encoder worth its latency", not "what is the end-to-end
pipeline delta".

### First-stage retrieval, specifically

[`scripts/benchmark_blend_locomo.py`](scripts/benchmark_blend_locomo.py) is the
companion one stage earlier: it varies the **first-stage** score, so which turns
are retrieved at all is what moves, and **recall** is the headline rather than an
invariant. It A/Bs the keyword half of

```
similarity = (1 - FTS_WEIGHT) * vec_sim + FTS_WEIGHT * fts_score
```

by rescaling `ts_rank_cd` before saturation (`--k`, defaulting to what
production ships; `k=1` is the **pre-#687** `r/(1+r)` formula), ranking the
*whole* corpus in both arms. Reports
recall@5/@10/@20, nDCG@10 and MRR with the same paired bootstrap CI and sign
test, overall, per category, and — separately — over just the queries where
`plainto_tsquery` matched anything, since the rest are a mathematical no-op that
only dilutes the average.

Unlike the rerank harness it needs a Postgres, because `ts_rank_cd` is the thing
under test and reimplementing Postgres FTS in Python would measure something
else. Any empty database works; it uses a `TEMP` table and never touches
application tables.

```bash
python scripts/benchmark_blend_locomo.py \
    --dataset locomo10.json \
    --embed-url http://localhost:8080 \
    --pg-dsn postgresql://memclaw:changeme@localhost:5433/memclaw \
    --k 6
```

`k=1` asserts a delta of exactly zero on every metric — a self-check that the
harness isn't manufacturing differences the change cannot cause. Since #687 shipped
`FTS_RANK_SCALE`, `k=1` is a *historical* arm: an A/B from it measures the delta
since before that change, not a delta against what production runs today.

Two limits worth knowing before quoting a number from it. Only ~17% of LoCoMo
questions lexically match any turn (they are paraphrases), so the overall delta is
diluted by a large majority of queries the change cannot affect — read the
`fts-matched-only` block for the effect on queries it applies to. And the measured
effect rises monotonically with `k` across the range tested, so this benchmark can
say whether more keyword weight helps on LoCoMo, but it does **not** locate an
optimum; don't read the largest `k` as the best one.

## How Caura compares

For a single chatbot, the public-benchmark leaders (Caura, Mem0, Zep) cluster
in a narrow accuracy band — the choice usually comes down to stack fit, latency,
and token budget. Caura differentiates on the dimensions a single-agent
benchmark can't see; see [`docs/performance.md`](docs/performance.md#how-caura-compares)
for the fleet-scale breakdown and the [feature comparison](README.md#how-caura-compares)
in the README.

## Sources

- **Blog write-up (canonical, current):** [Fast, Token-Efficient, and Built for Fleets](https://caura.ai/blog/caura-benchmarks) (2026-04-19)
- **Operator companion:** [`docs/performance.md`](docs/performance.md)
- **Public benchmarks:** [LoCoMo](https://arxiv.org/abs/2402.17753) · [LongMemEval](https://arxiv.org/abs/2410.10813)
