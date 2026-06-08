"""Integration tests for the tenant_suppression mirror (CAURA-694).

Covers the storage-side semantics:

  - upsert(``suppress``)  → row exists with ``suppressed_at`` set
  - upsert(``restore``)   → row exists with ``suppressed_at`` cleared
  - duplicate ``suppress`` → the ORIGINAL ``suppressed_at`` wins
    (idempotency of a re-delivered Pub/Sub message must not advance
    the "when did this start" timestamp)
  - GET unknown tenant    → ``is_suppressed: false`` (the standalone-OSS
                            shape: empty table reads as "live")
  - bad action / missing tenant_id → 422
"""

from __future__ import annotations

import uuid

import pytest
from httpx import AsyncClient

from tests.test_integration import PREFIX

pytestmark = pytest.mark.asyncio


def _fresh_tenant() -> str:
    return f"supp-tenant-{uuid.uuid4().hex[:8]}"


class TestTenantSuppression:
    async def test_suppress_sets_row_and_get_returns_true(self, client: AsyncClient) -> None:
        tid = _fresh_tenant()
        resp = await client.post(
            f"{PREFIX}/tenant-suppression",
            json={"tenant_id": tid, "action": "suppress", "updated_by": "ops"},
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["tenant_id"] == tid
        assert body["suppressed_at"] is not None
        assert body["updated_by"] == "ops"

        got = await client.get(f"{PREFIX}/tenant-suppression/{tid}")
        assert got.status_code == 200
        assert got.json() == {"tenant_id": tid, "is_suppressed": True}

    async def test_restore_clears_row_and_get_returns_false(self, client: AsyncClient) -> None:
        tid = _fresh_tenant()
        await client.post(
            f"{PREFIX}/tenant-suppression",
            json={"tenant_id": tid, "action": "suppress"},
        )
        resp = await client.post(
            f"{PREFIX}/tenant-suppression",
            json={"tenant_id": tid, "action": "restore", "updated_by": "ops"},
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["suppressed_at"] is None

        got = await client.get(f"{PREFIX}/tenant-suppression/{tid}")
        assert got.json() == {"tenant_id": tid, "is_suppressed": False}

    async def test_duplicate_suppress_preserves_original_timestamp(self, client: AsyncClient) -> None:
        """A redelivered Pub/Sub ``suppress`` MUST NOT advance
        ``suppressed_at`` — the field is the canonical "when did the
        suppression begin" and we always want the first one to win.
        """
        tid = _fresh_tenant()
        first = (
            await client.post(
                f"{PREFIX}/tenant-suppression",
                json={"tenant_id": tid, "action": "suppress"},
            )
        ).json()
        second = (
            await client.post(
                f"{PREFIX}/tenant-suppression",
                json={"tenant_id": tid, "action": "suppress"},
            )
        ).json()
        assert first["suppressed_at"] == second["suppressed_at"]
        # ``updated_at`` (NOT ``suppressed_at``) bumps on every upsert
        # so observability sees that the redelivery DID hit the row.
        assert second["updated_at"] >= first["updated_at"]

    async def test_get_unknown_tenant_returns_false(self, client: AsyncClient) -> None:
        tid = _fresh_tenant()
        got = await client.get(f"{PREFIX}/tenant-suppression/{tid}")
        assert got.status_code == 200
        assert got.json() == {"tenant_id": tid, "is_suppressed": False}

    async def test_rejects_unknown_action(self, client: AsyncClient) -> None:
        resp = await client.post(
            f"{PREFIX}/tenant-suppression",
            json={"tenant_id": "t", "action": "delete"},
        )
        assert resp.status_code == 422

    async def test_rejects_missing_tenant_id(self, client: AsyncClient) -> None:
        resp = await client.post(f"{PREFIX}/tenant-suppression", json={"action": "suppress"})
        assert resp.status_code == 422
        empty = await client.post(
            f"{PREFIX}/tenant-suppression",
            json={"tenant_id": "", "action": "suppress"},
        )
        assert empty.status_code == 422

    async def test_rejects_malformed_body(self, client: AsyncClient) -> None:
        resp = await client.post(
            f"{PREFIX}/tenant-suppression",
            content="not json",
            headers={"content-type": "application/json"},
        )
        assert resp.status_code == 422

    async def test_rejects_non_object_json_body(self, client: AsyncClient) -> None:
        """Valid JSON whose top-level value is an array / string / number
        must surface as a 422, not a 500. Before the round-1 fix, calling
        ``.get`` on the decoded list raised ``AttributeError`` and the
        request 500'd. Bot review round 1 on PR #244."""
        for body in ("[1,2,3]", '"just a string"', "42"):
            resp = await client.post(
                f"{PREFIX}/tenant-suppression",
                content=body,
                headers={"content-type": "application/json"},
            )
            assert resp.status_code == 422, f"body={body!r}: {resp.text}"
            assert "JSON object" in resp.json()["detail"]

    async def test_rejects_invalid_utf8_body(self, client: AsyncClient) -> None:
        """Invalid UTF-8 bytes raise ``UnicodeDecodeError`` — distinct
        from ``JSONDecodeError`` — so a single-class catch missed it and
        the request 500'd. Bot review round 1 on PR #244 (🟢 Low)."""
        resp = await client.post(
            f"{PREFIX}/tenant-suppression",
            content=b"\xff\xfe not utf-8",
            headers={"content-type": "application/json"},
        )
        assert resp.status_code == 422

    async def test_rejects_non_string_updated_by(self, client: AsyncClient) -> None:
        resp = await client.post(
            f"{PREFIX}/tenant-suppression",
            json={
                "tenant_id": _fresh_tenant(),
                "action": "suppress",
                "updated_by": 42,
            },
        )
        assert resp.status_code == 422
