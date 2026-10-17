"""Multi-peer completion against Caura's existing request lifecycle, read client-side.

The fake below mirrors the platform's read shapes: ``status`` returns the request
envelope plus per-recipient deliveries (state, reply_state, reply_due_at,
reply_message_id, late, reassigned_from), and ``recent(reply_to=...)`` returns
visible envelopes newest first. Tests mutate it between polls to model replies
arriving over time.
"""

import asyncio

import httpx
import pytest
from caura_bus_core import PlatformError, PresentedResponses, ResponseCollector, collect_responses

REQUEST = "msg_request"


def envelope(message_id, sender, *, kind="response", correlation_id=REQUEST, body="answer", to=("a",)):
    return {
        "id": message_id,
        "from": sender,
        "to": list(to),
        "kind": kind,
        "thread_id": "t1",
        "ts": 1,
        "body": body,
        "correlation_id": correlation_id,
    }


def delivery(recipient, *, state="pending", reply_state="awaiting", **extra):
    return {
        "id": f"d-{recipient}",
        "recipient": recipient,
        "state": state,
        "reply_state": reply_state,
        "reply_due_at": "2030-01-01T00:00:00+00:00",
        "cause": None,
        "reply_message_id": None,
        "late": None,
        "late_reply_message_id": None,
        "cancelled_reason": None,
        "reassigned_from": None,
        **extra,
    }


class FakeCaura:
    def __init__(self, recipients=("b", "c"), *, honour_reply_to=True):
        self.deliveries = {r: delivery(r) for r in recipients}
        self.messages = []  # oldest first
        self.honour_reply_to = honour_reply_to
        self.calls = []
        self.on_poll = []  # callables run before the Nth status read
        self.fail_next = None
        self.page_size = None

    def answer(self, recipient, message_id, *, body="answer", late=False, sender=None):
        self.messages.append(envelope(message_id, sender or recipient, body=body))
        row = self.deliveries[recipient]
        row.update(reply_state="replied", reply_message_id=message_id, state="acked", late=late)

    async def status(self, message_id):
        self.calls.append(("status", message_id))
        if self.fail_next:
            exc, self.fail_next = self.fail_next, None
            raise exc
        if message_id == REQUEST:
            polls = sum(1 for c in self.calls if c == ("status", REQUEST))
            for step in [s for n, s in self.on_poll if n == polls]:
                step()
            return {
                "envelope": envelope(REQUEST, "a", kind="request", correlation_id=None, to=self.deliveries),
                "deliveries": list(self.deliveries.values()),
            }
        for message in self.messages:
            if message["id"] == message_id:
                return {"envelope": message, "deliveries": []}
        raise PlatformError(404, "message unavailable")

    async def recent(self, *, reply_to=None, limit=20, before=None, **_):
        self.calls.append(("recent", reply_to, before))
        rows = list(reversed(self.messages))
        if self.honour_reply_to and reply_to:
            rows = [m for m in rows if m["kind"] == "response" and m["correlation_id"] == reply_to]
        start = int(before) if before else 0
        size = self.page_size or limit
        page = rows[start : start + size]
        cursor = str(start + size) if start + size < len(rows) else None
        return {"messages": page, "next_cursor": cursor}


def collector(fake, expected=("b", "c"), **options):
    options.setdefault("timeout", 2)
    options.setdefault("poll_interval", 0.01)
    options.setdefault("max_poll_interval", 0.02)
    return ResponseCollector(fake, REQUEST, expected, **options)


async def test_fan_out_completes_on_distinct_expected_recipients_out_of_order():
    fake = FakeCaura()
    fake.on_poll += [
        (2, lambda: fake.answer("c", "r-c", body="window: Tue")),
        (4, lambda: fake.answer("b", "r-b")),
    ]
    result = await collector(fake).collect()
    assert result.outcome == "complete" and result.complete
    assert {r: a.message_id for r, a in result.answers.items()} == {"b": "r-b", "c": "r-c"}
    assert result.answers["c"].body == "window: Tue" and result.answers["c"].sender == "c"
    assert result.pending == {} and "2/2 expected recipients answered (b, c)" in result.summary()


async def test_delivery_ack_and_progress_are_not_answers():
    fake = FakeCaura(("b",))
    # b claimed and ACKed the request but never replied; a non-response message is in the thread.
    fake.deliveries["b"].update(state="acked")
    fake.messages.append(envelope("p-b", "b", kind="info", body="working on it"))
    fake.honour_reply_to = False
    result = await collector(fake, ("b",), timeout=0.1).collect()
    assert result.outcome == "no_reply" and result.answers == {}
    assert result.pending["b"]["delivery_state"] == "acked"
    assert result.pending["b"]["reply_state"] == "awaiting"
    assert "Do not invent an answer" in result.summary()


async def test_unrelated_and_unexpected_responses_are_excluded():
    fake = FakeCaura(("b",), honour_reply_to=False)  # an older server ignores reply_to
    fake.messages += [
        envelope("other", "b", correlation_id="msg_other_request"),
        envelope("stranger", "d"),
        envelope("req", "b", kind="request", correlation_id=None),
    ]
    result = await collector(fake, ("b",), timeout=0.1).collect()
    assert result.outcome == "no_reply" and result.excluded == 3
    fake.answer("b", "r-b")
    result = await collector(fake, ("b",)).collect()
    assert result.outcome == "complete" and result.answers["b"].message_id == "r-b"


