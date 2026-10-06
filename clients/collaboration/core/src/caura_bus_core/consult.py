"""Bounded, read-only collection of correlated replies to one sent request.

The platform already owns request deadlines, recipient snapshots, per-recipient
reply state and overdue notices. This module only reads them: it never claims,
ACKs, cancels or renews anything, so stopping a collection (timeout, local
cancellation or task cancellation) never cancels work a recipient accepted.

Completion counts distinct expected recipients that sent a correlated
``kind=response``. A delivery ACK, progress report or any other message is not
an answer; responses to other requests or from unexpected senders are excluded.
"""

from __future__ import annotations

import asyncio
from collections import OrderedDict
from collections.abc import Iterable
from dataclasses import dataclass, field
from time import monotonic
from typing import Literal

import httpx

from .bus import PlatformError
from .retry import transient_status

# Host tool calls are typically cut off around a minute; stay well inside that.
DEFAULT_COLLECT_SECONDS = 30.0
MAX_COLLECT_SECONDS = 45.0
# A request is never answered by these reply states alone; they end the wait for that recipient.
CLOSED_REPLY_STATES = frozenset({"unanswered", "cancelled"})
MAX_RECENT_PAGES = 5

Outcome = Literal["complete", "partial", "no_reply", "cancelled"]


@dataclass(frozen=True)
class Answer:
    recipient: str
    """Expected recipient slot the answer completes (the original addressee)."""
    sender: str
    """Agent that actually answered; differs from recipient only after reassignment."""
    message_id: str
    body: str
    ts: int | None = None
    late: bool = False


@dataclass
class Collection:
    request_id: str
    expected: list[str]
    outcome: Outcome
    answers: dict[str, Answer] = field(default_factory=dict)
    pending: dict[str, dict] = field(default_factory=dict)
    """Recipients still awaiting a reply: reply_state, cause and reply_due_at when known."""
    closed: dict[str, dict] = field(default_factory=dict)
    """Recipients whose request was cancelled or marked unanswered without a reply."""
    excluded: int = 0
    """Messages read but not counted: unrelated, unexpected sender or duplicate."""
    elapsed_seconds: float = 0.0

    @property
    def complete(self) -> bool:
        return self.outcome == "complete"

    def summary(self) -> str:
        answered = ", ".join(sorted(self.answers)) or "nobody"
        parts = [f"{len(self.answers)}/{len(self.expected)} expected recipients answered ({answered})."]
        if self.pending:
            parts.append(
                f"No reply yet from {', '.join(sorted(self.pending))}; their requests stay accepted "
                "and may still be answered. Collect again later, or check status/requests."
            )
        if self.closed:
            parts.append(f"Closed without reply: {', '.join(sorted(self.closed))}.")
        if self.outcome == "cancelled":
            parts.append("Collection was stopped locally; nothing was cancelled on Caura.")
        if not self.answers:
            parts.append("Do not invent an answer; tell the human no reply arrived.")
        return " ".join(parts)


class PresentedResponses:
    """Per-session record of response message IDs already shown to the model.

    In memory and bounded (oldest IDs are forgotten first). It is not durable: after
    a process restart a queued response may be shown once more. This is presentation
    bookkeeping only, never an exactly-once guarantee for external effects.
    """

    def __init__(self, maximum: int = 4096):
        self.maximum = maximum
        self._ids: OrderedDict[str, None] = OrderedDict()

    def __contains__(self, message_id: object) -> bool:
        return message_id in self._ids

    def __len__(self) -> int:
        return len(self._ids)

    def add(self, message_id: str) -> bool:
        """Record an ID; return True only when it was not already presented."""
        if message_id in self._ids:
            self._ids.move_to_end(message_id)
            return False
        self._ids[message_id] = None
        while len(self._ids) > self.maximum:
            self._ids.popitem(last=False)
        return True


def _slots(deliveries: list[dict]) -> dict[str, str]:
    """Map each delivery ID to the expected recipient slot it serves (reassignment-aware)."""
    by_id = {d.get("id") or d.get("delivery_id"): d for d in deliveries}
    slots: dict[str, str] = {}

    def slot(delivery_id, seen=()):
        if delivery_id in slots:
            return slots[delivery_id]
        row = by_id.get(delivery_id)
        if row is None:
            return None
        parent = row.get("reassigned_from")
        if parent and parent in by_id and parent not in seen:
            found = slot(parent, (*seen, delivery_id))
            if found:
                slots[delivery_id] = found
                return found
        slots[delivery_id] = row["recipient"]
        return row["recipient"]

    for delivery_id in by_id:
        slot(delivery_id)
    return slots


