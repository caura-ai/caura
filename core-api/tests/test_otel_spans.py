"""OpenTelemetry GenAI memory spans (core_api.otel_spans).

Spans are captured with the SDK's in-memory exporter. The module's tracer is
swapped for one from a private provider rather than the global one, which the
OTel API lets a process set only once.
"""

from __future__ import annotations

import asyncio
import contextvars
import threading
import time
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import HTTPException
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import SpanKind, StatusCode
from opentelemetry.util._once import Once

from core_api import otel_spans
from core_api.services import agent_service, governance_gate, memory_service

CONTENT = "the customer's card ends in 4242 and they prefer email"
QUERY = "which payment card does the customer use"
OTEL_ENV = (
    "OTEL_EXPORTER_OTLP_ENDPOINT",
    "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT",
    "OTEL_SDK_DISABLED",
    "OTEL_SERVICE_NAME",
    "OTEL_RESOURCE_ATTRIBUTES",
    otel_spans.CAPTURE_CONTENT_ENV,
)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for name in OTEL_ENV:
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def exporter(monkeypatch):
    exp = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exp))
    monkeypatch.setattr(otel_spans, "_tracer", provider.get_tracer("test"))
    return exp


@pytest.fixture
def global_provider(monkeypatch):
    """Let a test set the process-wide provider, then put it back unset.

    The same reset ``opentelemetry.test.globals_test.reset_trace_globals``
    does; that helper ships in a package core-api does not install.
    """
    monkeypatch.setattr(trace, "_TRACER_PROVIDER", None)
    monkeypatch.setattr(trace, "_TRACER_PROVIDER_SET_ONCE", Once())
    monkeypatch.setattr(otel_spans, "_installed_provider", None)
    yield
    installed = otel_spans._installed_provider
    if installed is not None:
        installed.shutdown()


def _only(exporter: InMemorySpanExporter):
    spans = exporter.get_finished_spans()
    assert len(spans) == 1, [s.name for s in spans]
    return spans[0]


def _all_values(span) -> list[str]:
    values = [str(v) for v in span.attributes.values()]
    for event in span.events:
        values += [str(v) for v in event.attributes.values()]
    return values


def _memory_create(**overrides):
    fields = {
        "tenant_id": "t-1",
        "agent_id": "agent-a",
        "visibility": "scope_team",
        "content": CONTENT,
    }
    fields.update(overrides)
    return SimpleNamespace(**fields)


# ── Off by default ──


def test_nothing_is_installed_without_an_otlp_endpoint(global_provider):
    assert otel_spans.configure_tracing() is False
    assert isinstance(trace.get_tracer_provider(), trace.ProxyTracerProvider)
    with otel_spans.memory_span(otel_spans.OP_SEARCH, store_id="t-1", query=QUERY) as span:
        assert span.is_recording() is False


def test_sdk_disabled_wins_over_an_endpoint(global_provider, monkeypatch):
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://127.0.0.1:4318")
    monkeypatch.setenv("OTEL_SDK_DISABLED", "true")
    assert otel_spans.configure_tracing() is False
    assert isinstance(trace.get_tracer_provider(), trace.ProxyTracerProvider)


def test_an_endpoint_installs_the_otlp_provider(global_provider, monkeypatch):
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://127.0.0.1:4318")
    assert otel_spans.configure_tracing() is True
    provider = trace.get_tracer_provider()
    assert isinstance(provider, TracerProvider)
    assert provider.resource.attributes["service.name"] == "caura-core-api"


def test_operator_service_name_wins(global_provider, monkeypatch):
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT", "http://127.0.0.1:4318/v1/traces")
    monkeypatch.setenv("OTEL_SERVICE_NAME", "memory-eu")
    assert otel_spans.configure_tracing() is True
    assert trace.get_tracer_provider().resource.attributes["service.name"] == "memory-eu"


def test_a_provider_already_installed_is_kept(global_provider, monkeypatch):
    """ddtrace's OTel bridge installs its provider before the app starts."""
    theirs = TracerProvider()
    trace.set_tracer_provider(theirs)
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://127.0.0.1:4318")
    assert otel_spans.configure_tracing() is False
    assert trace.get_tracer_provider() is theirs
    assert otel_spans._installed_provider is None


# ── Span shape ──


