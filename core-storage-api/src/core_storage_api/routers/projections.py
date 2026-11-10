"""Saved field layouts (``common/models/memory_projection.py``).

core-api fits a tenant's field layout once, under a claim, and saves the model
and each memory's position here; readers serve the saved positions through
``POST /memories/field-page`` with ``model_version``.

  - ``POST /projections/claim``: ``{tenant_id, claim_token, lease_seconds?}``.
    Take the tenant's next fit, for 30-3600 s (default 900).
  - ``POST /projections/release``: ``{tenant_id, claim_token}``. Give it up
    without saving (the fit failed).
  - ``GET  /projections/model/{tenant_id}``: the saved model, and whether a
    fit is running.
  - ``PUT  /projections/model``: ``{tenant_id, claim_token, model_version,
    kind, dim, n_fit, params_b64}``. Save a fit; 409 unless the token holds
    the live claim.
  - ``POST /projections/positions``: ``{tenant_id, model_version,
    claim_token?, items}``, up to 2,000 ``{memory_id, x, y, cluster,
    row_version}`` with ``x`` and ``y`` in [0, 1]. ``model_version`` must be the saved model's, or
    ``claim_token`` must hold the live claim; 409 otherwise. ``row_version``
    is the field page's for the memory when the position was computed; a
    memory whose row has changed since is skipped.

Same trust model as the rest of core-storage-api (see ``routers/purge.py``):
no router-level authentication beyond the shared secret.
"""

from __future__ import annotations

import base64
import binascii
import re
from uuid import UUID

from fastapi import APIRouter, HTTPException, Request

from core_storage_api.routers._validation import _bounded_int, _require
from core_storage_api.services.postgres_service import PostgresService

router = APIRouter(prefix="/projections", tags=["Projections"])
_svc = PostgresService()

# Bounds on what a caller may send. The model blob is about 210 KB for 1,024
# dimensions (the mean and 50 axes); 4 MiB leaves room for wider embeddings.
MAX_TOKEN_CHARS = 128
MAX_VERSION_CHARS = 64
MAX_KIND_CHARS = 32
MAX_DIM = 8192
MAX_PARAMS_BYTES = 4 * 1024 * 1024
MAX_POSITIONS = 2000
MAX_CLUSTER = 32767  # smallint
# A PostgreSQL transaction id as text (``xmin::text``), the field page's ``row_version``.
ROW_VERSION = re.compile(r"[0-9]{1,20}")


def _object(body: object) -> None:
    # Each handler parses its own body, inline, so the tenant-scope gate can
    # see that ``tenant_id`` comes from the request.
    if not isinstance(body, dict):
        raise HTTPException(status_code=422, detail="request body must be a JSON object")


def _bounded_str(body: dict, key: str, max_chars: int) -> str:
    value = body.get(key)
    if not isinstance(value, str) or not 0 < len(value) <= max_chars:
        raise HTTPException(status_code=422, detail=f"{key} must be a string of 1-{max_chars} characters")
    return value


def _fraction(item: dict, key: str) -> float:
    """A coordinate in [0, 1], the range the layout normalizes positions to.

    The comparison is the whole check, and it comes before any float
    conversion: NaN and infinity fail it, and so does an integer too large for
    a float, which ``math.isfinite`` would raise on. In range, the value also
    fits the ``REAL`` column.
    """
    value = item.get(key)
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 <= value <= 1:
        raise HTTPException(status_code=422, detail=f"items[].{key} must be a number from 0 to 1")
    return float(value)


@router.post("/claim")
async def claim(request: Request) -> dict:
    body: dict = await request.json()
    _object(body)
    tenant_id = _require(body, "tenant_id")
    token = _bounded_str(body, "claim_token", MAX_TOKEN_CHARS)
    lease = _bounded_int(body, "lease_seconds", 900, 3600, minimum=30)
    return await _svc.projection_claim(tenant_id=tenant_id, claim_token=token, lease_seconds=lease)


