"""SAFE-01 — unknown fields on WRITE bodies are rejected, not dropped.

The defect: every request model inherited pydantic's default
``extra="ignore"``, so a field the model didn't declare was discarded without
a word. A caller who typed ``"contnet"`` instead of ``"content"``, or who sent
a plausible-but-unsupported key like ``"tags"``, got ``201 Created`` back and
a stored row missing the data they sent. The write "succeeded" and the payload
was gone.

Three things are pinned here, and the third matters as much as the first two:

1.  A bogus field on a write body → 422 that NAMES the field. One test per
    major write endpoint, because the fix is per-model and a model that got
    missed would otherwise still silently drop.
2.  Every legitimate spelling still works — including the ``AliasChoices``
    spellings on the search surface, which is the C1+C2 trap this repo has
    already been bitten by once (see ``schemas.SearchRequest``). Declared
    aliases are not "extra" fields, so ``forbid`` cannot break them; these
    tests are what makes that a checked fact rather than a claim.
3.  Search/filter/query bodies still ACCEPT unknown fields. That asymmetry is
    a deliberate product decision, not an oversight the fix didn't reach —
    so it gets a test that fails if someone later "finishes the job" and
    makes the search models strict too.
"""

import pytest

from tests.conftest import get_test_auth, uid as _uid


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _assert_names_field(resp, field: str) -> None:
    """The 422 must identify the offending field, not just say 'invalid'.

    Checks the canonical envelope (``core_api.errors.make_error_payload``),
    not pydantic's raw dump: a caller reading ``error.message`` must be able
    to see WHICH key was wrong without parsing the ``detail`` array.
    """
    assert resp.status_code == 422, f"expected 422, got {resp.status_code}: {resp.text}"
    body = resp.json()

    err = body.get("error")
    assert err, f"no canonical error envelope in {body}"
    assert err["code"] == "INVALID_ARGUMENTS", err
    assert field in err["message"], f"message does not name {field!r}: {err['message']}"

    # Machine-readable list, for clients that shouldn't have to regex a string.
    # Entries are dotted paths, so a top-level field is the whole entry and a
    # nested one is its leaf (``facts.0.saliance``).
    unknown = err["details"]["unknown_fields"]
    assert any(path.split(".")[-1] == field for path in unknown), unknown

    # Back-compat: pydantic's own array is preserved and carries the field
    # in ``loc``. Existing clients read ``detail``; they keep working.
    locs = [e.get("loc", []) for e in body["detail"]]
    assert any(field in loc for loc in locs), body["detail"]


# ---------------------------------------------------------------------------
# 1. Write bodies reject unknown fields
# ---------------------------------------------------------------------------


async def test_memory_create_rejects_unknown_field(client):
    """POST /memories — the headline case. A typo'd 'content' returned 201."""
    tenant_id, headers = get_test_auth()
    resp = await client.post(
        "/api/v1/memories",
        json={
            "tenant_id": tenant_id,
            "agent_id": f"agent-{_uid()}",
            "content": f"a memory with one misspelled field alongside it {_uid()}",
            "contnet": "this typo used to be accepted and thrown away",
        },
        headers=headers,
    )
    _assert_names_field(resp, "contnet")


async def test_memory_create_rejects_plausible_but_undeclared_field(client):
    """``tags`` is the realistic version of the bug.

    It is not a typo — it is a field a caller reasonably expects to exist
    (the plugin itself was sending it on four separate write paths, and the
    server was dropping it every time). Silently ignoring it is worse than a
    typo, because nothing about the request looks wrong.
    """
    tenant_id, headers = get_test_auth()
    resp = await client.post(
        "/api/v1/memories",
        json={
            "tenant_id": tenant_id,
            "agent_id": f"agent-{_uid()}",
            "content": f"a memory whose tags would have been silently discarded {_uid()}",
            "tags": ["alpha", "beta"],
        },
        headers=headers,
    )
    _assert_names_field(resp, "tags")


async def test_memory_update_rejects_unknown_field(client):
    """PATCH /memories/{id} — a typo here is a silent 200 no-op."""
    tenant_id, headers = get_test_auth()
    created = await client.post(
        "/api/v1/memories",
        json={
            "tenant_id": tenant_id,
            "agent_id": f"agent-{_uid()}",
            "content": f"a memory that will be patched with a bad field name {_uid()}",
        },
        headers=headers,
    )
    assert created.status_code == 201, created.text
    memory_id = created.json()["id"]

    resp = await client.patch(
        f"/api/v1/memories/{memory_id}",
        params={"tenant_id": tenant_id},
        json={"weightt": 0.9},
        headers=headers,
    )
    _assert_names_field(resp, "weightt")


async def test_bulk_envelope_rejects_unknown_field(client):
    """POST /memories/bulk — the ENVELOPE is strict.

    A single-write field sent at the top level of a batch request applies to
    nothing; it used to be dropped, so the caller believed it had been applied
    to every item.
    """
    tenant_id, headers = get_test_auth()
    resp = await client.post(
        "/api/v1/memories/bulk",
        json={
            "tenant_id": tenant_id,
            "agent_id": f"agent-{_uid()}",
            "items": [{"content": "a perfectly valid item in the batch"}],
            "write_mode": "strong",
        },
        headers=headers,
        # Envelope validation happens before the header check, so the
        # 422 lands regardless — but send it so the test is honest about
        # what a real caller does.
        **{},
    )
    _assert_names_field(resp, "write_mode")


