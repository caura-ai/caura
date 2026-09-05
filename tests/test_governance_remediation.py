"""Tests for fast-mode post-write governance remediation (eToro).

The fast-mode counterpart to ``GovernanceDecision``: in the default fast mode
enrichment is deferred to the worker, which PATCHes the LLM's ``contains_pii`` /
``business_relevance`` onto the already-persisted row; the enriched-event
consumer then calls ``remediate_after_enrichment`` to apply the tenant's
configured action on that free-form signal. Storage + audit are stubbed so
these stay deterministic (no async audit queue / storage round-trip).
"""

import pytest

from core_api.services import governance_remediation
from core_api.services.organization_settings import ResolvedConfig

pytestmark = pytest.mark.asyncio


@pytest.fixture
def emitted(monkeypatch):
    """Capture governance audit emissions; returns the list of captured kwargs."""
    calls: list[dict] = []

    async def _record(*args, **kwargs):
        calls.append(kwargs)

    monkeypatch.setattr(governance_remediation, "emit_governance_audit", _record)
    return calls


@pytest.fixture
def storage(monkeypatch):
    """Stub the storage client; record soft-delete / update calls.

    ``actions.children`` seeds what ``find_children_by_parent_id`` returns, so a
    test can put derived rows behind a parent without a database. Empty by
    default, which keeps every pre-H-10 test on its original path.
    """

    class _Actions(list):
        """A list that also carries the seeded children (a bare list cannot)."""

        children: list[dict] = []

    actions = _Actions()
    actions.children = []

    class _SC:
        async def soft_delete_memory(self, mid, tenant_id):
            actions.append(("soft_delete", mid, tenant_id))

        async def update_memory(self, mid, tenant_id, patch):
            actions.append(("update", mid, tenant_id, patch))

        async def find_children_by_parent_id(self, tenant_id, parent_id):
            actions.append(("find_children", parent_id, tenant_id))
            return list(actions.children)

        async def purge_entity_artifacts(self, tenant_id, memory_id):
            actions.append(("purge_entities", memory_id, tenant_id))
            return {"links": 0, "relations": 0, "entities": 0}

    monkeypatch.setattr(governance_remediation, "get_storage_client", lambda: _SC())
    return actions


def _cfg(*, pii: dict | None = None, nb: dict | None = None) -> ResolvedConfig:
    gov: dict = {}
    if pii is not None:
        gov["pii"] = pii
    if nb is not None:
        gov["non_business"] = nb
    return ResolvedConfig({"governance": gov})


def _mem(**kw) -> dict:
    return {
        "id": kw.get("id", "m1"),
        "tenant_id": "t1",
        "agent_id": "a1",
        "content": kw.get("content", "free-form detail"),
        "metadata": kw.get("metadata", {}),
    }


async def test_disabled_is_noop(emitted, storage):
    outcome = await governance_remediation.remediate_after_enrichment(_mem(), _cfg())
    assert outcome.dropped is False
    assert emitted == []
    assert storage == []


async def test_missing_id_skips_without_side_effects(emitted, storage):
    # A malformed enriched-event payload without an id must not soft-delete the
    # literal "None" or stamp resource_id="None" on an audit row.
    cfg = _cfg(pii={"enabled": True, "action": "drop"})
    mem = _mem(id=None, metadata={"contains_pii": True})
    outcome = await governance_remediation.remediate_after_enrichment(mem, cfg)
    assert outcome.dropped is False
    assert storage == []
    assert emitted == []


async def test_pii_drop_soft_deletes_and_audits(emitted, storage):
    cfg = _cfg(pii={"enabled": True, "action": "drop"})
    mem = _mem(metadata={"contains_pii": True, "pii_types": ["health"]})
    outcome = await governance_remediation.remediate_after_enrichment(mem, cfg)
    assert outcome.dropped is True
    assert ("soft_delete", "m1", "t1") in storage
    assert any(c["action"] == "pii_drop" for c in emitted)


# ---------------------------------------------------------------------------
# H-02 — entity rows mined out of dropped content
# ---------------------------------------------------------------------------
#
# #808 named this case when it fixed the inline path: "entities mined out of
# dropped content are the same leak in another table". Both non-inline paths
# schedule extraction at write time, racing the verdict, and a soft-deleted
# memory still satisfies the link/relation foreign keys — so the names survived,
# listable tenant-wide, with nothing tying them to the drop.


