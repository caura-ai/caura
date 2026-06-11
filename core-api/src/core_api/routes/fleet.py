"""Fleet heartbeat and command channel — replaces WebSocket/SSH gateway model."""

import json
import logging
from datetime import UTC, datetime, timedelta
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field, field_validator
from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)

from core_api.auth import AuthContext, get_auth_context
from core_api.clients.storage_client import get_storage_client
from core_api.constants import NODE_OFFLINE_SECONDS, NODE_STALE_SECONDS
from core_api.db.session import get_db
from core_api.repositories import fleet_repo
from core_api.services.audit_service import log_action
from core_api.services.organization_settings import get_raw_settings
from core_api.version_compat import (
    MIN_AUTO_DEPLOY_PLUGIN_VERSION,
    MIN_RECOMMENDED_PLUGIN_VERSION,
    is_plugin_outdated,
)

# How long an ``acked`` deploy command counts as "in flight" before the
# auto-upgrade gate allows queueing another. CAURA-000: customer prod
# accumulated 1,381 stuck-acked deploys at a 60s/queue cadence because
# the pending-only gate was blind to in-flight commands. A typical deploy
# completes in <2 min (build ~30s + restart ~10s + plugin re-init ~5s);
# 10 min is generous slack that still recovers genuinely-abandoned
# commands without needing operator intervention.
DEPLOY_IN_FLIGHT_WINDOW = timedelta(minutes=10)

router = APIRouter(tags=["Fleet"])


# ── Schemas ──


class FleetCreateIn(BaseModel):
    tenant_id: str
    fleet_id: str  # alphanumeric + hyphens, 3-50 chars
    display_name: str | None = None
    description: str | None = None

    @classmethod
    def validate_fleet_id(cls, v: str) -> str:
        import re

        if not re.match(r"^[a-zA-Z0-9][a-zA-Z0-9\-]{1,48}[a-zA-Z0-9]$", v):
            raise ValueError(
                "fleet_id must be 3-50 chars, alphanumeric + hyphens, no leading/trailing hyphens"
            )
        return v


class HeartbeatIn(BaseModel):
    tenant_id: str
    node_name: str
    fleet_id: str | None = None
    hostname: str | None = None
    ip: str | None = None
    openclaw_version: str | None = None
    plugin_version: str | None = None
    plugin_hash: str | None = None
    os_info: str | None = None
    agents: list | None = None
    tools: list | None = None
    channels: list | None = None
    metadata: dict | None = None
    # ``install_id`` is the per-OpenClaw-install opaque suffix the plugin
    # generates once at first heartbeat and persists locally. Used to
    # disambiguate the default ``"main"`` agent across fleet installs
    # so memories from different machines stop colliding on a single
    # ``(tenant_id, agent_id="main")`` row. Optional — older plugin
    # versions don't send it.
    #
    # ``max_length=32`` matches the ``agents.install_id`` column
    # (``String(32)``); without this Pydantic constraint, an oversized
    # value silently 422s at the storage layer in a per-agent
    # exception handler that swallows the failure, leaving the row
    # without an ``install_id`` and recreating the very collision
    # this feature exists to fix. Reject at the API boundary instead.
    install_id: str | None = Field(None, max_length=32)
    # CAURA-444: rolling counters from the plugin's recall-policy gate
    # (context-engine.ts:getRecallMetrics). Reset on plugin restart;
    # latest snapshot is stored on the node row for SQL aggregation.
    recall_metrics: dict | None = None
    # CAURA-444: epoch-ms cooldown signal. When set and in the future,
    # the auto-upgrade trigger SKIPS queueing further deploy commands
    # for this node — the plugin is signalling it knows it's in a
    # broken-deploy state and another deploy would just loop.
    #
    # ``ge=1`` rejects 0 and negative values at the API boundary. The
    # auto-upgrade gate already filters them out via the
    # ``> now_ms`` check, but they'd still land in
    # ``nodes.metadata.deploy_blocked_until`` and mislead operators
    # reading the column (e.g. "blocked since 1970?"). Fail-loud here
    # instead.
    deploy_blocked_until: int | None = Field(None, ge=1)

    @field_validator("recall_metrics")
    @classmethod
    def _cap_recall_metrics(cls, v: dict | None) -> dict | None:
        # Cap incoming counter blob to keep a misbehaving / malicious
        # plugin from ballooning `nodes.metadata` rows. The plugin's
        # actual `getRecallMetrics()` payload is well under 1 KB even
        # with every reason populated; 4 KB is a comfortable headroom.
        if v is not None and len(json.dumps(v)) > 4096:
            raise ValueError("recall_metrics exceeds 4 KB limit")
        return v


