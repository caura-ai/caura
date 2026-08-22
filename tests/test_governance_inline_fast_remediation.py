"""H-18 — the LLM governance verdict must be applied on the INLINE fast path.

Fast write mode always defers enrichment, so ``GovernanceDecision`` cannot run in
its pipeline; the fast path is governed by post-write remediation instead. That
remediation lived only in ``consumer.py``, which runs when the WORKER PATCHes
enrichment back. On an inline deployment — the DEFAULT — there is no publish and
no consumer, so PII-drop / non-business-drop / keep_private were computed by the
inline enrichment call and then silently discarded.

Two distinct defects had to be fixed for the inline path to enforce policy, and
the tests below are split accordingly:

1. **The verdict was never consulted.** ``_schedule_enrich_or_inline`` did not
   call ``remediate_after_enrichment`` at all.
2. **``business_relevance`` was never persisted.** ``_enrich_memory_background``
   wrote ``contains_pii``/``pii_types`` into metadata but not
   ``business_relevance`` — which is the key the non-business branch reads. So
   even once (1) was fixed, non-business DROP and KEEP_PRIVATE remained dead on
   inline deployments; only the PII branch worked.

The end-to-end tests drive the REAL ``_enrich_memory_background`` and the REAL
``remediate_after_enrichment`` against a fake storage client, so they fail on the
pre-fix code for the right reason rather than on a signature mismatch.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from core_api.services import memory_service
from core_api.services.organization_settings import ResolvedConfig

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]

TENANT = "t-h18"


def _enrichment(**over):
    """A full ``EnrichmentResult``-shaped double, benign unless overridden."""
    base = dict(
        memory_type="fact",
        weight=0.5,
        status="active",
        title="",
        summary="",
        tags=[],
        llm_ms=12,
        contains_pii=False,
        pii_types=[],
        business_relevance="business",
        retrieval_hint="",
        ts_valid_start=None,
        ts_valid_end=None,
        atomic_facts=None,
    )
    base.update(over)
    return SimpleNamespace(**base)


def _gov_cfg(*, pii=False, pii_action="drop", nb=False, nb_disposition="drop"):
    """A REAL ``ResolvedConfig``, not a stand-in.

    One object serves both consumers, as in production: the same
    ``resolve_config(tenant_id)`` result reaches ``_enrich_memory_background``
    for the enrichment flags and ``remediate_after_enrichment`` for the
    governance ones. Built from the raw settings dict so the accessors' own
    defaulting (``action or "flag"``, ``disposition or "store"``) is exercised
    rather than bypassed — a ``SimpleNamespace`` here can express governance
    states the real resolver never produces.
    """
    return ResolvedConfig(
        {
            "enrichment": {"enabled": True, "provider": "fake"},
            "entity_extraction": {"enabled": False},
            "governance": {
                "pii": {"enabled": pii, "action": pii_action},
                "non_business": {"enabled": nb, "disposition": nb_disposition},
            },
        }
    )


def _row():
    return {
        "id": str(uuid.uuid4()),
        "memory_type": "fact",
        "status": "active",
        "weight": 0.5,
        "ts_valid_start": None,
        "ts_valid_end": None,
        "metadata_": {},
        "deleted_at": None,
        "content": "body",
    }


def _storage():
    sc = AsyncMock(name="storage_client")
    sc.get_memory = AsyncMock(return_value=_row())
    sc.update_memory = AsyncMock(return_value=None)
    sc.update_memory_status = AsyncMock(return_value=None)
    sc.soft_delete_memory = AsyncMock(return_value=True)
    return sc


async def _run_inline(
    *,
    enrichment,
    gov_cfg,
    storage,
    remediate=True,
    inline=True,
    memory_id=None,
):
    """Drive ``_schedule_enrich_or_inline`` with the real enrichment + governance.

    Only the LLM call, the storage client and the audit sink are faked, so the
    ``business_relevance`` persistence and the remediation wiring are both
    exercised for real.
    """
    # ``inline_enrichment`` is a derived PROPERTY on Settings (no setter), so it
    # is patched on the class rather than the instance.
    with (
        patch.object(
            type(memory_service.settings),
            "inline_enrichment",
            property(lambda self: inline),
        ),
        patch.object(memory_service, "get_storage_client", lambda: storage),
        patch.object(memory_service, "track_task", MagicMock()),
        patch.object(
            memory_service, "publish_memory_enrich_request", AsyncMock(return_value=None)
        ),
        patch(
            "core_api.services.memory_enrichment.enrich_memory",
            new=AsyncMock(return_value=enrichment),
        ),
        patch(
            "core_api.services.organization_settings.resolve_config",
            new=AsyncMock(return_value=gov_cfg),
        ),
        patch(
            "core_api.services.governance_remediation.get_storage_client",
            lambda: storage,
        ),
        # ``governance_remediation`` binds this by name at import, so the patch
        # has to land on ITS module attribute, not on ``governance_gate``'s.
        patch(
            "core_api.services.governance_remediation.emit_governance_audit",
            new=AsyncMock(return_value=None),
        ),
    ):
        await memory_service._schedule_enrich_or_inline(
            memory_id or uuid.uuid4(),
            "body",
            TENANT,
            "f1",
            "a-1",
            gov_cfg,
            run_governance_remediation=remediate,
        )


def _patched_metadata(storage):
    """The ``metadata_`` dict the enrichment PATCH actually sent to storage."""
    for call in storage.update_memory.await_args_list:
        for arg in list(call.args) + list(call.kwargs.values()):
            if isinstance(arg, dict) and "metadata_" in arg:
                return arg["metadata_"]
    return None


# ── Defect 2: business_relevance must reach the row ───────────────────────────


async def test_inline_enrichment_persists_business_relevance():
    """Fails pre-fix: the inline path never wrote this key at all.

    Without it the non-business branch of ``remediate_after_enrichment`` — which
    reads ``md["business_relevance"]`` — can never fire on an inline deployment,
    and an inline row disagrees with the deferred path's row for the same input.
    """
    sc = _storage()
    await _run_inline(
        enrichment=_enrichment(business_relevance="personal"),
        gov_cfg=_gov_cfg(),
        storage=sc,
    )
    meta = _patched_metadata(sc)
    assert meta is not None, "enrichment did not PATCH metadata at all"
    assert meta.get("business_relevance") == "personal", (
        "inline enrichment dropped business_relevance; the non-business "
        f"governance branch cannot fire without it (got {meta!r})"
    )


# ── Defect 1 + 2 together: the policy actually runs ───────────────────────────


async def test_inline_fast_write_drops_a_personal_memory_when_configured():
    """End-to-end: nb=drop + a 'personal' verdict must soft-delete the row.

    This is the behaviour H-18 says was silently skipped. It needs BOTH fixes —
    the remediation call AND the persisted ``business_relevance``.
    """
    sc = _storage()
    mid = uuid.uuid4()
    await _run_inline(
        enrichment=_enrichment(business_relevance="personal"),
        gov_cfg=_gov_cfg(nb=True, nb_disposition="drop"),
        storage=sc,
        memory_id=mid,
    )
    sc.soft_delete_memory.assert_awaited_once_with(str(mid))


async def test_inline_fast_write_drops_a_pii_memory_when_configured():
    """The PII half of the same gap: pii=drop + ``contains_pii`` must delete."""
    sc = _storage()
    mid = uuid.uuid4()
    await _run_inline(
        enrichment=_enrichment(contains_pii=True, pii_types=["email"]),
        gov_cfg=_gov_cfg(pii=True, pii_action="drop"),
        storage=sc,
        memory_id=mid,
    )
    sc.soft_delete_memory.assert_awaited_once_with(str(mid))


async def test_keep_private_downgrades_visibility_instead_of_dropping():
    """nb=keep_private must set ``scope_agent`` and leave the row alive."""
    sc = _storage()
    mid = uuid.uuid4()
    await _run_inline(
        enrichment=_enrichment(business_relevance="personal"),
        gov_cfg=_gov_cfg(nb=True, nb_disposition="keep_private"),
        storage=sc,
        memory_id=mid,
    )
    sc.soft_delete_memory.assert_not_awaited()
    assert any(
        call.args[0] == str(mid) and call.args[2] == {"visibility": "scope_agent"}
        for call in sc.update_memory.await_args_list
        if len(call.args) > 2
    ), f"keep_private did not downgrade visibility: {sc.update_memory.await_args_list}"


async def test_clean_content_is_left_alone():
    """Governance on, verdict clean — the row must survive untouched."""
    sc = _storage()
    await _run_inline(
        enrichment=_enrichment(),
        gov_cfg=_gov_cfg(pii=True, nb=True),
        storage=sc,
    )
    sc.soft_delete_memory.assert_not_awaited()


# ── Cost + correctness of how the verdict reaches remediation ─────────────────


async def test_remediation_costs_no_extra_storage_read():
    """The verdict comes from the enrichment call's own frame, not a re-read.

    Fails pre-fix (the first version of this fix re-fetched the row): a second
    ``get_memory`` per write is an HTTP round-trip on the hot background path
    for every tenant, including the default ones with governance switched off.
    """
    sc = _storage()
    await _run_inline(
        enrichment=_enrichment(business_relevance="personal"),
        gov_cfg=_gov_cfg(nb=True, nb_disposition="drop"),
        storage=sc,
    )
    assert sc.get_memory.await_count == 1, (
        "governance remediation added a storage GET; the enrichment call "
        f"already holds the row ({sc.get_memory.await_count} reads)"
    )


async def test_a_stale_read_replica_cannot_defeat_the_verdict():
    """``get_memory`` routes to the READ REPLICA when a read split is configured.

    Re-reading the row to find the verdict can therefore return the
    pre-enrichment copy and silently no-op — on exactly the deployments big
    enough to run a replica. Here every ``get_memory`` after the first returns a
    stale row with no enrichment fields; the drop must still happen.
    """
    sc = _storage()
    fresh = _row()
    stale = {**fresh, "metadata_": {}}
    sc.get_memory = AsyncMock(side_effect=[fresh, stale, stale, stale])

    mid = uuid.uuid4()
    await _run_inline(
        enrichment=_enrichment(business_relevance="personal"),
        gov_cfg=_gov_cfg(nb=True, nb_disposition="drop"),
        storage=sc,
        memory_id=mid,
    )
    sc.soft_delete_memory.assert_awaited_once_with(str(mid))


# ── Wiring: which callers may remediate ───────────────────────────────────────


async def test_strong_mode_is_not_double_remediated():
    """Strong mode enforces the same policy synchronously via GovernanceDecision.

    Running remediation for it too would duplicate audit rows and re-attempt a
    drop on a row policy already acted on. The flag defaults off for that reason.
    """
    sc = _storage()
    await _run_inline(
        enrichment=_enrichment(business_relevance="personal"),
        gov_cfg=_gov_cfg(nb=True, nb_disposition="drop"),
        storage=sc,
        remediate=False,
    )
    sc.soft_delete_memory.assert_not_awaited()


async def test_deferred_deployment_does_not_remediate_here():
    """With enrichment deferred, the worker's consumer owns remediation.

    This path publishes instead of enriching inline, so remediating here would
    be acting on a row whose LLM signal has not landed yet.
    """
    sc = _storage()
    await _run_inline(
        enrichment=_enrichment(business_relevance="personal"),
        gov_cfg=_gov_cfg(nb=True, nb_disposition="drop"),
        storage=sc,
        inline=False,
    )
    sc.soft_delete_memory.assert_not_awaited()


async def test_no_verdict_means_no_remediation():
    """Enrichment that produced nothing must not trigger a policy decision.

    ``_enrich_memory_background`` returns ``None`` when the LLM call yielded no
    result, and a ``None`` verdict must not be read as "clean" OR as grounds to
    act — there is simply nothing to enforce.
    """
    sc = _storage()
    await _run_inline(
        enrichment=None,
        gov_cfg=_gov_cfg(pii=True, nb=True),
        storage=sc,
    )
    sc.soft_delete_memory.assert_not_awaited()


async def test_remediation_failure_surfaces_to_the_task_tracker():
    """A governance failure must propagate, not be swallowed into a log line.

    The call is the last statement of the background task — extraction and Path
    A are scheduled as their own ``track_task`` calls, so nothing can cascade —
    and the enclosing ``tracked_task`` turns the exception into a
    ``BackgroundTaskLog`` row. An unenforced governance policy is exactly the
    kind of failure that must not be silent.
    """
    sc = _storage()
    with (
        patch(
            "core_api.services.governance_remediation.remediate_after_enrichment",
            new=AsyncMock(side_effect=RuntimeError("boom")),
        ),
        pytest.raises(RuntimeError, match="boom"),
    ):
        await _run_inline(
            enrichment=_enrichment(),
            gov_cfg=_gov_cfg(pii=True),
            storage=sc,
        )