async def test_a_pii_drop_purges_the_graph_rows(emitted, storage):
    cfg = _cfg(pii={"enabled": True, "action": "drop"})
    mem = _mem(metadata={"contains_pii": True, "pii_types": ["health"]})

    outcome = await governance_remediation.remediate_after_enrichment(mem, cfg)

    assert outcome.dropped is True
    assert ("purge_entities", "m1", "t1") in storage, storage


async def test_a_nonbusiness_drop_purges_the_graph_rows(emitted, storage):
    """Both destructive dispositions, not just one — separate branches, separate
    configs, and a tenant on either policy leaks identically without this."""
    cfg = _cfg(nb={"enabled": True, "disposition": "drop"})
    mem = _mem(metadata={"business_relevance": "personal"})

    await governance_remediation.remediate_after_enrichment(mem, cfg)

    assert ("purge_entities", "m1", "t1") in storage, storage


async def test_a_failed_purge_does_not_abort_the_handler(
    emitted, storage, monkeypatch, caplog
):
    """A raise here would nack the Pub/Sub event and redeliver it forever.

    ``consumer.handle_memory_enriched`` has no guard around remediation, and the
    dispatcher nacks on a handler exception. Redelivery re-runs the whole drop
    branch, emitting a SECOND ``critical=True`` audit for a memory that was
    already dropped — duplicate destructive entries in a tamper-evident log, for
    a row nothing further can be done to.

    The trade is bounded the other way: the memory is already gone, so the
    content is not live. What remains is graph rows, and the ERROR names the
    memory so they can be purged by hand.
    """

    class _SC:
        async def soft_delete_memory(self, mid, tenant_id):
            storage.append(("soft_delete", mid, tenant_id))

        async def purge_entity_artifacts(self, tenant_id, memory_id):
            raise RuntimeError("storage refused the purge")

    monkeypatch.setattr(governance_remediation, "get_storage_client", lambda: _SC())

    cfg = _cfg(nb={"enabled": True, "disposition": "drop"})
    mem = _mem(metadata={"business_relevance": "personal"})

    with caplog.at_level("ERROR"):
        outcome = await governance_remediation.remediate_after_enrichment(mem, cfg)

    # The parent's verdict still reaches the caller.
    assert outcome.dropped is True
    assert ("soft_delete", "m1", "t1") in storage
    # And the failure is not silent.
    assert any("were NOT" in r.getMessage() for r in caplog.records), [
        r.getMessage()[:80] for r in caplog.records
    ]


async def test_a_non_destructive_verdict_purges_nothing(emitted, storage):
    """OVER-REFUSAL GUARD. ``flag`` and ``keep_private`` leave the row readable.

    The graph rows describe content that is still there and still allowed, so
    removing them would destroy data no policy asked to remove.
    """
    cfg = _cfg(
        pii={"enabled": True, "action": "flag"},
        nb={"enabled": True, "disposition": "keep_private"},
    )
    mem = _mem(metadata={"contains_pii": True, "business_relevance": "personal"})

    await governance_remediation.remediate_after_enrichment(mem, cfg)

    assert not any(kind == "purge_entities" for kind, *_ in storage), storage


# ---------------------------------------------------------------------------
# H-10 — derived rows that already exist when the verdict lands
# ---------------------------------------------------------------------------
#
# The atomic-fact fan-out is safe by ORDERING: it lives inside
# ``_enrich_memory_background``, runs remediation first, and early-returns on a
# drop — "a policy that could not be applied must not be followed by rows it
# might have forbidden". Auto-chunk children get no such protection on a
# deferred deployment. There the write-time ``GovernanceDecision`` runs with
# ``enrichment=None``, takes its uncertain branch and enforces NOTHING, and the
# children are built and committed immediately. The parent's real verdict
# arrives minutes later, through here — and remediated only the row the event
# named. ``parent_memory_id`` was written on every child and queried nowhere.
#
# So a tenant configured ``non_business.disposition=drop`` had its parent
# dropped and audited while N children carrying the same content stayed live at
# ``scope_team``, with no governance metadata and no audit row tying them to the
# drop. Permanently: children are never enriched, so no later pass revisits them.


def _child(mid: str) -> dict:
    return {
        "id": mid,
        "tenant_id": "t1",
        "agent_id": "a1",
        "content": "a fact",
        "metadata": {},
    }


