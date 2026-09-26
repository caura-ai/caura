"""C25 — the platform/caller metadata boundary (MemoryImpact C9 / AX-audit N8).

Enrichment used to write its telemetry straight into the caller's metadata
dict: a caller's own ``metadata.summary`` was silently overwritten, and a
caller-supplied ``llm_ms`` survived as fake telemetry when enrichment didn't
run. Now: platform values land in ``metadata["_system"]`` (mirrored to the
legacy top-level keys for one release unless caller-owned), forgeable
telemetry keys are stripped from caller input, and ``MemoryOut.system_metadata``
exposes the platform view for new AND historical rows.
"""

import pytest

from core_api.services.system_metadata import (
    CALLER_OWNABLE_KEYS,
    PLATFORM_ONLY_KEYS,
    SYSTEM_NAMESPACE,
    extract_system_metadata,
    sanitize_caller_metadata,
    set_system_value,
)
from tests.conftest import get_test_auth, uid

pytestmark = pytest.mark.unit


# --- sanitize: forgeable keys stripped, caller keys kept -----------------------


def test_sanitize_strips_platform_only_keys():
    dirty = {"llm_ms": 9999, "write_latency_ms": 1, "note": "mine", "_system": {"x": 1}}
    clean = sanitize_caller_metadata(dirty)
    assert clean == {"note": "mine"}


def test_sanitize_keeps_caller_ownable_keys():
    clean = sanitize_caller_metadata({"summary": "MINE", "tags": ["a"], "k": 1})
    assert clean == {"summary": "MINE", "tags": ["a"], "k": 1}


def test_sanitize_none_and_empty():
    assert sanitize_caller_metadata(None) == {}
    assert sanitize_caller_metadata({}) == {}


# --- set_system_value: dual-write + clobber fix --------------------------------


def test_platform_key_dual_written():
    md: dict = {}
    set_system_value(md, "llm_ms", 123)
    assert md["llm_ms"] == 123  # legacy mirror (one release)
    assert md[SYSTEM_NAMESPACE]["llm_ms"] == 123


def test_caller_owned_summary_not_clobbered():
    md: dict = {"summary": "MINE"}
    set_system_value(md, "summary", "platform version", caller_keys=frozenset(md))
    assert md["summary"] == "MINE"  # the N8 clobber fix
    assert md[SYSTEM_NAMESPACE]["summary"] == "platform version"


def test_summary_fills_top_level_when_caller_did_not_set_it():
    md: dict = {}
    set_system_value(md, "summary", "platform version", caller_keys=frozenset())
    assert md["summary"] == "platform version"
    assert md[SYSTEM_NAMESPACE]["summary"] == "platform version"


# --- extract: read-side view for new and historical rows -----------------------


def test_extract_from_historical_row_legacy_keys_only():
    md = {"summary": "s", "llm_ms": 42, "custom": "keep-out"}
    sysm = extract_system_metadata(md)
    assert sysm == {"summary": "s", "llm_ms": 42}


def test_extract_prefers_namespace_over_legacy():
    md = {"summary": "CALLER", SYSTEM_NAMESPACE: {"summary": "PLATFORM"}}
    assert extract_system_metadata(md)["summary"] == "PLATFORM"


def test_extract_none_when_nothing_platform_written():
    assert extract_system_metadata({"custom": 1}) is None
    assert extract_system_metadata(None) is None
    assert extract_system_metadata({}) is None


# --- merge step end-to-end ------------------------------------------------------


class _Enrichment:
    memory_type = "fact"
    weight = 0.5
    title = "t"
    summary = "PLATFORM SUMMARY"
    tags = ["p1"]
    llm_ms = 77
    ts_valid_start = None
    ts_valid_end = None
    contains_pii = False
    pii_types = None
    business_relevance = "business"
    status = None


class _Input:
    memory_type = None
    weight = None
    ts_valid_start = None
    ts_valid_end = None
    status = None

    def __init__(self, metadata):
        self.metadata = metadata


async def test_merge_step_preserves_caller_summary():
    """Input arrives PRE-SANITIZED (the single-write path strips forgeries
    before the governance gate); the merge step's job is the clobber fix."""
    from core_api.pipeline.context import PipelineContext
    from core_api.pipeline.steps.write.merge_enrichment_fields import (
        MergeEnrichmentFields,
    )

    ctx = PipelineContext(
        data={
            "input": _Input(
                sanitize_caller_metadata(
                    {"summary": "MINE", "llm_ms": 9999, "custom": "kept"}
                )
            ),
            "enrichment": _Enrichment(),
            "resolved_write_mode": "strong",
        }
    )
    await MergeEnrichmentFields().execute(ctx)
    md = ctx.data["memory_fields"]["metadata"]
    assert md["summary"] == "MINE"  # caller wins at top level
    assert md[SYSTEM_NAMESPACE]["summary"] == "PLATFORM SUMMARY"
    assert md["llm_ms"] == 77  # forged 9999 stripped at entry; real telemetry recorded
    assert md[SYSTEM_NAMESPACE]["llm_ms"] == 77
    assert md["custom"] == "kept"
    assert md["tags"] == ["p1"]  # caller didn't own tags → legacy mirror filled


