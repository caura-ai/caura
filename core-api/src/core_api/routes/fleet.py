"""Fleet heartbeat and command channel — replaces WebSocket/SSH gateway model."""

import logging
from datetime import UTC, datetime
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)

from core_api.auth import AuthContext, get_auth_context
from core_api.clients.storage_client import get_storage_client
from core_api.constants import NODE_OFFLINE_SECONDS, NODE_STALE_SECONDS
from core_api.db.session import get_db
from core_api.services.audit_service import log_action
from core_api.version_compat import MIN_RECOMMENDED_PLUGIN_VERSION, is_plugin_outdated

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


# ── Heartbeat ──


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
            "metadata": body.metadata,
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

    # Fetch pending commands and mark as acked
    commands = await sc.get_pending_commands(body.tenant_id, node_name)
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
    sc = get_storage_client()
    updated = await sc.update_command_status(
        str(command_id),
        {
            "status": body.status,
            "result": body.result,
            "completed_at": datetime.now(UTC).isoformat(),
        },
    )
    if not updated:
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
