"""oss-0814-l-08 — the caller/platform metadata boundary on the UPDATE surface.

C25 (#967) and its follow-ups closed the clobber on every path that CREATES a
memory: the caller's key set is snapshotted at write time and carried to
whichever enricher runs later, so ``metadata["summary"]`` / ``["tags"]`` supplied
on a create survive. ``tests/test_c25_caller_owned_metadata_all_paths.py`` pins
all four of those.

``PATCH /memories/{id}`` is the third surface on which a caller supplies
metadata, and the snapshot mechanism cannot see it. The key set is derived from
the CREATE payload (``_caller_owned_enrichment_metadata_keys``) and published
with the enrich request; a summary the caller adds afterwards is not in it. In
fast write mode — the default — enrichment is still in flight when that PATCH
lands, so the enricher writes its own summary over the one the caller just set,
on a row where the caller is the only party who ever supplied that key.

The snapshot is in-flight state. This file pins the fix: ownership is recorded
ON THE ROW (``metadata["_system"]["caller_owned"]``) at every surface that
accepts caller metadata, so a later writer can tell the two apart by reading the
row instead of by having been told at publish time.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import text

from core_api.services import memory_service
from core_api.services.system_metadata import SYSTEM_NAMESPACE

from .conftest import get_test_auth, uid

# ``asyncio_mode = auto`` (pytest.ini) collects the async tests here without a
# marker; a module-level ``pytest.mark.asyncio`` would warn on the one sync test.
pytestmark = pytest.mark.integration

PLATFORM_SUMMARY = "PLATFORM SUMMARY"


def _enrichment():
    return SimpleNamespace(
        memory_type="decision",
        weight=0.9,
        status="active",
        title="Enriched title",
        summary=PLATFORM_SUMMARY,
        tags=["platform-tag"],
        llm_ms=12,
        contains_pii=False,
        pii_types=[],
        retrieval_hint="",
        business_relevance="business",
        ts_valid_start=None,
        ts_valid_end=None,
    )


async def _stored_metadata(engine, memory_id: str) -> dict:
    async with engine.connect() as conn:
        row = (
            await conn.execute(
                text("SELECT metadata FROM memories WHERE id = :i"), {"i": memory_id}
            )
        ).first()
    assert row is not None, f"memory {memory_id} is not in the database"
    return row[0] or {}


async def _run_deferred_enrichment(
    memory_id: str, tenant_id: str, content: str
) -> None:
    """Run the inline-deployment enricher against the REAL storage client.

    ``caller_owned_metadata_keys=None`` on purpose: that is what the CREATE
    published, because at create time the caller owned nothing. The point of the
    test is that the row — not the message — has to carry the answer.
    """
    with (
        patch(
            "core_api.services.memory_enrichment.enrich_memory",
            new=AsyncMock(return_value=_enrichment()),
        ),
        patch(
            "core_api.services.organization_settings.resolve_config",
            new=AsyncMock(
                return_value=SimpleNamespace(
                    enrichment_enabled=True,
                    enrichment_provider="fake",
                    entity_extraction_enabled=False,
                )
            ),
        ),
    ):
        await memory_service._enrich_memory_background(
            uuid.UUID(memory_id),
            content,
            tenant_id,
            None,
            "l08-agent",
            caller_owned_metadata_keys=None,
        )


async def _write_then_patch(client, patch_metadata: dict) -> tuple[str, str, dict]:
    """Create a memory with no caller-ownable metadata, then PATCH some in."""
    tenant_id, headers = get_test_auth()
    content = (
        f"The caller annotated this row after writing it. {uid()} "
        "It carries enough surrounding context to clear the length gate."
    )
    resp = await client.post(
        "/api/v1/memories",
        json={
            "tenant_id": tenant_id,
            "agent_id": f"l08-{uid()}",
            "content": content,
            # Deliberately NOT summary/tags: the create snapshot must come out
            # empty, which is exactly the state the PATCH then contradicts.
            "metadata": {"project": "apollo"},
        },
        headers=headers,
    )
    assert resp.status_code in (200, 201), resp.text
    memory_id = resp.json()["id"]

    resp = await client.patch(
        f"/api/v1/memories/{memory_id}?tenant_id={tenant_id}",
        json={"metadata": patch_metadata},
        headers=headers,
    )
    assert resp.status_code == 200, resp.text
    return memory_id, tenant_id, {"content": content, "headers": headers}


async def test_a_patched_summary_survives_the_enrichment_that_lands_after_it(
    client, _engine
):
    """FAILS PRE-FIX. The enricher publishes with the CREATE's key set, which
    names nothing, so it mirrors its own summary to the top level and the
    caller's annotation is gone seconds after they wrote it."""
    memory_id, tenant_id, extra = await _write_then_patch(client, {"summary": "MINE"})

    before = await _stored_metadata(_engine, memory_id)
    assert before.get("summary") == "MINE", (
        f"the PATCH itself did not store the caller's summary: {before!r}"
    )

    await _run_deferred_enrichment(memory_id, tenant_id, extra["content"])

    stored = await _stored_metadata(_engine, memory_id)
    assert stored.get("summary") == "MINE", (
        "enrichment overwrote a summary the caller supplied by PATCH: "
        f"{stored.get('summary')!r}"
    )
    assert stored.get(SYSTEM_NAMESPACE, {}).get("summary") == PLATFORM_SUMMARY, (
        "the platform's own summary must still be recorded under _system — the "
        f"loser is preserved, not discarded: {stored.get(SYSTEM_NAMESPACE)!r}"
    )
    assert stored.get("project") == "apollo", "unrelated caller keys must survive"