async def test_a_drop_cascades_to_the_children_that_already_exist(emitted, storage):
    """The parent's drop must reach rows derived from it before the verdict."""
    storage.children = [_child("c1"), _child("c2")]
    cfg = _cfg(nb={"enabled": True, "disposition": "drop"})
    mem = _mem(
        metadata={
            "business_relevance": "personal",
            "auto_chunked": True,
            "child_count": 2,
        }
    )

    outcome = await governance_remediation.remediate_after_enrichment(mem, cfg)

    assert outcome.dropped is True
    deleted = {mid for kind, mid, *_ in storage if kind == "soft_delete"}
    assert deleted == {"m1", "c1", "c2"}, (
        f"the dropped content survives in the children: {deleted}"
    )


async def test_the_cascaded_children_are_audited_not_deleted_silently(emitted, storage):
    """A destructive action with no audit row is exactly what compliance cannot have.

    The parent's drop is already audited ``critical=True`` because the
    soft-delete leaves the audit as the only trace. The children carry the same
    content, so deleting them off the back of the parent's verdict with no
    record of their own would leave the compliance log claiming one row was
    dropped when N+1 were.
    """
    storage.children = [_child("c1"), _child("c2")]
    cfg = _cfg(nb={"enabled": True, "disposition": "drop"})
    mem = _mem(
        metadata={
            "business_relevance": "personal",
            "auto_chunked": True,
            "child_count": 2,
        }
    )

    await governance_remediation.remediate_after_enrichment(mem, cfg)

    audited = {c.get("resource_id") for c in emitted}
    assert {"c1", "c2"} <= audited, f"children dropped without an audit row: {audited}"


async def test_keep_private_cascades_the_downgrade_to_the_children(emitted, storage):
    """``keep_private`` downgrades the parent; a child left at ``scope_team``
    re-publishes exactly the content the policy just made private (#808)."""
    storage.children = [_child("c1")]
    cfg = _cfg(nb={"enabled": True, "disposition": "keep_private"})
    mem = _mem(
        metadata={
            "business_relevance": "personal",
            "auto_chunked": True,
            "child_count": 1,
        }
    )

    outcome = await governance_remediation.remediate_after_enrichment(mem, cfg)

    assert outcome.visibility == "scope_agent"
    downgraded = {mid for kind, mid, *_ in storage if kind == "update"}
    assert downgraded == {"m1", "c1"}, (
        f"a child still publishes the privatised content: {downgraded}"
    )


async def test_a_row_with_no_children_never_runs_the_lookup(emitted, storage):
    """OVER-REFUSAL GUARD. The common path must not pay for the rare one.

    The cascade query filters on a JSON key with no supporting index, and a
    compliance tenant configured ``drop`` remediates constantly. Gating on the
    parent's own record of having children keeps that query off every ordinary
    drop — and the marker is set unconditionally beside the children it
    describes, which ``test_the_auto_chunk_parent_records_that_it_has_children``
    pins.
    """
    cfg = _cfg(nb={"enabled": True, "disposition": "drop"})
    mem = _mem(metadata={"business_relevance": "personal"})

    outcome = await governance_remediation.remediate_after_enrichment(mem, cfg)

    assert outcome.dropped is True
    assert not any(kind == "find_children" for kind, *_ in storage), (
        "the child lookup fired for a row that has no children"
    )


async def test_pii_drop_cascades_too(emitted, storage):
    """Both destructive dispositions reach the children, not just the non-business one.

    They are separate branches with separate configs; a tenant on a PII drop
    policy leaks identically if only the non-business branch cascades.
    """
    storage.children = [_child("c1")]
    cfg = _cfg(pii={"enabled": True, "action": "drop"})
    mem = _mem(
        metadata={"contains_pii": True, "pii_types": ["health"], "auto_chunked": True}
    )

    outcome = await governance_remediation.remediate_after_enrichment(mem, cfg)

    assert outcome.dropped is True
    deleted = {mid for kind, mid, *_ in storage if kind == "soft_delete"}
    assert deleted == {"m1", "c1"}, deleted


