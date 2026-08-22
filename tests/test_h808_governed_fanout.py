"""Derived rows must not outlive the governance verdict on their source (#808).

``_enrich_memory_background`` fans the enricher's ``atomic_facts`` out into a
child memory per claim, each one made of text lifted from the parent. The LLM
governance verdict was applied by the CALLER, after this function returned — by
which time every child already existed. With ``governance_pii.action = "drop"``
the parent was soft-deleted and the children survived it: same content, no
governance metadata, and no audit row tying them to the drop.

``keep_private`` had the matching hole: the parent was downgraded to
``scope_agent`` while the children kept the visibility read off the parent row
BEFORE remediation ran, so the content the policy had just made private was
re-published at the original scope.

Inline deployments only — core-worker does not implement the fan-out
(``_ENRICHMENT_UNROUTED_FIELDS`` lists ``atomic_facts`` as deliberately
unrouted), so a deferred deployment has no children to leak. The deterministic
pre-write gate still covers regex/Luhn/entropy-detectable PII in both modes;
this is specifically the LLM's free-form judgement.

The fix moves remediation inside the enrichment function, ahead of anything
derived from the row. These tests pin all three consequences of that ordering:
no children on a drop, no entity extraction on a drop, and the downgrade
carried to the children on keep_private.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from core_api.services import memory_service
from core_api.services.governance_remediation import RemediationOutcome
from core_api.services.memory_enrichment import AtomicFact

pytestmark = [pytest.mark.unit]

TENANT = "t-h808"
FLEET = "f1"
AGENT = "a"


def _cfg(*, entity_extraction: bool = False) -> SimpleNamespace:
    return SimpleNamespace(
        enrichment_enabled=True,
        enrichment_provider="fake",
        entity_extraction_enabled=entity_extraction,
    )


def _parent_row(visibility: str = "scope_team") -> dict:
    return {
        "id": str(uuid.uuid4()),
        "memory_type": "fact",
        "status": "active",
        "weight": 0.5,
        "ts_valid_start": None,
        "ts_valid_end": None,
        "metadata_": {},
        "deleted_at": None,
        "fleet_id": FLEET,
        "embedding": None,
        "content": "body",
        "visibility": visibility,
    }


def _enrichment(facts: list[AtomicFact], *, contains_pii: bool = False, personal: bool = False):
    return SimpleNamespace(
        memory_type="fact",
        weight=0.5,
        status="active",
        title="t",
        summary="",
        tags=[],
        llm_ms=1,
        contains_pii=contains_pii,
        pii_types=["email"] if contains_pii else [],
        business_relevance="personal" if personal else "business",
        retrieval_hint="",
        ts_valid_start=None,
        ts_valid_end=None,
        atomic_facts=facts,
    )


async def _run(
    *,
    facts: list[AtomicFact],
    outcome: RemediationOutcome | Exception = RemediationOutcome(),
    run_governance: bool = True,
    entity_extraction: bool = False,
    contains_pii: bool = False,
    personal: bool = False,
    parent_visibility: str = "scope_team",
    _created_sink: list | None = None,
    _return_sink: list | None = None,
):
    """Drive one ``_enrich_memory_background`` with a stubbed verdict.

    Returns ``(children, calls)`` where ``calls`` records the ORDER of the
    governance call against the child writes — the ordering is the fix, so it
    has to be observable rather than inferred from the end state.
    """
    sc = AsyncMock(name="storage_client")
    sc.get_memory = AsyncMock(return_value=_parent_row(parent_visibility))
    sc.update_memory = AsyncMock(return_value=None)
    sc.update_memory_status = AsyncMock(return_value=None)
    sc.bulk_find_by_content_hashes = AsyncMock(return_value={})

    calls: list[str] = []

    async def _create(payload):
        calls.append("create_child")
        if _created_sink is not None:
            _created_sink.append(payload)
        return {"id": str(uuid.uuid4())}

    sc.create_memory = AsyncMock(side_effect=_create)

    async def _remediate(_row, _cfg_arg):
        calls.append("remediate")
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    async def _embed(_content, tenant_config=None, **_kw):
        return [0.0]

    def _stub_tracked_task(coro, *_a, **_k):
        coro.close()
        return None

    scheduled: list[str] = []

    def _stub_track_task(task, *_a, **_k):
        scheduled.append("task")
        return None

    with (
        patch.object(memory_service, "get_storage_client", lambda: sc),
        patch.object(memory_service, "track_task", MagicMock(side_effect=_stub_track_task)),
        patch.object(memory_service, "tracked_task", new=_stub_tracked_task),
        patch.object(memory_service, "get_embedding", new=_embed),
        patch(
            "core_api.services.memory_enrichment.enrich_memory",
            new=AsyncMock(return_value=_enrichment(facts, contains_pii=contains_pii, personal=personal)),
        ),
        patch(
            "core_api.services.organization_settings.resolve_config",
            new=AsyncMock(return_value=_cfg(entity_extraction=entity_extraction)),
        ),
        patch(
            "core_api.services.governance_remediation.remediate_after_enrichment",
            new=_remediate,
        ),
    ):
        _returned = await memory_service._enrich_memory_background(
            uuid.uuid4(),
            "body",
            TENANT,
            FLEET,
            AGENT,
            governance_config=_cfg(entity_extraction=entity_extraction),
            run_governance_remediation=run_governance,
        )
    if _return_sink is not None:
        _return_sink.append(_returned)

    children = [c.args[0] for c in sc.create_memory.await_args_list]
    return children, calls, scheduled


async def _run_returning(**kwargs):
    """``_run`` but yielding what the function returned rather than its effects."""
    box: list = []
    kwargs["_return_sink"] = box
    await _run(**kwargs)
    return box[0]


# ---------------------------------------------------------------------------
# A dropped parent must not have spawned anything
# ---------------------------------------------------------------------------


async def test_a_dropped_parent_creates_no_children():
    """The issue in one assertion.

    Pre-fix the two children were already written by the time the caller
    applied the verdict, so the drop removed the parent and left them behind.
    """
    children, calls, _ = await _run(
        facts=[AtomicFact(content="alpha fact"), AtomicFact(content="beta fact")],
        outcome=RemediationOutcome(dropped=True),
        contains_pii=True,
    )

    assert children == [], "a policy that says this row must not exist spawned derived rows"
    assert calls == ["remediate"], "governance must run before, not after, the fan-out"


async def test_a_dropped_parent_schedules_no_entity_extraction():
    """Entities mined out of dropped content are the same leak in another table."""
    _, _, scheduled = await _run(
        facts=[AtomicFact(content="alpha fact"), AtomicFact(content="beta fact")],
        outcome=RemediationOutcome(dropped=True),
        entity_extraction=True,
        contains_pii=True,
    )

    assert scheduled == [], "entity extraction ran on content the policy dropped"


async def test_a_remediation_failure_propagates_and_stops_the_fanout():
    """Two properties at once, and they pull against each other.

    The failure must PROPAGATE — the enclosing ``tracked_task`` turns it into a
    ``BackgroundTaskLog`` row, and swallowing it would downgrade an unenforced
    governance policy to a log line (the property
    ``test_remediation_failure_surfaces_to_the_task_tracker`` has pinned since
    H-18). Catching it inside the enrichment handler would have been the easy
    way to keep the fan-out from running, and would have broken that.

    And it must be FAIL-SAFE: a policy that could not be applied must not be
    followed by rows it might have forbidden. Raising achieves both, which is
    why the governance step sits between the two ``try`` blocks rather than
    inside either.
    """
    with pytest.raises(RuntimeError, match="audit sink unreachable"):
        await _run(
            facts=[AtomicFact(content="alpha fact")],
            outcome=RuntimeError("audit sink unreachable"),
            entity_extraction=True,
        )


async def test_no_child_survives_a_remediation_failure():
    """The fail-safe half of the above, observed rather than inferred."""
    created: list[dict] = []

    try:
        await _run(
            facts=[AtomicFact(content="alpha fact")],
            outcome=RuntimeError("audit sink unreachable"),
            entity_extraction=True,
            _created_sink=created,
        )
    except RuntimeError:
        pass

    assert created == []


async def test_a_dropped_parent_is_reported_as_gone():
    """``None`` means "no live governed row" — including after a drop.

    Returning the pre-drop snapshot would hand the next caller a dict for a row
    that had just been soft-deleted, and ``is not None`` is the reading a
    caller is most likely to assume means "still there".
    """
    result = await _run_returning(
        facts=[AtomicFact(content="alpha fact")],
        outcome=RemediationOutcome(dropped=True),
        contains_pii=True,
    )
    assert result is None


async def test_the_flag_without_a_config_fails_loudly_and_early():
    """The two parameters are independent, and remediation dereferences the
    config immediately. A call site that sets the flag but forgets the config
    must say so, not surface an AttributeError on ``None`` from two modules
    away — the caller-side "if you add a third call site" warning describes
    exactly when that would happen."""
    with pytest.raises(ValueError, match="requires governance_config"):
        _returned = await memory_service._enrich_memory_background(
            uuid.uuid4(),
            "body",
            TENANT,
            FLEET,
            AGENT,
            run_governance_remediation=True,
        )


# ---------------------------------------------------------------------------
# keep_private has to reach the children too
# ---------------------------------------------------------------------------


async def test_keep_private_cascades_to_the_children():
    """Pre-fix the children were written with the parent's PRE-downgrade
    visibility, re-publishing at ``scope_team`` exactly the content the policy
    had just restricted to ``scope_agent``."""
    children, _, _ = await _run(
        facts=[AtomicFact(content="alpha fact"), AtomicFact(content="beta fact")],
        outcome=RemediationOutcome(visibility="scope_agent"),
        personal=True,
    )

    assert len(children) == 2
    assert {c["visibility"] for c in children} == {"scope_agent"}


async def test_an_untouched_parent_still_passes_its_own_visibility_down():
    """The guard against over-applying the cascade: with no remediation action,
    children keep inheriting the parent's visibility as they always did."""
    children, _, _ = await _run(
        facts=[AtomicFact(content="alpha fact")],
        outcome=RemediationOutcome(),
        parent_visibility="scope_org",
    )

    assert [c["visibility"] for c in children] == ["scope_org"]


