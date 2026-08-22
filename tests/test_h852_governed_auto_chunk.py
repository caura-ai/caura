"""The auto-chunk branch must APPLY the LLM governance verdict, not just compute it.

``create_memory`` routes content over ``CHUNKING_THRESHOLD_CHARS`` to
``_handle_auto_chunk_from_ctx``. That branch runs ``build_enrichment_pipeline``,
which ends in ``MergeEnrichmentFields`` — so ``contains_pii`` / ``pii_types`` /
``business_relevance`` are all sitting in ``fields["metadata"]`` before the first
row is written. Nothing then acted on them: ``GovernanceDecision`` lives only in
``build_strong_write_pipeline``, and the post-write remediation is reached from
the enrichment and bulk paths, none of which this branch takes (#852).

The concrete losses, each pinned below:

* ``pii.action="drop"`` / ``non_business.disposition="drop"`` — the row and every
  chunk child persisted anyway;
* ``keep_private`` — the parent kept its original visibility, and so did the
  children cut out of it;
* ``flag`` / ``mask`` — the row was marked (``MergeEnrichmentFields`` does that
  much) but no governance audit row was ever written, so the enforcement action
  left no record;
* the children carried no verdict at all — ``metadata_`` was exactly
  ``{parent_memory_id, source}``, the same shape #808 fixed for atomic facts.

The deterministic gate is NOT part of this: ``GovernanceScanContent`` sits in
``build_enrichment_pipeline`` after ``LoadTenantConfig``, so regex/Luhn/entropy
PII was already masked or rejected here. What was lost is the free-form
judgement, the same thing H-18 (#806) was about on the other two entry points.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException

from common.enrichment.schema import EnrichmentResult
from core_api.pipeline.compositions import write as compositions
from core_api.pipeline.steps.write import GovernanceDecision
from core_api.schemas import MemoryCreate
from core_api.services import memory_service
from core_api.services.organization_settings import ResolvedConfig

pytestmark = [pytest.mark.unit]

TENANT = "t-h852"
FLEET = "f1"
AGENT = "a"


def _parent_row() -> dict:
    return {
        "id": str(uuid.uuid4()),
        "tenant_id": TENANT,
        "fleet_id": FLEET,
        "agent_id": AGENT,
        "memory_type": "fact",
        "title": "t",
        "content": "body",
        "weight": 0.5,
        "status": "active",
        "visibility": "scope_team",
        "recall_count": 0,
        "created_at": datetime(2026, 8, 21, tzinfo=UTC),
        "metadata_": {},
        "embedding": None,
        "deleted_at": None,
    }


def _config(*, pii: dict | None = None, non_business: dict | None = None) -> ResolvedConfig:
    """A real ``ResolvedConfig`` — the enum/default resolution is part of what a
    governance test should exercise, and a stand-in object drifts from it."""
    governance: dict = {}
    if pii is not None:
        governance["pii"] = pii
    if non_business is not None:
        governance["non_business"] = non_business
    return ResolvedConfig(
        {"governance": governance, "entity_extraction": {"enabled": False}}
    )


class _Run:
    """What one drive of the handler produced."""

    def __init__(self) -> None:
        self.parent: dict | None = None
        self.parent_id: str = ""
        self.children: list[dict] = []
        self.audits: list[str] = []
        self.chunked = False
        self.error: HTTPException | None = None


async def _run_auto_chunk(
    *,
    config: ResolvedConfig,
    chunks: tuple[str, ...] = ("chunk one", "chunk two"),
    enrichment: EnrichmentResult | None = None,
    audit_raises: bool = False,
) -> _Run:
    """Drive ``_handle_auto_chunk_from_ctx`` and capture what reached storage.

    ``enrichment=None`` means the LLM did not run at all (provider off / failed),
    which is also when ``MergeEnrichmentFields`` writes none of the three verdict
    keys — so the context is built to match rather than being set independently.

    Every ``EnrichmentResult`` a caller passes needs ``llm_ms`` set: the gate
    treats ``llm_ms == 0`` as "no live model judged this write" and takes no
    destructive action, so a test that left it at zero would pass against a
    completely unwired gate.
    """
    run = _Run()

    parent_row = _parent_row()
    run.parent_id = parent_row["id"]
    sc = AsyncMock(name="storage_client")
    sc.create_memory = AsyncMock(return_value=parent_row)
    sc.create_memories = AsyncMock(return_value=[])
    sc.bulk_find_by_content_hashes = AsyncMock(return_value={})

    data = MemoryCreate(
        tenant_id=TENANT,
        fleet_id=FLEET,
        agent_id=AGENT,
        content="a body long enough to be worth chunking " * 60,
    )
    ctx = SimpleNamespace(
        data={
            # ``GovernanceDecision`` reads the request off the context, exactly
            # as ``_create_memory_pipeline`` populates it.
            "input": data,
            "memory_fields": {
                "memory_type": "fact",
                "title": "t",
                "weight": 0.5,
                "status": "active",
                # What ``MergeEnrichmentFields`` leaves behind for this verdict.
                "metadata": _merged_metadata(enrichment),
            },
            "enrichment": enrichment,
            "embedding": [0.0],
            "t0": 0.0,
        },
        tenant_config=config,
    )

    async def _chunk_content(_content, _x, _cfg):
        run.chunked = True
        return [{"content": c, "suggested_type": "fact"} for c in chunks]

    async def _embeddings(texts, _cfg, background=False):
        return [[0.0] for _ in texts]

    async def _audit(**kwargs):
        if audit_raises:
            raise RuntimeError("audit backend unavailable")
        run.audits.append(kwargs["action"])

    with (
        patch.object(memory_service, "get_storage_client", lambda: sc),
        patch.object(memory_service, "track_task", MagicMock()),
        patch.object(memory_service, "get_embeddings_batch", new=_embeddings),
        patch("core_api.services.ingest_service._chunk_content", new=_chunk_content),
        # Patched where ``GovernanceDecision`` resolves it, not where it is
        # defined — the step imports the name at module level.
        patch(
            "core_api.pipeline.steps.write.governance_decision.emit_governance_audit",
            new=_audit,
        ),
    ):
        try:
            await memory_service._handle_auto_chunk_from_ctx(data, ctx)
        except HTTPException as exc:
            run.error = exc

    if sc.create_memory.await_args_list:
        run.parent = sc.create_memory.await_args_list[0].args[0]
    if sc.create_memories.await_args_list:
        run.children = sc.create_memories.await_args_list[0].args[0]
    return run


def _merged_metadata(enrichment: EnrichmentResult | None) -> dict:
    """The governance-relevant subset of what ``MergeEnrichmentFields`` writes.

    Reproduced rather than imported because the point of the fix is what happens
    to these three keys AFTER that step has run — the handler receives them
    already merged. Note that the step writes them for any enrichment result at
    all: they describe the content, and are recorded whether or not a governance
    policy is configured to act on them.
    """
    if enrichment is None:
        return {}
    metadata: dict = {"business_relevance": enrichment.business_relevance}
    if enrichment.contains_pii:
        metadata["contains_pii"] = True
        if enrichment.pii_types:
            metadata["pii_types"] = enrichment.pii_types
    return metadata


def _business() -> EnrichmentResult:
    return EnrichmentResult(llm_ms=5)


def _pii() -> EnrichmentResult:
    return EnrichmentResult(contains_pii=True, pii_types=["health"], llm_ms=5)


def _personal() -> EnrichmentResult:
    return EnrichmentResult(business_relevance="personal", llm_ms=5)


# ── drop: nothing may be written ─────────────────────────────────────────────


async def test_a_pii_drop_verdict_refuses_the_auto_chunk_write() -> None:
    run = await _run_auto_chunk(
        config=_config(pii={"enabled": True, "action": "drop"}), enrichment=_pii()
    )

    assert run.error is not None and run.error.status_code == 422
    assert run.parent is None, "the parent persisted despite a drop verdict"
    assert run.children == [], "chunk children persisted despite a drop verdict"
    assert "pii_drop" in run.audits


async def test_a_personal_drop_verdict_refuses_the_auto_chunk_write() -> None:
    run = await _run_auto_chunk(
        config=_config(non_business={"enabled": True, "disposition": "drop"}),
        enrichment=_personal(),
    )

    assert run.error is not None and run.error.status_code == 422
    assert run.parent is None
    assert run.children == []
    assert "nonbusiness_drop" in run.audits


async def test_a_drop_verdict_costs_no_chunking_call() -> None:
    """The gate is ahead of ``_chunk_content``, not merely ahead of the insert.

    Chunking is an LLM round-trip. Refusing the write after paying for it would
    still be correct, but the fail-safe ordering that makes the refusal reliable
    — decide before deriving anything — comes for free with the cheaper one.
    """
    run = await _run_auto_chunk(
        config=_config(pii={"enabled": True, "action": "drop"}), enrichment=_pii()
    )

    assert run.chunked is False


async def test_the_single_fact_fall_through_is_governed_too() -> None:
    """Chunking that yields 0-1 facts falls through to ``build_persist_pipeline``,
    which has no ``GovernanceDecision`` of its own. Placing the gate inside the
    ``len(facts) > 1`` branch would leave that sub-path exactly as it was."""
    run = await _run_auto_chunk(
        config=_config(pii={"enabled": True, "action": "drop"}),
        chunks=("just the one",),
        enrichment=_pii(),
    )

    # The audit is what makes this specific: a gate moved into the multi-fact
    # branch leaves a single-fact run with no governance action at all, which no
    # assertion about the raised status code alone would distinguish from the
    # fall-through failing for some unrelated reason.
    assert run.audits == ["pii_drop"]
    assert run.error is not None and run.error.status_code == 422
    assert run.parent is None


# ── keep_private: the downgrade must reach the children ──────────────────────


async def test_keep_private_downgrades_the_parent() -> None:
    run = await _run_auto_chunk(
        config=_config(non_business={"enabled": True, "disposition": "keep_private"}),
        enrichment=_personal(),
    )

    assert run.error is None
    assert run.parent is not None
    assert run.parent["visibility"] == "scope_agent"


async def test_keep_private_cascades_to_the_children() -> None:
    """The children are cut out of the parent's text. Leaving them at the
    original visibility re-publishes exactly the content the policy just made
    private — #808's finding on the other fan-out, and the same here."""
    run = await _run_auto_chunk(
        config=_config(non_business={"enabled": True, "disposition": "keep_private"}),
        enrichment=_personal(),
    )

    assert len(run.children) == 2
    assert {c["visibility"] for c in run.children} == {"scope_agent"}


