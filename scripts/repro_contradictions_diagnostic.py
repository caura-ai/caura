#!/usr/bin/env python3
"""Diagnostic follow-up to ``repro_contradictions_not_detected.py``.

Once the bug is reproduced (contradiction NOT detected for two
conflicting release dates), this script probes the three plausible
failure paths in isolation:

    Probe A: poll ``GET /memories/{id}`` over 180s, watching for
             ``entity_links`` to appear. If they never do → entity
             extraction never ran (or ran but emitted nothing).
    Probe B: ``POST /api/v1/search`` for the token. Both memories
             should appear with high mutual similarity. If they do
             → the embedding layer is fine and semantic detection
             SHOULD have had a candidate pair. Failure mode is
             downstream of the embedding/candidate-fetch step.
    Probe C: Re-run with EXPLICIT ``entity_links=[{role:subject}]``
             in the POST body. If THIS path produces non-empty
             triple columns (``predicate``, ``object_value``) →
             ``EmitMemoryTriple`` works when given subject context;
             the bug is that ingestion never populates entity_links
             for this content shape.

Each probe is independent. Output identifies which gate the bug
sits behind.
"""

from __future__ import annotations

import argparse
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


def _ts() -> str:
    return time.strftime("%H:%M:%S")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument(
        "--max-wait", type=int, default=180, help="entity-link poll budget (s)"
    )
    ap.add_argument("--probe", choices=("all", "a", "b", "c"), default="all")
    args = ap.parse_args()

    base = _env("MEMCLAW_API_URL").rstrip("/")
    key = _env("MEMCLAW_API_KEY")
    tenant = os.environ.get("MEMCLAW_TENANT_ID")

    token = f"TOKEN-{uuid.uuid4().hex[:8].upper()}"
    agent = f"diag-contradictions-{uuid.uuid4().hex[:6]}"

    print(f"env       : {base}")
    print(f"tenant    : {tenant}")
    print(f"token     : {token}")
    print(f"agent     : {agent}\n")

    headers = {"X-API-Key": key, "Content-Type": "application/json"}
    client = httpx.Client(base_url=base, headers=headers, timeout=30.0)

    body_common = {
        "agent_id": agent,
        "memory_type": "fact",
        "visibility": "scope_team",
        "write_mode": "strong",
    }
    if tenant:
        body_common["tenant_id"] = tenant
    qp = {"tenant_id": tenant} if tenant else {}

    # Write A & B without explicit entity_links — same shape as the
    # bug-report repro.
    print(f"[{_ts()}] Writing A and B (no entity_links)…")
    r = client.post(
        "/api/v1/memories",
        json={**body_common, "content": f"{token} has release date 2027-05-01."},
    )
    r.raise_for_status()
    a = r.json()
    r = client.post(
        "/api/v1/memories",
        json={**body_common, "content": f"{token} has release date 2028-10-15."},
    )
    r.raise_for_status()
    b = r.json()
    print(f"        A.id={a['id']}  B.id={b['id']}\n")

    # ---- Probe A: entity_links eventually populated? ----
    if args.probe in ("all", "a"):
        print(f"[{_ts()}] Probe A — polling entity_links over {args.max_wait}s")
        deadline = time.time() + args.max_wait
        last_a_links = last_b_links = -1
        while time.time() < deadline:
            a_now = client.get(f"/api/v1/memories/{a['id']}", params=qp).json()
            b_now = client.get(f"/api/v1/memories/{b['id']}", params=qp).json()
            n_a = len(a_now.get("entity_links") or [])
            n_b = len(b_now.get("entity_links") or [])
            if n_a != last_a_links or n_b != last_b_links:
                elapsed = args.max_wait - int(deadline - time.time())
                print(
                    f"  [{_ts()}] t={elapsed:>3}s  A.entity_links={n_a}  B.entity_links={n_b}  "
                    f"A.subject_entity_id={a_now.get('subject_entity_id')!r}  "
                    f"A.predicate={a_now.get('predicate')!r}  "
                    f"A.object_value={a_now.get('object_value')!r}"
                )
                last_a_links, last_b_links = n_a, n_b
                if n_a > 0 and n_b > 0:
                    print(f"  A.entity_links payload: {a_now.get('entity_links')}")
                    print(f"  B.entity_links payload: {b_now.get('entity_links')}")
                    break
            time.sleep(5)
        else:
            print(
                f"  [{_ts()}] FINAL — entity_links never appeared within {args.max_wait}s."
            )
        print()

    # ---- Probe B: search probe — do A and B even find each other? ----
    if args.probe in ("all", "b"):
        print(f"[{_ts()}] Probe B — POST /api/v1/search for token")
        try:
            # Only include ``tenant_id`` when the env actually supplied
            # one. Posting ``"tenant_id": None`` round-trips as JSON
            # null and would be rejected by the search route's
            # validation as a missing field, masking the real probe
            # outcome with a 422.
            search_body: dict = {"query": f"{token} release date", "top_k": 10}
            if tenant:
                search_body["tenant_id"] = tenant
            sresp = client.post("/api/v1/search", json=search_body)
            sresp.raise_for_status()
            hits = sresp.json().get("items") or []
            print(f"  Got {len(hits)} hit(s)")
            for h in hits:
                if h.get("id") in (a["id"], b["id"]):
                    label = "A" if h["id"] == a["id"] else "B"
                    print(
                        f"    {label}: similarity={h.get('similarity')}  score={h.get('score')}  content={h.get('content', '')[:60]!r}"
                    )
        except Exception as e:
            print(f"  search FAILED: {e}")
        print()

    # ---- Probe C: re-run with explicit entity_links ----
    if args.probe in ("all", "c"):
        print(
            f"[{_ts()}] Probe C — re-run with explicit entity_links=[{{role:subject}}]"
        )
        # Need to upsert/get an entity first. Same conditional-tenant
        # guard as Probe B — never POST ``tenant_id: None``.
        ent_body: dict = {"entity_type": "product", "canonical_name": token}
        if tenant:
            ent_body["tenant_id"] = tenant
        eresp = client.post("/api/v1/entities/upsert", json=ent_body)
        if eresp.status_code >= 400:
            print(f"  entities/upsert returned {eresp.status_code}: {eresp.text[:300]}")
        else:
            entity = eresp.json()
            eid = entity.get("id")
            print(f"  Entity upserted: {eid}")
            new_token = f"TOKEN-{uuid.uuid4().hex[:8].upper()}"
            # Also upsert a "release date" predicate-ish entity? No — entity_links
            # just need {entity_id, role}.
            r = client.post(
                "/api/v1/memories",
                json={
                    **body_common,
                    "content": f"{new_token} has release date 2027-05-01.",
                    "entity_links": [{"entity_id": eid, "role": "subject"}],
                },
            )
            print(f"  Probe-C write status: {r.status_code}")
            if r.status_code < 400:
                cmem = r.json()
                print(
                    f"    id={cmem.get('id')}  predicate={cmem.get('predicate')!r}  "
                    f"object_value={cmem.get('object_value')!r}  "
                    f"subject_entity_id={cmem.get('subject_entity_id')!r}"
                )
                time.sleep(3)
                cfetched = client.get(
                    f"/api/v1/memories/{cmem['id']}", params=qp
                ).json()
                print(
                    f"  After 3s re-read: predicate={cfetched.get('predicate')!r}  "
                    f"object_value={cfetched.get('object_value')!r}  "
                    f"subject_entity_id={cfetched.get('subject_entity_id')!r}  "
                    f"entity_links={cfetched.get('entity_links')}"
                )
            else:
                print(f"    body: {r.text[:300]}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
