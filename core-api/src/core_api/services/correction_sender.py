"""Correction propagation, CP-11 and CP-15: send a correction's notices, and read who confirmed.

A ``memory_correction`` in ``preview`` lists the agents that read the old
version (CP-10). Sending it posts one notice per recipient to the Caura bus's
system sender (enterprise ``platform-collaboration-api``,
``docs/agent-collaboration/SYSTEM_SENDERS.md`` there):

- ``POST {COLLABORATION_API_URL}/api/v1/bus/system/corrections`` with the
  ``X-Caura-System-Secret`` header, at most :data:`BUS_BATCH` recipients per
  call, ``Idempotency-Key`` = the correction id, so a retry reaches only the
  recipients that did not get the notice yet;
- ``GET .../corrections/{id}?tenant_id=`` for each recipient's outcome
  (``sent|delivered|confirmed|late|failed``).

Per recipient: ``accepted`` / ``already_sent`` → ``sent`` on the ``bus``
channel; ``unknown_agent`` → ``unreachable`` on the ``recall`` channel (the
agent is told on its next recall, CP-13); ``throttled`` / ``backlog_full`` →
stays ``pending`` with ``next_attempt_at``. A throttled batch is retried inline
when the bus asks for a short wait, then from a background task; anything
still waiting after that is retried by the next send or status read. With no
``COLLABORATION_API_URL`` (standalone OSS) every recipient goes to the
``recall`` channel, still ``pending``.

The correction is ``sending`` while any recipient waits on the bus, then
``sent``, and ``closed`` once every recipient confirmed or failed. A status
read refreshes bus outcomes at most every :data:`STATUS_CACHE_S` seconds. The
secret is read from settings and never logged. No LLM calls.
"""

from __future__ import annotations

import asyncio
import logging
import re
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
from fastapi import HTTPException

from core_api.clients.storage_client import get_storage_client
from core_api.config import settings
from core_api.tasks import track_task

logger = logging.getLogger(__name__)

#: Recipients per bus call (the system route's limit).
BUS_BATCH = 200
#: A status read asks the bus again at most this often.
STATUS_CACHE_S = 30
#: Inline retries of a throttled batch, each only when the bus asks for at most
#: :data:`INLINE_WAIT_MAX_S` seconds.
INLINE_RETRIES = 2
INLINE_WAIT_MAX_S = 5.0
#: Background retries after the request returns, each waiting what the bus
#: asked (at least :data:`BACKGROUND_WAIT_MIN_S`, at most
#: :data:`BACKGROUND_WAIT_MAX_S`).
BACKGROUND_RETRIES = 6
BACKGROUND_WAIT_MIN_S = 5.0
BACKGROUND_WAIT_MAX_S = 300.0
#: When the bus gives no ``retry_after_seconds`` for a throttled recipient.
DEFAULT_RETRY_AFTER_S = 30.0
BUS_TIMEOUT_S = 10.0

#: Notice states a bus status read can still change.
_OPEN_BUS_STATES = frozenset({"sent", "delivered", "late"})
#: States after which a recipient needs nothing more.
_SETTLED_STATES = frozenset({"confirmed", "failed"})
#: Bus ``outcome`` → ``notice_state``.
_OUTCOMES = frozenset({"sent", "delivered", "confirmed", "late", "failed"})
_KIND_RE = re.compile(r"^[a-z_]{1,40}$")

#: Corrections with a background retry already scheduled in this process.
_retrying: set[str] = set()


class BusUnavailable(Exception):
    """The bus did not answer (network, 5xx, 429): the recipients stay pending for a retry."""


def bus_configured() -> bool:
    return bool(settings.collaboration_api_url.strip())


def _bus_secret() -> str:
    own = settings.collaboration_system_send_secret.get_secret_value()
    return own or settings.core_storage_shared_secret.get_secret_value()


def _now() -> datetime:
    return datetime.now(UTC)


def _iso(value: datetime) -> str:
    return value.isoformat()


def _parse(value: str | None) -> datetime | None:
    if not value:
        return None
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


_STATE_FIELDS = (
    "agent_id",
    "notice_state",
    "channel",
    "confirmed_at",
    "notified_at",
    "next_attempt_at",
    "attempts",
    "bus_message_id",
    "bus_delivery_id",
)


def _state(recipient: dict, **changes: Any) -> dict:
    """The recipient's whole delivery state with ``changes`` applied (storage writes all of it)."""
    out = {k: recipient.get(k) for k in _STATE_FIELDS}
    out["attempts"] = int(out["attempts"] or 0)
    out.update(changes)
    return out


