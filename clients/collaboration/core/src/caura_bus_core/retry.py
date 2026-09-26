"""Transient HTTP classification and capped equal-jitter reconnect delays."""

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
