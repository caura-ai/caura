"""Build the heartbeat payload (schema 1).

Mirrors ``docs/telemetry-schema-v1.json`` field for field. Everything here is
either a constant, a closed enum, a boolean or a bucket string, so the
payload cannot carry a hostname, a key, a model name, a DSN, a URL or an
exact count no matter what the configuration holds. Adding a field means
bumping the schema, a changelog entry and a minor release.
"""

from __future__ import annotations

import asyncio
import logging
import os
import platform
import re
import sys
import time
from collections.abc import Awaitable, Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from core_api.constants import PROBE_TIMEOUT_SECONDS, VERSION
from core_api.heartbeat import clients

if TYPE_CHECKING:
    from core_api.clients.storage_client import CoreStorageClient
    from core_api.config import Settings

logger = logging.getLogger(__name__)

SCHEMA_VERSION = 1
PRODUCT = "caura-server"

# One scale for every count, so small installs cannot be fingerprinted by
# exact numbers. Shared by counts.* and clients_24h.* and tested once.
BUCKETS: tuple[str, ...] = (
    "0",
    "1",
    "2-5",
    "6-20",
    "21-100",
    "101-1k",
    "1k-10k",
    "10k-100k",
    ">100k",
)
_BUCKET_UPPER_BOUNDS: tuple[tuple[int, str], ...] = (
    (0, "0"),
    (1, "1"),
    (5, "2-5"),
    (20, "6-20"),
    (100, "21-100"),
    (1_000, "101-1k"),
    (10_000, "1k-10k"),
    (100_000, "10k-100k"),
)

UPTIME_BUCKETS: tuple[str, ...] = ("<1h", "1h-1d", "1d-7d", "7d-30d", ">30d")

# Closed enums. Anything the configuration holds that is not listed folds
# into ``other`` (or ``none``), so a provider value can never carry a free
# string into the payload.
EMBEDDING_PROVIDERS = frozenset({"none", "openai", "local", "other"})
ENTITY_EXTRACTION_PROVIDERS = frozenset({"none", "openai", "anthropic", "openrouter", "gemini", "other"})
RANK_PROVIDERS = frozenset({"noop", "local", "other"})
EVENT_BUS_BACKENDS = frozenset({"inprocess", "pubsub"})
DEPLOY_KINDS = frozenset({"docker", "source"})

# The Dockerfile stamps this file into the image; nothing else does, so its
# presence is the deploy kind.
DOCKER_VERSION_FILE = Path("/app/VERSION")

# Cap on ``counts.plugin_versions`` entries and on the tenant fan-out behind
# ``counts.plugin_nodes_7d``. Both are bounds on work, not on accuracy claims:
# the doc labels the node count a lower bound.
PLUGIN_VERSIONS_CAP = 10
TENANT_FANOUT_CAP = 500
PLUGIN_NODES_WINDOW_DAYS = 7

_MAJOR_MINOR = re.compile(r"^v?(\d+)\.(\d+)")

# Process start, for ``uptime_bucket``. Import time is close enough to boot:
# this module is imported from the lifespan, not lazily at first send.
_PROCESS_STARTED_MONOTONIC = time.monotonic()


def bucket(n: int) -> str:
    """Map a non-negative count to its bucket string."""
    if n < 0:
        n = 0
    for upper, label in _BUCKET_UPPER_BOUNDS:
        if n <= upper:
            return label
    return ">100k"


def uptime_bucket(seconds: float) -> str:
    """Map process uptime in seconds to its bucket string."""
    if seconds < 3600:
        return "<1h"
    if seconds < 86400:
        return "1h-1d"
    if seconds < 7 * 86400:
        return "1d-7d"
    if seconds < 30 * 86400:
        return "7d-30d"
    return ">30d"


def process_uptime_seconds() -> float:
    return time.monotonic() - _PROCESS_STARTED_MONOTONIC


def normalise_version(version: str | None) -> str:
    """``"v3.17.0"`` -> ``"3.17.0"``; empty -> ``"dev"``.

    ``docs/self-hosting.md`` tells operators to pin with ``CAURA_VERSION=v3.17.0``
    in ``.env``, which core-api also reads, so the tag form must not reach the
    payload (or the ``User-Agent``) as the version string.
    """
    v = (version or "").strip()
    if len(v) > 1 and v[0] in "vV" and v[1].isdigit():
        v = v[1:]
    return v or "dev"


def major_minor(version: str | None) -> str | None:
    """``"2.21.3"`` → ``"2.21"``; anything unparseable → ``None``."""
    if not version:
        return None
    m = _MAJOR_MINOR.match(version.strip())
    if not m:
        return None
    return f"{m.group(1)}.{m.group(2)}"


def _enum(value: str | None, allowed: frozenset[str], *, fallback: str) -> str:
    v = (value or "").strip().lower()
    return v if v in allowed else fallback


def _embedding_provider(value: str | None) -> str:
    v = (value or "").strip().lower()
    if v in ("", "fake", "none"):
        return "none"
    return _enum(v, EMBEDDING_PROVIDERS, fallback="other")


def _entity_extraction_provider(value: str | None) -> str:
    v = (value or "").strip().lower()
    if v in ("", "fake", "none"):
        return "none"
    return _enum(v, ENTITY_EXTRACTION_PROVIDERS, fallback="other")


def _rank_provider(env: os._Environ[str] | dict[str, str]) -> str:
    # Read the same env the ranking constants read, at build time rather than
    # via the import-time constants so a test can drive it without reloading.
    enabled = env.get("RANK_ENABLED", "").strip().lower() in ("true", "1", "yes", "on")
    if not enabled:
        return "noop"
    provider = (env.get("RANK_PROVIDER") or "noop").strip().lower()
    if provider == "fake":
        return "noop"
    return _enum(provider, RANK_PROVIDERS, fallback="other")


