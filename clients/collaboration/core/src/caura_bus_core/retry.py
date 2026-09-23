"""Transient HTTP classification, Retry-After parsing and capped equal-jitter reconnect delays."""

import asyncio
import secrets


def transient_status(status: int) -> bool:
    return status == 429 or 500 <= status < 600


class Backoff:
    def __init__(self, *, maximum: float = 30):
        self.failures = 0
        self.maximum = maximum

    def reset(self) -> None:
        self.failures = 0

    async def sleep(self) -> None:
        ceiling = min(2 ** min(self.failures, 6), self.maximum)
        self.failures = min(self.failures + 1, 6)
        await asyncio.sleep(ceiling * (0.5 + secrets.randbelow(1001) / 2000))


RETRY_AFTER_CAP_SECONDS = 5.0


def retry_after_seconds(response, *, cap: float = RETRY_AFTER_CAP_SECONDS) -> float | None:
    """Delta-seconds Retry-After from a retryable response, capped; None when absent or unusable."""
    value = response.headers.get("Retry-After")
    if not value:
        return None
    try:
        seconds = float(value.strip())
    except ValueError:
        return None
    if seconds < 0:
        return None
    return min(seconds, cap)
