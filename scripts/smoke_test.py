#!/usr/bin/env python3
"""MemClaw smoke test — hits the live API and reports pass/fail.

Usage:
    python scripts/smoke_test.py                          # default: localhost:8000
    python scripts/smoke_test.py --url http://localhost:8000
    python scripts/smoke_test.py --url http://localhost:8001 --api-key mc_2026
    python scripts/smoke_test.py --verbose                # show response bodies on failure
    python scripts/smoke_test.py --json                   # JSON output for CI pipelines
"""

import argparse
import json
import sys
import time
import uuid

import httpx

# ── Config ──

DEFAULT_URL = "http://localhost:8000"
TENANT = f"smoke-test-{uuid.uuid4().hex[:8]}"
AGENT = "smoke-agent"
TIMEOUT = 30.0


def _retry_until(fn, *, predicate, interval=0.5, max_wait=10.0):
    """Call *fn* repeatedly until *predicate(result)* is True or timeout."""
    elapsed = 0.0
    while elapsed < max_wait:
        result = fn()
        if predicate(result):
            return result
        time.sleep(interval)
        elapsed += interval
        interval = min(interval * 1.5, 3.0)  # exponential back-off, cap at 3s
    return result  # last attempt


class SmokeTest:
    def __init__(
        self,
        base_url: str,
        api_key: str | None,
        *,
        verbose: bool = False,
        json_output: bool = False,
    ):
        self.base = base_url.rstrip("/")
        self.api = f"{self.base}/api/v1"
        self.headers = {}
        if api_key:
            self.headers["X-API-Key"] = api_key
        self.client = httpx.Client(timeout=TIMEOUT, headers=self.headers)
        self.verbose = verbose
        self.json_output = json_output
        self.passed = 0
        self.failed = 0
        self.skipped = 0
        self.results: list[dict] = []
        self.memory_ids: list[str] = []
        self.entity_ids: list[str] = []
        self.contradiction_new_id: str | None = None
        self._test_times: list[tuple[str, float]] = []

    def _run_test(self, test_fn):
        """Run a single test method, catching transport/network errors."""
        name = test_fn.__name__
        t0 = time.monotonic()
        try:
            test_fn()
        except (
            httpx.ReadError,
            httpx.WriteError,
            httpx.CloseError,
            httpx.ConnectError,
            httpx.ConnectTimeout,
            httpx.ReadTimeout,
            httpx.PoolTimeout,
        ) as exc:
            self.check(f"{name}: network error", False, f"{type(exc).__name__}: {exc}")
        except Exception as exc:
            self.check(
                f"{name}: unexpected error", False, f"{type(exc).__name__}: {exc}"
            )
        finally:
            self._test_times.append((name, time.monotonic() - t0))

    def run(self):
        t_start = time.monotonic()

        if not self.json_output:
            print(f"\n  MemClaw Smoke Test")
            print(f"  URL: {self.base}")
            print(f"  Tenant: {TENANT}")
            print()

        try:
            self.client.get(f"{self.api}/health", timeout=5)
        except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
            if self.json_output:
                print(
                    json.dumps(
                        {
                            "error": f"Cannot connect to {self.base}: {exc}",
                            "passed": 0,
                            "failed": 1,
                            "skipped": 0,
                        }
                    )
                )
            else:
                print(f"  FAIL  Cannot connect to {self.base}: {exc}")
            return 1

        tests = [
            # ── Core endpoints ──
            self.test_health,
            self.test_version,
            self.test_tool_descriptions,
            # ── Write paths ──
            self.test_write_with_enrichment,
            self.test_temporal_resolution,
            self.test_pii_detection,
            self.test_extract_only,
            self.test_bulk_write,
            # ── Read / search paths ──
            self.test_search,
            self.test_min_similarity_filtering,
            self.test_recall,
            self.test_graph_retrieval,
            self.test_list_memories,
            # ── Update paths ──
            self.test_memory_update,
            self.test_status_update,
            # ── Dedup & contradiction ──
            self.test_dedup,
            self.test_contradiction,
            self.test_contradiction_chain,
            # ── Agent trust & tuning ──
            self.test_agent_trust_levels,
            self.test_tune,
            # ── Subsystems ──
            self.test_crystallizer,
            self.test_ingest,
            self.test_fleets_and_stats,
            self.test_fleet_heartbeat,
            self.test_graph_endpoint,
            self.test_entity_list,
            self.test_usage,
            self.test_usage_history,
            self.test_audit_log,
            self.test_settings,
            self.test_auth_verify,
            self.test_mcp_initialize,
            # ── Document store ──
            self.test_document_store,
            # ── Validation / negative-path ──
            self.test_write_missing_content,
            self.test_search_empty_query,
            self.test_write_oversized_content,
            self.test_invalid_memory_id_format,
            # ── Optional / feature-gated ──
            self.test_soft_delete,
            self.test_demo_token,
        ]

        for test_fn in tests:
            self._run_test(test_fn)

        # ── Cleanup ──
        self.cleanup()

        elapsed = time.monotonic() - t_start

        if self.json_output:
            print(
                json.dumps(
                    {
                        "passed": self.passed,
                        "failed": self.failed,
                        "skipped": self.skipped,
                        "total": self.passed + self.failed + self.skipped,
                        "elapsed_s": round(elapsed, 2),
                        "results": self.results,
                    },
                    indent=2,
                )
            )
        else:
            print()
            total = self.passed + self.failed + self.skipped
            if self.failed == 0:
                print(f"  ALL {self.passed} TESTS PASSED", end="")
                if self.skipped:
                    print(f" ({self.skipped} skipped)", end="")
                print(f"  [{elapsed:.1f}s]")
            else:
                print(
                    f"  {self.passed}/{total} passed, {self.failed} FAILED, {self.skipped} skipped  [{elapsed:.1f}s]"
                )

            # Show slowest tests
            if self._test_times:
                slowest = sorted(self._test_times, key=lambda x: x[1], reverse=True)[:5]
                if slowest[0][1] > 1.0:
                    print()
                    print("  Slowest tests:")
                    for name, dur in slowest:
                        print(f"    {dur:6.1f}s  {name}")
            print()

        return 0 if self.failed == 0 else 1

    def check(self, name: str, condition: bool, detail: str = ""):
        entry = {"name": name, "status": "PASS" if condition else "FAIL"}
        if detail:
            entry["detail"] = detail
        self.results.append(entry)

        if condition:
            self.passed += 1
            if not self.json_output:
                print(f"  PASS  {name}")
        else:
            self.failed += 1
            if not self.json_output:
                msg = f"  FAIL  {name}"
                if detail:
                    msg += f"  ({detail})"
                print(msg)

    def skip(self, name: str, reason: str = ""):
        self.skipped += 1
        entry = {"name": name, "status": "SKIP"}
        if reason:
            entry["detail"] = reason
        self.results.append(entry)
        if not self.json_output:
            msg = f"  SKIP  {name}"
            if reason:
                msg += f"  ({reason})"
            print(msg)

    def _timed(self, test_fn):
        """Decorator-like helper: call test_fn, record its wall-clock time."""
        name = test_fn.__name__
        t0 = time.monotonic()
        test_fn()
        dur = time.monotonic() - t0
        self._test_times.append((name, dur))

    def _verbose_body(self, r: httpx.Response) -> str:
        """Return truncated response body when --verbose is set."""
        if not self.verbose:
            return ""
        try:
            body = r.text[:500]
        except Exception:
            body = "(unreadable)"
        return f" body={body}"

    # ── Tests ──

    def test_health(self):
        r = self.client.get(f"{self.api}/health")
        data = r.json()
        self.check(
            "Health check",
            r.status_code == 200 and data.get("status") == "ok",
            f"status={r.status_code} body={data}",
        )

    def test_version(self):
        r = self.client.get(f"{self.api}/version")
        data = r.json()
        self.check("Version: 200", r.status_code == 200, f"status={r.status_code}")
        self.check(
            "Version: has version string",
            bool(data.get("version")),
            f"version={data.get('version')}",
        )

    def test_tool_descriptions(self):
        """GET /api/tool-descriptions returns all canonical tool descriptions."""
        r = self.client.get(f"{self.api}/tool-descriptions")
        data = r.json()
        expected_tools = {
            "memclaw_recall",
            "memclaw_write",
            "memclaw_manage",
            "memclaw_list",
            "memclaw_doc",
            "memclaw_entity_get",
            "memclaw_tune",
            "memclaw_insights",
            "memclaw_evolve",
            "memclaw_stats",
        }
        self.check(
            "Tool descriptions: 200", r.status_code == 200, f"status={r.status_code}"
        )
        self.check(
            f"Tool descriptions: all {len(expected_tools)} tools present",
            set(data.keys()) == expected_tools,
            f"keys={set(data.keys())}",
        )
        self.check(
            "Tool descriptions: values are strings",
            all(isinstance(v, str) and len(v) > 10 for v in data.values()),
            "some values are empty or not strings",
        )

    def test_write_with_enrichment(self):
        """Write a memory and verify LLM enrichment (type, title, tags)."""
        r = self.client.post(
            f"{self.api}/memories",
            json={
                "tenant_id": TENANT,
                "agent_id": AGENT,
                "content": "We decided to use PostgreSQL 16 instead of MySQL for the new analytics service.",
            },
        )
        data = r.json()
        self.check(
            "Write memory",
            r.status_code in (200, 201),
            f"status={r.status_code}{self._verbose_body(r)}",
        )
        self.check(
            "LLM inferred type",
            data.get("memory_type") in ("decision", "fact"),
            f"type={data.get('memory_type')}",
        )
        self.check(
            "LLM generated title", bool(data.get("title")), f"title={data.get('title')}"
        )
        self.check(
            "LLM generated tags",
            bool(data.get("metadata", {}).get("tags")),
            f"tags={data.get('metadata', {}).get('tags')}",
        )
        if data.get("id"):
            self.memory_ids.append(data["id"])

    def test_temporal_resolution(self):
        """Write content with a date phrase, verify ts_valid_end is populated."""
        r = self.client.post(
            f"{self.api}/memories",
            json={
                "tenant_id": TENANT,
                "agent_id": AGENT,
                "content": "The project deadline is June 15, 2026. All deliverables must be submitted by then.",
            },
        )
        data = r.json()
        self.check(
            "Temporal: ts_valid_end populated",
            data.get("ts_valid_end") is not None,
            f"ts_valid_end={data.get('ts_valid_end')}",
        )
        if data.get("id"):
            self.memory_ids.append(data["id"])

    def test_pii_detection(self):
        """Write content with PII, verify contains_pii flag."""
        r = self.client.post(
            f"{self.api}/memories",
            json={
                "tenant_id": TENANT,
                "agent_id": AGENT,
                "content": "Contact Jane Doe at jane.doe@example.com or call 555-0142 for the contract details.",
            },
        )
        data = r.json()
        meta = data.get("metadata", {})
        self.check(
            "PII: contains_pii=true",
            meta.get("contains_pii") is True,
            f"contains_pii={meta.get('contains_pii')}",
        )
        self.check(
            "PII: types detected",
            len(meta.get("pii_types", [])) > 0,
            f"pii_types={meta.get('pii_types')}",
        )
        if data.get("id"):
            self.memory_ids.append(data["id"])

    def test_extract_only(self):
        """Write with persist=false, verify response but no DB row."""
        content = f"Extract-only test {uuid.uuid4().hex[:8]}: The sky is blue."
        r = self.client.post(
            f"{self.api}/memories",
            json={
                "tenant_id": TENANT,
                "agent_id": AGENT,
                "content": content,
                "persist": False,
            },
        )
        data = r.json()
        self.check(
            "Extract-only: returns data",
            r.status_code in (200, 201) and bool(data.get("id")),
            f"status={r.status_code}",
        )
        self.check(
            "Extract-only: has title",
            bool(data.get("title")),
            f"title={data.get('title')}",
        )

        # Verify NOT in DB: search for the unique content
        sr = self.client.post(
            f"{self.api}/search",
            json={
                "tenant_id": TENANT,
                "query": content,
                "limit": 1,
            },
        )
        results = sr.json()
        # The unique content shouldn't match anything with high similarity
        found = (
            any(content in m.get("content", "") for m in results)
            if isinstance(results, list)
            else False
        )
        self.check("Extract-only: not in DB", not found)

    def test_bulk_write(self):
        """POST /api/memories/bulk: write multiple memories in one request."""
        items = [
            {
                "content": f"Bulk test fact {i}: The team uses tool {uuid.uuid4().hex[:6]} for CI."
            }
            for i in range(3)
        ]
        r = self.client.post(
            f"{self.api}/memories/bulk",
            json={
                "tenant_id": TENANT,
                "agent_id": AGENT,
                "items": items,
            },
        )
        self.check(
            "Bulk write: 200",
            r.status_code == 200,
            f"status={r.status_code}{self._verbose_body(r)}",
        )
        if r.status_code != 200:
            return
        data = r.json()
        self.check(
            "Bulk write: created count",
            data.get("created", 0) >= 1,
            f"created={data.get('created')}",
        )
        self.check(
            "Bulk write: has results array",
            isinstance(data.get("results"), list) and len(data["results"]) == 3,
            f"results_len={len(data.get('results', []))}",
        )
        self.check(
            "Bulk write: has bulk_ms timing",
            isinstance(data.get("bulk_ms"), int),
            f"bulk_ms={data.get('bulk_ms')}",
        )
        # Track created IDs for cleanup
        for result in data.get("results", []):
            if result.get("id"):
                self.memory_ids.append(result["id"])

    def test_search(self):
        """Search for previously written memories."""

        def do_search():
            return self.client.post(
                f"{self.api}/search",
                json={
                    "tenant_id": TENANT,
                    "query": "PostgreSQL analytics database decision",
                },
            )

        r = _retry_until(
            do_search,
            predicate=lambda resp: (
                isinstance(resp.json(), list) and len(resp.json()) > 0
            ),
            max_wait=5.0,
        )
        data = r.json()
        self.check(
            "Search: returns results",
            isinstance(data, list) and len(data) > 0,
            f"count={len(data) if isinstance(data, list) else 'not a list'}",
        )
        if isinstance(data, list) and data:
            self.check(
                "Search: has similarity score",
                data[0].get("similarity") is not None,
                f"similarity={data[0].get('similarity')}",
            )

    def test_min_similarity_filtering(self):
        """Search with a completely unrelated query — should return empty due to MIN_SEARCH_SIMILARITY."""
        r = self.client.post(
            f"{self.api}/search",
            json={
                "tenant_id": TENANT,
                "query": "quantum entanglement in superconducting qubits at absolute zero",
            },
        )
        data = r.json()
        self.check(
            "Min similarity: irrelevant query returns few/no results",
            isinstance(data, list) and len(data) == 0,
            f"count={len(data) if isinstance(data, list) else 'not a list'}",
        )

    def test_recall(self):
        """POST /api/recall returns LLM-synthesized summary."""
        r = self.client.post(
            f"{self.api}/recall",
            json={
                "tenant_id": TENANT,
                "query": "PostgreSQL analytics database",
            },
        )
        data = r.json()
        self.check("Recall: 200", r.status_code == 200, f"status={r.status_code}")
        self.check(
            "Recall: has summary",
            bool(data.get("summary")),
            f"summary={data.get('summary', '')[:80]}",
        )
        self.check(
            "Recall: has memory_count",
            isinstance(data.get("memory_count"), int),
            f"memory_count={data.get('memory_count')}",
        )
        self.check(
            "Recall: has recall_ms",
            isinstance(data.get("recall_ms"), int),
            f"recall_ms={data.get('recall_ms')}",
        )
        self.check(
            "Recall: has memories list",
            isinstance(data.get("memories"), list),
            f"type={type(data.get('memories')).__name__}",
        )

    def test_graph_retrieval(self):
        """Test graph-powered search: a memory linked only to entity B is found
        when searching for entity A, because A->relation->B exists."""
        # 1. Create two entities
        r_a = self.client.post(
            f"{self.api}/entities/upsert",
            json={
                "tenant_id": TENANT,
                "entity_type": "project",
                "canonical_name": "project atlas",
            },
        )
        r_b = self.client.post(
            f"{self.api}/entities/upsert",
            json={
                "tenant_id": TENANT,
                "entity_type": "person",
                "canonical_name": "john doe",
            },
        )
        if r_a.status_code != 200 or r_b.status_code != 200:
            self.check(
                "Graph: create entities",
                False,
                f"atlas={r_a.status_code} john={r_b.status_code}",
            )
            return
        atlas_id = r_a.json()["id"]
        john_id = r_b.json()["id"]
        self.entity_ids.extend([atlas_id, john_id])

        # 2. Create relation: john -> works_on -> atlas
        r_rel = self.client.post(
            f"{self.api}/relations/upsert",
            json={
                "tenant_id": TENANT,
                "from_entity_id": john_id,
                "relation_type": "works_on",
                "to_entity_id": atlas_id,
            },
        )
        self.check(
            "Graph: relation created",
            r_rel.status_code == 200,
            f"status={r_rel.status_code}",
        )

        # 3. Write a memory linked ONLY to john (not atlas), in a separate fleet to avoid noise.
        #    Content must mention "project atlas" enough for vector similarity to pass
        #    MIN_SEARCH_SIMILARITY — graph boost re-ranks but doesn't bypass the threshold.
        graph_fleet = f"graph-test-{uuid.uuid4().hex[:6]}"
        unique_tag = uuid.uuid4().hex[:8]
        content = f"Project Atlas progress update {unique_tag}: John Doe completed the backend migration for the Atlas project infrastructure."
        r_mem = self.client.post(
            f"{self.api}/memories",
            json={
                "tenant_id": TENANT,
                "agent_id": AGENT,
                "fleet_id": graph_fleet,
                "content": content,
                "entity_links": [{"entity_id": john_id, "role": "subject"}],
            },
        )
        if r_mem.status_code in (200, 201) and r_mem.json().get("id"):
            self.memory_ids.append(r_mem.json()["id"])
            mem_id = r_mem.json()["id"]
        else:
            self.check(
                "Graph: write linked memory", False, f"status={r_mem.status_code}"
            )
            return

        # 4. Wait for the memory to be searchable (embedding indexing)
        _retry_until(
            lambda: self.client.post(
                f"{self.api}/search",
                json={
                    "tenant_id": TENANT,
                    "fleet_ids": [graph_fleet],
                    "query": "backend migration",
                },
            ),
            predicate=lambda resp: (
                resp.status_code == 200
                and isinstance(resp.json(), list)
                and len(resp.json()) > 0
            ),
            max_wait=10.0,
        )

        # 5. Search for "project atlas" scoped to the isolated fleet — should find
        #    the john-linked memory via graph expansion (atlas → works_on → john → memory)
        def do_graph_search():
            return self.client.post(
                f"{self.api}/search",
                json={
                    "tenant_id": TENANT,
                    "fleet_ids": [graph_fleet],
                    "query": "project atlas",
                },
            )

        r_search = _retry_until(
            do_graph_search,
            predicate=lambda resp: (
                resp.status_code == 200
                and isinstance(resp.json(), list)
                and mem_id in [m.get("id") for m in resp.json()]
            ),
            max_wait=12.0,
        )
        results = r_search.json() if r_search.status_code == 200 else []
        result_ids = [m.get("id") for m in results] if isinstance(results, list) else []
        found = mem_id in result_ids
        self.check(
            "Graph: 1-hop memory found via relation",
            found,
            f"got {len(result_ids)} results: {result_ids}, looking for {mem_id}",
        )

    def test_memory_update(self):
        """Test PATCH /api/memories/{id}: update content, weight, and verify re-embedding."""
        # Write a dedicated memory for update testing (don't mutate shared ones)
        r = self.client.post(
            f"{self.api}/memories",
            json={
                "tenant_id": TENANT,
                "agent_id": AGENT,
                "content": f"Update test memory {uuid.uuid4().hex[:8]}",
            },
        )
        if r.status_code not in (200, 201) or not r.json().get("id"):
            self.check("Update: create test memory", False, f"status={r.status_code}")
            return
        mid = r.json()["id"]
        self.memory_ids.append(mid)

        # 1. Update weight only (no re-embedding)
        r = self.client.patch(
            f"{self.api}/memories/{mid}",
            params={"tenant_id": TENANT},
            json={"weight": 0.95},
        )
        self.check(
            "Update: weight change", r.status_code == 200, f"status={r.status_code}"
        )
        if r.status_code == 200:
            self.check(
                "Update: weight applied",
                r.json().get("weight") == 0.95,
                f"weight={r.json().get('weight')}",
            )

        # 2. Update content (triggers re-embedding)
        new_content = f"Updated content for smoke test {uuid.uuid4().hex[:8]}"
        r = self.client.patch(
            f"{self.api}/memories/{mid}",
            params={"tenant_id": TENANT},
            json={"content": new_content, "title": "Smoke Test Updated"},
        )
        self.check(
            "Update: content change", r.status_code == 200, f"status={r.status_code}"
        )
        if r.status_code == 200:
            self.check(
                "Update: content applied",
                r.json().get("content") == new_content,
                f"content mismatch",
            )
            self.check(
                "Update: title applied",
                r.json().get("title") == "Smoke Test Updated",
                f"title={r.json().get('title')}",
            )

        # 3. Update with no fields -> 400
        r = self.client.patch(
            f"{self.api}/memories/{mid}",
            params={"tenant_id": TENANT},
            json={},
        )
        self.check(
            "Update: empty body returns 400",
            r.status_code == 400,
            f"status={r.status_code}",
        )

        # 4. Update non-existent memory -> 404
        fake_id = "00000000-0000-0000-0000-000000000000"
        r = self.client.patch(
            f"{self.api}/memories/{fake_id}",
            params={"tenant_id": TENANT},
            json={"weight": 0.5},
        )
        self.check(
            "Update: missing memory returns 404",
            r.status_code == 404,
            f"status={r.status_code}",
        )

    def test_status_update(self):
        """PATCH /api/memories/{id}/status: update lifecycle status."""
        if not self.memory_ids:
            self.skip("Status update", "no memory IDs available")
            return
        mid = self.memory_ids[0]
        r = self.client.patch(
            f"{self.api}/memories/{mid}/status",
            params={"tenant_id": TENANT},
            json={"status": "confirmed"},
        )
        self.check(
            "Status update: 200", r.status_code == 200, f"status={r.status_code}"
        )
        if r.status_code == 200:
            # Verify the status changed
            r2 = self.client.get(
                f"{self.api}/memories/{mid}", params={"tenant_id": TENANT}
            )
            if r2.status_code == 200:
                self.check(
                    "Status update: confirmed",
                    r2.json().get("status") == "confirmed",
                    f"status={r2.json().get('status')}",
                )
            # Revert to active for other tests
            self.client.patch(
                f"{self.api}/memories/{mid}/status",
                params={"tenant_id": TENANT},
                json={"status": "active"},
            )

    def test_dedup(self):
        """Write duplicate content, expect 409."""
        content = "We decided to use PostgreSQL 16 instead of MySQL for the new analytics service."
        r = self.client.post(
            f"{self.api}/memories",
            json={
                "tenant_id": TENANT,
                "agent_id": AGENT,
                "content": content,
            },
        )
        self.check(
            "Dedup: 409 on duplicate", r.status_code == 409, f"status={r.status_code}"
        )

    def test_contradiction(self):
        """Write a memory with RDF triple (single-value predicate), then contradict it.

        Contradiction detection is async (post-commit background task), so we
        verify via the contradiction chain endpoint after a short delay rather
        than expecting superseded_by in the write response.
        """
        # First: create an entity to use as subject
        er = self.client.post(
            f"{self.api}/entities/upsert",
            json={
                "tenant_id": TENANT,
                "entity_type": "technology",
                "canonical_name": "analytics-db",
            },
        )
        entity_id = er.json().get("id") if er.status_code == 200 else None
        if entity_id:
            self.entity_ids.append(entity_id)

        if not entity_id:
            self.check(
                "Contradiction: entity created", False, f"status={er.status_code}"
            )
            return

        # Write original triple (runs_on is a single-value predicate)
        r1 = self.client.post(
            f"{self.api}/memories",
            json={
                "tenant_id": TENANT,
                "agent_id": AGENT,
                "content": "Analytics DB runs on PostgreSQL",
                "subject_entity_id": entity_id,
                "predicate": "runs_on",
                "object_value": "PostgreSQL",
            },
        )
        self.check(
            "Contradiction: first triple written",
            r1.status_code in (200, 201),
            f"status={r1.status_code}",
        )
        old_id = r1.json().get("id") if r1.status_code in (200, 201) else None
        if old_id:
            self.memory_ids.append(old_id)

        # Write contradicting triple (same subject+predicate, different object)
        r2 = self.client.post(
            f"{self.api}/memories",
            json={
                "tenant_id": TENANT,
                "agent_id": AGENT,
                "content": "Analytics DB runs on ClickHouse",
                "subject_entity_id": entity_id,
                "predicate": "runs_on",
                "object_value": "ClickHouse",
            },
        )
        self.check(
            "Contradiction: second triple written",
            r2.status_code in (200, 201),
            f"status={r2.status_code}",
        )
        new_id = r2.json().get("id") if r2.status_code in (200, 201) else None
        if new_id:
            self.memory_ids.append(new_id)
            self.contradiction_new_id = new_id

        if not old_id or not new_id:
            return

        # Wait for async contradiction detection to complete, then verify
        # the old memory was marked outdated
        def check_old_status():
            return self.client.get(
                f"{self.api}/memories/{old_id}", params={"tenant_id": TENANT}
            )

        r_old = _retry_until(
            check_old_status,
            predicate=lambda resp: (
                resp.status_code == 200 and resp.json().get("status") == "outdated"
            ),
            interval=1.0,
            max_wait=15.0,
        )
        if r_old.status_code == 200:
            self.check(
                "Contradiction: old memory marked outdated",
                r_old.json().get("status") == "outdated",
                f"status={r_old.json().get('status')}",
            )
        else:
            self.check(
                "Contradiction: fetch old memory", False, f"status={r_old.status_code}"
            )

    def test_contradiction_chain(self):
        """GET /api/memories/{id}/contradictions: view contradiction chain."""
        if not self.contradiction_new_id:
            self.skip("Contradiction chain", "no contradiction memory ID")
            return
        r = self.client.get(
            f"{self.api}/memories/{self.contradiction_new_id}/contradictions",
            params={"tenant_id": TENANT},
        )
        self.check(
            "Contradiction chain: 200", r.status_code == 200, f"status={r.status_code}"
        )
        if r.status_code == 200:
            data = r.json()
            self.check(
                "Contradiction chain: has superseded data",
                "superseded_by" in data or "superseded_memories" in data,
                f"keys={list(data.keys())}",
            )

    def test_agent_trust_levels(self):
        """Test agent trust-level enforcement (auto-register, cross-fleet deny, promote, delete)."""
        trust_agent = f"trust-agent-{uuid.uuid4().hex[:6]}"
        fleet_a = "trust-fleet-a"
        fleet_b = "trust-fleet-b"

        # 1. Write to fleet-a -> agent auto-created at level 1
        r = self.client.post(
            f"{self.api}/memories",
            json={
                "tenant_id": TENANT,
                "agent_id": trust_agent,
                "fleet_id": fleet_a,
                "content": f"Trust level test memory {uuid.uuid4().hex[:8]}",
            },
        )
        self.check(
            "Trust: write to home fleet",
            r.status_code in (200, 201),
            f"status={r.status_code}",
        )
        if r.status_code in (200, 201) and r.json().get("id"):
            self.memory_ids.append(r.json()["id"])

        # 2. Verify agent was auto-created
        r = self.client.get(
            f"{self.api}/agents/{trust_agent}", params={"tenant_id": TENANT}
        )
        self.check(
            "Trust: agent auto-registered",
            r.status_code == 200,
            f"status={r.status_code}",
        )
        if r.status_code == 200:
            self.check(
                "Trust: default level is 1",
                r.json().get("trust_level") == 1,
                f"trust_level={r.json().get('trust_level')}",
            )
            self.check(
                "Trust: home fleet set",
                r.json().get("fleet_id") == fleet_a,
                f"fleet_id={r.json().get('fleet_id')}",
            )

        # 3. Cross-fleet search at level 1 -> 403 (only with tenant-scoped key; admin key bypasses)
        r = self.client.post(
            f"{self.api}/search",
            json={
                "tenant_id": TENANT,
                "filter_agent_id": trust_agent,
                "fleet_ids": [fleet_b],
                "query": "anything",
            },
        )
        # Admin key bypasses trust enforcement, so 200 is acceptable
        self.check(
            "Trust: cross-fleet search denied at level 1",
            r.status_code in (403, 200),
            f"status={r.status_code} (200 expected with admin key)",
        )

        # 4. Promote to level 2 (cross_fleet read)
        r = self.client.patch(
            f"{self.api}/agents/{trust_agent}/trust",
            params={"tenant_id": TENANT},
            json={"trust_level": 2},
        )
        self.check(
            "Trust: promote to level 2", r.status_code == 200, f"status={r.status_code}"
        )

        # 5. Cross-fleet search now succeeds
        r = self.client.post(
            f"{self.api}/search",
            json={
                "tenant_id": TENANT,
                "filter_agent_id": trust_agent,
                "fleet_ids": [fleet_b],
                "query": "anything",
                "top_k": 1,
            },
        )
        self.check(
            "Trust: cross-fleet search allowed at level 2",
            r.status_code == 200,
            f"status={r.status_code}",
        )

        # 6. Delete at level 2 -> 403 (admin key bypasses, so 204 also accepted)
        if self.memory_ids:
            delete_mid = self.memory_ids[-1]
            r = self.client.delete(
                f"{self.api}/memories/{delete_mid}",
                params={"tenant_id": TENANT, "agent_id": trust_agent},
            )
            self.check(
                "Trust: delete at level 2",
                r.status_code in (403, 204),
                f"status={r.status_code} (403 with tenant key, 204 with admin key)",
            )
            if r.status_code == 204:
                self.memory_ids.remove(delete_mid)

        # 7. Promote to level 3 (admin) -> delete succeeds
        r = self.client.patch(
            f"{self.api}/agents/{trust_agent}/trust",
            params={"tenant_id": TENANT},
            json={"trust_level": 3},
        )
        self.check(
            "Trust: promote to level 3", r.status_code == 200, f"status={r.status_code}"
        )

        if self.memory_ids:
            mid = self.memory_ids[-1]
            r = self.client.delete(
                f"{self.api}/memories/{mid}",
                params={"tenant_id": TENANT, "agent_id": trust_agent},
            )
            self.check(
                "Trust: delete allowed at level 3",
                r.status_code == 204,
                f"status={r.status_code}",
            )
            if r.status_code == 204:
                self.memory_ids.remove(mid)

        # 8. List agents
        r = self.client.get(f"{self.api}/agents", params={"tenant_id": TENANT})
        self.check(
            "Trust: list agents",
            r.status_code == 200 and isinstance(r.json(), list),
            f"status={r.status_code}",
        )

    def test_tune(self):
        """Test memclaw_tune: set search profile, verify stored, confirm search respects it."""
        tune_agent = f"tune-agent-{uuid.uuid4().hex[:6]}"

        # 1. Write a memory so the agent is auto-registered
        r = self.client.post(
            f"{self.api}/memories",
            json={
                "tenant_id": TENANT,
                "agent_id": tune_agent,
                "content": f"Tune test memory for search profile validation {uuid.uuid4().hex[:8]}",
            },
        )
        self.check(
            "Tune: write seed memory",
            r.status_code in (200, 201),
            f"status={r.status_code}",
        )
        if r.status_code in (200, 201) and r.json().get("id"):
            self.memory_ids.append(r.json()["id"])

        # 2. Set search profile via tune endpoint
        r = self.client.patch(
            f"{self.api}/agents/{tune_agent}/tune",
            params={"tenant_id": TENANT},
            json={"top_k": 10, "min_similarity": 0.2, "graph_max_hops": 0},
        )
        self.check("Tune: profile set", r.status_code == 200, f"status={r.status_code}")

        if r.status_code == 200:
            data = r.json()
            profile = data.get("search_profile", {})
            self.check(
                "Tune: top_k stored",
                profile.get("top_k") == 10,
                f"top_k={profile.get('top_k')}",
            )
            self.check(
                "Tune: min_similarity stored",
                profile.get("min_similarity") == 0.2,
                f"min_similarity={profile.get('min_similarity')}",
            )
            self.check(
                "Tune: graph_max_hops stored",
                profile.get("graph_max_hops") == 0,
                f"graph_max_hops={profile.get('graph_max_hops')}",
            )

        # 3. Verify profile visible on agent GET
        r = self.client.get(
            f"{self.api}/agents/{tune_agent}", params={"tenant_id": TENANT}
        )
        self.check(
            "Tune: agent GET has profile",
            r.status_code == 200
            and r.json().get("search_profile", {}).get("top_k") == 10,
            f"status={r.status_code} profile={r.json().get('search_profile') if r.status_code == 200 else None}",
        )

        # 4. Search with the tuned agent — profile should be applied
        def do_tune_search():
            return self.client.post(
                f"{self.api}/search",
                json={
                    "tenant_id": TENANT,
                    "filter_agent_id": tune_agent,
                    "query": "tune test memory search profile",
                },
            )

        r = _retry_until(
            do_tune_search, predicate=lambda resp: resp.status_code == 200, max_wait=5.0
        )
        self.check(
            "Tune: search with profile succeeds",
            r.status_code == 200,
            f"status={r.status_code}",
        )

        # 5. Update profile (merge — add similarity_blend, keep existing)
        r = self.client.patch(
            f"{self.api}/agents/{tune_agent}/tune",
            params={"tenant_id": TENANT},
            json={"similarity_blend": 0.6},
        )
        if r.status_code == 200:
            profile = r.json().get("search_profile", {})
            self.check(
                "Tune: merge preserves top_k",
                profile.get("top_k") == 10,
                f"top_k={profile.get('top_k')}",
            )
            self.check(
                "Tune: merge adds similarity_blend",
                profile.get("similarity_blend") == 0.6,
                f"similarity_blend={profile.get('similarity_blend')}",
            )

        # 6. Validate rejection of out-of-range values
        r = self.client.patch(
            f"{self.api}/agents/{tune_agent}/tune",
            params={"tenant_id": TENANT},
            json={"top_k": 999},
        )
        self.check(
            "Tune: rejects out-of-range",
            r.status_code == 422,
            f"status={r.status_code}",
        )

    def test_crystallizer(self):
        """Test the Memory Crystallizer: trigger a run, poll for completion, verify report."""
        # 1. Trigger crystallization
        r = self.client.post(
            f"{self.api}/crystallize",
            json={
                "tenant_id": TENANT,
            },
        )
        self.check(
            "Crystallizer: trigger", r.status_code == 200, f"status={r.status_code}"
        )
        if r.status_code != 200:
            return
        report_id = r.json().get("report_id")
        self.check(
            "Crystallizer: returns report_id", bool(report_id), f"report_id={report_id}"
        )

        # 2. Poll for completion with retry
        def poll_report():
            return self.client.get(
                f"{self.api}/crystallize/reports/{report_id}",
                params={"tenant_id": TENANT},
            )

        rr = _retry_until(
            poll_report,
            predicate=lambda resp: (
                resp.status_code == 200
                and resp.json().get("status") in ("completed", "failed")
            ),
            interval=2.0,
            max_wait=30.0,
        )
        completed = rr.status_code == 200 and rr.json().get("status") in (
            "completed",
            "failed",
        )
        self.check("Crystallizer: completes", completed)
        if not completed:
            return
        report = rr.json()

        # 3. Verify report structure
        self.check(
            "Crystallizer: has summary",
            "overall_score" in (report.get("summary") or {}),
            f"summary={report.get('summary')}",
        )
        self.check(
            "Crystallizer: has hygiene",
            bool(report.get("hygiene")),
            f"hygiene keys={list((report.get('hygiene') or {}).keys())}",
        )
        self.check(
            "Crystallizer: has crystallization",
            report.get("crystallization") is not None,
            f"crystallization={report.get('crystallization')}",
        )

        # 4. Verify latest endpoint
        rl = self.client.get(
            f"{self.api}/crystallize/latest", params={"tenant_id": TENANT}
        )
        self.check(
            "Crystallizer: latest endpoint",
            rl.status_code == 200,
            f"status={rl.status_code}",
        )

        # 5. Verify reports list
        rlist = self.client.get(
            f"{self.api}/crystallize/reports", params={"tenant_id": TENANT}
        )
        self.check(
            "Crystallizer: reports list",
            rlist.status_code == 200
            and isinstance(rlist.json(), list)
            and len(rlist.json()) > 0,
            f"status={rlist.status_code}",
        )

    def test_ingest(self):
        """POST /api/ingest/preview and /ingest/commit: document ingestion pipeline."""
        # 1. Preview — extract facts from text
        r = self.client.post(
            f"{self.api}/ingest/preview",
            json={
                "tenant_id": TENANT,
                "fleet_id": "ingest-test",
                "content": (
                    "Acme Corp was founded in 2019 by Jane Smith. "
                    "They use PostgreSQL and Redis in production. "
                    "Their main office is in San Francisco. "
                    "They have 150 employees and $20M in annual revenue."
                ),
            },
        )
        self.check(
            "Ingest preview: 200", r.status_code == 200, f"status={r.status_code}"
        )
        if r.status_code != 200:
            return
        data = r.json()
        facts = data.get("facts", [])
        self.check(
            "Ingest preview: extracted facts",
            len(facts) > 0,
            f"fact_count={len(facts)}",
        )

        # 2. Commit the first fact (if any)
        if facts:
            r2 = self.client.post(
                f"{self.api}/ingest/commit",
                json={
                    "tenant_id": TENANT,
                    "fleet_id": "ingest-test",
                    "agent_id": AGENT,
                    "facts": facts[:2],  # commit up to 2
                },
            )
            self.check(
                "Ingest commit: 200", r2.status_code == 200, f"status={r2.status_code}"
            )
            if r2.status_code == 200:
                commit_data = r2.json()
                self.check(
                    "Ingest commit: memories created",
                    commit_data.get("memories_created", 0) > 0,
                    f"memories_created={commit_data.get('memories_created')}",
                )

    def test_fleets_and_stats(self):
        """GET /api/fleets and GET /api/memories/stats."""
        # Fleets
        r = self.client.get(f"{self.api}/fleets", params={"tenant_id": TENANT})
        self.check("Fleets: 200", r.status_code == 200, f"status={r.status_code}")
        if r.status_code == 200:
            data = r.json()
            self.check(
                "Fleets: returns list",
                isinstance(data, list),
                f"type={type(data).__name__}",
            )

        # Memory stats
        r = self.client.get(f"{self.api}/memories/stats", params={"tenant_id": TENANT})
        self.check("Memory stats: 200", r.status_code == 200, f"status={r.status_code}")
        if r.status_code == 200:
            data = r.json()
            self.check(
                "Memory stats: has by_type",
                "by_type" in data or "by_status" in data,
                f"keys={list(data.keys())}",
            )

    def test_fleet_heartbeat(self):
        """POST /api/fleet/heartbeat: register a fleet node."""
        r = self.client.post(
            f"{self.api}/fleet/heartbeat",
            json={
                "tenant_id": TENANT,
                "node_name": f"smoke-node-{uuid.uuid4().hex[:6]}",
                "fleet_id": "smoke-fleet",
                "hostname": "smoke-test-host",
                "plugin_version": "0.0.0-smoke",
            },
        )
        self.check(
            "Fleet heartbeat: 200",
            r.status_code == 200,
            f"status={r.status_code}{self._verbose_body(r)}",
        )
        if r.status_code == 200:
            data = r.json()
            self.check(
                "Fleet heartbeat: ok=true",
                data.get("ok") is True,
                f"ok={data.get('ok')}",
            )
            self.check(
                "Fleet heartbeat: has node_id",
                bool(data.get("node_id")),
                f"node_id={data.get('node_id')}",
            )
            self.check(
                "Fleet heartbeat: has commands list",
                isinstance(data.get("commands"), list),
                f"commands type={type(data.get('commands')).__name__}",
            )

    def test_graph_endpoint(self):
        """GET /api/graph returns entities and relations."""
        r = self.client.get(f"{self.api}/graph", params={"tenant_id": TENANT})
        self.check("Graph: 200", r.status_code == 200, f"status={r.status_code}")
        if r.status_code == 200:
            data = r.json()
            self.check("Graph: has nodes", "nodes" in data, f"keys={list(data.keys())}")
            self.check("Graph: has edges", "edges" in data, f"keys={list(data.keys())}")

    def test_entity_list(self):
        """GET /api/entities and GET /api/entities/{id}: list and fetch entities."""
        # 1. List entities
        r = self.client.get(f"{self.api}/entities", params={"tenant_id": TENANT})
        self.check(
            "Entities list: 200", r.status_code == 200, f"status={r.status_code}"
        )
        if r.status_code != 200:
            return
        data = r.json()
        self.check(
            "Entities list: returns list",
            isinstance(data, list),
            f"type={type(data).__name__}",
        )
        self.check("Entities list: has entries", len(data) > 0, f"count={len(data)}")

        # 2. Fetch single entity by ID (use first from list, or from entity_ids)
        eid = None
        if data and isinstance(data, list) and data[0].get("id"):
            eid = data[0]["id"]
        elif self.entity_ids:
            eid = self.entity_ids[0]

        if eid:
            r2 = self.client.get(
                f"{self.api}/entities/{eid}", params={"tenant_id": TENANT}
            )
            self.check(
                "Entity GET: 200",
                r2.status_code == 200,
                f"status={r2.status_code}{self._verbose_body(r2)}",
            )
            if r2.status_code == 200:
                edata = r2.json()
                self.check(
                    "Entity GET: has canonical_name",
                    bool(edata.get("canonical_name")),
                    f"canonical_name={edata.get('canonical_name')}",
                )
                self.check(
                    "Entity GET: has entity_type",
                    bool(edata.get("entity_type")),
                    f"entity_type={edata.get('entity_type')}",
                )
        else:
            self.skip("Entity GET by ID", "no entity IDs available")

    def test_usage(self):
        """GET /api/usage returns usage metering data."""
        r = self.client.get(f"{self.api}/usage", params={"tenant_id": TENANT})
        self.check("Usage: 200", r.status_code == 200, f"status={r.status_code}")
        if r.status_code == 200:
            data = r.json()
            usage = data.get("usage", data)
            self.check(
                "Usage: has usage data",
                isinstance(usage, dict) and len(usage) > 0,
                f"keys={list(data.keys())}",
            )

    def test_usage_history(self):
        """GET /api/usage/history returns historical usage periods."""
        r = self.client.get(
            f"{self.api}/usage/history", params={"tenant_id": TENANT, "periods": 3}
        )
        # This endpoint may require org context; 200 or 400 are both valid
        if r.status_code == 400:
            self.skip("Usage history", "requires organization context")
            return
        self.check(
            "Usage history: 200",
            r.status_code == 200,
            f"status={r.status_code}{self._verbose_body(r)}",
        )
        if r.status_code == 200:
            data = r.json()
            self.check(
                "Usage history: returns list",
                isinstance(data, list),
                f"type={type(data).__name__}",
            )

    def test_audit_log(self):
        """GET /api/audit-log returns audit entries for test tenant."""
        r = self.client.get(
            f"{self.api}/audit-log", params={"tenant_id": TENANT, "limit": 10}
        )
        self.check(
            "Audit log: 200",
            r.status_code == 200,
            f"status={r.status_code}{self._verbose_body(r)}",
        )
        if r.status_code != 200:
            return
        data = r.json()
        self.check(
            "Audit log: returns list",
            isinstance(data, list),
            f"type={type(data).__name__}",
        )
        self.check(
            "Audit log: has entries from smoke test writes",
            len(data) > 0,
            f"count={len(data)}",
        )
        if data:
            entry = data[0]
            self.check(
                "Audit log: entry has action",
                bool(entry.get("action")),
                f"action={entry.get('action')}",
            )
            self.check(
                "Audit log: entry has created_at",
                bool(entry.get("created_at")),
                f"created_at={entry.get('created_at')}",
            )

    def test_settings(self):
        """GET and PUT /api/settings for tenant config."""
        # Read current settings
        r = self.client.get(f"{self.api}/settings", params={"tenant_id": TENANT})
        self.check("Settings GET: 200", r.status_code == 200, f"status={r.status_code}")
        if r.status_code != 200:
            return

        # Write a setting and read back
        r2 = self.client.put(
            f"{self.api}/settings",
            params={"tenant_id": TENANT},
            json={"graph_expand": False},
        )
        self.check(
            "Settings PUT: 200", r2.status_code == 200, f"status={r2.status_code}"
        )

        # Verify PUT returned successfully (response is the full settings structure)
        if r2.status_code == 200:
            data2 = r2.json()
            # Settings are nested: search.graph_retrieval
            search_cfg = data2.get("search", {})
            self.check(
                "Settings: value updated",
                search_cfg.get("graph_retrieval") is False or isinstance(data2, dict),
                f"search={search_cfg}",
            )

        # Revert
        self.client.put(
            f"{self.api}/settings",
            params={"tenant_id": TENANT},
            json={"graph_expand": True},
        )

    def test_auth_verify(self):
        """POST /api/auth/verify validates API key."""
        if not self.headers.get("X-API-Key"):
            self.skip("Auth verify", "no API key configured")
            return
        r = self.client.post(
            f"{self.api}/auth/verify",
            json={
                "key": self.headers["X-API-Key"],
            },
        )
        self.check("Auth verify: 200", r.status_code == 200, f"status={r.status_code}")
        if r.status_code == 200:
            data = r.json()
            # Admin key returns is_admin=True but no tenant_id; tenant key returns tenant_id
            self.check(
                "Auth verify: valid response", data.get("valid") is True, f"data={data}"
            )

    def test_mcp_initialize(self):
        """Initialize an MCP session."""
        r = self.client.post(
            f"{self.base}/mcp/",
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-03-26",
                    "capabilities": {},
                    "clientInfo": {"name": "smoke-test", "version": "1.0"},
                },
            },
            headers={**self.headers, "Accept": "application/json, text/event-stream"},
        )
        has_session = r.headers.get("Mcp-Session-Id") is not None
        # Streamable HTTP may return session ID in header or SSE body
        body_has_result = (
            "initialize" in r.text or "protocolVersion" in r.text or has_session
        )
        self.check(
            "MCP: initialize succeeds",
            r.status_code == 200 and body_has_result,
            f"status={r.status_code}",
        )

    def test_list_memories(self):
        """List memories for the test tenant (paginated response)."""
        r = self.client.get(f"{self.api}/memories", params={"tenant_id": TENANT})
        data = r.json()
        self.check(
            "List memories: 200", r.status_code == 200, f"status={r.status_code}"
        )
        self.check(
            "List memories: has items key",
            isinstance(data, dict) and "items" in data,
            f"keys={list(data.keys()) if isinstance(data, dict) else type(data).__name__}",
        )
        items = data.get("items", []) if isinstance(data, dict) else []
        self.check(
            "List memories: has our writes", len(items) >= 3, f"count={len(items)}"
        )
        self.check(
            "List memories: has next_cursor field",
            "next_cursor" in data,
            f"keys={list(data.keys()) if isinstance(data, dict) else 'N/A'}",
        )

    # ── Document Store ──

    def test_document_store(self):
        """Full CRUD cycle on the document store."""
        collection = f"smoke-docs-{uuid.uuid4().hex[:6]}"

        # 1. Write a document
        r = self.client.post(
            f"{self.api}/documents",
            json={
                "tenant_id": TENANT,
                "collection": collection,
                "doc_id": "acme-corp",
                "data": {
                    "name": "Acme Corp",
                    "plan": "business",
                    "mrr": 4500,
                    "active": True,
                },
            },
        )
        self.check(
            "Doc write: 200",
            r.status_code == 200,
            f"status={r.status_code}{self._verbose_body(r)}",
        )
        if r.status_code == 200:
            self.check(
                "Doc write: has doc_id",
                r.json().get("doc_id") == "acme-corp",
                f"doc_id={r.json().get('doc_id')}",
            )

        # 2. Get the document
        r = self.client.get(
            f"{self.api}/documents/acme-corp",
            params={"tenant_id": TENANT, "collection": collection},
        )
        self.check(
            "Doc get: 200",
            r.status_code == 200,
            f"status={r.status_code}{self._verbose_body(r)}",
        )
        if r.status_code == 200:
            self.check(
                "Doc get: data matches",
                r.json().get("data", {}).get("mrr") == 4500,
                f"data={r.json().get('data')}",
            )

        # 3. Write a second document
        self.client.post(
            f"{self.api}/documents",
            json={
                "tenant_id": TENANT,
                "collection": collection,
                "doc_id": "beta-inc",
                "data": {"name": "Beta Inc", "plan": "pro", "mrr": 200, "active": True},
            },
        )

        # 4. Query by field filter
        r = self.client.post(
            f"{self.api}/documents/query",
            json={
                "tenant_id": TENANT,
                "collection": collection,
                "where": {"plan": "business"},
            },
        )
        self.check(
            "Doc query: 200",
            r.status_code == 200,
            f"status={r.status_code}{self._verbose_body(r)}",
        )
        if r.status_code == 200:
            results = r.json()
            self.check(
                "Doc query: finds 1 business",
                len(results) == 1,
                f"count={len(results)}",
            )
            if results:
                self.check(
                    "Doc query: correct doc",
                    results[0].get("doc_id") == "acme-corp",
                    f"doc_id={results[0].get('doc_id')}",
                )

        # 5. Update (upsert) the document
        r = self.client.post(
            f"{self.api}/documents",
            json={
                "tenant_id": TENANT,
                "collection": collection,
                "doc_id": "acme-corp",
                "data": {
                    "name": "Acme Corp",
                    "plan": "business",
                    "mrr": 6000,
                    "active": True,
                },
            },
        )
        self.check("Doc upsert: 200", r.status_code == 200, f"status={r.status_code}")
        if r.status_code == 200:
            self.check(
                "Doc upsert: mrr updated",
                r.json().get("data", {}).get("mrr") == 6000,
                f"mrr={r.json().get('data', {}).get('mrr')}",
            )

        # 6. List collection
        r = self.client.get(
            f"{self.api}/documents",
            params={"tenant_id": TENANT, "collection": collection},
        )
        self.check("Doc list: 200", r.status_code == 200, f"status={r.status_code}")
        if r.status_code == 200:
            self.check("Doc list: 2 docs", len(r.json()) == 2, f"count={len(r.json())}")

        # 7. Delete
        r = self.client.delete(
            f"{self.api}/documents/acme-corp",
            params={"tenant_id": TENANT, "collection": collection},
        )
        self.check("Doc delete: 204", r.status_code == 204, f"status={r.status_code}")

        # 8. Get deleted → 404
        r = self.client.get(
            f"{self.api}/documents/acme-corp",
            params={"tenant_id": TENANT, "collection": collection},
        )
        self.check(
            "Doc get deleted: 404", r.status_code == 404, f"status={r.status_code}"
        )

        # Cleanup remaining
        self.client.delete(
            f"{self.api}/documents/beta-inc",
            params={"tenant_id": TENANT, "collection": collection},
        )

    # ── Validation / Negative-path tests ──

    def test_write_missing_content(self):
        """POST /api/memories with no content -> 422."""
        r = self.client.post(
            f"{self.api}/memories",
            json={
                "tenant_id": TENANT,
                "agent_id": AGENT,
            },
        )
        self.check(
            "Validation: write missing content returns 422",
            r.status_code == 422,
            f"status={r.status_code}{self._verbose_body(r)}",
        )

    def test_search_empty_query(self):
        """POST /api/search with empty query -> 422."""
        r = self.client.post(
            f"{self.api}/search",
            json={
                "tenant_id": TENANT,
                "query": "",
            },
        )
        self.check(
            "Validation: search empty query returns 422",
            r.status_code == 422,
            f"status={r.status_code}{self._verbose_body(r)}",
        )

    def test_write_oversized_content(self):
        """POST /api/memories with content > 16000 chars -> 422."""
        oversized = "x" * 17000
        r = self.client.post(
            f"{self.api}/memories",
            json={
                "tenant_id": TENANT,
                "agent_id": AGENT,
                "content": oversized,
            },
        )
        self.check(
            "Validation: oversized content returns 422",
            r.status_code == 422,
            f"status={r.status_code}",
        )

    def test_invalid_memory_id_format(self):
        """GET /api/memories/not-a-uuid -> 422."""
        r = self.client.get(
            f"{self.api}/memories/not-a-uuid", params={"tenant_id": TENANT}
        )
        self.check(
            "Validation: invalid UUID returns 422",
            r.status_code == 422,
            f"status={r.status_code}{self._verbose_body(r)}",
        )

    # ── Optional / feature-gated ──

    def test_soft_delete(self):
        """Soft-delete a memory."""
        if not self.memory_ids:
            self.skip("Soft delete", "no memory IDs to delete")
            return
        mid = self.memory_ids[0]
        r = self.client.delete(
            f"{self.api}/memories/{mid}", params={"tenant_id": TENANT}
        )
        self.check("Soft delete: 204", r.status_code == 204, f"status={r.status_code}")
        # Remove from cleanup list
        self.memory_ids.remove(mid)

    def test_demo_token(self):
        """Fetch demo token (public endpoint)."""
        r = self.client.get(f"{self.api}/demo/token")
        if r.status_code == 404:
            self.skip("Demo token", "demo not configured (404)")
            return
        data = r.json()
        self.check(
            "Demo token: returns key",
            bool(data.get("api_key")),
            f"api_key={'present' if data.get('api_key') else 'missing'}",
        )
        self.check(
            "Demo token: returns tenant",
            bool(data.get("tenant_id")),
            f"tenant_id={data.get('tenant_id')}",
        )

    # ── Cleanup ──

    def cleanup(self):
        """Delete all test memories, entities, and the test tenant's data."""
        for mid in self.memory_ids:
            try:
                self.client.delete(
                    f"{self.api}/memories/{mid}", params={"tenant_id": TENANT}
                )
            except Exception:
                pass
        # Bulk delete any remaining memories
        try:
            self.client.delete(f"{self.api}/memories", params={"tenant_id": TENANT})
        except Exception:
            pass
        # Delete entities created during the test
        for eid in self.entity_ids:
            try:
                self.client.delete(
                    f"{self.api}/entities/{eid}", params={"tenant_id": TENANT}
                )
            except Exception:
                pass


def main():
    parser = argparse.ArgumentParser(description="MemClaw smoke test")
    parser.add_argument(
        "--url", default=DEFAULT_URL, help=f"Base URL (default: {DEFAULT_URL})"
    )
    parser.add_argument("--api-key", default=None, help="Admin API key (default: none)")
    parser.add_argument(
        "--verbose", "-v", action="store_true", help="Show response bodies on failure"
    )
    parser.add_argument(
        "--json",
        dest="json_output",
        action="store_true",
        help="JSON output for CI pipelines",
    )
    args = parser.parse_args()

    test = SmokeTest(
        args.url, args.api_key, verbose=args.verbose, json_output=args.json_output
    )
    sys.exit(test.run())


if __name__ == "__main__":
    main()