def _waits_on_bus(recipient: dict) -> bool:
    return recipient.get("notice_state") == "pending" and recipient.get("channel") != "recall"


def _due(recipient: dict, now: datetime) -> bool:
    at = _parse(recipient.get("next_attempt_at"))
    return at is None or at <= now


def bus_kind(correction: dict) -> str:
    """The notice's ``kind``: ``status_<new status>`` for a status change, else the record's kind."""
    kind = correction["kind"]
    if kind == "status":
        new_status = str((correction.get("detail") or {}).get("new_status") or "")
        if _KIND_RE.match(f"status_{new_status}"):
            return f"status_{new_status}"
    return kind


def corrected_by(correction: dict) -> str:
    actor = correction.get("actor") or {}
    return f"{actor.get('kind')}:{actor['id']}" if actor.get("id") else str(actor.get("kind") or "unknown")


def _status_after(recipients: list[dict], current: str) -> str:
    if any(_waits_on_bus(r) for r in recipients):
        return "sending"
    if recipients and all(r.get("notice_state") in _SETTLED_STATES for r in recipients):
        return "closed"
    return "sent" if current in ("preview", "sending") else current


async def _post_bus(tenant_id: str, correction: dict, batch: list[dict]) -> dict:
    """One system-send call; the bus's JSON. Raises :class:`BusUnavailable` for a retryable failure."""
    url = f"{settings.collaboration_api_url.rstrip('/')}/api/v1/bus/system/corrections"
    body = {
        "tenant_id": tenant_id,
        "correction": {
            "correction_id": correction["id"],
            "memory_id": correction["memory_id"],
            "kind": bus_kind(correction),
            "old_summary": correction.get("old_summary"),
            "new_summary": correction.get("new_summary"),
            "corrected_by": corrected_by(correction),
            "corrected_at": correction["corrected_at"],
        },
        "recipients": [{"agent_id": r["agent_id"], "first_seen_by_you": r["first_seen"]} for r in batch],
    }
    headers = {"X-Caura-System-Secret": _bus_secret(), "Idempotency-Key": correction["id"]}
    try:
        async with httpx.AsyncClient(timeout=BUS_TIMEOUT_S) as client:
            resp = await client.post(url, json=body, headers=headers)
    except httpx.HTTPError as exc:
        raise BusUnavailable(type(exc).__name__) from exc
    if resp.status_code == 429 or resp.status_code >= 500:
        raise BusUnavailable(f"HTTP {resp.status_code}")
    if resp.status_code >= 400:
        # 401/403/404/409/413/422: the bus will not take this notice (no
        # secret configured there, tenant sends disabled, body too large). A
        # retry changes nothing, so the agents are told on recall instead.
        logger.warning(
            "correction notice refused by the bus; falling back to recall",
            extra={
                "correction_id": correction["id"],
                "tenant_id": tenant_id,
                "status_code": resp.status_code,
            },
        )
        return {"refused": resp.status_code}
    return resp.json()


