"""E2E memory CRUD tests through HTTP API."""

from tests.conftest import get_test_auth, uid as _uid


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _write_memory(
    client,
    tenant_id: str,
    headers: dict,
    content: str,
    agent_id: str | None = None,
    fleet_id: str | None = None,
    memory_type: str = "fact",
) -> dict:
    """Helper to write a single memory and return the response JSON."""
    tag = _uid()
    if agent_id is None:
        agent_id = f"test-agent-{tag}"
    if fleet_id is None:
        fleet_id = f"test-fleet-{tag}"
    content_with_uid = f"{content} [{tag}]"
    resp = await client.post(
        "/api/v1/memories",
        json={
            "tenant_id": tenant_id,
            "content": content_with_uid,
            "agent_id": agent_id,
            "fleet_id": fleet_id,
            "memory_type": memory_type,
        },
        headers=headers,
    )
    assert resp.status_code == 201, f"Write failed: {resp.text}"
    return resp.json()


# ---------------------------------------------------------------------------
# Write & Read
# ---------------------------------------------------------------------------


async def test_write_memory(client):
    """POST /api/memories with API key → 201, returns memory with id."""
    tenant_id, headers = get_test_auth()

    data = await _write_memory(client, tenant_id, headers, "User prefers dark mode")
    assert "id" in data
    assert data["tenant_id"] == tenant_id
    assert data["content"].startswith("User prefers dark mode")
    assert data["memory_type"] == "fact"


async def test_list_memories(client):
    """Write 3 memories, GET /api/memories?tenant_id=X → list includes them."""
    tenant_id, headers = get_test_auth()

    for i in range(3):
        await _write_memory(client, tenant_id, headers, f"Memory number {i}")

    resp = await client.get(
        f"/api/v1/memories?tenant_id={tenant_id}",
        headers=headers,
    )
    assert resp.status_code == 200
    data = resp.json()
    memories = data.get("items", data) if isinstance(data, dict) else data
    assert len(memories) >= 3


async def test_list_memories_with_type_filter(client):
    """Write fact + episode, filter by type=fact → only facts returned."""
    tenant_id, headers = get_test_auth()
    tag = _uid()

    await _write_memory(
        client,
        tenant_id,
        headers,
        f"A known fact [{tag}]",
        agent_id=f"filter-agent-{tag}",
        fleet_id=f"filter-fleet-{tag}",
        memory_type="fact",
    )
    await _write_memory(
        client,
        tenant_id,
        headers,
        f"Something happened [{tag}]",
        agent_id=f"filter-agent-{tag}",
        fleet_id=f"filter-fleet-{tag}",
        memory_type="episode",
    )

    resp = await client.get(
        f"/api/v1/memories?tenant_id={tenant_id}&memory_type=fact",
        headers=headers,
    )
    assert resp.status_code == 200
    data = resp.json()
    memories = (
        data.get("items", data) if isinstance(data, dict) and "items" in data else data
    )
    assert all(m["memory_type"] == "fact" for m in memories)
    assert len(memories) >= 1


# ---------------------------------------------------------------------------
# Search
# ---------------------------------------------------------------------------


async def test_search_memories(client):
    """POST /api/search returns 200 with results array (semantic matching not guaranteed in test)."""
    tenant_id, headers = get_test_auth()

    await _write_memory(
        client, tenant_id, headers, "dark mode preferences for the user interface"
    )

    resp = await client.post(
        "/api/v1/search",
        json={
            "tenant_id": tenant_id,
            "query": "user settings",
        },
        headers=headers,
    )
    assert resp.status_code == 200
    data = resp.json()
    assert isinstance(data, dict) and "items" in data
    assert isinstance(data["items"], list)


# ---------------------------------------------------------------------------
# Delete
# ---------------------------------------------------------------------------


