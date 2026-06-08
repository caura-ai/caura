#!/usr/bin/env python3
"""S2 — Enrichment-lag race probe (Layer-3 hunt).

Hypothesis (from loadtest 1779803579 + S1 finding):
  Contradiction detection for proper-noun subjects rides on the
  entity-extraction worker that retroactively populates ``entity_links``.
  The loadtest reports ``embedding_pending: true, enrichment_pending: true``
  even after a 200/201 ack, and read-your-writes p95 is 2876ms. So if
  two contradicting writes arrive within that enrichment window, the
  second write may hit the contradiction comparator BEFORE the first
  write's entity_links are populated — and the conflict is silently
  missed. Production chat / OpenClaw agents emit at sub-second
  intervals; the loadtest itself only probes single-pair contradiction
  with multi-second spacing, so this gap is uncovered.

Method:
  Sweep inter-write delays:
      gap=0ms     (back-to-back, asyncio.gather)
      gap=100ms
      gap=500ms
      gap=2000ms

  For each trial:
    1. Mint a fresh proper-noun subject (collision-free across trials).
    2. Issue write A, sleep `gap` ms, issue write B.
    3. Poll /contradictions on both memories every 500ms for up to 60s.
    4. Record:
        - detected:       True iff either direction ever surfaces the other
        - detect_after_ms: ms from write B ack until first detection
        - final_status_a / final_status_b
        - final_entity_link_count

  A clean Layer-3 signal looks like:
      gap=0ms      → detected=False  (worker race lost)
      gap=100ms    → detected=False  or detected only after large delay
      gap=500ms    → detected=True   with low-ish latency
      gap=2000ms   → detected=True   ~ baseline

  If all gaps detect, S2 is closed. If 0ms misses but 2000ms hits, that's
  the gap (file CAURA-128 around enrichment ordering / write-blocking
  on entity extraction).

Usage:
    export MEMCLAW_API_URL=https://memclaw.net
    export MEMCLAW_API_KEY=mc_...
    export MEMCLAW_TENANT_ID=ran-test
    python scripts/repro_contradictions_race.py

    # poll longer if your env is slow
    python scripts/repro_contradictions_race.py --poll-secs 90
"""

from __future__ import annotations

import argparse
import json
import os
import random
import string
import sys
import time
import uuid

import httpx

_STEMS = (
    "Lumenwave",
    "Trident",
    "Helios",
    "Aerolith",
    "Quillforge",
    "Nimbus",
    "Pyrostella",
    "Brightwood",
    "Wyvern",
    "Citrine",
)


def _mint_proper_noun() -> str:
    stem = random.choice(_STEMS)
    tail = "".join(random.choices(string.ascii_lowercase, k=6))
    return f"Project {stem}{tail.capitalize()}"


def _env(name: str) -> str:
    val = os.environ.get(name)
    if not val:
        print(f"ERROR: ${name} must be set", file=sys.stderr)
        sys.exit(2)
    return val


def _detected(target_id: str, resp: dict) -> bool:
    items = resp.get("contradictions") or resp.get("items") or []
    return any(
        (it.get("memory_id") == target_id) or (it.get("id") == target_id)
        for it in items
    )