class CommandIn(BaseModel):
    tenant_id: str | None = None
    node_id: UUID
    command: str
    payload: dict | None = None


class CommandResultIn(BaseModel):
    status: str  # done | failed
    result: dict | None = None


# ── Fleet CRUD ──


@router.post("/fleet", status_code=201)
async def create_fleet(
    body: FleetCreateIn,
    auth: AuthContext = Depends(get_auth_context),
    db: AsyncSession = Depends(get_db),
):
    """Explicitly create a fleet (team) within a tenant."""
    auth.enforce_read_only()
    auth.enforce_usage_limits()
    auth.enforce_tenant(body.tenant_id)

    # Validate fleet_id format
    FleetCreateIn.validate_fleet_id(body.fleet_id)

    sc = get_storage_client()

    # Check if fleet already exists
    if await sc.fleet_exists(body.tenant_id, body.fleet_id):
        raise HTTPException(status_code=409, detail=f"Fleet '{body.fleet_id}' already exists")

    # Create a sentinel node to register the fleet
    await sc.upsert_node(
        {
            "tenant_id": body.tenant_id,
            "node_name": f"_fleet_{body.fleet_id}",
            "fleet_id": body.fleet_id,
            "metadata": {
                "display_name": body.display_name,
                "description": body.description,
                "sentinel": True,
            },
            "last_heartbeat": datetime.now(UTC).isoformat(),
        }
    )
    await log_action(
        db,
        tenant_id=body.tenant_id,
        action="create",
        resource_type="fleet",
        detail={"fleet_id": body.fleet_id, "display_name": body.display_name},
    )
    await db.commit()
    return {"ok": True, "fleet_id": body.fleet_id, "tenant_id": body.tenant_id}


@router.get("/fleet")
async def list_fleets(
    tenant_id: str = Query(...),
    auth: AuthContext = Depends(get_auth_context),
):
    """List distinct fleets for a tenant with node counts."""
    auth.enforce_tenant(tenant_id)

    sc = get_storage_client()
    rows = await sc.list_fleets(tenant_id)
    now = datetime.now(UTC)
    return [
        {
            "fleet_id": r.get("fleet_id"),
            "node_count": int(r.get("node_count", 0)),
            "last_heartbeat": r.get("last_heartbeat"),
            "status": "online"
            if r.get("last_heartbeat") and _age_seconds(r.get("last_heartbeat"), now) < NODE_OFFLINE_SECONDS
            else "offline",
        }
        for r in rows
    ]


@router.delete("/fleet/{fleet_id}", status_code=204)
async def delete_fleet(
    fleet_id: str,
    tenant_id: str = Query(...),
    auth: AuthContext = Depends(get_auth_context),
    db: AsyncSession = Depends(get_db),
):
    """Delete a fleet and all its nodes. Memories are NOT deleted (they retain fleet_id for history)."""
    auth.enforce_read_only()
    auth.enforce_tenant(tenant_id)

    sc = get_storage_client()

    # Count nodes to delete
    node_count = await sc.count_nodes(tenant_id=tenant_id, fleet_id=fleet_id)
    if node_count == 0:
        raise HTTPException(status_code=404, detail=f"Fleet '{fleet_id}' not found")

    # Delete all commands for fleet nodes, then delete nodes
    await sc.delete_fleet(tenant_id=tenant_id, fleet_id=fleet_id)
    await log_action(
        db,
        tenant_id=tenant_id,
        action="delete",
        resource_type="fleet",
        detail={"fleet_id": fleet_id, "nodes_deleted": node_count},
    )
    await db.commit()


