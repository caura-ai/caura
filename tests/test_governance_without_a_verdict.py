"""A fast-mode row an LLM governance policy could not judge is marked and retried.

In fast mode the LLM half of governance (free-form PII, business or personal)
runs after enrichment, from the verdict enrichment wrote. When every provider
fails, enrichment falls back to the keyword heuristic, which gives no verdict,
and the row was left looking as if the model had judged it clean, with nothing
to find or retry it.

Remediation is now told whether a model judged the text. Without a verdict,
under an active LLM policy, the row is marked ``governance_llm_uncertain`` (as
strong mode marks it), one governance audit row records it, and a
``background_task_log`` row lets the hourly sweep enrich it again. A later
verdict clears the mark. Each fast-mode path says which it was: core-worker's
ENRICHED event, the inline enrichment task and the inline bulk write.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID

import pytest
from fastapi import HTTPException
from sqlalchemy import text

from common.enrichment.schema import EnrichmentResult
from common.events.base import Event
from common.events.topics import Topics
from core_api import consumer
from core_api.services import governance_remediation, memory_service
from core_api.services.governance_remediation import RemediationOutcome
from core_api.services.organization_settings import ResolvedConfig
from tests.conftest import new_tenant_id
from tests.test_governance_bulk_inline_remediation import _enrichment, _write_one

pytestmark = pytest.mark.asyncio

_MARK = "governance_llm_uncertain"
_MARKED = {_MARK: True, "_system": {_MARK: True}}
_CLEARED = {_MARK: None, "_system": {_MARK: None}}


def _cfg(*, provider: str = "openai", **governance) -> ResolvedConfig:
    return ResolvedConfig(
        {
            "enrichment": {"enabled": True, "provider": provider},
            "entity_extraction": {"enabled": False},
            "governance": governance,
        }
    )


_PII_DROP = {"pii": {"enabled": True, "action": "drop"}}
_ID = str(uuid.uuid4())


# ── Remediation: what a run without a verdict leaves ──


@pytest.fixture
def emitted(monkeypatch) -> list[dict]:
    calls: list[dict] = []

    async def _record(**kwargs):
        calls.append(kwargs)

    monkeypatch.setattr(governance_remediation, "emit_governance_audit", _record)
    return calls


@pytest.fixture
def logged(monkeypatch) -> list[tuple]:
    rows: list[tuple] = []

    async def _record(task_name, memory_id, tenant_id, exc, **_kw):
        rows.append((task_name, str(memory_id), tenant_id))

    monkeypatch.setattr(
        governance_remediation, "record_task_failure", _record, raising=False
    )
    return rows


@pytest.fixture
def patches(monkeypatch) -> list[tuple]:
    """Storage stand-in; ``patches.row_exists`` answers the PATCH as storage
    would for a live row or a gone one."""

    class _Patches(list):
        row_exists = True

    sent = _Patches()

    class _SC:
        async def update_memory(self, mid, tenant_id, patch):
            sent.append((mid, tenant_id, patch))
            return {"ok": True} if sent.row_exists else None

    monkeypatch.setattr(governance_remediation, "get_storage_client", lambda: _SC())
    return sent


def _mem(**metadata) -> dict:
    return {
        "id": _ID,
        "tenant_id": "t1",
        "agent_id": "a1",
        "content": "free-form detail about a colleague",
        "metadata": metadata,
    }


async def test_an_unjudged_row_is_marked_audited_and_logged(emitted, logged, patches):
    # What the heuristic leaves: the schema's default relevance, no PII verdict.
    mem = _mem(business_relevance="business")

    outcome = await governance_remediation.remediate_after_enrichment(
        mem, _cfg(**_PII_DROP), llm_verdict=False
    )

    assert outcome.dropped is False
    assert patches == [(_ID, "t1", {"metadata_patch": _MARKED})]
    [audit] = emitted
    assert audit["action"] == _MARK
    assert audit["resource_id"] == _ID
    assert "colleague" not in str(audit["detail"])
    assert logged == [(_MARK, _ID, "t1")]


async def test_without_an_llm_policy_nothing_is_recorded(emitted, logged, patches):
    await governance_remediation.remediate_after_enrichment(
        _mem(), _cfg(), llm_verdict=False
    )

    assert (patches, emitted, logged) == ([], [], [])


async def test_a_provider_that_cannot_judge_is_marked_but_not_retried(
    emitted, logged, patches
):
    """``fake`` never gives a verdict, so another run would not either."""
    await governance_remediation.remediate_after_enrichment(
        _mem(), _cfg(provider="fake", **_PII_DROP), llm_verdict=False
    )

    assert patches == [(_ID, "t1", {"metadata_patch": _MARKED})]
    assert [a["action"] for a in emitted] == [_MARK]
    assert logged == []


async def test_a_row_gone_before_the_mark_is_not_audited(emitted, logged, patches):
    patches.row_exists = False

    await governance_remediation.remediate_after_enrichment(
        _mem(), _cfg(**_PII_DROP), llm_verdict=False
    )

    assert (emitted, logged) == ([], [])


async def test_a_verdict_clears_the_mark(emitted, logged, patches):
    mem = _mem(business_relevance="business", **_MARKED)

    await governance_remediation.remediate_after_enrichment(
        mem, _cfg(**_PII_DROP), llm_verdict=True
    )

    assert patches == [(_ID, "t1", {"metadata_patch": _CLEARED})]
    assert (emitted, logged) == ([], [])


async def test_a_verdict_on_an_unmarked_row_writes_no_mark(emitted, logged, patches):
    await governance_remediation.remediate_after_enrichment(
        _mem(business_relevance="business"), _cfg(**_PII_DROP), llm_verdict=True
    )

    assert (patches, emitted, logged) == ([], [], [])


# ── Each fast-mode path says whether a model judged the text ──


@pytest.mark.parametrize(
    ("extra", "verdict"),
    [({"llm_verdict": False}, False), ({"llm_verdict": True}, True), ({}, None)],
    ids=["heuristic", "model", "older-worker"],
)
async def test_the_enriched_event_tells_remediation(monkeypatch, extra, verdict):
    content = "some enriched memory content"
    sc = MagicMock()
    sc.get_memory = AsyncMock(
        return_value={
            "id": _ID,
            "tenant_id": "t1",
            "content": content,
            "metadata_": {},
            "embedding": None,
        }
    )
    remediate = AsyncMock(return_value=RemediationOutcome())
    monkeypatch.setattr(consumer, "get_storage_client", lambda: sc)
    monkeypatch.setattr(consumer, "resolve_config", AsyncMock(return_value=_cfg()))
    monkeypatch.setattr(consumer, "remediate_after_enrichment", remediate)
    monkeypatch.setattr(consumer, "_fan_out_persisted_atomic_facts", AsyncMock())

    await consumer.handle_memory_enriched(
        Event(
            event_type=Topics.Memory.ENRICHED,
            tenant_id="t1",
            payload={"memory_id": _ID, "tenant_id": "t1", "content": content, **extra},
        )
    )

    assert remediate.await_args.kwargs.get("llm_verdict") is verdict


@pytest.mark.parametrize(("llm_ms", "verdict"), [(0, False), (7, True)])
async def test_the_inline_enrichment_tells_remediation(llm_ms, verdict):
    row = {
        "id": _ID,
        "memory_type": "fact",
        "weight": 0.5,
        "ts_valid_start": None,
        "ts_valid_end": None,
        "metadata_": {},
        "deleted_at": None,
        "content": "body",
    }
    sc = AsyncMock()
    sc.get_memory = AsyncMock(return_value=row)
    sc.update_memory = AsyncMock(return_value={"ok": True})
    remediate = AsyncMock(return_value=RemediationOutcome())
    cfg = SimpleNamespace(
        enrichment_enabled=True,
        enrichment_provider="openai",
        entity_extraction_enabled=False,
    )

    with (
        patch.object(memory_service, "get_storage_client", lambda: sc),
        patch(
            "core_api.services.memory_enrichment.enrich_memory",
            new=AsyncMock(return_value=EnrichmentResult(llm_ms=llm_ms)),
        ),
        patch(
            "core_api.services.organization_settings.resolve_config",
            new=AsyncMock(return_value=cfg),
        ),
        patch(
            "core_api.services.governance_remediation.remediate_after_enrichment",
            new=remediate,
        ),
    ):
        await memory_service._enrich_memory_background(
            UUID(_ID),
            "body",
            "t1",
            None,
            "a1",
            governance_config=cfg,
            run_governance_remediation=True,
        )

    assert remediate.await_args.kwargs.get("llm_verdict") is verdict


async def _values(conn, sql: str, memory_id) -> list:
    return (await conn.execute(text(sql), {"i": memory_id})).scalars().all()


async def test_an_unjudged_bulk_row_is_marked_audited_and_logged(monkeypatch, _engine):
    """The inline bulk path end to end, on the real database: the row stays
    live, carries the mark in both homes, and has its audit and task rows."""
    memory_id = await _write_one(
        monkeypatch, cfg=_cfg(**_PII_DROP), enrichment=_enrichment(llm_ms=0)
    )

    async with _engine.connect() as conn:
        deleted_at, metadata = (
            await conn.execute(
                text("SELECT deleted_at, metadata FROM memories WHERE id = :i"),
                {"i": memory_id},
            )
        ).one()
        actions = await _values(
            conn, "SELECT action FROM audit_log WHERE resource_id = :i", memory_id
        )
        tasks = await _values(
            conn,
            "SELECT task_name FROM background_task_log WHERE memory_id = :i",
            memory_id,
        )

    assert deleted_at is None
    assert metadata[_MARK] is True and metadata["_system"][_MARK] is True
    assert _MARK in actions
    assert tasks.count(_MARK) == 1


# ── The sweep enriches marked rows again ──


_SINCE = datetime.now(UTC) - timedelta(days=1)


async def _marked_memory(sc, tenant: str, content: str, *, marked: bool = True) -> str:
    row = await sc.create_memory(
        {
            "tenant_id": tenant,
            "agent_id": "rerun-agent",
            "memory_type": "fact",
            "content": content,
            "status": "active",
            "visibility": "scope_team",
            "metadata_": dict(_MARKED) if marked else {},
        }
    )
    return str(row["id"])


async def _unjudged(sc, tenant: str, memory_id: str) -> None:
    await sc.add_task_failure(
        {
            "task_name": _MARK,
            "memory_id": memory_id,
            "tenant_id": tenant,
            "error_message": "no verdict, for the test",
            "error_traceback": "",
            "status": "failed",
        }
    )


async def _open(sc, tenant: str) -> list[dict]:
    return await sc.list_open_task_failures(tenant, [_MARK], since=_SINCE, limit=100)


async def _sweep(sc, monkeypatch, tenant: str) -> tuple[dict, AsyncMock]:
    """Run one sweep over ``tenant`` and the re-runs it scheduled."""
    from core_api.services import enrichment_rerun

    scheduled: list = []
    enrich = AsyncMock()
    monkeypatch.setattr(sc, "list_active_tenants", AsyncMock(return_value=[tenant]))
    monkeypatch.setattr(enrichment_rerun, "_schedule_enrich_or_inline", enrich)
    monkeypatch.setattr(enrichment_rerun, "track_task", scheduled.append)

    counts = await enrichment_rerun.rerun_unjudged_enrichments()
    for coro in scheduled:
        await coro
    return counts, enrich


async def test_the_sweep_enriches_a_marked_row_again(sc, monkeypatch):
    tenant = new_tenant_id()
    await sc.update_org_settings(
        tenant,
        {
            "governance": _PII_DROP,
            "enrichment": {"enabled": True, "provider": "openai"},
        },
    )
    memory = await _marked_memory(sc, tenant, "Notes from the planning review.")
    gone = await _marked_memory(sc, tenant, "A memory deleted since.")
    assert await sc.soft_delete_memory(gone, tenant)
    judged = await _marked_memory(sc, tenant, "Judged since.", marked=False)
    for memory_id in (memory, gone, judged):
        await _unjudged(sc, tenant, memory_id)

    counts, enrich = await _sweep(sc, monkeypatch, tenant)

    assert counts == {
        "tenants": 1,
        "unreadable_tenants": 0,
        "memories": 3,
        "scheduled": 1,
        "skipped": 2,
        "failed_memories": 0,
    }
    enrich.assert_awaited_once()
    args, kwargs = enrich.await_args
    assert args[:5] == (
        UUID(memory),
        "Notes from the planning review.",
        tenant,
        None,
        "rerun-agent",
    )
    assert kwargs["run_governance_remediation"] is True
    # The fields the writer may have set are kept, as on a release.
    assert "memory_type" in kwargs["agent_provided_fields"]
    assert await _open(sc, tenant) == []


async def test_the_sweep_skips_a_tenant_whose_policy_is_off(sc, monkeypatch):
    tenant = new_tenant_id()
    memory = await _marked_memory(sc, tenant, "Notes from the planning review.")
    await _unjudged(sc, tenant, memory)

    counts, enrich = await _sweep(sc, monkeypatch, tenant)

    assert (counts["scheduled"], counts["skipped"]) == (0, 1)
    enrich.assert_not_awaited()


# ── The route core-operations calls ──


@pytest.mark.unit
async def test_the_sweep_route_is_admin_only():
    from core_api.routes import lifecycle

    auth = MagicMock()
    auth.enforce_admin.side_effect = HTTPException(status_code=403, detail="admin only")

    with pytest.raises(HTTPException) as caught:
        await lifecycle.rerun_unjudged_enrichments(auth=auth)

    assert caught.value.status_code == 403


@pytest.mark.unit
def test_the_sweep_route_resolves_to_its_handler():
    from starlette.routing import Match

    from core_api.routes import lifecycle

    scope = {
        "type": "http",
        "method": "POST",
        "path": "/admin/enrichment/rerun-unjudged",
        "headers": [],
        "root_path": "",
    }
    [route] = [r for r in lifecycle.router.routes if r.matches(scope)[0] == Match.FULL]
    assert route.endpoint is lifecycle.rerun_unjudged_enrichments
