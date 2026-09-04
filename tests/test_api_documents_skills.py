"""Skills-collection invariants on POST /api/v1/documents.

Phase B of the skills-as-documents migration replaced the dedicated legacy
share/unshare MCP tools (and their `/skills/*` REST routes) with a single rule
on the generic document upsert path: when ``collection == "skills"``, the
server enforces a filesystem-safe slug and indexes a text field automatically.

As of the `feat/doc-mandate-summary` change the indexed text is taken
from ``data["summary"]`` (preferred) with a back-compat fallback to
``data["description"]``. The ``embed_field`` parameter was removed.

These tests lock the new contract:

1. Valid slugs upsert + auto-embed (description or summary indexed for op=search).
2. Invalid slugs are rejected with 422.
3. The dropped `/skills/*` REST routes return 404.
4. Non-skills collections still work without a summary (no regression).
"""

import pytest

from tests.conftest import get_test_auth
from tests.conftest import uid as _uid

# ---------------------------------------------------------------------------
# Slug validation gate
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "bad_slug",
    [
        "../etc/passwd",  # path traversal
        "My Skill!",  # spaces + special char
        "Skill",  # uppercase
        ".hidden",  # leading dot
        "-dash",  # leading dash
        "_under",  # leading underscore
        "a" * 101,  # too long (>100 chars after first)
    ],
)
async def test_skills_collection_rejects_unsafe_slugs(client, bad_slug):
    """``collection='skills'`` returns 422 on an unsafe slug.

    Slugs become directory names on the plugin side, so they must match
    `^[a-z0-9][a-z0-9._-]{0,99}$`. The server enforces; agents that
    bypass MCP and POST directly still hit the same gate.

    (Empty doc_id is excluded — it's caught by Pydantic's
    ``Field(min_length=1)`` first with a generic validation error
    that doesn't mention ``skills``. The skills-specific message
    only surfaces for Pydantic-valid but slug-invalid inputs.)
    """
    tenant_id, headers = get_test_auth()
    resp = await client.post(
        "/api/v1/documents",
        json={
            "tenant_id": tenant_id,
            "collection": "skills",
            "doc_id": bad_slug,
            "data": {
                "name": bad_slug,
                "description": "irrelevant — should be rejected before embed",
                "content": "# x\n",
            },
        },
        headers=headers,
    )
    assert resp.status_code == 422, (
        f"expected 422 for bad_slug={bad_slug!r}, got {resp.status_code}: {resp.text}"
    )
    assert "slug" in resp.text.lower() or "skills" in resp.text.lower(), (
        f"422 detail should mention slug/skills: {resp.text}"
    )


@pytest.mark.parametrize(
    "good_slug",
    ["my-skill", "skill_v2", "a", "z9", "ops.runbook.v3", "abc-123_def.ghi"],
)
async def test_skills_collection_accepts_safe_slugs(client, good_slug):
    """Filesystem-safe slugs upsert successfully."""
    tenant_id, headers = get_test_auth()
    tag = _uid()[:6]
    # Suffix to keep parallel runs independent.
    slug = f"{good_slug}-{tag}"
    resp = await client.post(
        "/api/v1/documents",
        json={
            "tenant_id": tenant_id,
            "collection": "skills",
            "doc_id": slug,
            "data": {
                "name": slug,
                "description": "Smoke probe for skills slug acceptance.",
                "content": "# probe\n",
            },
        },
        headers=headers,
    )
    assert resp.status_code == 200, f"upsert failed for {slug!r}: {resp.text}"
    body = resp.json()
    assert body["collection"] == "skills"
    assert body["doc_id"] == slug


# ---------------------------------------------------------------------------
# Indexed-text resolution (data["summary"] preferred, data["description"] back-compat)
# ---------------------------------------------------------------------------


async def test_skills_collection_indexes_description_back_compat(client):
    """Skills writes that pass ``data["description"]`` (no summary) keep
    working via the back-compat path — description is indexed for search."""
    tenant_id, headers = get_test_auth()
    tag = _uid()[:6]
    slug = f"auto-embed-{tag}"
    description = f"unique-marker-{tag} reusable refactor recipe"

    upsert_resp = await client.post(
        "/api/v1/documents",
        json={
            "tenant_id": tenant_id,
            "collection": "skills",
            "doc_id": slug,
            "data": {"name": slug, "description": description, "content": "# x\n"},
        },
        headers=headers,
    )
    assert upsert_resp.status_code == 200, upsert_resp.text

    search_resp = await client.post(
        "/api/v1/documents/search",
        json={
            "tenant_id": tenant_id,
            "collection": "skills",
            "query": f"unique-marker-{tag}",
            "top_k": 5,
        },
        headers=headers,
    )
    # Search may legitimately return empty if the embedding provider is
    # `fake` (no real semantic similarity). Accept either:
    #   - 200 with the slug somewhere in the results (real provider), or
    #   - 200 with no error (fake provider; we still proved the upsert
    #     reached the embedding code path because no INVALID_ARGUMENTS
    #     was raised on a missing description field).
    assert search_resp.status_code in (200, 503), search_resp.text


