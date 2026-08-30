"""A63 — conditional subject write-back route.

``POST /memories/{id}/subject-entity`` sets ``subject_entity_id`` ONLY
when the column is NULL: the write-time triple path's value (CAURA-123)
always wins over the async extraction-derived one, and concurrent
deliveries can't clobber each other. Integration tests against real PG
because the whole contract is one conditional UPDATE.
"""

from __future__ import annotations

import hashlib
import struct
import uuid

from httpx import AsyncClient

from common.constants import VECTOR_DIM

PREFIX = "/api/v1/storage"


def _fake_embedding(seed: str, dim: int = VECTOR_DIM) -> list[float]:
    """Deterministic unit-length embedding — mirrors test_integration's helper."""
    h = hashlib.sha256(seed.encode()).digest()
    raw = h * (dim // len(h) + 1)
    values = [struct.unpack_from("b", raw, i)[0] / 128.0 for i in range(dim)]
    norm = sum(v * v for v in values) ** 0.5
    return [v / norm for v in values]


async def _create_memory(client: AsyncClient, tenant_id: str, fleet_id: str) -> str:
    content = f"subject writeback test memory {tenant_id} {uuid.uuid4().hex[:8]}"
    resp = await client.post(
        f"{PREFIX}/memories",
        json={
            "tenant_id": tenant_id,
            "fleet_id": fleet_id,
            "agent_id": "a63-test-agent",
            "memory_type": "fact",
            "content": content,
            "embedding": _fake_embedding(content),
            "content_hash": hashlib.sha256(content.encode()).hexdigest(),
            "weight": 0.7,
            "visibility": "scope_team",
        },
    )
    assert resp.status_code == 200, resp.text
    return resp.json()["id"]


async def _create_entity(client: AsyncClient, tenant_id: str, fleet_id: str, name: str) -> str:
    resp = await client.post(
        f"{PREFIX}/entities",
        json={
            "tenant_id": tenant_id,
            "fleet_id": fleet_id,
            "entity_type": "person",
            "canonical_name": name,
            "attributes": {},
        },
    )
    assert resp.status_code == 200, resp.text
    return resp.json()["id"]


class TestSubjectWriteback:
    async def test_sets_when_null_and_visible_on_read(
        self, client: AsyncClient, tenant_id: str, fleet_id: str
    ) -> None:
        memory_id = await _create_memory(client, tenant_id, fleet_id)
        entity_id = await _create_entity(client, tenant_id, fleet_id, "Priya Writeback")

        resp = await client.post(
            f"{PREFIX}/memories/{memory_id}/subject-entity",
            json={"tenant_id": tenant_id, "subject_entity_id": entity_id},
        )
        assert resp.status_code == 200, resp.text
        assert resp.json() == {"updated": True}

        row = (await client.get(f"{PREFIX}/memories/{memory_id}", params={"tenant_id": tenant_id})).json()
        assert row["subject_entity_id"] == entity_id

    async def test_never_clobbers_an_existing_subject(
        self, client: AsyncClient, tenant_id: str, fleet_id: str
    ) -> None:
        memory_id = await _create_memory(client, tenant_id, fleet_id)
        first = await _create_entity(client, tenant_id, fleet_id, "First Subject")
        second = await _create_entity(client, tenant_id, fleet_id, "Second Subject")

        r1 = await client.post(
            f"{PREFIX}/memories/{memory_id}/subject-entity",
            json={"tenant_id": tenant_id, "subject_entity_id": first},
        )
        assert r1.json() == {"updated": True}

        r2 = await client.post(
            f"{PREFIX}/memories/{memory_id}/subject-entity",
            json={"tenant_id": tenant_id, "subject_entity_id": second},
        )
        assert r2.json() == {"updated": False}

        row = (await client.get(f"{PREFIX}/memories/{memory_id}", params={"tenant_id": tenant_id})).json()
        assert row["subject_entity_id"] == first

    async def test_foreign_tenant_matches_no_row(
        self, client: AsyncClient, tenant_id: str, fleet_id: str
    ) -> None:
        memory_id = await _create_memory(client, tenant_id, fleet_id)
        entity_id = await _create_entity(client, tenant_id, fleet_id, "Cross Tenant Subject")

        resp = await client.post(
            f"{PREFIX}/memories/{memory_id}/subject-entity",
            json={"tenant_id": f"other-{tenant_id}", "subject_entity_id": entity_id},
        )
        assert resp.status_code == 200
        assert resp.json() == {"updated": False}

        row = (await client.get(f"{PREFIX}/memories/{memory_id}", params={"tenant_id": tenant_id})).json()
        assert row["subject_entity_id"] is None
