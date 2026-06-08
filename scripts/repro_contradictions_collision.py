#!/usr/bin/env python3
"""P4 — L3.4 collision probe (post-CAURA-130).

Hypothesis: CAURA-130's forward-Path-C entity-links preflight should
DROP candidate pairs whose canonical subjects share a surface name but
resolve to distinct entity rows ("priya"-collision class — original
followup TODO at ``contradiction_detector.py:1100``).

This probe writes two memories that share a same-surface-name subject
but distinguish it via different qualifiers (different team, different
city), trying to coerce the entity-extraction worker into resolving
them to two different entity_ids. We then check that no contradiction
is flagged.

CAVEAT: this depends on dev's entity extractor producing two distinct
entity rows for the two memories. If the extractor merges them into a
single entity (which is also a plausible behaviour), the preflight
won't fire — but in that case the "contradiction" would also be a real
within-subject contradiction, which IS the right call. So:

  * detected=True  + same entity_id  → contradiction on the SAME subject
                                        (expected, no L3.4 signal)
  * detected=True  + distinct entity_ids → BUG — preflight didn't drop
  * detected=False + distinct entity_ids → L3.4 working as intended
  * detected=False + same entity_id     → Path A/C silent on a real
                                          contradiction (separate bug)

The verdict logic inspects the entity_links after the writes settle
and reports which arm fired.

Usage:
    export MEMCLAW_API_URL=https://memclaw.dev
    export MEMCLAW_API_KEY=mc_...
    export MEMCLAW_TENANT_ID=rantaig-...
    python scripts/repro_contradictions_collision.py
"""

from __future__ import annotations

import argparse
import os
import random
import string
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


def _detected(target_id: str, resp: dict) -> bool:
    items = resp.get("contradictions") or resp.get("items") or []
    return any(
        (it.get("memory_id") == target_id) or (it.get("id") == target_id)
        for it in items
    )


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument(
        "--wait",
        type=int,
        default=30,
        help="seconds to wait for entity extraction + Path C",
    )
    ap.add_argument("--write-mode", default="fast", choices=("fast", "strong"))
    ap.add_argument("--verbose", "-v", action="store_true")
    args = ap.parse_args()

    base = _env("MEMCLAW_API_URL").rstrip("/")
    key = _env("MEMCLAW_API_KEY")
    tenant = _env("MEMCLAW_TENANT_ID")

    # Generate two distinguishable contexts that share a common given
    # name. Use a uniqued suffix to avoid polluting prior runs.
    suffix = "".join(random.choices(string.ascii_lowercase, k=4))
    person_name = "Priya"
    company_a = f"AcmeCorp-{suffix}"
    company_b = f"BetaIndustries-{suffix}"
    agent = f"repro-collision-{uuid.uuid4().hex[:6]}"

    print(f"env        : {base}")
    print(f"tenant     : {tenant}")
    print(f"person     : {person_name}")
    print(f"context A  : {company_a}")
    print(f"context B  : {company_b}")
    print(f"wait       : {args.wait}s\n")

    headers = {"X-API-Key": key, "Content-Type": "application/json"}
    client = httpx.Client(base_url=base, headers=headers, timeout=90.0)

    common = {
        "tenant_id": tenant,
        "agent_id": agent,
        "memory_type": "fact",
        "visibility": "scope_team",
        "write_mode": args.write_mode,
    }

    # Two memories with the SAME surface name "Priya" but DIFFERENT
    # contextual signals — opposite-leaning facts. If the extractor
    # respects context, it should produce two distinct entity rows.
    body_a = {
        **common,
        "content": f"{person_name} from {company_a} lives in Tel Aviv.",
    }
    body_b = {
        **common,
        "content": f"{person_name} from {company_b} lives in Haifa.",
    }

    print("[1/3] POST /api/v1/memories (A)")
    r = client.post("/api/v1/memories", json=body_a)
    r.raise_for_status()
    mem_a = r.json()
    print(f"      → id={mem_a['id']}  status={mem_a.get('status')}")

    print("\n[2/3] POST /api/v1/memories (B)")
    r = client.post("/api/v1/memories", json=body_b)
    r.raise_for_status()
    mem_b = r.json()
    print(f"      → id={mem_b['id']}  status={mem_b.get('status')}")

    print(f"\n[3/3] Waiting {args.wait}s for entity-extraction + Path C…")
    time.sleep(args.wait)

    qp = {"tenant_id": tenant}
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

    print()
    print(f"  A.status       : {a_now.get('status')}")
    print(f"  A.entity_links : {a_now.get('entity_links') or []}")
    print(f"  B.status       : {b_now.get('status')}")
    print(f"  B.entity_links : {b_now.get('entity_links') or []}")

    ab = _detected(mem_b["id"], a_contra)
    ba = _detected(mem_a["id"], b_contra)
    status_hit = (a_now.get("status") in ("outdated", "conflicted")) or (
        b_now.get("status") in ("outdated", "conflicted")
    )
    detected = ab or ba or status_hit

    # Extract subject-role entity_ids if present. Filter out None
    # entity_ids defensively — a malformed link with role=subject but
    # no entity_id would otherwise propagate None into the shared/
    # distinct set comparisons below and silently mis-classify the
    # case.
    def _subjects(mem: dict) -> list[str]:
        return [
            el["entity_id"]
            for el in (mem.get("entity_links") or [])
            if (el.get("role") or "").lower() == "subject"
            and el.get("entity_id") is not None
        ]

    a_subj = _subjects(a_now)
    b_subj = _subjects(b_now)
    shared = set(a_subj) & set(b_subj)
    distinct = bool(a_subj) and bool(b_subj) and not shared

    print()
    print(f"  A subject-role entity_ids: {a_subj}")
    print(f"  B subject-role entity_ids: {b_subj}")
    print(f"  shared subject_ids       : {sorted(shared)}")
    print(f"  distinct subjects?       : {distinct}")
    print(f"  contradiction detected?  : {detected}")

    print("\n" + "=" * 60)
    print("VERDICT")
    print("=" * 60)
    if detected and distinct:
        print(
            "  ❌ BUG — distinct subject entity_ids but contradiction flagged. "
            "L3.4 preflight did not drop the false-positive."
        )
        return 1
    if not detected and distinct:
        print(
            "  ✅ L3.4 WORKING — distinct entity_ids, preflight dropped the "
            "would-be false-positive (or Path A also chose not to flag)."
        )
        return 0
    if detected and shared:
        print(
            "  ℹ️ Real contradiction — extractor merged the two memories "
            "to the same canonical subject (Priya = Priya). This isn't an "
            "L3.4 test case; the system correctly flagged within-subject."
        )
        return 0
    if not detected and shared:
        print(
            "  ⚠️ Extractor merged the subjects but no contradiction surfaced — "
            "separate Path A/C silence issue (not L3.4)."
        )
        return 1
    print(
        "  🤔 Inconclusive — entity-extraction may not have completed (empty "
        "entity_links). Re-run with --wait 60."
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
