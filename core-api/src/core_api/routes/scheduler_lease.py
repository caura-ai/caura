"""Per-tick lease so a replicated scheduler fires each tick exactly once.

WHY THIS EXISTS. ``core-operations`` runs its scheduler as an in-process
asyncio loop (``core_operations.scheduler``). That loop's ``_started``
latch guards a double start WITHIN a process and nothing guards across
processes, so the instance count is the fire count: every wall-clock
aligned task fires once per live instance, all inside the same second.

Measured in prod on 2026-09-16, 00:00-06:00Z, with core-operations at
maxScale=2: all nine scheduled endpoints reached here twice per tick.
Under the doubled load two exceeded this service's own 45s request
budget and 504ed — ``lifecycle/fanout/archive-expired`` at 46.8s and
45.0s, and the hourly ``interview/schedule/run`` at 45.0s. A killed
fanout leaves its lifecycle audit rows open at 'pending' for every org
whose publish never ran.

WHY THE RECEIVER MEDIATES. core-operations deliberately holds no
datastore — its config carries a core-api URL and an admin key, nothing
else — so it cannot elect a leader among its own replicas. core-api can:
it already owns the Redis this uses. Putting the decision here also makes
the guarantee caller-agnostic. It holds for N scheduler replicas, for a
retry, and for an operator firing the same tick by hand, none of which a
leader election among schedulers would cover.

WHY NOT UNDER /admin/lifecycle/. The lease covers every scheduled job,
including ``agent-digest``, ``interviewer-schedule`` and
``embedding-coverage``, which are not lifecycle actions. Filing it under
the lifecycle prefix would name it after one of its callers — the same
mistake ``storage_http_timeout_s`` made before it became
``core_api_http_timeout_s``.

FAIL-OPEN, DELIBERATELY. ``cache_set_nx`` returns True when Redis is
unavailable or the call raises, so a Redis outage grants every lease and
the estate degrades to exactly today's behaviour: a possible duplicate
fire. The alternative — denying on error — would let one Redis blip stop
every nightly sweep silently, which is a far worse failure than the one
this endpoint exists to prevent. A lease is an optimisation over
correctness that already tolerates duplicates downstream (the consumer
dedup gate and the per-tenant activity gate both survive a double fire);
it is not a correctness primitive, and must never become a single point
of failure for the schedule.

WHAT THIS DOES NOT DO. It does not make a duplicate fire impossible, only
unlikely-and-bounded. Two instances whose clocks disagree by more than
the lease TTL will both be granted. That is acceptable for the same
reason as above, and is why the cap on ``maxScale`` is not removed by
this endpoint landing — see scripts/update-cloud-run-scale.sh in
caura-enterprise for the two gaps the cap covers.
"""

from __future__ import annotations

import logging
import re

from fastapi import APIRouter, Depends, HTTPException, Request

from core_api.auth import AuthContext, get_auth_context
from core_api.cache import cache_set_nx

logger = logging.getLogger(__name__)

router = APIRouter(tags=["Admin", "Scheduler"])

# Task names come from ``scheduler.register(...)`` and are hyphenated
# lowercase identifiers ("lifecycle-archive-expired"). Constrained because
# the value is concatenated into a Redis key: an unbounded string would let
# a caller with the admin key write keys outside this namespace, and a
# name containing whitespace or a newline would be a different key than it
# reads as in the logs.
_TASK_NAME = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")

# The TTL a caller may ask for. The floor keeps a lease long enough to
# outlive the spread between replicas firing the same tick (measured at
# 116ms-300ms in prod, so seconds of headroom is ample). The ceiling is
# the real safety property: a lease MUST expire before the next legitimate
# tick of the same task, or it suppresses real work rather than duplicate
# work. core-operations' fastest task is hourly, and it asks for
# ``min(default, interval / 2)`` rather than a constant, so the ceiling
# here is a backstop against a caller that computes it wrongly, not the
# primary defence.
_MIN_TTL_S = 1.0
_MAX_TTL_S = 900.0

_KEY_PREFIX = "scheduler:tick-lease:"


@router.post("/admin/scheduler/tick-lease")
async def claim_tick_lease(
    request: Request,
    auth: AuthContext = Depends(get_auth_context),
) -> dict:
    """Claim the right to run one scheduled tick.

    Body: ``{"task": "<registered-task-name>", "ttl_s": <seconds>}``.
    Returns ``{"task", "granted"}``. ``granted`` is True for the first
    caller within the TTL and False for every other caller, so a replica
    that loses the race skips this tick and waits for its next one.

    Answers 200 either way — losing a lease is a normal outcome, not an
    error, and mapping it to a 4xx would make every skipped duplicate
    look like a fault in the caller's logs.
    """
    auth.enforce_admin()

    # ``request.json()`` raises on a malformed body; without the guard
    # FastAPI's catch-all maps it to 500. Same posture as the lifecycle
    # manual trigger — surface 422 so the caller can self-diagnose.
    try:
        body = await request.json()
    except Exception as exc:
        raise HTTPException(
            status_code=422,
            detail="request body must be valid JSON",
        ) from exc

    # Valid JSON is not necessarily a JSON *object*. ``[]``, ``null``,
    # ``42``, ``"x"`` and ``true`` all parse without raising, so the guard
    # above never fires for them, and reading ``.get`` off any of them is
    # an AttributeError — the very 500 that guard exists to prevent,
    # reached one line later by a different route. Annotating the parse
    # ``body: dict`` asserted this rather than checking it: ``json()``
    # returns ``Any``, so the annotation bound a type mypy could not
    # contradict and the shape went untested until runtime.
    if not isinstance(body, dict):
        raise HTTPException(
            status_code=422,
            detail="request body must be a JSON object",
        )

    task = body.get("task")
    if not isinstance(task, str) or not _TASK_NAME.match(task):
        raise HTTPException(
            status_code=422,
            detail=(f"'task' must be a lowercase hyphenated name matching {_TASK_NAME.pattern}"),
        )

    ttl_raw = body.get("ttl_s")
    # bool is an int subclass, and True would otherwise sail through as
    # a 1-second TTL rather than being rejected as the wrong type.
    if isinstance(ttl_raw, bool) or not isinstance(ttl_raw, int | float):
        raise HTTPException(status_code=422, detail="'ttl_s' must be a number of seconds")
    ttl = float(ttl_raw)
    if not _MIN_TTL_S <= ttl <= _MAX_TTL_S:
        raise HTTPException(
            status_code=422,
            detail=f"'ttl_s' must be between {_MIN_TTL_S} and {_MAX_TTL_S}",
        )

    # Redis SETEX takes whole seconds; round UP so a fractional TTL can
    # never expire early and re-admit the duplicate this exists to block.
    granted = await cache_set_nx(f"{_KEY_PREFIX}{task}", "held", int(-(-ttl // 1)))

    # At info because a denied lease is the mechanism working, and an
    # operator reading "why did only one instance run this" needs to see
    # it. The granted case stays quiet: it is every normal tick.
    if not granted:
        logger.info(
            "scheduler tick lease denied; another instance holds this tick",
            extra={"task": task, "ttl_s": ttl},
        )

    return {"task": task, "granted": granted}