async def test_document_upsert_rejects_unknown_field(client):
    """POST /documents — ``agent_id`` was the plugin's own junk field."""
    tenant_id, headers = get_test_auth()
    tag = _uid()
    resp = await client.post(
        "/api/v1/documents",
        json={
            "tenant_id": tenant_id,
            "collection": f"notes-{tag}",
            "doc_id": f"doc-{tag}",
            "data": {"title": "Hello"},
            "agent_id": "the-documents-routes-take-identity-from-auth",
        },
        headers=headers,
    )
    _assert_names_field(resp, "agent_id")


async def test_entity_upsert_rejects_unknown_field(client):
    tenant_id, headers = get_test_auth()
    resp = await client.post(
        "/api/v1/entities/upsert",
        json={
            "tenant_id": tenant_id,
            "entity_type": "person",
            "canonical_name": f"Person {_uid()}",
            "attribute": {"role": "singular spelling of 'attributes'"},
        },
        headers=headers,
    )
    _assert_names_field(resp, "attribute")


async def test_ingest_preview_rejects_unknown_field(client):
    tenant_id, headers = get_test_auth()
    resp = await client.post(
        "/api/v1/ingest/preview",
        json={
            "tenant_id": tenant_id,
            "content": f"some text to preview facts from {_uid()}",
            "focous": "typo of focus — used to be dropped, changing the result",
        },
        headers=headers,
    )
    _assert_names_field(resp, "focous")


async def test_ingest_commit_rejects_unknown_field_on_nested_fact(client):
    """Nested models are strict too, and the error locates the nested field.

    ``IngestFact`` sits inside ``IngestCommitRequest.facts[]``. A leaf-only
    error message would send the caller hunting through the top-level body,
    so the message carries the dotted path as well as the name.
    """
    tenant_id, headers = get_test_auth()
    resp = await client.post(
        "/api/v1/ingest/commit",
        json={
            "tenant_id": tenant_id,
            "facts": [
                {
                    "content": f"a fact whose salience key is misspelled {_uid()}",
                    "saliance": 0.9,
                }
            ],
        },
        headers=headers,
    )
    _assert_names_field(resp, "saliance")
    envelope = resp.json()["error"]
    assert "facts.0.saliance" in envelope["details"]["unknown_fields"], envelope
    assert "facts.0.saliance" in envelope["message"], envelope["message"]


async def test_evolve_report_rejects_unknown_field(client):
    tenant_id, headers = get_test_auth()
    resp = await client.post(
        "/api/v1/evolve/report",
        json={
            "tenant_id": tenant_id,
            "outcome": f"the refactor landed and the gate went green {_uid()}",
            "outcome_type": "success",
            "related_id": ["singular spelling of related_ids"],
        },
        headers=headers,
    )
    _assert_names_field(resp, "related_id")


async def test_insights_generate_rejects_unknown_field(client):
    tenant_id, headers = get_test_auth()
    resp = await client.post(
        "/api/v1/insights/generate",
        json={
            "tenant_id": tenant_id,
            "focus": "patterns",
            "scopes": "all",
        },
        headers=headers,
    )
    _assert_names_field(resp, "scopes")


async def test_agent_tune_rejects_unknown_knob(client):
    """PATCH /agents/{id}/tune — a misspelled knob was a 200 that tuned nothing.

    ``SearchProfileUpdate`` is built by ``create_model``, so it needed
    ``__config__`` rather than a ``model_config`` field; this pins that the
    generated model actually came out strict.
    """
    tenant_id, headers = get_test_auth()
    agent_id = f"agent-{_uid()}"
    resp = await client.patch(
        f"/api/v1/agents/{agent_id}/tune",
        params={"tenant_id": tenant_id},
        json={"fts_wieght": 0.5},
        headers=headers,
    )
    _assert_names_field(resp, "fts_wieght")


# ---------------------------------------------------------------------------
# 2. Legitimate spellings still work — the alias trap
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "payload",
    [
        pytest.param({"memory_type_filter": "fact"}, id="canonical-long-form"),
        pytest.param({"memory_type": "fact"}, id="alias-short-form"),
        pytest.param({"status_filter": "active"}, id="canonical-status"),
        pytest.param({"status": "active"}, id="alias-status"),
        pytest.param(
            {"memory_type": "fact", "status": "active"}, id="both-mcp-spellings"
        ),
    ],
)
async def test_search_alias_spellings_still_accepted(client, payload):
    """Every ``AliasChoices`` spelling on /search keeps working.

    An alias is a DECLARED spelling, so ``extra="forbid"`` would not have
    rejected it even if /search had been made strict — but "would not have"
    is exactly the kind of reasoning the C1+C2 incident punished, so each
    spelling is exercised against the running route instead of argued about.
    """
    tenant_id, headers = get_test_auth()
    resp = await client.post(
        "/api/v1/search",
        json={"tenant_id": tenant_id, "query": "anything", "top_k": 1, **payload},
        headers=headers,
    )
    assert resp.status_code == 200, resp.text


