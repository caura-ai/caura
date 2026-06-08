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

The companion pre-apply hook (``rescan_before_apply``) is also here:
right before an operator-driven ``staged → active`` transition, we
re-run the Sentinel scan against the *current* doc body. This catches
the case where the lake state changed between propose-time and
apply-time (e.g. a new prompt-injection marker appeared in a cited
memory's content, and the scan would now flag it).

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
from typing import Any

from sqlalchemy import text

from core_api.services.forge.poison import is_fingerprint_poisoned
from core_api.services.forge.sentinel_scan import scan_skill_doc
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
    (with breakdown)" without a second query."""

    doc_id: str
    promoted: bool
    gates: AutoGateResult


@dataclass(frozen=True)
class PromoterRunResult:
    tenant_id: str
    fleet_id: str | None
    scanned: int
    promoted: int
    held: int
    attempts: tuple[PromotionAttempt, ...] = field(default_factory=tuple)


# ── Default DB-backed callable factories ───────────────────────────


def make_db_poison_checker(db: Any) -> PoisonCheckerCallable:
    """Wrap :func:`is_fingerprint_poisoned` into the
    ``(tenant, fleet, fp)`` signature ``evaluate_auto_gates`` expects.
    """

    async def _check(tenant_id: str, fleet_id: str | None, fp: str) -> bool:
        return await is_fingerprint_poisoned(
            db,
            tenant_id=tenant_id,
            fleet_id=fleet_id,
            cluster_fingerprint=fp,
        )

    return _check


def make_db_live_data_fetcher(db: Any) -> LiveDataFetcherCallable:
    """Read the live ``data`` jsonb for a (tenant, collection, doc_id)
    triple. Used by hash-binding gate G6 to compare
    ``target.target_content_hash`` against the live doc's
    ``content_hash``.

    Cast ``data`` to jsonb on the wire because eToro's deployment
    persists it as ``json`` while OSS uses ``jsonb`` — the cast is a
    no-op on jsonb and the only safe shape across both.
    """

    async def _fetch(tenant_id: str, collection: str, doc_id: str) -> dict | None:
        row = (
            await db.execute(
                text(
                    """
                    SELECT data::jsonb AS data
                    FROM documents
                    WHERE tenant_id = :tenant_id
                      AND collection = :collection
                      AND doc_id     = :doc_id
                    LIMIT 1
                    """
                ),
                {
                    "tenant_id": tenant_id,
                    "collection": collection,
                    "doc_id": doc_id,
                },
            )
        ).fetchone()
        if row is None:
            return None
        data = row.data
        return data if isinstance(data, dict) else None

    return _fetch


def make_db_status_updater(db: Any, *, expected_status: str) -> StatusUpdaterCallable:
    """Conditional-update factory: the returned callable performs an
    UPDATE narrowed on the EXPECTED source status. On zero-row updates
    (someone else moved the doc since the promoter loaded it) the
    callable raises :class:`AlreadyTransitionedError`, which the
    promoter catches and counts as a held attempt.

    Why conditional: an unconditional ``SET data = …`` would silently
    re-clobber any concurrent transition — two promoter ticks racing
    on the same candidate would both "succeed" and the later one would
    flip the freshly-staged doc back to staged (harmless here) — but
    the same shape would happily re-stage an already-``active`` doc
    if the gate logic ever drifted. ``WHERE … AND
    (data->>'status') = :expected_status`` makes that physically
    impossible.
    """

    async def _update(tenant_id: str, collection: str, doc_id: str, new_status: str) -> None:
        # Stamp ``<new_status>_at`` alongside the status flip, mirroring
        # ``_persist_status_transition`` in routes/skills_inbox.py
        # (which always sets the timestamp on the same update). Without
        # this, the promoter-driven path would leave a freshly-staged
        # doc with no ``staged_at`` — making "promoted age" queries
        # silently inaccurate for the worker-driven half of the
        # population.
        at_key = f"{new_status}_at"
        now_iso = datetime.now(UTC).isoformat(timespec="seconds")
        result = await db.execute(
            text(
                """
                UPDATE documents
                SET data = jsonb_set(
                               jsonb_set(data::jsonb, '{status}', to_jsonb(:new_status::text)),
                               ARRAY[:at_key],
                               to_jsonb(:now_iso::text)
                           )::json
                WHERE tenant_id = :tenant_id
                  AND collection = :collection
                  AND doc_id     = :doc_id
                  AND (data->>'status') = :expected_status
                RETURNING doc_id
                """
            ),
            {
                "tenant_id": tenant_id,
                "collection": collection,
                "doc_id": doc_id,
                "new_status": new_status,
                "expected_status": expected_status,
                "at_key": at_key,
                "now_iso": now_iso,
            },
        )
        row = result.fetchone()
        if row is None:
            raise AlreadyTransitionedError(
                f"doc {doc_id!r} no longer has status={expected_status!r}; "
                f"another writer transitioned it concurrently"
            )

    return _update


# ── Tick entry point ───────────────────────────────────────────────


async def promote_pending_candidates(
    db: Any,
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
) -> PromoterRunResult:
    """One promoter tick. Reads up to ``limit`` candidates; evaluates
    each; promotes those that pass all gates.

    ``limit`` caps wall-time per tick (and matches
    ``org_settings.skills_factory.inbox_max_pending`` — beyond that,
    the Inbox is the relief valve).
    """
    now = now or datetime.now(UTC)
    rows = (
        await db.execute(
            text(
                """
                SELECT doc_id, fleet_id, data::jsonb AS data
                FROM documents
                WHERE tenant_id = :tenant_id
                  AND collection = 'skills'
                  AND (data->>'status') = 'candidate'
                  AND (data->>'source') = 'forge'
                  -- Filter on the top-level ``fleet_id`` column, not
                  -- ``data->>'fleet_id'`` — the column is what storage
                  -- writes (and indexes) under, and the in-jsonb copy
                  -- can drift if writers forget to mirror it.
                  AND (:fleet_id IS NULL OR fleet_id = :fleet_id)
                ORDER BY (data->>'created_at') ASC
                LIMIT :limit
                """
            ),
            {
                "tenant_id": tenant_id,
                "fleet_id": fleet_id,
                "limit": limit,
            },
        )
    ).fetchall()

    attempts: list[PromotionAttempt] = []
    promoted = 0
    for row in rows:
        doc = row.data if isinstance(row.data, dict) else {}
        # Use the candidate's OWN fleet for gate evaluation — the tick
        # may run in all-fleet mode (``fleet_id=None``), but each
        # candidate still belongs to a specific fleet. Passing the
        # tick's None to ``poison_checker`` would only match the
        # tenant-wide poison rows and silently skip fleet-scoped
        # ``rejected_at`` rows the operator wrote against this exact
        # fleet.
        doc_fleet_id = row.fleet_id
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
            try:
                await status_updater(tenant_id, "skills", row.doc_id, "staged")
            except AlreadyTransitionedError:
                # Concurrent writer (another promoter tick or an
                # operator action) moved the row. Don't count it as a
                # promotion or as an io_error; it's a no-op.
                logger.info(
                    "skill_promoter: doc %s already transitioned by a concurrent writer",
                    row.doc_id,
                )
                attempts.append(PromotionAttempt(doc_id=row.doc_id, promoted=False, gates=gates))
                continue
            except Exception as e:
                # Don't let one bad write kill the tick.
                logger.warning(
                    "skill_promoter: status_updater raised for %s: %s",
                    row.doc_id,
                    e,
                    exc_info=True,
                )
                attempts.append(PromotionAttempt(doc_id=row.doc_id, promoted=False, gates=gates))
                continue
            promoted += 1
            attempts.append(PromotionAttempt(doc_id=row.doc_id, promoted=True, gates=gates))
        else:
            attempts.append(PromotionAttempt(doc_id=row.doc_id, promoted=False, gates=gates))

    held = len(attempts) - promoted
    # ``make_db_status_updater`` issues its UPDATEs via ``db.execute``
    # — those land in the SQLAlchemy session but are NOT persisted
    # until commit. Without this, the entire tick's worth of
    # promotions silently rolls back when the session exits.
    await db.commit()
    logger.info(
        "skill_promoter tick: tenant=%s fleet=%s scanned=%d promoted=%d held=%d",
        tenant_id,
        fleet_id,
        len(attempts),
        promoted,
        held,
    )
    return PromoterRunResult(
        tenant_id=tenant_id,
        fleet_id=fleet_id,
        scanned=len(attempts),
        promoted=promoted,
        held=held,
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