class ResponseCollector:
    """Poll Caura for correlated responses to one request until a local bound."""

    def __init__(
        self,
        bus,
        request_id: str,
        expected: Iterable[str] | None = None,
        *,
        timeout: float = DEFAULT_COLLECT_SECONDS,
        poll_interval: float = 1.0,
        max_poll_interval: float = 5.0,
    ):
        if not 0 <= timeout <= MAX_COLLECT_SECONDS:
            raise ValueError(f"collect timeout must be 0–{MAX_COLLECT_SECONDS:g} seconds")
        self.bus = bus
        self.request_id = request_id
        self.expected: list[str] | None = sorted(set(expected)) if expected is not None else None
        self.timeout = timeout
        self.poll_interval = poll_interval
        self.max_poll_interval = max_poll_interval
        self.stopped = asyncio.Event()
        self.answers: dict[str, Answer] = {}
        self._seen: set[str] = set()
        self._excluded = 0
        self._deliveries: list[dict] = []

    def cancel(self) -> None:
        """Stop collecting locally. Accepted recipient work is left untouched."""
        self.stopped.set()

    async def collect(self) -> Collection:
        started = monotonic()
        deadline = started + self.timeout
        interval = self.poll_interval
        while True:
            await self._poll()
            outcome = self._outcome()
            if outcome is not None:
                return self._result(outcome, started)
            remaining = deadline - monotonic()
            if remaining <= 0:
                return self._result("partial" if self.answers else "no_reply", started)
            try:
                await asyncio.wait_for(self.stopped.wait(), min(interval, remaining))
            except TimeoutError:
                pass
            if self.stopped.is_set():
                return self._result("cancelled", started)
            interval = min(interval * 1.5, self.max_poll_interval)

    async def _read(self, call):
        try:
            return await call
        except PlatformError as exc:
            if not transient_status(exc.status):
                raise
        except httpx.TransportError:
            pass
        return None

    async def _poll(self):
        status = await self._read(self.bus.status(self.request_id))
        if status is not None:
            self._deliveries = list(status.get("deliveries", []))
            if self.expected is None:
                # Caura's recipient snapshot: original deliveries, not reassignment children.
                self.expected = sorted(
                    {d["recipient"] for d in self._deliveries if not d.get("reassigned_from")}
                )
        if self.expected is None:
            return
        before = None
        for _ in range(MAX_RECENT_PAGES):
            page = await self._read(self.bus.recent(reply_to=self.request_id, limit=100, before=before))
            if page is None:
                break
            for envelope in page.get("messages", []):
                self._consider(envelope)
            before = page.get("next_cursor")
            if not before or self._outcome() == "complete":
                break
        await self._fetch_recorded_replies()

    async def _fetch_recorded_replies(self):
        # Status may record a reply that is beyond the readable recent window.
        for row in self._deliveries:
            message_id = row.get("reply_message_id") or row.get("late_reply_message_id")
            slot = self._slot_of(row)
            if not message_id or message_id in self._seen or slot in self.answers:
                continue
            found = await self._read(self.bus.status(message_id))
            if found and found.get("envelope"):
                self._consider(found["envelope"])

    def _slot_of(self, row) -> str | None:
        return _slots(self._deliveries).get(row.get("id") or row.get("delivery_id"))

    def _sender_slots(self) -> dict[str, str]:
        mapping = {r: r for r in self.expected or []}
        slots = _slots(self._deliveries)
        for row in self._deliveries:
            slot = slots.get(row.get("id") or row.get("delivery_id"))
            if slot in mapping:
                mapping.setdefault(row["recipient"], slot)
        return mapping

    def _consider(self, envelope: dict):
        message_id = envelope.get("id")
        if not message_id or message_id in self._seen:
            return
        self._seen.add(message_id)
        sender = envelope.get("from") or envelope.get("from_")
        slot = self._sender_slots().get(sender)
        if (
            envelope.get("kind") != "response"
            or envelope.get("correlation_id") != self.request_id
            or slot is None
            or slot in self.answers
        ):
            self._excluded += 1
            return
        late = any(
            message_id in {r.get("reply_message_id"), r.get("late_reply_message_id")}
            and (r.get("late") or r.get("late_reply_message_id") == message_id)
            for r in self._deliveries
        )
        self.answers[slot] = Answer(
            recipient=slot,
            sender=sender,
            message_id=message_id,
            body=envelope.get("body", ""),
            ts=envelope.get("ts"),
            late=late,
        )

    def _states(self) -> tuple[dict[str, dict], dict[str, dict]]:
        pending: dict[str, dict] = {}
        closed: dict[str, dict] = {}
        slots = _slots(self._deliveries)
        latest: dict[str, dict] = {}
        for row in self._deliveries:
            slot = slots.get(row.get("id") or row.get("delivery_id"))
            if slot is not None:
                # A reassignment child supersedes its cancelled parent for state reporting.
                if slot not in latest or row.get("reassigned_from"):
                    latest[slot] = row
        for recipient in self.expected or []:
            if recipient in self.answers:
                continue
            row = latest.get(recipient, {})
            info = {
                "reply_state": row.get("reply_state"),
                "delivery_state": row.get("state"),
                "cause": row.get("cause"),
                "reply_due_at": row.get("reply_due_at"),
            }
            if row.get("reply_state") in CLOSED_REPLY_STATES:
                closed[recipient] = info | {"cancelled_reason": row.get("cancelled_reason")}
            else:
                pending[recipient] = info
        return pending, closed

    def _outcome(self) -> Outcome | None:
        if self.expected is None:
            return None
        if all(r in self.answers for r in self.expected):
            return "complete"
        pending, _ = self._states()
        if not pending:
            return "partial" if self.answers else "no_reply"
        return None

    def _result(self, outcome: Outcome, started: float) -> Collection:
        pending, closed = self._states()
        return Collection(
            request_id=self.request_id,
            expected=list(self.expected or []),
            outcome=outcome,
            answers=dict(self.answers),
            pending=pending,
            closed=closed,
            excluded=self._excluded,
            elapsed_seconds=round(monotonic() - started, 3),
        )


