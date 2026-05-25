"""Integration tests for core-storage-api HTTP endpoints.

Each test exercises a real FastAPI endpoint via httpx against a live PostgreSQL
database.  Run with:

    cd core-storage-api && pytest -m integration tests/test_integration.py -v
"""

from __future__ import annotations

import hashlib
import struct
import uuid

import pytest
from httpx import AsyncClient

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

PREFIX = "/api/v1/storage"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _uid() -> str:
    """Short unique suffix to avoid collisions across test runs."""
    return uuid.uuid4().hex[:8]


def fake_embedding(seed: str, dim: int = 768) -> list[float]:
    """Deterministic unit-length embedding from a seed string."""
    h = hashlib.sha256(seed.encode()).digest()
    raw = h * (dim // len(h) + 1)
    values = [struct.unpack_from("b", raw, i)[0] / 128.0 for i in range(dim)]
    norm = sum(v * v for v in values) ** 0.5
    return [v / norm for v in values]


def _memory_payload(
    tenant_id: str,
    fleet_id: str,
    *,
    content: str | None = None,
    content_hash: str | None = None,
) -> dict:
    """Build a minimal valid memory creation payload."""
    suffix = _uid()
    content = content or f"integration test memory {suffix}"
    return {
        "tenant_id": tenant_id,
        "fleet_id": fleet_id,
        "agent_id": f"test-agent-{suffix}",
        "memory_type": "fact",
        "content": content,
        "embedding": fake_embedding(content),
        "content_hash": content_hash or hashlib.sha256(content.encode()).hexdigest(),
        "weight": 0.7,
        "visibility": "scope_team",
    }


# =====================================================================
# Health
# =====================================================================


class TestHealth:
    async def test_healthz(self, client: AsyncClient) -> None:
        resp = await client.get("/healthz")
        assert resp.status_code == 200
        assert resp.json()["status"] == "ok"

    async def test_readyz(self, client: AsyncClient) -> None:
        resp = await client.get("/readyz")
        assert resp.status_code == 200
        assert resp.json()["status"] == "ok"

    async def test_healthz_under_prefix(self, client: AsyncClient) -> None:
        resp = await client.get(f"{PREFIX}/healthz")
        assert resp.status_code == 200

    async def test_debug_pg_locks_returns_shape(self, client: AsyncClient) -> None:
        """GET /_debug/pg_locks returns the expected snapshot shape.

        CAURA-686: the endpoint is wired up and runs the
        ``pg_stat_activity`` + ``pg_blocking_pids`` snapshot against
        the test DB without raising. ``rows`` may be empty when no
        contention is happening (the normal case during a unit
        test); the contract pinned here is the response shape,
        not the row count.
        """
        resp = await client.get(f"{PREFIX}/_debug/pg_locks")
        assert resp.status_code == 200
        body = resp.json()
        assert "captured_at" in body
        assert "pg_read_all_stats" in body
        # Direct ``pg_has_role`` privilege check — always boolean,
        # independent of whether contention is present.
        assert isinstance(body["pg_read_all_stats"], bool)
        assert "rows" in body
        assert isinstance(body["rows"], list)
        # If any rows DO appear (e.g., the test's own connections),
        # they must carry the documented field surface so callers
        # (loadtest poller, operator triage) can rely on it.
        for row in body["rows"]:
            for key in (
                "pid",
                "wait_event_type",
                "wait_event",
                "xact_age_sec",
                "query_age_sec",
                "query",
                "blocked_by_pids",
                "blocked_by_n",
            ):
                assert key in row, f"missing field {key!r}; row={row!r}"


# =====================================================================
# Memories
# =====================================================================


class TestMemories:
    async def test_create_and_get_memory(
        self,
        client: AsyncClient,
        tenant_id: str,
        fleet_id: str,
    ) -> None:
        payload = _memory_payload(tenant_id, fleet_id)
        resp = await client.post(f"{PREFIX}/memories", json=payload)
        assert resp.status_code == 200, resp.text
        body = resp.json()
        memory_id = body["id"]
        assert body["content"] == payload["content"]
        assert body["tenant_id"] == tenant_id

        # GET by id
        resp2 = await client.get(f"{PREFIX}/memories/{memory_id}")
        assert resp2.status_code == 200
        assert resp2.json()["id"] == memory_id

    async def test_update_status_via_patch(
        self,
        client: AsyncClient,
        tenant_id: str,
        fleet_id: str,
    ) -> None:
        payload = _memory_payload(tenant_id, fleet_id)
        mem = (await client.post(f"{PREFIX}/memories", json=payload)).json()
        memory_id = mem["id"]

        resp = await client.patch(
            f"{PREFIX}/memories/{memory_id}",
            json={"status": "archived"},
        )
        assert resp.status_code == 200
        assert resp.json()["ok"] is True

        # Verify via GET
        updated = (await client.get(f"{PREFIX}/memories/{memory_id}")).json()
        assert updated["status"] == "archived"

    async def test_metadata_patch_jsonb_merge(
        self,
        client: AsyncClient,
        tenant_id: str,
        fleet_id: str,
    ) -> None:
        """``metadata_patch`` synthetic key triggers a JSONB ``||``
        merge that overwrites individual keys without clobbering peers.

        Two production drift hazards locked here:

        1. Pre-CAURA-595, the merge SQL was ``COALESCE(metadata, '{}'::jsonb)
           || (:patch)::jsonb``. On installations where the legacy
           ``metadata`` column is typed ``json`` (lowercase) rather than
           ``jsonb`` (the ORM declaration), Postgres raised
           ``CannotCoerceError: COALESCE could not convert type jsonb
           to json``. The cast on the column (``metadata::jsonb``)
           normalises both shapes — this test only exercises ``jsonb``
           since SQLAlchemy creates the column from the ORM model, but
           the cast is a no-op on already-jsonb columns so the test
           still validates the corrected SQL shape.
        2. ``metadata_patch`` is a SYNTHETIC key — the body's other
           top-level fields hit ``hasattr(Memory, key)`` and become
           ``UPDATE ... SET <col>``. Locks that the worker's PATCH
           shape ``{"embedding": ..., "metadata_patch": {...}}``
           applies BOTH branches in the same transaction.
        """
        payload = _memory_payload(tenant_id, fleet_id)
        # Seed a row with the hot-path's ``embedding_pending=true`` flag
        # and a sibling key that must survive the merge.
        payload["metadata_"] = {"embedding_pending": True, "trace_id": "abc"}
        mem = (await client.post(f"{PREFIX}/memories", json=payload)).json()
        memory_id = mem["id"]

        resp = await client.patch(
            f"{PREFIX}/memories/{memory_id}",
            json={"metadata_patch": {"embedding_pending": False, "summary": "done"}},
        )
        assert resp.status_code == 200, resp.text

        updated = (await client.get(f"{PREFIX}/memories/{memory_id}")).json()
        md = updated["metadata"]
        # Patched keys reflect the new values.
        assert md["embedding_pending"] is False
        assert md["summary"] == "done"
        # Sibling key untouched by the merge.
        assert md["trace_id"] == "abc"

    async def test_patch_coerces_iso_datetime_strings(
        self,
        client: AsyncClient,
        tenant_id: str,
        fleet_id: str,
    ) -> None:
        """PATCH /memories/{id} must coerce ISO ``ts_valid_*`` strings
        to ``datetime`` instances before SQLAlchemy issues the UPDATE.

        Pre-fix, asyncpg rejected ISO strings on ``DateTime(timezone=True)``
        columns with ``CannotCoerceError: invalid input for query
        argument $N: '<iso>' (expected a datetime.date or datetime.datetime
        instance, got 'str')``. Surfaced live on staging at 2026-04-26
        when the CAURA-595 async-enrich worker started PATCHing
        ``ts_valid_*`` for any memory whose LLM extraction populated
        temporal bounds. The POST route always parsed via
        ``_parse_datetimes(body)``; the PATCH route silently passed
        the strings to SQLAlchemy → asyncpg → 500. Fixed by adding
        the same parse step at the PATCH route ingress.
        """
        payload = _memory_payload(tenant_id, fleet_id)
        mem = (await client.post(f"{PREFIX}/memories", json=payload)).json()
        memory_id = mem["id"]

        # Same shape the async-enrich worker emits via
        # ``model_dump(mode="json")`` — bare ISO strings, including
        # the timezone-aware ``+00:00`` suffix.
        resp = await client.patch(
            f"{PREFIX}/memories/{memory_id}",
            json={
                "ts_valid_start": "2026-09-14T00:00:00+00:00",
                "ts_valid_end": "2026-10-15T23:59:59+00:00",
            },
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["ok"] is True

        updated = (await client.get(f"{PREFIX}/memories/{memory_id}")).json()
        # Round-trips back as ISO strings on read; confirm the values
        # we sent landed at the right ORM columns.
        assert updated["ts_valid_start"].startswith("2026-09-14T00:00:00")
        assert updated["ts_valid_end"].startswith("2026-10-15T23:59:59")

    async def test_patch_invalid_iso_datetime_returns_422(
        self,
        client: AsyncClient,
        tenant_id: str,
        fleet_id: str,
    ) -> None:
        """A malformed ISO string in any datetime field returns 422
        (client validation), not 500 (server fault). Pre-fix, the
        ``ValueError`` from ``datetime.fromisoformat`` propagated as
        an unhandled exception → 500 → DLQ for any client (including
        the async-enrich worker if an LLM ever emits a malformed
        date). The handler now wraps the parse and surfaces a clean
        422 with the offending field + value echoed back so operators
        can grep logs."""
        payload = _memory_payload(tenant_id, fleet_id)
        mem = (await client.post(f"{PREFIX}/memories", json=payload)).json()
        memory_id = mem["id"]

        resp = await client.patch(
            f"{PREFIX}/memories/{memory_id}",
            json={"ts_valid_start": "tomorrow"},  # not a valid ISO string
        )
        assert resp.status_code == 422, resp.text
        body = resp.json()
        assert "ts_valid_start" in body["detail"]
        assert "tomorrow" in body["detail"]

    async def test_batch_update_status(
        self,
        client: AsyncClient,
        tenant_id: str,
        fleet_id: str,
    ) -> None:
        p1 = _memory_payload(tenant_id, fleet_id)
        p2 = _memory_payload(tenant_id, fleet_id)
        m1 = (await client.post(f"{PREFIX}/memories", json=p1)).json()
        m2 = (await client.post(f"{PREFIX}/memories", json=p2)).json()

        resp = await client.patch(
            f"{PREFIX}/memories/batch-status",
            json={
                "updates": [
                    {"memory_id": m1["id"], "status": "archived"},
                    {"memory_id": m2["id"], "status": "archived"},
                ],
            },
        )
        assert resp.status_code == 200
        assert resp.json()["ok"] is True

    async def test_soft_delete(
        self,
        client: AsyncClient,
        tenant_id: str,
        fleet_id: str,
    ) -> None:
        payload = _memory_payload(tenant_id, fleet_id)
        mem = (await client.post(f"{PREFIX}/memories", json=payload)).json()
        memory_id = mem["id"]

        resp = await client.delete(f"{PREFIX}/memories/{memory_id}")
        assert resp.status_code == 200
        assert resp.json()["ok"] is True

        # Verify deleted_at is set
        fetched = (await client.get(f"{PREFIX}/memories/{memory_id}")).json()
        assert fetched["deleted_at"] is not None
        assert fetched["status"] == "deleted"

    async def test_patch_after_delete_does_not_resurrect(
        self,
        client: AsyncClient,
        tenant_id: str,
        fleet_id: str,
    ) -> None:
        """A PATCH that lands after a soft DELETE must no-op against
        the deleted row AND surface as 404 to the caller, not silently
        resurrect or fake-success.

        Pre-fix the UPDATE statement filtered only by ``Memory.id``, so
        a PATCH with ``status="active"`` on a deleted row would set
        ``status`` without touching ``deleted_at`` — the row showed
        ``status='active', deleted_at=<ts>``, an inconsistent state.
        The fix combines a ``SELECT ... FOR UPDATE`` snapshot lock
        (atomicity across both UPDATE branches), a per-statement
        ``deleted_at IS NULL`` predicate (defence in depth), and a 404
        response so callers can distinguish "applied" from "row is
        gone" rather than always seeing ``200 {"ok": True}``.
        """
        payload = _memory_payload(tenant_id, fleet_id)
        mem = (await client.post(f"{PREFIX}/memories", json=payload)).json()
        memory_id = mem["id"]

        # 1. Soft-delete the row.
        await client.delete(f"{PREFIX}/memories/{memory_id}")

        # 2. PATCH it. Both the column-set branch (status) and the
        # metadata-merge branch (metadata_patch) must no-op, and the
        # route must surface 404.
        resp = await client.patch(
            f"{PREFIX}/memories/{memory_id}",
            json={"status": "active", "metadata_patch": {"resurrect_attempt": True}},
        )
        assert resp.status_code == 404, resp.text

        # 3. Re-read: deleted_at is still set, status is still 'deleted',
        # metadata is unchanged.
        after = (await client.get(f"{PREFIX}/memories/{memory_id}")).json()
        assert after["deleted_at"] is not None
        assert after["status"] == "deleted"
        assert (after.get("metadata") or {}).get("resurrect_attempt") is None

    async def test_patch_on_nonexistent_memory_returns_404(
        self,
        client: AsyncClient,
    ) -> None:
        """PATCH on a memory_id that never existed must surface 404 too,
        not the legacy ``200 {"ok": True}`` silent success.

        Pre-fix this returned 200 because ``memory_update`` issued an
        UPDATE that matched zero rows and the route ignored the
        rowcount. Locked here so a future change can't quietly
        regress to silent-success.
        """
        fake_id = str(uuid.uuid4())
        resp = await client.patch(
            f"{PREFIX}/memories/{fake_id}",
            json={"status": "active"},
        )
        assert resp.status_code == 404, resp.text

    async def test_bulk_per_attempt_idempotency(
        self,
        client: AsyncClient,
        tenant_id: str,
        fleet_id: str,
    ) -> None:
        """CAURA-602: ``/memories/bulk`` is idempotent at the row level.

        First call inserts every item; second call with the same
        ``client_request_id``s returns the same canonical ids and
        ``was_inserted=False``. Eliminates the silent-create class:
        a retry can never produce a row that wasn't returned to the
        caller, regardless of whether the original response made it
        back."""
        attempt = f"bulk-attempt-{_uid()}"

        def _payload(idx: int) -> dict:
            content = f"bulk-attempt-{attempt}-{idx}"
            return {
                **_memory_payload(tenant_id, fleet_id, content=content),
                "client_request_id": f"{attempt}:{idx}",
            }

        items = [_payload(i) for i in range(3)]

        first = await client.post(f"{PREFIX}/memories/bulk", json=items)
        assert first.status_code == 200, first.text
        first_data = first.json()
        assert len(first_data) == 3
        for entry in first_data:
            assert entry["was_inserted"] is True
            assert entry["id"]
            assert entry["client_request_id"]
        ids_by_request = {e["client_request_id"]: e["id"] for e in first_data}

        # Resend the same payload. The unique index swallows the
        # inserts; the post-conflict re-query resolves every item to
        # the existing row. ``was_inserted`` flips to False so the
        # upstream core-api can label them ``duplicate_attempt``.
        second = await client.post(f"{PREFIX}/memories/bulk", json=items)
        assert second.status_code == 200, second.text
        second_data = second.json()
        assert len(second_data) == 3
        for entry in second_data:
            assert entry["was_inserted"] is False
            assert entry["id"] == ids_by_request[entry["client_request_id"]]

    async def test_bulk_missing_client_request_id_rejected(
        self,
        client: AsyncClient,
        tenant_id: str,
        fleet_id: str,
    ) -> None:
        """The storage-writer rejects bulk inserts without a
        ``client_request_id`` per item — the contract is enforced one
        layer below the core-api route so in-process callers can't
        bypass it (CAURA-602)."""
        payload = _memory_payload(tenant_id, fleet_id)  # no client_request_id
        resp = await client.post(f"{PREFIX}/memories/bulk", json=[payload])
        # Storage-writer surfaces the ValueError as 500; core-api would
        # be the layer that returns a clean 4xx — but at this layer the
        # important assertion is "no row was inserted."
        assert resp.status_code >= 400

    async def test_find_by_content_hash(
        self,
        client: AsyncClient,
        tenant_id: str,
        fleet_id: str,
    ) -> None:
        content = f"unique hash content {_uid()}"
        content_hash = hashlib.sha256(content.encode()).hexdigest()
        payload = _memory_payload(
            tenant_id,
            fleet_id,
            content=content,
            content_hash=content_hash,
        )
        await client.post(f"{PREFIX}/memories", json=payload)

        resp = await client.get(
            f"{PREFIX}/memories/by-content-hash",
            params={
                "tenant_id": tenant_id,
                "content_hash": content_hash,
                "fleet_id": fleet_id,
            },
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["memory"] is not None
        assert body["memory"]["content_hash"] == content_hash

    async def test_find_by_content_hash_not_found(
        self,
        client: AsyncClient,
        tenant_id: str,
    ) -> None:
        resp = await client.get(
            f"{PREFIX}/memories/by-content-hash",
            params={"tenant_id": tenant_id, "content_hash": "nonexistent"},
        )
        assert resp.status_code == 200
        assert resp.json()["memory"] is None

    async def test_get_stats(
        self,
        client: AsyncClient,
        tenant_id: str,
        fleet_id: str,
    ) -> None:
        # Create at least one memory so stats are non-empty
        payload = _memory_payload(tenant_id, fleet_id)
        await client.post(f"{PREFIX}/memories", json=payload)

        resp = await client.get(
            f"{PREFIX}/memories/stats",
            params={"tenant_id": tenant_id, "fleet_id": fleet_id},
        )
        assert resp.status_code == 200
        body = resp.json()
        # Stats should be a dict (exact keys depend on implementation)
        assert isinstance(body, dict)

    async def test_get_nonexistent_memory_returns_404(
        self,
        client: AsyncClient,
    ) -> None:
        fake_id = str(uuid.uuid4())
        resp = await client.get(f"{PREFIX}/memories/{fake_id}")
        assert resp.status_code == 404

    async def test_scored_search_surfaces_null_embedding_via_fts(
        self,
        client: AsyncClient,
        tenant_id: str,
        fleet_id: str,
    ) -> None:
        """CAURA-594: memories inserted with ``embedding=NULL`` (async-embed
        workflow, row persisted before the worker backfills) must remain
        discoverable via FTS. The scored-search CTE coalesces ``vec_sim``
        to 0.0 for NULL rows so they survive the pgvector operator and
        rank by text score alone until the backfill lands.

        Also asserts:
          * NULL-embedding rows with no FTS match are excluded from the
            CTE (they can't rank on ``Memory.weight`` alone and displace
            real matches).
          * ``has_embedding`` is the authoritative NULL-vs-orthogonal
            signal — ``vec_sim == 0.0`` is ambiguous with a genuinely
            orthogonal embedding.
          * An embedded row with an aligned vector still outranks a
            NULL-embedding row on the blended score.
        """
        keyword = f"caura594_{_uid()}"
        query_embedding = fake_embedding(f"query {keyword}")

        # Row A: normal memory, no keyword match and unrelated vector.
        payload_a = _memory_payload(
            tenant_id, fleet_id, content=f"standard memory about unrelated topic {_uid()}"
        )
        resp_a = await client.post(f"{PREFIX}/memories", json=payload_a)
        assert resp_a.status_code == 200, resp_a.text

        # Row B: embedding=None, keyword matches → should surface via FTS.
        payload_b = _memory_payload(tenant_id, fleet_id, content=f"urgent alert {keyword} body")
        payload_b["embedding"] = None
        resp_b = await client.post(f"{PREFIX}/memories", json=payload_b)
        assert resp_b.status_code == 200, resp_b.text
        memory_b_id = resp_b.json()["id"]

        # Row C: same keyword as B with an aligned vector → guards
        # against CASE leaking 0.0 into the embedded branch (C must
        # still outrank B).
        payload_c = _memory_payload(tenant_id, fleet_id, content=f"critical notice {keyword} body")
        payload_c["embedding"] = query_embedding
        resp_c = await client.post(f"{PREFIX}/memories", json=payload_c)
        assert resp_c.status_code == 200, resp_c.text
        memory_c_id = resp_c.json()["id"]

        # Row D: embedding=None AND no keyword match — the FTS guard on
        # the CTE must exclude this row so it can't displace real matches
        # on `weight` alone during a backfill window.
        payload_d = _memory_payload(tenant_id, fleet_id, content=f"completely unrelated pending row {_uid()}")
        payload_d["embedding"] = None
        payload_d["weight"] = 0.95  # intentionally high — must still be excluded
        resp_d = await client.post(f"{PREFIX}/memories", json=payload_d)
        assert resp_d.status_code == 200, resp_d.text
        memory_d_id = resp_d.json()["id"]

        search_body = {
            "tenant_id": tenant_id,
            "embedding": query_embedding,
            "query": keyword,
            "fleet_ids": [fleet_id],
            "top_k": 10,
            "search_params": {
                "fts_weight": 0.5,
                "freshness_floor": 0.5,
                "freshness_decay_days": 30.0,
                "recall_boost_cap": 2.0,
                "recall_decay_window_days": 7.0,
                "similarity_blend": 0.7,
            },
        }
        resp = await client.post(f"{PREFIX}/memories/scored-search", json=search_body)
        assert resp.status_code == 200, resp.text

        ids_found = {row["id"] for row in resp.json()}
        assert memory_b_id in ids_found, (
            f"NULL-embedding memory matching the FTS query should surface; found ids={ids_found!r}"
        )
        assert memory_c_id in ids_found, "embedded memory with matching content should also be returned"
        assert memory_d_id not in ids_found, (
            "NULL-embedding memory with no FTS match must NOT be returned "
            "— ranking on Memory.weight alone would displace real matches"
        )

        row_b = next(r for r in resp.json() if r["id"] == memory_b_id)
        row_c = next(r for r in resp.json() if r["id"] == memory_c_id)
        # `has_embedding` is the authoritative NULL signal.
        assert row_b["has_embedding"] is False, (
            f"expected has_embedding=False for NULL row, got {row_b['has_embedding']}"
        )
        assert row_c["has_embedding"] is True, (
            f"expected has_embedding=True for embedded row, got {row_c['has_embedding']}"
        )
        # vec_sim CASE coalesces NULL rows to the 0.0 literal.
        assert row_b["vec_sim"] == 0.0, f"expected vec_sim=0.0 for NULL-embedding row, got {row_b['vec_sim']}"
        # Embedded row with an aligned vector still outranks the NULL
        # row on the blended score — the CASE hasn't leaked into the
        # embedded branch.
        assert row_c["score"] > row_b["score"], (
            "embedded row must outrank NULL-embedding row on equal FTS; "
            f"got C.score={row_c['score']} vs B.score={row_b['score']}"
        )

        # `plainto_tsquery('english', X)` matches every non-NULL tsvector
        # whenever X normalises down to the empty tsquery — empty string,
        # whitespace-only, or stop-word-only input. Without the Python-
        # side truthiness gate the FTS guard would silently admit every
        # NULL-embedding row in those cases (vector-only / entity-lookup
        # modes), reintroducing the backfill displacement bug.
        for blank_query in ("", "   "):
            blank_resp = await client.post(
                f"{PREFIX}/memories/scored-search",
                json={**search_body, "query": blank_query},
            )
            assert blank_resp.status_code == 200, blank_resp.text
            blank_ids = {row["id"] for row in blank_resp.json()}
            assert memory_b_id not in blank_ids, (
                f"query={blank_query!r} must not admit NULL-embedding rows "
                "via the empty tsquery match-everything quirk"
            )
            assert memory_d_id not in blank_ids, (
                f"query={blank_query!r} must not admit any NULL-embedding rows"
            )

    async def test_scored_search_null_embedding_similarity_invariant_to_fts_weight(
        self,
        client: AsyncClient,
        tenant_id: str,
        fleet_id: str,
    ) -> None:
        """CAURA-679: NULL-embedding rows rank on ``fts_score`` alone — the
        ``fts_weight`` haircut no longer applies. The old unconditional
        ``(1 - w) * vec_sim + w * fts_score`` blend, combined with the
        ``vec_sim = 0.0`` sentinel for NULL rows, made NULL-row similarity
        scale linearly with ``w``: at ``w = 0.3`` it was ``0.3 * fts_score``,
        at ``w = 0.7`` it was ``0.7 * fts_score``. That haircut let
        noise-floor-cosine embedded rows (``vec_sim`` ~0.15-0.20 from
        high-dim sphere clustering) beat unembedded FTS matches on rank
        and slip past ``LIMIT top_k * overfetch_factor``.

        The ``CASE`` on ``similarity`` falls back to ``fts_score`` when
        ``embedding IS NULL``, so identical NULL rows produce identical
        ``similarity`` across any ``fts_weight``. This test pins that
        invariance — under the old blend the two values would differ by
        a factor of ``w_high / w_low``.
        """
        keyword = f"caura679_{_uid()}"
        query_embedding = fake_embedding(f"q-{keyword}")

        # NULL-embedding row with an FTS-matching keyword.
        payload = _memory_payload(tenant_id, fleet_id, content=f"urgent dispatch {keyword} body details")
        payload["embedding"] = None
        resp = await client.post(f"{PREFIX}/memories", json=payload)
        assert resp.status_code == 200, resp.text
        memory_id = resp.json()["id"]

        def make_body(fts_weight: float) -> dict:
            return {
                "tenant_id": tenant_id,
                "embedding": query_embedding,
                "query": keyword,
                "fleet_ids": [fleet_id],
                "top_k": 10,
                "search_params": {
                    "fts_weight": fts_weight,
                    "freshness_floor": 0.5,
                    "freshness_decay_days": 30.0,
                    "recall_boost_cap": 2.0,
                    "recall_decay_window_days": 7.0,
                    "similarity_blend": 0.7,
                },
            }

        resp_low = await client.post(f"{PREFIX}/memories/scored-search", json=make_body(0.3))
        assert resp_low.status_code == 200, resp_low.text
        resp_high = await client.post(f"{PREFIX}/memories/scored-search", json=make_body(0.7))
        assert resp_high.status_code == 200, resp_high.text

        row_low = next(r for r in resp_low.json() if r["id"] == memory_id)
        row_high = next(r for r in resp_high.json() if r["id"] == memory_id)

        # Sanity: still the NULL-embedding row, vec_sim still coalesced
        # to the 0.0 sentinel (the CASE on `similarity` doesn't touch
        # the CASE on `vec_sim`).
        assert row_low["has_embedding"] is False
        assert row_low["vec_sim"] == 0.0
        assert row_high["has_embedding"] is False
        assert row_high["vec_sim"] == 0.0

        # FTS contributes a real signal — sanity that the keyword
        # actually matched (else the assertion below is vacuous).
        assert row_low["similarity"] > 0.0

        # The defining assertion: similarity is invariant under
        # fts_weight changes for NULL rows. Under the OLD blend,
        # similarity_high / similarity_low would equal 0.7 / 0.3 ≈ 2.33.
        assert row_low["similarity"] == pytest.approx(row_high["similarity"], rel=1e-9), (
            "NULL-embedding similarity must not depend on fts_weight under CAURA-679; "
            f"got similarity@w=0.3={row_low['similarity']}, "
            f"similarity@w=0.7={row_high['similarity']} "
            f"(old-blend ratio would be 0.7/0.3 ≈ 2.33)"
        )

    async def test_public_counters_exclude_soft_deleted(
        self,
        client: AsyncClient,
        tenant_id: str,
        fleet_id: str,
    ) -> None:
        """``/memories/count`` (no tenant_id), ``/memories/distinct-agents``,
        and ``/memories/distinct-tenants`` must all filter ``deleted_at IS NULL``.

        Regression for the count-inflation bug: prior to the fix these
        endpoints reported tombstoned rows alongside live ones, so the
        marketing-site landing-page tiles were ~10× higher than the
        actually-queryable footprint.
        """
        # Use a fresh, dedicated agent_id so distinct-agent / distinct-tenant
        # deltas around our row are observable. Two writes in case the
        # baseline already contains tenant_id/agent_id from another test.
        unique_agent = f"counter-soft-delete-agent-{_uid()}"
        payload_one = _memory_payload(tenant_id, fleet_id)
        payload_one["agent_id"] = unique_agent
        payload_two = _memory_payload(tenant_id, fleet_id)
        payload_two["agent_id"] = unique_agent

        before_total = (await client.get(f"{PREFIX}/memories/count")).json()["count"]
        before_agents = (await client.get(f"{PREFIX}/memories/distinct-agents")).json()["count"]
        before_tenants = (await client.get(f"{PREFIX}/memories/distinct-tenants")).json()["count"]

        m1 = (await client.post(f"{PREFIX}/memories", json=payload_one)).json()
        m2 = (await client.post(f"{PREFIX}/memories", json=payload_two)).json()

        after_write = (await client.get(f"{PREFIX}/memories/count")).json()["count"]
        after_agents = (await client.get(f"{PREFIX}/memories/distinct-agents")).json()["count"]
        # tenant_count delta is loose — fixture may already contribute the tenant.
        assert after_write == before_total + 2
        assert after_agents == before_agents + 1
        assert (await client.get(f"{PREFIX}/memories/distinct-tenants")).json()["count"] >= before_tenants

        # Soft-delete both. ``deleted_at`` is set; ``status`` flips to ``"deleted"``.
        assert (await client.delete(f"{PREFIX}/memories/{m1['id']}")).status_code == 200
        assert (await client.delete(f"{PREFIX}/memories/{m2['id']}")).status_code == 200

        # Live counters must roll back our two contributions.
        post_total = (await client.get(f"{PREFIX}/memories/count")).json()["count"]
        post_agents = (await client.get(f"{PREFIX}/memories/distinct-agents")).json()["count"]
        assert post_total == before_total
        assert post_agents == before_agents

    async def test_patch_entities_is_bulk_and_idempotent(
        self,
        client: AsyncClient,
        tenant_id: str,
        fleet_id: str,
    ) -> None:
        # CAURA-686: ``PATCH /memories/{id}/entities`` is a single bulk
        # ``INSERT … ON CONFLICT (memory_id, entity_id) DO NOTHING``. Two
        # behaviours are pinned here:
        #   1. All N links land via one PATCH — re-PATCHing the same body
        #      is a no-op, not an ``IntegrityError`` on the PK.
        #   2. ``ON CONFLICT DO NOTHING`` preserves the *original* role
        #      when the same ``(memory_id, entity_id)`` is re-sent with a
        #      different role. This is the contract the route relies on
        #      to keep concurrent storage-api writes from serialising on
        #      ``Lock/transactionid``.
        mem = (
            await client.post(
                f"{PREFIX}/memories",
                json=_memory_payload(tenant_id, fleet_id),
            )
        ).json()
        memory_id = mem["id"]

        entity_ids: list[str] = []
        for _ in range(3):
            entity = (
                await client.post(
                    f"{PREFIX}/entities",
                    json={
                        "tenant_id": tenant_id,
                        "fleet_id": fleet_id,
                        "entity_type": "person",
                        "canonical_name": f"BulkLink-{_uid()}",
                    },
                )
            ).json()
            entity_ids.append(entity["id"])

        first = [{"entity_id": eid, "role": "mentioned"} for eid in entity_ids]
        resp = await client.patch(
            f"{PREFIX}/memories/{memory_id}/entities",
            json={"entity_links": first},
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["ok"] is True

        links_after_first = (
            await client.post(
                f"{PREFIX}/memories/entity-links",
                json={"memory_ids": [memory_id]},
            )
        ).json()[memory_id]
        assert sorted(link["entity_id"] for link in links_after_first) == sorted(entity_ids)
        assert {link["role"] for link in links_after_first} == {"mentioned"}

        # Re-PATCH with two old entity_ids (different role) + one new
        # entity. The conflicting rows must be silently skipped (original
        # role retained); the new row must be inserted.
        new_entity = (
            await client.post(
                f"{PREFIX}/entities",
                json={
                    "tenant_id": tenant_id,
                    "fleet_id": fleet_id,
                    "entity_type": "person",
                    "canonical_name": f"BulkLink-{_uid()}",
                },
            )
        ).json()
        second = [
            {"entity_id": entity_ids[0], "role": "subject"},
            {"entity_id": entity_ids[1], "role": "object"},
            {"entity_id": new_entity["id"], "role": "mentioned"},
        ]
        resp2 = await client.patch(
            f"{PREFIX}/memories/{memory_id}/entities",
            json={"entity_links": second},
        )
        assert resp2.status_code == 200, resp2.text

        final_links = (
            await client.post(
                f"{PREFIX}/memories/entity-links",
                json={"memory_ids": [memory_id]},
            )
        ).json()[memory_id]
        by_entity = {link["entity_id"]: link["role"] for link in final_links}
        assert by_entity == {
            entity_ids[0]: "mentioned",
            entity_ids[1]: "mentioned",
            entity_ids[2]: "mentioned",
            new_entity["id"]: "mentioned",
        }


# =====================================================================
# Entities
# =====================================================================


class TestEntities:
    async def test_create_and_get_entity(
        self,
        client: AsyncClient,
        tenant_id: str,
        fleet_id: str,
    ) -> None:
        name = f"TestEntity-{_uid()}"
        payload = {
            "tenant_id": tenant_id,
            "fleet_id": fleet_id,
            "entity_type": "person",
            "canonical_name": name,
            "attributes": {"role": "engineer"},
        }
        resp = await client.post(f"{PREFIX}/entities", json=payload)
        assert resp.status_code == 200, resp.text
        body = resp.json()
        entity_id = body["id"]
        assert body["canonical_name"] == name

        # GET by id
        resp2 = await client.get(f"{PREFIX}/entities/{entity_id}")
        assert resp2.status_code == 200
        assert resp2.json()["canonical_name"] == name

    async def test_find_exact_entity_by_name(
        self,
        client: AsyncClient,
        tenant_id: str,
        fleet_id: str,
    ) -> None:
        name = f"ExactMatch-{_uid()}"
        payload = {
            "tenant_id": tenant_id,
            "fleet_id": fleet_id,
            "entity_type": "person",
            "canonical_name": name,
        }
        await client.post(f"{PREFIX}/entities", json=payload)

        resp = await client.get(
            f"{PREFIX}/entities/by-name",
            params={
                "tenant_id": tenant_id,
                "name": name,
                "entity_type": "person",
                "fleet_id": fleet_id,
            },
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["entity"] is not None
        assert body["entity"]["canonical_name"] == name

    async def test_find_exact_entity_not_found(
        self,
        client: AsyncClient,
        tenant_id: str,
    ) -> None:
        resp = await client.get(
            f"{PREFIX}/entities/by-name",
            params={
                "tenant_id": tenant_id,
                "name": "NoSuchEntity",
                "entity_type": "person",
            },
        )
        assert resp.status_code == 200
        assert resp.json()["entity"] is None

    async def test_create_relation(
        self,
        client: AsyncClient,
        tenant_id: str,
        fleet_id: str,
    ) -> None:
        # Create two entities
        e1 = (
            await client.post(
                f"{PREFIX}/entities",
                json={
                    "tenant_id": tenant_id,
                    "fleet_id": fleet_id,
                    "entity_type": "person",
                    "canonical_name": f"Alice-{_uid()}",
                },
            )
        ).json()
        e2 = (
            await client.post(
                f"{PREFIX}/entities",
                json={
                    "tenant_id": tenant_id,
                    "fleet_id": fleet_id,
                    "entity_type": "person",
                    "canonical_name": f"Bob-{_uid()}",
                },
            )
        ).json()

        resp = await client.post(
            f"{PREFIX}/entities/relations",
            json={
                "tenant_id": tenant_id,
                "fleet_id": fleet_id,
                "from_entity_id": e1["id"],
                "relation_type": "works_with",
                "to_entity_id": e2["id"],
                "weight": 0.9,
            },
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["relation_type"] == "works_with"
        assert body["from_entity_id"] == e1["id"]
        assert body["to_entity_id"] == e2["id"]

    async def test_create_relation_duplicate_upserts(
        self,
        client: AsyncClient,
        tenant_id: str,
        fleet_id: str,
    ) -> None:
        """A second POST with the same natural key
        ``(tenant_id, from_entity_id, relation_type, to_entity_id)``
        upserts in place rather than 500'ing on the unique constraint.
        Pre-fix this raised ``IntegrityError: duplicate key value
        violates unique constraint "uq_relations_natural_key"`` and
        cascaded into bulk_write 5xx + silent-create-bulk during the
        2026-04-26 load test."""
        e1 = (
            await client.post(
                f"{PREFIX}/entities",
                json={
                    "tenant_id": tenant_id,
                    "fleet_id": fleet_id,
                    "entity_type": "person",
                    "canonical_name": f"Carol-{_uid()}",
                },
            )
        ).json()
        e2 = (
            await client.post(
                f"{PREFIX}/entities",
                json={
                    "tenant_id": tenant_id,
                    "fleet_id": fleet_id,
                    "entity_type": "person",
                    "canonical_name": f"Dan-{_uid()}",
                },
            )
        ).json()

        first = await client.post(
            f"{PREFIX}/entities/relations",
            json={
                "tenant_id": tenant_id,
                "fleet_id": fleet_id,
                "from_entity_id": e1["id"],
                "relation_type": "manages",
                "to_entity_id": e2["id"],
                "weight": 0.5,
            },
        )
        assert first.status_code == 200, first.text
        first_id = first.json()["id"]

        # Second POST with the same natural key but a different weight +
        # evidence: must succeed (upsert), preserve the row id, and pick
        # up the new weight.
        second = await client.post(
            f"{PREFIX}/entities/relations",
            json={
                "tenant_id": tenant_id,
                "fleet_id": fleet_id,
                "from_entity_id": e1["id"],
                "relation_type": "manages",
                "to_entity_id": e2["id"],
                "weight": 0.9,
            },
        )
        assert second.status_code == 200, second.text
        body = second.json()
        assert body["id"] == first_id, "upsert must preserve the existing row id"
        assert body["weight"] == 0.9, "upsert must refresh weight on conflict"

    async def test_entity_with_linked_memories(
        self,
        client: AsyncClient,
        tenant_id: str,
        fleet_id: str,
    ) -> None:
        # Create entity
        entity = (
            await client.post(
                f"{PREFIX}/entities",
                json={
                    "tenant_id": tenant_id,
                    "fleet_id": fleet_id,
                    "entity_type": "concept",
                    "canonical_name": f"LinkedConcept-{_uid()}",
                },
            )
        ).json()
        entity_id = entity["id"]

        # Create memory and link it
        mem_payload = _memory_payload(tenant_id, fleet_id)
        mem = (await client.post(f"{PREFIX}/memories", json=mem_payload)).json()
        memory_id = mem["id"]

        link_resp = await client.post(
            f"{PREFIX}/entities/memory-links/create",
            json={
                "memory_id": memory_id,
                "entity_id": entity_id,
                "role": "subject",
            },
        )
        assert link_resp.status_code == 200

        # Get entity with linked memories
        resp = await client.get(
            f"{PREFIX}/entities/linked-memories/{entity_id}",
            params={"tenant_id": tenant_id},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["entity"]["id"] == entity_id
        assert len(body["linked_memories"]) >= 1

    async def test_get_nonexistent_entity_returns_404(
        self,
        client: AsyncClient,
    ) -> None:
        fake_id = str(uuid.uuid4())
        resp = await client.get(f"{PREFIX}/entities/{fake_id}")
        assert resp.status_code == 404


# =====================================================================
# Agents
# =====================================================================


class TestAgents:
    async def test_create_and_get_agent(
        self,
        client: AsyncClient,
        tenant_id: str,
        fleet_id: str,
    ) -> None:
        agent_id = f"agent-{_uid()}"
        payload = {
            "tenant_id": tenant_id,
            "fleet_id": fleet_id,
            "agent_id": agent_id,
            "trust_level": 2,
        }
        resp = await client.post(f"{PREFIX}/agents", json=payload)
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["agent_id"] == agent_id
        assert body["trust_level"] == 2

        # GET
        resp2 = await client.get(
            f"{PREFIX}/agents/{agent_id}",
            params={"tenant_id": tenant_id},
        )
        assert resp2.status_code == 200
        assert resp2.json()["agent_id"] == agent_id

    async def test_list_agents(
        self,
        client: AsyncClient,
        tenant_id: str,
        fleet_id: str,
    ) -> None:
        agent_id = f"agent-list-{_uid()}"
        await client.post(
            f"{PREFIX}/agents",
            json={
                "tenant_id": tenant_id,
                "fleet_id": fleet_id,
                "agent_id": agent_id,
            },
        )

        resp = await client.get(
            f"{PREFIX}/agents",
            params={"tenant_id": tenant_id},
        )
        assert resp.status_code == 200
        agents = resp.json()
        assert isinstance(agents, list)
        assert any(a["agent_id"] == agent_id for a in agents)

    async def test_update_trust_level(
        self,
        client: AsyncClient,
        tenant_id: str,
        fleet_id: str,
    ) -> None:
        agent_id = f"agent-trust-{_uid()}"
        await client.post(
            f"{PREFIX}/agents",
            json={
                "tenant_id": tenant_id,
                "fleet_id": fleet_id,
                "agent_id": agent_id,
                "trust_level": 1,
            },
        )

        resp = await client.patch(
            f"{PREFIX}/agents/{agent_id}/trust",
            json={
                "tenant_id": tenant_id,
                "trust_level": 3,
                "fleet_id": fleet_id,
            },
        )
        assert resp.status_code == 200
        assert resp.json()["ok"] is True

        # Verify
        agent = (
            await client.get(
                f"{PREFIX}/agents/{agent_id}",
                params={"tenant_id": tenant_id},
            )
        ).json()
        assert agent["trust_level"] == 3

    async def test_get_nonexistent_agent_returns_404(
        self,
        client: AsyncClient,
        tenant_id: str,
    ) -> None:
        resp = await client.get(
            f"{PREFIX}/agents/nonexistent-agent",
            params={"tenant_id": tenant_id},
        )
        assert resp.status_code == 404


# =====================================================================
# Documents
# =====================================================================


class TestDocuments:
    async def test_upsert_and_get_document(
        self,
        client: AsyncClient,
        tenant_id: str,
        fleet_id: str,
    ) -> None:
        doc_id = f"doc-{_uid()}"
        collection = "test-collection"
        payload = {
            "tenant_id": tenant_id,
            "fleet_id": fleet_id,
            "collection": collection,
            "doc_id": doc_id,
            "data": {"title": "Test Document", "body": "Hello world"},
        }
        resp = await client.post(f"{PREFIX}/documents", json=payload)
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["doc_id"] == doc_id
        assert body["data"]["title"] == "Test Document"

        # GET by doc_id
        resp2 = await client.get(
            f"{PREFIX}/documents/{doc_id}",
            params={"tenant_id": tenant_id, "collection": collection},
        )
        assert resp2.status_code == 200
        assert resp2.json()["doc_id"] == doc_id

    async def test_upsert_updates_existing(
        self,
        client: AsyncClient,
        tenant_id: str,
        fleet_id: str,
    ) -> None:
        doc_id = f"doc-upsert-{_uid()}"
        collection = "test-collection"
        base = {
            "tenant_id": tenant_id,
            "fleet_id": fleet_id,
            "collection": collection,
            "doc_id": doc_id,
        }
        # Create
        await client.post(
            f"{PREFIX}/documents",
            json={**base, "data": {"v": 1}},
        )
        # Upsert with new data
        resp = await client.post(
            f"{PREFIX}/documents",
            json={**base, "data": {"v": 2}},
        )
        assert resp.status_code == 200
        assert resp.json()["data"]["v"] == 2

    async def test_query_documents(
        self,
        client: AsyncClient,
        tenant_id: str,
        fleet_id: str,
    ) -> None:
        collection = f"query-coll-{_uid()}"
        for i in range(3):
            await client.post(
                f"{PREFIX}/documents",
                json={
                    "tenant_id": tenant_id,
                    "fleet_id": fleet_id,
                    "collection": collection,
                    "doc_id": f"qdoc-{i}",
                    "data": {"index": i},
                },
            )

        resp = await client.post(
            f"{PREFIX}/documents/query",
            json={
                "tenant_id": tenant_id,
                "collection": collection,
                "fleet_id": fleet_id,
            },
        )
        assert resp.status_code == 200
        docs = resp.json()
        assert isinstance(docs, list)
        assert len(docs) >= 3

    async def test_delete_document(
        self,
        client: AsyncClient,
        tenant_id: str,
        fleet_id: str,
    ) -> None:
        doc_id = f"doc-del-{_uid()}"
        collection = "test-collection"
        await client.post(
            f"{PREFIX}/documents",
            json={
                "tenant_id": tenant_id,
                "fleet_id": fleet_id,
                "collection": collection,
                "doc_id": doc_id,
                "data": {"temp": True},
            },
        )

        resp = await client.delete(
            f"{PREFIX}/documents/{doc_id}",
            params={"tenant_id": tenant_id, "collection": collection},
        )
        assert resp.status_code == 200
        assert "deleted_id" in resp.json()

        # Verify deleted
        resp2 = await client.get(
            f"{PREFIX}/documents/{doc_id}",
            params={"tenant_id": tenant_id, "collection": collection},
        )
        assert resp2.status_code == 404

    async def test_get_nonexistent_document_returns_404(
        self,
        client: AsyncClient,
        tenant_id: str,
    ) -> None:
        resp = await client.get(
            f"{PREFIX}/documents/nonexistent-doc",
            params={"tenant_id": tenant_id, "collection": "nope"},
        )
        assert resp.status_code == 404


# =====================================================================
# Fleet
# =====================================================================


class TestFleet:
    async def test_upsert_and_list_nodes(
        self,
        client: AsyncClient,
        tenant_id: str,
        fleet_id: str,
    ) -> None:
        node_name = f"node-{_uid()}"
        payload = {
            "tenant_id": tenant_id,
            "fleet_id": fleet_id,
            "node_name": node_name,
            "hostname": "test-host",
            "ip": "127.0.0.1",
            "openclaw_version": "0.1.0",
        }
        resp = await client.post(f"{PREFIX}/fleet/nodes", json=payload)
        assert resp.status_code == 200, resp.text
        body = resp.json()
        node_id = body["id"]
        assert node_id is not None

        # List nodes
        resp2 = await client.get(
            f"{PREFIX}/fleet/nodes",
            params={"tenant_id": tenant_id, "fleet_id": fleet_id},
        )
        assert resp2.status_code == 200
        nodes = resp2.json()
        assert isinstance(nodes, list)
        assert any(n["node_name"] == node_name for n in nodes)

    async def test_upsert_node_is_idempotent(
        self,
        client: AsyncClient,
        tenant_id: str,
        fleet_id: str,
    ) -> None:
        node_name = f"node-idem-{_uid()}"
        payload = {
            "tenant_id": tenant_id,
            "fleet_id": fleet_id,
            "node_name": node_name,
            "hostname": "host-v1",
        }
        r1 = (await client.post(f"{PREFIX}/fleet/nodes", json=payload)).json()

        # Upsert again with updated hostname
        payload["hostname"] = "host-v2"
        r2 = (await client.post(f"{PREFIX}/fleet/nodes", json=payload)).json()

        # Same node id
        assert r1["id"] == r2["id"]

    async def test_create_and_list_commands(
        self,
        client: AsyncClient,
        tenant_id: str,
        fleet_id: str,
    ) -> None:
        # Create a node first
        node_name = f"cmd-node-{_uid()}"
        node_resp = await client.post(
            f"{PREFIX}/fleet/nodes",
            json={
                "tenant_id": tenant_id,
                "fleet_id": fleet_id,
                "node_name": node_name,
            },
        )
        node_id = node_resp.json()["id"]

        # Create a command
        cmd_payload = {
            "tenant_id": tenant_id,
            "node_id": node_id,
            "command": "sync_memories",
            "payload": {"batch_size": 100},
        }
        resp = await client.post(f"{PREFIX}/fleet/commands", json=cmd_payload)
        assert resp.status_code == 200, resp.text
        cmd = resp.json()
        assert cmd["command"] == "sync_memories"
        assert cmd["status"] == "pending"

        # List commands
        resp2 = await client.get(
            f"{PREFIX}/fleet/commands",
            params={"tenant_id": tenant_id},
        )
        assert resp2.status_code == 200
        commands = resp2.json()
        assert isinstance(commands, list)
        assert any(c["id"] == cmd["id"] for c in commands)

    async def test_get_pending_commands(
        self,
        client: AsyncClient,
        tenant_id: str,
        fleet_id: str,
    ) -> None:
        node_name = f"pending-node-{_uid()}"
        node_resp = await client.post(
            f"{PREFIX}/fleet/nodes",
            json={
                "tenant_id": tenant_id,
                "fleet_id": fleet_id,
                "node_name": node_name,
            },
        )
        node_id = node_resp.json()["id"]

        # Create two commands
        for i in range(2):
            await client.post(
                f"{PREFIX}/fleet/commands",
                json={
                    "tenant_id": tenant_id,
                    "node_id": node_id,
                    "command": f"cmd-{i}",
                },
            )

        resp = await client.get(
            f"{PREFIX}/fleet/commands/pending/{node_name}",
            params={"tenant_id": tenant_id},
        )
        assert resp.status_code == 200
        pending = resp.json()
        assert isinstance(pending, list)
        assert len(pending) >= 2


# =====================================================================
# Keystones (CAURA-000)
# =====================================================================


class TestKeystones:
    """Keystones live in collection ``_keystones``. Trust is enforced
    upstream in core-api; storage tests just exercise the contract:
    validation, scope union, system-collection guard, audit emission.
    """

    @staticmethod
    def _payload(
        *,
        doc_id: str,
        scope: str,
        fleet_id: str | None = None,
        agent_id: str | None = None,
        weight: str = "med",
        title: str = "rule",
        content: str = "do the thing",
    ) -> dict:
        body: dict = {
            "doc_id": doc_id,
            "title": title,
            "content": content,
            "weight": weight,
            "scope": scope,
        }
        if fleet_id is not None:
            body["fleet_id"] = fleet_id
        if agent_id is not None:
            body["agent_id"] = agent_id
        return body

    async def test_upsert_tenant_scope_and_list(
        self,
        client: AsyncClient,
        tenant_id: str,
    ) -> None:
        doc_id = f"ks-tenant-{_uid()}"
        payload = {
            "tenant_id": tenant_id,
            **self._payload(doc_id=doc_id, scope="tenant", weight="high"),
        }
        resp = await client.post(f"{PREFIX}/keystones", json=payload)
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["doc_id"] == doc_id
        assert body["collection"] == "_keystones"
        assert body["data"]["weight"] == 100  # 'high' bucket → 100
        assert body["fleet_id"] is None

        # Listing without fleet_id still returns tenant-scope rule.
        resp2 = await client.get(
            f"{PREFIX}/keystones",
            params={"tenant_id": tenant_id},
        )
        assert resp2.status_code == 200
        rules = resp2.json()
        assert any(r["doc_id"] == doc_id for r in rules)

    async def test_scope_union(
        self,
        client: AsyncClient,
        tenant_id: str,
    ) -> None:
        # Use a per-test fleet so this test's rules don't collide with
        # leftovers from other tests in the same tenant.
        fleet_id = f"fleet-union-{_uid()}"
        agent_id = f"agent-{_uid()}"
        created_doc_ids: list[str] = []
        # Seed one rule per scope.
        for scope, suffix, fid, aid, weight in [
            ("tenant", "t", None, None, "low"),
            ("fleet", "f", fleet_id, None, "med"),
            ("agent", "a", fleet_id, agent_id, "high"),
        ]:
            doc_id = f"ks-{suffix}-{_uid()}"
            payload = {
                "tenant_id": tenant_id,
                **self._payload(
                    doc_id=doc_id,
                    scope=scope,
                    fleet_id=fid,
                    agent_id=aid,
                    weight=weight,
                ),
            }
            resp = await client.post(f"{PREFIX}/keystones", json=payload)
            assert resp.status_code == 200, resp.text
            created_doc_ids.append(doc_id)

        # Full scope set (tenant + fleet + agent) should return our 3,
        # ordered by weight DESC: agent(100) > fleet(50) > tenant(25).
        # Filter to doc_ids we just created so leftover tenant-scope
        # rules from earlier tests in the same tenant don't mask order.
        resp = await client.get(
            f"{PREFIX}/keystones",
            params={
                "tenant_id": tenant_id,
                "fleet_id": fleet_id,
                "agent_id": agent_id,
            },
        )
        assert resp.status_code == 200
        rules_ours = [r for r in resp.json() if r["doc_id"] in created_doc_ids]
        assert len(rules_ours) == 3, f"expected 3 of ours, got {rules_ours}"
        scopes_in_order = [r["data"]["scope"] for r in rules_ours]
        assert scopes_in_order == ["agent", "fleet", "tenant"]

        # Without agent_id → no agent rules.
        resp2 = await client.get(
            f"{PREFIX}/keystones",
            params={"tenant_id": tenant_id, "fleet_id": fleet_id},
        )
        assert resp2.status_code == 200
        scopes_no_agent = {r["data"]["scope"] for r in resp2.json() if r["doc_id"] in created_doc_ids}
        assert "agent" not in scopes_no_agent

    async def test_validation_rejects_bad_scope(
        self,
        client: AsyncClient,
        tenant_id: str,
    ) -> None:
        resp = await client.post(
            f"{PREFIX}/keystones",
            json={
                "tenant_id": tenant_id,
                "doc_id": f"ks-bad-{_uid()}",
                "title": "x",
                "content": "y",
                "scope": "global",  # not in {tenant, fleet, agent}
            },
        )
        assert resp.status_code == 422

    async def test_validation_agent_scope_requires_agent_id(
        self,
        client: AsyncClient,
        tenant_id: str,
        fleet_id: str,
    ) -> None:
        resp = await client.post(
            f"{PREFIX}/keystones",
            json={
                "tenant_id": tenant_id,
                "doc_id": f"ks-bad-{_uid()}",
                "title": "x",
                "content": "y",
                "scope": "agent",
                "fleet_id": fleet_id,
                # no agent_id
            },
        )
        assert resp.status_code == 422

    async def test_validation_tenant_scope_rejects_fleet(
        self,
        client: AsyncClient,
        tenant_id: str,
        fleet_id: str,
    ) -> None:
        resp = await client.post(
            f"{PREFIX}/keystones",
            json={
                "tenant_id": tenant_id,
                "doc_id": f"ks-bad-{_uid()}",
                "title": "x",
                "content": "y",
                "scope": "tenant",
                "fleet_id": fleet_id,  # disallowed for tenant scope
            },
        )
        assert resp.status_code == 422

    async def test_validation_rejects_unknown_weight(
        self,
        client: AsyncClient,
        tenant_id: str,
    ) -> None:
        resp = await client.post(
            f"{PREFIX}/keystones",
            json={
                "tenant_id": tenant_id,
                "doc_id": f"ks-bad-{_uid()}",
                "title": "x",
                "content": "y",
                "scope": "tenant",
                "weight": 99,  # not a bucket label
            },
        )
        assert resp.status_code == 422

    async def test_system_collection_guard_on_documents_endpoint(
        self,
        client: AsyncClient,
        tenant_id: str,
    ) -> None:
        """Public /documents endpoint must reject _keystones writes.

        Authoring goes through /keystones; bypassing it would skip
        validation and audit.
        """
        resp = await client.post(
            f"{PREFIX}/documents",
            json={
                "tenant_id": tenant_id,
                "collection": "_keystones",
                "doc_id": f"ks-bypass-{_uid()}",
                "data": {"hi": "there"},
            },
        )
        assert resp.status_code == 400
        assert "system-managed" in resp.json()["detail"]

    async def test_delete_keystone(
        self,
        client: AsyncClient,
        tenant_id: str,
    ) -> None:
        doc_id = f"ks-del-{_uid()}"
        await client.post(
            f"{PREFIX}/keystones",
            json={
                "tenant_id": tenant_id,
                **self._payload(doc_id=doc_id, scope="tenant"),
            },
        )
        resp = await client.delete(
            f"{PREFIX}/keystones/{doc_id}",
            params={"tenant_id": tenant_id},
        )
        assert resp.status_code == 200
        assert "deleted_id" in resp.json()

        # Idempotency: second delete is 404, not 500.
        resp2 = await client.delete(
            f"{PREFIX}/keystones/{doc_id}",
            params={"tenant_id": tenant_id},
        )
        assert resp2.status_code == 404

    async def test_get_rejects_agent_without_fleet(
        self,
        client: AsyncClient,
        tenant_id: str,
    ) -> None:
        """agent_id without fleet_id can't resolve agent-scope rows; the
        endpoint must surface this as 422 rather than silently degrading
        to fleet/tenant scope."""
        resp = await client.get(
            f"{PREFIX}/keystones",
            params={
                "tenant_id": tenant_id,
                "agent_id": f"agent-{_uid()}",
            },
        )
        assert resp.status_code == 422

    async def test_upsert_replaces_existing(
        self,
        client: AsyncClient,
        tenant_id: str,
    ) -> None:
        doc_id = f"ks-upsert-{_uid()}"
        base = {
            "tenant_id": tenant_id,
            **self._payload(doc_id=doc_id, scope="tenant", weight="low"),
        }
        await client.post(f"{PREFIX}/keystones", json=base)
        resp = await client.post(
            f"{PREFIX}/keystones",
            json={**base, "weight": "high", "content": "updated"},
        )
        assert resp.status_code == 200
        assert resp.json()["data"]["weight"] == 100
        assert resp.json()["data"]["content"] == "updated"