async def test_one_failing_child_does_not_abandon_the_rest(
    emitted, storage, monkeypatch, caplog
):
    """The parent is already gone by the time this loop runs.

    So aborting on the first failure would leave every later child live with the
    parent deleted — this feature's own leak, reintroduced by its error
    handling. One bad row must cost one row.

    The failure still surfaces: it is raised once, after every child has been
    ATTEMPTED, so ``tracked_task`` records an unapplied policy rather than the
    cascade swallowing it.
    """
    storage.children = [_child("c1"), _child("c2"), _child("c3")]

    class _SC:
        async def soft_delete_memory(self, mid, tenant_id):
            if mid == "c1":
                raise RuntimeError("storage blipped on the first child")
            storage.append(("soft_delete", mid, tenant_id))

        async def update_memory(self, mid, tenant_id, patch):
            storage.append(("update", mid, tenant_id, patch))

        async def find_children_by_parent_id(self, tenant_id, parent_id):
            return list(storage.children)

        async def purge_entity_artifacts(self, tenant_id, memory_id):
            storage.append(("purge_entities", memory_id, tenant_id))
            return {"links": 0, "relations": 0, "entities": 0}

    monkeypatch.setattr(governance_remediation, "get_storage_client", lambda: _SC())

    cfg = _cfg(nb={"enabled": True, "disposition": "drop"})
    mem = _mem(
        metadata={
            "business_relevance": "personal",
            "auto_chunked": True,
            "child_count": 3,
        }
    )

    with caplog.at_level("ERROR"):
        with pytest.raises(governance_remediation.GovernanceCascadeError) as exc:
            await governance_remediation.remediate_after_enrichment(mem, cfg)

    # The exception carries what the PARENT's remediation did. Without it a
    # caller cannot tell "nothing happened, retry from scratch" from "the parent
    # was dropped and only the derived cleanup fell short" — two states that
    # want opposite follow-up.
    assert exc.value.outcome.dropped is True

    deleted = {mid for kind, mid, *_ in storage if kind == "soft_delete"}
    # c1 failed; c2 and c3 must still have been remediated, and so must the parent.
    assert deleted == {"m1", "c2", "c3"}, (
        f"a failure on one child abandoned the others: {deleted}"
    )
    assert any("c1" in r.getMessage() for r in caplog.records)


async def test_a_child_whose_delete_failed_after_its_audit_is_logged_as_such(
    emitted, storage, caplog, monkeypatch
):
    """The audit says "dropped" and the row is live. Nothing retries it.

    That combination used to be indistinguishable in the logs from "nothing
    happened to this row", and the docstring claimed a redelivery would clean it
    up — which stopped being true once ``handle_memory_enriched`` began acking
    on ``GovernanceCascadeError``. A person is the recovery path now, so the log
    has to say WHICH state the row is in and carry a field an alert can key on.

    Asserts the structured field, not the prose: the message can be reworded,
    but a monitor keyed on ``governance_cascade_needs_manual_remediation``
    breaking silently is the whole failure mode this guards.
    """
    storage.children = [_child("c1")]

    class _SC:
        async def soft_delete_memory(self, mid, tenant_id):
            if mid == "c1":
                raise RuntimeError("delete failed AFTER the audit landed")
            storage.append(("soft_delete", mid, tenant_id))

        async def update_memory(self, mid, tenant_id, patch):
            storage.append(("update", mid, tenant_id, patch))

        async def find_children_by_parent_id(self, tenant_id, parent_id):
            return list(storage.children)

        async def purge_entity_artifacts(self, tenant_id, memory_id):
            storage.append(("purge_entities", memory_id, tenant_id))
            return {"links": 0, "relations": 0, "entities": 0}

    monkeypatch.setattr(governance_remediation, "get_storage_client", lambda: _SC())

    cfg = _cfg(nb={"enabled": True, "disposition": "drop"})
    mem = _mem(
        metadata={
            "business_relevance": "personal",
            "auto_chunked": True,
            "child_count": 1,
        }
    )

    with caplog.at_level("ERROR"):
        with pytest.raises(governance_remediation.GovernanceCascadeError):
            await governance_remediation.remediate_after_enrichment(mem, cfg)

    # The audit for c1 DID land — which is exactly what makes this state bad.
    assert any(a.get("resource_id") == "c1" for a in emitted), (
        "precondition: the child's audit must have been emitted before the delete"
    )

    flagged = [
        r
        for r in caplog.records
        if getattr(r, "governance_cascade_needs_manual_remediation", False)
    ]
    assert flagged, "the failure carried no field an alert could key on"
    assert flagged[0].audit_emitted is True, (
        "an audit-emitted-then-delete-failed row must not be reported as if "
        "nothing had been written about it"
    )
    assert flagged[0].memory_id == "c1"
    assert flagged[0].parent_memory_id == "m1"