async def collect_responses(
    bus, request_id: str, expected: Iterable[str] | None = None, **options
) -> Collection:
    """Collect correlated replies with a local bound; see :class:`ResponseCollector`."""
    return await ResponseCollector(bus, request_id, expected, **options).collect()


class ConsultationLimitError(ValueError):
    """A consultation would exceed this task's request count or deadline."""


class ConsultationCycleError(ConsultationLimitError):
    """A new request would go to the peer that is already waiting on this task."""


@dataclass
class _Scope:
    started: float
    deadline: float
    sent: int = 0
    keys: set[str] = field(default_factory=set)


ROOT_SCOPE = "root"


class ConsultationBudget:
    """Bound how many requests one task may send and for how long it may consult.

    A scope is one unit of work: a claimed delivery (keyed by delivery ID) or, with
    nothing claimed, the agent's own turn (``ROOT_SCOPE``). A scope serving a claimed
    delivery never outlives that delivery's processing deadline. The root scope
    starts a fresh window once its deadline has passed, so a stuck root loop is
    rate-bounded rather than permanently blocked.

    One active lease does not prevent A→B→A deadlock: A can be collecting B's answer
    while B asks A, and neither claims the other's request. A direct back-edge (a new
    request to the sender of the request being handled) is refused immediately; longer
    cycles end when each hop's count or deadline is spent, because collection is
    clamped to the remaining scope time.
    """

    def __init__(self, max_requests: int = 4, deadline_seconds: float = 300, *, clock=monotonic):
        if max_requests < 1 or deadline_seconds <= 0:
            raise ValueError("consultation limits must be positive")
        self.max_requests = max_requests
        self.deadline_seconds = deadline_seconds
        self.clock = clock
        self._scopes: dict[str, _Scope] = {}

    def _scope(self, key: str, deadline_in: float | None = None) -> _Scope:
        now = self.clock()
        scope = self._scopes.get(key)
        if scope is None or (key == ROOT_SCOPE and now >= scope.deadline):
            limit = self.deadline_seconds if deadline_in is None else min(self.deadline_seconds, deadline_in)
            scope = self._scopes[key] = _Scope(started=now, deadline=now + max(0.0, limit))
        elif deadline_in is not None:
            scope.deadline = min(scope.deadline, now + max(0.0, deadline_in))
        return scope

    def admit(
        self,
        key: str,
        recipients: Iterable[str],
        *,
        waiting_sender: str | None = None,
        deadline_in: float | None = None,
        request_key: str | None = None,
    ) -> None:
        """Record one request or raise a model-readable limit error.

        Retrying the same ``request_key`` (an idempotent resend) is not counted again.
        """
        recipients = list(recipients)
        if waiting_sender is not None and waiting_sender in recipients:
            raise ConsultationCycleError(
                f"consultation cycle: {waiting_sender} is waiting on your reply to its request, so a new "
                "request to it would leave both sides waiting. Reply to its request instead (use "
                "reply with ack=false to ask a clarifying question), or answer with what you have."
            )
        scope = self._scope(key, deadline_in)
        if request_key is not None and request_key in scope.keys:
            return
        if scope.sent >= self.max_requests:
            raise ConsultationLimitError(
                f"consultation budget spent: {scope.sent} requests already sent for this task "
                f"(limit {self.max_requests}). Stop consulting; answer with the replies you have "
                "and say which peers did not respond."
            )
        if self.clock() >= scope.deadline:
            raise ConsultationLimitError(
                "consultation deadline passed for this task. Stop consulting; answer with the replies "
                "you have and say which peers did not respond."
            )
        scope.sent += 1
        if request_key is not None:
            scope.keys.add(request_key)

    def remaining(self, key: str, deadline_in: float | None = None) -> float:
        """Seconds left to consult in this scope (the full window for an unused scope)."""
        scope = self._scopes.get(key)
        if scope is None or (key == ROOT_SCOPE and self.clock() >= scope.deadline):
            limit = self.deadline_seconds if deadline_in is None else min(self.deadline_seconds, deadline_in)
            return max(0.0, limit)
        left = scope.deadline - self.clock()
        if deadline_in is not None:
            left = min(left, deadline_in)
        return max(0.0, left)

    def release(self, key: str) -> None:
        """Forget a finished delivery's scope."""
        if key != ROOT_SCOPE:
            self._scopes.pop(key, None)