async def test_delete_memory(client):
    """Write then DELETE /api/memories/{id}?tenant_id=X → 204."""
    tenant_id, headers = get_test_auth()

    mem = await _write_memory(client, tenant_id, headers, "Temporary memory")
    memory_id = mem["id"]

    resp = await client.delete(
        f"/api/v1/memories/{memory_id}?tenant_id={tenant_id}",
        headers=headers,
    )
    assert resp.status_code == 204


async def test_patch_metadata_merges_by_default(client):
    """PATCH /memories/{id} with ``metadata`` deep-merges by default —
    pre-existing keys not in the patch are preserved.

    Closes the load-test review's ``patch-metadata-replace`` MEDIUM
    finding: pre-fix this overwrote the column wholesale, dropping
    sibling keys (e.g. ``ground_truth`` set at write time vanished
    when a status-only PATCH later set ``metadata={"new": ...}``)."""
    tenant_id, headers = get_test_auth()

    mem = await _write_memory(client, tenant_id, headers, "Memory with metadata seed")
    memory_id = mem["id"]

    # Seed two metadata keys via a first PATCH (use replace so the
    # initial state is fully under our control).
    seed_resp = await client.patch(
        f"/api/v1/memories/{memory_id}?tenant_id={tenant_id}",
        json={
            "metadata": {"ground_truth": "v1", "label": "alpha"},
            "metadata_mode": "replace",
        },
        headers=headers,
    )
    assert seed_resp.status_code == 200, seed_resp.text

    # Second PATCH with only ``label`` (default merge mode) — must
    # preserve ``ground_truth`` from the seed.
    resp = await client.patch(
        f"/api/v1/memories/{memory_id}?tenant_id={tenant_id}",
        json={"metadata": {"label": "beta"}},
        headers=headers,
    )
    assert resp.status_code == 200, resp.text

    # Read back via GET — both keys should be present, label updated.
    read = await client.get(
        f"/api/v1/memories/{memory_id}?tenant_id={tenant_id}",
        headers=headers,
    )
    assert read.status_code == 200
    md = read.json().get("metadata") or {}
    assert md.get("ground_truth") == "v1", "merge must preserve sibling keys"
    assert md.get("label") == "beta", "patched key takes new value"


async def test_patch_metadata_empty_dict_in_merge_mode_is_noop(client):
    """``{"metadata": {}}`` in default merge mode is a no-op —
    storage's ``memory_update`` skips the JSONB merge for an empty
    patch, and core-api MUST NOT emit a phantom "metadata changed"
    audit entry for the no-op."""
    tenant_id, headers = get_test_auth()

    mem = await _write_memory(client, tenant_id, headers, "Memory empty noop test")
    memory_id = mem["id"]

    # Seed two keys so we can verify they survive the no-op.
    await client.patch(
        f"/api/v1/memories/{memory_id}?tenant_id={tenant_id}",
        json={
            "metadata": {"keep_a": "1", "keep_b": "2"},
            "metadata_mode": "replace",
        },
        headers=headers,
    )

    # Empty-dict merge → no-op (200, seeded keys survive).
    resp = await client.patch(
        f"/api/v1/memories/{memory_id}?tenant_id={tenant_id}",
        json={"metadata": {}},
        headers=headers,
    )
    assert resp.status_code == 200, resp.text

    read = await client.get(
        f"/api/v1/memories/{memory_id}?tenant_id={tenant_id}",
        headers=headers,
    )
    md = read.json().get("metadata") or {}
    assert md.get("keep_a") == "1"
    assert md.get("keep_b") == "2"


async def test_patch_metadata_null_in_merge_mode_returns_400(client):
    """``{"metadata": null}`` in default merge mode returns 400.

    Pre-PR the payload cleared the column (replace semantic);
    silently changing it to a no-op would be a data-integrity
    regression for callers relying on null-as-clear. Force the
    intent to be explicit via ``metadata_mode="replace"``."""
    tenant_id, headers = get_test_auth()

    mem = await _write_memory(client, tenant_id, headers, "Memory null-merge test")
    memory_id = mem["id"]

    resp = await client.patch(
        f"/api/v1/memories/{memory_id}?tenant_id={tenant_id}",
        json={"metadata": None},
        headers=headers,
    )
    assert resp.status_code == 400, resp.text
    detail = str(resp.json().get("detail", ""))
    assert "replace" in detail.lower(), (
        "error message should point the caller at metadata_mode=replace"
    )
    # Surfaces the breaking-change to operators / SDKs without
    # requiring them to crawl 400 logs to find lingering callers.
    assert resp.headers.get("deprecation") == "true", (
        "400 must carry Deprecation header so SDKs can flag the legacy "
        "null-clears-column payload shape"
    )


