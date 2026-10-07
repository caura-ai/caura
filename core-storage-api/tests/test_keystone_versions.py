"""Keystone versions (migration 061): each set and delete records the tenant's
whole keystone set after it, numbered per tenant, and a version gives each agent
exactly the rules the list gave it then."""

from __future__ import annotations

import asyncio
import json
import uuid
from datetime import UTC, datetime, timedelta, timezone

import pytest
from httpx import AsyncClient, Response
from sqlalchemy import text

from common.governance.ruleset_hash import RuleSetHashError, rule_set_hash, rules_from_keystone_rows
from core_storage_api.services import keystones
from core_storage_api.services.postgres_service import PostgresService, get_session
from tests.conftest import load_migration
from tests.test_integration import PREFIX

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


def _tenant() -> str:
    # The ``test-tenant-`` prefix is what the end-of-run sweep deletes.
    return f"test-tenant-ksv-{uuid.uuid4().hex[:8]}"


def _rule(
    doc_id: str,
    scope: str = "tenant",
    *,
    fleet_id: str | None = None,
    agent_id: str | None = None,
    weight: str = "med",
    content: str = "Do the thing.",
) -> dict:
    rule = {"doc_id": doc_id, "title": doc_id, "content": content, "scope": scope, "weight": weight}
    if fleet_id is not None:
        rule["fleet_id"] = fleet_id
    if agent_id is not None:
        rule["agent_id"] = agent_id
    return rule


async def _set(client: AsyncClient, tenant: str, rule: dict, **actor: str) -> None:
    resp = await client.post(f"{PREFIX}/keystones", json={"tenant_id": tenant, **rule, **actor})
    assert resp.status_code == 200, resp.text


async def _get(client: AsyncClient, path: str, tenant: str, **params) -> Response:
    resp = await client.get(f"{PREFIX}/keystones{path}", params={"tenant_id": tenant, **params})
    assert resp.status_code == 200, resp.text
    return resp


_EARLIER = datetime(2026, 10, 6, 8, tzinfo=UTC)


async def _insert_raw(
    tenant: str, *docs: tuple[str, str | None, dict], updated_at: datetime = _EARLIER
) -> None:
    """Keystone documents written straight to the table, as no keystone write
    writes them: from before versioning, or around it."""
    async with get_session() as session:
        await session.execute(
            text(
                "INSERT INTO documents (tenant_id, fleet_id, collection, doc_id, data, updated_at) "
                "VALUES (:t, :f, '_keystones', :d, CAST(:data AS json), :at)"
            ),
            [
                {"t": tenant, "f": fleet_id, "d": doc_id, "data": json.dumps(data), "at": updated_at}
                for doc_id, fleet_id, data in docs
            ],
        )


def _hash(rows: list[dict]) -> str | None:
    try:
        return rule_set_hash(rules_from_keystone_rows(rows))
    except RuleSetHashError:
        return None


async def _assert_latest_matches_the_list(client: AsyncClient, tenant: str, **asked: str) -> None:
    """The tenant's latest version gives ``asked`` what the list gives it: the
    same rules in the same order, the same cap, and so the same hash."""
    resp = await _get(client, "", tenant, **asked)
    listed, truncated = resp.json(), resp.headers.get("X-Truncated") == "true"
    [latest] = (await _get(client, "/versions", tenant, limit=1, **asked)).json()["items"]
    detail = (await _get(client, f"/versions/{latest['version']}", tenant, **asked)).json()
    assert [r["doc_id"] for r in detail["items"]] == [r["doc_id"] for r in listed], asked
    assert (latest["rule_count"], latest["truncated"]) == (len(listed), truncated), asked
    assert latest["rule_set_hash"] == _hash(listed), asked


