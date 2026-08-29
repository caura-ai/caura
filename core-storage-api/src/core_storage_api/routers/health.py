"""Health check endpoints."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException

from core_storage_api.config import settings

router = APIRouter(tags=["Health"])


@router.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/readyz")
async def readyz() -> dict[str, str]:
    if not settings.core_storage_shared_secret.get_secret_value():
        raise HTTPException(
            status_code=503,
            detail="storage service credentials not configured",
        )
    return {"status": "ok"}