async def test_create_span_carries_ids_and_sizes_never_content(exporter):
    record_id = uuid4()

    @otel_spans.trace_create
    async def create(data):
        return SimpleNamespace(id=record_id, content=data.content)

    await create(_memory_create())

    span = _only(exporter)
    assert span.name == "create_memory"
    assert span.kind == SpanKind.INTERNAL
    assert span.attributes == {
        "gen_ai.operation.name": "create_memory",
        "gen_ai.provider.name": "caura",
        "gen_ai.memory.store.id": "t-1",
        "gen_ai.agent.id": "agent-a",
        "caura.scope": "scope_team",
        "gen_ai.memory.record.count": 1,
        "caura.payload.bytes": len(CONTENT.encode()),
        "gen_ai.memory.record.id": str(record_id),
    }
    assert CONTENT not in _all_values(span)


async def test_bulk_create_counts_items_and_bytes(exporter):
    @otel_spans.trace_create_bulk
    async def create_bulk(data):
        return None

    items = [SimpleNamespace(content="é" * 3), SimpleNamespace(content="abc")]
    await create_bulk(SimpleNamespace(tenant_id="t-1", agent_id="a", visibility=None, items=items))

    span = _only(exporter)
    assert span.name == "create_memory"
    assert span.attributes["gen_ai.memory.record.count"] == 2
    assert span.attributes["caura.payload.bytes"] == 9
    assert "caura.scope" not in span.attributes


async def test_update_and_delete_name_the_record(exporter):
    memory_id = uuid4()

    @otel_spans.trace_update
    async def update(memory_id, tenant_id, data, agent_id=None):
        return None

    @otel_spans.trace_delete
    async def delete(memory_id, tenant_id):
        return None

    await update(memory_id, "t-1", SimpleNamespace(content=None, visibility="scope_agent"), agent_id="a")
    await delete(memory_id=memory_id, tenant_id="t-1")

    updated, deleted = exporter.get_finished_spans()
    assert updated.name == "update_memory"
    assert updated.attributes["gen_ai.memory.record.id"] == str(memory_id)
    assert updated.attributes["caura.scope"] == "scope_agent"
    assert "caura.payload.bytes" not in updated.attributes
    assert deleted.name == "delete_memory"
    assert deleted.attributes["gen_ai.memory.record.id"] == str(memory_id)
    assert deleted.attributes["gen_ai.memory.store.id"] == "t-1"


async def test_search_reports_results_not_their_content(exporter):
    @otel_spans.trace_search
    async def search(tenant_id, query, caller_agent_id=None):
        return [SimpleNamespace(content=CONTENT), SimpleNamespace(content="ok")]

    await search(tenant_id="t-1", query=QUERY, caller_agent_id="agent-a")

    span = _only(exporter)
    assert span.name == "search_memory"
    assert span.attributes["gen_ai.memory.record.count"] == 2
    assert span.attributes["caura.payload.bytes"] == len(CONTENT.encode()) + 2
    assert span.attributes["gen_ai.agent.id"] == "agent-a"
    assert "gen_ai.memory.query.text" not in span.attributes
    assert CONTENT not in _all_values(span)
    assert QUERY not in _all_values(span)


@pytest.mark.parametrize(
    ("setting", "captured"),
    [
        ("SPAN_ONLY", True),
        ("span_and_event", True),
        ("EVENT_ONLY", False),
        ("NO_CONTENT", False),
        ("true", False),
    ],
)
async def test_query_text_needs_the_span_opt_in(exporter, monkeypatch, setting, captured):
    monkeypatch.setenv(otel_spans.CAPTURE_CONTENT_ENV, setting)

    @otel_spans.trace_search
    async def search(tenant_id, query):
        return []

    await search("t-1", QUERY)

    attrs = _only(exporter).attributes
    assert (attrs.get("gen_ai.memory.query.text") == QUERY) is captured
    assert ("gen_ai.memory.query.text" in attrs) is captured


async def test_opt_in_never_exports_memory_content(exporter, monkeypatch):
    monkeypatch.setenv(otel_spans.CAPTURE_CONTENT_ENV, "SPAN_AND_EVENT")

    @otel_spans.trace_search
    async def search(tenant_id, query):
        return [SimpleNamespace(content=CONTENT)]

    await search("t-1", QUERY)

    span = _only(exporter)
    assert CONTENT not in _all_values(span)
    assert "gen_ai.memory.records" not in span.attributes