async def test_each_set_and_delete_records_the_set_it_leaves(client: AsyncClient) -> None:
    tenant = _tenant()
    await _set(client, tenant, _rule("a"), actor_agent_id="author", actor_user_id="user-1")
    await _set(client, tenant, _rule("b"))
    await _set(client, tenant, _rule("a", content="Changed."))
    resp = await client.delete(
        f"{PREFIX}/keystones/b", params={"tenant_id": tenant, "actor_agent_id": "remover"}
    )
    assert resp.status_code == 200, resp.text
    # A delete that deletes nothing is no change, so no version.
    resp = await client.delete(f"{PREFIX}/keystones/b", params={"tenant_id": tenant})
    assert resp.status_code == 404

    page = (await _get(client, "/versions", tenant)).json()
    assert [(v["version"], v["op"], v["doc_id"]) for v in page["items"]] == [
        (4, "delete", "b"),
        (3, "set", "a"),
        (2, "set", "b"),
        (1, "set", "a"),
    ]
    assert (page["count"], page["next_before"]) == (4, None)
    first, last = page["items"][-1], page["items"][0]
    assert (first["actor_agent_id"], first["actor_user_id"]) == ("author", "user-1")
    assert (last["actor_agent_id"], last["actor_user_id"]) == ("remover", None)

    # Each version holds the whole set its change left.
    two = (await _get(client, "/versions/2", tenant)).json()
    assert {r["doc_id"] for r in two["items"]} == {"a", "b"}
    [only] = (await _get(client, "/versions/4", tenant)).json()["items"]
    assert (only["doc_id"], only["data"]["content"]) == ("a", "Changed.")


async def test_a_version_gives_each_agent_what_the_list_gives_it(client: AsyncClient) -> None:
    """The hash a receipt carries is the list's, so a version must resolve its
    snapshot exactly as the list resolves the live set, for every combination of
    fleet and agent."""
    tenant = _tenant()
    for rule in (
        _rule("t-high", weight="high"),
        _rule("t-low", weight="low"),
        _rule("f1-rule", "fleet", fleet_id="f1"),
        _rule("f2-rule", "fleet", fleet_id="f2", weight="high"),
        _rule("a1-in-f1", "agent", fleet_id="f1", agent_id="a1"),
        _rule("a2-in-f1", "agent", fleet_id="f1", agent_id="a2", weight="low"),
        _rule("a1-in-f2", "agent", fleet_id="f2", agent_id="a1"),
    ):
        await _set(client, tenant, rule)

    # Heaviest first, then newest: each rule above was written after the last.
    for asked, expected in (
        ({}, ["t-high", "t-low"]),
        ({"fleet_id": "f1"}, ["t-high", "f1-rule", "t-low"]),
        ({"fleet_id": "f1", "agent_id": "a1"}, ["t-high", "a1-in-f1", "f1-rule", "t-low"]),
        ({"fleet_id": "f1", "agent_id": "a2"}, ["t-high", "f1-rule", "a2-in-f1", "t-low"]),
        ({"fleet_id": "f2", "agent_id": "a1"}, ["f2-rule", "t-high", "a1-in-f2", "t-low"]),
        ({"fleet_id": "f3", "agent_id": "a1"}, ["t-high", "t-low"]),
    ):
        resp = await _get(client, "", tenant, **asked)
        assert [r["doc_id"] for r in resp.json()] == expected, asked
        await _assert_latest_matches_the_list(client, tenant, **asked)


async def test_rows_no_keystone_write_makes_resolve_as_the_list_resolves_them(
    client: AsyncClient,
) -> None:
    """One SQL resolves both sets, so they agree even on a row the API never
    writes: here a weight stored as text, which the list ranks by its value."""
    tenant = _tenant()
    as_text = {"title": "T", "content": "C", "scope": "tenant", "weight": "75"}
    await _insert_raw(tenant, ("as-text", None, as_text))
    await _set(client, tenant, _rule("high", weight="high"))
    await _set(client, tenant, _rule("med"))
    resp = await _get(client, "", tenant)
    assert [r["doc_id"] for r in resp.json()] == ["high", "as-text", "med"]
    await _assert_latest_matches_the_list(client, tenant)


async def test_the_cap_keeps_the_same_rules_in_a_version_as_in_the_list(client: AsyncClient) -> None:
    """Past 50 rules the list drops some, and a version must drop the same
    ones. Ties on weight and time fall to ``doc_id`` in byte order, which no
    collation update can reorder. A linguistic collation such as en_US ignores
    the punctuation and puts ``r16a`` before ``r16-b``, so which tied rules the
    cap kept could change under a version with nothing written."""
    tenant = _tenant()
    tied = [f"r{i:02d}{tail}" for i in range(18) for tail in ("-b", ".c", "a")]  # 54 rules
    # Heavier rules among them in doc_id order, so no sort finds its input
    # already in order and keeps the ties where they lie.
    heavy = [f"r{i:02d}z" for i in range(0, 18, 3)]
    await _insert_raw(
        tenant,
        *((d, None, {"title": d, "content": d, "scope": "tenant", "weight": 50}) for d in tied),
        *((d, None, {"title": d, "content": d, "scope": "tenant", "weight": 100}) for d in heavy),
    )
    await _set(client, tenant, _rule("newest"))  # the write that records the version
    resp = await _get(client, "", tenant)
    assert [r["doc_id"] for r in resp.json()] == [*heavy, "newest", *sorted(tied)[:43]]
    await _assert_latest_matches_the_list(client, tenant)
    [latest] = (await _get(client, "/versions", tenant, limit=1)).json()["items"]
    assert latest["truncated"]


