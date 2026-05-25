#!/usr/bin/env python3
"""CAURA-126 wet test — release-date contradiction with explicit entity_links.

Reproduces the loadtest-1779685590 bug shape (two memories about the
same token with conflicting release dates), but adds an explicit
``entity_links=[{role:subject}]`` to the POST so the local Layer-2
gap (entity extraction doesn't auto-populate links for token-shaped
subjects, A5) is bypassed. This isolates Layer 1 — the phrase-table
coverage CAURA-126 just expanded.

Expected behaviour with the fix:
    A.predicate  = "release_date"
    B.predicate  = "release_date"
    A.object_value = "2027-05-01" (or similar)
    B.object_value = "2028-10-15"
    Synchronous detector: A becomes outdated, B.supersedes_id = A.id.

Usage (against local docker-compose stack with the rebuilt image):
    python scripts/repro_contradictions_with_entity_links.py
"""

from __future__ import annotations

import argparse
import os
import time
import uuid

import httpx

BASE = os.environ.get("MEMCLAW_API_URL", "http://localhost:8000").rstrip("/")
TENANT = os.environ.get("MEMCLAW_TENANT_ID", "default")  # standalone default


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--wait", type=int, default=5)
    args = ap.parse_args()

    key = os.environ.get("MEMCLAW_API_KEY", "")
    headers = {"Content-Type": "application/json"}
    if key:
        headers["X-API-Key"] = key

    client = httpx.Client(base_url=BASE, headers=headers, timeout=30.0)

    token = f"TOKEN-{uuid.uuid4().hex[:8].upper()}"
    suffix = uuid.uuid4().hex[:8]
    fleet = f"caura126-fleet-{suffix}"
    agent = f"caura126-agent-{suffix}"

    print(f"env       : {BASE}")
    print(f"tenant    : {TENANT}")
    print(f"token     : {token}")
    print(f"fleet     : {fleet}")
    print(f"agent     : {agent}\n")

    # 1. Upsert the subject entity once.
    # Each ``.json()`` chain below is split into a named response +
    # ``raise_for_status()`` so a 4xx with an error body surfaces
    # immediately instead of producing a malformed entity / memory
    # dict that fails later with a confusing KeyError.
    print("[1/4] Upsert subject entity")
    ent_resp = client.post(
        "/api/v1/entities/upsert",
        json={
            "tenant_id": TENANT,
            "fleet_id": fleet,
            "entity_type": "product",
            "canonical_name": token,
        },
    )
    ent_resp.raise_for_status()
    ent = ent_resp.json()
    subject_id = ent["id"]
    print(f"      subject_entity_id = {subject_id}\n")

    body_common = {
        "tenant_id": TENANT,
        "fleet_id": fleet,
        "agent_id": agent,
        "memory_type": "fact",
        "visibility": "scope_team",
        "write_mode": "strong",
        "entity_links": [{"entity_id": subject_id, "role": "subject"}],
    }

    # 2. Write memory A — earlier release date.
    print("[2/4] Write memory A — 'has release date 2027-05-01'")
    a_resp = client.post(
        "/api/v1/memories",
        json={**body_common, "content": f"{token} has release date 2027-05-01."},
    )
    a_resp.raise_for_status()
    a = a_resp.json()
    print(
        f"      id={a['id']}  predicate={a.get('predicate')!r}  object_value={a.get('object_value')!r}\n"
    )

    # 3. Write memory B — conflicting release date.
    print("[3/4] Write memory B — 'has release date 2028-10-15'")
    b_resp = client.post(
        "/api/v1/memories",
        json={**body_common, "content": f"{token} has release date 2028-10-15."},
    )
    b_resp.raise_for_status()
    b = b_resp.json()
    print(
        f"      id={b['id']}  predicate={b.get('predicate')!r}  object_value={b.get('object_value')!r}\n"
    )

    # 4. Wait briefly for inline detection to settle, then re-fetch.
    print(f"[4/4] Wait {args.wait}s and re-fetch")
    time.sleep(args.wait)
    qp = {"tenant_id": TENANT}
    a_now = client.get(f"/api/v1/memories/{a['id']}", params=qp).json()
    b_now = client.get(f"/api/v1/memories/{b['id']}", params=qp).json()

    def _dump(label, m):
        print(
            f"  {label}: status={m.get('status')!s:<11}  predicate={m.get('predicate')!r}  "
            f"object={m.get('object_value')!r}  supersedes_id={m.get('supersedes_id')}"
        )

    print()
    _dump("A", a_now)
    _dump("B", b_now)

    checks = {
        "A.predicate == 'release_date'": a_now.get("predicate") == "release_date",
        "B.predicate == 'release_date'": b_now.get("predicate") == "release_date",
        "A.object_value populated": bool(a_now.get("object_value")),
        "B.object_value populated": bool(b_now.get("object_value")),
        "A.object_value != B.object_value": a_now.get("object_value")
        != b_now.get("object_value"),
        "A.status == 'outdated' (RDF fired)": a_now.get("status") == "outdated",
        "B.supersedes_id == A.id": b_now.get("supersedes_id") == a["id"],
    }
    print("\nGates:")
    for k, v in checks.items():
        print(f"  {'✓' if v else '✗'}  {k}")

    return 0 if all(checks.values()) else 1


if __name__ == "__main__":
    raise SystemExit(main())
