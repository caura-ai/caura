"""Shared configuration helpers for internal storage authentication."""

from __future__ import annotations

from pathlib import Path

import httpx

STORAGE_SHARED_SECRET_REJECTION_DETAIL = "invalid storage service credentials"


def is_storage_shared_secret_rejection(response: httpx.Response) -> bool:
    """Return whether storage rejected its service-to-service credential."""
    if response.status_code != 401:
        return False
    try:
        payload = response.json()
    except (AttributeError, ValueError):
        return False
    return (
        isinstance(payload, dict)
        and payload.get("detail") == STORAGE_SHARED_SECRET_REJECTION_DETAIL
    )


def read_shared_secret_file(
    path: str, *, env_name: str = "CORE_STORAGE_SHARED_SECRET_FILE"
) -> str:
    """Read a non-empty shared secret from ``path`` when one is configured.

    ``env_name`` is the setting that named the file, for the error message; the
    admin key reads its file through here too.
    """
    if not path:
        return ""
    try:
        secret = Path(path).read_text().strip()
    except OSError as exc:
        raise ValueError(f"cannot read {env_name}: {exc}") from exc
    if not secret:
        raise ValueError(f"{env_name} is empty")
    return secret
