"""Shared fail-closed request-body validation guards for the storage routers.

Each storage router validates its own request contract (it never trusts the
calling service). ``_require`` / ``_require_number`` are the common primitives,
kept in one place so the four routers that use them don't drift.
"""

from __future__ import annotations

from fastapi import HTTPException


def _require(body: dict, key: str) -> str:
    """Fail-closed required-field guard — 422 if ``key`` is missing/falsy."""
    val = body.get(key)
    if not val:
        raise HTTPException(status_code=422, detail=f"{key} is required")
    return val


def _require_number(body: dict, key: str) -> float:
    """Fail-closed numeric guard — 422 on missing / non-numeric.

    ``bool`` is a subclass of ``int`` but is never a valid numeric value here,
    so reject it explicitly.
    """
    val = body.get(key)
    if isinstance(val, bool) or not isinstance(val, (int, float)):
        raise HTTPException(status_code=422, detail=f"{key} (number) is required")
    return float(val)
