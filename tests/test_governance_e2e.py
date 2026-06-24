"""End-to-end use-case tests for the two ingestion-boundary governance gates (eToro).

Unlike the step-level tests in ``test_governance_gate.py`` (which execute a single
pipeline step against a hand-built context), these drive a REAL memory write through
``create_memory`` — the whole fast/strong/STM pipeline including ``LoadTenantConfig``,
the governance steps, ``ComputeContentHash`` and ``WriteMemoryRow`` — against the
integration Postgres DB, then inspect the stored row and the tamper-evident audit log.

This closes the gap the step tests document (the original HTTP E2E was dropped as
flaky because the settings cache didn't propagate across the test/app process
boundary). We avoid that here by driving the pipeline IN-PROCESS: governance config
is seeded via ``update_settings`` and the per-process ``_settings_cache`` is cleared
in an autouse fixture, so the config the test seeds is exactly the one the pipeline
reads — no cross-process divergence, no TTL race.

The deterministic PII/PCI/secret gate (``GovernanceScanContent``) needs no LLM and is
exercised through real writes here. The LLM-signal gate (``GovernanceDecision``, strong
mode) and the business-vs-personal gate are driven by injecting a deterministic fake
enrichment (the ``fake`` providers used in tests produce ``llm_ms==0`` → the fail-closed
branch, so a real signal must be supplied).
"""

from __future__ import annotations

import uuid

import pytest
from fastapi import HTTPException
from sqlalchemy import text

import core_api.services.memory_service as memory_service
import core_api.services.organization_settings as ts_svc
from common.enrichment.schema import EnrichmentResult
from core_api.clients.storage_client import get_storage_client
from core_api.schemas import BulkMemoryCreate, BulkMemoryItem, MemoryCreate, MemoryOut
from core_api.services.memory_service import create_memories_bulk, create_memory
from core_api.services.organization_settings import invalidate_cache, update_settings
from core_storage_api.services.postgres_service import PostgresService, get_session

pytestmark = pytest.mark.asyncio

# Content long enough to clear CheckContentLength's minimum-length quality gate.
_PADDING = (
    " This memory carries enough surrounding context to pass the content-length gate."
)


@pytest.fixture(autouse=True)
def _use_pipeline_write():
    """These tests assert pipeline behavior; pin the pipeline write path on."""
    original = memory_service._USE_PIPELINE_WRITE
    memory_service._USE_PIPELINE_WRITE = True
    yield
    memory_service._USE_PIPELINE_WRITE = original


@pytest.fixture(autouse=True)
def _reset_settings_cache():
    """The settings cache is per-process; clear it around each test so a seeded
    governance config is read from the DB rather than a stale/empty cache entry."""
    ts_svc._settings_cache.clear()
    yield
    ts_svc._settings_cache.clear()


def _tenant() -> str:
    # `test-tenant-%` rows are auto-cleaned by the conftest schema fixture.
    return f"test-tenant-gov-{uuid.uuid4().hex[:8]}"


def _make_input(tenant_id: str, content: str, **kwargs) -> MemoryCreate:
    return MemoryCreate(
        tenant_id=tenant_id,
        fleet_id="test-fleet",
        agent_id="test-agent",
        content=content,
        persist=True,
        entity_links=[],
        **kwargs,
    )


async def _seed_governance(
    db,
    tenant_id: str,
    *,
    pii: dict | None = None,
    non_business: dict | None = None,
) -> None:
    """Persist a tenant's governance config the way the real PUT path does
    (``update_settings`` validates the enums + commits), then drop the cache."""
    gov: dict = {}
    if pii is not None:
        gov["pii"] = pii
    if non_business is not None:
        gov["non_business"] = non_business
    await update_settings(tenant_id, {"governance": gov})
    invalidate_cache(tenant_id)


def _inject_enrichment(
    monkeypatch,
    *,
    contains_pii: bool = False,
    pii_types: list[str] | None = None,
    business_relevance: str = "business",
) -> None:
    """Force ``GovernanceDecision`` (strong mode) to see a concrete LLM signal.

    Wraps ``ParallelEmbedEnrich.execute`` so the real step still runs (embedding
    is produced) and then overwrites ``ctx.data["enrichment"]`` with a fixed
    ``EnrichmentResult`` (``llm_ms=5`` so the gate doesn't fail-closed). This is
    independent of the tenant's enrichment provider/enabled config — CI sets
    ENTITY_EXTRACTION_PROVIDER=none, which would otherwise gate enrichment off
    and leave the signal absent, so monkeypatching ``enrich_memory`` alone
    wouldn't fire."""
    from core_api.pipeline.steps.write.parallel_embed_enrich import ParallelEmbedEnrich

    fake = EnrichmentResult(
        contains_pii=contains_pii,
        pii_types=pii_types or [],
        business_relevance=business_relevance,
        llm_ms=5,
    )
    original = ParallelEmbedEnrich.execute

    async def _patched(self, ctx):
        result = await original(self, ctx)
        ctx.data["enrichment"] = fake
        return result

    monkeypatch.setattr(ParallelEmbedEnrich, "execute", _patched)