async def test_patch_metadata_null_with_replace_mode_clears_column(client):
    """``{"metadata": null, "metadata_mode": "replace"}`` is the
    explicit clear-the-column path — preserves the pre-PR semantic
    for any caller that needs it, and is what the 400 above redirects
    them to."""
    tenant_id, headers = get_test_auth()

    mem = await _write_memory(client, tenant_id, headers, "Memory clear test")
    memory_id = mem["id"]

    # Seed metadata.
    await client.patch(
        f"/api/v1/memories/{memory_id}?tenant_id={tenant_id}",
        json={
            "metadata": {"to_clear": "value"},
            "metadata_mode": "replace",
        },
        headers=headers,
    )

    # Null + replace clears.
    resp = await client.patch(
        f"/api/v1/memories/{memory_id}?tenant_id={tenant_id}",
        json={"metadata": None, "metadata_mode": "replace"},
        headers=headers,
    )
    assert resp.status_code == 200, resp.text

    read = await client.get(
        f"/api/v1/memories/{memory_id}?tenant_id={tenant_id}",
        headers=headers,
    )
    # NULL column reads as None / empty — depending on serializer.
    md = read.json().get("metadata")
    assert md in (None, {}), f"expected metadata cleared, got {md!r}"


async def test_patch_metadata_mode_without_metadata_returns_422(client):
    """``{"metadata_mode": "replace"}`` without a corresponding
    ``metadata`` field is rejected at the schema boundary with 422.

    Pre-fix this slipped through the "no fields to update" guard
    (``metadata_mode`` is in ``model_fields_set``) and reached the
    service layer, where it produced an empty patch + empty
    ``changes`` — a silent 200 no-op that misled the caller into
    thinking their intent landed. The schema-level
    ``model_validator`` now fails fast so clients see the bug in
    their own code, not in production logs."""
    tenant_id, headers = get_test_auth()

    mem = await _write_memory(client, tenant_id, headers, "Memory mode-only test")
    memory_id = mem["id"]

    resp = await client.patch(
        f"/api/v1/memories/{memory_id}?tenant_id={tenant_id}",
        json={"metadata_mode": "replace"},
        headers=headers,
    )
    assert resp.status_code == 422, resp.text
    # Detail mentions the schema constraint so the caller can grep.
    body = resp.json()
    detail_text = str(body.get("detail", ""))
    assert "metadata" in detail_text.lower()


async def test_patch_metadata_replace_mode_overwrites(client):
    """``metadata_mode=replace`` opts back into the pre-2026-04-26
    wholesale-replace behaviour for callers that want it."""
    tenant_id, headers = get_test_auth()

    mem = await _write_memory(client, tenant_id, headers, "Memory replace test")
    memory_id = mem["id"]

    # Seed first.
    await client.patch(
        f"/api/v1/memories/{memory_id}?tenant_id={tenant_id}",
        json={
            "metadata": {"keep_me": "original", "drop_me": "original"},
            "metadata_mode": "replace",
        },
        headers=headers,
    )

    # Replace → drops both seed keys, leaves only the new key.
    resp = await client.patch(
        f"/api/v1/memories/{memory_id}?tenant_id={tenant_id}",
        json={
            "metadata": {"only_key": "new_value"},
            "metadata_mode": "replace",
        },
        headers=headers,
    )
    assert resp.status_code == 200

    read = await client.get(
        f"/api/v1/memories/{memory_id}?tenant_id={tenant_id}",
        headers=headers,
    )
    md = read.json().get("metadata") or {}
    assert "keep_me" not in md, "replace mode drops sibling keys"
    assert "drop_me" not in md
    assert md.get("only_key") == "new_value"


