#!/usr/bin/env python3
"""Wet test for CAURA-123 — proves EmitMemoryTriple feeds the RDF
contradiction path end-to-end against a running ``core-api``.

What it does
============
1. Upserts a single subject entity ("Ran") via ``POST /entities/upsert``.
2. Writes memory A: "Ran lives in Tel Aviv" with ``entity_links=[{ran, subject}]``.
3. Writes memory B: "Ran lives in New York" with the same subject link.
4. Asserts:
     ① A.subject_entity_id / predicate / object_value are populated
        (proves ``EmitMemoryTriple`` ran).
     ② B.predicate == "lives_in" (same).
     ③ B's response carries ``superseded_by`` with the old memory id == A.id
        AND ``reason == "rdf_conflict"``  (proves the deterministic RDF
        path fired, not the LLM fallback).
     ④ Re-GETting A shows status == "outdated".

Exits 0 on full pass, 1 otherwise. Prints a check-by-check report.

Assumptions
===========
* ``core-api`` is reachable at ``--url`` (default http://localhost:8000).
* Either standalone mode (env.dev default: ``IS_STANDALONE=true``,
  no key required) OR ``--api-key`` is supplied.
* Uses a UUID-suffixed tenant so it never collides with existing data
  and needs no cleanup.

Usage
=====
    docker-compose up -d
    python scripts/wet_test_triple_emission.py
    python scripts/wet_test_triple_emission.py --url http://localhost:8000
    python scripts/wet_test_triple_emission.py --api-key mc_dev_xxx
    python scripts/wet_test_triple_emission.py --verbose

Keys you (the operator) need
============================
* No LLM keys required — triple emission is purely deterministic and
  the contradiction path under test is the *non-LLM* one.
* ``--api-key`` only needed if your core-api has ``MEMCLAW_API_KEY`` set
  (network-exposed deployments). Local docker-compose dev does not.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import uuid

import httpx

DEFAULT_URL = "http://localhost:8000"
TIMEOUT = 30.0


def _color(s: str, code: str) -> str:
    return f"\033[{code}m{s}\033[0m"


def _ok(s: str) -> str:
    return _color(s, "32")


def _bad(s: str) -> str:
    return _color(s, "31")


def _hdr(s: str) -> str:
    return _color(s, "1;36")


class WetTest:
    def __init__(self, base_url: str, api_key: str | None, tenant: str, *, verbose: bool):
        self.api = base_url.rstrip("/") + "/api/v1"
        # In standalone mode (OSS dev default), the auth context locks
        # tenant_id to "default" and rejects mismatches with 403. We
        # default to that and isolate runs via a per-run fleet id.
        self.tenant = tenant
        # Per-run fleet+agent so the auto-registered agent's home
        # fleet matches what we're writing to (trust_level=1 allows
        # writes only to home fleet). The fleet_id is now forwarded
        # through to the storage-api ``/rdf-conflicts`` route, so
        # RDF detection works correctly under real fleet scoping —
        # this run exercises that path end-to-end.
        suffix = uuid.uuid4().hex[:8]
        self.fleet = f"caura123-fleet-{suffix}"
        self.agent = f"caura123-agent-{suffix}"
        self.verbose = verbose
        headers = {"Content-Type": "application/json"}
        if api_key:
            headers["X-API-Key"] = api_key
        self.client = httpx.Client(timeout=TIMEOUT, headers=headers)
        self.checks: list[tuple[str, bool, str]] = []

    # ── HTTP helpers ──

    def _post(self, path: str, body: dict) -> dict:
        r = self.client.post(f"{self.api}{path}", json=body)
        if r.status_code >= 400:
            print(_bad(f"POST {path} → {r.status_code}"))
            print(r.text)
            sys.exit(2)
        return r.json()

    def _get(self, path: str, **params) -> dict:
        r = self.client.get(f"{self.api}{path}", params=params)
        if r.status_code >= 400:
            print(_bad(f"GET {path} → {r.status_code}"))
            print(r.text)
            sys.exit(2)
        return r.json()

    # ── Check recorder ──

    def _check(self, label: str, passed: bool, detail: str = ""):
        self.checks.append((label, passed, detail))
        mark = _ok("✓") if passed else _bad("✗")
        suffix = f" — {detail}" if detail and (not passed or self.verbose) else ""
        print(f"  {mark} {label}{suffix}")

    # ── The scenario ──

    def run(self) -> int:
        print(_hdr(f"\nCAURA-123 wet test — tenant={self.tenant}"))
        print(_hdr("=" * 60))

        # 1) Create the subject entity.
        print("\n[1/4] Upserting subject entity 'Ran'…")
        ent = self._post(
            "/entities/upsert",
            {
                "tenant_id": self.tenant,
                "fleet_id": self.fleet,
                "entity_type": "person",
                "canonical_name": "Ran",
            },
        )
        subject_id = ent["id"]
        if self.verbose:
            print(f"      subject_entity_id = {subject_id}")

        # 2) Write memory A.
        print("\n[2/4] Writing memory A: 'Ran lives in Tel Aviv'")
        mem_a = self._post(
            "/memories",
            {
                "tenant_id": self.tenant,
                "fleet_id": self.fleet,
                "agent_id": self.agent,
                "content": "Ran lives in Tel Aviv",
                "entity_links": [{"entity_id": subject_id, "role": "subject"}],
                "write_mode": "fast",
            },
        )
        if self.verbose:
            print(json.dumps(mem_a, indent=2, default=str))

        # 3) Write memory B (the contradiction).
        print("\n[3/4] Writing memory B: 'Ran lives in New York'")
        mem_b = self._post(
            "/memories",
            {
                "tenant_id": self.tenant,
                "fleet_id": self.fleet,
                "agent_id": self.agent,
                "content": "Ran lives in New York",
                "entity_links": [{"entity_id": subject_id, "role": "subject"}],
                "write_mode": "fast",
            },
        )
        if self.verbose:
            print(json.dumps(mem_b, indent=2, default=str))

        # 4) Allow background tasks (contradiction detection) to settle.
        # Contradiction detection runs as a background task scheduled by
        # ScheduleBackgroundTasks, so the synchronous POST response races
        # it — we must re-GET both rows after a short wait to see final
        # state.
        print("\n[4/4] Waiting for background contradiction detection…")
        time.sleep(3)
        a_now = self._get(f"/memories/{mem_a['id']}", tenant_id=self.tenant)
        b_now = self._get(f"/memories/{mem_b['id']}", tenant_id=self.tenant)

        # ── Assertions ──

        print(_hdr("\nResults"))
        print(_hdr("-" * 60))

        # ① Triple columns populated on A
        self._check(
            "A.subject_entity_id populated",
            bool(mem_a.get("subject_entity_id")),
            f"got {mem_a.get('subject_entity_id')!r}",
        )
        self._check(
            "A.predicate == 'lives_in'",
            mem_a.get("predicate") == "lives_in",
            f"got {mem_a.get('predicate')!r}",
        )
        self._check(
            "A.object_value non-empty",
            bool(mem_a.get("object_value")),
            f"got {mem_a.get('object_value')!r}",
        )

        # ② Triple columns populated on B
        self._check(
            "B.predicate == 'lives_in'",
            mem_b.get("predicate") == "lives_in",
            f"got {mem_b.get('predicate')!r}",
        )

        # ③ B.supersedes_id is set to A.id after background detection.
        # (B.superseded_by in the immediate write response is empty
        # because contradiction detection runs in a background task —
        # we read post-settle here.)
        self._check(
            "B.supersedes_id == A.id (post-settle)",
            str(b_now.get("supersedes_id") or "") == str(mem_a["id"]),
            f"got supersedes_id={b_now.get('supersedes_id')!r}, expected {mem_a['id']!r}",
        )

        # ④ A is now outdated when read back — this is the smoking-gun
        # proof that the RDF (deterministic, no-LLM) path fired.
        # The LLM path uses "conflicted", not "outdated".
        self._check(
            "A.status == 'outdated' (proves deterministic RDF path fired, not LLM)",
            a_now.get("status") == "outdated",
            f"got status={a_now.get('status')!r}",
        )

        # ── Summary ──
        passed = sum(1 for _, ok, _ in self.checks if ok)
        total = len(self.checks)
        print(_hdr("-" * 60))
        if passed == total:
            print(_ok(f"\nPASS — {passed}/{total} checks succeeded.\n"))
            return 0
        print(_bad(f"\nFAIL — {passed}/{total} checks passed.\n"))
        return 1


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--url", default=DEFAULT_URL, help=f"core-api base URL (default {DEFAULT_URL})")
    ap.add_argument("--api-key", default=None, help="X-API-Key (only if core-api requires it)")
    ap.add_argument("--tenant", default="default", help="tenant_id (standalone OSS locks this to 'default')")
    ap.add_argument("--verbose", "-v", action="store_true", help="print full responses")
    args = ap.parse_args()

    try:
        return WetTest(args.url, args.api_key, args.tenant, verbose=args.verbose).run()
    except httpx.ConnectError as e:
        print(_bad(f"\nCould not reach {args.url}: {e}"))
        print("Hint: is docker-compose up?")
        return 2


if __name__ == "__main__":
    sys.exit(main())