async def test_duplicate_responses_from_one_recipient_count_once():
    fake = FakeCaura()
    fake.answer("b", "r-b1", body="first")
    fake.messages.append(envelope("r-b2", "b", body="second"))
    result = await collector(fake, timeout=0.1).collect()
    assert result.outcome == "partial"
    assert list(result.answers) == ["b"] and result.answers["b"].body in {"first", "second"}
    assert result.excluded == 1 and list(result.pending) == ["c"]


async def test_partial_completion_terminates_at_local_timeout_with_attribution():
    fake = FakeCaura()
    fake.answer("b", "r-b", body="code: 7731")
    started = asyncio.get_running_loop().time()
    result = await collector(fake, timeout=0.3).collect()
    assert asyncio.get_running_loop().time() - started < 1
    assert result.outcome == "partial"
    assert result.answers["b"].body == "code: 7731" and result.answers["b"].recipient == "b"
    assert result.pending["c"]["reply_state"] == "awaiting"
    summary = result.summary()
    assert "1/2" in summary and "No reply yet from c" in summary and "stay accepted" in summary


async def test_closed_recipients_end_collection_before_timeout():
    fake = FakeCaura()
    fake.answer("b", "r-b")
    fake.deliveries["c"].update(reply_state="unanswered", state="acked")
    result = await collector(fake, timeout=30).collect()
    assert result.outcome == "partial" and result.elapsed_seconds < 1
    assert list(result.closed) == ["c"] and result.pending == {}


async def test_late_answer_is_picked_up_by_a_later_collection():
    fake = FakeCaura()
    fake.answer("b", "r-b")
    first = await collector(fake, timeout=0.05).collect()
    assert first.outcome == "partial"
    fake.answer("c", "r-c", late=True)
    second = await collector(fake).collect()
    assert second.outcome == "complete" and second.answers["c"].late is True
    assert second.answers["b"].late is False


async def test_reply_after_cancellation_is_reported_as_late():
    fake = FakeCaura(("b",))
    fake.messages.append(envelope("r-b", "b"))
    fake.deliveries["b"].update(reply_state="cancelled", state="cancelled", late_reply_message_id="r-b")
    result = await collector(fake, ("b",)).collect()
    assert result.outcome == "complete" and result.answers["b"].late is True


async def test_local_cancel_returns_partial_and_mutates_nothing():
    fake = FakeCaura()
    fake.answer("b", "r-b")
    running = collector(fake, timeout=10)
    task = asyncio.create_task(running.collect())
    await asyncio.sleep(0.05)
    running.cancel()
    result = await asyncio.wait_for(task, 1)
    assert result.outcome == "cancelled" and list(result.answers) == ["b"]
    assert "nothing was cancelled on Caura" in result.summary()
    assert {c[0] for c in fake.calls} <= {"status", "recent"}
    assert fake.deliveries["c"]["reply_state"] == "awaiting"


async def test_task_cancellation_propagates_without_side_effects():
    fake = FakeCaura()
    task = asyncio.create_task(collector(fake, timeout=10).collect())
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert {c[0] for c in fake.calls} <= {"status", "recent"}


async def test_expected_recipients_default_to_the_request_snapshot_and_follow_reassignment():
    fake = FakeCaura()
    fake.deliveries["b"].update(reply_state="cancelled", state="cancelled", cancelled_reason="reassigned")
    fake.deliveries["e"] = delivery("e", reassigned_from="d-b")
    fake.messages.append(envelope("r-e", "e"))
    fake.deliveries["e"].update(reply_state="replied", reply_message_id="r-e")
    fake.answer("c", "r-c")
    result = await ResponseCollector(fake, REQUEST, timeout=1, poll_interval=0.01).collect()
    assert result.expected == ["b", "c"]
    assert result.outcome == "complete"
    assert result.answers["b"].sender == "e" and result.answers["b"].recipient == "b"


async def test_recorded_reply_outside_the_recent_window_is_fetched_by_id():
    fake = FakeCaura(("b",))
    fake.answer("b", "r-b")
    for i in range(12):
        fake.messages.append(envelope(f"noise-{i}", "b", correlation_id="msg_other"))
    fake.honour_reply_to = False
    fake.page_size = 2
    result = await collector(fake, ("b",)).collect()
    assert result.outcome == "complete" and ("status", "r-b") in fake.calls


async def test_transient_read_failures_are_retried_and_rejections_raise():
    fake = FakeCaura(("b",))
    fake.answer("b", "r-b")
    fake.fail_next = httpx.ConnectError("down")
    assert (await collector(fake, ("b",)).collect()).outcome == "complete"
    fake.fail_next = PlatformError(503, "busy")
    assert (await collector(fake, ("b",)).collect()).outcome == "complete"
    fake.fail_next = PlatformError(404, "request is unavailable")
    with pytest.raises(PlatformError):
        await collector(fake, ("b",)).collect()


async def test_collection_timeout_stays_below_host_limits():
    with pytest.raises(ValueError):
        ResponseCollector(FakeCaura(), REQUEST, timeout=46)
    result = await collect_responses(FakeCaura(), REQUEST, ["b"], timeout=0)
    assert result.outcome == "no_reply" and result.pending


def test_presented_responses_are_bounded_and_report_first_presentation():
    seen = PresentedResponses(maximum=2)
    assert seen.add("m1") and not seen.add("m1")
    seen.add("m2")
    seen.add("m3")
    assert "m1" not in seen and "m3" in seen and len(seen) == 2