async def test_empty_dict_metadata_round_trips_as_empty_dict(client):
    """An intentionally-empty ``{}`` metadata column surfaces as
    ``{}`` (not ``null``) through every dict-driven response builder.

    Pre-fix the helpers used ``mem.get("metadata_") or
    mem.get("metadata")`` — ``{}`` is falsy so ``or`` fell through to
    the legacy ``"metadata"`` key, masking an intentional empty dict
    as ``None``. PR #187 already cured the audit-log path; this
    pins down ``_dict_to_memory_out`` (PATCH response body) and
    ``_memory_to_out`` (LIST + search). GET-by-id bypasses both
    helpers (the route hand-rolls the response dict), so it isn't
    asserted here."""
    tenant_id, headers = get_test_auth()

    mem = await _write_memory(client, tenant_id, headers, "Empty-dict metadata test")
    memory_id = mem["id"]

    # Force the column to ``{}`` via explicit replace. PATCH's
    # response body comes from ``_dict_to_memory_out`` — first
    # assertion site.
    patch_resp = await client.patch(
        f"/api/v1/memories/{memory_id}?tenant_id={tenant_id}",
        json={"metadata": {}, "metadata_mode": "replace"},
        headers=headers,
    )
    assert patch_resp.status_code == 200, patch_resp.text
    assert patch_resp.json().get("metadata") == {}, (
        "PATCH response (_dict_to_memory_out) must surface the stored {} "
        f"as {{}}, got {patch_resp.json().get('metadata')!r}"
    )

    # LIST goes through ``_memory_to_out`` — second assertion site.
    list_resp = await client.get(
        f"/api/v1/memories?tenant_id={tenant_id}",
        headers=headers,
    )
    assert list_resp.status_code == 200
    items = list_resp.json().get("items") or list_resp.json()
    found = next((m for m in items if m["id"] == memory_id), None)
    assert found is not None, "freshly-written memory must appear in LIST"
    assert found.get("metadata") == {}, (
        "LIST response (_memory_to_out) must surface the stored {} "
        f"as {{}}, got {found.get('metadata')!r}"
    )


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------


async def test_api_key_auth(client):
    """Use admin API key in X-API-Key header → works."""
    tenant_id, headers = get_test_auth()

    resp = await client.get(
        f"/api/v1/memories?tenant_id={tenant_id}",
        headers=headers,
    )
    assert resp.status_code == 200


# ---------------------------------------------------------------------------
# Stats
# ---------------------------------------------------------------------------


async def test_memory_stats(client):
    """Write memories, GET /api/memories/stats?tenant_id=X → total, by_type, by_agent."""
    tenant_id, headers = get_test_auth()
    tag = _uid()
    agent = f"stats-agent-{tag}"
    fleet = f"stats-fleet-{tag}"

    await _write_memory(
        client,
        tenant_id,
        headers,
        "Stat fact one",
        agent_id=agent,
        fleet_id=fleet,
        memory_type="fact",
    )
    await _write_memory(
        client,
        tenant_id,
        headers,
        "Stat episode one",
        agent_id=agent,
        fleet_id=fleet,
        memory_type="episode",
    )
    await _write_memory(
        client,
        tenant_id,
        headers,
        "Stat fact two",
        agent_id=agent,
        fleet_id=fleet,
        memory_type="fact",
    )

    resp = await client.get(
        f"/api/v1/memories/stats?tenant_id={tenant_id}",
        headers=headers,
    )
    assert resp.status_code == 200
    data = resp.json()
    assert "total" in data
    assert data["total"] >= 3
    assert "by_type" in data
    assert "by_agent" in data
    assert data["by_type"].get("fact", 0) >= 2
    assert data["by_type"].get("episode", 0) >= 1


