"""oss-0922-m-02 — the classifier's entity FTS ignored ``strict_fleet_scoping``.

C27 (#1410) made null-fleet inheritance a per-tenant choice and wired the flag
into every fleet-scoped storage read it could find. It found two of the three
entity-FTS callers. ``ParallelEmbedAndEntityBoost._entity_boost_via_storage``
forwards it, the deprecated ``_entity_boost_pipeline`` forwards it, and
``ClassifyQuery._entity_fts`` built its payload as ``{tokens, tenant_id,
fleet_ids}`` and never sent it. The storage route defaults the flag to ``False``
when it is absent (``entities.py`` ``/fts-search``), so the omission did not
fail — it silently served the permissive predicate to a tenant that had asked
for the strict one.

WHAT THAT COSTS, precisely, because it is narrower than it first looks: the
memories a strict tenant gets back stay in scope either way, since
``_collect_memories`` was scoped correctly from the start. What leaks is the
ENTITY layer — ``entity_matches`` counts entities the tenant excluded, which can
trip the ``ENTITY_LOOKUP_MAX_MATCHES`` decline on entities that were never in
scope, and can seed ENTITY_LOOKUP (and the ``_classified_entity_hops`` stash the
boost step consumes) from them.

WHY THE TESTS BELOW GO THROUGH THE DATABASE AND THE SETTINGS ROUTE.
pm-0918-c-03 shipped a per-tenant switch that was UNSETTABLE: registered as a
``ResolvedConfig`` property but never added to ``DEFAULT_SETTINGS``, so
``_check_keys`` rejected every write and ``PUT /settings`` answered 422 while
the resolver cheerfully served the default. Every test it had built
``ResolvedConfig`` directly, which skips validation — which is exactly why it
read as shipped. The same shape of test would hide the same class of defect
here, so the behavioural test drives the real ``PUT /api/v1/settings``, seeds
real rows, and asserts on what the classifier actually matched. A mock
recording that ``_entity_fts`` was called with ``strict_fleet_scoping=True``
would have passed on the broken code the moment the argument existed, without
anything ever reaching the wire.
"""

from __future__ import annotations

import ast
import inspect
import uuid
from pathlib import Path

import pytest

from common.embedding import fake_embedding
from tests.conftest import get_test_auth

_FLEET = "fleet-strict-scope"


async def _seed_entity_and_memories(
    sc,
    tenant_id: str,
    entity_name: str,
    *,
    entity_fleet_id: str | None,
    count: int = 3,
) -> list[str]:
    """One entity (in ``entity_fleet_id``) linked to ``count`` in-fleet memories.

    The memories are ALWAYS written to ``_FLEET`` while the entity's fleet is the
    variable. That separation is the point: memory scoping was never broken, so
    leaving the memories in scope isolates the entity predicate as the only thing
    the assertions can be reacting to.
    """
    entity = await sc.create_entity(
        {
            "tenant_id": tenant_id,
            "entity_type": "concept",
            "canonical_name": entity_name,
            "fleet_id": entity_fleet_id,
        }
    )
    embedding = fake_embedding(entity_name)
    created = await sc.create_memories(
        [
            {
                "tenant_id": tenant_id,
                "fleet_id": _FLEET,
                "agent_id": "test-agent",
                "memory_type": "fact",
                "content": f"Memory {i} about {entity_name}",
                "embedding": embedding,
                "weight": 0.5,
                "content_hash": f"hash-{entity_name}-{i}",
                "status": "active",
                "client_request_id": str(uuid.uuid4()),
            }
            for i in range(count)
        ]
    )
    memory_ids = [row["id"] for row in created]
    assert all(memory_ids), "bulk insert did not return an id for every memory"
    await sc.bulk_upsert_entity_links(
        tenant_id,
        [
            {
                "input_idx": i,
                "memory_id": mid,
                "entity_id": entity["id"],
                "role": "subject",
            }
            for i, mid in enumerate(memory_ids)
        ],
    )
    return memory_ids