@router.post("/fleet/{fleet_id}/purge")
async def purge_fleet(
    fleet_id: str,
    tenant_id: str = Query(...),
    auth: AuthContext = Depends(get_auth_context),
    db: AsyncSession = Depends(get_db),
):
    """Permanently purge a fleet's entire footprint within a tenant.

    Unlike ``DELETE /fleet/{fleet_id}`` (which removes only the fleet's nodes +
    commands and intentionally keeps memories for history), this HARD-deletes
    everything scoped to ``(tenant_id, fleet_id)``: memories, entities,
    relations, agents, documents, analysis/dedup rows, nodes, and commands.
    Irreversible. Returns the per-table deleted counts.

    Intended for test-tenant hygiene: the OpenClaw fleet-tester purges its
    run-scoped ``nightly-<run_id>-fleet-NN`` fleets at teardown so the shared
    dev tenant doesn't accumulate run data that confounds isolation/trust tests.

    Auth: a write-capable tenant-owner key. Agent-scoped credentials are
    blocked (BFLA) — a hard fleet purge is an admin-plane operation, on par
    with bulk memory delete and agent deletion. Idempotent.
    """
    auth.enforce_read_only()
    auth.enforce_tenant(tenant_id)
    auth.enforce_not_agent_credential("purge fleet data")

    sc = get_storage_client()
    counts = await sc.purge_fleet_data(tenant_id, fleet_id)
    await log_action(
        db,
        tenant_id=tenant_id,
        action="purge",
        resource_type="fleet",
        detail={"fleet_id": fleet_id, "deleted": counts},
    )
    await db.commit()
    return {"ok": True, "tenant_id": tenant_id, "fleet_id": fleet_id, "deleted": counts}


# ── Heartbeat ──


# CAURA-444 — plugin auto-upgrade. Versions whose deploy machinery is
# itself broken; we never queue auto-deploy for these because the plugin
# would loop or land in a partially-deployed state. Each entry comes off
# the list once every node in the wild has been manually upgraded past it.
#
# 2.3.0: hardcoded srcFiles list drifted from backend (15 vs 22 files);
#        prebuild references a monorepo path missing on flat installs,
#        so version.ts is never refreshed and PLUGIN_VERSION reports
#        stale.
KNOWN_BROKEN_DEPLOY_VERSIONS: frozenset[str] = frozenset({"2.3.0"})

# Cap how far into the future a node can defer its own auto-upgrade.
# Pre-cap a misbehaving / malicious plugin could send
# ``deploy_blocked_until = Number.MAX_SAFE_INTEGER`` and DoS its own
# upgrade path indefinitely. 7 days is comfortably above the longest
# ``MEMCLAW_DEPLOY_FAILURE_COOLDOWN_HOURS`` an operator would set.
MAX_BLOCK_MS: int = 7 * 24 * 3600 * 1000


def _semver_lt(a: str | None, b: str | None) -> bool:
    """``a < b`` for plain dotted-int versions. Returns False if either
    side is unparseable so we never queue a deploy on a version we
    don't understand.
    """
    if not a or not b:
        return False
    try:
        a_parts = [int(x) for x in a.split(".") if x]
        b_parts = [int(x) for x in b.split(".") if x]
        # Pure-separator strings like "..." filter to an empty list. After
        # zero-padding they would look like [0, 0, 0] and falsely test
        # "older than" any real version — triggering a spurious auto-upgrade.
        # Treat any side with no numeric components as "unknown", not "0".
        if not a_parts or not b_parts:
            return False
        # Pad to the same length with zeros so "2.4" < "2.4.1".
        n = max(len(a_parts), len(b_parts))
        a_parts += [0] * (n - len(a_parts))
        b_parts += [0] * (n - len(b_parts))
        return a_parts < b_parts
    except ValueError:
        return False


async def _auto_upgrade_enabled_for_tenant(db: AsyncSession, tenant_id: str) -> bool:
    """Default true; per-tenant flip via
    ``organization_settings.memclaw.auto_upgrade_enabled = false``.
    """
    try:
        raw = await get_raw_settings(db, tenant_id)
        flag = raw.get("memclaw", {}).get("auto_upgrade_enabled")
        # None (no override) → use the global default (true).
        return flag is not False
    except Exception:
        # Fail-open on settings-resolve errors so a misconfigured tenant
        # doesn't permanently lose auto-upgrade. The cooldown machinery
        # on the plugin side prevents loops in the worst case.
        # Log the failure so chronic settings-resolve breakage is
        # observable rather than silently masked.
        logger.warning(
            "fleet.heartbeat: failed to resolve auto_upgrade_enabled for tenant=%s",
            tenant_id,
            exc_info=True,
        )
        return True


