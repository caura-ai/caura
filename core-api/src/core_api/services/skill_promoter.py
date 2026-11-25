"""Skill promoter — lifecycle worker (SF-205).

One tick = scan all ``status='candidate'`` skill docs for a tenant /
fleet, evaluate the 6 auto-gates against each, and promote the ones
that pass to ``status='staged'``.

This is *intentionally* a stateless service module rather than a
long-running daemon: callers (scheduled cron, manual CLI invocation,
post-Forge hook) drive it. Each tick is idempotent — running it twice
back-to-back is a no-op on the second run (candidates that passed in
the first run are now ``staged`` and the query skips them).

Plan §15 Phase 2 deliverable:

  "Sentinel scanner pre-screens; auto-gates evaluator promotes
   candidate → staged when all 6 gates pass."

The companion pre-apply scan helper (``rescan_before_apply``) remains
available to callers. Activation itself requires the Inbox route, which
rescans the current bytes, persists the full ``ScanResult`` and requires
explicit attributable review. Staging never skips that review.

External callable injection (mirrors Forge's pattern):

  * ``poison_checker``    — (tenant, fleet, fp) → bool
  * ``live_data_fetcher`` — (tenant, collection, doc_id) → live data
  * ``status_updater``    — (tenant, collection, doc_id, new_status) → None
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime

from core_api.clients.storage_client import get_storage_client
from core_api.services.forge.poison import is_fingerprint_poisoned
from core_api.services.forge.sentinel_scan import (
    DEFAULT_BODY_MAX_BYTES,
    DEFAULT_DESCRIPTION_MAX_BYTES,
    scan_skill_doc,
)
from core_api.services.skill_lifecycle import (
    AutoGateResult,
    evaluate_auto_gates,
)

logger = logging.getLogger(__name__)


# Injected callable types — the route layer wires real impls; tests
# inject hermetic fakes.
PoisonCheckerCallable = Callable[[str, str | None, str], Awaitable[bool]]
LiveDataFetcherCallable = Callable[[str, str, str], Awaitable[dict | None]]
# (tenant_id, collection, doc_id, new_status) → None
#
# Contract: implementations MUST perform a CONDITIONAL update that
# narrows on the EXPECTED source status — for the promoter's
# candidate→staged tick, that means ``WHERE … AND data->>'status' =
# 'candidate'``. If the row was already promoted by a concurrent tick
# (or any other writer), the UPDATE matches zero rows and the
# implementation MUST raise :class:`AlreadyTransitionedError` so the
# promoter records the candidate as held rather than overwriting a
# now-``staged`` (or worse, ``active``) row.
#
# An unconditional ``SET data = …`` would silently re-clobber any
# downstream state — the lifecycle worker would happily "promote" an
# already-active doc back to staged. The factory below
# (:func:`make_db_status_updater`) ships the safe shape.
StatusUpdaterCallable = Callable[[str, str, str, str], Awaitable[None]]


class AlreadyTransitionedError(RuntimeError):
    """Raised by a :data:`StatusUpdaterCallable` when the row no longer
    matches the expected source status — i.e. someone else already
    transitioned this doc since the promoter loaded it. The promoter
    catches this and records a non-promote outcome with reason
    ``already_transitioned``.
    """


@dataclass(frozen=True)
class PromotionAttempt:
    """One candidate's verdict — included in :class:`PromoterRunResult`
    so the operator UI / audit log can show "promoted: N, held: M
    (with breakdown)" without a second query.

    ``target_status`` is ``'staged'`` for passing candidates, which must
    await Inbox approval. It is ``None`` for held candidates.
    """

    doc_id: str
    promoted: bool
    gates: AutoGateResult
    target_status: str | None = None


@dataclass(frozen=True)
class PromoterRunResult:
    tenant_id: str
    fleet_id: str | None
    scanned: int
    promoted: int
    held: int
    # Kept for response compatibility; automatic activation is retired. Always zero.
    auto_approved: int = 0
    attempts: tuple[PromotionAttempt, ...] = field(default_factory=tuple)


# ── Default DB-backed callable factories ───────────────────────────


def make_db_poison_checker() -> PoisonCheckerCallable:
    """Wrap :func:`is_fingerprint_poisoned` (now storage-backed) into the
    ``(tenant, fleet, fp)`` signature ``evaluate_auto_gates`` expects.
    """

    async def _check(tenant_id: str, fleet_id: str | None, fp: str) -> bool:
        return await is_fingerprint_poisoned(
            tenant_id=tenant_id,
            fleet_id=fleet_id,
            cluster_fingerprint=fp,
        )

    return _check


def make_db_live_data_fetcher() -> LiveDataFetcherCallable:
    """Read the live ``data`` jsonb for a (tenant, collection, doc_id)
    triple via core-storage-api. Used by hash-binding gate G6 to compare
    ``target.target_content_hash`` against the live doc's
    ``content_hash``.

    ``read=False`` (writer) — the promoter reads a doc it may have just
    written in an adjacent tick, so replica lag could yield a stale /
    missing row.
    """
    sc = get_storage_client()

    async def _fetch(tenant_id: str, collection: str, doc_id: str) -> dict | None:
        doc = await sc.get_document(tenant_id=tenant_id, collection=collection, doc_id=doc_id, read=False)
        if doc is None:
            return None
        data = doc.get("data") if isinstance(doc, dict) else None
        return data if isinstance(data, dict) else None

    return _fetch


def make_db_status_updater(*, expected_status: str) -> StatusUpdaterCallable:
    """Conditional-update factory: the returned callable performs a
    CAS status flip via ``sc.update_document_status`` narrowed on the
    EXPECTED source status. On a CAS miss (storage returns None — someone
    else moved the doc since the promoter loaded it) the callable raises
    :class:`AlreadyTransitionedError`, which the promoter catches and
    counts as a held attempt.

    Why conditional: an unconditional ``SET data = …`` would silently
    re-clobber any concurrent transition — two promoter ticks racing
    on the same candidate would both "succeed" and the later one would
    flip the freshly-staged doc back to staged (harmless here) — but
    the same shape would happily re-stage an already-``active`` doc
    if the gate logic ever drifted. The storage-side ``WHERE … AND
    (data->>'status') = :expected_status`` makes that physically
    impossible (and stamps ``<new_status>_at``).
    """
    sc = get_storage_client()

    async def _update(tenant_id: str, collection: str, doc_id: str, new_status: str) -> None:
        result = await sc.update_document_status(
            tenant_id=tenant_id,
            collection=collection,
            doc_id=doc_id,
            new_status=new_status,
            expected_status=expected_status,
        )
        if result is None:
            raise AlreadyTransitionedError(
                f"doc {doc_id!r} no longer has status={expected_status!r}; "
                f"another writer transitioned it concurrently"
            )

    return _update


# ── Tick entry point ───────────────────────────────────────────────


def _scan_is_clean(doc: dict) -> bool:
    """Whether a stored scan is clean; this never authorizes activation."""
    scan = doc.get("scan") or {}
    if not isinstance(scan, dict):
        return False
    try:
        critical = int(scan.get("critical", 0))
    except (TypeError, ValueError):
        return False
    return scan.get("state") == "clean" and critical == 0


async def promote_pending_candidates(
    *,
    tenant_id: str,
    fleet_id: str | None,
    poison_checker: PoisonCheckerCallable,
    live_data_fetcher: LiveDataFetcherCallable,
    status_updater: StatusUpdaterCallable,
    min_cluster_size: int,
    min_distinct_agents: int,
    freshness_window_days: int,
    now: datetime | None = None,
    limit: int = 50,
    auto_promote_clean: bool = False,
    body_max_bytes: int = DEFAULT_BODY_MAX_BYTES,
    description_max_bytes: int = DEFAULT_DESCRIPTION_MAX_BYTES,
) -> PromoterRunResult:
    """One promoter tick. Reads up to ``limit`` candidates; evaluates
    each; promotes those that pass all gates.

    ``limit`` caps wall-time per tick (and matches
    ``org_settings.skills_factory.inbox_max_pending`` — beyond that,
    the Inbox is the relief valve).

    Every passing candidate lands ``staged`` for explicit review. The retired
    ``auto_promote_clean`` argument remains accepted for older callers but has
    no effect, including when cached settings still say True. Existing active
    skills are outside this candidate query and are not changed.
    """
    now = now or datetime.now(UTC)
    sc = get_storage_client()
    # Candidate scan via the generic document-query endpoint. The JSONB
    # field-equality ``where`` maps the source SELECT's
    # ``data->>'status' = 'candidate' AND data->>'source' = 'forge'``;
    # ``order_by='created_at'`` (a JSONB key) reproduces the
    # ``ORDER BY (data->>'created_at') ASC`` ordering; fleet scoping uses
    # the top-level ``fleet_id`` column (what storage writes + indexes
    # under), applied storage-side only when ``fleet_id`` is non-None.
    # 09/02 L-33: read=False — the PRIMARY. The forge cron mines candidates and
    # then calls this in the SAME tick, so the rows being queried were written
    # moments ago. On the replica they may not have arrived yet, and the query
    # returns a short list rather than an error — so promotion is quietly
    # deferred to the next tick with nothing logged to say why.
    rows = await sc.query_documents(
        {
            "tenant_id": tenant_id,
            "collection": "skills",
            "fleet_id": fleet_id,
            "where": {"status": "candidate", "source": "forge"},
            "order_by": "created_at",
            "order": "asc",
            "limit": limit,
        },
        read=False,
    )

    attempts: list[PromotionAttempt] = []
    promoted = 0
    auto_approved = 0
    for row in rows:
        raw_data = row.get("data") if isinstance(row, dict) else None
        doc = raw_data if isinstance(raw_data, dict) else {}
        doc_id = row.get("doc_id")
        # Use the candidate's OWN fleet for gate evaluation — the tick
        # may run in all-fleet mode (``fleet_id=None``), but each
        # candidate still belongs to a specific fleet. Passing the
        # tick's None to ``poison_checker`` would only match the
        # tenant-wide poison rows and silently skip fleet-scoped
        # ``rejected_at`` rows the operator wrote against this exact
        # fleet.
        doc_fleet_id = row.get("fleet_id")
        gates = await evaluate_auto_gates(
            doc,
            tenant_id=tenant_id,
            fleet_id=doc_fleet_id,
            now=now,
            poison_checker=poison_checker,
            live_data_fetcher=live_data_fetcher,
            min_cluster_size=min_cluster_size,
            min_distinct_agents=min_distinct_agents,
            freshness_window_days=freshness_window_days,
        )
        if gates.promote:
            # Quality gates determine admission to review, never activation.
            target_status = "staged"
            try:
                await status_updater(tenant_id, "skills", doc_id, target_status)
            except AlreadyTransitionedError:
                # Concurrent writer (another promoter tick or an
                # operator action) moved the row. Don't count it as a
                # promotion or as an io_error; it's a no-op.
                logger.info(
                    "skill_promoter: doc %s already transitioned by a concurrent writer",
                    doc_id,
                )
                attempts.append(PromotionAttempt(doc_id=doc_id, promoted=False, gates=gates))
                continue
            except Exception as e:
                # Don't let one bad write kill the tick.
                logger.warning(
                    "skill_promoter: status_updater raised for %s: %s",
                    doc_id,
                    e,
                    exc_info=True,
                )
                attempts.append(PromotionAttempt(doc_id=doc_id, promoted=False, gates=gates))
                continue
            promoted += 1
            attempts.append(
                PromotionAttempt(
                    doc_id=doc_id,
                    promoted=True,
                    gates=gates,
                    target_status=target_status,
                )
            )
        else:
            attempts.append(PromotionAttempt(doc_id=doc_id, promoted=False, gates=gates))

    held = len(attempts) - promoted
    # No explicit commit: ``status_updater`` now flips each doc via a
    # storage-api CAS endpoint (``sc.update_document_status``), each its
    # own committed transaction storage-side. The previous
    # ``await db.commit()`` (which persisted the in-session UPDATEs) is
    # gone with the direct-DB session.
    logger.info(
        "skill_promoter tick: tenant=%s fleet=%s scanned=%d promoted=%d "
        "(auto_approved=%d, %d → staged) held=%d",
        tenant_id,
        fleet_id,
        len(attempts),
        promoted,
        auto_approved,
        promoted - auto_approved,
        held,
    )
    return PromoterRunResult(
        tenant_id=tenant_id,
        fleet_id=fleet_id,
        scanned=len(attempts),
        promoted=promoted,
        held=held,
        auto_approved=auto_approved,
        attempts=tuple(attempts),
    )


# ── Pre-apply hook (staged → active) ───────────────────────────────


@dataclass(frozen=True)
class PreApplyVerdict:
    """Returned by :func:`rescan_before_apply`. ``allow=False`` blocks
    the staged→active transition; the operator UI surfaces the
    Sentinel findings to explain why.
    """

    allow: bool
    state: str
    findings: tuple  # tuple[ScanFinding, ...]; opaque to keep this dataclass importable from routes


async def rescan_before_apply(
    doc_data: dict,
    *,
    body_max_bytes: int,
    description_max_bytes: int,
) -> PreApplyVerdict:
    """Re-run Sentinel against the current doc body just before
    ``staged → active``. Catches drift between propose-time and
    apply-time.

    The verdict shape mirrors the pre-write hook: any fatal finding,
    OR ``scan.state == 'quarantined'`` blocks the apply. Findings are
    returned so the operator can decide whether to Edit + re-stage or
    Reject outright.
    """
    result = await scan_skill_doc(
        doc_data,
        mode="pre-apply",
        body_max_bytes=body_max_bytes,
        description_max_bytes=description_max_bytes,
    )
    allow = result.state == "clean" and not result.any_fatal
    return PreApplyVerdict(allow=allow, state=result.state, findings=result.findings)
