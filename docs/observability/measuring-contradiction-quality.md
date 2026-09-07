# Measuring contradiction quality without lying to yourself

**D4.** The contradiction-quality dashboards under-report, because they sample
too soon after the write. This is how to measure it correctly. The dashboards
themselves live in Datadog, not in this repo — this is the query contract they
should implement.

## Why fixed-delay sampling under-reports

Contradiction detection is **post-commit and asynchronous**, on both routes:

* the content route runs after the write returns;
* the entity route runs only after entity extraction has resolved subjects,
  which is a second async hop and involves an LLM call.

A write returns `201` long before either has concluded. Measured locally, a
single detection run takes **4–15 s** on Gemini, and the entity route cannot
start until extraction finishes. Anything sampling at "write + a few seconds"
counts a verdict that has not happened yet and records it as *no contradiction*.

That failure is silent and biased in one direction: it can only ever
**under**-count conflicts, never over-count, so the dashboard looks healthy
precisely when detection is slow or failing.

## Two correct approaches

### 1. Event-arrival (preferred)

Since D3, every concluded run logs — including runs that found nothing, which
is exactly the case the old log dropped:

```
Async contradiction detection completed for memory <id>: <n> conflict(s)
  path=contradiction-detection  memory_id=<id>  conflicts_found=<n>
```

Count outcomes from **this** event, not from the write. It is emitted once per
concluded run, carries the verdict count, and its absence is itself meaningful:
a write with no matching completion event means detection never concluded (lock
contention, an early return, a crashed worker) — which is a different failure
from "found nothing", and the two were previously indistinguishable.

Suggested shape:

* `conflicts_found > 0` / all completions — detection **yield**
* writes with no completion event within the window — detection **loss**

Keep those two separate. Collapsing them is what made the old dashboard
unreadable.

### 2. Fixed delay, if you must

If a query cannot join on the completion event, sample at **≥ 60 s** after the
write. That is not a tuning knob — it is above the observed p99 for
extraction + detection with an LLM in the path. Below it, the measurement is
dominated by how fast the provider answered rather than by detection quality.

## What NOT to use as a quality signal

`memory_compute_health_stats`'s `contradiction_count` is a **stock** measure —
how many rows are currently `outdated`/`conflicted` — not a rate. It answers
"how much of this corpus is superseded", which is a useful hygiene number and a
misleading quality number: it moves with corpus age and write volume, not with
whether detection is working.

## Verifying a dashboard change

`benchmark/a66_subject_stability.py` writes a known 3-value chain and reports the
resolved statuses. A correct dashboard shows the same verdicts that script
observes; if they disagree, the dashboard's window is the thing that is wrong.
