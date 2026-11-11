"""Background-only model admission. Service startup supplies the storage adapter."""

from __future__ import annotations

from collections.abc import Awaitable, Callable

Reserver = Callable[..., Awaitable[dict]]
_reserver: Reserver | None = None
_releaser: Reserver | None = None


class BackgroundWorkDeferred(Exception):
    """A scheduling decision, never a provider failure or a foreground refusal."""

    def __init__(self, reason: str, retry_after_seconds: int = 60) -> None:
        super().__init__(reason)
        self.reason = reason
        self.retry_after_seconds = retry_after_seconds


def set_background_reserver(
    reserver: Reserver | None, releaser: Reserver | None = None
) -> None:
    global _reserver, _releaser
    _reserver, _releaser = reserver, releaser


async def reserve_background_attempt(**payload) -> dict:
    if _reserver is None:
        raise BackgroundWorkDeferred("budget_unavailable")
    return await _reserver(**payload)


async def release_background_attempt(**payload) -> None:
    if _releaser is not None:
        await _releaser(**payload)
