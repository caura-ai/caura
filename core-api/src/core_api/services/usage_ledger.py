"""core-api's usage metering wiring: where ``common.usage`` records go.

Registers a :class:`common.usage.UsageAggregator` as the process's sink, so
every model call core-api makes — and the savings measures — are added up per
minute and flushed through core-api's own storage client (idempotent, see
``storage_client.flush_usage``).

Also registers the scope fallback. Most request paths need no explicit
``usage_scope``: the REST auth already put the signed-in tenant in
``tenant_context``, and the MCP plane holds the tenant and the gateway-verified
agent in its own ContextVars. Background work spawned from a request inherits
both (``asyncio.create_task`` copies the context).

A workspace turns this off with ``observability.usage_metering_enabled``;
core-storage-api drops its rows at flush time, for core-api and core-worker
alike.
"""

from __future__ import annotations

import logging
import sys
from dataclasses import replace

from common.usage import UsageAggregator, UsageScope, set_scope_resolver, set_usage_sink
from core_api.clients.storage_client import get_storage_client
from core_api.tenant_context import get_current_tenant

logger = logging.getLogger(__name__)


def _ambient_scope(scope: UsageScope | None) -> UsageScope | None:
    """Fill what the scope leaves blank from the request's own identity."""
    base = scope or UsageScope()
    if base.tenant_id and base.agent_id:
        return base
    tenant, agent = base.tenant_id, base.agent_id
    mcp = sys.modules.get("core_api.mcp_server")
    if mcp is not None:
        if not tenant:
            mcp_tenant = mcp._tenant_id_var.get(None)
            # The MCP plane parks auth sentinels in this var; only a real tenant counts.
            if mcp_tenant and mcp_tenant not in getattr(mcp, "_SENTINEL_TENANTS", ()):
                tenant = mcp_tenant
        if not agent:
            agent = mcp._agent_id_var.get(None) or None
    if not tenant:
        tenant = get_current_tenant()
    if (tenant, agent) == (base.tenant_id, base.agent_id):
        return scope
    return replace(base, tenant_id=tenant, agent_id=agent)


def install() -> UsageAggregator:
    """Register the sink and the scope fallback, and start flushing."""
    aggregator = UsageAggregator(get_storage_client().flush_usage)
    set_usage_sink(aggregator)
    set_scope_resolver(_ambient_scope)
    aggregator.start()
    return aggregator


async def uninstall(aggregator: UsageAggregator) -> None:
    """Final flush, then stop recording."""
    await aggregator.stop()
    set_usage_sink(None)
    set_scope_resolver(None)