async def test_a_failed_operation_reports_its_type_not_its_message(exporter):
    @otel_spans.trace_create
    async def create(data):
        raise HTTPException(status_code=422, detail=f"rejected: {data.content}")

    with pytest.raises(HTTPException):
        await create(_memory_create())

    span = _only(exporter)
    assert span.attributes["error.type"] == "422"
    assert span.events == ()
    assert not any(CONTENT in v for v in _all_values(span))


@pytest.mark.parametrize(
    ("exc", "error_type", "status"),
    [
        (HTTPException(status_code=409), "409", StatusCode.UNSET),
        (HTTPException(status_code=503), "503", StatusCode.ERROR),
        (TimeoutError(), "TimeoutError", StatusCode.ERROR),
    ],
)
async def test_only_server_failures_mark_the_span_failed(exporter, exc, error_type, status):
    """A duplicate or a refusal is an answer; it must not count as an error."""

    @otel_spans.trace_create
    async def create(data):
        raise exc

    with pytest.raises(type(exc)):
        await create(_memory_create())

    span = _only(exporter)
    assert (span.attributes["error.type"], span.status.status_code) == (error_type, status)


async def test_a_broken_describer_never_fails_the_operation(exporter):
    @otel_spans.trace_create
    async def create(data):
        return "written"

    assert await create(object()) == "written"
    assert _only(exporter).attributes["gen_ai.operation.name"] == "create_memory"


# ── Policy decision and trust level ──


async def test_policy_decisions_land_on_the_operation_span(exporter):
    with otel_spans.memory_span(otel_spans.OP_CREATE, store_id="t-1"):
        otel_spans.record_policy_decision("pii_flag")
        otel_spans.record_policy_decision("nonbusiness_keep_private")

    span = _only(exporter)
    assert span.attributes["caura.policy_decision"] == ("pii_flag", "nonbusiness_keep_private")
    assert [e.name for e in span.events] == ["caura.policy_decision"] * 2
    assert [e.attributes["caura.policy_decision"] for e in span.events] == [
        "pii_flag",
        "nonbusiness_keep_private",
    ]


async def test_a_decision_after_the_operation_gets_its_own_span(exporter):
    """Fast-mode remediation decides in the background, after the write span ended."""
    with otel_spans.memory_span(otel_spans.OP_CREATE, store_id="t-1"):
        captured = contextvars.copy_context()

    captured.run(otel_spans.record_policy_decision, "pii_drop")

    write, decision = exporter.get_finished_spans()
    assert "caura.policy_decision" not in write.attributes
    assert decision.name == "caura.policy_decision"
    assert decision.attributes["caura.policy_decision"] == ("pii_drop",)
    assert decision.context.trace_id == write.context.trace_id


async def test_governance_audit_records_the_decision(exporter, monkeypatch):
    async def log_action(**kwargs):
        return None

    monkeypatch.setattr(governance_gate, "log_action", log_action)
    with otel_spans.memory_span(otel_spans.OP_CREATE, store_id="t-1"):
        await governance_gate.emit_governance_audit(
            tenant_id="t-1", agent_id="a", action=governance_gate.ACTION_PII_MASK, detail={}
        )

    assert _only(exporter).attributes["caura.policy_decision"] == ("pii_mask",)


async def test_write_agent_trust_level_reaches_the_span(exporter, monkeypatch):
    async def get_or_create_agent(*args, **kwargs):
        return {"agent_id": "agent-a", "trust_level": 2}

    monkeypatch.setattr(agent_service, "get_or_create_agent", get_or_create_agent)
    await agent_service.resolve_write_agent(
        "agent-a", "t-1", None, is_install_credential=False, install_uuid=None
    )
    with otel_spans.memory_span(otel_spans.OP_CREATE, store_id="t-1", agent_id="agent-a"):
        pass

    assert _only(exporter).attributes["caura.trust_level"] == 2


def test_a_trust_level_never_lands_on_another_agents_span(exporter):
    def run():
        otel_spans.note_caller_trust_level("agent-a", 3)
        with otel_spans.memory_span(otel_spans.OP_SEARCH, store_id="t-1", agent_id="agent-b"):
            pass
        with otel_spans.memory_span(otel_spans.OP_DELETE, store_id="t-1"):
            pass

    contextvars.copy_context().run(run)
    assert [s.attributes.get("caura.trust_level") for s in exporter.get_finished_spans()] == [None, None]