def _event_bus(env: os._Environ[str] | dict[str, str]) -> str:
    return _enum(env.get("EVENT_BUS_BACKEND", "inprocess"), EVENT_BUS_BACKENDS, fallback="inprocess")


def deploy_kind(version_file: Path = DOCKER_VERSION_FILE) -> str:
    return "docker" if version_file.exists() else "source"


def runtime() -> dict[str, str]:
    return {
        "python": f"{sys.version_info.major}.{sys.version_info.minor}",
        "os": platform.system().lower() or "unknown",
        "arch": platform.machine().lower() or "unknown",
        "deploy": deploy_kind(),
    }


@dataclass
class Counts:
    """Exact counts as read from storage; bucketed by :func:`build_payload`."""

    memories: int = 0
    agents: int = 0
    tenants: int = 0
    plugin_nodes_7d: int = 0
    plugin_versions: list[str] = field(default_factory=list)


async def _bounded(label: str, coro: Awaitable[Any], default: Any) -> Any:
    """Run one storage call under ``PROBE_TIMEOUT_SECONDS``; ``default`` on failure.

    Same posture as ``GET /stats``: a stalled storage backend once a day must
    not wedge the loop, and the bucket ``0`` is an honest "could not read".
    """
    try:
        return await asyncio.wait_for(coro, timeout=PROBE_TIMEOUT_SECONDS)
    except Exception:
        logger.debug("[telemetry] %s read failed; reporting 0", label, exc_info=True)
        return default


async def _plugin_summary(sc: CoreStorageClient, tenant_ids: Iterable[str]) -> tuple[int, list[str]]:
    """Fan the tenant-scoped ``/fleet/nodes/summary`` out over ``tenant_ids``.

    Every storage read is bound to a tenant (the tenant-scope gate holds that
    invariant across the storage surface), so the cross-tenant total is
    assembled here, once a day, rather than by an unscoped SQL statement.
    """
    nodes = 0
    versions: set[str] = set()
    for i, tenant_id in enumerate(tenant_ids):
        if i >= TENANT_FANOUT_CAP:
            break
        summary = await _bounded(
            "plugin_nodes",
            sc.fleet_nodes_summary(tenant_id, days=PLUGIN_NODES_WINDOW_DAYS),
            {},
        )
        nodes += int(summary.get("nodes_7d") or 0)
        for raw in summary.get("plugin_versions") or ():
            mm = major_minor(raw if isinstance(raw, str) else None)
            if mm:
                versions.add(mm)
    return nodes, sorted(versions)[:PLUGIN_VERSIONS_CAP]


async def collect_counts(sc: CoreStorageClient, *, standalone_tenant_id: str | None = None) -> Counts:
    """Read the counts behind ``counts.*`` — the same calls ``GET /stats`` makes."""
    tenants, memories, agents, active = await asyncio.gather(
        _bounded("tenants", sc.count_distinct_tenants(), 0),
        _bounded("memories", sc.count_all(tenant_id=""), 0),
        _bounded("agents", sc.count_distinct_agents(), 0),
        _bounded("active_tenants", sc.list_active_tenants(), []),
    )
    tenant_ids: list[str] = [t for t in active if isinstance(t, str)]
    if standalone_tenant_id and standalone_tenant_id not in tenant_ids:
        tenant_ids.insert(0, standalone_tenant_id)
    nodes, versions = await _plugin_summary(sc, tenant_ids)
    return Counts(
        memories=int(memories or 0),
        agents=int(agents or 0),
        tenants=int(tenants or 0),
        plugin_nodes_7d=nodes,
        plugin_versions=versions,
    )


def build_payload(
    *,
    settings: Settings,
    deployment_id: str,
    counts: Counts,
    client_counts: dict[str, int] | None = None,
    sent_at: datetime | None = None,
    version: str = VERSION,
    uptime_seconds: float | None = None,
    env: os._Environ[str] | dict[str, str] | None = None,
) -> dict[str, Any]:
    """Assemble the schema-1 payload. Pure: no I/O, no clock beyond ``sent_at``."""
    environ = os.environ if env is None else env
    now = sent_at or datetime.now(UTC)
    up = process_uptime_seconds() if uptime_seconds is None else uptime_seconds
    per_family = client_counts if client_counts is not None else clients.snapshot()
    return {
        "schema": SCHEMA_VERSION,
        "product": PRODUCT,
        "deployment_id": deployment_id,
        "sent_at": now.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "version": normalise_version(version),
        "runtime": runtime(),
        "mode": {"standalone": bool(settings.is_standalone)},
        "uptime_bucket": uptime_bucket(up),
        "providers": {
            "embedding": _embedding_provider(settings.embedding_provider),
            "entity_extraction": _entity_extraction_provider(settings.entity_extraction_provider),
            "rank": _rank_provider(environ),
            "event_bus": _event_bus(environ),
            "redis": bool(settings.redis_url),
            "sentry": bool(settings.sentry_dsn),
        },
        "counts": {
            "memories": bucket(counts.memories),
            "agents": bucket(counts.agents),
            "tenants": bucket(counts.tenants),
            "plugin_nodes_7d": bucket(counts.plugin_nodes_7d),
            "plugin_versions": list(counts.plugin_versions[:PLUGIN_VERSIONS_CAP]),
        },
        "clients_24h": {family: bucket(int(per_family.get(family, 0))) for family in clients.FAMILIES},
    }