async def test_concurrent_writes_take_consecutive_versions(client: AsyncClient) -> None:
    """Writes to one tenant queue on its lock: no number is taken twice, none
    is skipped, and each version adds its rule to the one before."""
    tenant = _tenant()
    await asyncio.gather(*(_set(client, tenant, _rule(f"k{i:02d}")) for i in range(16)))

    page = (await _get(client, "/versions", tenant, limit=100)).json()
    assert [v["version"] for v in page["items"]] == list(range(16, 0, -1))
    previous: set[str] = set()
    for n in range(1, 17):
        rules = {r["doc_id"] for r in (await _get(client, f"/versions/{n}", tenant)).json()["items"]}
        assert len(rules) == n and previous < rules
        previous = rules


async def test_versions_page_newest_first(client: AsyncClient) -> None:
    tenant = _tenant()
    for i in range(5):
        await _set(client, tenant, _rule(f"k{i}"))
    first = (await _get(client, "/versions", tenant, limit=2)).json()
    assert ([v["version"] for v in first["items"]], first["next_before"]) == ([5, 4], 4)
    second = (await _get(client, "/versions", tenant, limit=2, before=first["next_before"])).json()
    assert ([v["version"] for v in second["items"]], second["next_before"]) == ([3, 2], 2)
    last = (await _get(client, "/versions", tenant, limit=2, before=second["next_before"])).json()
    assert ([v["version"] for v in last["items"]], last["next_before"]) == ([1], None)


async def test_bad_reads_and_writes_are_refused(client: AsyncClient) -> None:
    tenant = _tenant()
    await _set(client, tenant, _rule("a"))
    resp = await client.get(f"{PREFIX}/keystones/versions/9", params={"tenant_id": tenant})
    assert resp.status_code == 404
    resp = await client.get(f"{PREFIX}/keystones/versions", params={"tenant_id": tenant, "agent_id": "a1"})
    assert resp.status_code == 422  # an agent's rules are keyed on its fleet
    # ``version`` is an int4: a number outside it is refused, not sent to Postgres.
    for path, params, status in (
        ("/versions/0", {}, 422),
        (f"/versions/{2**31}", {}, 422),
        ("/versions", {"before": 2**31}, 422),
        (f"/versions/{2**31 - 1}", {}, 404),
        ("/versions", {"before": 2**31 - 1}, 200),
    ):
        resp = await client.get(f"{PREFIX}/keystones{path}", params={"tenant_id": tenant, **params})
        assert resp.status_code == status, (path, params, resp.text)
    resp = await client.post(
        f"{PREFIX}/keystones", json={"tenant_id": tenant, **_rule("b"), "actor_agent_id": 7}
    )
    assert resp.status_code == 422
    resp = await client.delete(f"{PREFIX}/keystones/a", params={"tenant_id": tenant, "actor_user_id": ""})
    assert resp.status_code == 422
    # Neither refused write moved anything.
    assert [v["version"] for v in (await _get(client, "/versions", tenant)).json()["items"]] == [1]


async def test_a_change_made_around_versioning_is_its_own_version(client: AsyncClient) -> None:
    """A fleet purge deletes the fleet's keystones without a keystone write.
    The next write records that first, as a ``resync`` with no rule or actor,
    so its own version shows its own change, not the purge under its name."""
    tenant = _tenant()
    await _set(client, tenant, _rule("everyone"))
    await _set(client, tenant, _rule("fleet-rule", "fleet", fleet_id="f1"))
    await PostgresService().purge_fleet_data(tenant, "f1")
    await _set(client, tenant, _rule("next"), actor_agent_id="writer")

    page = (await _get(client, "/versions", tenant)).json()
    assert [(v["version"], v["op"], v["doc_id"], v["actor_agent_id"]) for v in page["items"]] == [
        (4, "set", "next", "writer"),
        (3, "resync", None, None),
        (2, "set", "fleet-rule", None),
        (1, "set", "everyone", None),
    ]
    resynced = (await _get(client, "/versions/3", tenant, fleet_id="f1")).json()
    assert [r["doc_id"] for r in resynced["items"]] == ["everyone"]