def _has_recent_deploy_command_from_list(pending: list) -> bool:
    """True if the pending-commands list already contains a ``deploy``.
    Sync helper — operates on an already-fetched list rather than
    issuing its own storage call. The heartbeat handler fetches
    ``pending`` exactly once and threads it through to
    ``_maybe_queue_auto_upgrade``; without this split, the same
    ``get_pending_commands`` round-trip fired twice per heartbeat
    (once for the auto-upgrade gate, once for the response payload).

    The list is already node-scoped at the call site (the storage
    query takes the node name), so we don't re-filter by node here.

    ``isinstance(c, dict)`` (not ``(c or {})``) guards against truthy
    non-dict list elements — strings, numbers, etc. would have made
    the pre-fix ``(c or {}).get(...)`` raise ``AttributeError`` because
    ``c or {}`` returns ``c`` when ``c`` is truthy, and only dicts have
    ``.get``. A storage backend bug returning unexpected types should
    fail-closed (skip the gate) rather than crash the heartbeat.
    """
    return any(isinstance(c, dict) and c.get("command") == "deploy" for c in pending or [])


async def _maybe_queue_auto_upgrade(
    *,
    db: AsyncSession,
    sc,
    body: "HeartbeatIn",
    pending_commands: list,
    node_id: str,
) -> bool:
    """If the node is on an older plugin version and the tenant has
    auto-upgrade enabled, queue a ``deploy`` command. Multiple skip
    conditions for safety:

    - missing or unparseable plugin_version
    - plugin_version >= MIN_RECOMMENDED_PLUGIN_VERSION (no upgrade needed)
    - plugin_version < MIN_AUTO_DEPLOY_PLUGIN_VERSION (pre-manifest-aware;
      old client can't fetch new files, so auto-deploy would leave it
      partially upgraded and unable to load)
    - plugin_version is in KNOWN_BROKEN_DEPLOY_VERSIONS
    - node has signalled cooldown via ``deploy_blocked_until``
    - tenant has explicitly disabled auto-upgrade
    - a deploy command is already pending for this node

    ``pending_commands`` is the heartbeat handler's already-fetched
    list of unacked commands for this node — passed in so we avoid a
    redundant ``get_pending_commands`` round-trip.

    Returns True iff a new ``deploy`` command was successfully created
    (so the caller can re-fetch + return it in the same heartbeat).
    """
    # Plugin release cadence is independent of the backend's ``VERSION``
    # (CAURA-000 / PR #131). Auto-upgrade target is ``MIN_RECOMMENDED_PLUGIN_VERSION``
    # — the operator-curated floor in ``core_api.version_compat`` which is
    # bumped on each plugin release. Comparing against backend ``VERSION``
    # (pre-merge behaviour) would queue spurious deploys whenever the backend
    # released ahead of the plugin (or block real upgrades when the plugin
    # released ahead of the backend).
    target_version = MIN_RECOMMENDED_PLUGIN_VERSION
    if not body.plugin_version:
        return False
    if not _semver_lt(body.plugin_version, target_version):
        return False
    # Pre-manifest-aware floor — old clients fetch source from their own
    # hardcoded list and silently miss files added in later releases,
    # which leaves dist/ importing modules that aren't on disk. Same
    # recovery as KNOWN_BROKEN_DEPLOY_VERSIONS: manual re-install via
    # ``/api/v1/install-plugin``.
    if _semver_lt(body.plugin_version, MIN_AUTO_DEPLOY_PLUGIN_VERSION):
        logger.info(
            "fleet.heartbeat: skipping auto-upgrade for node=%s on "
            "pre-manifest-aware version %s (manual re-install required; "
            "floor=%s)",
            body.node_name,
            body.plugin_version,
            MIN_AUTO_DEPLOY_PLUGIN_VERSION,
        )
        return False
    if body.plugin_version in KNOWN_BROKEN_DEPLOY_VERSIONS:
        logger.info(
            "fleet.heartbeat: skipping auto-upgrade for node=%s on "
            "broken-deploy version %s (manual re-install required)",
            body.node_name,
            body.plugin_version,
        )
        return False
    now_ms = int(datetime.now(UTC).timestamp() * 1000)
    if (
        body.deploy_blocked_until
        and body.deploy_blocked_until > now_ms
        and body.deploy_blocked_until < now_ms + MAX_BLOCK_MS
    ):
        return False
    if not await _auto_upgrade_enabled_for_tenant(db, body.tenant_id):
        return False
    if _has_recent_deploy_command_from_list(pending_commands):
        return False
    # CAURA-000: defence against ``acked``-then-stuck queue runaway.
    # The pending-only list above is blind to commands the heartbeat
    # handler has already shipped to the plugin (status: ``acked``).
    # If a previous deploy is still in flight — OR was killed mid-
    # result-POST by its own systemctl restart — queueing another
    # one creates the 60s SIGTERM cycle observed on customer prod.
    # The repo lookup is gated behind the cheaper checks above so we
    # only pay for the DB roundtrip when an auto-upgrade is actually
    # eligible. Falls back to ALLOW on error so a transient DB hiccup
    # doesn't break the upgrade path entirely (the pending check above
    # is already a safety net).
    try:
        in_flight = await fleet_repo.has_recent_in_flight_deploy(
            db,
            node_id=UUID(node_id),
            since=datetime.now(UTC) - DEPLOY_IN_FLIGHT_WINDOW,
        )
    except Exception:
        logger.warning(
            "fleet.heartbeat: has_recent_in_flight_deploy lookup failed "
            "node=%s tenant=%s — falling back to allow (pending check "
            "above still gates)",
            body.node_name,
            body.tenant_id,
            exc_info=True,
        )
        in_flight = False
    if in_flight:
        logger.info(
            "fleet.heartbeat: skipping auto-upgrade for node=%s — a "
            "deploy is already in flight within the last %s",
            body.node_name,
            DEPLOY_IN_FLIGHT_WINDOW,
        )
        return False

    try:
        await sc.create_command(
            {
                "tenant_id": body.tenant_id,
                # Storage expects ``node_id`` (UUID, FK to fleet_nodes.id),
                # not ``node_name``. Pre-fix this call passed ``node_name``,
                # which ``_filter_fields`` silently dropped (not a FleetCommand
                # column), leaving ``node_id`` NULL and tripping the NOT NULL
                # constraint at INSERT time — every auto-upgrade attempt 500'd
                # silently on the storage round-trip.
                "node_id": node_id,
                "command": "deploy",
                # Plugin re-fetches the canonical file list via
                # /plugin-manifest; payload only carries the target
                # version for logging / cooldown bookkeeping.
                "payload": {"target_version": target_version},
            }
        )
        logger.info(
            "fleet.heartbeat: auto-upgrade queued for node=%s tenant=%s (%s -> %s)",
            body.node_name,
            body.tenant_id,
            body.plugin_version,
            target_version,
        )
        return True
    except Exception as e:
        logger.warning(
            "fleet.heartbeat: failed to queue auto-upgrade for node=%s: %s",
            body.node_name,
            e,
        )
        return False


