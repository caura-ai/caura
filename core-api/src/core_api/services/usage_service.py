"""Usage metering — a no-op in OSS standalone, hook-backed on a platform deploy.

OSS standalone has no usage limits, so with no hook wired every call here
returns "allowed, unlimited" and records nothing. That is the intended
behaviour, not a gap: the core engine must run without a billing plane.

A platform deployment wires ``ServiceHooks.usage_meter`` at startup and these
functions delegate to it, which is what connects the write paths to durable
per-period counters.

WHY THIS AND NOT ``capability_usage``
-------------------------------------
core-api already counts per-tenant operations —
``services/capability_usage.py``, fed by ``middleware/request_observation.py``.
That path is deliberately lossy: it buffers in memory and its own docstring
notes counts "are lost if the process dies", and it appends rows to be SUMmed
rather than upserting a period total. Right for adoption analytics, unfit for
anything a plan cap is computed from. Hence a second, exact path rather than a
rewrite of that one.

METERING, NOT ENFORCEMENT — AND THAT IS BY DESIGN
--------------------------------------------------
Nothing in core-api reads the returned ``allowed``, and that is not an
oversight waiting to be corrected. Enforcement arrives out-of-band: the
platform computes "over plan" from the persisted counters and stamps
``x-org-read-only`` on the request, which ``AuthContext.is_read_only`` reads
and ``enforce_usage_limits()`` turns into a 403 at ~22 write routes. So a hook
returning ``allowed=False`` blocks nothing here, by design — the decision it
would express is already travelling a different way.

What the result IS used for: the ``X-RateLimit-Limit`` /
``X-RateLimit-Remaining`` response headers on three routes. Everything else
discards it.

Implementation guidance for the platform side: enqueue and return ``None``.
This runs on the write path, and this codebase has twice moved off per-request
round-trips there — CAURA-628 for audit, and ``capability_usage``'s in-memory
aggregation. Report counters only when doing so costs no network call.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Literal, get_args

from core_api.services.hooks import get_hooks

logger = logging.getLogger(__name__)

OperationType = Literal["write", "search", "recall", "insights", "evolve"]

# --- Per-verb usage policy: what a mutating operation costs, and what gates it -
#
# TWO INDEPENDENT AXES, and they do not line up. Collapsing them into one
# boolean is wrong for ``update``, which charges the write budget but is NOT
# refused when the org is over plan:
#
#     verb          charges write budget   refused when over plan
#     create               yes                     yes
#     bulk_create          yes                     yes
#     update               yes                     NO      <-- see below
#     redistribute         yes                     yes
#     transition            no                      no
#     delete                no                      no
#     bulk_delete           no                      no
#
# Both axes previously existed only as the presence or absence of a call at
# each REST route and each MCP tool, so "free" was expressed by an omission —
# invisible in review. That is how the surfaces drifted: the same tenant at its
# cap was refused a status transition over REST (``enforce_usage_limits()`` on
# ``PATCH /memories/{id}/status``) and allowed one over MCP
# (``caura_manage(op="transition")``, which checked nothing).
#
# THE DECISION: transitions and deletes are free and ungated by plan limits, on
# both surfaces.
#
# The principle is GROWTH, not direction. Read-only mode exists to stop an
# over-plan org adding to the store; ``AuthContext.enforce_usage_limits`` spells
# out the corollary — "users in read-only mode must be able to delete data to
# get back under limits". A status transition writes one column on a row that
# already exists. It adds nothing, in EITHER direction: ``active -> archived``
# and ``archived -> active`` leave the store exactly the same size, so neither
# is the thing read-only mode is defending against.
#
# That direction-independence is deliberate and was questioned in review, so the
# reasoning is recorded rather than left implicit. Reactivating an archived
# memory looks like it should cost something, but against the counters this
# service actually maintains it cannot: ``tenant_usage_counters`` is keyed
# ``(tenant_id, operation, period_start)`` — a monotonic count of OPERATIONS per
# period, with no active-row or footprint dimension anywhere. There is no
# quantity for a reactivation to inflate, and equally none for an archive to
# reduce. Both directions are inert.
#
# ⚠ REVISIT IF THAT CHANGES. If a plan ever meters live rows (an "active
# memories" cap rather than an operations-per-period cap), then archiving really
# would reduce usage and reactivating really would raise it, and this verb stops
# being safe to treat as one thing — it would need to discriminate on the
# ``old_status -> new_status`` pair. Note that such a fix is only implementable
# on REST today: MCP cannot see plan-limit mode at all (see below), so gating
# one direction there would rebuild the exact surface drift this table exists to
# close.
#
# ``enforce_read_only()`` is NEITHER axis. That is the demo-mode gate, a
# separate question, and it stays on the transition route.
#
# ``update`` charges quota and is deliberately NOT plan-limit gated: an update
# rewrites a row rather than adding one, so it does not grow the store, which
# is the principle this table encodes. That omission is a decision, not the
# oversight it once sat beside — ``update`` also skipped ``enforce_read_only()``
# and so was gated by neither, which let a read-only credential rewrite any
# memory in its tenant (caura-ai/caura#1204). That half is now closed: the route
# calls ``enforce_read_only()`` like every other mutating memory route, which is
# what makes the remaining omission legible as a choice.
WRITE_QUOTA_OPS: frozenset[str] = frozenset({"create", "bulk_create", "update", "redistribute"})

# Ops refused when the org is over its plan limit. A subset of the above minus
# ``update`` — see the note on it. REST-ONLY IN PRACTICE: the MCP surface
# cannot consult this because it has no read-only signal at all — the MCP
# middleware never reads the gateway's ``x-org-read-only`` header, so no MCP
# tool can see plan-limit mode (caura-ai/caura#1205). Until that is closed, any
# op listed here is gated on REST and ungated on MCP, which is exactly why
# ``transition`` is deliberately NOT listed.
PLAN_LIMIT_GATED_OPS: frozenset[str] = frozenset({"create", "bulk_create", "redistribute"})

# Typed so a mistyped verb is a mypy error at the call site rather than a
# ``ValueError`` at request time — i.e. a 500 for the caller.
#
# It does NOT catch today's call sites, and the honest reason is worth writing
# down rather than discovering later: ``core_api.routes.memories`` and
# ``core_api.mcp_server`` are both on the ``ignore_errors`` list in
# ``core-api/pyproject.toml``, which is where every lookup added here lives.
# Verified by mistyping one and watching mypy still report success. So the
# runtime check below is the ACTUAL protection at those two call sites, not a
# belt-and-braces extra — do not delete it on the assumption the type covers it.
# The annotation still earns its place: it is correct for callers outside the
# exempted modules, and it starts working for these the day either module comes
# off that list, which the config itself calls a to-do rather than a policy.
MutatingOp = Literal["create", "bulk_create", "update", "redistribute", "transition", "delete", "bulk_delete"]

_KNOWN_OPS: frozenset[str] = frozenset(get_args(MutatingOp))


def _known(op: str) -> str:
    """Reject an unrecognised verb rather than answering a question nobody asked.

    Defaulting either way is silent: a new mutating op would be quietly free
    (revenue leak) or quietly charged (surprise refusals). Raising makes adding
    one a decision.
    """
    if op not in _KNOWN_OPS:
        raise ValueError(
            f"No usage policy for operation {op!r}. Add it to the tables in "
            f"usage_service — known: {sorted(_KNOWN_OPS)}."
        )
    return op


def charges_write_quota(op: MutatingOp) -> bool:
    """Whether ``op`` costs write budget (i.e. calls ``check_and_increment``)."""
    return _known(op) in WRITE_QUOTA_OPS


def plan_limit_gated(op: MutatingOp) -> bool:
    """Whether ``op`` is refused when the org is over its plan limit."""
    return _known(op) in PLAN_LIMIT_GATED_OPS


# One traceback per failed write would turn a meter outage into a log-volume
# incident on top of a metering one. Same throttle shape as ``audit_queue``'s
# drop counter, for the same reason.
_METER_FAILURE_LOG_EVERY = 100
_meter_failures = 0


@dataclass
class UsageCheckResult:
    """What a meter reports back. Only ``limit`` and ``remaining`` are read.

    The rest default so a platform implementation constructs what it actually
    knows rather than filling six slots to deliver two — and so it is not
    nudged into setting ``allowed``, which core-api ignores (module docstring).
    """

    allowed: bool
    operation: str
    current: int = 0
    limit: int | None = None
    remaining: int | None = None
    resets_at: datetime | None = None
    plan: str = "free"

    def get(self, key: str, default=None):
        """Dict-style access for backward compatibility with route code."""
        return getattr(self, key, default)


def _allowed(op: str) -> UsageCheckResult:
    return UsageCheckResult(allowed=True, operation=op)


async def _meter(tenant_id: str, operation: OperationType, count: int) -> UsageCheckResult:
    """Delegate to the wired meter, or report unlimited when there is none."""
    global _meter_failures
    hook = get_hooks().usage_meter
    if hook is None:
        return _allowed(operation)
    try:
        result = await hook(tenant_id=tenant_id, operation=operation, count=count)
    except Exception:
        # FAIL OPEN. This sits on the write path of every metered route, and a
        # metering backend that is down must not turn into a failed customer
        # write — losing a count is the cheaper error.
        #
        # ⚠ Revisit the moment ``allowed`` gains a reader in core-api:
        # fail-open would then read "meter down ⇒ every tenant unlimited", the
        # standard billing bypass. It is safe today only because enforcement
        # travels via ``x-org-read-only`` instead (module docstring).
        _meter_failures += 1
        if _meter_failures == 1 or _meter_failures % _METER_FAILURE_LOG_EVERY == 0:
            logger.exception(
                "usage meter failed (%d since start) for tenant=%s operation=%s; reporting unlimited",
                _meter_failures,
                tenant_id,
                operation,
            )
        return _allowed(operation)
    # A hook may legitimately report nothing — it recorded the usage and has no
    # counters to hand back. ``is not None`` rather than truthiness, so a
    # falsy-but-valid result is not silently discarded.
    return result if result is not None else _allowed(operation)


def recall_operation() -> OperationType:
    """D13 — which counter a recall (search + LLM brief) bills against.

    Plans have carried separate ``searches`` / ``recalls`` limits since the
    initial schema, and the platform hook maps ``"recall"`` to the ``recalls``
    counter — but every recall call site passed ``"search"``, so the recalls
    counter never moved and the per-plan recall cap could never fire
    (canonical D13). The correct operation is gated behind
    ``settings.meter_recall_as_recall`` (default off) because the recalls
    counter feeds over-plan enforcement; see the setting's comment.
    """
    from core_api.config import settings

    return "recall" if settings.meter_recall_as_recall else "search"


def meters_mcp_bulk_write() -> bool:
    """Whether the MCP batch write bills the write counter (caura-ai/caura#1220).

    ``caura_write(items=[...])`` reaches ``create_memories_bulk`` with no
    metering call of any kind, while REST's ``POST /memories/bulk`` charges one
    unit per item. Same tenant, same N memories, different bill.

    Gated for the same reason as ``recall_operation`` above, and more sharply:
    that one bills the wrong counter, this one bills nothing. Turning it on
    starts charging for writes that have been free, so it is a billing decision
    rather than a deploy side effect — see the setting's comment for why the
    first refusal a tenant sees will come from REST.

    ``charges_write_quota("bulk_create")`` is still consulted at the call site.
    This flag is the deploy-time gate; that table remains the policy record, so
    a future change to it reaches this path like any other.
    """
    from core_api.config import settings

    return settings.meter_mcp_bulk_writes


async def check_and_increment(
    tenant_id: str,
    operation: OperationType,
    count: int = 1,
) -> UsageCheckResult:
    """Record ``count`` of ``operation`` against ``tenant_id``.

    The first parameter was named ``org_id``, which was safe only while the
    body ignored it: every call site passes a tenant id. A meter trusting the
    old name would have keyed those writes on the wrong entity.
    """
    return await _meter(tenant_id, operation, count)


# The same function under the name five modules import it by — all of them as
# ``check_and_increment_by_tenant as check_and_increment``, so no call site
# spells it. An alias rather than a copy, so there is visibly one
# implementation.
check_and_increment_by_tenant = check_and_increment


async def bulk_check_and_increment(
    tenant_id: str,
    count: int,
) -> UsageCheckResult:
    """Record a bulk write of ``count`` items as a single metered call."""
    return await _meter(tenant_id, "write", count)