async def _entity_matches(client, headers, tenant_id: str, query: str) -> int | None:
    """``entity_matches`` as the classifier recorded it, via ``POST /search``.

    Through the ROUTE rather than through ``search_memories`` directly, and that
    is not incidental: ``_search_memories_pipeline`` takes ``tenant_config`` as a
    parameter defaulting to ``None`` and reads the switch off it with
    ``getattr(..., False)``. Call the service function without one and the
    tenant's setting is not merely unread — it cannot be reached, and a test
    written that way reports "permissive" for every tenant no matter what the
    settings row says. The route is where the config is resolved, so the route
    is the smallest caller that can observe this setting at all.

    ``diagnostic`` is what copies the count into the response; it also forces
    the pipeline path, which is the path under test. ``entity_matches`` is
    deliberately read with no ``or 0`` fallback — 0 (matched nothing) and None
    (FTS never ran, so the fixture is not testing what it claims) are different
    answers and the assertions below distinguish them.
    """
    resp = await client.post(
        "/api/v1/search",
        json={
            "tenant_id": tenant_id,
            "query": query,
            "fleet_ids": [_FLEET],
            "top_k": 5,
            "diagnostic": True,
        },
        headers=headers,
    )
    assert resp.status_code == 200, resp.text
    return resp.json()["diagnostic"]["entity_matches"]


# ── the defect, end to end ───────────────────────────────────────────────


@pytest.mark.integration
async def test_a_strict_tenant_stops_matching_null_fleet_entities(client, sc):
    """The regression test. Same rows, same query, same fleet — only the setting
    moves, and it is written through the route a tenant would use.

    Before the fix both halves answer 1: the permissive branch of the fleet
    predicate ran regardless of what the tenant had asked for, because the flag
    never left core-api. The control half is not decoration — it is what proves
    the strict half's 0 means "excluded" rather than "the fixture never
    matched", which is the way a scoping test most easily passes for the wrong
    reason.
    """
    tenant_id, headers = get_test_auth(tenant_id=f"test-tenant-{uuid.uuid4().hex[:8]}")
    entity_name = f"zorblat{uuid.uuid4().hex[:8]}"
    await _seed_entity_and_memories(sc, tenant_id, entity_name, entity_fleet_id=None)

    # CONTROL, and it runs first on purpose: the default is permissive (contract
    # D4 — a null fleet is tenant-shared BY DESIGN), so a tenant that has not
    # opted in must still see the entity. If this is what fails, the fix has
    # broken the default rather than honoured the switch.
    assert await _entity_matches(client, headers, tenant_id, entity_name) == 1, (
        "the tenant-shared entity did not match with permissive (default) "
        "scoping — the fixture never exercised the predicate"
    )

    # THE WRITE, through the real route. A knob registered only as a resolver
    # property answers 422 here while reading as shipped everywhere else
    # (pm-0918-c-03), so this assertion has to be the loud one.
    resp = await client.put(
        f"/api/v1/settings?tenant_id={tenant_id}",
        json={"search": {"strict_fleet_scoping": True}},
        headers=headers,
    )
    assert resp.status_code == 200, f"PUT failed — the knob is unsettable: {resp.text}"

    assert await _entity_matches(client, headers, tenant_id, entity_name) == 0, (
        "a strict tenant still matched a null-fleet entity: the classifier's "
        "entity FTS is not forwarding strict_fleet_scoping to storage"
    )


@pytest.mark.integration
async def test_strict_scoping_keeps_the_tenants_own_fleet_entities(client, sc):
    """The other half of the switch, and the one a careless fix breaks.

    Dropping the flag on the floor and dropping ``fleet_ids`` on the floor look
    identical from the permissive side — both leave everything visible. They
    diverge here: strict mode narrows the predicate to the named fleets, it does
    not empty it.
    """
    tenant_id, headers = get_test_auth(tenant_id=f"test-tenant-{uuid.uuid4().hex[:8]}")
    entity_name = f"zorblat{uuid.uuid4().hex[:8]}"
    await _seed_entity_and_memories(sc, tenant_id, entity_name, entity_fleet_id=_FLEET)

    resp = await client.put(
        f"/api/v1/settings?tenant_id={tenant_id}",
        json={"search": {"strict_fleet_scoping": True}},
        headers=headers,
    )
    assert resp.status_code == 200, resp.text

    assert await _entity_matches(client, headers, tenant_id, entity_name) == 1, (
        "strict scoping hid an entity that lives in the searched fleet"
    )


