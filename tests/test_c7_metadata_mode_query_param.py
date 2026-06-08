"""C7 — ``metadata_mode`` accepted as query-string parameter on PATCH.

Pre-C7: ``PATCH /api/v1/memories/{id}`` read ``metadata_mode`` only
from the request body. A query-param of the same name was silently
dropped by FastAPI as an unknown query arg and the PATCH fell back to
the default ``merge`` behaviour — confusing for callers who can't
easily wedge a body field in but can append to the URL (CLI tooling,
audit-replay scripts, etc.).

Post-C7: the endpoint accepts ``metadata_mode`` as a query parameter
too. Body wins on conflict (the body is the canonical write payload).
The query mode goes through the same regex validation as the body
field (``merge|replace``); anything else 422s at FastAPI's parse
layer.

These tests pin the new query-param surface plus the corner cases
where body-vs-query precedence matters and where the validator
mirroring of the body-side "mode without metadata patch" rule applies.
"""

import pytest

from tests.conftest import get_test_auth, uid as _uid


pytestmark = [pytest.mark.unit, pytest.mark.asyncio]


# ---------------------------------------------------------------------------
# Local helper (mirrors test_api_memories._write_memory shape but kept
# inline so this file is self-contained — the C7 contract is narrow and
# we don't want to take a churn dep on the sibling test module).
# ---------------------------------------------------------------------------


async def _write_memory(client, tenant_id: str, headers: dict, content: str) -> dict:
    tag = _uid()
    resp = await client.post(
        "/api/v1/memories",
        json={
            "tenant_id": tenant_id,
            "content": f"{content} [{tag}]",
            "agent_id": f"c7-agent-{tag}",
            "fleet_id": f"c7-fleet-{tag}",
            "memory_type": "fact",
        },
        headers=headers,
    )
    assert resp.status_code == 201, f"seed write failed: {resp.text}"
    return resp.json()


async def _seed_metadata(
    client, tenant_id: str, headers: dict, memory_id: str, md: dict
) -> None:
    """Seed initial metadata via an explicit replace PATCH so the
    starting state is fully under test control regardless of any
    enrichment-driven keys the write path might inject."""
    resp = await client.patch(
        f"/api/v1/memories/{memory_id}?tenant_id={tenant_id}",
        json={"metadata": md, "metadata_mode": "replace"},
        headers=headers,
    )
    assert resp.status_code == 200, f"seed PATCH failed: {resp.text}"


async def _read_metadata(client, tenant_id: str, headers: dict, memory_id: str) -> dict:
    resp = await client.get(
        f"/api/v1/memories/{memory_id}?tenant_id={tenant_id}",
        headers=headers,
    )
    assert resp.status_code == 200, resp.text
    return resp.json().get("metadata") or {}


# ---------------------------------------------------------------------------
# Truth-table tests
# ---------------------------------------------------------------------------


async def test_query_only_replace_applies(client):
    """Body has ``metadata`` only; query has ``?metadata_mode=replace``
    → REPLACE semantic applied (pre-C7 this would have merged)."""
    tenant_id, headers = get_test_auth()
    mem = await _write_memory(client, tenant_id, headers, "C7 query-only replace")
    memory_id = mem["id"]

    await _seed_metadata(client, tenant_id, headers, memory_id, {"a": 1, "b": 2})

    resp = await client.patch(
        f"/api/v1/memories/{memory_id}?tenant_id={tenant_id}&metadata_mode=replace",
        json={"metadata": {"c": 3}},
        headers=headers,
    )
    assert resp.status_code == 200, resp.text

    md = await _read_metadata(client, tenant_id, headers, memory_id)
    assert "a" not in md, "query-side replace must drop pre-existing keys"
    assert "b" not in md
    assert md.get("c") == 3


async def test_body_only_replace_still_works(client):
    """Regression: body-side ``metadata_mode=replace`` (the pre-C7
    surface) keeps working unchanged."""
    tenant_id, headers = get_test_auth()
    mem = await _write_memory(client, tenant_id, headers, "C7 body-only replace")
    memory_id = mem["id"]

    await _seed_metadata(client, tenant_id, headers, memory_id, {"a": 1, "b": 2})

    resp = await client.patch(
        f"/api/v1/memories/{memory_id}?tenant_id={tenant_id}",
        json={"metadata": {"c": 3}, "metadata_mode": "replace"},
        headers=headers,
    )
    assert resp.status_code == 200, resp.text

    md = await _read_metadata(client, tenant_id, headers, memory_id)
    assert "a" not in md and "b" not in md
    assert md.get("c") == 3


async def test_no_mode_anywhere_defaults_to_merge(client):
    """No ``metadata_mode`` in body or query → deep merge (the
    pre-C7 default), sibling keys survive."""
    tenant_id, headers = get_test_auth()
    mem = await _write_memory(client, tenant_id, headers, "C7 default merge")
    memory_id = mem["id"]

    await _seed_metadata(client, tenant_id, headers, memory_id, {"a": 1, "b": 2})

    resp = await client.patch(
        f"/api/v1/memories/{memory_id}?tenant_id={tenant_id}",
        json={"metadata": {"c": 3}},
        headers=headers,
    )
    assert resp.status_code == 200, resp.text

    md = await _read_metadata(client, tenant_id, headers, memory_id)
    assert md.get("a") == 1, "merge must preserve sibling keys"
    assert md.get("b") == 2
    assert md.get("c") == 3


