"""OpenTelemetry GenAI memory spans for Caura's memory operations.

Every search, create, update and delete runs inside a span named after its
``gen_ai.operation.name`` (``search_memory``, ``create_memory``,
``update_memory``, ``delete_memory``), so Langfuse, Datadog, Phoenix or any
OTLP backend shows Caura's work inside the agent's own trace.

The GenAI memory conventions are still "Development" and live in
open-telemetry/semantic-conventions-genai, which has published no release yet.
The attribute names below are pinned to ``SEMCONV_GENAI_COMMIT``; moving to a
newer revision is a deliberate edit here, not something a dependency bump
does behind our back.

Off unless an operator turns it on:

- No ``OTEL_EXPORTER_OTLP_ENDPOINT`` / ``OTEL_EXPORTER_OTLP_TRACES_ENDPOINT``:
  :func:`configure_tracing` installs nothing, and the spans go to the OTel
  API's no-op tracer.
- Under ``ddtrace-run`` with ``DD_TRACE_OTEL_ENABLED=true``, ddtrace installs
  its own tracer provider first. :func:`configure_tracing` leaves it in place,
  so the memory spans become ddtrace spans under ddtrace's request span.
- ``OTEL_SDK_DISABLED=true`` turns everything off.

FastAPI (0.142+) can trace each request itself: it reads the caller's
``traceparent`` and opens the HTTP server span the memory spans nest under.
:data:`FASTAPI_TELEMETRY` lets it do so only into the provider
:func:`configure_tracing` installed, whose span processor redacts the query
string values it records. Under ddtrace, or a provider an operator installed,
that tracer's own HTTP instrumentation traces the request instead.

Memory content is never exported. The query text of a search is exported only
when ``OTEL_INSTRUMENTATION_GENAI_CAPTURE_CONTENT`` is ``SPAN_ONLY`` or
``SPAN_AND_EVENT``, the opt-in the conventions mandate. Exception messages are
not recorded either (they can quote content); a failed operation carries only
``error.type``.
"""

from __future__ import annotations

import asyncio
import contextvars
import functools
import logging
import os
import threading
from collections.abc import Callable, Coroutine, Iterator, MutableMapping
from contextlib import contextmanager
from typing import Any, ParamSpec, TypeVar
from urllib.parse import parse_qsl, urlencode

from fastapi import HTTPException
from fastapi.telemetry import TelemetryConfig
from opentelemetry import trace
from opentelemetry.trace import Span, SpanKind, Status, StatusCode

logger = logging.getLogger(__name__)

# open-telemetry/semantic-conventions-genai @ 2026-11-06 (docs/gen-ai/gen-ai-spans.md#memory).
SEMCONV_GENAI_COMMIT = "e6658f14c9d9ea0760825f029cb82e7673f48fab"

OP_SEARCH = "search_memory"
OP_CREATE = "create_memory"
OP_UPDATE = "update_memory"
OP_DELETE = "delete_memory"

PROVIDER_NAME = "caura"

ATTR_OPERATION = "gen_ai.operation.name"
ATTR_PROVIDER = "gen_ai.provider.name"
ATTR_AGENT_ID = "gen_ai.agent.id"
ATTR_STORE_ID = "gen_ai.memory.store.id"
ATTR_RECORD_ID = "gen_ai.memory.record.id"
ATTR_RECORD_COUNT = "gen_ai.memory.record.count"
ATTR_QUERY_TEXT = "gen_ai.memory.query.text"
ATTR_ERROR_TYPE = "error.type"
ATTR_SCOPE = "caura.scope"
ATTR_TRUST_LEVEL = "caura.trust_level"
ATTR_POLICY_DECISION = "caura.policy_decision"
ATTR_PAYLOAD_BYTES = "caura.payload.bytes"

POLICY_DECISION_EVENT = "caura.policy_decision"

CAPTURE_CONTENT_ENV = "OTEL_INSTRUMENTATION_GENAI_CAPTURE_CONTENT"
_SPAN_CAPTURE_VALUES = frozenset({"SPAN_ONLY", "SPAN_AND_EVENT"})

_tracer = trace.get_tracer(
    "core_api.memory",
    attributes={"caura.semconv.genai.commit": SEMCONV_GENAI_COMMIT},
)

# The memory span the current operation runs in, for annotations made deeper
# in the call (a governance decision). A contextvar rather than
# ``trace.get_current_span()``: a span opened inside the operation (ddtrace's
# own, an HTTP client's) must not receive them.
_active_span: contextvars.ContextVar[MemorySpan | None] = contextvars.ContextVar(
    "caura_memory_span", default=None
)
# (agent id, trust level) of the agent the request acts as, noted where the
# request resolves that agent. A memory span takes the level only when it acts
# as the same agent, so a level can never be reported for someone else.
_caller_trust_level: contextvars.ContextVar[tuple[str, int] | None] = contextvars.ContextVar(
    "caura_caller_trust_level", default=None
)

