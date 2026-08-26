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
from typing import Literal

from core_api.services.hooks import get_hooks

logger = logging.getLogger(__name__)

OperationType = Literal["write", "search", "recall", "insights", "evolve"]

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