async def _governance_audit_rows(tenant_id: str) -> list[dict]:
    """Read governance audit rows (pii_*/nonbusiness_*) back from the DB. Audit
    writes commit through the in-process storage bridge, so they're readable here."""
    async with get_session() as s:
        rows = (
            await s.execute(
                text(
                    "SELECT action, detail, resource_id FROM audit_log "
                    "WHERE tenant_id=:t AND (action LIKE 'pii_%' OR action LIKE 'nonbusiness_%') "
                    "ORDER BY seq"
                ),
                {"t": tenant_id},
            )
        ).all()
    return [{"action": r[0], "detail": r[1], "resource_id": r[2]} for r in rows]


async def _assert_chain_valid(tenant_id: str) -> None:
    res = await PostgresService().audit_verify_chain(tenant_id)
    assert res["valid"] is True, res


# ── PII deterministic gate (GovernanceScanContent) through a real write ──────


async def test_pii_mask_stores_redacted_content_and_audits(db):
    tenant = _tenant()
    await _seed_governance(db, tenant, pii={"enabled": True, "action": "mask"})
    content = f"Reach me at john.doe@example.com or card 4111 1111 1111 1111.{_PADDING}"
    result = await create_memory(_make_input(tenant, content))

    assert isinstance(result, MemoryOut)
    # The stored/returned content is the masked form — proves the scan mutated
    # content BEFORE ComputeContentHash/WriteMemoryRow (mask-vs-hash ordering).
    assert "john.doe@example.com" not in result.content
    assert "4111 1111 1111 1111" not in result.content
    assert "«EMAIL»" in result.content and "«CARD»" in result.content

    rows = await _governance_audit_rows(tenant)
    assert any(r["action"] == "pii_mask" for r in rows), rows
    mask = next(r for r in rows if r["action"] == "pii_mask")
    assert mask["detail"]["write_mode"] == "fast"  # default mode
    assert "email" in mask["detail"]["categories"]
    # PII-safe: never the raw values.
    assert "john.doe@example.com" not in str(mask["detail"])
    await _assert_chain_valid(tenant)


async def test_pii_drop_rejects_write_and_persists_nothing(db):
    tenant = _tenant()
    await _seed_governance(db, tenant, pii={"enabled": True, "action": "drop"})
    content = f"My card is 4111 1111 1111 1111, please remember it.{_PADDING}"
    with pytest.raises(HTTPException) as exc:
        await create_memory(_make_input(tenant, content))
    assert exc.value.status_code == 422

    # The drop is audited BEFORE the reject; nothing is persisted.
    rows = await _governance_audit_rows(tenant)
    assert any(r["action"] == "pii_drop" for r in rows), rows
    async with get_session() as s:
        cnt = (
            await s.execute(
                text("SELECT count(*) FROM memories WHERE tenant_id=:t"), {"t": tenant}
            )
        ).scalar_one()
    assert cnt == 0
    await _assert_chain_valid(tenant)


async def test_pii_flag_keeps_content_and_marks_metadata(db):
    tenant = _tenant()
    await _seed_governance(db, tenant, pii={"enabled": True, "action": "flag"})
    content = f"Ping me on john.doe@example.com about the rollout.{_PADDING}"
    result = await create_memory(_make_input(tenant, content))

    assert "john.doe@example.com" in result.content  # flag does not redact
    assert result.metadata is not None
    assert result.metadata.get("contains_pii") is True
    assert "email" in (result.metadata.get("pii_types") or [])
    rows = await _governance_audit_rows(tenant)
    assert any(r["action"] == "pii_flag" for r in rows), rows


