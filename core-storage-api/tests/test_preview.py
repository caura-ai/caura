"""Integration tests for the per-tenant deletion-preview endpoint (CAURA-696).

Uses a fresh, unique tenant_id per test so the count isn't polluted by
data other integration tests seeded under the shared tenant. The
preview is read-only and must mirror exactly what ``purge_tenant_data``
would delete — the assertions here lean on the same memory-payload
fixture as ``test_purge.py``.
"""

from __future__ import annotations

import uuid

import pytest
from httpx import AsyncClient

from tests.test_integration import PREFIX, _memory_payload

pytestmark = pytest.mark.asyncio


def _fresh_ids() -> tuple[str, str]:
    suffix = uuid.uuid4().hex[:8]
    return f"preview-tenant-{suffix}", f"preview-fleet-{suffix}"


class TestPreviewTenantCounts:
    async def test_counts_match_seeded_memory_rows(self, client: AsyncClient) -> None:
        """Seed three memories under a fresh tenant; preview should
        return ``memories=3`` plus zeros for every other table. The
        full-breakdown shape (zeros included) is part of the
        contract — callers shouldn't have to handle missing keys."""
        tenant_id, fleet_id = _fresh_ids()
        for _ in range(3):
            resp = await client.post(f"{PREFIX}/memories", json=_memory_payload(tenant_id, fleet_id))
            assert resp.status_code == 200, resp.text

        resp = await client.post(f"{PREFIX}/preview/tenant-counts", json={"tenant_id": tenant_id})
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["tenant_id"] == tenant_id
        counts = body["counts"]
        assert counts["memories"] == 3
        # Every other table in the purge set reports zero, not absent.
        for table in (
            "relations",
            "agents",
            "fleet_nodes",
            "audit_log",
            "documents",
            "organization_settings",
        ):
            assert counts.get(table) == 0, f"expected 0 for {table}, got {counts!r}"

    async def test_unknown_tenant_returns_all_zeros(self, client: AsyncClient) -> None:
        """A tenant with no data anywhere yields zeros across the
        full table set — the preview never 404s on an unknown
        tenant. (Matches the standalone-OSS shape where every fresh
        tenant starts empty.)"""
        tid = f"never-seen-{uuid.uuid4().hex[:8]}"
        resp = await client.post(f"{PREFIX}/preview/tenant-counts", json={"tenant_id": tid})
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["tenant_id"] == tid
        assert all(v == 0 for v in body["counts"].values())

    async def test_preview_is_read_only(self, client: AsyncClient) -> None:
        """Two consecutive previews must return the same counts —
        the read MUST NOT mutate state."""
        tenant_id, fleet_id = _fresh_ids()
        await client.post(f"{PREFIX}/memories", json=_memory_payload(tenant_id, fleet_id))
        first = (await client.post(f"{PREFIX}/preview/tenant-counts", json={"tenant_id": tenant_id})).json()[
            "counts"
        ]
        second = (await client.post(f"{PREFIX}/preview/tenant-counts", json={"tenant_id": tenant_id})).json()[
            "counts"
        ]
        assert first == second

    async def test_rejects_missing_tenant_id(self, client: AsyncClient) -> None:
        missing = await client.post(f"{PREFIX}/preview/tenant-counts", json={})
        assert missing.status_code == 422
        empty = await client.post(f"{PREFIX}/preview/tenant-counts", json={"tenant_id": ""})
        assert empty.status_code == 422

    async def test_rejects_malformed_body(self, client: AsyncClient) -> None:
        resp = await client.post(
            f"{PREFIX}/preview/tenant-counts",
            content="not json",
            headers={"content-type": "application/json"},
        )
        assert resp.status_code == 422

    async def test_rejects_non_object_json_body(self, client: AsyncClient) -> None:
        """Same defence the suppression router added on PR #244 round 1
        — a top-level array / number / string is valid JSON but blows
        up on ``.get`` if we don't guard."""
        for shape in ("[1,2,3]", '"a string"', "42"):
            resp = await client.post(
                f"{PREFIX}/preview/tenant-counts",
                content=shape,
                headers={"content-type": "application/json"},
            )
            assert resp.status_code == 422, f"body={shape!r}: {resp.text}"