@router.post("/release")
async def release(request: Request) -> dict:
    body: dict = await request.json()
    _object(body)
    tenant_id = _require(body, "tenant_id")
    token = _bounded_str(body, "claim_token", MAX_TOKEN_CHARS)
    return {"released": await _svc.projection_release(tenant_id=tenant_id, claim_token=token)}


# The tenant is a path parameter, not ``Query(...)``: the tenant-scope gate reads
# any parameter with a default as optional (scripts/tenant_scope_gate.py).
@router.get("/model/{tenant_id}")
async def get_model(tenant_id: str) -> dict:
    return await _svc.projection_model_get(tenant_id=tenant_id)


@router.put("/model")
async def save_model(request: Request) -> dict:
    body: dict = await request.json()
    _object(body)
    tenant_id = _require(body, "tenant_id")
    token = _bounded_str(body, "claim_token", MAX_TOKEN_CHARS)
    version = _bounded_str(body, "model_version", MAX_VERSION_CHARS)
    kind = _bounded_str(body, "kind", MAX_KIND_CHARS)
    dim = _bounded_int(body, "dim", 0, MAX_DIM, minimum=1)
    n_fit = _bounded_int(body, "n_fit", 0, 2**31 - 1, minimum=0)
    encoded = body.get("params_b64")
    if not isinstance(encoded, str) or not encoded:
        raise HTTPException(status_code=422, detail="params_b64 is required")
    try:
        params = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise HTTPException(status_code=422, detail="params_b64 must be base64") from exc
    if len(params) > MAX_PARAMS_BYTES:
        raise HTTPException(status_code=422, detail=f"params must be <= {MAX_PARAMS_BYTES} bytes")
    saved = await _svc.projection_model_save(
        tenant_id=tenant_id,
        claim_token=token,
        model_version=version,
        kind=kind,
        dim=dim,
        n_fit=n_fit,
        params=params,
    )
    if not saved:
        raise HTTPException(status_code=409, detail="projection_claim_not_held")
    return {"saved": True, "model_version": version}


@router.post("/positions")
async def save_positions(request: Request) -> dict:
    body: dict = await request.json()
    _object(body)
    tenant_id = _require(body, "tenant_id")
    version = _bounded_str(body, "model_version", MAX_VERSION_CHARS)
    token = _bounded_str(body, "claim_token", MAX_TOKEN_CHARS) if "claim_token" in body else None
    raw = body.get("items")
    if not isinstance(raw, list) or not 0 < len(raw) <= MAX_POSITIONS:
        raise HTTPException(status_code=422, detail=f"items must be a list of 1-{MAX_POSITIONS} positions")
    items = []
    for item in raw:
        if not isinstance(item, dict):
            raise HTTPException(status_code=422, detail="items[] must be objects")
        try:
            memory_id = UUID(str(item.get("memory_id")))
        except ValueError as exc:
            raise HTTPException(status_code=422, detail="items[].memory_id must be a UUID") from exc
        cluster = item.get("cluster")
        if isinstance(cluster, bool) or not isinstance(cluster, int) or not 0 <= cluster <= MAX_CLUSTER:
            raise HTTPException(status_code=422, detail=f"items[].cluster must be an integer 0-{MAX_CLUSTER}")
        row_version = item.get("row_version")
        if not isinstance(row_version, str) or not ROW_VERSION.fullmatch(row_version):
            raise HTTPException(
                status_code=422, detail="items[].row_version must be the field page's row_version"
            )
        items.append(
            {
                "memory_id": memory_id,
                "x": _fraction(item, "x"),
                "y": _fraction(item, "y"),
                "cluster": cluster,
                "row_version": row_version,
            }
        )
    saved = await _svc.projection_positions_save(
        tenant_id=tenant_id, model_version=version, items=items, claim_token=token
    )
    if saved is None:
        raise HTTPException(status_code=409, detail="projection_version_not_current")
    return {"saved": saved}