async def test_the_baseline_versions_the_keystones_already_there(client: AsyncClient) -> None:
    """Migration 061 gives a tenant that already had keystones a version 1 of
    them, in the shape a write records, and runs again without adding one."""
    baseline_sql = load_migration("061_keystone_versions.py").BASELINE_SQL
    tenant, versioned = _tenant(), _tenant()
    await _set(client, versioned, _rule("already"))
    await _insert_raw(
        tenant,
        # Byte order and en_US order these two ids differently, so a snapshot
        # the write path sorted any other way than the baseline would show.
        ("old-tenant-rule", None, {"title": "T", "content": "C", "scope": "tenant", "weight": 100}),
        ("old.fleet.rule", "f1", {"title": "F", "content": "D", "scope": "fleet", "weight": 25}),
        updated_at=datetime(2026, 10, 6, 9, 30, 0, 500000, tzinfo=timezone(timedelta(hours=2))),
    )
    async with get_session() as session:
        # Away from UTC, so a time written in the session's zone would show.
        await session.execute(text("SET LOCAL TIME ZONE 'Asia/Jerusalem'"))
        await session.execute(text(baseline_sql))
        await session.execute(text(baseline_sql))

    [baseline] = (await _get(client, "/versions", tenant)).json()["items"]
    assert (baseline["version"], baseline["op"], baseline["doc_id"], baseline["actor_agent_id"]) == (
        1,
        "baseline",
        None,
        None,
    )
    assert [v["version"] for v in (await _get(client, "/versions", versioned)).json()["items"]] == [1]
    [kept] = (await _get(client, "/versions/1", tenant)).json()["items"]
    assert kept["updated_at"] == "2026-10-06T07:30:00.500000Z"

    # The migration's SQL and the write path build one snapshot, to the byte:
    # the next write finds nothing changed around it, so no resync before it.
    await _set(client, tenant, _rule("new-rule"))
    page = (await _get(client, "/versions", tenant)).json()
    assert [(v["version"], v["op"]) for v in page["items"]] == [(2, "set"), (1, "baseline")]


async def test_a_keystone_that_jsonb_cannot_hold_still_versions(client: AsyncClient) -> None:
    """``documents.data`` is ``json`` and keeps a ``\\u0000`` escape, which
    JSONB refuses. A JSONB snapshot would fail migration 061 for every tenant
    and then every write to this one."""
    tenant = _tenant()
    nul = {"title": "T", "content": "before\u0000after", "scope": "tenant", "weight": 50}
    await _insert_raw(tenant, ("nul", None, nul))
    async with get_session() as session:
        await session.execute(text(load_migration("061_keystone_versions.py").BASELINE_SQL))
    await _set(client, tenant, _rule("next"))
    resp = await client.delete(f"{PREFIX}/keystones/next", params={"tenant_id": tenant})
    assert resp.status_code == 200, resp.text

    async with get_session() as session:
        versions = (
            await session.execute(
                text("SELECT version, op FROM keystone_versions WHERE tenant_id = :t ORDER BY version"),
                {"t": tenant},
            )
        ).all()
    assert [tuple(v) for v in versions] == [(1, "baseline"), (2, "set"), (3, "delete")]


async def test_a_write_whose_version_fails_is_not_made(monkeypatch) -> None:
    """The keystone write and its version commit together or not at all, so
    no change can go unversioned."""
    tenant = _tenant()
    rule = {"title": "T", "content": "C", "scope": "tenant", "weight": 50}
    await keystones.set_keystone(
        tenant_id=tenant, doc_id="kept", data=rule, fleet_id=None, actor_agent_id=None, actor_user_id=None
    )

    async def unrecordable(*args, **kwargs):
        raise RuntimeError("the version could not be recorded")

    monkeypatch.setattr(keystones, "_record_version", unrecordable)
    with pytest.raises(RuntimeError):
        await keystones.set_keystone(
            tenant_id=tenant,
            doc_id="added",
            data=rule,
            fleet_id=None,
            actor_agent_id=None,
            actor_user_id=None,
        )
    with pytest.raises(RuntimeError):
        await keystones.remove_keystone(
            tenant_id=tenant, doc_id="kept", actor_agent_id=None, actor_user_id=None
        )

    docs, _ = await keystones.list_keystones(tenant_id=tenant)
    assert [d.doc_id for d in docs] == ["kept"]
