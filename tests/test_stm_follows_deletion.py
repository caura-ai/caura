"""Short-term memory goes with the agent or tenant it belongs to.

* Deleting an agent clears its STM notes, so whoever is registered next under
  the same id does not read them.
* An organization purge clears every STM key of each purged tenant: its
  agents' notes and its fleets' bulletins. Another tenant's keys stay, even one
  whose id is the purged tenant's id followed by a colon.
* Redis keys join tenant and scope with ``:`` and neither id is restricted, so
  a pattern on the tenant prefix could reach another tenant. The tenant clear
  deletes exactly the keys that tenant wrote.
* Both clears follow a delete that has already happened, so one that raises
  is logged and the request still succeeds.
"""

from __future__ import annotations

import pytest

from core_api.config import settings
from core_api.providers.inmemory_stm import InMemorySTM
from core_api.providers.redis_stm import RedisSTM
from core_api.services import stm_service
from tests.conftest import get_test_auth, uid


@pytest.fixture
def stm(monkeypatch):
    """STM on, on a fresh in-memory backend."""
    backend = InMemorySTM()
    monkeypatch.setattr(settings, "use_stm", True)
    monkeypatch.setattr(stm_service, "_stm_instance", backend)
    return backend


async def test_deleting_an_agent_clears_its_notes(client, sc, stm):
    tenant, agent = f"tenant-{uid()}", f"agent-{uid()}"
    await sc.create_or_update_agent(
        {"tenant_id": tenant, "agent_id": agent, "trust_level": 1}
    )
    await stm.post_note(tenant, agent, {"content": "the door code is 4417"})
    await stm.post_note(tenant, "agent-kept", {"content": "kept"})
    _, headers = get_test_auth(tenant)

    resp = await client.delete(
        f"/api/v1/agents/{agent}?tenant_id={tenant}", headers=headers
    )

    assert resp.status_code == 204, resp.text
    assert await stm.get_notes(tenant, agent) == []
    assert await stm.get_notes(tenant, "agent-kept") != []


async def test_an_org_purge_clears_its_tenants_stm(client, stm):
    tenant = f"tenant-{uid()}"
    neighbour, other = f"{tenant}:x", f"tenant-{uid()}"
    await stm.post_note(tenant, "agent-1", {"content": "the door code is 4417"})
    await stm.post_bulletin(tenant, "fleet-1", {"content": "standup moved"})
    for kept in (neighbour, other):
        await stm.post_note(kept, "agent-1", {"content": "kept"})
    _, headers = get_test_auth()

    resp = await client.post(
        "/api/v1/admin/org/purge-data", json={"tenant_ids": [tenant]}, headers=headers
    )

    assert resp.status_code == 200, resp.text
    assert await stm.get_notes(tenant, "agent-1") == []
    assert await stm.get_bulletin(tenant, "fleet-1") == []
    for kept in (neighbour, other):
        assert await stm.get_notes(kept, "agent-1") != []


class _Failing(InMemorySTM):
    async def clear_notes(self, tenant_id, agent_id):
        raise ConnectionError("STM backend went away")

    async def clear_tenant(self, tenant_id):
        raise ConnectionError("STM backend went away")


async def test_a_failed_clear_does_not_fail_the_delete_it_follows(
    client, sc, monkeypatch
):
    monkeypatch.setattr(settings, "use_stm", True)
    monkeypatch.setattr(stm_service, "_stm_instance", _Failing())
    tenant, agent = f"tenant-{uid()}", f"agent-{uid()}"
    await sc.create_or_update_agent(
        {"tenant_id": tenant, "agent_id": agent, "trust_level": 1}
    )
    _, headers = get_test_auth(tenant)

    deleted = await client.delete(
        f"/api/v1/agents/{agent}?tenant_id={tenant}", headers=headers
    )
    purged = await client.post(
        "/api/v1/admin/org/purge-data", json={"tenant_ids": [tenant]}, headers=headers
    )

    assert deleted.status_code == 204, deleted.text
    assert purged.status_code == 200, purged.text
    assert purged.json()["failed"] == {}


class _Pipe:
    def __init__(self, redis: _Redis) -> None:
        self._redis = redis
        self._ops: list = []

    def lpush(self, key, value):
        self._ops.append(lambda: self._redis.lists.setdefault(key, []).insert(0, value))

    def ltrim(self, *args): ...

    def expire(self, *args): ...

    def sadd(self, key, *members):
        self._ops.append(
            lambda: self._redis.sets.setdefault(key, set()).update(members)
        )

    async def execute(self):
        for op in self._ops:
            op()
        return []


class _Redis:
    """The lists, sets and pipeline ``RedisSTM`` uses, kept in dicts."""

    def __init__(self) -> None:
        self.lists: dict[str, list[str]] = {}
        self.sets: dict[str, set[str]] = {}

    def pipeline(self, transaction=False):
        return _Pipe(self)

    async def lrange(self, key, start, stop):
        return self.lists.get(key, [])[start : stop + 1]

    async def smembers(self, key):
        return set(self.sets.get(key, set()))

    async def delete(self, *keys):
        deleted = 0
        for key in keys:
            deleted += self.lists.pop(key, None) is not None
            deleted += self.sets.pop(key, None) is not None
        return deleted


async def _async(value):
    return value


async def test_redis_clears_exactly_the_purged_tenants_keys(monkeypatch):
    redis = _Redis()
    stm = RedisSTM()
    monkeypatch.setattr(stm, "_redis", staticmethod(lambda: _async(redis)))
    await stm.post_note("t1", "agent-1", {"content": "the door code is 4417"})
    await stm.post_bulletin("t1", "fleet-1", {"content": "standup moved"})
    await stm.post_note("t1:x", "agent-1", {"content": "kept"})
    await stm.post_note("t2", "agent-1", {"content": "kept"})

    assert await stm.clear_tenant("t1") is True

    assert await stm.get_notes("t1", "agent-1") == []
    assert await stm.get_bulletin("t1", "fleet-1") == []
    assert await stm.get_notes("t1:x", "agent-1") != []
    assert await stm.get_notes("t2", "agent-1") != []