async def test_body_wins_on_conflict(client):
    """Body says ``merge``, query says ``replace`` → body wins, merge
    semantic applies. The body is the canonical write payload; query
    params are a convenience shim and must NOT override an explicit
    body value (otherwise audit-log replay of the body alone would
    produce a different outcome than the original call)."""
    tenant_id, headers = get_test_auth()
    mem = await _write_memory(client, tenant_id, headers, "C7 body wins")
    memory_id = mem["id"]

    await _seed_metadata(client, tenant_id, headers, memory_id, {"a": 1, "b": 2})

    resp = await client.patch(
        f"/api/v1/memories/{memory_id}?tenant_id={tenant_id}&metadata_mode=replace",
        json={"metadata": {"c": 3}, "metadata_mode": "merge"},
        headers=headers,
    )
    assert resp.status_code == 200, resp.text

    md = await _read_metadata(client, tenant_id, headers, memory_id)
    # body's "merge" beats query's "replace" — siblings survive.
    assert md.get("a") == 1, "body's merge must win over query's replace"
    assert md.get("b") == 2
    assert md.get("c") == 3


async def test_query_mode_without_metadata_patch_returns_422(client):
    """Query-side ``metadata_mode`` without a corresponding
    ``metadata`` field in the body is a no-op intent — the body-side
    validator already rejects ``{"metadata_mode": "replace"}`` alone
    with 422; the query-side surface must mirror that contract."""
    tenant_id, headers = get_test_auth()
    mem = await _write_memory(client, tenant_id, headers, "C7 query mode-only")
    memory_id = mem["id"]

    resp = await client.patch(
        f"/api/v1/memories/{memory_id}?tenant_id={tenant_id}&metadata_mode=replace",
        json={"title": "renamed but no metadata patch"},
        headers=headers,
    )
    assert resp.status_code == 422, resp.text
    detail_text = str(resp.json().get("detail", "")).lower()
    # Error must namestamp both terms so the caller can grep their own
    # client code without crawling the route source.
    assert "metadata" in detail_text
    assert "metadata_mode" in detail_text


async def test_bogus_query_mode_returns_422_at_parse_layer(client):
    """``?metadata_mode=BOGUS`` is rejected by FastAPI's regex
    validation before the route body runs — no service call, no
    storage hit. Mirrors the body-side field's ``Pattern`` constraint."""
    tenant_id, headers = get_test_auth()
    mem = await _write_memory(client, tenant_id, headers, "C7 bogus query mode")
    memory_id = mem["id"]

    resp = await client.patch(
        f"/api/v1/memories/{memory_id}?tenant_id={tenant_id}&metadata_mode=BOGUS",
        json={"metadata": {"c": 3}},
        headers=headers,
    )
    assert resp.status_code == 422, resp.text


async def test_unrelated_patch_still_works(client):
    """No metadata patch anywhere, no ``metadata_mode`` anywhere → an
    unrelated field PATCH (title) is unaffected by the C7 plumbing
    and still 200s."""
    tenant_id, headers = get_test_auth()
    mem = await _write_memory(client, tenant_id, headers, "C7 unrelated patch")
    memory_id = mem["id"]

    new_title = f"renamed-{_uid()}"
    resp = await client.patch(
        f"/api/v1/memories/{memory_id}?tenant_id={tenant_id}",
        json={"title": new_title},
        headers=headers,
    )
    assert resp.status_code == 200, resp.text

    # Verify the title actually landed (defends against a silent no-op
    # in the same vein as the C7 422 we just pinned).
    read = await client.get(
        f"/api/v1/memories/{memory_id}?tenant_id={tenant_id}",
        headers=headers,
    )
    assert read.status_code == 200
    assert read.json().get("title") == new_title


async def test_body_and_query_both_replace_is_idempotent(client):
    """Body has ``{"metadata": ..., "metadata_mode": "replace"}`` AND
    URL has ``?metadata_mode=replace`` — the query is redundant but
    must NOT double-apply, error, or alter the body's outcome. Net
    effect = single replace."""
    tenant_id, headers = get_test_auth()
    mem = await _write_memory(client, tenant_id, headers, "C7 both replace")
    memory_id = mem["id"]

    await _seed_metadata(client, tenant_id, headers, memory_id, {"a": 1, "b": 2})

    resp = await client.patch(
        f"/api/v1/memories/{memory_id}?tenant_id={tenant_id}&metadata_mode=replace",
        json={"metadata": {"c": 3}, "metadata_mode": "replace"},
        headers=headers,
    )
    assert resp.status_code == 200, resp.text

    md = await _read_metadata(client, tenant_id, headers, memory_id)
    assert "a" not in md and "b" not in md
    assert md.get("c") == 3
