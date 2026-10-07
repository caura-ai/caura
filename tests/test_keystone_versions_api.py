"""Keystone versions through core-api (plan row g1.12).

The versions routes give an agent the rule-set hash the keystones list gives
it: the hash a session receipt carries. Each write's actor is covered with the
rest of the governance writes, in ``test_audit_actor``.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException

from core_api.auth import AuthContext
from core_api.routes import keystones
from tests.conftest import get_admin_headers
from tests.conftest import uid as _uid

_PAGE = {"count": 0, "items": [], "next_before": None}


def _storage(monkeypatch, **returns) -> MagicMock:
    sc = MagicMock(name="storage_client")
    for name, value in returns.items():
        setattr(sc, name, AsyncMock(return_value=value))
    monkeypatch.setattr(keystones, "get_storage_client", lambda: sc)
    return sc


async def _list_versions(auth: AuthContext, **asked):
    params = {
        "tenant_id": "t1",
        "fleet_id": None,
        "agent_id": None,
        "limit": 50,
        "before": None,
    }
    return await keystones.list_keystone_versions(**(params | asked), auth=auth)


@pytest.mark.unit
async def test_versions_are_read_by_their_own_tenant(monkeypatch) -> None:
    """As ``/audit-log``: the history names who made each change, so a
    credential that may only READ the tenant's rules doesn't get it."""
    sc = _storage(monkeypatch, list_keystone_versions=_PAGE, get_keystone_version={})
    reader = AuthContext(tenant_id="t2", readable_tenant_ids=["t2", "t1"])
    with pytest.raises(HTTPException) as listed:
        await _list_versions(reader)
    with pytest.raises(HTTPException) as one:
        await keystones.get_keystone_version(
            version=1, tenant_id="t1", fleet_id=None, agent_id=None, auth=reader
        )
    assert (listed.value.status_code, one.value.status_code) == (403, 403)
    sc.list_keystone_versions.assert_not_awaited()
    sc.get_keystone_version.assert_not_awaited()


@pytest.mark.unit
async def test_versions_resolve_for_the_agent_the_list_would(monkeypatch) -> None:
    sc = _storage(monkeypatch, list_keystone_versions=_PAGE, get_keystone_version={})
    monkeypatch.setattr(
        keystones, "canonical_service_agent_id", lambda a: f"canonical-{a}"
    )
    me = AuthContext(tenant_id="t1")
    # An agent without a fleet is dropped, as the list drops it.
    await _list_versions(me, agent_id="a1", limit=10, before=3)
    assert sc.list_keystone_versions.await_args.kwargs == {
        "fleet_id": None,
        "agent_id": None,
        "limit": 10,
        "before": 3,
    }
    await _list_versions(me, fleet_id="f1", agent_id="a1")
    assert sc.list_keystone_versions.await_args.kwargs["agent_id"] == "canonical-a1"
    await keystones.get_keystone_version(
        version=2, tenant_id="t1", fleet_id="f1", agent_id="a1", auth=me
    )
    assert sc.get_keystone_version.await_args.kwargs == {
        "fleet_id": "f1",
        "agent_id": "canonical-a1",
    }


@pytest.mark.unit
async def test_an_unknown_version_is_not_found(monkeypatch) -> None:
    _storage(monkeypatch, get_keystone_version=None)
    with pytest.raises(HTTPException) as exc:
        await keystones.get_keystone_version(
            version=9,
            tenant_id="t1",
            fleet_id=None,
            agent_id=None,
            auth=AuthContext(tenant_id="t1"),
        )
    assert exc.value.status_code == 404


async def test_a_version_storage_cannot_number_is_refused_here(
    client, tenant_id, monkeypatch
) -> None:
    """Storage numbers versions in an int4: a larger ``version`` or ``before``
    is a 422 here, without a call to storage."""
    sc = _storage(monkeypatch, list_keystone_versions=_PAGE, get_keystone_version={})
    headers = get_admin_headers()
    versions = "/api/v1/keystones/versions"
    for path, status in (
        (f"{versions}/{2**31}?tenant_id={tenant_id}", 422),
        (f"{versions}?tenant_id={tenant_id}&before={2**31}", 422),
        (f"{versions}/{2**31 - 1}?tenant_id={tenant_id}", 200),
        (f"{versions}?tenant_id={tenant_id}&before={2**31 - 1}", 200),
    ):
        resp = await client.get(path, headers=headers)
        assert resp.status_code == status, (path, resp.text)
    assert sc.get_keystone_version.await_count == 1
    assert sc.list_keystone_versions.await_count == 1


# ── end to end ────────────────────────────────────────────────────────────


async def test_a_version_carries_the_hash_the_list_gives_an_agent(
    client, tenant_id
) -> None:
    """The plan row's done criterion: editing a keystone creates v+1 with the
    new hash, and the hash is the one the list's envelope gives (g1.10). A
    change elsewhere in the tenant is a version too, but leaves this agent's
    hash alone."""
    headers = get_admin_headers()
    fleet, agent = f"fleet-{_uid()}", f"agent-{_uid()}"
    asked = f"tenant_id={tenant_id}&fleet_id={fleet}&agent_id={agent}"

    async def write(doc_id: str, scope: str, **fields) -> None:
        rule = {
            "tenant_id": tenant_id,
            "doc_id": doc_id,
            "title": doc_id,
            "scope": scope,
        }
        rule |= {"content": "Do it.", "weight": "med"} | fields
        resp = await client.post("/api/v1/keystones", json=rule, headers=headers)
        assert resp.status_code == 200, resp.text

    async def now() -> tuple[str, dict]:
        listed = await client.get(
            f"/api/v1/keystones?{asked}&envelope=true", headers=headers
        )
        page = await client.get(
            f"/api/v1/keystones/versions?{asked}&limit=1", headers=headers
        )
        [latest] = page.json()["items"]
        return listed.json()["rule_set_hash"], latest

    await write("everyone", "tenant")
    await write("the-fleet", "fleet", fleet_id=fleet)
    await write("this-agent", "agent", fleet_id=fleet, agent_id=agent)
    listed_hash, latest = await now()
    assert (latest["version"], latest["op"], latest["doc_id"]) == (
        3,
        "set",
        "this-agent",
    )
    assert latest["rule_set_hash"] == listed_hash
    assert latest["actor_agent_id"] == "rest-admin"

    await write(
        "this-agent", "agent", fleet_id=fleet, agent_id=agent, content="Changed."
    )
    edited_hash, edited = await now()
    assert (edited["version"], edited["rule_set_hash"]) == (4, edited_hash)
    assert edited_hash != listed_hash

    await write("another-fleet", "fleet", fleet_id=f"other-{fleet}")
    unmoved_hash, unmoved = await now()
    assert (unmoved["version"], unmoved["rule_set_hash"]) == (5, edited_hash)
    assert unmoved_hash == edited_hash

    detail = await client.get(f"/api/v1/keystones/versions/4?{asked}", headers=headers)
    assert detail.status_code == 200, detail.text
    assert {r["doc_id"] for r in detail.json()["items"]} == {
        "everyone",
        "the-fleet",
        "this-agent",
    }