# ── flag: the row is marked, the children inherit, the action is recorded ────


async def test_the_children_carry_the_parents_governance_verdict() -> None:
    run = await _run_auto_chunk(
        config=_config(pii={"enabled": True, "action": "flag"}), enrichment=_pii()
    )

    assert len(run.children) == 2
    for child in run.children:
        meta = child["metadata_"]
        assert meta["contains_pii"] is True
        assert meta["pii_types"] == ["health"]
        assert meta["business_relevance"] == "business"
        # The verdict is added to the provenance keys, not in place of them.
        assert meta["source"] == "auto_chunk"
        assert meta["parent_memory_id"] == run.parent_id


async def test_a_flagged_write_is_audited() -> None:
    """``MergeEnrichmentFields`` already set ``contains_pii`` on the row, so the
    metadata alone cannot tell whether the gate ran. The audit row can: before
    this fix no governance action was recorded for an auto-chunk write at all."""
    run = await _run_auto_chunk(
        config=_config(pii={"enabled": True, "action": "flag"}), enrichment=_pii()
    )

    assert run.audits == ["pii_flag"]
    assert run.parent is not None
    assert run.parent["metadata_"]["contains_pii"] is True


# ── failure and over-application guards ──────────────────────────────────────


async def test_a_failed_governance_step_refuses_the_write() -> None:
    """The runner CATCHES a non-``HTTPException`` and reports ``failed`` rather
    than raising, so the handler has to check. Without the check an audit
    backend outage would quietly downgrade an enforced policy to no policy —
    which is the defect being fixed, one level up."""
    run = await _run_auto_chunk(
        config=_config(pii={"enabled": True, "action": "flag"}),
        enrichment=_pii(),
        audit_raises=True,
    )

    assert run.error is not None and run.error.status_code == 500
    assert run.parent is None
    assert run.children == []