# The provider configure_tracing() installed, for shutdown_tracing() to flush.
_installed_provider: Any = None


def _env_true(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in {"1", "true", "yes", "on"}


def otlp_configured() -> bool:
    """Whether the environment asks for OTLP trace export."""
    if _env_true("OTEL_SDK_DISABLED"):
        return False
    return bool(
        os.environ.get("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT", "").strip()
        or os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT", "").strip()
    )


def capture_query_text() -> bool:
    """The conventions' content opt-in, parsed case-insensitively. Any other
    value, including an unrecognised one, means no content."""
    return os.environ.get(CAPTURE_CONTENT_ENV, "").strip().upper() in _SPAN_CAPTURE_VALUES


def configure_tracing(service_name: str = "caura-core-api") -> bool:
    """Install an OTLP/HTTP span exporter when the environment asks for one.

    Returns whether this call installed a provider. A provider someone else
    already installed (ddtrace's bridge, an operator's own
    ``opentelemetry-instrument``) is left alone. Never raises: tracing is not
    worth failing startup over.
    """
    global _installed_provider
    if not otlp_configured():
        return False
    if not isinstance(trace.get_tracer_provider(), trace.ProxyTracerProvider):
        logger.info("OTel tracer provider already installed; memory spans will use it")
        return False
    try:
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
        from opentelemetry.sdk.resources import SERVICE_NAME, Resource
        from opentelemetry.sdk.trace import SpanProcessor, TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor
    except ImportError:
        logger.warning(
            "OTEL_EXPORTER_OTLP_ENDPOINT is set but the 'otel' extra is not installed; "
            "memory spans are not exported"
        )
        return False
    try:
        # Resource.create reads OTEL_SERVICE_NAME and OTEL_RESOURCE_ATTRIBUTES,
        # but attributes passed to it override both, so the default goes in
        # only where they left the SDK's "unknown_service" placeholder. The
        # exporter reads the endpoint, headers and timeout from the standard
        # OTEL_EXPORTER_OTLP_* variables.
        resource = Resource.create()
        if str(resource.attributes.get(SERVICE_NAME, "")).startswith("unknown_service"):
            resource = resource.merge(Resource({SERVICE_NAME: service_name}))
        # shutdown_on_exit=False: shutdown_tracing() is the one, bounded
        # shutdown. The SDK's own atexit hook would join the export thread
        # again, unbounded, as the process exits.
        provider = TracerProvider(resource=resource, shutdown_on_exit=False)

        class QueryStringRedactor(SpanProcessor):
            def on_start(self, span: Any, parent_context: Any = None) -> None:
                _redact_query_string(span)

        provider.add_span_processor(QueryStringRedactor())
        provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))
        trace.set_tracer_provider(provider)
    except Exception:
        logger.warning("OTel tracing setup failed; memory spans are not exported", exc_info=True)
        return False
    _installed_provider = provider
    logger.info("OTel memory spans exported over OTLP/HTTP")
    return True


async def shutdown_tracing(timeout_seconds: float = 2.0) -> None:
    """Flush and stop the provider :func:`configure_tracing` installed, giving
    up after ``timeout_seconds`` in all.

    Cloud Run allows 10s between SIGTERM and SIGKILL, and the other shutdown
    steps need most of it. ``provider.shutdown()`` joins the export thread,
    which an unreachable endpoint can hold for the exporter's own timeout, so
    the work runs on a daemon thread that is simply abandoned at the deadline;
    being a daemon, it cannot hold up the process exit either.
    """
    provider = _installed_provider
    if provider is None:
        return
    finished = threading.Event()

    def flush_and_shut_down() -> None:
        try:
            provider.force_flush(int(timeout_seconds * 1000))
            provider.shutdown()
        except Exception:
            logger.warning("OTel tracer shutdown failed", exc_info=True)
        finally:
            finished.set()

    threading.Thread(target=flush_and_shut_down, name="otel-shutdown", daemon=True).start()
    if not await asyncio.to_thread(finished.wait, timeout_seconds):
        logger.warning("OTel tracer shutdown still running after %.1fs; not waiting for it", timeout_seconds)


def _redact_query_string(span: Any) -> None:
    """Replace the values in FastAPI's ``url.query`` with ``REDACTED``.

    A query string can carry what a person searched for (``GET /entities?search=``),
    which is content; the parameter names stay, for debugging. Under the
    content opt-in the values are kept, as the search query itself is. Run as
    the span opens, because that is when FastAPI sets the attribute.
    """
    query = (span.attributes or {}).get("url.query")
    if isinstance(query, str) and query and not capture_query_text():
        pairs = parse_qsl(query, keep_blank_values=True)
        span.set_attribute("url.query", urlencode([(key, "REDACTED") for key, _ in pairs]))