@router.post("/fleet/heartbeat")
async def heartbeat(
    body: HeartbeatIn,
    auth: AuthContext = Depends(get_auth_context),
    db: AsyncSession = Depends(get_db),
):
    """Plugin pushes status; receives pending commands in response."""
    auth.enforce_tenant(body.tenant_id)

    if is_plugin_outdated(body.plugin_version):
        logger.warning(
            "outdated plugin heartbeat",
            extra={
                "node": body.node_name,
                "plugin_version": body.plugin_version,
                "min_recommended": MIN_RECOMMENDED_PLUGIN_VERSION,
            },
        )

    now = datetime.now(UTC)
    sc = get_storage_client()

    # Merge CAURA-444 metrics (recall counters + cooldown signal) into
    # the existing metadata JSONB blob rather than introducing new
    # columns. This keeps the storage schema unchanged while still
    # exposing the data via the existing /fleet/nodes endpoint and
    # ad-hoc SQL on `nodes.metadata`.
    merged_metadata: dict | None = body.metadata
    if body.recall_metrics is not None or body.deploy_blocked_until is not None:
        merged_metadata = dict(merged_metadata or {})
        if body.recall_metrics is not None:
            merged_metadata["recall_metrics"] = body.recall_metrics
        if body.deploy_blocked_until is not None:
            # Mirror the gate's MAX_BLOCK_MS cap at write time. The gate
            # already ignores beyond-cap values (treats them as if the
            # cooldown weren't set), so persisting them would only
            # mislead operators reading nodes.metadata ("blocked until
            # year 5138?"). Storing only what the gate honors keeps the
            # column truthful.
            _now_ms = int(datetime.now(UTC).timestamp() * 1000)
            # Also reject already-expired timestamps. The gate above only
            # honours ``deploy_blocked_until > now_ms``, so persisting a
            # past value would leak into ``nodes.metadata`` and mislead
            # operators inspecting the column (the "blocked until last
            # week?" trap). Symmetric with the gate's lower bound.
            if _now_ms < body.deploy_blocked_until <= _now_ms + MAX_BLOCK_MS:
                merged_metadata["deploy_blocked_until"] = body.deploy_blocked_until

    node = await sc.upsert_node(
        {
            "tenant_id": body.tenant_id,
            "node_name": body.node_name,
            "fleet_id": body.fleet_id,
            "hostname": body.hostname,
            "ip": body.ip,
            "openclaw_version": body.openclaw_version,
            "plugin_version": body.plugin_version,
            "plugin_hash": body.plugin_hash,
            "os_info": body.os_info,
            "agents_json": body.agents,
            "tools_json": body.tools,
            "channels_json": body.channels,
            "metadata": merged_metadata,
            "last_heartbeat": now.isoformat(),
        }
    )

    # Materialise / refresh per-agent rows on every heartbeat so the
    # admin UI sees agents the moment they appear (not only after their
    # first write) and ``display_name`` tracks the current hostname when
    # operators rename their machines. Old plugin versions that don't
    # send ``display_name`` / ``install_id`` simply pass NULL — the
    # diff-merge in ``get_or_create_agent`` only overwrites when the
    # value is not None, so prior data is preserved.
    #
    # ``get_or_create_agent`` (rather than a direct
    # ``sc.create_or_update_agent``) is load-bearing here: it does
    # ``GET /agents/{id}`` first and only POSTs an update when the
    # diff is non-empty. The bare POST hits storage's ``agent_add``
    # which catches ``IntegrityError`` from the unique-key conflict,
    # rolls back, and re-selects — but the rollback closes the
    # outer ``session.begin()`` transaction, so the re-select 500s.
    # The pre-Task6 only-on-write callers always pre-checked, so the
    # path was never live; the heartbeat upsert exercises it on the
    # second tick.
    if body.agents:
        from core_api.services.agent_service import get_or_create_agent

        failed_agents: list[str] = []
        for a in body.agents:
            if not isinstance(a, dict):
                continue
            agent_key = a.get("agentId") or a.get("agent_id")
            if not agent_key:
                continue
            # Bound ``display_name`` at the API boundary. Storage column
            # is ``Text`` (unlimited) and ``MEMCLAW_DISPLAY_NAME_OVERRIDE``
            # passes verbatim from the plugin, so a hostile or buggy
            # client could push an oversized blob into audit logs and UI
            # rendering. 255 chars is comfortably above any real
            # hostname-derived label.
            raw_dn = a.get("display_name") or a.get("displayName")
            display_name = raw_dn[:255] if isinstance(raw_dn, str) else None
            try:
                await get_or_create_agent(
                    db,
                    tenant_id=body.tenant_id,
                    agent_id=agent_key,
                    fleet_id=body.fleet_id,
                    display_name=display_name,
                    install_id=body.install_id,
                )
            except Exception:
                # A single agent upsert failure mustn't drop the heartbeat
                # — the node + commands path is the contract; the row
                # refresh is best-effort observability.
                logger.warning(
                    "fleet.heartbeat: agent upsert failed for agent_id=%s in tenant=%s",
                    agent_key,
                    body.tenant_id,
                    exc_info=True,
                )
                failed_agents.append(str(agent_key))
                # Clear any ``PendingRollbackError`` left on the session
                # by the failed call (e.g. a flush that hit
                # IntegrityError) so subsequent iterations of the loop
                # can use the session normally instead of cascading
                # rollback errors. Best-effort — if the rollback itself
                # fails the session is still poisoned, but we can't do
                # better than swallow and continue.
                try:
                    await db.rollback()
                except Exception:
                    pass
        # Persist any audit rows (``agent_registered``) that
        # ``get_or_create_agent`` queued on the local session. The
        # storage-api's agent row was already committed by the HTTP
        # call inside the loop; this is just for the audit side-effect.
        # Guarded because a per-agent failure inside the loop above can
        # leave the SQLAlchemy session in an error state — without the
        # try/except, a ``PendingRollbackError`` here would 500 the
        # heartbeat and drop the pending-commands response, which is
        # the route's actual contract. Same posture as the per-agent
        # exception swallow: best-effort observability, never break
        # the heartbeat.
        try:
            await db.commit()
        except Exception:
            logger.warning(
                "fleet.heartbeat: failed to commit audit rows for tenant=%s",
                body.tenant_id,
                exc_info=True,
            )
        # Summary log so the committed audit trail is recoverable: the
        # individual per-agent warnings above are stack-traced but not
        # easy to correlate; this single line tells the on-call exactly
        # how many agents in the batch failed and which ones, with the
        # tenant pivot for dashboard filters.
        if failed_agents:
            logger.warning(
                "fleet.heartbeat: agent upsert failed for %d/%d agents in tenant=%s: %s",
                len(failed_agents),
                len(body.agents),
                body.tenant_id,
                failed_agents,
            )

    node_id = node.get("id", "")
    node_name = node.get("node_name", body.node_name)

    # Fetch pending commands ONCE. Used twice: (a) by the auto-upgrade
    # gate to skip queueing if a deploy is already in-flight, and (b)
    # returned to the caller in the response payload. Pre-refactor
    # these were two separate ``get_pending_commands`` round-trips per
    # heartbeat (CAURA-444 review feedback).
    try:
        pending = await sc.get_pending_commands(body.tenant_id, node_name)
    except Exception:
        # Same fail-loud-but-continue posture as the prior dedicated
        # helper. Log so a chronic storage outage on this path is
        # observable; treat as "no pending commands" so the rest of
        # the heartbeat still completes (and the auto-upgrade gate
        # below sees no in-flight deploy → may queue one).
        logger.warning(
            "fleet.heartbeat: failed to fetch pending commands node=%s tenant=%s",
            node_name,
            body.tenant_id,
            exc_info=True,
        )
        pending = []

    # CAURA-444: opportunistic auto-upgrade. Compares incoming
    # plugin_version to MIN_RECOMMENDED_PLUGIN_VERSION and queues a deploy
    # command when behind. Reads ``pending`` to check for an
    # in-flight deploy. Multiple guards inside the helper — see
    # _maybe_queue_auto_upgrade docstring. Returns True iff it queued
    # a new command; we re-fetch in that case so the response carries
    # it back to the plugin this heartbeat (instead of waiting 60 s).
    # ``node`` came from the earlier ``upsert_node`` call and includes the
    # storage-issued UUID. ``_maybe_queue_auto_upgrade`` needs it because
    # ``fleet_commands.node_id`` is NOT NULL (FK to ``fleet_nodes.id``); a
    # ``node_name``-only insert silently drops to None via ``_filter_fields``
    # and 500s at the DB layer. Skip the queue (gracefully) if the upsert
    # somehow didn't return an id — we'd rather miss one auto-upgrade tick
    # than crash the heartbeat handler.
    node_id_for_queue = node.get("id") if isinstance(node, dict) else None
    if node_id_for_queue:
        queued_new = await _maybe_queue_auto_upgrade(
            db=db, sc=sc, body=body, pending_commands=pending, node_id=str(node_id_for_queue)
        )
    else:
        queued_new = False
    if queued_new:
        try:
            commands = await sc.get_pending_commands(body.tenant_id, node_name)
        except Exception:
            logger.warning(
                "fleet.heartbeat: failed to re-fetch pending commands after "
                "auto-upgrade queue node=%s tenant=%s",
                node_name,
                body.tenant_id,
                exc_info=True,
            )
            commands = pending  # fall back to pre-queue list
    else:
        commands = pending

    if commands:
        await sc.ack_commands([c.get("id") for c in commands])

    return {
        "ok": True,
        "node_id": str(node_id),
        "commands": [
            {
                "id": str(c.get("id", "")),
                "command": c.get("command"),
                "payload": c.get("payload"),
            }
            for c in commands
        ],
    }