async def test_write_accepts_every_declared_field(client):
    """A write using the full declared vocabulary is unaffected by ``forbid``.

    Guards the other direction of the change: strictness must reject only
    what the model does not declare, never narrow what it does.
    """
    tenant_id, headers = get_test_auth()
    resp = await client.post(
        "/api/v1/memories",
        json={
            "tenant_id": tenant_id,
            "agent_id": f"agent-{_uid()}",
            "content": f"a fully-specified write {_uid()}",
            "memory_type": "fact",
            "weight": 0.7,
            "source_uri": "https://example.invalid/doc",
            "run_id": f"run-{_uid()}",
            "metadata": {"tags": ["alpha"]},
            "status": "active",
            "visibility": "scope_team",
            "write_mode": "fast",
        },
        headers=headers,
    )
    assert resp.status_code == 201, resp.text


# ---------------------------------------------------------------------------
# 3. The deliberate asymmetry — search/filter stays permissive
# ---------------------------------------------------------------------------


async def test_search_still_accepts_unknown_field(client):
    """DELIBERATE. Do not "fix" this test by making /search strict.

    Erni's scope decision for SAFE-01 was write bodies only. A misspelled
    filter returns a wrong RESULT SET, which the caller can see; a misspelled
    write field corrupts STORED DATA, which they cannot. Only the second is
    worth a breaking change to every integrator.

    If this test starts failing, someone made the search models strict — that
    is a product decision, not a tidy-up, and it needs to be made explicitly.
    """
    tenant_id, headers = get_test_auth()
    resp = await client.post(
        "/api/v1/search",
        json={
            "tenant_id": tenant_id,
            "query": "anything",
            "top_k": 1,
            "not_a_real_filter": "ignored on purpose",
        },
        headers=headers,
    )
    assert resp.status_code == 200, resp.text


async def test_document_query_still_accepts_unknown_field(client):
    """Same asymmetry on the document surface: /documents is strict (above),
    /documents/query is not."""
    tenant_id, headers = get_test_auth()
    resp = await client.post(
        "/api/v1/documents/query",
        json={
            "tenant_id": tenant_id,
            "collection": f"notes-{_uid()}",
            "not_a_real_filter": "ignored on purpose",
        },
        headers=headers,
    )
    assert resp.status_code == 200, resp.text


async def test_heartbeat_still_accepts_unknown_field(client):
    """DELIBERATE, and the one write-shaped body left permissive.

    Plugin↔backend has no version handshake (RELEASING.md § Compatibility):
    installs in the field roll forward on their own cadence, and the command
    channel rides the heartbeat RESPONSE. A 422 over an unrecognised
    telemetry key would take the node offline in fleet views AND cut the
    channel carrying the deploy command that would have fixed it. The body
    also carries no caller-owned data — only the node describing itself.
    """
    tenant_id, headers = get_test_auth()
    resp = await client.post(
        "/api/v1/fleet/heartbeat",
        json={
            "tenant_id": tenant_id,
            "node_name": f"node-{_uid()}",
            "a_field_from_a_newer_plugin": {"rolled": "forward"},
        },
        headers=headers,
    )
    assert resp.status_code == 200, resp.text


# ---------------------------------------------------------------------------
# 4. Bulk items: per-item error, never a whole-batch 422
# ---------------------------------------------------------------------------


async def test_bulk_item_unknown_field_is_a_per_item_error(client):
    """One item's typo must not take its valid siblings down with it.

    ``BulkMemoryItem`` is the single write model deliberately left
    non-``forbid``: ``forbid`` raises while parsing the whole request, which
    is precisely the whole-batch rejection the model's additive-tolerance
    policy exists to prevent. It is ``extra="allow"`` instead, and
    ``create_memories_bulk`` reports the unknown keys per item — same
    information, delivered without discarding the good rows.
    """
    tenant_id, headers = get_test_auth()
    resp = await client.post(
        "/api/v1/memories/bulk",
        json={
            "tenant_id": tenant_id,
            "agent_id": f"agent-{_uid()}",
            "items": [
                {"content": f"a valid item that must still be written {_uid()}"},
                {
                    "content": f"an item with a misspelled key {_uid()}",
                    "meta_data": {"dropped": "silently, before this fix"},
                },
            ],
        },
        headers=headers | {"X-Bulk-Attempt-Id": f"attempt-{_uid()}"},
    )
    # 207 Multi-Status: some created, some errored. NOT 422 for the batch.
    assert resp.status_code == 207, f"{resp.status_code}: {resp.text}"
    body = resp.json()

    results = {r["index"]: r for r in body["results"]}
    assert results[0]["status"] == "created", results[0]
    assert results[1]["status"] == "error", results[1]
    assert "meta_data" in results[1]["error"], results[1]["error"]
    assert body["created"] == 1 and body["errors"] == 1, body