async def test_a_child_whose_audit_failed_is_reported_as_untouched(
    emitted, storage, caplog, monkeypatch
):
    """The other state, and the one that must NOT claim an audit exists.

    Pairs with the test above: same failure surface, opposite value of
    ``audit_emitted``. Without the split ``try`` blocks both states produced the
    same log line, so a responder could not tell a live row the log misdescribes
    from a live row it says nothing about.
    """
    storage.children = [_child("c1")]

    async def _boom(*args, **kwargs):
        # Only the CHILD's audit fails. The parent's has to succeed or
        # remediation never reaches the cascade and this tests nothing.
        if kwargs.get("resource_id") == "c1":
            raise RuntimeError("audit queue rejected the entry")
        emitted.append(kwargs)

    monkeypatch.setattr(governance_remediation, "emit_governance_audit", _boom)

    cfg = _cfg(nb={"enabled": True, "disposition": "drop"})
    mem = _mem(
        metadata={
            "business_relevance": "personal",
            "auto_chunked": True,
            "child_count": 1,
        }
    )

    with caplog.at_level("ERROR"):
        with pytest.raises(governance_remediation.GovernanceCascadeError):
            await governance_remediation.remediate_after_enrichment(mem, cfg)

    flagged = [
        r
        for r in caplog.records
        if getattr(r, "governance_cascade_needs_manual_remediation", False)
    ]
    assert flagged, "the failure carried no field an alert could key on"
    assert flagged[0].audit_emitted is False
    # And the delete was never attempted for it.
    assert not [a for a in storage if a[0] == "soft_delete" and a[1] == "c1"]


async def test_the_cascade_count_does_not_include_rows_it_skipped(
    emitted, storage, caplog
):
    """A summary log that overstates enforcement is worse than no summary.

    ``len(children)`` counted rows the loop skipped for a missing id — so the
    line claimed N remediated while N-1 were, in a log a compliance review
    reads.
    """
    storage.children = [_child("c1"), {"id": None, "content": "unidentifiable"}]
    cfg = _cfg(nb={"enabled": True, "disposition": "drop"})
    mem = _mem(
        metadata={
            "business_relevance": "personal",
            "auto_chunked": True,
            "child_count": 2,
        }
    )

    with caplog.at_level("INFO"):
        with pytest.raises(governance_remediation.GovernanceCascadeError):
            await governance_remediation.remediate_after_enrichment(mem, cfg)

    summary = [r.getMessage() for r in caplog.records if "cascaded" in r.getMessage()]
    assert summary, "no summary line was logged"
    assert "to 1 derived row(s)" in summary[0], summary


async def test_a_failed_cascade_never_erases_the_parents_own_audit(
    emitted, storage, monkeypatch
):
    """The parent's mutation is durable; its audit must be too.

    ``keep_private`` narrows the parent BEFORE auditing it — correct on its own,
    because nothing is lost if a non-destructive audit fails after the change.
    But the cascade raises when a child fails, so putting the cascade between
    the two left the parent durably narrowed with NO audit row at all. An
    untracked mutation is precisely what this module's ordering rules exist to
    prevent.

    The drop branches never had this exposure: they audit before the destructive
    delete, so the cascade is already the last thing they do.
    """
    storage.children = [_child("c1")]

    class _SC:
        async def update_memory(self, mid, tenant_id, patch):
            if mid == "c1":
                raise RuntimeError("storage blipped on the child")
            storage.append(("update", mid, tenant_id, patch))

        async def soft_delete_memory(self, mid, tenant_id):
            storage.append(("soft_delete", mid, tenant_id))

        async def find_children_by_parent_id(self, tenant_id, parent_id):
            return list(storage.children)

        async def purge_entity_artifacts(self, tenant_id, memory_id):
            storage.append(("purge_entities", memory_id, tenant_id))
            return {"links": 0, "relations": 0, "entities": 0}

    monkeypatch.setattr(governance_remediation, "get_storage_client", lambda: _SC())

    cfg = _cfg(nb={"enabled": True, "disposition": "keep_private"})
    mem = _mem(
        metadata={
            "business_relevance": "personal",
            "auto_chunked": True,
            "child_count": 1,
        }
    )

    with pytest.raises(governance_remediation.GovernanceCascadeError):
        await governance_remediation.remediate_after_enrichment(mem, cfg)

    # The parent WAS narrowed...
    assert ("update", "m1", "t1", {"visibility": "scope_agent"}) in storage
    # ...so the record of that narrowing must exist.
    assert any(c.get("resource_id") == "m1" for c in emitted), (
        f"the parent's visibility changed with no audit row: {emitted}"
    )