# ---------------------------------------------------------------------------
# Surviving children must not read as clean
# ---------------------------------------------------------------------------


async def test_children_carry_the_parents_governance_verdict():
    """A flagged (not dropped) parent's children are made of the flagged text.

    Pre-fix ``child_meta`` was ``{parent_memory_id, source, retrieval_hint}``
    only, so a child read as clean to an audit query while being made of the
    content the LLM flagged.
    """
    children, _, _ = await _run(
        facts=[AtomicFact(content="alpha fact")],
        outcome=RemediationOutcome(),  # flag, not drop
        contains_pii=True,
        personal=True,
    )

    assert len(children) == 1
    meta = children[0]["metadata_"]
    assert meta["contains_pii"] is True
    assert meta["pii_types"] == ["email"]
    assert meta["business_relevance"] == "personal"
    # The existing keys must survive alongside them.
    assert meta["source"] == "atomic_fact_fanout"
    assert "parent_memory_id" in meta


# ---------------------------------------------------------------------------
# The flag still gates it, and the caller must not apply it twice
# ---------------------------------------------------------------------------


async def test_the_flag_off_leaves_the_path_untouched():
    """Strong mode already applied ``GovernanceDecision`` synchronously; a second
    application would duplicate audit rows and re-drop a row policy already
    acted on. With the flag off, remediation must not be called at all."""
    children, calls, _ = await _run(
        facts=[AtomicFact(content="alpha fact")],
        outcome=RemediationOutcome(dropped=True),  # would kill the fan-out if consulted
        run_governance=False,
        contains_pii=True,
    )

    assert "remediate" not in calls
    assert len(children) == 1


