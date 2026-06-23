#!/usr/bin/env python3
"""S1 — Proper-noun subject contradiction probe (Layer-3 hunt).

Hypothesis:
  CAURA-127 closed the bare-POST gap for IDENTIFIER-TOKEN subjects
  (``TOKEN-XXXXXXXX``, UUIDs, ``PR-1234``, ``v2.8.0``…). But its
  ``_IDENTIFIER_TOKEN`` regex deliberately rejects proper nouns
  ("Project Zephyr", "Atlas rollout", "Tungsten datacenter") so the
  heuristic cannot accidentally upsert random capitalised English as
  entities. Production traffic is FULL of proper-noun subjects —
  ``retrieval_precision`` in the latest loadtest report shows the system
  routinely stores facts like "Atlas rollout ownership and escalation
  path via Rina". If two such facts contradict and the caller does NOT
  populate ``entity_links``, the triple emitter SKIPS — and contradiction
  detection misses the conflict.

This script probes that exact gap with two arms:

  ARM A (gap candidate):   bare POST, proper-noun subject, NO entity_links
  ARM B (control):         identical pair WITH entity_links pre-populated

  If A fails to detect and B succeeds → Layer 3 confirmed
  (proper-noun subject inference gap).

  If both fail → something else is broken (likely predicate or object
  normalisation; revisit S3/S4).

  If both succeed → S1 is closed; move to S2 (enrichment-lag race).

Usage:
    export MEMCLAW_API_URL=https://memclaw.net
    export MEMCLAW_API_KEY=mc_...
    python scripts/repro_contradictions_proper_noun.py

    # tweak settle time
    python scripts/repro_contradictions_proper_noun.py --wait 25 -v

Exits 0 iff ARM A detects (i.e. nothing to fix). Otherwise exits 1 and
prints the diagnostic.
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

# Synthesised proper-noun stems. Mixed-case + no digits + no hyphens →
# guaranteed NOT to match _IDENTIFIER_TOKEN, exercising the gap.
_STEMS = (
    "Lumenwave",
    "Trident",
    "Helios",
    "Aerolith",
    "Quillforge",
    "Nimbus",
    "Pyrostella",
    "Brightwood",
)


def _mint_proper_noun() -> str:
    """Two-word proper-noun phrase with a 6-letter lowercase tail to
    avoid colliding with any real entity on the target environment."""
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


def _summary(label: str, mem: dict) -> None:
    print(f"  {label}.id             : {mem.get('id')}")
    print(f"  {label}.status         : {mem.get('status')}")
    print(f"  {label}.supersedes_id  : {mem.get('supersedes_id')}")
    print(f"  {label}.subject_entity : {mem.get('subject_entity_id')}")
    print(f"  {label}.predicate      : {mem.get('predicate')!r}")
    print(f"  {label}.object_value   : {mem.get('object_value')!r}")
    el = mem.get("entity_links") or []
    print(f"  {label}.entity_links   : {len(el)} link(s)")


def _run_arm(
    *,
    client: httpx.Client,
    label: str,
    subject: str,
    common: dict,
    entity_links: list | None,
    wait: int,
    verbose: bool,
) -> dict:
    """Write a pair of contradicting facts about ``subject`` and return
    a verdict dict for the post-mortem table."""
    print(
        f"\n=== ARM {label} ({'WITH entity_links' if entity_links else 'BARE POST'}) ==="
    )
    print(f"  subject: {subject!r}")

    body_a = {**common, "content": f"{subject} has release date 2027-05-01."}
    body_b = {**common, "content": f"{subject} has release date 2028-10-15."}
    if entity_links is not None:
        body_a["entity_links"] = entity_links
        body_b["entity_links"] = entity_links

    r = client.post("/api/v1/memories", json=body_a)
    r.raise_for_status()
    mem_a = r.json()
    r = client.post("/api/v1/memories", json=body_b)
    r.raise_for_status()
    mem_b = r.json()
    print(f"  wrote A={mem_a.get('id')}  B={mem_b.get('id')} — waiting {wait}s…")
    if verbose:
        print(f"    A raw: {json.dumps(mem_a, default=str)[:300]}")
        print(f"    B raw: {json.dumps(mem_b, default=str)[:300]}")
    time.sleep(wait)

    qp = {"tenant_id": common.get("tenant_id") or mem_a.get("tenant_id")}
    r = client.get(f"/api/v1/memories/{mem_a['id']}", params=qp)
    r.raise_for_status()
    a_now = r.json()
    r = client.get(f"/api/v1/memories/{mem_b['id']}", params=qp)
    r.raise_for_status()
    b_now = r.json()
    r = client.get(f"/api/v1/memories/{mem_a['id']}/contradictions", params=qp)
    r.raise_for_status()
    a_contra = r.json()
    r = client.get(f"/api/v1/memories/{mem_b['id']}/contradictions", params=qp)
    r.raise_for_status()
    b_contra = r.json()

    _summary(f"{label}.A", a_now)
    _summary(f"{label}.B", b_now)

    ab = _detected(mem_b["id"], a_contra)
    ba = _detected(mem_a["id"], b_contra)
    status_hit = (a_now.get("status") in ("outdated", "conflicted")) or (
        b_now.get("status") in ("outdated", "conflicted")
    )
    chain_hit = (a_now.get("supersedes_id") == mem_b["id"]) or (
        b_now.get("supersedes_id") == mem_a["id"]
    )

    detected = ab or ba or status_hit or chain_hit
    print(f"  A/contradictions includes B? {ab}")
    print(f"  B/contradictions includes A? {ba}")
    print(f"  Either outdated/conflicted?  {status_hit}")
    print(f"  supersedes_id chain?         {chain_hit}")
    print(f"  ==> ARM {label} DETECTED: {detected}")

    return {
        "arm": label,
        "subject": subject,
        "id_a": mem_a["id"],
        "id_b": mem_b["id"],
        "subject_entity_id_a": a_now.get("subject_entity_id"),
        "subject_entity_id_b": b_now.get("subject_entity_id"),
        "predicate_a": a_now.get("predicate"),
        "object_value_a": a_now.get("object_value"),
        "ab_hit": ab,
        "ba_hit": ba,
        "status_hit": status_hit,
        "chain_hit": chain_hit,
        "detected": detected,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--wait", type=int, default=20)
    ap.add_argument("--write-mode", default="strong", choices=("fast", "strong"))
    ap.add_argument("--verbose", "-v", action="store_true")
    ap.add_argument(
        "--skip-control",
        action="store_true",
        help="skip ARM B (entity_links control) — gap probe only",
    )
    args = ap.parse_args()

    base = _env("MEMCLAW_API_URL").rstrip("/")
    key = _env("MEMCLAW_API_KEY")
    tenant = os.environ.get("MEMCLAW_TENANT_ID")

    agent = f"repro-proper-noun-{uuid.uuid4().hex[:6]}"
    common = {
        "agent_id": agent,
        "memory_type": "fact",
        "visibility": "scope_team",
        "write_mode": args.write_mode,
    }
    if tenant:
        common["tenant_id"] = tenant

    print(f"env        : {base}")
    print(f"tenant     : {tenant or '(inferred from key)'}")
    print(f"write_mode : {args.write_mode}")
    print(f"wait       : {args.wait}s")

    headers = {"X-API-Key": key, "Content-Type": "application/json"}
    client = httpx.Client(base_url=base, headers=headers, timeout=90.0)

    # ARM A — bare POST, proper-noun subject, NO entity_links.
    subject_a = _mint_proper_noun()
    arm_a = _run_arm(
        client=client,
        label="A",
        subject=subject_a,
        common=common,
        entity_links=None,
        wait=args.wait,
        verbose=args.verbose,
    )

    # ARM B — control. Same shape, but caller pre-populates entity_links
    # with a freshly-minted entity so the heuristic path is bypassed.
    arm_b: dict | None = None
    if not args.skip_control:
        subject_b = _mint_proper_noun()
        # Mint a synthetic entity_id deterministically so both writes
        # point at the same Entity row. The API resolves unknown UUIDs
        # by creating-on-write when entity_links carries name+type.
        ent_id = str(uuid.uuid4())
        entity_links = [
            {
                "entity_id": ent_id,
                "name": subject_b,
                "entity_type": "project",
                "role": "subject",
            }
        ]
        arm_b = _run_arm(
            client=client,
            label="B",
            subject=subject_b,
            common=common,
            entity_links=entity_links,
            wait=args.wait,
            verbose=args.verbose,
        )

    # ── Verdict table ────────────────────────────────────────────────
    print("\n" + "=" * 60)
    print("VERDICT")
    print("=" * 60)
    rows = [arm_a] + ([arm_b] if arm_b else [])
    for r in rows:
        print(
            f"  ARM {r['arm']}: detected={r['detected']}  "
            f"subj_entity_id={r['subject_entity_id_a']}  "
            f"predicate={r['predicate_a']!r}  "
            f"object={r['object_value_a']!r}"
        )

    if arm_b is None:
        print("\n  Control arm skipped.")
        return 0 if arm_a["detected"] else 1

    if arm_a["detected"] and arm_b["detected"]:
        print(
            "\n  ✅  S1 CLOSED — proper-noun contradictions are detected. "
            "Move to S2 (enrichment-lag race)."
        )
        return 0
    if (not arm_a["detected"]) and arm_b["detected"]:
        print(
            "\n  🎯  LAYER 3 CONFIRMED — proper-noun subject inference gap. "
            "Bare POST with proper-noun subject does NOT detect; the same "
            "shape WITH entity_links does. Filing CAURA-128 is justified."
        )
        return 1
    if (not arm_a["detected"]) and (not arm_b["detected"]):
        print(
            "\n  ⚠️  Both arms missed — bug is downstream of subject "
            "inference (predicate normalisation? object comparison? "
            "Investigate S3/S4 before filing.)."
        )
        return 1
    # arm_a detected, arm_b didn't — unexpected; entity_links should be
    # strictly better than bare POST.
    print(
        "\n  🤔  ARM A detected but ARM B did not — unexpected. "
        "entity_links regression? Investigate."
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