async def _send_due(tenant_id: str, correction: dict) -> tuple[dict, float | None]:
    """Send every due recipient that waits on the bus; ``(record, seconds until the next retry or None)``."""
    recipients = correction.get("recipients") or []
    if not bus_configured():
        to_recall = [_state(r, channel="recall") for r in recipients if _waits_on_bus(r)]
        return await _apply(tenant_id, correction, recipients, to_recall), None

    retry_in: float | None = None
    # After an inline wait, "now" is at least the end of the wait, so the
    # recipients it waited for are due even if the clock ran a little short.
    waited_until = _now()
    for round_ in range(INLINE_RETRIES + 1):
        now = max(_now(), waited_until)
        due = [r for r in recipients if _waits_on_bus(r) and _due(r, now)]
        if not due:
            break
        updates: list[dict] = []
        retry_in = None
        for start in range(0, len(due), BUS_BATCH):
            batch = due[start : start + BUS_BATCH]
            try:
                answer = await _post_bus(tenant_id, correction, batch)
            except BusUnavailable as exc:
                logger.warning(
                    "correction notice send failed; will retry",
                    extra={"correction_id": correction["id"], "tenant_id": tenant_id, "error": str(exc)},
                )
                wait = DEFAULT_RETRY_AFTER_S
                updates += [
                    _state(
                        r,
                        channel="bus",
                        attempts=int(r.get("attempts") or 0) + 1,
                        next_attempt_at=_iso(now + timedelta(seconds=wait)),
                    )
                    for r in batch
                ]
                retry_in = wait if retry_in is None else min(retry_in, wait)
                continue
            if "refused" in answer:
                updates += [_state(r, channel="recall", notice_state="unreachable") for r in batch]
                continue
            by_agent = {a.get("agent_id"): a for a in answer.get("recipients") or ()}
            after = float(answer.get("retry_after_seconds") or DEFAULT_RETRY_AFTER_S)
            for r in batch:
                result = by_agent.get(r["agent_id"]) or {}
                status = result.get("status")
                attempts = int(r.get("attempts") or 0) + 1
                if status in ("accepted", "already_sent"):
                    updates.append(
                        _state(
                            r,
                            channel="bus",
                            notice_state="sent",
                            attempts=attempts,
                            notified_at=r.get("notified_at") or _iso(now),
                            next_attempt_at=None,
                            bus_message_id=result.get("message_id"),
                            bus_delivery_id=result.get("delivery_id"),
                        )
                    )
                elif status == "unknown_agent":
                    updates.append(
                        _state(
                            r,
                            channel="recall",
                            notice_state="unreachable",
                            attempts=attempts,
                            next_attempt_at=None,
                        )
                    )
                else:
                    # throttled, backlog_full, or an agent the answer left out.
                    updates.append(
                        _state(
                            r,
                            channel="bus",
                            attempts=attempts,
                            next_attempt_at=_iso(now + timedelta(seconds=after)),
                        )
                    )
                    retry_in = after if retry_in is None else min(retry_in, after)
        correction = await _apply(tenant_id, correction, recipients, updates)
        recipients = correction.get("recipients") or []
        if retry_in is None or retry_in > INLINE_WAIT_MAX_S or round_ == INLINE_RETRIES:
            break
        await asyncio.sleep(retry_in)
        waited_until = now + timedelta(seconds=retry_in)
    return correction, retry_in if any(_waits_on_bus(r) for r in recipients) else None


async def _apply(tenant_id: str, correction: dict, recipients: list[dict], updates: list[dict]) -> dict:
    by_agent = {u["agent_id"]: u for u in updates}
    merged = [{**r, **by_agent.get(r["agent_id"], {})} for r in recipients]
    status = _status_after(merged, correction["status"])
    if not updates and status == correction["status"]:
        return correction
    written = await get_storage_client().apply_correction_states(
        tenant_id, correction["id"], recipients=updates, status=status
    )
    return written or correction


def _schedule_retry(tenant_id: str, correction_id: str, wait: float) -> None:
    if correction_id in _retrying:
        return
    _retrying.add(correction_id)
    track_task(_retry_loop(tenant_id, correction_id, wait))


async def _retry_loop(tenant_id: str, correction_id: str, wait: float) -> None:
    """Background retries of recipients the bus throttled; bounded, then left to the next send or read."""
    try:
        for _ in range(BACKGROUND_RETRIES):
            await asyncio.sleep(min(max(wait, BACKGROUND_WAIT_MIN_S), BACKGROUND_WAIT_MAX_S))
            correction = await get_storage_client().get_memory_correction(tenant_id, correction_id)
            if correction is None or correction["status"] != "sending":
                return
            correction, next_wait = await _send_due(tenant_id, correction)
            if next_wait is None:
                return
            wait = next_wait
        logger.warning(
            "correction notices still throttled after background retries; left pending",
            extra={"correction_id": correction_id, "tenant_id": tenant_id},
        )
    except Exception:
        logger.exception("correction notice retry failed", extra={"correction_id": correction_id})
    finally:
        _retrying.discard(correction_id)


async def send_correction(tenant_id: str, correction_id: str, *, sent_by: str) -> dict | None:
    """Send the correction's notices; the record, ``None`` when it is not the tenant's.

    Idempotent: a correction already ``sent`` or ``closed`` is returned as it
    is; one still ``sending`` retries its due recipients.
    """
    sc = get_storage_client()
    correction = await sc.begin_correction_send(tenant_id, correction_id, sent_by=sent_by)
    if correction is None:
        return None
    if correction["status"] != "sending":
        return correction
    correction, retry_in = await _send_due(tenant_id, correction)
    if retry_in is not None:
        _schedule_retry(tenant_id, correction_id, retry_in)
    logger.info(
        "correction notices sent",
        extra={
            "correction_id": correction_id,
            "tenant_id": tenant_id,
            "status": correction["status"],
            "counts": correction.get("counts"),
        },
    )
    return correction