async def test_the_scheduler_does_not_apply_the_verdict_a_second_time():
    """Remediation moved out of ``_schedule_enrich_or_inline`` and into the
    enrichment function. If the old call were left behind, a drop would be
    audited and soft-deleted twice for one write."""
    seen: list[str] = []

    async def _remediate(_row, _cfg_arg):
        seen.append("remediate")
        return RemediationOutcome()

    async def _fake_enrich(*_a, **kwargs):
        # Stand in for the real function; report the flag it was handed so the
        # test also proves the caller still forwards it.
        seen.append(f"enrich(run={kwargs.get('run_governance_remediation')})")
        return {"id": "x"}

    with (
        patch.object(memory_service, "_enrich_memory_background", new=_fake_enrich),
        patch(
            "core_api.services.governance_remediation.remediate_after_enrichment",
            new=_remediate,
        ),
        # inline_enrichment is a derived PROPERTY on Settings (no setter),
        # so it is patched on the class — same idiom as
        # test_governance_inline_fast_remediation.
        patch.object(
            type(memory_service.settings),
            "inline_enrichment",
            property(lambda self: True),
        ),
    ):
        await memory_service._schedule_enrich_or_inline(
            uuid.uuid4(),
            "body",
            TENANT,
            FLEET,
            AGENT,
            _cfg(),
            run_governance_remediation=True,
        )

    assert seen == ["enrich(run=True)"], (
        "the scheduler must forward the flag and NOT apply remediation itself"
    )
