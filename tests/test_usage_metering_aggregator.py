"""Usage metering, buffer side: per-minute totals and the idempotent flush.

``common.usage.UsageAggregator`` adds model calls and savings up per minute and
sends them as one batch. A batch that failed is re-sent with the SAME id (so
storage counts it once even if the failed attempt landed), the waiting list is
bounded, and stopping makes a last flush.
"""

from __future__ import annotations

from datetime import UTC, datetime

from common.usage import (
    UNATTRIBUTED,
    ModelCall,
    UsageAggregator,
    UsageScope,
    ValueRecord,
    per_tenant,
)

T0 = datetime(2026, 10, 11, 12, 30, 15, tzinfo=UTC)


def _call(
    tenant="t1",
    *,
    ok=True,
    tokens=(10, 2, 1),
    operation="enrichment",
    agent="a1",
    minute=None,
):
    return ModelCall(
        scope=UsageScope(tenant_id=tenant, agent_id=agent, fleet_id="f1"),
        operation=operation,
        kind="llm",
        provider="openai",
        model="m",
        tokens_in=tokens[0],
        tokens_out=tokens[1],
        tokens_reasoning=tokens[2],
        ok=ok,
    )


class Recorder:
    def __init__(self, fail_times: int = 0) -> None:
        self.sent: list[dict] = []
        self.fail_times = fail_times

    async def __call__(self, batch: dict) -> None:
        if self.fail_times:
            self.fail_times -= 1
            raise RuntimeError("storage down")
        self.sent.append(batch)


async def test_calls_in_one_minute_add_up_to_one_row():
    post = Recorder()
    agg = UsageAggregator(post, clock=lambda: T0)
    agg.add_model_call(_call())
    agg.add_model_call(_call(tokens=(5, 5, 0)))
    agg.add_model_call(_call(ok=False))
    assert await agg.flush() == 1
    [row] = post.sent[0]["llm"]
    assert row["minute"] == "2026-10-11T12:30:00+00:00"
    assert (
        row["calls"],
        row["failures"],
        row["tokens_in"],
        row["tokens_out"],
        row["tokens_reasoning"],
    ) == (2, 1, 15, 7, 1)
    assert (row["tenant_id"], row["agent_id"], row["fleet_id"], row["operation"]) == (
        "t1",
        "a1",
        "f1",
        "enrichment",
    )


async def test_different_agents_and_operations_stay_apart():
    post = Recorder()
    agg = UsageAggregator(post, clock=lambda: T0)
    agg.add_model_call(_call(agent="a1"))
    agg.add_model_call(_call(agent="a2"))
    agg.add_model_call(_call(agent="a1", operation="contradiction"))
    await agg.flush()
    assert len(post.sent[0]["llm"]) == 3


async def test_a_call_with_no_tenant_lands_as_unattributed():
    post = Recorder()
    agg = UsageAggregator(post, clock=lambda: T0)
    agg.add_model_call(_call(tenant=None))
    await agg.flush()
    assert post.sent[0]["llm"][0]["tenant_id"] == UNATTRIBUTED


async def test_savings_add_up_per_metric():
    post = Recorder()
    agg = UsageAggregator(post, clock=lambda: T0)
    scope = UsageScope(tenant_id="t1", agent_id="a1", operation="search")
    agg.add_value(ValueRecord(scope=scope, metric="recall.source_tokens", quantity=400))
    agg.add_value(ValueRecord(scope=scope, metric="recall.source_tokens", quantity=100))
    agg.add_value(
        ValueRecord(scope=scope, metric="recall.returned_tokens", quantity=60)
    )
    await agg.flush()
    got = {r["metric"]: r["quantity"] for r in post.sent[0]["value"]}
    assert got == {"recall.source_tokens": 500, "recall.returned_tokens": 60}


async def test_nothing_recorded_sends_nothing():
    post = Recorder()
    assert await UsageAggregator(post).flush() == 0
    assert post.sent == []


async def test_a_failed_batch_is_resent_with_the_same_id_and_counted_once():
    post = Recorder(fail_times=1)
    agg = UsageAggregator(post, clock=lambda: T0)
    agg.add_model_call(_call())
    assert await agg.flush() == 0
    agg.add_model_call(_call(agent="later"))
    assert await agg.flush() == 2
    first, second = post.sent
    # The waiting batch goes first, unchanged; what arrived meanwhile is a new batch.
    assert first["batch_id"] != second["batch_id"]
    assert [r["agent_id"] for r in first["llm"]] == ["a1"]
    assert [r["agent_id"] for r in second["llm"]] == ["later"]


async def test_the_waiting_list_is_bounded_and_says_what_it_dropped(caplog):
    from common.usage import aggregator as mod

    post = Recorder(fail_times=10_000)
    agg = UsageAggregator(post, clock=lambda: T0)
    for i in range(mod.MAX_PENDING_BATCHES + 3):
        agg.add_model_call(_call(agent=f"a{i}"))
        await agg.flush()
    assert len(agg._pending) == mod.MAX_PENDING_BATCHES
    assert agg.dropped_rows == 3
    assert "dropped a batch" in caplog.text


async def test_stop_makes_a_last_flush():
    post = Recorder()
    agg = UsageAggregator(post, flush_interval=3600, clock=lambda: T0)
    agg.start()
    agg.add_model_call(_call())
    await agg.stop()
    assert len(post.sent) == 1


async def test_a_tenant_filter_drops_rows_for_tenants_that_opted_out():
    post = Recorder()

    async def allow(tenants: set[str]) -> set[str]:
        return {t for t in tenants if t != "off"}

    agg = UsageAggregator(post, allow_tenants=allow, clock=lambda: T0)
    agg.add_model_call(_call(tenant="on"))
    agg.add_model_call(_call(tenant="off"))
    await agg.flush()
    assert [r["tenant_id"] for r in post.sent[0]["llm"]] == ["on"]


def test_per_tenant_splits_a_batch_into_one_flush_per_tenant():
    batch = {
        "batch_id": "5f0e2b7a-8c55-4a43-9d7e-1f2a3b4c5d6e",
        "llm": [
            {"minute": "m", "tenant_id": "t1", "calls": 1},
            {"minute": "m", "tenant_id": "t2", "calls": 2},
        ],
        "value": [
            {
                "minute": "m",
                "tenant_id": "t1",
                "metric": "recall.source_tokens",
                "quantity": 9,
            }
        ],
    }
    parts = dict(per_tenant(batch))
    assert sorted(parts) == ["t1", "t2"]
    assert parts["t1"]["llm"] == [{"minute": "m", "calls": 1}]
    assert parts["t1"]["value"] == [
        {"minute": "m", "metric": "recall.source_tokens", "quantity": 9}
    ]
    assert parts["t2"] == {
        "batch_id": parts["t2"]["batch_id"],
        "llm": [{"minute": "m", "calls": 2}],
        "value": [],
    }
    # Each tenant's id differs, and a re-sent batch derives the same ones.
    assert parts["t1"]["batch_id"] != parts["t2"]["batch_id"] != batch["batch_id"]
    assert dict(per_tenant(batch))["t1"]["batch_id"] == parts["t1"]["batch_id"]