# ── Command result ──


@router.post("/fleet/commands/{command_id}/result")
async def command_result(
    command_id: UUID,
    body: CommandResultIn,
    auth: AuthContext = Depends(get_auth_context),
):
    """Plugin reports command completion."""
    auth.enforce_read_only()
    sc = get_storage_client()
    # Tenant-scope the update: keying on command_id alone would let any
    # authenticated tenant complete another tenant's command by UUID
    # (cross-tenant BOLA). ``auth.tenant_id`` is None only for admin
    # credentials, which legitimately operate unscoped.
    updated = await sc.update_command_status(
        str(command_id),
        {
            "tenant_id": auth.tenant_id,
            "status": body.status,
            "result": body.result,
            "completed_at": datetime.now(UTC).isoformat(),
        },
    )
    if not updated or not updated.get("ok", False):
        raise HTTPException(status_code=404, detail="Command not found")
    return {"ok": True}


# ── Fleet nodes (frontend reads) ──


@router.get("/fleet/nodes")
async def list_nodes(
    tenant_id: str = Query(...),
    fleet_id: str | None = Query(default=None),
    auth: AuthContext = Depends(get_auth_context),
):
    """List fleet nodes for a tenant with computed status."""
    auth.enforce_tenant(tenant_id)

    sc = get_storage_client()
    nodes = await sc.list_nodes(tenant_id, fleet_id=fleet_id)
    now = datetime.now(UTC)

    out = []
    for n in nodes:
        hb = n.get("last_heartbeat")
        age = _age_seconds(hb, now) if hb else 999999
        if age > NODE_OFFLINE_SECONDS:
            status = "offline"
        elif age > NODE_STALE_SECONDS:
            status = "stale"
        else:
            status = "online"

        out.append(
            {
                "node_id": str(n.get("id", "")),
                "node_name": n.get("node_name"),
                "fleet_id": n.get("fleet_id"),
                "hostname": n.get("hostname"),
                "ip": n.get("ip"),
                "openclaw_version": n.get("openclaw_version"),
                "plugin_version": n.get("plugin_version"),
                "plugin_hash": n.get("plugin_hash"),
                "os_info": n.get("os_info"),
                "status": status,
                "agents": n.get("agents_json"),
                "tools": n.get("tools_json"),
                "channels": n.get("channels_json"),
                "metadata": n.get("metadata"),
                "last_heartbeat": n.get("last_heartbeat"),
                "created_at": n.get("created_at"),
            }
        )

    return out


