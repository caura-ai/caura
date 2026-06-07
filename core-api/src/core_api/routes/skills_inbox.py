"""Skills Inbox — HITL endpoints (SF-206 + SF-207).

The Inbox UI lives here as a tight read+act surface over the
``skills`` collection. There is no new persistence — every Inbox
action is a status transition (or in the case of Edit, a content
revision) on the existing skill doc.

Endpoints (all under ``/v1/skills-inbox``):

  GET    /                       — list staged candidates
  POST   /{slug}/approve         — staged → active   (+ pre-apply rescan)
  POST   /{slug}/reject          — staged → rejected (+ poison-table write)
  POST   /{slug}/quarantine      — staged → quarantined  (security review)
  POST   /{slug}/defer           — no-op; stamps ``deferred_at`` (Forge can revise)
  POST   /{slug}/edit            — revise content / description / summary;
                                   rehash + rescan; stays staged

Phase-2 scope (per plan §15): the 5 actions land status transitions.
Phase 3 wires the actual harness install on ``staged → active`` —
this route still flips status; the Phase-3 install worker watches the
status flip and emits SKILL.md files.

All endpoints require the flag
``org_settings.skills_factory.enabled == True``; if disabled they
respond with ``403 SKILLS_FACTORY_DISABLED`` so a curious operator
gets a clear error instead of a silent 404.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from core_api.auth import AuthContext, get_auth_context
from core_api.clients.storage_client import get_storage_client
from core_api.db.session import get_db
from core_api.services.audit_service import log_action
from core_api.services.forge.poison import write_rejected_fingerprint
from core_api.services.forge.sentinel_scan import scan_skill_doc
from core_api.services.organization_settings import (
    get_raw_settings,
    get_settings_for_display,
)
from core_api.services.skill_lifecycle import (
    SkillWriteContext,
    validate_and_normalize_skill_write,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/skills-inbox", tags=["Skill Factory · Inbox"])


SKILLS_COLLECTION = "skills"


# ── Flag gate ──────────────────────────────────────────────────────


async def _require_skills_factory_enabled(db: AsyncSession, tenant_id: str) -> dict:
    """Hot-path: read the raw settings row and short-circuit when the
    feature flag is off. Returns the resolved settings-for-display
    dict so each endpoint has the per-tenant caps in one fetch.
    """
    raw = await get_raw_settings(db, tenant_id)
    enabled = (
        isinstance(raw, dict)
        and isinstance(raw.get("skills_factory"), dict)
        and bool(raw["skills_factory"].get("enabled"))
    )
    if not enabled:
        raise HTTPException(
            status_code=403,
            detail="SKILLS_FACTORY_DISABLED — set org_settings.skills_factory.enabled=true to use the inbox",
        )
    return await get_settings_for_display(db, tenant_id)


def _require_inbox_admin(auth: AuthContext) -> None:
    """Inbox MUTATING actions (approve/reject/quarantine/defer/edit)
    require admin privileges. The ``GET /`` list endpoint is left open
    to any tenant member so non-admin operators can still see what's
    in flight.

    Centralized so the check stays consistent across all five action
    handlers — a missed handler is a privilege-escalation bug.
    """
    if not getattr(auth, "is_admin", False):
        raise HTTPException(
            status_code=403,
            detail="SKILLS_INBOX_FORBIDDEN — inbox actions require admin privileges",
        )


# ── Pydantic shapes ────────────────────────────────────────────────


class InboxCard(BaseModel):
    """One row in the Inbox list response. Shape matches what the
    card-UI surfaces — keep the field list in sync with plan §10.
    """

    slug: str = Field(..., description="Skill slug (also doc_id, with optional forge/ prefix)")
    doc_id: str
    name: str | None = None
    description: str | None = None
    summary: str | None = None
    domain: str | None = None
    tags: list[str] = Field(default_factory=list)
    source: str | None = None
    status: str
    fingerprint: str | None = None
    scan_state: str | None = None
    scan_critical: int = 0
    scan_warn: int = 0
    origin: dict = Field(default_factory=dict)
    evidence: dict = Field(default_factory=dict)
    created_at: str | None = None
    content_hash: str | None = None
    kind: str | None = None
    target: dict | None = None
    # When set, this card was Deferred — Inbox sorts it to the bottom
    # so the queue surface stays focused on fresh actionable items.
    deferred_at: str | None = None


class InboxListResponse(BaseModel):
    tenant_id: str
    fleet_id: str | None
    count: int
    items: list[InboxCard]


class RejectRequest(BaseModel):
    reason: str = Field(..., min_length=1, max_length=2000)
    cooloff_days: int | None = Field(
        default=None,
        ge=1,
        le=365,
        description="Override poison-table cooloff. Defaults to org_settings.skills_factory.rejection_cooloff_days.",
    )


class QuarantineRequest(BaseModel):
    reason: str = Field(..., min_length=1, max_length=2000)


class DeferRequest(BaseModel):
    reason: str | None = Field(default=None, max_length=2000)


class EditRequest(BaseModel):
    content: str | None = None
    description: str | None = None
    summary: str | None = None

    def has_changes(self) -> bool:
        return any(v is not None for v in (self.content, self.description, self.summary))


class ActionResponse(BaseModel):
    slug: str
    previous_status: str
    new_status: str
    detail: str | None = None


# ── Helpers ────────────────────────────────────────────────────────


def _card_from_doc(doc: dict) -> InboxCard:
    data = doc.get("data") or {}
    scan = data.get("scan") or {}
    return InboxCard(
        slug=data.get("slug") or doc.get("doc_id"),
        doc_id=doc.get("doc_id"),
        name=data.get("name"),
        description=data.get("description"),
        summary=data.get("summary"),
        domain=data.get("domain"),
        tags=data.get("tags") or [],
        source=data.get("source"),
        status=data.get("status", ""),
        fingerprint=data.get("fingerprint"),
        scan_state=scan.get("state"),
        scan_critical=scan.get("critical", 0),
        scan_warn=scan.get("warn", 0),
        origin=data.get("origin") or {},
        evidence=data.get("evidence") or {},
        created_at=data.get("created_at"),
        content_hash=data.get("content_hash"),
        kind=data.get("kind"),
        target=data.get("target"),
        deferred_at=data.get("deferred_at"),
    )


async def _load_doc_or_404(*, tenant_id: str, slug: str) -> dict:
    sc = get_storage_client()
    doc = await sc.get_document(
        tenant_id=tenant_id,
        collection=SKILLS_COLLECTION,
        doc_id=slug,
    )
    if doc is None:
        raise HTTPException(status_code=404, detail=f"skill {slug!r} not found")
    return doc


async def _reload_and_assert_status(
    *,
    tenant_id: str,
    slug: str,
    expected_statuses: set[str],
) -> dict:
    """TOCTOU guard: re-fetch the doc just before mutating it, and
    raise 409 if its status changed since the handler's initial load.

    Every Inbox action follows the same shape: load → do work
    (rescan / validate / poison-write) → mutate. Between the initial
    load and the mutation, a concurrent operator (or the lifecycle
    promoter worker) may have moved the doc — without this guard, two
    racing approves both flip ``staged → active`` and the second one
    silently re-clobbers the doc; a race between Approve and Reject
    leaves the poison row + an ``active`` doc.
    """
    doc = await _load_doc_or_404(tenant_id=tenant_id, slug=slug)
    current_status = (doc.get("data") or {}).get("status")
    if current_status not in expected_statuses:
        raise HTTPException(
            status_code=409,
            detail=(
                f"skill {slug!r} was concurrently transitioned to "
                f"status={current_status!r} (expected one of {sorted(expected_statuses)}); "
                f"reload and retry"
            ),
        )
    return doc


async def _persist_status_transition(
    *,
    tenant_id: str,
    fleet_id: str | None,
    slug: str,
    doc: dict,
    new_status: str,
    extra_data_patches: dict | None = None,
    remove_keys: tuple[str, ...] = (),
) -> tuple[str, dict]:
    """Patch ``data.status`` (plus any extras) and upsert. Returns
    ``(previous_status, new_data)`` for audit + response shaping.

    ``remove_keys`` drops the named keys from ``data`` before the
    upsert — useful for clearing transient markers (e.g. clearing
    ``deferred_at`` when an Approve crystallizes the doc to active).
    """
    data = dict(doc.get("data") or {})
    previous_status = data.get("status", "")
    data["status"] = new_status
    now_iso = datetime.now(UTC).isoformat(timespec="seconds")
    data[f"{new_status}_at"] = now_iso
    if extra_data_patches:
        data.update(extra_data_patches)
    for key in remove_keys:
        data.pop(key, None)
    sc = get_storage_client()
    await sc.upsert_document(
        {
            "tenant_id": tenant_id,
            "fleet_id": fleet_id,
            "collection": SKILLS_COLLECTION,
            "doc_id": slug,
            "data": data,
        }
    )
    return previous_status, data


# ── Endpoints ──────────────────────────────────────────────────────


@router.get("/", response_model=InboxListResponse)
async def list_inbox(
    fleet_id: str | None = None,
    # Validated at the FastAPI layer: 1 ≤ limit ≤ 200. A bare ``int=50``
    # default would 200 on any non-negative input — including ``limit=0``
    # (silently empty list) and ``limit=10_000`` (DoS via wide query).
    limit: int = Query(50, ge=1, le=200),
    auth: AuthContext = Depends(get_auth_context),
    db: AsyncSession = Depends(get_db),
) -> InboxListResponse:
    """List ``status='staged'`` skill candidates for the tenant.

    Caps default to ``org_settings.skills_factory.inbox_max_pending``;
    beyond that, auto-defer is the relief valve (Phase 2 worker
    enforces).
    """
    settings = await _require_skills_factory_enabled(db, auth.tenant_id)
    max_pending = (
        ((settings.get("skills_factory") or {}).get("inbox_max_pending"))
        if isinstance(settings, dict)
        else None
    )
    # ``max_pending or limit`` would coerce ``max_pending=0`` (a
    # tenant explicitly muting the inbox) into "uncapped"; check
    # ``is not None`` so a zero cap actually caps.
    effective_limit = min(limit, max_pending if max_pending is not None else limit, 200)

    # Storage ``where`` is JSONB scalar equality on ``data->>key`` --
    # it does NOT filter the top-level ``fleet_id`` column. The
    # ``query_documents`` API has a separate top-level ``fleet_id``
    # parameter for that (see core-storage's ``document_query``).
    # Putting fleet_id in ``where`` only works if writers mirror
    # ``fleet_id`` into ``data``, which is brittle. Pass it as the
    # dedicated top-level parameter so we filter on the indexed
    # column directly.
    where: dict = {"status": "staged"}

    # The storage layer's ``where`` is JSONB scalar equality and does
    # NOT support an ``IS NULL`` predicate (see ``document_query`` in
    # core-storage's postgres_service), so we can't ask the DB to
    # split deferred vs non-deferred for us. To avoid the prior bug --
    # an older deferred doc consuming a page slot ahead of a fresh
    # candidate -- we OVERSAMPLE in a single query (capped at 2x the
    # effective limit, hard-capped at 400), partition in Python, and
    # take up-to-limit non-deferred FIRST, then fill remaining slots
    # with deferred. The deferred-at-bottom invariant holds for the
    # 2x window; an explicit DB-side priority sort would require
    # extending storage's ``order_by`` shape and is out of scope here.
    oversample_limit = min(effective_limit * 2, 400)
    query_body: dict = {
        "tenant_id": auth.tenant_id,
        "collection": SKILLS_COLLECTION,
        "where": where,
        "limit": oversample_limit,
        "offset": 0,
        "order_by": "created_at",
        # DESC so fresh candidates land at the front of the
        # oversample window. ASC would let an old deferred
        # backlog fill the window first and starve the page of
        # fresh items.
        "order": "desc",
    }
    if fleet_id is not None:
        query_body["fleet_id"] = fleet_id
    sc = get_storage_client()
    rows = await sc.query_documents(query_body)

    all_cards = [_card_from_doc(r) for r in rows or []]
    # Guard against ``oversample_limit == 0`` (tenant explicitly muted
    # the inbox via ``inbox_max_pending=0``); otherwise we'd log a
    # spurious "cap hit" warning on every empty list call.
    if oversample_limit > 0 and len(all_cards) >= oversample_limit:
        # The oversample window saturated -- there are more staged
        # candidates than the partition pass can see. We won't 500,
        # but the page is missing the tail; operators should narrow
        # by fleet or raise inbox_max_pending.
        logger.warning(
            "skill_inbox list: oversample cap hit (tenant=%s fleet=%s oversample_limit=%d); "
            "some staged candidates may not appear in this page",
            auth.tenant_id,
            fleet_id,
            oversample_limit,
        )
    active = [c for c in all_cards if not c.deferred_at]
    deferred = [c for c in all_cards if c.deferred_at]
    # Take non-deferred first up to effective_limit; backfill remaining
    # slots with deferred. This is the page the operator actually
    # works through — fresh candidates always surface before stashed
    # ones, regardless of which set is older by ``created_at``.
    items = active[:effective_limit]
    remaining = effective_limit - len(items)
    if remaining > 0:
        items.extend(deferred[:remaining])

    return InboxListResponse(
        tenant_id=auth.tenant_id,
        fleet_id=fleet_id,
        count=len(items),
        items=items,
    )


@router.post("/{slug:path}/approve", response_model=ActionResponse)
async def approve(
    slug: str,
    auth: AuthContext = Depends(get_auth_context),
    db: AsyncSession = Depends(get_db),
) -> ActionResponse:
    """Promote ``staged → active``. Pre-apply rescan via Sentinel
    blocks the transition if the doc became unsafe between propose
    and apply.
    """
    settings = await _require_skills_factory_enabled(db, auth.tenant_id)
    _require_inbox_admin(auth)
    sf = (settings or {}).get("skills_factory") if isinstance(settings, dict) else {}
    body_max = (sf or {}).get("body_max_bytes", 40_000)
    desc_max = (sf or {}).get("description_max_bytes", 160)

    # Initial cheap pre-flight: bail out fast if the doc is obviously
    # not in a staged state. The expensive Sentinel rescan only runs
    # against the doc we'll actually approve (see TOCTOU guard below).
    doc = await _load_doc_or_404(tenant_id=auth.tenant_id, slug=slug)
    data = doc.get("data") or {}
    if data.get("status") != "staged":
        raise HTTPException(
            status_code=409,
            detail=f"skill {slug!r} status={data.get('status')!r}; can only approve from 'staged'",
        )

    # TOCTOU guard FIRST — a concurrent Edit between the initial load
    # and the rescan would mean we scan the old content but stamp the
    # rescan result onto the new content (post-edit). Reload, then
    # scan the canonical doc.
    #
    # A second concurrent Edit between THIS reload and the upsert is
    # still theoretically possible; the storage layer's per-row
    # ordering keeps last write wins and an Edit during Approve is
    # the operator's prerogative anyway. We narrow the window from
    # "across rescan" to "across a single upsert", which is the
    # tightest we can get without a per-doc lock.
    doc = await _reload_and_assert_status(tenant_id=auth.tenant_id, slug=slug, expected_statuses={"staged"})
    data = doc.get("data") or {}
    # Snapshot the content_hash BEFORE the rescan. After the rescan
    # we check that the content hasn't drifted — a concurrent Edit
    # leaves status='staged' (the status guard wouldn't catch it) but
    # changes ``content`` + ``content_hash``. Without this check the
    # operator would persist a stale "clean" verdict on now-modified
    # (possibly injected) content.
    pre_scan_content_hash = data.get("content_hash")
    if not isinstance(pre_scan_content_hash, str) or not pre_scan_content_hash:
        # Fail closed: every Forge-written candidate gets a
        # ``content_hash`` via the validator (SF-002). A staged doc
        # without one is malformed and cannot be safely approved
        # because the drift guard below would degenerate to
        # ``None != None`` (always False) and silently pass.
        raise HTTPException(
            status_code=422,
            detail=(
                f"skill {slug!r} has no content_hash; cannot safely approve "
                f"(rerun Forge to re-derive, or reject the candidate)"
            ),
        )

    # Call ``scan_skill_doc`` directly so the allow-verdict AND the
    # ``data.scan`` payload come from the SAME ``ScanResult``. The
    # previous ``rescan_before_apply`` indirection dropped the
    # ``scanned_at``/``critical``/``warn``/``info`` counters from the
    # persisted scan block; ``as_doc_field()`` rehydrates them.
    scan_result = await scan_skill_doc(
        data, mode="pre-apply", body_max_bytes=body_max, description_max_bytes=desc_max
    )
    if not (scan_result.state == "clean" and not scan_result.any_fatal):
        raise HTTPException(
            status_code=422,
            detail=(
                f"pre-apply rescan refused (state={scan_result.state}): "
                f"{[(f.code, f.message) for f in scan_result.findings]}"
            ),
        )
    rescan_payload = scan_result.as_doc_field()

    # Third TOCTOU reload — catches Reject/Quarantine races (status
    # changed away from 'staged'). Plus the content_hash check below
    # catches Edit races (status stayed 'staged' but content changed).
    doc = await _reload_and_assert_status(tenant_id=auth.tenant_id, slug=slug, expected_statuses={"staged"})
    third_data = doc.get("data") or {}
    if third_data.get("content_hash") != pre_scan_content_hash:
        raise HTTPException(
            status_code=409,
            detail=(f"skill {slug!r} content was modified during the rescan; reload and retry approve"),
        )

    prev, new_data = await _persist_status_transition(
        tenant_id=auth.tenant_id,
        fleet_id=(doc or {}).get("fleet_id"),
        slug=slug,
        doc=doc,
        new_status="active",
        extra_data_patches={"scan": rescan_payload},
        # Approving crystallizes the doc to ``active``; clear the
        # transient defer markers so an active skill never carries
        # a stale "deferred_at" timestamp. Mirrors the same pop in
        # the edit handler.
        remove_keys=("deferred_at", "defer_reason"),
    )
    # Best-effort audit: the status transition already landed in
    # storage via the upsert above. Failing to write the audit row
    # should NOT 500 the operator — we log + swallow.
    try:
        await log_action(
            db,
            tenant_id=auth.tenant_id,
            action="skill_inbox_approve",
            resource_type="document",
            resource_id=doc.get("doc_id") or slug,
            detail={"slug": slug, "previous_status": prev},
        )
        await db.commit()
    except Exception:
        logger.error(
            "skill_inbox: audit log failed for approve slug=%s",
            slug,
            exc_info=True,
        )
    return ActionResponse(slug=slug, previous_status=prev, new_status="active")


@router.post("/{slug:path}/reject", response_model=ActionResponse)
async def reject(
    slug: str,
    body: RejectRequest,
    auth: AuthContext = Depends(get_auth_context),
    db: AsyncSession = Depends(get_db),
) -> ActionResponse:
    """Reject ``staged → rejected`` and write the cluster fingerprint
    to ``forge_rejected_fingerprints`` so the next Forge run skips
    that cluster for ``cooloff_days``.
    """
    settings = await _require_skills_factory_enabled(db, auth.tenant_id)
    _require_inbox_admin(auth)
    sf = (settings or {}).get("skills_factory") if isinstance(settings, dict) else {}
    default_cooloff = (sf or {}).get("rejection_cooloff_days", 30)
    # ``or`` would treat ``cooloff_days=0`` (operator intent: don't
    # cool off at all) as "fall back to default". Pydantic's ``ge=1``
    # makes 0 unreachable today, but ``is not None`` is the future-
    # safe shape and matches the rest of this module.
    cooloff = body.cooloff_days if body.cooloff_days is not None else default_cooloff

    doc = await _load_doc_or_404(tenant_id=auth.tenant_id, slug=slug)
    data = doc.get("data") or {}
    if data.get("status") not in {"staged", "candidate", "quarantined"}:
        raise HTTPException(
            status_code=409,
            detail=f"skill {slug!r} status={data.get('status')!r}; can only reject from staged/candidate/quarantined",
        )
    fingerprint = data.get("fingerprint")
    if not isinstance(fingerprint, str) or not fingerprint:
        raise HTTPException(
            status_code=422,
            detail=f"skill {slug!r} has no fingerprint; cannot poison cluster",
        )

    # TOCTOU guard: re-fetch the doc and confirm it's still in a
    # rejectable status before we poison the cluster. Without this,
    # a concurrent Approve could flip the doc to ``active`` between
    # our initial load and this point — we'd then poison a cluster
    # that just shipped (and the next Forge run would refuse to
    # re-derive the now-deleted+re-needed skill for cooloff_days).
    doc = await _reload_and_assert_status(
        tenant_id=auth.tenant_id,
        slug=slug,
        expected_statuses={"staged", "candidate", "quarantined"},
    )
    # Re-derive fingerprint from the FRESH doc — an Edit may have
    # changed adjacent fields but content_hash + fingerprint stay
    # bound to the cluster identity, so this is belt-and-suspenders.
    data = doc.get("data") or {}
    fingerprint = data.get("fingerprint")
    if not isinstance(fingerprint, str) or not fingerprint:
        raise HTTPException(
            status_code=422,
            detail=f"skill {slug!r} has no fingerprint after reload; cannot poison cluster",
        )

    # Two writes live on two separate commit paths:
    #   1. ``write_rejected_fingerprint`` → SQLAlchemy session ``db``
    #   2. ``_persist_status_transition`` → storage-client HTTP upsert
    #
    # Ordering matters: we stage the poison INSERT, then do a third
    # TOCTOU reload BEFORE committing it. If the status check raises
    # 409 (e.g. a concurrent Approve flipped the doc to ``active``),
    # the exception propagates through SQLAlchemy's session and the
    # poison row gets rolled back — we never poison a cluster whose
    # skill just shipped. Only after the reload confirms the doc is
    # still rejectable do we commit the poison row, then issue the
    # HTTP upsert to flip status to ``rejected``.
    #
    # Worst-case ordering after commit: poison commits but the HTTP
    # upsert errors out. That leaves an extra poison row whose
    # cooloff is harmless (the doc still has its prior rejectable
    # status, the operator can retry, and the poison table tolerates
    # duplicate rows).
    await write_rejected_fingerprint(
        db,
        tenant_id=auth.tenant_id,
        fleet_id=doc.get("fleet_id"),
        cluster_fingerprint=fingerprint,
        rejected_by_agent=auth.agent_id or "unknown",
        reason=body.reason,
        cooloff_days=cooloff,
    )

    # Third TOCTOU guard — a concurrent Approve may have flipped the
    # doc to ``active`` between the prior reload and this point. The
    # check runs BEFORE ``db.commit()`` so a 409 here rolls the
    # poison row back via the session exit, leaving no durable
    # poison artifact for a cluster whose skill just shipped.
    doc = await _reload_and_assert_status(
        tenant_id=auth.tenant_id,
        slug=slug,
        expected_statuses={"staged", "candidate", "quarantined"},
    )
    await db.commit()

    prev, _ = await _persist_status_transition(
        tenant_id=auth.tenant_id,
        fleet_id=doc.get("fleet_id"),
        slug=slug,
        doc=doc,
        new_status="rejected",
        extra_data_patches={"rejection_reason": body.reason},
    )
    # Best-effort audit. The poison row already committed above and
    # the doc-status upsert already landed in storage; an audit-row
    # failure must not 500 a successful reject.
    try:
        await log_action(
            db,
            tenant_id=auth.tenant_id,
            action="skill_inbox_reject",
            resource_type="document",
            resource_id=doc.get("doc_id") or slug,
            detail={
                "slug": slug,
                "previous_status": prev,
                "cooloff_days": cooloff,
                "fingerprint": fingerprint,
            },
        )
        await db.commit()
    except Exception:
        logger.error(
            "skill_inbox: audit log failed for reject slug=%s",
            slug,
            exc_info=True,
        )
    return ActionResponse(
        slug=slug,
        previous_status=prev,
        new_status="rejected",
        detail=f"cluster fingerprint poisoned for {cooloff} days",
    )


@router.post("/{slug:path}/quarantine", response_model=ActionResponse)
async def quarantine(
    slug: str,
    body: QuarantineRequest,
    auth: AuthContext = Depends(get_auth_context),
    db: AsyncSession = Depends(get_db),
) -> ActionResponse:
    """Move to ``quarantined`` for security review. Does NOT touch the
    poison table — quarantine is reversible by a security admin; only
    Reject crystallizes a poison row.
    """
    await _require_skills_factory_enabled(db, auth.tenant_id)
    _require_inbox_admin(auth)
    doc = await _load_doc_or_404(tenant_id=auth.tenant_id, slug=slug)
    data = doc.get("data") or {}
    if data.get("status") not in {"staged", "candidate"}:
        raise HTTPException(
            status_code=409,
            detail=f"skill {slug!r} status={data.get('status')!r}; can only quarantine from staged/candidate",
        )
    # TOCTOU guard before the status flip.
    doc = await _reload_and_assert_status(
        tenant_id=auth.tenant_id, slug=slug, expected_statuses={"staged", "candidate"}
    )
    prev, _ = await _persist_status_transition(
        tenant_id=auth.tenant_id,
        fleet_id=doc.get("fleet_id"),
        slug=slug,
        doc=doc,
        new_status="quarantined",
        extra_data_patches={"quarantine_reason": body.reason},
    )
    # Best-effort audit; status transition already persisted.
    try:
        await log_action(
            db,
            tenant_id=auth.tenant_id,
            action="skill_inbox_quarantine",
            resource_type="document",
            resource_id=doc.get("doc_id") or slug,
            detail={"slug": slug, "previous_status": prev, "reason": body.reason},
        )
        await db.commit()
    except Exception:
        logger.error(
            "skill_inbox: audit log failed for quarantine slug=%s",
            slug,
            exc_info=True,
        )
    return ActionResponse(slug=slug, previous_status=prev, new_status="quarantined")


@router.post("/{slug:path}/defer", response_model=ActionResponse)
async def defer(
    slug: str,
    body: DeferRequest,
    auth: AuthContext = Depends(get_auth_context),
    db: AsyncSession = Depends(get_db),
) -> ActionResponse:
    """Defer — leaves the doc in ``staged`` so Forge can revise it on
    the next run. Stamps ``deferred_at`` so the inbox can sort
    deferred items to the bottom + show "deferred N days ago".
    """
    await _require_skills_factory_enabled(db, auth.tenant_id)
    _require_inbox_admin(auth)
    doc = await _load_doc_or_404(tenant_id=auth.tenant_id, slug=slug)
    data = doc.get("data") or {}
    if data.get("status") != "staged":
        raise HTTPException(
            status_code=409,
            detail=f"skill {slug!r} status={data.get('status')!r}; can only defer from 'staged'",
        )
    # TOCTOU guard before the deferred_at stamp.
    doc = await _reload_and_assert_status(tenant_id=auth.tenant_id, slug=slug, expected_statuses={"staged"})
    data = doc.get("data") or {}
    # Status stays 'staged'; only stamp deferred_at + optional reason.
    new_data = dict(data)
    new_data["deferred_at"] = datetime.now(UTC).isoformat(timespec="seconds")
    if body.reason:
        new_data["defer_reason"] = body.reason
    sc = get_storage_client()
    await sc.upsert_document(
        {
            "tenant_id": auth.tenant_id,
            "fleet_id": doc.get("fleet_id"),
            "collection": SKILLS_COLLECTION,
            "doc_id": slug,
            "data": new_data,
        }
    )
    # Best-effort audit; defer mark already persisted.
    try:
        await log_action(
            db,
            tenant_id=auth.tenant_id,
            action="skill_inbox_defer",
            resource_type="document",
            resource_id=doc.get("doc_id") or slug,
            detail={"slug": slug, "reason": body.reason},
        )
        await db.commit()
    except Exception:
        logger.error(
            "skill_inbox: audit log failed for defer slug=%s",
            slug,
            exc_info=True,
        )
    return ActionResponse(slug=slug, previous_status="staged", new_status="staged", detail="deferred")


@router.post("/{slug:path}/edit", response_model=ActionResponse)
async def edit(
    slug: str,
    body: EditRequest,
    auth: AuthContext = Depends(get_auth_context),
    db: AsyncSession = Depends(get_db),
) -> ActionResponse:
    """Edit content / description / summary, then rehash + rescan.
    Stays ``staged``. Plan §10 acceptance:

        "Edit + save → new content_hash, scan rerun, stays staged".

    Raw markdown only (per OQ-D — no WYSIWYG in MVP).
    """
    settings = await _require_skills_factory_enabled(db, auth.tenant_id)
    _require_inbox_admin(auth)
    sf = (settings or {}).get("skills_factory") if isinstance(settings, dict) else {}
    desc_max = (sf or {}).get("description_max_bytes", 160)
    body_max = (sf or {}).get("body_max_bytes", 40_000)

    if not body.has_changes():
        raise HTTPException(
            status_code=422,
            detail="edit requires at least one of content/description/summary",
        )

    # TOCTOU guard: edits are most likely to race against the
    # lifecycle promoter (candidate→staged) and against concurrent
    # Approve/Reject; we re-fetch to confirm the doc is still
    # mutable here.
    doc = await _reload_and_assert_status(tenant_id=auth.tenant_id, slug=slug, expected_statuses={"staged"})
    data = dict(doc.get("data") or {})
    if body.content is not None:
        data["content"] = body.content
    if body.description is not None:
        data["description"] = body.description
    if body.summary is not None:
        data["summary"] = body.summary

    # Re-run the validator — it recomputes content_hash + scan + size
    # caps; same code path as the original write so we get the same
    # guarantees.
    ctx = SkillWriteContext(
        caller_agent_id=auth.agent_id,
        is_admin=getattr(auth, "is_admin", False),
        is_internal_forge=False,
        description_max_bytes=desc_max,
        body_max_bytes=body_max,
    )
    # Pass the doc itself as ``live_skill_doc`` so kind='update'
    # hash-binding validates against the current persisted version
    # (the edit is a same-slug revision, not a new skill).
    normalized, scan = await validate_and_normalize_skill_write(data, ctx=ctx, live_skill_doc=doc)
    # Force status='staged' regardless of what the validator decided —
    # an Edit is not a re-write to 'candidate'.
    normalized["status"] = "staged"
    normalized["edited_at"] = datetime.now(UTC).isoformat(timespec="seconds")
    # An edit is an active intervention — clear the deferred marker
    # and any stale defer_reason so the doc resurfaces at the top of
    # the inbox sort (deferred items live at the bottom).
    normalized.pop("deferred_at", None)
    normalized.pop("defer_reason", None)

    sc = get_storage_client()
    await sc.upsert_document(
        {
            "tenant_id": auth.tenant_id,
            "fleet_id": doc.get("fleet_id"),
            "collection": SKILLS_COLLECTION,
            "doc_id": slug,
            "data": normalized,
        }
    )
    # Best-effort audit; edit already persisted via the upsert above.
    try:
        await log_action(
            db,
            tenant_id=auth.tenant_id,
            action="skill_inbox_edit",
            resource_type="document",
            resource_id=doc.get("doc_id") or slug,
            detail={
                "slug": slug,
                "content_hash": normalized.get("content_hash"),
                "scan_state": scan.state,
            },
        )
        await db.commit()
    except Exception:
        logger.error(
            "skill_inbox: audit log failed for edit slug=%s",
            slug,
            exc_info=True,
        )
    return ActionResponse(
        slug=slug,
        previous_status="staged",
        new_status="staged",
        detail=f"rehashed → {normalized.get('content_hash')}; scan={scan.state}",
    )