async def test_pii_mask_config_flags_but_records_intent(emitted, storage):
    # Fast mode can't redact a free-form LLM span; a mask policy flags the row
    # but stays distinguishable from a genuine flag policy in the audit.
    cfg = _cfg(pii={"enabled": True, "action": "mask"})
    mem = _mem(metadata={"contains_pii": True, "pii_types": ["health"]})
    outcome = await governance_remediation.remediate_after_enrichment(mem, cfg)
    assert outcome.dropped is False
    assert storage == []  # nothing redacted/dropped
    flag = next(c for c in emitted if c["action"] == "pii_flag")
    assert flag["detail"]["configured_action"] == "pii_mask"


async def test_pii_flag_config_records_no_configured_action(emitted, storage):
    cfg = _cfg(pii={"enabled": True, "action": "flag"})
    mem = _mem(metadata={"contains_pii": True})
    await governance_remediation.remediate_after_enrichment(mem, cfg)
    flag = next(c for c in emitted if c["action"] == "pii_flag")
    assert "configured_action" not in flag["detail"]


async def test_nonbusiness_keep_private_updates_visibility(emitted, storage):
    cfg = _cfg(nb={"enabled": True, "disposition": "keep_private"})
    mem = _mem(metadata={"business_relevance": "personal"})
    outcome = await governance_remediation.remediate_after_enrichment(mem, cfg)
    assert outcome.dropped is False
    assert ("update", "m1", "t1", {"visibility": "scope_agent"}) in storage
    assert any(c["action"] == "nonbusiness_keep_private" for c in emitted)


async def test_nonbusiness_drop_soft_deletes(emitted, storage):
    cfg = _cfg(nb={"enabled": True, "disposition": "drop"})
    mem = _mem(metadata={"business_relevance": "personal"})
    outcome = await governance_remediation.remediate_after_enrichment(mem, cfg)
    assert outcome.dropped is True
    assert ("soft_delete", "m1", "t1") in storage
    assert any(c["action"] == "nonbusiness_drop" for c in emitted)


async def test_business_content_is_noop(emitted, storage):
    cfg = _cfg(nb={"enabled": True, "disposition": "drop"})
    mem = _mem(metadata={"business_relevance": "business"})
    outcome = await governance_remediation.remediate_after_enrichment(mem, cfg)
    assert outcome.dropped is False
    assert storage == []
    assert emitted == []


@pytest.mark.parametrize(
    ("cfg_kwargs", "metadata", "drop_action"),
    [
        (
            {"pii": {"enabled": True, "action": "drop"}},
            {"contains_pii": True},
            "pii_drop",
        ),
        (
            {"nb": {"enabled": True, "disposition": "drop"}},
            {"business_relevance": "personal"},
            "nonbusiness_drop",
        ),
    ],
)
async def test_drop_audits_before_soft_delete(
    monkeypatch, cfg_kwargs, metadata, drop_action
):
    # Compliance invariant: the audit must be recorded BEFORE the destructive
    # soft-delete, so a delete that succeeds before a failing audit can't leave
    # an untracked deletion in the tamper-evident log (mirrors the audit-before-
    # mutate ordering in GovernanceScanContent). Capture both into one ordered log.
    order: list[str] = []

    async def _audit(*_a, **kw):
        order.append(f"audit:{kw['action']}")

    class _SC:
        async def soft_delete_memory(self, _mid, _tenant_id):
            order.append("soft_delete")

    monkeypatch.setattr(governance_remediation, "emit_governance_audit", _audit)
    monkeypatch.setattr(governance_remediation, "get_storage_client", lambda: _SC())

    outcome = await governance_remediation.remediate_after_enrichment(
        _mem(metadata=metadata), _cfg(**cfg_kwargs)
    )
    assert outcome.dropped is True
    assert order == [f"audit:{drop_action}", "soft_delete"]