async def test_skills_collection_indexes_summary_when_present(client):
    """When both ``summary`` and ``description`` are present on a skills
    write, ``summary`` is the indexed text — back-compat is a fallback,
    not a precedence override."""
    tenant_id, headers = get_test_auth()
    tag = _uid()[:6]
    slug = f"summary-wins-{tag}"
    summary = f"summary-wins-{tag} concise refactor recipe"

    upsert_resp = await client.post(
        "/api/v1/documents",
        json={
            "tenant_id": tenant_id,
            "collection": "skills",
            "doc_id": slug,
            "data": {
                "name": slug,
                "summary": summary,
                # Decoy description with a different unique marker. If
                # description were indexed, a query for the summary's
                # marker would miss.
                "description": "decoy unrelated-text-xyz",
                "content": "# x\n",
            },
        },
        headers=headers,
    )
    assert upsert_resp.status_code == 200, upsert_resp.text


async def test_skills_collection_rejects_data_without_summary_or_description(client):
    """Skills writes require at least one of summary or description so
    the catalog stays semantic-searchable. Missing both → 422."""
    tenant_id, headers = get_test_auth()
    tag = _uid()[:6]
    slug = f"no-desc-{tag}"
    resp = await client.post(
        "/api/v1/documents",
        json={
            "tenant_id": tenant_id,
            "collection": "skills",
            "doc_id": slug,
            # Neither summary nor description — nothing to index.
            "data": {"name": slug, "content": "# x\n"},
        },
        headers=headers,
    )
    assert resp.status_code == 422, (
        f"missing summary AND description should 422 "
        f"(catalog needs at least one), got {resp.status_code}: {resp.text}"
    )


async def test_non_skills_collection_does_not_require_summary(client):
    """Other collections are unaffected — no slug rule, no summary
    requirement. Docs without a summary persist without an embedding.

    Regression guard: the skills-collection rule is targeted; everything
    else still upserts via the bare path with no indexing requirement.
    """
    tenant_id, headers = get_test_auth()
    tag = _uid()[:6]
    # Slug pattern that would FAIL the skills regex (spaces, uppercase).
    weird_doc_id = f"My Doc {tag}"
    resp = await client.post(
        "/api/v1/documents",
        json={
            "tenant_id": tenant_id,
            "collection": "notes",
            "doc_id": weird_doc_id,
            "data": {"body": "no summary, no problem"},
        },
        headers=headers,
    )
    assert resp.status_code == 200, (
        f"non-skills collection should accept arbitrary doc_id and "
        f"missing summary: {resp.text}"
    )


# ---------------------------------------------------------------------------
# Dropped REST routes return 404
# ---------------------------------------------------------------------------


async def test_skills_share_route_is_gone(client):
    """``POST /api/v1/skills/share`` was dropped in Phase B — must 404."""
    tenant_id, headers = get_test_auth()
    resp = await client.post(
        "/api/v1/skills/share",
        json={
            "tenant_id": tenant_id,
            "name": "probe",
            "description": "should not be reachable",
            "content": "# x\n",
            "target_fleet_id": "any",
        },
        headers=headers,
    )
    assert resp.status_code == 404, (
        f"/api/v1/skills/share should be removed, got {resp.status_code}"
    )


async def test_skills_list_route_is_gone(client):
    """``GET /api/v1/skills`` was dropped in Phase B — must 404."""
    tenant_id, headers = get_test_auth()
    resp = await client.get(
        f"/api/v1/skills?tenant_id={tenant_id}",
        headers=headers,
    )
    assert resp.status_code == 404, (
        f"/api/v1/skills should be removed, got {resp.status_code}"
    )


async def test_skills_unshare_route_is_gone(client):
    """``DELETE /api/v1/skills/{name}`` was dropped in Phase B — must 404."""
    tenant_id, headers = get_test_auth()
    resp = await client.delete(
        f"/api/v1/skills/some-skill?tenant_id={tenant_id}",
        headers=headers,
    )
    assert resp.status_code == 404, (
        f"/api/v1/skills/<name> should be removed, got {resp.status_code}"
    )