async def test_pii_category_toggle_limits_scope(db):
    tenant = _tenant()
    await _seed_governance(
        db,
        tenant,
        pii={"enabled": True, "action": "mask", "categories": {"email": True}},
    )
    content = f"Email john.doe@example.com, card 4111 1111 1111 1111.{_PADDING}"
    result = await create_memory(_make_input(tenant, content))
    assert "john.doe@example.com" not in result.content  # email masked
    assert "4111 1111 1111 1111" in result.content  # card not in scope


async def test_governance_disabled_is_a_noop(db):
    tenant = _tenant()
    # No governance seeded at all → gate skips, content untouched, no audit.
    content = f"Email john.doe@example.com, card 4111 1111 1111 1111.{_PADDING}"
    result = await create_memory(_make_input(tenant, content))
    assert "john.doe@example.com" in result.content
    assert "4111 1111 1111 1111" in result.content
    assert await _governance_audit_rows(tenant) == []


@pytest.mark.parametrize("write_mode", ["fast", "strong"])
async def test_pii_mask_runs_in_every_write_mode(db, write_mode):
    # The deterministic scan is wired into all write-mode compositions; a real
    # write in each LTM mode must redact and record the mode in the audit detail.
    # (STM is covered by test_governance_scan_wired_into_stm — driving it here
    # would require the global USE_STM flag, which the default test env leaves off.)
    tenant = _tenant()
    await _seed_governance(db, tenant, pii={"enabled": True, "action": "mask"})
    content = f"Contact john.doe@example.com for access.{_PADDING}"
    result = await create_memory(_make_input(tenant, content, write_mode=write_mode)
    )
    assert "john.doe@example.com" not in result.content
    rows = await _governance_audit_rows(tenant)
    mask = next((r for r in rows if r["action"] == "pii_mask"), None)
    assert mask is not None, rows
    assert mask["detail"]["write_mode"] == write_mode


# ── Business-vs-personal + LLM-signal PII gate (GovernanceDecision, strong) ──
#
# These drive a real strong-mode write with a deterministic injected enrichment.
# The content carries NO regex-detectable PII, so the deterministic scan is a
# no-op and only the LLM-signal gate acts — isolating that path.

_NEUTRAL = (
    "Let's sync on the quarterly planning thread and the roadmap review next week."
)


async def test_personal_content_kept_private(db, monkeypatch):
    tenant = _tenant()
    await _seed_governance(
        db,
        tenant,
        non_business={"enabled": True, "disposition": "keep_private"},
    )
    _inject_enrichment(monkeypatch, business_relevance="personal")
    result = await create_memory(_make_input(tenant, _NEUTRAL, write_mode="strong"))
    assert result.visibility == "scope_agent"  # retained, but agent-private
    rows = await _governance_audit_rows(tenant)
    assert any(r["action"] == "nonbusiness_keep_private" for r in rows), rows


async def test_personal_content_dropped(db, monkeypatch):
    tenant = _tenant()
    await _seed_governance(
        db,
        tenant,
        non_business={"enabled": True, "disposition": "drop"},
    )
    _inject_enrichment(monkeypatch, business_relevance="personal")
    with pytest.raises(HTTPException) as exc:
        await create_memory(_make_input(tenant, _NEUTRAL, write_mode="strong"))
    assert exc.value.status_code == 422
    rows = await _governance_audit_rows(tenant)
    assert any(r["action"] == "nonbusiness_drop" for r in rows), rows


async def test_business_content_flows_through(db, monkeypatch):
    tenant = _tenant()
    await _seed_governance(
        db,
        tenant,
        non_business={"enabled": True, "disposition": "drop"},
    )
    _inject_enrichment(monkeypatch, business_relevance="business")
    result = await create_memory(_make_input(tenant, _NEUTRAL, write_mode="strong"))
    assert isinstance(result, MemoryOut)
    assert result.visibility != "scope_agent"  # business content stored normally
    assert not any(
        r["action"].startswith("nonbusiness_")
        for r in await _governance_audit_rows(tenant)
    )


async def test_non_business_store_disposition_is_noop(db, monkeypatch):
    tenant = _tenant()
    await _seed_governance(
        db,
        tenant,
        non_business={"enabled": True, "disposition": "store"},
    )
    _inject_enrichment(monkeypatch, business_relevance="personal")
    result = await create_memory(_make_input(tenant, _NEUTRAL, write_mode="strong"))
    # store = no enforcement; classification is recorded, nothing dropped/hidden.
    assert result.visibility != "scope_agent"
    assert (result.metadata or {}).get("business_relevance") == "personal"