async def test_merge_step_does_not_clobber_upstream_gate_flags():
    """The governance gate writes PII flags into the input metadata BEFORE the
    merge step — the regression that moved sanitize to the write entry point."""
    from core_api.pipeline.context import PipelineContext
    from core_api.pipeline.steps.write.merge_enrichment_fields import (
        MergeEnrichmentFields,
    )

    gate_written = {
        "contains_pii": True,
        "pii_types": ["email"],
        SYSTEM_NAMESPACE: {"contains_pii": True, "pii_types": ["email"]},
    }
    ctx = PipelineContext(
        data={
            "input": _Input(dict(gate_written)),
            "enrichment": None,
            "resolved_write_mode": "fast",
        }
    )
    await MergeEnrichmentFields().execute(ctx)
    md = ctx.data["memory_fields"]["metadata"]
    assert md["contains_pii"] is True
    assert md["pii_types"] == ["email"]


def test_registries_are_disjoint():
    assert not (PLATFORM_ONLY_KEYS & CALLER_OWNABLE_KEYS)


# --- M-48 / M-52: the boundary has to hold on EVERY write surface -------------
#
# Everything above tests the sanitizer in isolation, and it always passed —
# because the helper was never the broken part. ``create_memory`` called it and
# the other two write paths did not: POST /memories/bulk runs its own
# governance/embed/write loop, and PATCH /memories/{id} handed the caller's dict
# to storage as ``metadata_``/``metadata_patch``.
#
# So these go through the REST routes rather than calling the helper, because a
# helper-level test cannot tell whether a route reaches it.

_FORGED = {
    "llm_ms": 9999,  # fake telemetry
    "contains_pii": False,  # governance verdict
    "pii_types": [],
    SYSTEM_NAMESPACE: {"llm_ms": 1},  # the namespace itself
    "mine": "keep me",  # caller's own key
    "summary": "CALLER SUMMARY",  # caller-OWNABLE, must survive
}


def _assert_boundary_held(metadata: dict) -> None:
    for key in ("llm_ms", "contains_pii", "pii_types"):
        assert key not in metadata, (
            f"forged platform key {key!r} reached the row: {metadata!r}"
        )
    assert SYSTEM_NAMESPACE not in metadata or "llm_ms" not in metadata.get(
        SYSTEM_NAMESPACE, {}
    ), f"caller wrote into the {SYSTEM_NAMESPACE} namespace: {metadata!r}"
    assert metadata.get("mine") == "keep me", (
        f"over-refusal: the caller's own key was stripped: {metadata!r}"
    )
    assert metadata.get("summary") == "CALLER SUMMARY", (
        f"over-refusal: a caller-ownable key was stripped: {metadata!r}"
    )


async def _get_metadata(client, headers, tenant_id: str, memory_id: str) -> dict:
    resp = await client.get(
        f"/api/v1/memories/{memory_id}?tenant_id={tenant_id}", headers=headers
    )
    assert resp.status_code == 200, resp.text
    return resp.json().get("metadata") or {}


async def test_bulk_write_sanitizes_caller_metadata(client):
    """M-48. POST /memories/bulk never passed through ``create_memory``."""
    tenant_id, headers = get_test_auth()
    resp = await client.post(
        "/api/v1/memories/bulk",
        json={
            "tenant_id": tenant_id,
            "agent_id": f"c25-bulk-{uid()}",
            "items": [{"content": f"bulk c25 boundary {uid()}", "metadata": _FORGED}],
        },
        headers={**headers, "X-Bulk-Attempt-Id": f"c25-{uid()}"},
    )
    assert resp.status_code in (200, 201), resp.text
    results = resp.json()["results"]
    assert len(results) == 1, resp.text
    memory_id = results[0].get("id")
    assert memory_id, f"bulk item did not come back with an id: {results!r}"

    _assert_boundary_held(await _get_metadata(client, headers, tenant_id, memory_id))


