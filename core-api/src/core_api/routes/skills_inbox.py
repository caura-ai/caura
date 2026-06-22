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
from typing import Any

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


async def _require_skills_factory_enabled(db: AsyncSession | None, tenant_id: str) -> dict:
    """Hot-path: read the raw settings row and short-circuit when the
    feature flag is off. Returns the resolved settings-for-display
    dict so each endpoint has the per-tenant caps in one fetch.

    ``db`` is forwarded to ``get_raw_settings`` / ``get_settings_for_display``,
    both of which ignore it (org settings route through core-storage-api as
    of Fix 2 Phase 0). The Ph5a ``/reject`` route passes ``None``; the other
    inbox routes still pass their request-scoped session.
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


def _require_tenant(auth: AuthContext) -> str:
    """Every inbox endpoint needs a concrete tenant. ``AuthContext.tenant_id``
    is typed ``str | None`` because some bootstrap paths land there
    pre-auth; the inbox is an authenticated route, so a missing tenant
    is a 401. Returning the narrowed ``str`` lets mypy verify the
    downstream calls without litter ``cast``s.
    """
    if not auth.tenant_id:
        raise HTTPException(
            status_code=401,
            detail="UNAUTHENTICATED — auth context has no tenant_id",
        )
    return auth.tenant_id


def _require_inbox_admin(auth: AuthContext) -> None:
    """Inbox MUTATING actions (approve/reject/quarantine/defer/edit)
    require admin privileges. The ``GET /`` list endpoint is left open
    to any tenant member so non-admin operators can still see what's
    in flight.

    Centralized so the check stays consistent across all five action
    handlers — a missed handler is a privilege-escalation bug.
    """
    # Mirror documents.py:215-216 — admin status may come from either
    # the legacy ``is_admin`` flag OR ``org_role == "admin"``. Keeping
    # both surfaces in lockstep means an operator authorized to write
    # admin-gated skills via ``memclaw_doc`` can also act on the inbox.
    is_admin = bool(getattr(auth, "is_admin", False)) or (getattr(auth, "org_role", None) == "admin")
    if not is_admin:
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
    # Coerce to ``str`` for the typed Pydantic model. Storage always
    # populates ``doc_id`` (it's the lookup key); the empty-string
    # fallbacks are defensive against malformed rows.
    #
    # ``slug`` must be the FULL ``doc_id`` (e.g. ``forge/abc``), not
    # the bare ``data.slug`` (``abc``), because every Inbox action
    # endpoint resolves the doc via ``doc_id=slug``. Forge writes
    # ``doc_id="forge/<slug>"`` while ``data.slug="<slug>"`` (bare);
    # if the card surfaced the bare slug, every action call would
    # 404 for Forge-namespaced candidates.
    doc_id: str = doc.get("doc_id") or ""
    slug: str = doc_id or data.get("slug") or ""
    return InboxCard(
        slug=slug,
        doc_id=doc_id,
        name=data.get("name"),
        description=data.get("description"),
        summary=data.get("summary"),
        domain=data.get("domain"),
        tags=data.get("tags") or [],
        source=data.get("source"),
        status=data.get("status", ""),
        fingerprint=data.get("cluster_fingerprint"),
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
    # Bump the indexable ``updated_at`` so every Inbox-driven status
    # transition (approve/reject/quarantine/defer/edit) becomes
    # discoverable via sort-by-modified-time. Without this, a doc that
    # transitions through the inbox retains the timestamp from its
    # original Forge write. Coexists with the per-status
    # ``<status>_at`` (human-readable intent) and ``edited_at`` (set
    # by the edit handler) — those tell you WHY, this tells you WHEN.
    data["updated_at"] = now_iso
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
    tenant_id = _require_tenant(auth)
    settings = await _require_skills_factory_enabled(db, tenant_id)
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
        "tenant_id": tenant_id,
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
            tenant_id,
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
        tenant_id=tenant_id,
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
    tenant_id = _require_tenant(auth)
    settings = await _require_skills_factory_enabled(db, tenant_id)
    _require_inbox_admin(auth)
    sf = (settings or {}).get("skills_factory") if isinstance(settings, dict) else {}
    body_max = (sf or {}).get("body_max_bytes", 40_000)
    desc_max = (sf or {}).get("description_max_bytes", 160)

    # Initial cheap pre-flight: bail out fast if the doc is obviously
    # not in a staged state. The expensive Sentinel rescan only runs
    # against the doc we'll actually approve (see TOCTOU guard below).
    doc = await _load_doc_or_404(tenant_id=tenant_id, slug=slug)
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
    doc = await _reload_and_assert_status(tenant_id=tenant_id, slug=slug, expected_statuses={"staged"})
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

    # Third TOCTOU reload — catches Reject/Quarantine races (status
    # changed away from 'staged'). Plus the content_hash check below
    # catches Edit races (status stayed 'staged' but content changed).
    doc = await _reload_and_assert_status(tenant_id=tenant_id, slug=slug, expected_statuses={"staged"})
    third_data = doc.get("data") or {}
    if third_data.get("content_hash") != pre_scan_content_hash:
        raise HTTPException(
            status_code=409,
            detail=(f"skill {slug!r} content was modified during the rescan; reload and retry approve"),
        )

    # Scan against ``third_data`` (the post-reload canonical doc), not
    # the earlier ``data`` snapshot. The reload above already proved
    # ``third_data.content_hash == pre_scan_content_hash`` so the
    # content bytes are guaranteed identical to what we snapshotted,
    # but scanning ``third_data`` is the honest shape: the ``data.scan``
    # payload we persist is computed against the bytes we're about to
    # crystallize. Call ``scan_skill_doc`` directly so the allow-verdict
    # AND the persisted ``data.scan`` come from the SAME ``ScanResult``
    # (``as_doc_field()`` rehydrates the full ``scanned_at`` / counters /
    # findings shape).
    scan_result = await scan_skill_doc(
        third_data, mode="pre-apply", body_max_bytes=body_max, description_max_bytes=desc_max
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

    prev, new_data = await _persist_status_transition(
        tenant_id=tenant_id,
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
            tenant_id=tenant_id,
            action="skill_inbox_approve",
            resource_type="document",
            # ``log_action`` types ``resource_id`` as ``UUID | None`` but
            # its runtime accepts any truthy value (it stringifies via
            # ``str(resource_id) if resource_id else None``). Slugs are
            # human-readable and grep-friendly in the audit log; keep
            # the directive intact and suppress the type warning.
            resource_id=doc.get("doc_id") or slug,  # type: ignore[arg-type]
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
) -> ActionResponse:
    """Reject ``staged → rejected`` and write the cluster fingerprint
    to ``forge_rejected_fingerprints`` so the next Forge run skips
    that cluster for ``cooloff_days``.

    Fix 2 Ph5a: the poison write goes through core-storage-api
    (``write_rejected_fingerprint`` → ``sc.forge_write_rejected_fingerprint``)
    rather than a request-scoped DB session, so this route no longer
    depends on ``get_db`` (the settings gate + audit log already ignore
    their ``db`` arg and route through storage).
    """
    tenant_id = _require_tenant(auth)
    settings = await _require_skills_factory_enabled(None, tenant_id)
    _require_inbox_admin(auth)
    sf = (settings or {}).get("skills_factory") if isinstance(settings, dict) else {}
    default_cooloff = (sf or {}).get("rejection_cooloff_days", 30)
    # ``or`` would treat ``cooloff_days=0`` (operator intent: don't
    # cool off at all) as "fall back to default". Pydantic's ``ge=1``
    # makes 0 unreachable today, but ``is not None`` is the future-
    # safe shape and matches the rest of this module.
    cooloff = body.cooloff_days if body.cooloff_days is not None else default_cooloff

    doc = await _load_doc_or_404(tenant_id=tenant_id, slug=slug)
    data = doc.get("data") or {}
    if data.get("status") not in {"staged", "candidate", "quarantined"}:
        raise HTTPException(
            status_code=409,
            detail=f"skill {slug!r} status={data.get('status')!r}; can only reject from staged/candidate/quarantined",
        )
    fingerprint = data.get("cluster_fingerprint")
    if not isinstance(fingerprint, str) or not fingerprint:
        raise HTTPException(
            status_code=422,
            detail=f"skill {slug!r} has no fingerprint; cannot poison cluster",
        )

    # TOCTOU guard: re-fetch the doc and confirm it's still in a
    # rejectable status BEFORE we poison the cluster. Without this,
    # a concurrent Approve could flip the doc to ``active`` between
    # our initial load and this point — we'd then poison a cluster
    # that just shipped (and the next Forge run would refuse to
    # re-derive the now-deleted+re-needed skill for cooloff_days).
    #
    # Ph5a NOTE: the poison write now commits storage-side immediately
    # (no shared SQLAlchemy transaction to roll back), so the pre-Ph5a
    # "stage INSERT → reload → commit-or-rollback" dance is gone. We
    # instead do BOTH reload guards up front and only issue the poison
    # write once the doc is confirmed rejectable. The residual race
    # (a concurrent Approve landing between this final reload and the
    # poison write) leaves at most one harmless extra poison row — the
    # exact worst case the pre-Ph5a code already documented and
    # tolerated (the poison table dedups nothing and the cooloff on a
    # shipped cluster is benign).
    doc = await _reload_and_assert_status(
        tenant_id=tenant_id,
        slug=slug,
        expected_statuses={"staged", "candidate", "quarantined"},
    )
    # Re-derive fingerprint from the FRESH doc — an Edit may have
    # changed adjacent fields but content_hash + fingerprint stay
    # bound to the cluster identity, so this is belt-and-suspenders.
    data = doc.get("data") or {}
    fingerprint = data.get("cluster_fingerprint")
    if not isinstance(fingerprint, str) or not fingerprint:
        raise HTTPException(
            status_code=422,
            detail=f"skill {slug!r} has no fingerprint after reload; cannot poison cluster",
        )

    # Second TOCTOU reload — narrows the window before the poison write.
    doc = await _reload_and_assert_status(
        tenant_id=tenant_id,
        slug=slug,
        expected_statuses={"staged", "candidate", "quarantined"},
    )

    try:
        await write_rejected_fingerprint(
            tenant_id=tenant_id,
            fleet_id=doc.get("fleet_id"),
            cluster_fingerprint=fingerprint,
            rejected_by_agent=auth.agent_id or "unknown",
            reason=body.reason,
            cooloff_days=cooloff,
        )
    except ValueError as exc:
        # ``write_rejected_fingerprint`` raises ValueError on cooloff_days < 1
        # or an empty fingerprint. Pydantic's ``ge=1`` on the request body
        # catches the former, but a stale org_settings.rejection_cooloff_days
        # could still inject 0; surface as 422 rather than 500.
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    try:
        prev, _ = await _persist_status_transition(
            tenant_id=tenant_id,
            fleet_id=doc.get("fleet_id"),
            slug=slug,
            doc=doc,
            new_status="rejected",
            extra_data_patches={"rejection_reason": body.reason},
        )
    except Exception:
        # The poison row already committed storage-side (no shared txn to roll
        # back). If the status flip fails HERE, the cluster is poisoned for
        # cooloff_days while the doc still reads as a rejectable status — an
        # inconsistent state an operator must reconcile by hand. Surface it
        # loudly rather than letting it read as a generic 500, then re-raise.
        logger.error(
            "skill_inbox: reject status-flip FAILED after poison write for slug=%s — "
            "the poison row is committed but the doc was NOT flipped to 'rejected'; "
            "the cluster is silently blocked for %d days. Manual intervention required.",
            slug,
            cooloff,
            exc_info=True,
        )
        raise
    # Best-effort audit. The poison row already committed storage-side and
    # the doc-status upsert already landed in storage; an audit-row
    # failure must not 500 a successful reject.
    try:
        await log_action(
            None,
            tenant_id=tenant_id,
            action="skill_inbox_reject",
            resource_type="document",
            # ``log_action`` types ``resource_id`` as ``UUID | None`` but
            # its runtime accepts any truthy value (it stringifies via
            # ``str(resource_id) if resource_id else None``). Slugs are
            # human-readable and grep-friendly in the audit log; keep
            # the directive intact and suppress the type warning.
            resource_id=doc.get("doc_id") or slug,  # type: ignore[arg-type]
            detail={
                "slug": slug,
                "previous_status": prev,
                "cooloff_days": cooloff,
                "fingerprint": fingerprint,
            },
        )
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
    tenant_id = _require_tenant(auth)
    await _require_skills_factory_enabled(db, tenant_id)
    _require_inbox_admin(auth)
    doc = await _load_doc_or_404(tenant_id=tenant_id, slug=slug)
    data = doc.get("data") or {}
    if data.get("status") not in {"staged", "candidate"}:
        raise HTTPException(
            status_code=409,
            detail=f"skill {slug!r} status={data.get('status')!r}; can only quarantine from staged/candidate",
        )
    # TOCTOU guard before the status flip.
    doc = await _reload_and_assert_status(
        tenant_id=tenant_id, slug=slug, expected_statuses={"staged", "candidate"}
    )
    prev, _ = await _persist_status_transition(
        tenant_id=tenant_id,
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
            tenant_id=tenant_id,
            action="skill_inbox_quarantine",
            resource_type="document",
            # ``log_action`` types ``resource_id`` as ``UUID | None`` but
            # its runtime accepts any truthy value (it stringifies via
            # ``str(resource_id) if resource_id else None``). Slugs are
            # human-readable and grep-friendly in the audit log; keep
            # the directive intact and suppress the type warning.
            resource_id=doc.get("doc_id") or slug,  # type: ignore[arg-type]
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
    tenant_id = _require_tenant(auth)
    await _require_skills_factory_enabled(db, tenant_id)
    _require_inbox_admin(auth)
    doc = await _load_doc_or_404(tenant_id=tenant_id, slug=slug)
    data = doc.get("data") or {}
    if data.get("status") != "staged":
        raise HTTPException(
            status_code=409,
            detail=f"skill {slug!r} status={data.get('status')!r}; can only defer from 'staged'",
        )
    # TOCTOU guard before the deferred_at stamp.
    doc = await _reload_and_assert_status(tenant_id=tenant_id, slug=slug, expected_statuses={"staged"})
    data = doc.get("data") or {}
    # Status stays 'staged'; only stamp deferred_at + optional reason.
    new_data = dict(data)
    new_data["deferred_at"] = datetime.now(UTC).isoformat(timespec="seconds")
    if body.reason:
        new_data["defer_reason"] = body.reason
    # Defer doesn't transition status (stays ``staged``), so it never
    # reaches ``_persist_status_transition``'s ``updated_at`` bump.
    # Stamp it here so a Deferred-but-not-status-changed doc still
    # surfaces correctly in sort-by-modified-time queries.
    new_data["updated_at"] = new_data["deferred_at"]
    sc = get_storage_client()
    await sc.upsert_document(
        {
            "tenant_id": tenant_id,
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
            tenant_id=tenant_id,
            action="skill_inbox_defer",
            resource_type="document",
            # ``log_action`` types ``resource_id`` as ``UUID | None`` but
            # its runtime accepts any truthy value (it stringifies via
            # ``str(resource_id) if resource_id else None``). Slugs are
            # human-readable and grep-friendly in the audit log; keep
            # the directive intact and suppress the type warning.
            resource_id=doc.get("doc_id") or slug,  # type: ignore[arg-type]
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
    tenant_id = _require_tenant(auth)
    settings = await _require_skills_factory_enabled(db, tenant_id)
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
    doc = await _reload_and_assert_status(tenant_id=tenant_id, slug=slug, expected_statuses={"staged"})
    data = dict(doc.get("data") or {})
    if body.content is not None:
        data["content"] = body.content
    if body.description is not None:
        data["description"] = body.description
    if body.summary is not None:
        data["summary"] = body.summary

    # Snapshot the server-controlled / RBAC-gated fields BEFORE
    # validation. The validator's SF-002 RBAC checks would 403 a
    # non-admin operator editing a Forge-minted candidate because
    # it carries ``source='forge'`` (which only the internal Forge
    # writer is allowed to set). The Inbox edit only edits the
    # human-facing content fields; the rest survive untouched.
    # Restored onto ``normalized`` after validation so the upsert
    # writes back what we read in.
    # ``source`` is INTENTIONALLY NOT in this list. The validator
    # requires ``source`` in REQUIRED_TOP_LEVEL_KEYS and would 422 if
    # we stripped it. Instead, the validator now respects
    # ``ctx.is_inbox_edit=True`` (set below) which bypasses the
    # source-RBAC mint-gate so a Forge-minted candidate can be edited
    # without 403-ing on ``source='forge'``. The other fields below
    # are server-controlled (no validator surface) — strip + restore
    # cleanly around the validator call.
    _RESERVED_FIELDS = (
        "status",
        "cluster_fingerprint",
        "cites",
        "origin",
        "created_at",
        "telemetry",
    )
    reserved_snapshot: dict[str, Any] = {k: data[k] for k in _RESERVED_FIELDS if k in data}
    for k in _RESERVED_FIELDS:
        data.pop(k, None)

    # Re-run the validator — it recomputes content_hash + scan + size
    # caps; same code path as the original write so we get the same
    # guarantees on the EDITABLE fields. ``is_admin`` mirrors
    # ``_require_inbox_admin``'s two-part check (is_admin flag OR
    # org_role='admin') so the validator's admin-only branches
    # (e.g. setting ``source='forge'`` for re-installs) stay
    # consistent with what the surrounding endpoint allows.
    ctx = SkillWriteContext(
        caller_agent_id=auth.agent_id,
        is_admin=bool(getattr(auth, "is_admin", False)) or (getattr(auth, "org_role", None) == "admin"),
        is_internal_forge=False,
        description_max_bytes=desc_max,
        body_max_bytes=body_max,
        # Tell the validator this is an inbox edit (preserves
        # existing ``source``, doesn't mint a new one). Without this
        # flag the validator's source-RBAC would 403 every Forge-
        # minted candidate's edit because ``source='forge'`` is
        # INTERNAL_ONLY and our caller is the operator (not the
        # internal Forge writer). Admin enforcement is upstream via
        # ``_require_inbox_admin``.
        is_inbox_edit=True,
    )
    # For ``kind='update'`` candidates, hash-binding must validate
    # against the live TARGET skill (a separate doc identified by
    # ``data.target.slug``), NOT the candidate itself. Passing the
    # candidate as its own ``live_skill_doc`` would let
    # ``target.target_content_hash`` self-match and silently bypass
    # the binding. For ``kind='create'`` the validator ignores
    # ``live_skill_doc``, so ``None`` is the safe default.
    live_for_binding: dict | None = None
    if data.get("kind") == "update":
        target_slug = (data.get("target") or {}).get("slug")
        if target_slug:
            sc_binding = get_storage_client()
            live_for_binding = await sc_binding.get_document(
                tenant_id=tenant_id,
                collection=SKILLS_COLLECTION,
                doc_id=target_slug,
            )
    normalized, scan = await validate_and_normalize_skill_write(
        data, ctx=ctx, live_skill_doc=live_for_binding
    )
    # Capture the validator's quarantine verdict BEFORE the reserved
    # snapshot restoration. The snapshot contains the pre-edit
    # ``status`` (typically ``"staged"``); without this capture the
    # restoration loop overwrites a fresh Sentinel ``"quarantined"``
    # verdict and the quarantine guard below would never fire.
    quarantine_triggered = normalized.get("status") == "quarantined"
    quarantined_at_val = normalized.get("quarantined_at")

    # Restore the server-controlled snapshot — these survive the
    # validation round-trip unchanged. ``status`` may be overwritten
    # immediately below by the quarantine guard.
    for k, v in reserved_snapshot.items():
        normalized[k] = v
    # Force status='staged' UNLESS the validator's Sentinel pass found
    # non-fatal critical content (prompt injection, shell injection).
    # Overwriting that unconditionally would route quarantined content
    # back into the staged inbox where Approve might still attempt it
    # (the approve rescan would block, but the security-review queue
    # is the correct surface).
    if quarantine_triggered:
        normalized["status"] = "quarantined"
        if quarantined_at_val:
            normalized["quarantined_at"] = quarantined_at_val
    else:
        normalized["status"] = "staged"
    normalized["edited_at"] = datetime.now(UTC).isoformat(timespec="seconds")
    # An edit is an active intervention — clear the deferred marker
    # and any stale defer_reason so the doc resurfaces at the top of
    # the inbox sort (deferred items live at the bottom).
    normalized.pop("deferred_at", None)
    normalized.pop("defer_reason", None)

    # Second TOCTOU guard — between handler entry and now, the
    # validator ran (potentially slow on prompt-injection regexes);
    # a concurrent Approve/Reject may have moved the doc. Without
    # this reload, edit's upsert would silently revert a freshly-
    # ``active`` doc back to ``staged``.
    doc = await _reload_and_assert_status(tenant_id=tenant_id, slug=slug, expected_statuses={"staged"})
    sc = get_storage_client()
    await sc.upsert_document(
        {
            "tenant_id": tenant_id,
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
            tenant_id=tenant_id,
            action="skill_inbox_edit",
            resource_type="document",
            # ``log_action`` types ``resource_id`` as ``UUID | None`` but
            # its runtime accepts any truthy value (it stringifies via
            # ``str(resource_id) if resource_id else None``). Slugs are
            # human-readable and grep-friendly in the audit log; keep
            # the directive intact and suppress the type warning.
            resource_id=doc.get("doc_id") or slug,  # type: ignore[arg-type]
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
    # ``new_status`` reflects what we actually persisted — when the
    # validator's Sentinel pass quarantined the doc, the upsert wrote
    # ``status='quarantined'`` and the response must say so.
    return ActionResponse(
        slug=slug,
        previous_status="staged",
        new_status=normalized.get("status", "staged"),
        detail=f"rehashed → {normalized.get('content_hash')}; scan={scan.state}",
    )
