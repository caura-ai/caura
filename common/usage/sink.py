"""Where usage records go, and the two calls that make them."""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol

from common.usage.scope import UsageScope, current_scope

logger = logging.getLogger(__name__)

#: The tenant a model call is recorded under when no scope names one, so a
#: call path that was missed shows up in the totals instead of vanishing.
UNATTRIBUTED = "_unattributed"


@dataclass(frozen=True, slots=True)
class ModelCall:
    scope: UsageScope
    operation: str
    #: ``"llm"`` or ``"embedding"``.
    kind: str
    provider: str
    model: str
    tokens_in: int
    tokens_out: int
    tokens_reasoning: int
    ok: bool


@dataclass(frozen=True, slots=True)
class ValueRecord:
    scope: UsageScope
    metric: str
    quantity: int


class UsageSink(Protocol):
    def add_model_call(self, call: ModelCall) -> None: ...

    def add_value(self, record: ValueRecord) -> None: ...


class _NullSink:
    def add_model_call(self, call: ModelCall) -> None:
        return None

    def add_value(self, record: ValueRecord) -> None:
        return None


_sink: UsageSink = _NullSink()

#: Fills in what the scope leaves blank from the service's own ambient
#: context (core-api: the signed-in tenant, the MCP session's agent), so a
#: request path needs no explicit scope to be attributed.
_resolver: Callable[[UsageScope | None], UsageScope | None] | None = None


def set_scope_resolver(resolver: Callable[[UsageScope | None], UsageScope | None] | None) -> None:
    global _resolver
    _resolver = resolver


def _resolved() -> UsageScope | None:
    scope = current_scope()
    if _resolver is not None:
        try:
            scope = _resolver(scope)
        except Exception:  # pragma: no cover - defensive
            logger.warning("usage: scope resolver failed", exc_info=True)
    return scope


def set_usage_sink(sink: UsageSink | None) -> None:
    """Register the process's sink; None restores the default that drops records."""
    global _sink
    _sink = sink if sink is not None else _NullSink()


def get_usage_sink() -> UsageSink:
    return _sink


def operation_from_label(label: str) -> str:
    """``"contradiction_batch-fallback-openai"`` → ``"contradiction_batch"``.

    The retry layer suffixes the service label with the tier; the tier is a
    reliability detail, not an operation, so totals group by the service.
    """
    for marker in ("-primary", "-fallback"):
        cut = label.find(marker)
        if cut > 0:
            return label[:cut]
    return label


def _attributed(scope: UsageScope | None) -> UsageScope:
    if scope is None or not scope.tenant_id:
        return UsageScope(
            tenant_id=UNATTRIBUTED,
            agent_id=scope.agent_id if scope else None,
            fleet_id=scope.fleet_id if scope else None,
            operation=scope.operation if scope else None,
        )
    return scope


def record_model_call(
    *,
    kind: str,
    provider: str,
    model: str,
    tokens_in: int = 0,
    tokens_out: int = 0,
    tokens_reasoning: int = 0,
    ok: bool = True,
    operation: str | None = None,
) -> None:
    """Record one real call to a model provider under the current scope.

    The operation is, in order: the one given, the retry layer's label for
    the LLM call in flight, the scope's, else ``"unlabelled"``.

    Never raises: metering must not fail the call it measures.
    """
    try:
        # Imported here, not at the top: the embedding providers import this
        # module while ``common.llm`` (which imports ``common.embedding``) may
        # still be initialising, and a top-level import would close that cycle.
        from common.llm.call_context import llm_call_label

        scope = _attributed(_resolved())
        label = operation_from_label(llm_call_label.get())
        operation = operation or label or scope.operation or "unlabelled"
        _sink.add_model_call(
            ModelCall(
                scope=scope,
                operation=operation,
                kind=kind,
                provider=provider or "unknown",
                model=model or "unknown",
                tokens_in=max(0, int(tokens_in)),
                tokens_out=max(0, int(tokens_out)),
                tokens_reasoning=max(0, int(tokens_reasoning)),
                ok=ok,
            )
        )
    except Exception:  # pragma: no cover - defensive
        logger.warning("usage: could not record a model call", exc_info=True)


def record_value(metric: str, quantity: int, *, scope: UsageScope | None = None) -> None:
    """Record a savings measure; skipped without a tenant, since it can't be shown to anyone."""
    try:
        s = scope or _resolved()
        if s is None or not s.tenant_id or quantity <= 0:
            return
        _sink.add_value(ValueRecord(scope=s, metric=metric, quantity=int(quantity)))
    except Exception:  # pragma: no cover - defensive
        logger.warning("usage: could not record a value", exc_info=True)
