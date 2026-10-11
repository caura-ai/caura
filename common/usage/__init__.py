"""Per-call usage metering: who made each model call, for what, and its tokens.

Public surface:

* :func:`scoped` — the same as a decorator on an async entry point, reading
  the names from its arguments.
* :func:`usage_scope` — context manager naming the tenant, agent, fleet and
  operation the enclosed work is for. Nested scopes fill in what the outer
  one left blank, so a background task can name its memory while inheriting
  the request's agent.
* :func:`record_model_call` — called by the LLM and embedding providers after
  each real (billed) call, with the provider's own token counts.
* :func:`record_value` — a savings measure (recall source size, tokens a merge
  retired, cross-agent reuse) for the current scope.
* :class:`UsageAggregator` — the sink a service registers: per-minute totals
  in memory, flushed to storage with an idempotency key.
* :func:`set_usage_sink` — a service registers where records go; the default
  drops them, so a script or test that never registers one pays nothing.

The records carry counts only, never content.
"""

from common.usage.aggregator import UsageAggregator, per_tenant
from common.usage.scope import UsageScope, current_scope, scoped, usage_scope
from common.usage.sink import (
    UNATTRIBUTED,
    ModelCall,
    UsageSink,
    ValueRecord,
    get_usage_sink,
    operation_from_label,
    record_model_call,
    record_value,
    set_scope_resolver,
    set_usage_sink,
)

__all__ = [
    "UNATTRIBUTED",
    "UsageAggregator",
    "ModelCall",
    "UsageScope",
    "UsageSink",
    "ValueRecord",
    "current_scope",
    "get_usage_sink",
    "operation_from_label",
    "record_model_call",
    "record_value",
    "scoped",
    "set_scope_resolver",
    "set_usage_sink",
    "usage_scope",
    "per_tenant",
]