# ── Fleet & agent stats ──


@router.get("/fleet/stats")
async def fleet_stats(
    tenant_id: str = Query(...),
    fleet_id: str | None = Query(default=None),
    auth: AuthContext = Depends(get_auth_context),
):
    """Per-agent and fleet-level memory stats for the Fleet UI."""
    auth.enforce_tenant(tenant_id)
    sc = get_storage_client()
    return await sc.fleet_stats(tenant_id, fleet_id)


# ── Queue command (frontend posts) ──


@router.post("/fleet/commands", status_code=201)
async def create_command(
    body: CommandIn,
    auth: AuthContext = Depends(get_auth_context),
):
    """Queue a command for a fleet node."""
    sc = get_storage_client()
    # We need to verify node exists and get its tenant_id
    # The storage client get_node takes tenant_id + node_name, but we have node_id
    # Create the command via the storage client
    tenant_id = body.tenant_id or auth.tenant_id
    cmd = await sc.create_command(
        {
            "tenant_id": tenant_id,
            "node_id": str(body.node_id),
            "command": body.command,
            "payload": body.payload,
        }
    )
    return {"id": str(cmd.get("id", "")), "status": cmd.get("status", "pending")}


# ── Command history ──


@router.get("/fleet/commands")
async def list_commands(
    tenant_id: str = Query(...),
    node_id: UUID | None = Query(default=None),
    auth: AuthContext = Depends(get_auth_context),
):
    """List recent commands for a tenant, optionally filtered by node."""
    auth.enforce_tenant(tenant_id)

    sc = get_storage_client()
    commands = await sc.list_commands(tenant_id=tenant_id)

    return [
        {
            "id": str(c.get("id", "")),
            "node_id": str(c.get("node_id", "")),
            "command": c.get("command"),
            "payload": c.get("payload"),
            "status": c.get("status"),
            "result": c.get("result"),
            "created_at": c.get("created_at"),
            "acked_at": c.get("acked_at"),
            "completed_at": c.get("completed_at"),
        }
        for c in commands
    ]


# ── Helpers ──


def _age_seconds(timestamp: str | None, now: datetime) -> float:
    """Compute age in seconds from an ISO timestamp string."""
    if not timestamp:
        return 999999
    try:
        if isinstance(timestamp, str):
            dt = datetime.fromisoformat(timestamp)
        else:
            dt = timestamp
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        return (now - dt).total_seconds()
    except (ValueError, TypeError):
        return 999999