async def test_patched_tags_survive_too(client, _engine):
    """``tags`` is the other half of ``CALLER_OWNABLE_KEYS``. Every writer wires
    the boundary per key, so a summary-only test would pass a fix that threaded
    the marker into one call and missed the one two lines below."""
    memory_id, tenant_id, extra = await _write_then_patch(client, {"tags": ["mine"]})

    await _run_deferred_enrichment(memory_id, tenant_id, extra["content"])

    stored = await _stored_metadata(_engine, memory_id)
    assert stored.get("tags") == ["mine"], (
        f"enrichment overwrote tags the caller supplied by PATCH: {stored.get('tags')!r}"
    )
    assert stored.get(SYSTEM_NAMESPACE, {}).get("tags") == ["platform-tag"]


async def test_the_deferred_worker_patch_cannot_take_a_patched_key_either(
    client, _engine
):
    """The other deployment. ``deployment_mode="deferred"`` runs enrichment in
    core-worker, which PATCHes core-storage-api directly and never reads the
    row — so no amount of core-api-side care reaches it, and the fix has to be
    enforced where the row is. This drives the worker's patch SHAPE through the
    storage client rather than the worker itself (separate pytest root): a
    ``metadata_patch`` carrying the platform's value in both homes, with no
    ownership marker, which is exactly what ``_build_patch`` emits."""
    from core_api.clients.storage_client import get_storage_client

    memory_id, tenant_id, _ = await _write_then_patch(
        client, {"summary": "MINE", "tags": ["mine"]}
    )

    applied = await get_storage_client().update_memory(
        memory_id,
        tenant_id,
        {
            "metadata_patch": {
                "summary": PLATFORM_SUMMARY,
                "tags": ["platform-tag"],
                "llm_ms": 12,
                "enrichment_pending": False,
                SYSTEM_NAMESPACE: {
                    "summary": PLATFORM_SUMMARY,
                    "tags": ["platform-tag"],
                    "llm_ms": 12,
                    "enrichment_pending": False,
                },
            }
        },
    )
    assert applied is not False, "the storage PATCH did not reach the row"

    stored = await _stored_metadata(_engine, memory_id)
    assert stored.get("summary") == "MINE", (
        f"the worker's patch took a key the caller owns: {stored.get('summary')!r}"
    )
    assert stored.get("tags") == ["mine"], (
        f"the worker's patch took the caller's tags: {stored.get('tags')!r}"
    )
    nested = stored.get(SYSTEM_NAMESPACE, {})
    assert nested.get("summary") == PLATFORM_SUMMARY, (
        "withholding the mirror must not discard the platform's value — it is "
        f"the _system copy that keeps it: {nested!r}"
    )
    assert nested.get("tags") == ["platform-tag"]
    assert stored.get("llm_ms") == 12, (
        "only CALLER_OWNABLE keys are withheld; platform-only telemetry in the "
        f"same patch must land as before: {stored!r}"
    )
    assert nested.get("enrichment_pending") is False, (
        "the pending flag must still clear in both homes, or a fast-mode row "
        "polls forever"
    )


async def test_a_caller_can_still_rewrite_a_key_they_already_claimed(client, _engine):
    """The exemption. Withholding is keyed on the ROW's marker, so without an
    escape the caller's FIRST claim on ``summary`` would block their second and
    the key would become permanently unwritable by anyone."""
    memory_id, tenant_id, extra = await _write_then_patch(client, {"summary": "MINE"})

    resp = await client.patch(
        f"/api/v1/memories/{memory_id}?tenant_id={tenant_id}",
        json={"metadata": {"summary": "MINE, CORRECTED"}},
        headers=extra["headers"],
    )
    assert resp.status_code == 200, resp.text

    stored = await _stored_metadata(_engine, memory_id)
    assert stored.get("summary") == "MINE, CORRECTED", (
        f"the caller could not rewrite their own claimed key: {stored!r}"
    )