def test_reader_trust_level_reaches_the_span(exporter):
    def read():
        agent_service.refuse_restricted_reader({"trust_level": 1}, "agent-r")
        with otel_spans.memory_span(otel_spans.OP_SEARCH, store_id="t-1", agent_id="agent-r"):
            pass

    contextvars.copy_context().run(read)
    assert _only(exporter).attributes["caura.trust_level"] == 1


# ── The memory service is wired ──


async def test_create_memory_is_traced(exporter):
    with pytest.raises(ValueError):
        await memory_service.create_memory(_memory_create(agent_id=None))
    span = _only(exporter)
    assert (span.name, span.attributes["error.type"]) == ("create_memory", "ValueError")


async def test_create_memories_bulk_is_traced(exporter):
    data = SimpleNamespace(tenant_id="t-1", agent_id=None, visibility=None, items=[])
    with pytest.raises(ValueError):
        await memory_service.create_memories_bulk(data, bulk_attempt_id="x")
    span = _only(exporter)
    assert (span.name, span.attributes["gen_ai.memory.record.count"]) == ("create_memory", 0)


class _MissingRows:
    async def get_memory(self, *args, **kwargs):
        return None


async def test_update_and_soft_delete_are_traced(exporter, monkeypatch):
    monkeypatch.setattr(memory_service, "get_storage_client", lambda: _MissingRows())
    memory_id = uuid4()
    with pytest.raises(HTTPException):
        await memory_service.update_memory(memory_id, "t-1", SimpleNamespace(content="x", visibility=None))
    with pytest.raises(HTTPException):
        await memory_service.soft_delete_memory(memory_id, "t-1")

    updated, deleted = exporter.get_finished_spans()
    assert (updated.name, updated.attributes["error.type"]) == ("update_memory", "404")
    assert (deleted.name, deleted.attributes["error.type"]) == ("delete_memory", "404")
    assert deleted.attributes["gen_ai.memory.record.id"] == str(memory_id)


async def test_search_memories_is_traced(exporter, monkeypatch):
    async def pipeline(*args, **kwargs):
        # Tenant-bound, as the receipt annotation after the pipeline requires.
        return [{"id": "m-1", "tenant_id": "t-1", "content": "abc"}]

    monkeypatch.setattr(memory_service, "_search_memories_pipeline", pipeline)
    await memory_service.search_memories("t-1", QUERY, caller_agent_id="agent-a")

    span = _only(exporter)
    assert span.name == "search_memory"
    assert span.attributes["gen_ai.memory.record.count"] == 1


# ── Inside FastAPI's request span ──

TRACE_ID = "4bf92f3577b34da6a3ce929d0e0e4736"
PARENT_ID = "00f067aa0ba902b7"
TRACEPARENT = {"traceparent": f"00-{TRACE_ID}-{PARENT_ID}-01"}


def _probe_app():
    from fastapi import FastAPI

    probe = FastAPI(telemetry=otel_spans.FASTAPI_TELEMETRY)

    @probe.get("/probe")
    async def handler():
        with otel_spans.memory_span(otel_spans.OP_SEARCH, store_id="t-1"):
            return {"ok": True}

    return probe


async def _get(app, url):
    import httpx

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://core") as client:
        assert (await client.get(url, headers=TRACEPARENT)).status_code == 200


@pytest.fixture
def otlp_mode(global_provider, monkeypatch):
    """configure_tracing's own provider, with a local exporter beside OTLP's."""
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
    from opentelemetry.sdk.trace.export import SpanExportResult

    monkeypatch.setattr(OTLPSpanExporter, "export", lambda self, spans: SpanExportResult.SUCCESS)
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://127.0.0.1:9")
    assert otel_spans.configure_tracing() is True
    exp = InMemorySpanExporter()
    otel_spans._installed_provider.add_span_processor(SimpleSpanProcessor(exp))
    monkeypatch.setattr(otel_spans, "_tracer", trace.get_tracer("core_api.memory"))
    return exp


def test_only_shutdown_tracing_shuts_the_provider_down(otlp_mode):
    """The SDK's atexit hook would join the export thread again, with no bound."""
    assert trace.get_tracer_provider()._atexit_handler is None


