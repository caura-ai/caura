"""Usage metering, capture side: scopes, attribution and the provider hooks.

What a model call is recorded under (common/usage), that each provider family
reports the provider's own token counts, and that the retry layer counts a
failed attempt without double-counting a billed one.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from common.llm.call_context import llm_call_label
from common.llm.providers._genai_usage import genai_usage_tokens
from common.usage import (
    UNATTRIBUTED,
    ModelCall,
    ValueRecord,
    operation_from_label,
    record_model_call,
    record_value,
    set_usage_sink,
    usage_scope,
)


class ListSink:
    def __init__(self) -> None:
        self.calls: list[ModelCall] = []
        self.values: list[ValueRecord] = []

    def add_model_call(self, call: ModelCall) -> None:
        self.calls.append(call)

    def add_value(self, record: ValueRecord) -> None:
        self.values.append(record)


@pytest.fixture
def sink():
    s = ListSink()
    set_usage_sink(s)
    yield s
    set_usage_sink(None)


def test_a_call_is_recorded_under_the_scope_and_the_retry_label(sink):
    with usage_scope(tenant_id="t1", agent_id="a1", fleet_id="f1", operation="write"):
        token = llm_call_label.set("enrichment-primary")
        try:
            record_model_call(
                kind="llm",
                provider="openai",
                model="m",
                tokens_in=10,
                tokens_out=3,
                tokens_reasoning=2,
            )
        finally:
            llm_call_label.reset(token)
    [call] = sink.calls
    assert (call.scope.tenant_id, call.scope.agent_id, call.scope.fleet_id) == (
        "t1",
        "a1",
        "f1",
    )
    assert call.operation == "enrichment"
    assert (call.tokens_in, call.tokens_out, call.tokens_reasoning, call.ok) == (
        10,
        3,
        2,
        True,
    )


def test_without_a_retry_label_the_scope_names_the_operation(sink):
    with usage_scope(tenant_id="t1", operation="crystallize"):
        record_model_call(kind="llm", provider="openai", model="m")
    assert sink.calls[0].operation == "crystallize"


def test_an_explicit_operation_wins(sink):
    with usage_scope(tenant_id="t1", operation="write"):
        record_model_call(
            kind="embedding", provider="openai", model="e", operation="embed"
        )
    assert sink.calls[0].operation == "embed"


def test_a_call_outside_any_scope_is_recorded_as_unattributed_not_dropped(sink):
    record_model_call(kind="llm", provider="openai", model="m", tokens_in=5)
    assert sink.calls[0].scope.tenant_id == UNATTRIBUTED
    assert sink.calls[0].operation == "unlabelled"


def test_an_inner_scope_fills_in_only_what_it_names():
    with usage_scope(tenant_id="t1", agent_id="a1", operation="write"):
        with usage_scope(operation="entity_extraction") as inner:
            assert (inner.tenant_id, inner.agent_id, inner.operation) == (
                "t1",
                "a1",
                "entity_extraction",
            )


async def test_the_scope_reaches_a_worker_thread_and_a_spawned_task(sink):
    def in_thread():
        record_model_call(kind="llm", provider="vertex", model="g")

    async def in_task():
        record_model_call(kind="llm", provider="openai", model="m")

    with usage_scope(tenant_id="t9"):
        await asyncio.to_thread(in_thread)
        await asyncio.create_task(in_task())
    assert [c.scope.tenant_id for c in sink.calls] == ["t9", "t9"]


def test_the_retry_tier_is_not_part_of_the_operation():
    assert (
        operation_from_label("contradiction_batch-fallback-openai")
        == "contradiction_batch"
    )
    assert operation_from_label("enrichment-primary") == "enrichment"
    assert operation_from_label("recall") == "recall"
    assert operation_from_label("") == ""


def test_a_value_needs_a_tenant_and_a_positive_quantity(sink):
    record_value("recall.source_tokens", 100)
    with usage_scope(tenant_id="t1", agent_id="a1"):
        record_value("recall.source_tokens", 0)
        record_value("recall.source_tokens", 120)
    assert [(v.scope.tenant_id, v.metric, v.quantity) for v in sink.values] == [
        ("t1", "recall.source_tokens", 120)
    ]


def test_a_failing_sink_never_fails_the_call():
    class Broken:
        def add_model_call(self, call):
            raise RuntimeError("boom")

        def add_value(self, record):
            raise RuntimeError("boom")

    set_usage_sink(Broken())
    try:
        record_model_call(kind="llm", provider="openai", model="m")
        with usage_scope(tenant_id="t1"):
            record_value("x", 1)
    finally:
        set_usage_sink(None)


def test_genai_usage_reads_prompt_output_and_thoughts():
    response = SimpleNamespace(
        usage_metadata=SimpleNamespace(
            prompt_token_count=40, candidates_token_count=12, thoughts_token_count=30
        )
    )
    assert genai_usage_tokens(response) == (40, 12, 30)
    assert genai_usage_tokens(SimpleNamespace(usage_metadata=None)) == (0, 0, 0)
    assert genai_usage_tokens(
        SimpleNamespace(usage_metadata=SimpleNamespace(prompt_token_count=None))
    ) == (0, 0, 0)


async def test_openai_compatible_llm_records_the_billed_tokens(sink):
    from common.llm.providers.openai import OpenAILLMProvider

    response = SimpleNamespace(
        usage=SimpleNamespace(
            prompt_tokens=50,
            completion_tokens=20,
            completion_tokens_details=SimpleNamespace(reasoning_tokens=15),
        ),
        choices=[
            SimpleNamespace(
                message=SimpleNamespace(content="hello"), finish_reason="stop"
            )
        ],
    )

    async def create(**_):
        return response

    provider = OpenAILLMProvider.__new__(OpenAILLMProvider)
    provider._model = "gpt-test"
    provider._provider_name = "openai"
    provider._client = SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=create))
    )
    with usage_scope(tenant_id="t1", agent_id="a1"):
        assert await provider.complete_text("hi") == "hello"
    [call] = sink.calls
    assert (
        call.kind,
        call.provider,
        call.model,
        call.tokens_in,
        call.tokens_out,
        call.tokens_reasoning,
    ) == ("llm", "openai", "gpt-test", 50, 20, 15)


async def test_openai_compatible_embeddings_record_their_tokens(sink):
    from common.embedding.providers.openai import OpenAIEmbeddingProvider

    async def create(**kwargs):
        texts = (
            kwargs["input"] if isinstance(kwargs["input"], list) else [kwargs["input"]]
        )
        return SimpleNamespace(
            usage=SimpleNamespace(prompt_tokens=7 * len(texts)),
            data=[
                SimpleNamespace(index=i, embedding=[0.0]) for i, _ in enumerate(texts)
            ],
        )

    provider = OpenAIEmbeddingProvider.__new__(OpenAIEmbeddingProvider)
    provider._model = "emb-test"
    provider._send_dimensions = False
    provider._client = SimpleNamespace(embeddings=SimpleNamespace(create=create))
    provider._postprocess = lambda emb: emb
    provider._provider_label = "openai"
    with usage_scope(tenant_id="t1"):
        await provider.embed("one")
        await provider._embed_batch_chunk(["a", "b"])
    assert [(c.kind, c.operation, c.tokens_in) for c in sink.calls] == [
        ("embedding", "embed", 7),
        ("embedding", "embed", 14),
    ]


async def test_the_retry_layer_counts_a_failed_attempt_once_and_a_success_not_at_all(
    sink,
):
    from common.llm.retry import call_with_fallback

    attempts = {"n": 0}
    provider = SimpleNamespace(provider_name="openai", model="m", is_fake=False)

    async def call_fn(p):
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise RuntimeError("transient")
        return "ok"

    with usage_scope(tenant_id="t1"):
        result = await call_with_fallback(
            "openai",
            call_fn,
            lambda: "fake",
            service_label="enrichment",
            provider_factory=lambda *a, **k: provider,
            max_attempts=2,
        )
    assert result == "ok"
    # The fake call_fn bills nothing, so only the failure is recorded.
    assert [(c.ok, c.operation, c.provider) for c in sink.calls] == [
        (False, "enrichment", "openai")
    ]


def test_a_registered_resolver_fills_what_the_scope_leaves_blank(sink):
    from dataclasses import replace

    from common.usage import UsageScope, set_scope_resolver

    def resolver(scope):
        base = scope or UsageScope()
        return replace(
            base,
            tenant_id=base.tenant_id or "signed-in",
            agent_id=base.agent_id or "mcp-agent",
        )

    set_scope_resolver(resolver)
    try:
        record_model_call(kind="llm", provider="openai", model="m")
        with usage_scope(tenant_id="explicit"):
            record_model_call(kind="llm", provider="openai", model="m")
    finally:
        set_scope_resolver(None)
    assert [(c.scope.tenant_id, c.scope.agent_id) for c in sink.calls] == [
        ("signed-in", "mcp-agent"),
        ("explicit", "mcp-agent"),
    ]


async def test_scoped_reads_the_names_from_the_arguments(sink):
    from common.usage import scoped

    @scoped("write", tenant=lambda a: a["data"]["tenant"], agent="agent_id")
    async def write(data, agent_id=None):
        record_model_call(kind="llm", provider="openai", model="m")
        return "done"

    assert await write({"tenant": "t1"}, agent_id="a1") == "done"
    assert (
        sink.calls[0].scope.tenant_id,
        sink.calls[0].scope.agent_id,
        sink.calls[0].operation,
    ) == ("t1", "a1", "write")
