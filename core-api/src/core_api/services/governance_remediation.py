"""Fast-mode post-write governance remediation.

In fast mode (the default) enrichment is deferred to core-worker, which PATCHes
the LLM's ``contains_pii`` / ``business_relevance`` onto the already-persisted
row. The ``memclaw.memory.enriched`` consumer then calls
:func:`remediate_after_enrichment` to apply the tenant's configured action on
that free-form signal — the fast-mode counterpart to the synchronous
``GovernanceDecision`` step (strong mode).

The DETERMINISTIC pattern gate already ran synchronously pre-write
(``GovernanceScanContent``), so regex/Luhn/entropy-detectable PII/PCI/secrets
were never persisted in either mode; only the LLM's free-form judgement is
eventually-consistent here (≈ enrichment-deferral latency).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from core_api.clients.storage_client import get_storage_client
from core_api.services.governance_gate import (
    ACTION_NB_DROP,
    ACTION_NB_KEEP_PRIVATE,
    ACTION_PII_DROP,
    ACTION_PII_FLAG,
    ACTION_PII_MASK,
    emit_governance_audit,
    llm_pii_audit_detail,
    nonbusiness_audit_detail,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class RemediationOutcome:
    """What remediation did to the row — enough for a caller to follow it.

    ``dropped`` alone was sufficient while the only caller had nothing left to
    do afterwards. A caller that goes on to create rows DERIVED from this one
    needs the rest: ``keep_private`` downgrades the row's visibility, and a
    derived row that keeps the pre-downgrade visibility re-publishes exactly
    the content the policy just made private (#808).
    """

    dropped: bool = False
    visibility: str | None = None
    """The row's new visibility when remediation changed it; ``None`` when it
    was left alone."""


async def _pre_verdict_children(sc: Any, memory_id: str, tenant_id: str, md: dict) -> list[dict]:
    """Rows derived from this one BEFORE any verdict existed. ``[]`` when none can.

    H-10. The atomic-fact fan-out needs nothing here — it lives inside
    ``_enrich_memory_background`` and runs remediation *before* it fans out, so
    a drop stops it and a downgrade is carried onto the children it does create.
    Auto-chunk is the path with no such ordering: on a deferred deployment the
    write-time ``GovernanceDecision`` sees ``enrichment=None``, takes its
    documented uncertain branch and enforces nothing, and the children are
    committed immediately. The verdict arrives here minutes later, naming only
    the parent.

    Gated on ``auto_chunked`` rather than querying unconditionally. The lookup
    filters on a JSON key with no supporting index, and a tenant configured
    ``drop`` remediates constantly — so an ungated query would tax every
    ordinary drop to serve the rare chunked one. The marker is safe to gate on
    because it is stamped on the parent unconditionally, in the same function
    that builds the children, and it is already present on the production rows
    this fix has to reach — a NEW marker would only ever appear on rows written
    after the deploy, leaving the existing leak in place.

    Failures propagate. This module's contract is that a policy which could not
    be applied must not be quietly treated as applied; swallowing a lookup error
    would leave the children live, which is the leak this exists to close.
    """
    if not md.get("auto_chunked"):
        return []
    return await sc.find_children_by_parent_id(tenant_id, memory_id)


def _child_id(child: dict) -> str | None:
    raw = child.get("id")
    return str(raw) if raw else None


class GovernanceCascadeError(RuntimeError):
    """Some derived rows could not be remediated after the parent already was.

    Raised only once every child has been ATTEMPTED. The distinction matters:
    aborting the loop on the first failure would leave the remaining children
    live with the parent already gone — the leak this cascade exists to close,
    reintroduced by the error handling rather than by the policy.

    Still raised rather than logged, because the enclosing ``tracked_task`` is
    what turns an unapplied policy into something a human sees; a swallowed
    failure here is a policy silently treated as applied.

    ``outcome`` carries what the PARENT's own remediation did, because that
    part succeeded and a caller may need it. Without it this exception conflates
    two very different states: "the parent's remediation failed, nothing
    happened, retry from scratch" and "the parent was dropped or privatised and
    only the derived cleanup fell short". A caller that wants to honour the
    parent's verdict can read it; one that simply refuses to go on — which is
    the safe default, and what every current caller does — can ignore it.
    """

    def __init__(self, message: str, outcome: RemediationOutcome | None = None) -> None:
        super().__init__(message)
        self.outcome = outcome or RemediationOutcome()


def _raise_if_any_failed(
    failed: list[str], *, parent_id: str, what: str, outcome: RemediationOutcome
) -> None:
    if not failed:
        return
    raise GovernanceCascadeError(
        f"governance: {len(failed)} derived row(s) of {parent_id} could not be "
        f"{what} ({', '.join(failed)}); the parent was already remediated, so these "
        "rows still carry content the policy forbade",
        outcome,
    )


async def _drop_children(
    sc: Any,
    children: list[dict],
    *,
    tenant_id: str,
    parent_id: str,
    action: str,
    detail_for: Any,
) -> None:
    """Soft-delete each derived row, auditing before the delete as the parent does.

    Per child rather than one rolled-up row: each is a separate soft-delete, and
    an audit log that recorded one deletion while N happened would misstate the
    compliance record in the direction that matters.

    Contained PER CHILD, then raised once at the end. The parent is ALREADY
    deleted by the time this runs, so letting the first failure abort the loop
    would leave every later child live with the parent gone — this feature's own
    leak, reintroduced by its error handling. One bad row must cost one row.

    NOTHING RETRIES A FAILED CHILD, and this docstring used to say the opposite.
    The retry it described was real while the raise reached the Pub/Sub
    dispatcher and redelivered the event. It stopped being real when
    ``handle_memory_enriched`` began catching ``GovernanceCascadeError`` and
    acking — which it does because redelivery re-ran the parent's entire drop
    branch and wrote another ``critical=True`` audit for an already-dropped
    memory, on every delivery, without bound. The mechanism went; the paragraph
    describing it did not, until this change. The parent's own remediation has
    already succeeded by now, so no later event revisits this row either.

    Recovery is therefore a person, working from the ERROR logs below, and they
    are written to make that possible rather than merely to record that
    something failed. The audit and the delete are attempted in SEPARATE
    ``try`` blocks so a reader can tell the two states apart:

    * ``audit_emitted=False`` — nothing happened to the row. It is live and the
      compliance log does not claim otherwise.
    * ``audit_emitted=True`` — the one that needs hands. The log RECORDS the row
      as removed and the row is still live.

    The audit still precedes the delete, and that ordering is what makes the
    second state possible — deliberately, because the alternative is worse. A
    false "dropped" entry is discoverable and the content is still there to
    remove; auditing afterwards would turn the same failure into a deletion
    with no record of it at all.
    """
    remediated = 0
    failed: list[str] = []
    for child in children:
        cid = _child_id(child)
        if not cid:
            # Cannot delete it and cannot audit it against an id. Counted as a
            # failure, not just logged: the row still holds content a policy
            # forbade, and "we could not identify it" is not "it is handled".
            # A retry will not fix this one — a person has to look.
            logger.error(
                "governance: derived row of %s has no usable id; it still holds "
                "dropped content and was NOT removed",
                parent_id,
            )
            failed.append("<no id>")
            continue
        try:
            detail = detail_for(child.get("content") or "")
            # Distinguishes a cascaded removal from a direct one, so a compliance
            # review can see WHY a row with no governance signals of its own was
            # dropped — the children are never enriched, so they carry none.
            detail["cascaded_from"] = parent_id
            await emit_governance_audit(
                tenant_id=tenant_id,
                agent_id=child.get("agent_id"),
                action=action,
                detail=detail,
                resource_id=cid,
                # Destructive, same as the parent: the audit is the only trace.
                critical=True,
            )
        except Exception:
            # Nothing was written and nothing was deleted. The row is live and
            # the compliance log makes no claim about it.
            logger.exception(
                "governance: could not cascade %s to derived row %s of %s; it still holds dropped content",
                action,
                cid,
                parent_id,
                extra={
                    "governance_cascade_needs_manual_remediation": True,
                    "audit_emitted": False,
                    "memory_id": cid,
                    "parent_memory_id": parent_id,
                    "tenant_id": tenant_id,
                    "action": action,
                },
            )
            failed.append(cid)
            continue

        # Separate block on purpose: past this point the audit EXISTS, so a
        # failure here is a different state to report, not the same one.
        try:
            await sc.soft_delete_memory(cid, tenant_id)
        except Exception:
            logger.exception(
                "governance: audit RECORDS derived row %s of %s as %s, but the delete "
                "failed and the row is STILL LIVE; nothing retries this — the "
                "compliance log is wrong about this row until someone removes it",
                cid,
                parent_id,
                action,
                extra={
                    # The field a log-based alert keys on. Not derivable from the
                    # message text, and it is the only signal that this row exists.
                    "governance_cascade_needs_manual_remediation": True,
                    "audit_emitted": True,
                    "memory_id": cid,
                    "parent_memory_id": parent_id,
                    "tenant_id": tenant_id,
                    "action": action,
                },
            )
            failed.append(cid)
            continue
        remediated += 1
    if remediated:
        # ``remediated``, not ``len(children)``: rows skipped above were not
        # remediated, and a count that includes them overstates enforcement in
        # the compliance log.
        logger.info(
            "governance: cascaded %s to %d derived row(s) of %s",
            action,
            remediated,
            parent_id,
        )
    _raise_if_any_failed(
        failed, parent_id=parent_id, what="dropped", outcome=RemediationOutcome(dropped=True)
    )


async def _privatise_children(
    sc: Any,
    children: list[dict],
    *,
    tenant_id: str,
    parent_id: str,
    detail_for: Any,
) -> None:
    """Downgrade each derived row's visibility alongside the parent's.

    A child left at ``scope_team`` republishes exactly the content
    ``keep_private`` just restricted (#808). Update-then-audit, mirroring the
    parent's non-destructive branch — nothing is lost if the audit fails after
    a visibility narrowing.

    Contained per child and raised once at the end, for the same reason as
    :func:`_drop_children`: the parent is already downgraded, so aborting on the
    first failure would leave the rest publishing what the policy restricted.

    Nothing retries a failed child here either — see :func:`_drop_children` for
    why the redelivery this used to rely on is gone. The consequence is milder
    on this branch: the update runs BEFORE the audit, so a failure leaves the
    row correctly narrowed but under-recorded, never recorded-but-unnarrowed.
    That is why the two branches order these operations oppositely.
    """
    remediated = 0
    failed: list[str] = []
    for child in children:
        cid = _child_id(child)
        if not cid:
            logger.error(
                "governance: derived row of %s has no usable id; it still "
                "publishes content the policy made private",
                parent_id,
            )
            failed.append("<no id>")
            continue
        try:
            await sc.update_memory(cid, tenant_id, {"visibility": "scope_agent"})
            detail = detail_for(child.get("content") or "")
            detail["cascaded_from"] = parent_id
            await emit_governance_audit(
                tenant_id=tenant_id,
                agent_id=child.get("agent_id"),
                action=ACTION_NB_KEEP_PRIVATE,
                detail=detail,
                resource_id=cid,
            )
        except Exception:
            logger.exception(
                "governance: could not cascade keep_private to derived row %s of %s; "
                "it still publishes content the policy made private",
                cid,
                parent_id,
            )
            failed.append(cid)
            continue
        remediated += 1
    if remediated:
        logger.info(
            "governance: cascaded keep_private to %d derived row(s) of %s",
            remediated,
            parent_id,
        )
    _raise_if_any_failed(
        failed,
        parent_id=parent_id,
        what="made private",
        outcome=RemediationOutcome(visibility="scope_agent"),
    )


async def _purge_entity_artifacts(sc: Any, memory_id: str, tenant_id: str, action: str) -> None:
    """Remove the graph rows mined out of a memory the policy just dropped.

    H-02. #808 established that a drop must stop the derived rows too, and
    named this case explicitly — "entities mined out of dropped content are the
    same leak in another table". It fixed the INLINE path, by ordering:
    ``_enrich_memory_background`` runs remediation first and its early return
    skips the entity extraction scheduled below it.

    Both non-inline paths schedule extraction independently, at write time, as
    a task that races the verdict — and extraction is one LLM call while the
    verdict needs enrichment plus an event round-trip, so extraction usually
    wins. ``process_entity_extraction`` never re-checked the row, and a
    soft-deleted memory still satisfies the link and relation foreign keys. So
    the names survived, listable tenant-wide through ``/entities`` and
    ``/graph``, with nothing tying them to the drop.

    Not guarded by a marker, unlike the child cascade: any dropped memory may
    have been extracted from, there is no flag on the row saying so, and the
    purge is three targeted deletes keyed on ``memory_id`` — cheap enough to
    run unconditionally rather than gate on something that could drift.

    Failures are LOGGED, not raised, and that is the opposite of what the rest
    of this module does — so it needs its reason stated.

    An earlier draft let this propagate "matching every other unapplied-policy
    path in this module". That was wrong about the caller. The other paths run
    under ``_enrich_memory_background``, where a raise becomes a
    ``BackgroundTaskLog`` row. This one also runs under
    ``consumer.handle_memory_enriched``, which has no guard around it, and the
    Pub/Sub dispatcher NACKS on a handler exception — a documented, load-bearing
    invariant. So a raise here redelivers the same event, re-runs the whole drop
    branch, and emits a SECOND ``critical=True`` audit for a memory that was
    already dropped. Repeatedly. Duplicate destructive entries in a
    tamper-evident log, for a row nothing further can be done to.

    The trade the other way is bounded: the memory itself is already gone, so
    the content is not live. What remains is graph rows, and an ERROR naming the
    memory is enough to purge them by hand. A transient failure does not even
    reach here — ``purge_entity_artifacts`` is marked idempotent, so the client
    retries 5xx and timeouts on its own.
    """
    try:
        counts = await sc.purge_entity_artifacts(tenant_id, memory_id)
    except Exception:
        logger.exception(
            "governance: %s dropped memory %s but its entity/relation rows were NOT "
            "removed; the names mined from that content are still listable and need "
            "purging by hand",
            action,
            memory_id,
        )
        return
    if any(counts.get(k) for k in ("links", "relations", "entities")):
        logger.info(
            "governance: %s purged graph rows for %s (links=%s relations=%s entities=%s)",
            action,
            memory_id,
            counts.get("links"),
            counts.get("relations"),
            counts.get("entities"),
        )


async def remediate_after_enrichment(memory: dict, cfg: Any) -> RemediationOutcome:
    """Apply LLM-signal governance to a fast-mode row after enrichment landed.

    Returns what was done. A caller that only needs "should I stop?" reads
    ``.dropped``; one that creates derived rows must also honour
    ``.visibility``. No-op when governance is disabled or the signals are clean.

    MAY RAISE ``GovernanceCascadeError`` after the parent's own remediation has
    already succeeded — when rows derived from it could not be cleaned up. That
    is deliberate rather than an oversight in the return contract: a caller
    about to create MORE derived rows must not proceed when the existing ones
    still hold content the policy forbade, and aborting is the fail-safe answer.
    The exception carries the parent's ``outcome`` for any caller that needs to
    tell "nothing happened" from "the parent was handled, the cleanup was not".
    """
    pii_cfg = cfg.governance_pii
    nb_cfg = cfg.governance_non_business
    if not pii_cfg.enabled and not nb_cfg.enabled:
        return RemediationOutcome()

    md = memory.get("metadata_") or memory.get("metadata") or {}
    content = memory.get("content") or ""
    tenant_id = memory.get("tenant_id")
    agent_id = memory.get("agent_id")
    raw_id = memory.get("id")
    if raw_id is None:
        # A malformed enriched-event payload without an id would otherwise
        # soft-delete "None" and stamp resource_id="None" on every audit row.
        logger.warning("governance: remediate_after_enrichment called with memory missing 'id'; skipping")
        return RemediationOutcome()
    memory_id = str(raw_id)
    sc = get_storage_client()

    # ── PII (LLM free-form signal) ──
    if pii_cfg.enabled and md.get("contains_pii"):
        pii_types = md.get("pii_types") or []
        if pii_cfg.action == "drop":
            # Resolved BEFORE the parent's audit and delete: a lookup failure
            # then leaves everything intact and remediable, rather than a
            # dropped parent whose children were never found (H-10).
            children = await _pre_verdict_children(sc, memory_id, tenant_id, md)
            # Audit BEFORE the destructive delete (mirrors GovernanceScanContent's
            # audit-before-mutate): a delete that succeeds before a failing audit
            # would leave an untracked deletion in the tamper-evident log, whereas
            # an audit-then-failed-delete leaves a remediable "intended to drop" trace.
            await emit_governance_audit(
                tenant_id=tenant_id,
                agent_id=agent_id,
                action=ACTION_PII_DROP,
                detail=llm_pii_audit_detail(ACTION_PII_DROP, pii_types, content, "fast"),
                resource_id=memory_id,
                # Destructive: the soft-delete below removes the row, so this
                # audit is the only trace — must survive queue overflow.
                critical=True,
            )
            await sc.soft_delete_memory(memory_id, tenant_id)
            logger.info("governance: dropped fast-mode memory %s (pii)", memory_id)
            # Purge BEFORE the cascade, not after. ``_drop_children`` raises once
            # any child fails, so ordering it first would skip this parent's own
            # graph rows on exactly the runs where something already went wrong.
            # The purge cannot raise (it logs), so it never blocks the cascade.
            await _purge_entity_artifacts(sc, memory_id, tenant_id, ACTION_PII_DROP)
            await _drop_children(
                sc,
                children,
                tenant_id=tenant_id,
                parent_id=memory_id,
                action=ACTION_PII_DROP,
                detail_for=lambda c: llm_pii_audit_detail(ACTION_PII_DROP, pii_types, c, "fast"),
            )
            return RemediationOutcome(dropped=True)
        # mask/flag: the LLM gives no offsets to redact a free-form span, and in
        # fast mode the row is already persisted — so a "mask"-configured tenant
        # can only be flagged here. Keep the action truthful (flag), but record
        # the configured intent in the detail so compliance can tell this apart
        # from a genuine flag policy.
        await emit_governance_audit(
            tenant_id=tenant_id,
            agent_id=agent_id,
            action=ACTION_PII_FLAG,
            detail=llm_pii_audit_detail(
                ACTION_PII_FLAG,
                pii_types,
                content,
                "fast",
                configured_action=ACTION_PII_MASK if pii_cfg.action == "mask" else None,
            ),
            resource_id=memory_id,
        )

    # ── Business-vs-personal disposition ──
    if nb_cfg.enabled and md.get("business_relevance") == "personal":
        if nb_cfg.disposition == "drop":
            # Before the parent's audit and delete — see the PII-drop branch.
            children = await _pre_verdict_children(sc, memory_id, tenant_id, md)
            # Audit before the destructive delete (see the PII-drop branch above).
            await emit_governance_audit(
                tenant_id=tenant_id,
                agent_id=agent_id,
                action=ACTION_NB_DROP,
                detail=nonbusiness_audit_detail(ACTION_NB_DROP, content, "fast"),
                resource_id=memory_id,
                # Destructive: see the PII-drop branch — audit is the only trace.
                critical=True,
            )
            await sc.soft_delete_memory(memory_id, tenant_id)
            logger.info("governance: dropped fast-mode memory %s (non-business)", memory_id)
            # Purge before the cascade — see the PII-drop branch above.
            await _purge_entity_artifacts(sc, memory_id, tenant_id, ACTION_NB_DROP)
            await _drop_children(
                sc,
                children,
                tenant_id=tenant_id,
                parent_id=memory_id,
                action=ACTION_NB_DROP,
                detail_for=lambda c: nonbusiness_audit_detail(ACTION_NB_DROP, c, "fast"),
            )
            return RemediationOutcome(dropped=True)
        if nb_cfg.disposition == "keep_private":
            children = await _pre_verdict_children(sc, memory_id, tenant_id, md)
            await sc.update_memory(memory_id, tenant_id, {"visibility": "scope_agent"})
            # The parent's OWN audit comes before the cascade, and the ordering
            # is load-bearing rather than tidy. ``_privatise_children`` raises
            # when a child fails, so with the cascade in between, one failed
            # child left the parent durably narrowed with NO audit row at all —
            # an untracked mutation, which this module's own docstrings forbid.
            #
            # The drop branches never had that exposure: they audit the parent
            # before the destructive delete, so the cascade is already the last
            # thing they do. This branch mutates first (nothing is lost if a
            # non-destructive audit fails after), which put the natural place
            # for the audit AFTER the mutation — and dropping the cascade in
            # between quietly moved it behind a raise.
            await emit_governance_audit(
                tenant_id=tenant_id,
                agent_id=agent_id,
                action=ACTION_NB_KEEP_PRIVATE,
                detail=nonbusiness_audit_detail(ACTION_NB_KEEP_PRIVATE, content, "fast"),
                resource_id=memory_id,
            )
            await _privatise_children(
                sc,
                children,
                tenant_id=tenant_id,
                parent_id=memory_id,
                detail_for=lambda c: nonbusiness_audit_detail(ACTION_NB_KEEP_PRIVATE, c, "fast"),
            )
            # Reported so a caller creating derived rows can carry the
            # downgrade to them instead of publishing the same content wider.
            return RemediationOutcome(visibility="scope_agent")
    return RemediationOutcome()