def _skip_fastapi_telemetry(scope: MutableMapping[str, Any]) -> bool:
    # FastAPI traces requests only into the provider configure_tracing()
    # installed, the one that redacts url.query. Under ddtrace's OTel bridge its
    # spans would duplicate ddtrace's own request span; under a provider an
    # operator installed they would carry the raw query string, and that
    # tracer's own HTTP instrumentation is the operator's to choose.
    return _installed_provider is None or trace.get_tracer_provider() is not _installed_provider


# FastAPI's own telemetry, for ``FastAPI(telemetry=...)``. Its OTel logs are off
# outright: they record validation failures with their input values, and the
# input of a memory write is memory content.
FASTAPI_TELEMETRY: TelemetryConfig = {"exclude": _skip_fastapi_telemetry, "logs": False}


def note_caller_trust_level(agent_id: object, trust_level: object) -> None:
    """Record the trust level of the agent this request acts as, for the
    memory spans that follow in the same request and act as that agent."""
    if isinstance(trust_level, int) and not isinstance(trust_level, bool) and agent_id:
        _caller_trust_level.set((str(agent_id), trust_level))


def _trust_level_for(agent_id: str | None) -> int | None:
    noted = _caller_trust_level.get()
    if noted is None or agent_id is None or noted[0] != str(agent_id):
        return None
    return noted[1]


def _error_type(exc: BaseException) -> str:
    if isinstance(exc, HTTPException):
        return str(exc.status_code)
    return type(exc).__qualname__


def _is_server_error(exc: BaseException) -> bool:
    # A 4xx (a duplicate, a refused agent, a missing row) is the operation
    # answering the caller, not the server failing: it gets error.type but
    # keeps an unset status, as HTTP server spans treat it. Otherwise every
    # dedup would count against the error rate.
    return not (isinstance(exc, HTTPException) and exc.status_code < 500)


def _set(span: Span, key: str, value: object) -> None:
    if value is not None:
        span.set_attribute(key, value)  # type: ignore[arg-type]


class MemorySpan:
    """What an operation can add to its span once the result is known."""

    def __init__(self, span: Span) -> None:
        self._span = span
        self._policy_decisions: tuple[str, ...] = ()

    def is_recording(self) -> bool:
        return self._span.is_recording()

    def add_policy_decision(self, action: str) -> None:
        self._policy_decisions = (*self._policy_decisions, action)
        self._span.set_attribute(ATTR_POLICY_DECISION, self._policy_decisions)
        self._span.add_event(POLICY_DECISION_EVENT, {ATTR_POLICY_DECISION: action})

    def set_record_id(self, record_id: object) -> None:
        if record_id is not None:
            self._span.set_attribute(ATTR_RECORD_ID, str(record_id))

    def set_record_count(self, count: int | None) -> None:
        _set(self._span, ATTR_RECORD_COUNT, count)

    def set_payload_bytes(self, size: int | None) -> None:
        _set(self._span, ATTR_PAYLOAD_BYTES, size)


@contextmanager
def memory_span(
    operation: str,
    *,
    store_id: str | None = None,
    agent_id: str | None = None,
    record_id: object = None,
    record_count: int | None = None,
    scope: str | None = None,
    payload_bytes: int | None = None,
    query: str | None = None,
) -> Iterator[MemorySpan]:
    """Run one memory operation inside its GenAI memory span.

    ``query`` is exported only under the content opt-in; every other argument
    is an identifier or a size, never content.
    """
    with _tracer.start_as_current_span(
        operation,
        kind=SpanKind.INTERNAL,
        record_exception=False,
        set_status_on_exception=False,
    ) as span:
        handle = MemorySpan(span)
        token = _active_span.set(handle)
        try:
            if span.is_recording():
                span.set_attribute(ATTR_OPERATION, operation)
                span.set_attribute(ATTR_PROVIDER, PROVIDER_NAME)
                _set(span, ATTR_STORE_ID, store_id)
                _set(span, ATTR_AGENT_ID, agent_id)
                _set(span, ATTR_SCOPE, scope)
                _set(span, ATTR_TRUST_LEVEL, _trust_level_for(agent_id))
                handle.set_record_id(record_id)
                handle.set_record_count(record_count)
                handle.set_payload_bytes(payload_bytes)
                if query is not None and capture_query_text():
                    span.set_attribute(ATTR_QUERY_TEXT, query)
            yield handle
        except BaseException as exc:
            span.set_attribute(ATTR_ERROR_TYPE, _error_type(exc))
            if _is_server_error(exc):
                span.set_status(Status(StatusCode.ERROR))
            raise
        finally:
            _active_span.reset(token)