async def test_an_uncertain_llm_signal_takes_no_destructive_action() -> None:
    """``llm_ms == 0`` means no live model judged this write. The deterministic
    scan is the fail-closed backstop; the free-form gate records the uncertainty
    and lets the write through rather than dropping on an absent signal."""
    run = await _run_auto_chunk(
        config=_config(pii={"enabled": True, "action": "drop"}),
        enrichment=EnrichmentResult(contains_pii=True, pii_types=["health"], llm_ms=0),
    )

    assert run.error is None
    assert run.parent is not None
    assert run.parent["metadata_"]["governance_llm_uncertain"] is True
    assert run.audits == []


async def test_business_content_is_written_unchanged() -> None:
    """Over-application guard: an enabled policy with nothing to act on must
    leave the write exactly as it was."""
    run = await _run_auto_chunk(
        config=_config(
            pii={"enabled": True, "action": "drop"},
            non_business={"enabled": True, "disposition": "drop"},
        ),
        enrichment=_business(),
    )

    assert run.error is None
    assert run.parent is not None
    assert run.parent["visibility"] == "scope_team"
    assert len(run.children) == 2
    assert run.audits == []


async def test_governance_disabled_enforces_nothing() -> None:
    """With no policy configured the step SKIPs: a flagged verdict costs the
    write nothing — no rejection, no downgrade, no audit row.

    The children still inherit the three labels, and that is not the gate
    firing: ``MergeEnrichmentFields`` records them for any enrichment result
    because they describe the content, and the children are made of that
    content. Enforcement is what governance adds, not the labelling.
    """
    run = await _run_auto_chunk(config=_config(), enrichment=_pii())

    assert run.error is None
    assert run.audits == []
    assert run.parent is not None
    assert run.parent["visibility"] == "scope_team"
    assert len(run.children) == 2
    assert all(c["metadata_"]["contains_pii"] is True for c in run.children)