@pytest.mark.integration
async def test_the_setting_survives_a_real_settings_put(client):
    """The registration guard, kept separate from the behavioural tests above.

    Those two would also fail if the key stopped being writable, but they would
    fail on a search result several steps downstream. This one fails on the
    write, naming the cause.
    """
    tenant_id, headers = get_test_auth(tenant_id=f"test-tenant-{uuid.uuid4().hex[:8]}")

    resp = await client.put(
        f"/api/v1/settings?tenant_id={tenant_id}",
        json={"search": {"strict_fleet_scoping": True}},
        headers=headers,
    )
    assert resp.status_code == 200, f"PUT failed — the knob is unsettable: {resp.text}"

    reloaded = await client.get(
        f"/api/v1/settings?tenant_id={tenant_id}", headers=headers
    )
    assert reloaded.status_code == 200, reloaded.text
    assert reloaded.json()["search"]["strict_fleet_scoping"] is True, (
        "the write was accepted but did not persist"
    )

    from core_api.services.organization_settings import resolve_config

    config = await resolve_config(tenant_id)
    assert config.strict_fleet_scoping is True


# ── the A54 lesson: no entity-FTS caller outside the switch ──────────────


@pytest.mark.unit
def test_every_entity_fts_caller_forwards_the_scope_flag():
    """Fails on the NEXT caller that forgets, instead of on the leak it causes.

    This defect was invisible for the same reason A54's was: the omission is a
    key that is not there, and the route's ``body.get(..., False)`` turns it
    into a working request with the wrong predicate. There is no exception, no
    log line, and no contract movement for oasdiff to see — so the only place it
    can be caught cheaply is here, at the call sites.

    Read from the AST rather than by grepping the file, so a caller that builds
    its payload under a different local name is still counted.
    """
    src_root = Path(__file__).resolve().parents[1] / "core-api" / "src"
    callers: dict[str, bool] = {}

    for path in src_root.rglob("*.py"):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            body_src = ast.unparse(node)
            # The client method's own definition is not a call site.
            if (
                "fts_search_entities(" not in body_src
                or node.name == "fts_search_entities"
            ):
                continue
            key = f"{path.relative_to(src_root)}::{node.name}"
            callers[key] = "strict_fleet_scoping" in body_src

    assert len(callers) >= 3, (
        f"expected at least the three known entity-FTS callers, found {sorted(callers)} "
        "— if a caller moved, follow it here rather than lowering this floor"
    )
    missing = sorted(k for k, ok in callers.items() if not ok)
    assert not missing, (
        f"entity-FTS caller(s) that never mention strict_fleet_scoping: {missing}. "
        "The /fts-search route defaults the flag to False when absent, so an "
        "omission here is not an error — it is a strict tenant silently served "
        "the permissive fleet predicate (oss-0922-m-02)."
    )


@pytest.mark.unit
def test_the_classifier_sends_the_flag_only_alongside_fleet_ids():
    """Documents the shape, because the alternative is defensible and this one
    was chosen: ``entity_fts_search`` applies the fleet predicate only when
    fleets were named, so sending the flag without ``fleet_ids`` would be inert
    at the SQL and misleading in a request log. Matches the sibling caller.
    """
    from core_api.pipeline.steps.search.classify_query import ClassifyQuery

    src = inspect.getsource(ClassifyQuery._entity_fts)
    fleet_branch = src.split("if fleet_ids:")[1]
    assert "strict_fleet_scoping" in fleet_branch, (
        "the flag moved outside the fleet_ids branch — if that is deliberate, "
        "say why here; storage ignores it without fleets"
    )
