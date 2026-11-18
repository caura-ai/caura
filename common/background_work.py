"""Background-only model admission. Service startup supplies the storage adapter."""

from __future__ import annotations

from collections.abc import Awaitable, Callable

Reserver = Callable[..., Awaitable[dict]]
_reserver: Reserver | None = None
_releaser: Reserver | None = None
_checker: Reserver | None = None


class BackgroundWorkDeferred(Exception):
    """A scheduling decision, never a provider failure or a foreground refusal."""

    def __init__(self, reason: str, retry_after_seconds: int = 60) -> None:
        super().__init__(reason)
        self.reason = reason
        self.retry_after_seconds = retry_after_seconds


def set_background_reserver(
    reserver: Reserver | None,
    releaser: Reserver | None = None,
    checker: Reserver | None = None,
) -> None:
    global _reserver, _releaser, _checker
    _reserver, _releaser, _checker = reserver, releaser, checker


async def reserve_background_attempt(**payload) -> dict:
    if _reserver is None:
        raise BackgroundWorkDeferred("budget_unavailable")
    return await _reserver(**payload)


async def release_background_attempt(**payload) -> None:
    if _releaser is not None:
        await _releaser(**payload)


async def check_background_admission(**payload) -> dict | None:
    """Whether a reservation would be admitted now, without reserving one.

    ``None`` when the service registered no checker: the caller then has no
    answer and goes on to the reservation, which still decides.
    """
    if _checker is None:
        return None
    return await _checker(**payload)