async def test_claims_accumulate_across_patches(client, _engine):
    """The storage layer replaces the ``_system`` sub-object's keys wholesale,
    so a marker written as a bare list would forget last week's claim. A caller
    who claimed ``summary`` and later ``tags`` owns both."""
    memory_id, tenant_id, extra = await _write_then_patch(client, {"summary": "MINE"})

    resp = await client.patch(
        f"/api/v1/memories/{memory_id}?tenant_id={tenant_id}",
        json={"metadata": {"tags": ["mine"]}},
        headers=extra["headers"],
    )
    assert resp.status_code == 200, resp.text

    await _run_deferred_enrichment(memory_id, tenant_id, extra["content"])

    stored = await _stored_metadata(_engine, memory_id)
    assert stored.get("summary") == "MINE", (
        f"the earlier claim was dropped by the later PATCH: {stored!r}"
    )
    assert stored.get("tags") == ["mine"]


async def test_a_create_supplied_key_is_marked_on_the_row(client, _engine):
    """The create surfaces write the marker too — not because C25's in-flight
    snapshot is broken there, but because the row must answer the question on
    its own for every writer that arrives later, including ones that were never
    handed a snapshot."""
    from core_api.services.system_metadata import CALLER_OWNED_KEY

    tenant_id, headers = get_test_auth()
    resp = await client.post(
        "/api/v1/memories",
        json={
            "tenant_id": tenant_id,
            "agent_id": f"l08-{uid()}",
            "content": (
                f"The caller supplied their own summary at write time. {uid()} "
                "It carries enough surrounding context to clear the length gate."
            ),
            "metadata": {"summary": "MINE", "project": "apollo"},
        },
        headers=headers,
    )
    assert resp.status_code in (200, 201), resp.text

    stored = await _stored_metadata(_engine, resp.json()["id"])
    assert stored.get(SYSTEM_NAMESPACE, {}).get(CALLER_OWNED_KEY) == ["summary"], (
        f"the create path recorded no ownership marker: {stored!r}"
    )


async def test_a_row_the_caller_claimed_nothing_on_carries_no_marker(client, _engine):
    """``None`` rather than an empty list, matching the in-flight snapshot's own
    convention — and so the marker's presence is itself the signal."""
    from core_api.services.system_metadata import CALLER_OWNED_KEY

    tenant_id, headers = get_test_auth()
    resp = await client.post(
        "/api/v1/memories",
        json={
            "tenant_id": tenant_id,
            "agent_id": f"l08-{uid()}",
            "content": (
                f"The caller claimed nothing on this row. {uid()} "
                "It carries enough surrounding context to clear the length gate."
            ),
            "metadata": {"project": "apollo"},
        },
        headers=headers,
    )
    assert resp.status_code in (200, 201), resp.text

    stored = await _stored_metadata(_engine, resp.json()["id"])
    assert CALLER_OWNED_KEY not in stored.get(SYSTEM_NAMESPACE, {}), (
        f"an empty claim was recorded as a marker: {stored!r}"
    )


def test_the_storage_mirror_of_the_boundary_constants_does_not_drift():
    """core-storage-api cannot import core-api, so it carries its own copy of
    the three names the boundary is made of — the same arrangement core-worker
    has. This is the one pytest root that can import both, and a silent drift
    here disables the enforcement rather than failing it: a renamed marker key
    reads back as "the caller owns nothing" and every patch sails through."""
    from core_api.services.system_metadata import CALLER_OWNABLE_KEYS, CALLER_OWNED_KEY
    from core_api.services.system_metadata import SYSTEM_NAMESPACE as CORE_NAMESPACE
    from core_storage_api.services.postgres_service import (
        _CALLER_OWNABLE_KEYS,
        _CALLER_OWNED_KEY,
        _SYSTEM_NAMESPACE,
    )

    assert _CALLER_OWNABLE_KEYS == CALLER_OWNABLE_KEYS
    assert _CALLER_OWNED_KEY == CALLER_OWNED_KEY
    assert _SYSTEM_NAMESPACE == CORE_NAMESPACE


async def test_a_key_the_caller_never_patched_is_still_filled(client, _engine):
    """The boundary must not degrade into "never write summary". A caller who
    PATCHes only ``tags`` leaves ``summary`` to the platform, and the legacy
    top-level mirror is still how a C25-unaware reader sees it."""
    memory_id, tenant_id, extra = await _write_then_patch(client, {"tags": ["mine"]})

    await _run_deferred_enrichment(memory_id, tenant_id, extra["content"])

    stored = await _stored_metadata(_engine, memory_id)
    assert stored.get("summary") == PLATFORM_SUMMARY, (
        "over-correction: the platform must still fill a key the caller never "
        f"claimed: {stored!r}"
    )