class _Provider:
    def __init__(self, stuck: bool = False):
        self.calls: list[str] = []
        self.release = threading.Event()
        self.stuck = stuck

    def force_flush(self, timeout_millis):
        self.calls.append("force_flush")

    def shutdown(self):
        self.calls.append("shutdown")
        if self.stuck:
            self.release.wait(10)


async def test_shutdown_flushes_then_stops(monkeypatch):
    provider = _Provider()
    monkeypatch.setattr(otel_spans, "_installed_provider", provider)
    await otel_spans.shutdown_tracing()
    assert provider.calls == ["force_flush", "shutdown"]


async def test_shutdown_gives_up_at_its_deadline(monkeypatch, caplog):
    """An export stuck on an unreachable endpoint must not eat the 10s SIGTERM window."""
    provider = _Provider(stuck=True)
    monkeypatch.setattr(otel_spans, "_installed_provider", provider)
    started = time.monotonic()
    try:
        await asyncio.wait_for(otel_spans.shutdown_tracing(timeout_seconds=0.2), 3)
    finally:
        provider.release.set()
    assert time.monotonic() - started < 1.5
    assert "still running" in caplog.text


async def test_memory_spans_nest_in_the_request_span_of_the_callers_trace(otlp_mode):
    await _get(_probe_app(), "/probe")

    spans = otlp_mode.get_finished_spans()
    by_id = {s.context.span_id: s for s in spans}
    (server,) = [s for s in spans if s.kind == SpanKind.SERVER]
    (memory,) = [s for s in spans if s.name == "search_memory"]
    assert format(server.context.trace_id, "032x") == TRACE_ID
    assert format(server.parent.span_id, "016x") == PARENT_ID
    ancestors, span = [], memory
    while span.parent is not None and span.parent.span_id in by_id:
        span = by_id[span.parent.span_id]
        ancestors.append(span.name)
    # FastAPI's own operation span (fastapi.endpoint) sits between the two.
    assert ancestors[-1] == server.name
    assert span is server


async def test_query_string_values_are_redacted(otlp_mode):
    await _get(_probe_app(), "/probe?search=Alice+Cooper&tenant_id=t-1")

    (server,) = [s for s in otlp_mode.get_finished_spans() if s.kind == SpanKind.SERVER]
    assert server.attributes["url.query"] == "search=REDACTED&tenant_id=REDACTED"


async def test_query_string_values_are_kept_under_the_opt_in(otlp_mode, monkeypatch):
    monkeypatch.setenv(otel_spans.CAPTURE_CONTENT_ENV, "SPAN_ONLY")
    await _get(_probe_app(), "/probe?search=Alice")

    (server,) = [s for s in otlp_mode.get_finished_spans() if s.kind == SpanKind.SERVER]
    assert server.attributes["url.query"] == "search=Alice"


async def test_under_ddtrace_fastapi_adds_no_request_spans(global_provider, monkeypatch):
    """ddtrace already traces the request and reads traceparent itself."""
    ddtrace_provider = type("TracerProvider", (TracerProvider,), {"__module__": "ddtrace.opentelemetry"})()
    exp = InMemorySpanExporter()
    ddtrace_provider.add_span_processor(SimpleSpanProcessor(exp))
    trace.set_tracer_provider(ddtrace_provider)
    monkeypatch.setattr(otel_spans, "_tracer", trace.get_tracer("core_api.memory"))

    await _get(_probe_app(), "/probe")

    assert [s.name for s in exp.get_finished_spans()] == ["search_memory"]


async def test_under_an_operators_provider_fastapi_adds_no_raw_query_string(global_provider, monkeypatch):
    """Their provider has no redactor of ours; their own HTTP instrumentation decides."""
    theirs = TracerProvider()
    exp = InMemorySpanExporter()
    theirs.add_span_processor(SimpleSpanProcessor(exp))
    trace.set_tracer_provider(theirs)
    monkeypatch.setattr(otel_spans, "_tracer", trace.get_tracer("core_api.memory"))

    await _get(_probe_app(), "/probe?search=Alice")

    assert [s.name for s in exp.get_finished_spans()] == ["search_memory"]
    assert not any("Alice" in v for s in exp.get_finished_spans() for v in _all_values(s))


def test_the_app_uses_this_telemetry_config():
    from core_api.app import app

    assert app._telemetry["exclude"] is otel_spans.FASTAPI_TELEMETRY["exclude"]
    assert app._telemetry["logs"] is False
