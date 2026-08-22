"""``PATCH /memories/{id}`` must never leave the previous content's vector.

``get_embedding`` returns ``None`` on failure (exhausted retries, or a degraded
/ misconfigured provider) rather than raising — its documented contract is that
callers "persist rows with ``embedding=NULL`` and let the async-embed worker
backfill".

``update_memory`` used to guard the write on ``if new_embedding is not None``,
which omitted the key entirely and so left the OLD content's vector on a row
whose content had just changed. That row is wrong twice over: recall ranks it
against text it no longer holds, and because the column is non-NULL neither the
async-embed worker nor the nightly NULL-embedding sweep can ever find it.
"""

import pytest

from core_api.clients.storage_client import get_storage_client
from tests.conftest import get_test_auth, uid as _uid

pytestmark = pytest.mark.asyncio


async def _missing(tenant_id: str, fleet_id: str) -> int:
    """Rows the NULL-embedding sweep would see for this fleet."""
    coverage = await get_storage_client().get_embedding_coverage(tenant_id, fleet_id)
    return int(coverage.get("missing_embeddings", 0))


async def test_content_change_with_failed_embedding_nulls_the_vector(
    client, monkeypatch
):
    tenant_id, headers = get_test_auth()
    tag = _uid()
    fleet_id, agent_id = f"upd-fleet-{tag}", f"upd-agent-{tag}"

    resp = await client.post(
        "/api/v1/memories",
        json={
            "tenant_id": tenant_id,
            "agent_id": agent_id,
            "fleet_id": fleet_id,
            "memory_type": "fact",
            "visibility": "scope_team",
            "content": f"original content {tag}",
        },
        headers=headers,
    )
    assert resp.status_code == 201, resp.text
    memory_id = resp.json()["id"]

    # Baseline: the write embedded normally, so the sweep sees nothing.
    assert await _missing(tenant_id, fleet_id) == 0

    # Now make the re-embed fail the way a provider hiccup does.
    async def _failed_embed(*args, **kwargs):
        return None

    monkeypatch.setattr(
        "core_api.services.memory_service.get_embedding",
        _failed_embed,
    )

    resp = await client.patch(
        f"/api/v1/memories/{memory_id}?tenant_id={tenant_id}",
        json={"content": f"REPLACED content {tag}"},
        headers=headers,
    )
    assert resp.status_code == 200, resp.text

    # The content change must have landed...
    got = await get_storage_client().get_memory_for_tenant(tenant_id, memory_id)
    assert got is not None
    assert got["content"] == f"REPLACED content {tag}"

    # ...and the row must now be NULL-embedded, i.e. visible to the sweep.
    # Before the fix this was 0: the patch omitted ``embedding`` entirely, so
    # the row kept the ORIGINAL content's vector and no repair path could see it.
    assert await _missing(tenant_id, fleet_id) == 1


async def test_content_change_with_working_embedding_still_embeds(client):
    """The success path is unchanged — a good embedding is still written."""
    tenant_id, headers = get_test_auth()
    tag = _uid()
    fleet_id, agent_id = f"upd-ok-fleet-{tag}", f"upd-ok-agent-{tag}"

    resp = await client.post(
        "/api/v1/memories",
        json={
            "tenant_id": tenant_id,
            "agent_id": agent_id,
            "fleet_id": fleet_id,
            "memory_type": "fact",
            "visibility": "scope_team",
            "content": f"original content {tag}",
        },
        headers=headers,
    )
    assert resp.status_code == 201, resp.text
    memory_id = resp.json()["id"]

    resp = await client.patch(
        f"/api/v1/memories/{memory_id}?tenant_id={tenant_id}",
        json={"content": f"rewritten content {tag}"},
        headers=headers,
    )
    assert resp.status_code == 200, resp.text

    assert await _missing(tenant_id, fleet_id) == 0
