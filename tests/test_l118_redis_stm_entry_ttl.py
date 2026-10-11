"""Redis STM honours the per-entry TTL the write receipt quotes (L-118).

``STMWriteResponse.ttl`` describes the entry. Redis held a whole list under one
key TTL that every later write renewed, so an agent posting hourly read back
week-old notes there and only the last day in memory. Reads now drop entries
older than the TTL, as the in-memory backend does; the key TTL still bounds
what Redis keeps.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest

pytestmark = pytest.mark.unit

_TTL = 3600


class _Redis:
    """Answers ``lrange`` from a fixed list, newest first, as LPUSH leaves it."""

    def __init__(self, entries: list[dict]) -> None:
        self._raw = [json.dumps(entry) for entry in entries]

    async def lrange(self, key: str, start: int, stop: int) -> list[str]:
        return self._raw[start : stop + 1]


async def _async(value):
    return value


def _stm(monkeypatch, entries: list[dict]):
    from core_api.providers.redis_stm import RedisSTM

    stm = RedisSTM(notes_ttl=_TTL, bulletin_ttl=_TTL)
    redis = _Redis(entries)
    monkeypatch.setattr(stm, "_redis", staticmethod(lambda: _async(redis)))
    return stm


def _entries() -> list[dict]:
    now = datetime.now(UTC)
    return [
        {"id": "fresh", "posted_at": now.isoformat()},
        {"id": "expired", "posted_at": (now - timedelta(seconds=2 * _TTL)).isoformat()},
        # Written before entries carried a timestamp: the key TTL bounds it.
        {"id": "undated"},
    ]


async def test_notes_older_than_the_ttl_are_not_read(monkeypatch) -> None:
    stm = _stm(monkeypatch, _entries())

    notes = await stm.get_notes("t1", "a1")

    assert [n["id"] for n in notes] == ["fresh", "undated"]


async def test_bulletin_entries_older_than_the_ttl_are_not_read(monkeypatch) -> None:
    stm = _stm(monkeypatch, _entries())

    bulletin = await stm.get_bulletin("t1", "f1")

    assert [e["id"] for e in bulletin] == ["fresh", "undated"]
