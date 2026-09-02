# A37 — N>2 contradiction chains resolve inconsistently

**Status:** defect found while closing A37 ("N>2 / transitive contradictions
untested"). The row asked for coverage; the coverage found a problem.

**Environment:** local full stack, real embeddings (Vertex `gemini-embedding-001`,
1024-dim) and a real judge (`gemini-2.5-flash` via the platform LLM). Prod runs
`gemini-2.5-flash-lite`, so absolute rates may differ — the *inconsistency* is
the finding, not the exact ratio.

## Method

Write three mutually exclusive values for one subject, in order, waiting ~55 s
between writes for async detection to settle, then read all three rows back.

Correct end state: exactly one row `active`; the other two `outdated` /
`conflicted`, joined by `supersedes_id` edges.

## Observed, three runs

| run | subject | A (oldest) | B | C (newest) |
|---|---|---|---|---|
| 1 | deploy window 01:00 → 03:00 → 05:00 | `conflicted` | `conflicted` (sup=A) | `active` (sup=B) |
| 2 | primary oncall Marta → Tomas → Wei | `active` | `active` | `active` |
| 3 | agent pool 4 → 8 → 16 workers | `conflicted` | `active` (sup=A) | `active` |

* **Run 1 is correct** — full chain, one winner.
* **Run 3** leaves B (8 workers) and C (16 workers) both `active`; C never
  superseded B.
* **Run 2** fired nothing at all: three contradictory oncall assignments, all
  live and all retrievable.

**2 of 3 runs end with mutually exclusive claims co-active.**

## Why it matters

A search for the subject returns several contradictory `active` rows with no
signal that they compete. This is the failure A34's ranking contract exists to
prevent, arriving by another route: A34 orders a successor above its stale
predecessor, but here the stale row was never marked stale — so there is no edge
to order by, and nothing looks wrong.

## What this is NOT

Not the pairwise case, which is well covered and behaves. The gap is chains
longer than two, where each new write is judged against candidates whose own
status is still settling from the previous write.

## Likely mechanism (unconfirmed)

Each write's detection races the previous write's status updates. Candidate
queries filter `status IN (active, confirmed, pending)`, so a row mid-transition
can be missed, and the per-memory Redis lock is keyed by
`(memory_id, content_fingerprint)` — it serialises runs for ONE memory, not for
a subject. Run 2 firing nothing at all points at the candidate lookup rather
than the judge.

Confirming this needs the candidate/score breakdown, which is what D12's
diagnostic mode surfaces — the natural next step.

## Reproduce

`benchmark/a37_n_way_chain.py` — three subjects, three runs; reports how often a
stale claim stays live.