async def test_llm_pii_signal_drops(db, monkeypatch):
    tenant = _tenant()
    await _seed_governance(
        db,
        tenant,
        pii={"enabled": True, "action": "drop"},
    )
    # No regex-detectable PII in content; only the LLM signal flags it.
    _inject_enrichment(monkeypatch, contains_pii=True, pii_types=["health"])
    with pytest.raises(HTTPException) as exc:
        await create_memory(_make_input(tenant, _NEUTRAL, write_mode="strong"))
    assert exc.value.status_code == 422
    rows = await _governance_audit_rows(tenant)
    drop = next((r for r in rows if r["action"] == "pii_drop"), None)
    assert drop is not None and drop["detail"].get("source") == "llm", rows


async def test_both_gates_act_on_one_memory(db, monkeypatch):
    # A memory the LLM flags as BOTH PII-bearing and personal: PII flag (mask
    # config can't redact a free-form span) + non-business keep_private both fire.
    tenant = _tenant()
    await _seed_governance(
        db,
        tenant,
        pii={"enabled": True, "action": "flag"},
        non_business={"enabled": True, "disposition": "keep_private"},
    )
    _inject_enrichment(
        monkeypatch,
        contains_pii=True,
        pii_types=["health"],
        business_relevance="personal",
    )
    result = await create_memory(_make_input(tenant, _NEUTRAL, write_mode="strong"))
    assert (result.metadata or {}).get("contains_pii") is True
    assert result.visibility == "scope_agent"
    actions = {r["action"] for r in await _governance_audit_rows(tenant)}
    assert "pii_flag" in actions and "nonbusiness_keep_private" in actions, actions


# ── Bulk path deterministic gate (create_memories_bulk) ─────────────────────
#
# The bulk gate is a SEPARATE inline implementation from the pipeline step, so
# the step tests give it no coverage. Each item is scanned independently.


def _bulk(tenant: str, contents: list[str]) -> BulkMemoryCreate:
    return BulkMemoryCreate(
        tenant_id=tenant,
        fleet_id="test-fleet",
        agent_id="test-agent",
        items=[BulkMemoryItem(content=c) for c in contents],
    )


async def test_bulk_drop_rejects_only_the_pii_item(db):
    tenant = _tenant()
    await _seed_governance(db, tenant, pii={"enabled": True, "action": "drop"})
    clean = f"Quarterly planning notes for the team.{_PADDING}"
    dirty = f"Customer card 4111 1111 1111 1111 is on file.{_PADDING}"
    resp = await create_memories_bulk(_bulk(tenant, [clean, dirty]), bulk_attempt_id=uuid.uuid4().hex
    )
    assert resp.results[0].status == "created"
    assert resp.results[1].status == "error"
    assert "content policy" in (resp.results[1].error or "")
    rows = await _governance_audit_rows(tenant)
    drop = next((r for r in rows if r["action"] == "pii_drop"), None)
    assert drop is not None and drop["detail"]["write_mode"] == "bulk", rows


async def test_bulk_mask_redacts_stored_item(db):
    tenant = _tenant()
    await _seed_governance(db, tenant, pii={"enabled": True, "action": "mask"})
    dirty = f"Reach me at jane.roe@example.com about the deal.{_PADDING}"
    resp = await create_memories_bulk(_bulk(tenant, [dirty]), bulk_attempt_id=uuid.uuid4().hex
    )
    assert resp.results[0].status == "created"
    mem = await get_storage_client().get_memory(resp.results[0].id)
    assert "jane.roe@example.com" not in mem["content"]  # stored masked
    rows = await _governance_audit_rows(tenant)
    assert any(
        r["action"] == "pii_mask" and r["detail"]["write_mode"] == "bulk" for r in rows
    ), rows


async def test_bulk_flag_marks_stored_metadata(db):
    tenant = _tenant()
    await _seed_governance(db, tenant, pii={"enabled": True, "action": "flag"})
    dirty = f"Ping jane.roe@example.com for the rollout plan.{_PADDING}"
    resp = await create_memories_bulk(_bulk(tenant, [dirty]), bulk_attempt_id=uuid.uuid4().hex
    )
    assert resp.results[0].status == "created"
    mem = await get_storage_client().get_memory(resp.results[0].id)
    md = mem.get("metadata_") or mem.get("metadata") or {}
    assert md.get("contains_pii") is True
    assert "jane.roe@example.com" in mem["content"]  # flag does not redact
    rows = await _governance_audit_rows(tenant)
    assert any(
        r["action"] == "pii_flag" and r["detail"]["write_mode"] == "bulk" for r in rows
    ), rows