def _run_trial(
    *,
    client: httpx.Client,
    common: dict,
    gap_ms: int,
    poll_secs: int,
    verbose: bool,
) -> dict:
    subject = _mint_proper_noun()
    print(f"\n── gap={gap_ms:>5}ms  subject={subject!r} ──")

    body_a = {**common, "content": f"{subject} has release date 2027-05-01."}
    body_b = {**common, "content": f"{subject} has release date 2028-10-15."}

    t0 = time.monotonic()
    r = client.post("/api/v1/memories", json=body_a)
    r.raise_for_status()
    mem_a = r.json()
    if gap_ms > 0:
        time.sleep(gap_ms / 1000.0)
    r = client.post("/api/v1/memories", json=body_b)
    r.raise_for_status()
    mem_b = r.json()
    t_ack = time.monotonic()
    print(
        f"   wrote A={mem_a['id'][:8]} B={mem_b['id'][:8]}  "
        f"write_window={(t_ack - t0) * 1000:.0f}ms"
    )

    # Poll loop — transient HTTP errors here log a warning and continue;
    # we don't want a single 5xx mid-poll to abort a multi-trial run.
    qp = {"tenant_id": common.get("tenant_id") or mem_a.get("tenant_id")}
    detect_after_ms: float | None = None
    a_now, b_now = {}, {}
    a_contra, b_contra = {}, {}
    deadline = time.monotonic() + poll_secs
    while time.monotonic() < deadline:
        try:
            r = client.get(f"/api/v1/memories/{mem_a['id']}/contradictions", params=qp)
            r.raise_for_status()
            a_contra = r.json()
            r = client.get(f"/api/v1/memories/{mem_b['id']}/contradictions", params=qp)
            r.raise_for_status()
            b_contra = r.json()
        except Exception as e:
            print(f"   ! poll GET error (continuing): {e}")
            time.sleep(0.5)
            continue
        if _detected(mem_b["id"], a_contra) or _detected(mem_a["id"], b_contra):
            detect_after_ms = (time.monotonic() - t_ack) * 1000.0
            break
        time.sleep(0.5)
    # Final memory state — raise on error here so a broken read at
    # the verdict step doesn't silently produce a misleading trial
    # result.
    r = client.get(f"/api/v1/memories/{mem_a['id']}", params=qp)
    r.raise_for_status()
    a_now = r.json()
    r = client.get(f"/api/v1/memories/{mem_b['id']}", params=qp)
    r.raise_for_status()
    b_now = r.json()

    # Detection can also surface as status=conflicted or supersedes_id chain
    status_hit = (a_now.get("status") in ("outdated", "conflicted")) or (
        b_now.get("status") in ("outdated", "conflicted")
    )
    chain_hit = (a_now.get("supersedes_id") == mem_b["id"]) or (
        b_now.get("supersedes_id") == mem_a["id"]
    )
    contra_hit = detect_after_ms is not None
    detected = contra_hit or status_hit or chain_hit

    el_a = len(a_now.get("entity_links") or [])
    el_b = len(b_now.get("entity_links") or [])

    print(
        f"   detected={detected}  "
        f"detect_after_ms={detect_after_ms!r}  "
        f"status_a={a_now.get('status')}  status_b={b_now.get('status')}  "
        f"entity_links={el_a}/{el_b}"
    )
    if verbose:
        print(f"   A.contradictions: {json.dumps(a_contra, default=str)[:200]}")
        print(f"   B.contradictions: {json.dumps(b_contra, default=str)[:200]}")

    return {
        "gap_ms": gap_ms,
        "subject": subject,
        "id_a": mem_a["id"],
        "id_b": mem_b["id"],
        "detected": detected,
        "detect_after_ms": detect_after_ms,
        "status_a": a_now.get("status"),
        "status_b": b_now.get("status"),
        "supersedes_chain": chain_hit,
        "entity_links_a": el_a,
        "entity_links_b": el_b,
        "contra_path": "endpoint"
        if contra_hit
        else ("status_only" if status_hit else ("chain_only" if chain_hit else None)),
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument(
        "--gaps",
        default="0,100,500,2000",
        help="comma-separated inter-write gaps in ms",
    )
    ap.add_argument(
        "--poll-secs",
        type=int,
        default=60,
        help="how long to wait for detection per trial",
    )
    ap.add_argument(
        "--trials",
        type=int,
        default=1,
        help="trials per gap (default 1; use 5+ to measure variance)",
    )
    ap.add_argument("--write-mode", default="fast", choices=("fast", "strong"))
    ap.add_argument("--verbose", "-v", action="store_true")
    args = ap.parse_args()

    base = _env("MEMCLAW_API_URL").rstrip("/")
    key = _env("MEMCLAW_API_KEY")
    tenant = _env("MEMCLAW_TENANT_ID")

    agent = f"repro-race-{uuid.uuid4().hex[:6]}"
    common = {
        "tenant_id": tenant,
        "agent_id": agent,
        "memory_type": "fact",
        "visibility": "scope_team",
        "write_mode": args.write_mode,
    }

    print(f"env        : {base}")
    print(f"tenant     : {tenant}")
    print(f"write_mode : {args.write_mode}")
    print(f"poll_secs  : {args.poll_secs}s  per trial")
    print(f"gaps_ms    : {args.gaps}")

    headers = {"X-API-Key": key, "Content-Type": "application/json"}
    client = httpx.Client(base_url=base, headers=headers, timeout=90.0)

    gaps = [int(g.strip()) for g in args.gaps.split(",") if g.strip()]
    trials = []
    for g in gaps:
        for t in range(args.trials):
            print(f"\n[gap={g}ms trial {t + 1}/{args.trials}]")
            trials.append(
                _run_trial(
                    client=client,
                    common=common,
                    gap_ms=g,
                    poll_secs=args.poll_secs,
                    verbose=args.verbose,
                )
            )

    # Per-gap aggregation
    print("\n" + "=" * 70)
    print("PER-GAP AGGREGATE")
    print("=" * 70)
    print(f"  {'gap_ms':>7}  {'detect':>10}  {'rate':>6}  {'mean_after_ms':>13}")
    by_gap: dict[int, list] = {}
    for t in trials:
        by_gap.setdefault(t["gap_ms"], []).append(t)
    for g in gaps:
        bucket = by_gap.get(g, [])
        det = [t for t in bucket if t["detected"]]
        n_det, n_tot = len(det), len(bucket)
        rate = (n_det / n_tot * 100.0) if n_tot else 0.0
        afters = [t["detect_after_ms"] for t in det if t["detect_after_ms"] is not None]
        mean_after = (sum(afters) / len(afters)) if afters else None
        mean_str = f"{mean_after:.0f}" if mean_after is not None else "—"
        print(f"  {g:>7}  {n_det:>4}/{n_tot:<4}  {rate:>5.0f}%  {mean_str:>13}")

    print("\n" + "=" * 70)
    print("VERDICT TABLE (per-trial)")
    print("=" * 70)
    print(
        f"  {'gap_ms':>7}  {'detected':>9}  {'after_ms':>10}  "
        f"{'status_a':>11}  {'status_b':>11}  {'links_a/b':>10}  path"
    )
    for t in trials:
        after = (
            f"{t['detect_after_ms']:.0f}" if t["detect_after_ms"] is not None else "—"
        )
        print(
            f"  {t['gap_ms']:>7}  {str(t['detected']):>9}  {after:>10}  "
            f"{str(t['status_a']):>11}  {str(t['status_b']):>11}  "
            f"{t['entity_links_a']}/{t['entity_links_b']:<8}  "
            f"{t['contra_path'] or '—'}"
        )

    n_total = len(trials)
    n_missed = sum(1 for t in trials if not t["detected"])
    short_misses = [t for t in trials if not t["detected"] and t["gap_ms"] <= 500]
    long_hits = [t for t in trials if t["detected"] and t["gap_ms"] >= 500]

    print("\nSummary:")
    print(f"  trials       : {n_total}")
    print(f"  missed       : {n_missed}")
    print(f"  short misses : {len(short_misses)}  (gap ≤ 500ms)")
    print(f"  long  hits   : {len(long_hits)}     (gap ≥ 500ms)")

    if short_misses and long_hits:
        print(
            "\n  🎯 LAYER 3 CONFIRMED — enrichment-lag race. "
            "Tight inter-write windows defeat contradiction detection; "
            "longer windows succeed. File CAURA-128."
        )
        return 1
    if n_missed == 0:
        print(
            "\n  ✅ S2 CLOSED — detection survives all probed write spacings. "
            "Move to S3 (object-side normalisation)."
        )
        return 0
    if n_missed == n_total:
        print(
            "\n  ⚠️  All trials missed — broader regression than just race. "
            "Investigate before filing."
        )
        return 1
    print(
        "\n  🤔  Mixed result without a clean gap-ordering — re-run with "
        "more trials per gap to see if it's flaky."
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
