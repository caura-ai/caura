"""Fleet node and command endpoints."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

from fastapi import APIRouter, HTTPException, Request

from core_storage_api.schemas import FLEET_COMMAND_FIELDS, FLEET_NODE_FIELDS, orm_to_dict
from core_storage_api.services.postgres_service import PostgresService

router = APIRouter(prefix="/fleet", tags=["Fleet"])
_svc = PostgresService()


# ------------------------------------------------------------------
# Fleets (top-level)
# ------------------------------------------------------------------


@router.get("")
async def list_fleets(tenant_id: str) -> list[dict]:
    rows = await _svc.fleet_list(tenant_id=tenant_id)
    return [
        {
            "fleet_id": r.fleet_id,
            "node_count": r.node_count,
            "last_heartbeat": r.last_heartbeat.isoformat() if r.last_heartbeat else None,
        }
        for r in rows
    ]


@router.get("/stats")
async def fleet_stats(
    tenant_id: str,
    fleet_id: str | None = None,
) -> dict:
    return await _svc.fleet_agent_stats(tenant_id, fleet_id)


@router.get("/exists")
async def fleet_exists(tenant_id: str, fleet_id: str) -> dict:
    exists = await _svc.fleet_exists(tenant_id=tenant_id, fleet_id=fleet_id)
    return {"exists": exists}


@router.delete("/{fleet_id}")
async def delete_fleet(fleet_id: str, tenant_id: str) -> dict:
    await _svc.fleet_delete(tenant_id=tenant_id, fleet_id=fleet_id)
    return {"ok": True}


# ------------------------------------------------------------------
# Nodes
# ------------------------------------------------------------------


@router.post("/nodes")
async def upsert_node(request: Request) -> dict:
    body: dict = await request.json()
    # Parse ISO datetime strings from HTTP JSON
    for dt_key in ("last_heartbeat",):
        if isinstance(body.get(dt_key), str):
            body[dt_key] = datetime.fromisoformat(body[dt_key])
    node_id = await _svc.fleet_upsert_node(values=body)
    return {"id": str(node_id)}


@router.get("/nodes")
async def list_nodes(
    tenant_id: str,
    fleet_id: str | None = None,
) -> list[dict]:
    nodes = await _svc.fleet_list_nodes(tenant_id=tenant_id, fleet_id=fleet_id)
    return [orm_to_dict(n, FLEET_NODE_FIELDS) for n in nodes]


@router.get("/nodes/count")
async def count_nodes(
    tenant_id: str,
    fleet_id: str | None = None,
) -> dict:
    if fleet_id:
        count = await _svc.fleet_count_nodes(tenant_id=tenant_id, fleet_id=fleet_id)
    else:
        nodes = await _svc.fleet_list_nodes(tenant_id=tenant_id)
        count = len(nodes)
    return {"count": count}


@router.get("/nodes/{node_name}")
async def get_node(
    node_name: str,
    tenant_id: str,
) -> dict:
    node_id = await _svc.fleet_get_node_id(tenant_id=tenant_id, node_name=node_name)
    node = await _svc.fleet_get_node_by_id(node_id=node_id)
    if node is None:
        raise HTTPException(status_code=404, detail="Node not found")
    return orm_to_dict(node, FLEET_NODE_FIELDS)


@router.delete("/nodes/{node_name}")
async def delete_node(
    node_name: str,
    tenant_id: str,
) -> dict:
    node_id = await _svc.fleet_get_node_id(tenant_id=tenant_id, node_name=node_name)
    node = await _svc.fleet_get_node_by_id(node_id=node_id)
    if node is None:
        raise HTTPException(status_code=404, detail="Node not found")
    fleet_id = getattr(node, "fleet_id", None)
    if fleet_id:
        await _svc.fleet_delete_commands_for_nodes(node_ids=[node_id])
    return {"ok": True}


# ------------------------------------------------------------------
# Commands
# ------------------------------------------------------------------


@router.post("/commands")
async def create_command(request: Request) -> dict:
    body: dict = await request.json()
    command = await _svc.fleet_add_command(body)
    return orm_to_dict(command, FLEET_COMMAND_FIELDS)


@router.get("/commands")
async def list_commands(
    tenant_id: str,
    node_name: str | None = None,
    status: str | None = None,
    limit: int = 50,
) -> list[dict]:
    node_id: UUID | None = None
    if node_name:
        node_id = await _svc.fleet_get_node_id(tenant_id=tenant_id, node_name=node_name)
    commands = await _svc.fleet_list_commands(
        tenant_id=tenant_id,
        node_id=node_id,
        limit=limit,
    )
    results = [orm_to_dict(c, FLEET_COMMAND_FIELDS) for c in commands]
    if status:
        results = [c for c in results if c.get("status") == status]
    return results


@router.get("/commands/pending")
async def get_pending_commands(
    tenant_id: str,
    node_name: str,
) -> list[dict]:
    node_id = await _svc.fleet_get_node_id(tenant_id=tenant_id, node_name=node_name)
    commands = await _svc.fleet_get_pending_commands(node_id=node_id)
    return [orm_to_dict(c, FLEET_COMMAND_FIELDS) for c in commands]


@router.patch("/commands/{command_id}/status")
async def update_command_status(command_id: UUID, request: Request) -> dict:
    body: dict = await request.json()
    status = body["status"]
    if status == "acked":
        await _svc.fleet_ack_commands(
            command_ids=[command_id],
            now=datetime.now(UTC),
        )
    return {"ok": True}


@router.post("/commands/ack")
async def ack_commands(request: Request) -> dict:
    body: dict = await request.json()
    command_ids = [UUID(cid) if isinstance(cid, str) else cid for cid in body["command_ids"]]
    await _svc.fleet_ack_commands(
        command_ids=command_ids,
        now=datetime.now(UTC),
    )
    return {"ok": True, "count": len(command_ids)}