def record_policy_decision(action: str) -> None:
    """Record a governance enforcement action on the memory operation it
    belongs to: an attribute (every action, in order) and an event per action.

    A decision made after its operation's span has ended (fast-mode
    remediation runs in the background) gets a span of its own instead, in the
    same trace.
    """
    active = _active_span.get()
    if active is not None and active.is_recording():
        active.add_policy_decision(action)
        return
    with _tracer.start_as_current_span(POLICY_DECISION_EVENT, kind=SpanKind.INTERNAL) as own:
        own.set_attribute(ATTR_PROVIDER, PROVIDER_NAME)
        own.set_attribute(ATTR_POLICY_DECISION, (action,))


P = ParamSpec("P")
R = TypeVar("R")


class TracedMemoryOperation:
    """Decorator: run an async memory operation inside its span.

    ``describe`` receives the call's arguments and returns
    :func:`memory_span` keyword arguments; ``on_result`` adds what only the
    result knows. A failure in either is logged and dropped: a bug in the
    telemetry never fails the operation. A class rather than a closure so each
    decorated function keeps its own signature for the type checker.
    """

    def __init__(
        self,
        operation: str,
        describe: Callable[..., dict[str, Any]],
        on_result: Callable[[MemorySpan, Any], None] | None = None,
    ) -> None:
        self.operation = operation
        self.describe = describe
        self.on_result = on_result

    def __call__(self, fn: Callable[P, Coroutine[Any, Any, R]]) -> Callable[P, Coroutine[Any, Any, R]]:
        operation, describe, on_result = self.operation, self.describe, self.on_result

        @functools.wraps(fn)
        async def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
            try:
                attrs = describe(*args, **kwargs)
            except Exception:
                logger.debug("memory span describe failed for %s", operation, exc_info=True)
                attrs = {}
            with memory_span(operation, **attrs) as span:
                result = await fn(*args, **kwargs)
                if on_result is not None:
                    try:
                        on_result(span, result)
                    except Exception:
                        logger.debug("memory span result failed for %s", operation, exc_info=True)
                return result

        return wrapper


def utf8_len(text: object) -> int | None:
    return len(text.encode("utf-8")) if isinstance(text, str) else None


# ── The memory service's operations ──
# Read by attribute so this module imports no schemas: each describer takes the
# operation's own arguments and returns identifiers and sizes only.


def _sum_utf8(texts: Any) -> int:
    return sum(utf8_len(text) or 0 for text in texts)


def _describe_create(data: Any, *args: Any, **kwargs: Any) -> dict[str, Any]:
    return {
        "store_id": data.tenant_id,
        "agent_id": data.agent_id,
        "scope": data.visibility,
        "record_count": 1,
        "payload_bytes": utf8_len(data.content),
    }


def _describe_create_bulk(data: Any, *args: Any, **kwargs: Any) -> dict[str, Any]:
    return {
        "store_id": data.tenant_id,
        "agent_id": data.agent_id,
        "scope": data.visibility,
        "record_count": len(data.items),
        "payload_bytes": _sum_utf8(item.content for item in data.items),
    }


def _describe_update(
    memory_id: Any, tenant_id: str, data: Any, agent_id: str | None = None, *args: Any, **kwargs: Any
) -> dict[str, Any]:
    return {
        "store_id": tenant_id,
        "agent_id": agent_id,
        "record_id": memory_id,
        "record_count": 1,
        "scope": data.visibility,
        "payload_bytes": utf8_len(data.content),
    }


def _describe_delete(memory_id: Any, tenant_id: str, *args: Any, **kwargs: Any) -> dict[str, Any]:
    return {"store_id": tenant_id, "record_id": memory_id, "record_count": 1}


def _describe_search(
    tenant_id: str, query: str, *args: Any, caller_agent_id: str | None = None, **kwargs: Any
) -> dict[str, Any]:
    return {"store_id": tenant_id, "agent_id": caller_agent_id, "query": query}


def _created(span: MemorySpan, result: Any) -> None:
    span.set_record_id(getattr(result, "id", None))


def _found(span: MemorySpan, result: Any) -> None:
    span.set_record_count(len(result))
    span.set_payload_bytes(_sum_utf8(getattr(hit, "content", None) for hit in result))


trace_create = TracedMemoryOperation(OP_CREATE, _describe_create, _created)
trace_create_bulk = TracedMemoryOperation(OP_CREATE, _describe_create_bulk)
trace_update = TracedMemoryOperation(OP_UPDATE, _describe_update)
trace_delete = TracedMemoryOperation(OP_DELETE, _describe_delete)
trace_search = TracedMemoryOperation(OP_SEARCH, _describe_search, _found)