async def test_the_inheritance_does_not_invent_a_verdict() -> None:
    """Over-application guard on the copy itself: a parent with no verdict keys
    (the LLM did not run) yields children with the two provenance keys and
    nothing else."""
    run = await _run_auto_chunk(
        config=_config(pii={"enabled": True, "action": "drop"}), enrichment=None
    )

    assert run.error is None
    assert len(run.children) == 2
    for child in run.children:
        assert set(child["metadata_"]) == {"parent_memory_id", "source"}


def test_only_the_persisting_compositions_apply_the_llm_verdict() -> None:
    """Pins WHERE the step may live, which is the whole design question here.

    ``build_enrichment_pipeline`` is shared with the extract-only branch
    (``persist=False``), which writes nothing and returns a preview of content
    the caller already holds — a 422 there refuses a request that leaks nothing.
    So the step goes on the auto-chunk composition instead. Asserting the exact
    set rather than "auto-chunk has it" also catches the reverse mistake of
    dropping it from strong mode.
    """
    builders = {
        name: getattr(compositions, name)
        for name in dir(compositions)
        if name.startswith("build_") and name.endswith("_pipeline")
    }
    assert len(builders) >= 6, f"composition set shrank unexpectedly: {sorted(builders)}"

    applies = {
        name
        for name, build in builders.items()
        if any(isinstance(step, GovernanceDecision) for step in build()._steps)
    }
    assert applies == {
        "build_strong_write_pipeline",
        "build_auto_chunk_governance_pipeline",
    }