@pytest.mark.parametrize("mode", ["replace", "merge"])
async def test_update_sanitizes_caller_metadata(client, mode: str):
    """M-52. Both modes carried it.

    ``replace`` set ``metadata_`` wholesale; ``merge`` went to storage as
    ``metadata_patch``, whose shallow ``||`` lets a top-level key overwrite the
    platform's. The second is the sharper one: it rewrites governance output on
    an EXISTING row, so a PII verdict recorded at creation could be flipped
    afterwards by the same credential that wrote the memory.
    """
    tenant_id, headers = get_test_auth()
    created = await client.post(
        "/api/v1/memories",
        json={
            "tenant_id": tenant_id,
            "agent_id": f"c25-upd-{uid()}",
            "content": f"update c25 boundary {uid()}",
            "memory_type": "fact",
        },
        headers=headers,
    )
    assert created.status_code in (200, 201), created.text
    memory_id = created.json()["id"]

    patched = await client.patch(
        f"/api/v1/memories/{memory_id}?tenant_id={tenant_id}",
        json={"metadata": _FORGED, "metadata_mode": mode},
        headers=headers,
    )
    assert patched.status_code == 200, patched.text

    _assert_boundary_held(await _get_metadata(client, headers, tenant_id, memory_id))


# --- the other edge of the same boundary: platform callers keep their stamps --
#
# Sanitizing bulk items is what a caller must not get around. It is also what a
# trusted internal caller must not be silently subjected to. Ingest sets
# CAURA-703 provenance on every fact it commits, and while that stamp travelled
# in item ``metadata`` the new sanitizer deleted it — flipping
# ``memory_type_agent_set`` False->True on every ingested memory, with no test
# failing, because the field has no in-repo consumer.
#
# The channel is now an argument, which no request body can reach.


async def test_bulk_honours_memory_type_is_agent_set():
    """The real ``create_memories_bulk`` — not the ingest fixture's stand-in.

    ``tests/test_ingest_commit.py`` monkeypatches ``create_memories_bulk``, so
    it can only assert what ingest passes. This asserts the function acts on it.
    """
    import uuid

    from core_api.clients.storage_client import get_storage_client
    from core_api.schemas import BulkMemoryCreate, BulkMemoryItem
    from core_api.services.memory_service import create_memories_bulk

    tenant_id, _ = get_test_auth()
    req = BulkMemoryCreate(
        tenant_id=tenant_id,
        fleet_id="test-fleet",
        agent_id=f"c25-703-{uid()}",
        # A type IS supplied, so the inferred value would be True. Only the
        # argument can make it False.
        items=[
            BulkMemoryItem(content=f"c25 703 provenance {uid()}", memory_type="fact")
        ],
    )
    resp = await create_memories_bulk(
        req, bulk_attempt_id=uuid.uuid4().hex, memory_type_is_agent_set=False
    )
    assert resp.results[0].status == "created", resp.results[0]

    mem = await get_storage_client().get_memory(resp.results[0].id, tenant_id)
    assert (mem["metadata_"] or {}).get("memory_type_agent_set") is False, (
        "the trusted caller's provenance did not survive to the row: "
        f"{mem['metadata_']}"
    )


async def test_bulk_infers_agent_set_when_no_caller_says_otherwise():
    """Control: without the argument the flag is still inferred from the item.

    Guards the opposite failure — an implementation that always writes False.
    """
    import uuid

    from core_api.clients.storage_client import get_storage_client
    from core_api.schemas import BulkMemoryCreate, BulkMemoryItem
    from core_api.services.memory_service import create_memories_bulk

    tenant_id, _ = get_test_auth()
    req = BulkMemoryCreate(
        tenant_id=tenant_id,
        fleet_id="test-fleet",
        agent_id=f"c25-703-{uid()}",
        items=[BulkMemoryItem(content=f"c25 703 inferred {uid()}", memory_type="fact")],
    )
    resp = await create_memories_bulk(req, bulk_attempt_id=uuid.uuid4().hex)
    assert resp.results[0].status == "created", resp.results[0]

    mem = await get_storage_client().get_memory(resp.results[0].id, tenant_id)
    assert (mem["metadata_"] or {}).get("memory_type_agent_set") is True, mem[
        "metadata_"
    ]


# --- the registry was incomplete, which made the boundary partial ------------
#
# M-48/M-52 fixed WHERE sanitation runs. This is WHAT it strips. Twelve keys
# were reserved; the platform writes considerably more than twelve, and every
# one it wrote without reserving was accepted verbatim from callers on all
# three surfaces — including the create path, which C25 always "covered".
#
# Governance verdicts are the sharp end, the same class as ``contains_pii``:
# ``GovernanceDecision`` writes ``nonbusiness_kept_private`` to record that it
# kept a non-business memory private, and a caller could simply assert it.
# ``near_duplicate_of`` is worse than telemetry in a different way — it is
# documented as read back by callers to decide whether to merge or undo, so a
# forged one redirects a decision rather than just misreporting one.