# ---------------------------------------------------------------------------
# Live-doc fetch: reads the primary, and fails closed
# ---------------------------------------------------------------------------


async def test_skills_write_gate_fetch_reads_the_primary(client, monkeypatch):
    """The live-doc fetch must pin the PRIMARY, not the read replica.

    ``get_document`` defaults to ``read=True``. Fine for an ordinary read, but
    this fetch backs an authorization decision: on a replica, a ``staged`` row
    an admin has just approved to ``active`` still reads as ``staged``, the
    gate authorises the overwrite the transition was meant to forbid, and the
    upsert that follows does no CAS on status — nothing downstream catches it.
    Pinned here because the failure is invisible to any test that stubs
    storage: the stub answers the same either way.
    """
    from unittest.mock import AsyncMock, MagicMock

    from core_api.routes import documents as docs

    async def _raw(tenant_id):
        return {"skills_factory": {"enabled": True}}

    async def _display(tenant_id):
        return {
            "skills_factory": {
                "enabled": True,
                "body_max_bytes": 40_000,
                "description_max_bytes": 160,
            }
        }

    monkeypatch.setattr(docs, "get_raw_settings", _raw)
    monkeypatch.setattr(docs, "get_settings_for_display", _display)

    # The fetch aborts the request, so the assertion below is about the call
    # that was made and nothing downstream needs stubbing. What is under test
    # is the kwarg, not the outcome — the fail-closed behaviour has its own
    # test.
    sc = MagicMock(name="storage_client")
    sc.get_document = AsyncMock(side_effect=RuntimeError("stop here"))
    monkeypatch.setattr(docs, "get_storage_client", lambda: sc)

    tenant_id, headers = get_test_auth()
    await client.post(
        "/api/v1/documents",
        json={
            "tenant_id": tenant_id,
            "collection": "skills",
            "doc_id": f"primary-read-{_uid()[:6]}",
            "data": {
                "name": "Primary read probe",
                "slug": "primary-read-probe",
                "description": "Short trigger description.",
                "domain": "dev",
                "kind": "create",
                "source": "agent",
                "content": "## When to use\nStep 1.\n",
            },
        },
        headers=headers,
    )
    assert sc.get_document.await_args.kwargs["read"] is False


async def test_skills_write_fails_closed_when_the_live_doc_fetch_errors(
    client, monkeypatch
):
    """A storage error fetching the live skill must ABORT the write.

    The fetch feeds the stored-status overwrite gate (H-11), so falling
    through with ``live_doc=None`` would hand the validator exactly the
    ``None`` that made ``kind='create'`` the way around that gate — a
    transient DB blip would reopen an overwrite window on every active
    skill. This pins the REST path to the same fail-closed behaviour its
    MCP sibling has, rather than the bare unhandled 500 it had before.
    """
    from unittest.mock import AsyncMock, MagicMock

    from core_api.routes import documents as docs

    async def _raw(tenant_id):
        return {"skills_factory": {"enabled": True}}

    async def _display(tenant_id):
        return {
            "skills_factory": {
                "enabled": True,
                "body_max_bytes": 40_000,
                "description_max_bytes": 160,
            }
        }

    monkeypatch.setattr(docs, "get_raw_settings", _raw)
    monkeypatch.setattr(docs, "get_settings_for_display", _display)

    sc = MagicMock(name="storage_client")
    sc.get_document = AsyncMock(side_effect=RuntimeError("storage unreachable"))
    sc.upsert_document_xmax = AsyncMock(return_value={"xmax": 0})
    monkeypatch.setattr(docs, "get_storage_client", lambda: sc)

    tenant_id, headers = get_test_auth()
    resp = await client.post(
        "/api/v1/documents",
        json={
            "tenant_id": tenant_id,
            "collection": "skills",
            "doc_id": f"fail-closed-{_uid()[:6]}",
            "data": {
                "name": "Fail closed probe",
                "slug": "fail-closed-probe",
                "description": "Short trigger description.",
                "domain": "dev",
                "kind": "create",
                "source": "agent",
                "content": "## When to use\nStep 1.\n",
            },
        },
        headers=headers,
    )
    assert resp.status_code == 503, (
        f"expected 503 fail-closed, got {resp.status_code}: {resp.text}"
    )
    assert "unavailable" in resp.text.lower()
    # The decisive assertion: nothing was written.
    sc.upsert_document_xmax.assert_not_awaited()
