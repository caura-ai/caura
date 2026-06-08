#!/usr/bin/env python3
"""Minimal reproduction of the ``contradictions-not-detected`` bug.

Bug report (loadtest-1779685590, env=net, server v2.8.0):
  Seeded two directly-conflicting facts about ``TOKEN-736C57D0``
  (release date 2027-05-01 vs 2028-10-15). GET /memories/:id/contradictions
  flagged neither.

This script mirrors that exact shape against ANY environment:

    1. Mint a fresh ``TOKEN-XXXXXXXX`` entity token (UUID-derived so two
       runs never collide).
    2. POST /memories — memory A: "<TOKEN> has release date 2027-05-01."
    3. POST /memories — memory B: "<TOKEN> has release date 2028-10-15."
    4. Wait N seconds for background enrichment + entity extraction +
       contradiction detection.
    5. GET /memories/{A}/contradictions  → expect non-empty referring to B
       GET /memories/{B}/contradictions  → expect non-empty referring to A
    6. GET /memories/{A}, /memories/{B} → inspect status, supersedes_id,
       and triple columns (subject_entity_id / predicate / object_value).

Exits 0 iff contradictions are detected in either direction. Otherwise
prints a structured diagnostic dump showing exactly what the server
reported.

Usage:
    export MEMCLAW_API_URL=https://memclaw.dev      # or https://memclaw.net
    export MEMCLAW_API_KEY=mc_...
    python scripts/repro_contradictions_not_detected.py

    # Adjust settle time if your env's detection is slow
    python scripts/repro_contradictions_not_detected.py --wait 30
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import uuid

import httpx


def _env(name: str) -> str:
    val = os.environ.get(name)
    if not val:
        print(f"ERROR: ${name} must be set", file=sys.stderr)
        sys.exit(2)
    return val


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument(
        "--wait",
        type=int,
        default=20,
        help="seconds to wait for async detection (default 20)",
    )
    ap.add_argument("--write-mode", default="strong", choices=("fast", "strong"))
    ap.add_argument("--verbose", "-v", action="store_true")
    ap.add_argument(
        "--token", default=None, help="override the synthetic token (for replay)"
    )
    args = ap.parse_args()

    base = _env("MEMCLAW_API_URL").rstrip("/")
    key = _env("MEMCLAW_API_KEY")
    tenant = os.environ.get("MEMCLAW_TENANT_ID")  # optional — server may infer from key

    token = args.token or f"TOKEN-{uuid.uuid4().hex[:8].upper()}"
    agent = f"repro-contradictions-{uuid.uuid4().hex[:6]}"

    print(f"env       : {base}")
    print(f"tenant    : {tenant or '(inferred from key)'}")
    print(f"token     : {token}")
    print(f"agent     : {agent}")
    print(f"write_mode: {args.write_mode}")
    print(f"wait      : {args.wait}s\n")

    headers = {"X-API-Key": key, "Content-Type": "application/json"}
    client = httpx.Client(base_url=base, headers=headers, timeout=30.0)

    body_common = {
        "agent_id": agent,
        "memory_type": "fact",
        "visibility": "scope_team",
        "write_mode": args.write_mode,
    }
    if tenant:
        body_common["tenant_id"] = tenant

    # 1. Write memory A — earlier release date.
    body_a = {**body_common, "content": f"{token} has release date 2027-05-01."}
    print("[1/4] POST /api/v1/memories  (memory A: 2027-05-01)")
    r = client.post("/api/v1/memories", json=body_a)
    r.raise_for_status()
    mem_a = r.json()
    print(f"      → id={mem_a.get('id')}  status={mem_a.get('status')}")
    if args.verbose:
        print(f"      raw: {json.dumps(mem_a, default=str)[:400]}")

    # 2. Write memory B — later release date. Direct conflict on the
    #    same single-value predicate.
    body_b = {**body_common, "content": f"{token} has release date 2028-10-15."}
    print("\n[2/4] POST /api/v1/memories  (memory B: 2028-10-15)")
    r = client.post("/api/v1/memories", json=body_b)
    r.raise_for_status()
    mem_b = r.json()
    print(f"      → id={mem_b.get('id')}  status={mem_b.get('status')}")
    if args.verbose:
        print(f"      raw: {json.dumps(mem_b, default=str)[:400]}")

    # 3. Wait for async detection (RDF compare runs inline if triples
    #    are populated synchronously; semantic LLM check runs in a
    #    background task after the write returns).
    print(f"\n[3/4] Waiting {args.wait}s for async detection…")
    time.sleep(args.wait)

    # 4. Re-fetch both memories + their contradictions endpoints.
    # All GETs scoped by tenant_id (the API requires it as a query
    # parameter; without it, validation 422s and the response
    # decoder silently returns ``{"detail": [...]}``). Use the
    # tenant id returned on the write so this works even when
    # ``MEMCLAW_TENANT_ID`` is unset.
    print("\n[4/4] Re-fetching memories + contradictions endpoints\n")
    qp = {"tenant_id": tenant or mem_a.get("tenant_id")}
    a_now = client.get(f"/api/v1/memories/{mem_a['id']}", params=qp).json()
    b_now = client.get(f"/api/v1/memories/{mem_b['id']}", params=qp).json()
    a_contra = client.get(
        f"/api/v1/memories/{mem_a['id']}/contradictions", params=qp
    ).json()
    b_contra = client.get(
        f"/api/v1/memories/{mem_b['id']}/contradictions", params=qp
    ).json()

    def _summary(label: str, mem: dict) -> None:
        print(f"  {label}.id              : {mem.get('id')}")
        print(f"  {label}.status          : {mem.get('status')}")
        print(f"  {label}.supersedes_id   : {mem.get('supersedes_id')}")
        print(f"  {label}.subject_entity  : {mem.get('subject_entity_id')}")
        print(f"  {label}.predicate       : {mem.get('predicate')!r}")
        print(f"  {label}.object_value    : {mem.get('object_value')!r}")
        sb = mem.get("superseded_by") or []
        print(f"  {label}.superseded_by   : {len(sb)} entry(s)")
        for item in sb:
            print(f"     • {item}")
        el = mem.get("entity_links") or []
        print(f"  {label}.entity_links    : {len(el)} link(s)  {el}")

    _summary("A", a_now)
    print()
    _summary("B", b_now)

    def _detected(target_id: str, resp: dict) -> bool:
        # /contradictions is documented as returning {contradictions: [...]}
        # but call sites in algo-stress also accept {items: [...]} for
        # forward compat — accept either.
        items = resp.get("contradictions") or resp.get("items") or []
        return any(
            (it.get("memory_id") == target_id) or (it.get("id") == target_id)
            for it in items
        )

    print("\nContradictions endpoint:")
    print(f"  GET /memories/{mem_a['id']}/contradictions:")
    print(f"    {json.dumps(a_contra, default=str)[:400]}")
    print(f"  GET /memories/{mem_b['id']}/contradictions:")
    print(f"    {json.dumps(b_contra, default=str)[:400]}")

    ab_hit = _detected(mem_b["id"], a_contra)
    ba_hit = _detected(mem_a["id"], b_contra)
    status_hit = (a_now.get("status") in ("outdated", "conflicted")) or (
        b_now.get("status") in ("outdated", "conflicted")
    )
    chain_hit = (a_now.get("supersedes_id") == mem_b["id"]) or (
        b_now.get("supersedes_id") == mem_a["id"]
    )

    print("\nVerdict:")
    print(f"  A/contradictions includes B? {ab_hit}")
    print(f"  B/contradictions includes A? {ba_hit}")
    print(f"  Either marked outdated/conflicted? {status_hit}")
    print(f"  Either carries supersedes_id pointing at the other? {chain_hit}")

    detected = ab_hit or ba_hit or status_hit or chain_hit
    print(f"\n  ==>  CONTRADICTION DETECTED: {detected}\n")
    return 0 if detected else 1


if __name__ == "__main__":
    raise SystemExit(main())