_NEWLY_RESERVED: dict = {
    # governance verdicts
    "governance_llm_uncertain": "FORGED",
    "nonbusiness_kept_private": "FORGED",
    # dedup / near-duplicate outcomes
    "near_duplicate_of": "FORGED",
    "near_duplicate_similarity": -1.0,
    "near_dup_skipped_reason": "FORGED",
    "dedup_skipped_reason": "FORGED",
    "dedup_candidate_similarity": -1.0,
    "dedup_judge_confidence": -1.0,
    "dedup_subject_preflight": "FORGED",
    # timing
    "near_dup_check_ms": -1,
    "dedup_judge_ms": -1,
    "triple_emission_ms": -1,
    # lineage and enrichment output
    "parent_memory_id": "FORGED",
    "auto_chunked": "FORGED",
    "child_count": -1,
    "retrieval_hint": "FORGED",
}


async def _write_with_metadata(client, headers, tenant_id: str, surface: str, metadata):
    """Write ``metadata`` through one of the three caller-facing surfaces."""
    if surface == "create":
        resp = await client.post(
            "/api/v1/memories",
            json={
                "tenant_id": tenant_id,
                "agent_id": f"c25-reg-{uid()}",
                "content": f"registry boundary create {uid()}",
                "memory_type": "fact",
                "metadata": metadata,
            },
            headers=headers,
        )
        assert resp.status_code in (200, 201), resp.text
        return resp.json()["id"]

    if surface == "bulk":
        resp = await client.post(
            "/api/v1/memories/bulk",
            json={
                "tenant_id": tenant_id,
                "agent_id": f"c25-reg-{uid()}",
                "items": [
                    {
                        "content": f"registry boundary bulk {uid()}",
                        "metadata": metadata,
                    }
                ],
            },
            headers={**headers, "X-Bulk-Attempt-Id": f"c25-reg-{uid()}"},
        )
        assert resp.status_code in (200, 201), resp.text
        results = resp.json()["results"]
        assert len(results) == 1, resp.text
        memory_id = results[0].get("id")
        assert memory_id, f"bulk item came back without an id: {results!r}"
        return memory_id

    created = await client.post(
        "/api/v1/memories",
        json={
            "tenant_id": tenant_id,
            "agent_id": f"c25-reg-{uid()}",
            "content": f"registry boundary update {uid()}",
            "memory_type": "fact",
        },
        headers=headers,
    )
    assert created.status_code in (200, 201), created.text
    memory_id = created.json()["id"]
    patched = await client.patch(
        f"/api/v1/memories/{memory_id}?tenant_id={tenant_id}",
        json={"metadata": metadata, "metadata_mode": "merge"},
        headers=headers,
    )
    assert patched.status_code == 200, patched.text
    return memory_id


@pytest.mark.parametrize("surface", ["create", "bulk", "update"])
async def test_platform_keys_outside_the_registry_are_not_forgeable(client, surface):
    """Every reserved key, on every surface.

    Asserts on the forged VALUES rather than key absence: several of these are
    legitimately platform-written, and a test that demanded their absence would
    fail the day the platform started writing one — reporting a security
    regression that had not happened.

    ``create`` is in the parametrize list on purpose. Before this, no test drove
    the sanitizer through the create ROUTE at all — every one called the helper
    directly, which is precisely how M-48 and M-52 stayed invisible.
    """
    tenant_id, headers = get_test_auth()
    payload = {**_NEWLY_RESERVED, "mine": "keep me"}

    memory_id = await _write_with_metadata(client, headers, tenant_id, surface, payload)
    metadata = await _get_metadata(client, headers, tenant_id, memory_id)

    survived = {k: metadata[k] for k in _NEWLY_RESERVED if k in metadata}
    forged = {k: v for k, v in survived.items() if v == _NEWLY_RESERVED[k]}
    assert not forged, f"caller-forged platform values reached the row: {forged!r}"

    assert metadata.get("mine") == "keep me", (
        f"over-refusal: the caller's own key was stripped: {metadata!r}"
    )


def test_source_is_deliberately_not_reserved():
    """``source`` is written by the platform AND by callers.

    Reserving it would strip ingest's own ``{"source": "ingest"}`` stamp — the
    M-48 regression shape. Pinned so the next person widening this registry has
    to read the reason before overriding it.
    """
    assert "source" not in PLATFORM_ONLY_KEYS