async def _bus_status(tenant_id: str, correction_id: str) -> dict | None:
    url = f"{settings.collaboration_api_url.rstrip('/')}/api/v1/bus/system/corrections/{correction_id}"
    async with httpx.AsyncClient(timeout=BUS_TIMEOUT_S) as client:
        resp = await client.get(
            url, params={"tenant_id": tenant_id}, headers={"X-Caura-System-Secret": _bus_secret()}
        )
    if resp.status_code == 404:
        return None
    resp.raise_for_status()
    return resp.json()


async def _close_if_settled(tenant_id: str, correction: dict) -> dict:
    """``closed`` once every recipient confirmed or failed, whichever channel told it."""
    if _status_after(correction.get("recipients") or [], correction["status"]) != "closed":
        return correction
    written = await get_storage_client().apply_correction_states(
        tenant_id, correction["id"], recipients=[], status="closed"
    )
    return written or correction


async def refresh_correction(tenant_id: str, correction: dict) -> dict:
    """``correction`` with its bus outcomes refreshed (at most every :data:`STATUS_CACHE_S` s).

    Also retries due recipients of a correction still ``sending``. A bus that
    does not answer leaves the record as it was: the read never fails for it.
    """
    if correction.get("status") not in ("sending", "sent"):
        return correction
    if not bus_configured():
        return await _close_if_settled(tenant_id, correction)
    try:
        if correction["status"] == "sending" and any(
            _waits_on_bus(r) and _due(r, _now()) for r in correction.get("recipients") or ()
        ):
            correction, retry_in = await _send_due(tenant_id, correction)
            if retry_in is not None:
                _schedule_retry(tenant_id, correction["id"], retry_in)
        checked = _parse(correction.get("bus_checked_at"))
        now = _now()
        if checked is not None and now - checked < timedelta(seconds=STATUS_CACHE_S):
            return await _close_if_settled(tenant_id, correction)
        recipients = correction.get("recipients") or []
        if not any(
            r.get("channel") == "bus" and r.get("notice_state") in _OPEN_BUS_STATES for r in recipients
        ):
            return await _close_if_settled(tenant_id, correction)
        status = await _bus_status(tenant_id, correction["id"])
        updates: list[dict] = []
        by_agent = {a.get("agent_id"): a for a in (status or {}).get("recipients") or ()}
        for r in recipients:
            seen = by_agent.get(r["agent_id"])
            if r.get("channel") != "bus" or r.get("notice_state") not in _OPEN_BUS_STATES or not seen:
                continue
            outcome = seen.get("outcome")
            if outcome not in _OUTCOMES or outcome == r.get("notice_state"):
                continue
            confirmed_at = seen.get("replied_at") or seen.get("acked_at") if outcome == "confirmed" else None
            updates.append(
                _state(r, notice_state=outcome, confirmed_at=confirmed_at or r.get("confirmed_at"))
            )
        merged = [{**r, **{u["agent_id"]: u for u in updates}.get(r["agent_id"], {})} for r in recipients]
        written = await get_storage_client().apply_correction_states(
            tenant_id,
            correction["id"],
            recipients=updates,
            status=_status_after(merged, correction["status"]),
            bus_checked_at=_iso(now),
        )
        return written or correction
    except (httpx.HTTPError, BusUnavailable, ValueError) as exc:
        logger.warning(
            "correction status refresh failed; showing the last known states",
            extra={
                "correction_id": correction.get("id"),
                "tenant_id": tenant_id,
                "error": type(exc).__name__,
            },
        )
        return correction


async def confirm_as_agent(tenant_id: str, correction_id: str, agent_id: str) -> dict:
    """``agent_id`` confirms its notice; its own notice state. 404 when it is not a recipient.

    Shared by REST ``POST /corrections/{id}/confirm`` and MCP
    ``caura_manage op=confirm_correction``.
    """
    record = await get_storage_client().confirm_correction(tenant_id, correction_id, agent_id=agent_id)
    if record is None:
        raise HTTPException(status_code=404, detail="Not a recipient of this correction")
    mine: dict = next((r for r in record.get("recipients") or () if r.get("agent_id") == agent_id), {})
    return {
        "correction_id": record["id"],
        "memory_id": record["memory_id"],
        "agent_id": agent_id,
        "notice_state": mine.get("notice_state"),
        "confirmed_at": mine.get("confirmed_at"),
    }


def compact(correction: dict) -> dict:
    """The send result an agent needs: status, counts and each recipient's channel and state."""
    return {
        "correction_id": correction["id"],
        "memory_id": correction["memory_id"],
        "status": correction["status"],
        "counts": correction.get("counts"),
        "recipients": [
            {"agent_id": r["agent_id"], "channel": r.get("channel"), "notice_state": r.get("notice_state")}
            for r in correction.get("recipients") or ()
        ],
    }