async def test_memory_stats_excludes_scope_agent_when_caller_unidentified(client):
    # Regression: ``GET /api/v1/memories/stats`` previously had no
    # visibility predicate, so it counted ``scope_agent`` rows that
    # ``GET /api/v1/memories`` (which mirrors
    # ``memory_repository.list_by_filters``) excludes when no
    # ``agent_id`` is in the URL. Result: Prism showed inflated counts
    # alongside a list view that hid the same rows.
    tenant_id, headers = get_test_auth()
    tag = _uid()
    fleet = f"stats-vis-fleet-{tag}"

    async def _post(agent: str, visibility: str, content: str) -> None:
        resp = await client.post(
            "/api/v1/memories",
            json={
                "tenant_id": tenant_id,
                "content": f"{content} [{tag}]",
                "agent_id": agent,
                "fleet_id": fleet,
                "memory_type": "fact",
                "visibility": visibility,
            },
            headers=headers,
        )
        assert resp.status_code == 201, f"Write failed: {resp.text}"

    await _post(f"alice-{tag}", "scope_agent", "alice private")
    await _post(f"bob-{tag}", "scope_agent", "bob private")
    await _post(f"shared-{tag}", "scope_org", "team-wide")

    list_resp = await client.get(
        f"/api/v1/memories?tenant_id={tenant_id}&fleet_id={fleet}",
        headers=headers,
    )
    assert list_resp.status_code == 200
    items = list_resp.json().get("items", [])

    stats_resp = await client.get(
        f"/api/v1/memories/stats?tenant_id={tenant_id}&fleet_id={fleet}",
        headers=headers,
    )
    assert stats_resp.status_code == 200
    stats = stats_resp.json()

    assert stats["total"] == len(items), (
        f"stats total ({stats['total']}) must match list length ({len(items)}); "
        f"mismatch indicates /memories/stats is missing the visibility predicate "
        f"that /memories applies via list_by_filters."
    )
    # Anchor the expected behavior: only the scope_org row is visible to
    # an unidentified caller. If this assertion fires, the visibility
    # contract itself has changed (not just stats vs list parity).
    assert stats["total"] == 1, (
        f"expected 1 visible memory (scope_org only) when no agent_id is passed, "
        f"got {stats['total']}"
    )


async def test_memory_stats_with_agent_id_includes_own_scope_agent(client):
    # Companion to the above: when ``agent_id`` IS passed, the caller can
    # see their own ``scope_agent`` rows (mirrors list_by_filters lines
    # 122-135). Stats must agree with the list under the same filter.
    tenant_id, headers = get_test_auth()
    tag = _uid()
    fleet = f"stats-vis-own-fleet-{tag}"
    alice = f"alice-{tag}"
    bob = f"bob-{tag}"

    async def _post(agent: str, visibility: str, content: str) -> None:
        resp = await client.post(
            "/api/v1/memories",
            json={
                "tenant_id": tenant_id,
                "content": f"{content} [{tag}]",
                "agent_id": agent,
                "fleet_id": fleet,
                "memory_type": "fact",
                "visibility": visibility,
            },
            headers=headers,
        )
        assert resp.status_code == 201, f"Write failed: {resp.text}"

    await _post(alice, "scope_agent", "alice private")
    await _post(alice, "scope_org", "alice public")
    await _post(bob, "scope_agent", "bob private")

    list_resp = await client.get(
        f"/api/v1/memories?tenant_id={tenant_id}&fleet_id={fleet}&agent_id={alice}",
        headers=headers,
    )
    assert list_resp.status_code == 200
    items = list_resp.json().get("items", [])

    stats_resp = await client.get(
        f"/api/v1/memories/stats?tenant_id={tenant_id}&fleet_id={fleet}&agent_id={alice}",
        headers=headers,
    )
    assert stats_resp.status_code == 200
    stats = stats_resp.json()

    assert stats["total"] == len(items)
    # Both endpoints scope to "memories alice authored" (agent_id doubles
    # as author + visibility identity), so bob's scope_agent row is
    # excluded and alice sees both her own.
    assert stats["total"] == 2
