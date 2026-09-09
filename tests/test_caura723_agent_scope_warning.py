"""CAURA-723 — an agent filter that matches nothing says so.

Before this, a tenant-scoped caller passing a wrong ``filter_agent_id`` got
``HTTP 200 · items [] · warnings null`` — byte-identical to a correct id that
simply had nothing relevant. The filter is a SQL ``WHERE memories.agent_id =
?``, so a typo matches no rows and the query returns nothing exactly as an
empty query would.

The cases below are the four the probe has to separate, and the third is the one
that makes the naive implementation wrong: ``agent_delete`` removes the agents
row and leaves every memory live, so "not in ``agents``" must NOT be allowed to
skip the search.
"""

import pytest

from core_api.config import settings
from core_api.services import memory_service
from core_api.services.agent_scope import (
    FILTER_AGENT_DEREGISTERED,
    FILTER_AGENT_EMPTY,
    FILTER_AGENT_UNKNOWN,
)
from tests._legacy_contracts import LEGACY_API_KEY_FIELD
from tests.conftest import get_test_auth, new_tenant_id

REAL = "agent-real"
TYPO = "agnet-real"
CONTENT = "Rome sits on seven hills."
AGENT_KEY = "caura723-agent-key"


@pytest.fixture
def pipeline_search(monkeypatch):
    monkeypatch.setattr(memory_service, "_USE_PIPELINE_SEARCH", True)


@pytest.fixture
def tenant_scoped(monkeypatch):
    """Auth Path 2 with no ``X-Agent-ID`` — a tenant key acting for any agent.

    Distinct from ``get_test_auth``'s ADMIN key in the one way that matters
    here: ``auth.tenant_id`` is set, so the route runs ``get_or_create_agent``
    and the ``registration_ctx`` reports whether the agent pre-existed. Admin
    skips that block (``if auth.tenant_id:``), which is why an admin caller
    cannot be told an agent is deregistered on a NON-empty result — there is no
    free pre-existence signal, and buying one would put a query back on the
    success path. This is also the credential shape the feature exists for.
    """
    # Via the shared constant rather than the literal: #1436-#1438 are actively
    # removing incidental legacy-name lines, and this is the field the aliases
    # collapse onto at validation time — so it is the only spelling that has
    # any effect on auth Path 2, and the only place naming it should be
    # ``_legacy_contracts``.
    monkeypatch.setattr(settings, LEGACY_API_KEY_FIELD, AGENT_KEY, raising=False)
    monkeypatch.setattr(settings, "is_standalone", False, raising=False)

    def headers_for(tenant_id):
        return {"X-API-Key": AGENT_KEY, "X-Tenant-ID": tenant_id}

    return headers_for


async def _write(client, headers, tenant_id, agent_id, content=CONTENT):
    r = await client.post(
        "/api/v1/memories",
        headers=headers,
        json={"tenant_id": tenant_id, "agent_id": agent_id, "content": content},
    )
    assert r.status_code == 201, r.text
    return r.json()["id"]


async def _search(client, headers, tenant_id, filter_agent_id=None, **extra):
    body = {"tenant_id": tenant_id, "query": "Rome seven hills", "top_k": 5}
    body.update(extra)
    if filter_agent_id is not None:
        body["filter_agent_id"] = filter_agent_id
    return await client.post("/api/v1/search", headers=headers, json=body)


def _codes(body) -> list[str]:
    return [w["code"] for w in (body.get("warnings") or [])]


# ---------------------------------------------------------------------------
# The four states
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_an_unknown_agent_id_is_named_as_the_cause(client, pipeline_search):
    """The typo case — the one that cost a 589-query benchmark run."""
    tenant_id, headers = get_test_auth(new_tenant_id())
    await _write(client, headers, tenant_id, REAL)

    resp = await _search(client, headers, tenant_id, TYPO)
    body = resp.json()

    assert resp.status_code == 200, resp.text
    assert body["items"] == []
    assert FILTER_AGENT_UNKNOWN in _codes(body)
    # The offending id rides in ``details`` so a client can act on it without
    # parsing prose.
    assert body["warnings"][0]["details"]["filter_agent_id"] == TYPO


@pytest.mark.integration
async def test_a_registered_agent_with_no_memories_is_distinguished(
    client, pipeline_search
):
    """A brand-new user is NOT a misconfiguration, and must not be told it is."""
    from core_api.services.agent_service import get_or_create_agent

    tenant_id, headers = get_test_auth(new_tenant_id())
    await _write(client, headers, tenant_id, REAL)

    # Register the agent without writing anything as it — the same call the
    # write and read paths make, so the row is exactly what production creates.
    # There is no POST /agents in core-api; registration is always implicit.
    await get_or_create_agent(tenant_id, "agent-fresh", None)

    body = (await _search(client, headers, tenant_id, "agent-fresh")).json()
    assert body["items"] == []
    assert FILTER_AGENT_EMPTY in _codes(body), (
        "a registered agent with no memories must not be reported as unknown"
    )


@pytest.mark.integration
async def test_a_deregistered_agent_still_returns_its_memories(
    client, pipeline_search, tenant_scoped
):
    """The case that makes an ``agents``-only check wrong.

    ``DELETE /agents/{id}`` states it outright — "Delete an agent. Memories
    written by this agent are NOT deleted." So the memories stay live and
    searchable while the agent row is gone. Skipping the search on "not in
    agents" would throw away real results, which is why ``has_memories``
    decides the skip and ``agent_registered`` only picks the wording.

    Deleted through the route rather than raw SQL on the ``db`` fixture: that
    fixture runs in ``join_transaction_mode="create_savepoint"`` and rolls back,
    so its ``commit()`` is invisible to the storage service's own connection and
    the probe would still see the agent row.
    """
    tenant_id, headers = get_test_auth(new_tenant_id())
    mem_id = await _write(client, headers, tenant_id, REAL)

    dropped = await client.delete(
        f"/api/v1/agents/{REAL}", headers=headers, params={"tenant_id": tenant_id}
    )
    assert dropped.status_code == 204, dropped.text

    # Searched as a tenant-scoped caller: that is the shape the warning serves,
    # and the only one with a free pre-existence signal (see ``tenant_scoped``).
    resp = await _search(client, tenant_scoped(tenant_id), tenant_id, REAL)
    body = resp.json()

    assert resp.status_code == 200, resp.text
    assert [i["id"] for i in body["items"]] == [mem_id], (
        "the search MUST still run — the memories outlive the agent row"
    )
    assert FILTER_AGENT_DEREGISTERED in _codes(body)


@pytest.mark.integration
async def test_a_good_filter_warns_about_nothing(client, pipeline_search):
    tenant_id, headers = get_test_auth(new_tenant_id())
    await _write(client, headers, tenant_id, REAL)

    body = (await _search(client, headers, tenant_id, REAL)).json()
    assert len(body["items"]) == 1
    assert body.get("warnings") is None


@pytest.mark.integration
async def test_no_filter_at_all_warns_about_nothing(client, pipeline_search):
    """The probe must not fire on a tenant-wide search."""
    tenant_id, headers = get_test_auth(new_tenant_id())
    await _write(client, headers, tenant_id, REAL)

    body = (await _search(client, headers, tenant_id)).json()
    assert len(body["items"]) == 1
    assert body.get("warnings") is None


# ---------------------------------------------------------------------------
# The gate: authenticated identity is already protected, so stay off it
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_an_agent_scoped_credential_still_gets_its_403(
    client, pipeline_search, tenant_scoped
):
    """Unchanged behaviour, asserted so the probe can't be seen to soften it.

    An agent-scoped credential naming any id but its own is refused by
    ``enforce_self_agent`` before the probe is reached. Agent identity arrives
    only via ``X-Agent-ID`` on auth Path 2 — Paths 1 (admin) and 3 (standalone)
    deliberately never plumb it, which is why this builds Path 2 rather than
    adding the header to ``get_test_auth``'s admin headers.
    """
    tenant_id, admin_headers = get_test_auth(new_tenant_id())
    await _write(client, admin_headers, tenant_id, REAL)

    agent_headers = {**tenant_scoped(tenant_id), "X-Agent-ID": REAL}

    refused = await _search(client, agent_headers, tenant_id, TYPO)
    assert refused.status_code == 403, refused.text

    allowed = await _search(client, agent_headers, tenant_id, REAL)
    assert allowed.status_code == 200, allowed.text
    assert len(allowed.json()["items"]) == 1
    assert allowed.json().get("warnings") is None


# ---------------------------------------------------------------------------
# Scoping: the probe must mirror the search's own predicates
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_a_fleet_narrowed_request_probes_the_same_fleet(client, pipeline_search):
    """An agent with memories in fleet-a, searched under fleet-b.

    The probe must not report "has memories" for rows outside the requested
    fleets, or it would run a search that cannot match and explain nothing.
    """
    tenant_id, headers = get_test_auth(new_tenant_id())
    r = await client.post(
        "/api/v1/memories",
        headers=headers,
        json={
            "tenant_id": tenant_id,
            "agent_id": REAL,
            "fleet_id": "fleet-a",
            "content": CONTENT,
        },
    )
    assert r.status_code == 201, r.text

    body = (
        await _search(client, headers, tenant_id, REAL, fleet_ids=["fleet-b"])
    ).json()
    assert body["items"] == []
    assert FILTER_AGENT_EMPTY in _codes(body) or FILTER_AGENT_UNKNOWN in _codes(body), (
        "an out-of-scope fleet must be reported, not returned as a bare empty list"
    )


# ---------------------------------------------------------------------------
# /recall carries it too
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_recall_reports_the_same_cause(client, pipeline_search):
    """``/recall`` parses the same request and had the identical silent empty.

    Leaving it out is how the two routes came to disagree about what these
    fields mean (the reason ``_resolve_read_identity`` is shared at all).
    """
    tenant_id, headers = get_test_auth(new_tenant_id())
    await _write(client, headers, tenant_id, REAL)

    resp = await client.post(
        "/api/v1/recall",
        headers=headers,
        json={
            "tenant_id": tenant_id,
            "query": "Rome seven hills",
            "filter_agent_id": TYPO,
            "top_k": 5,
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["memory_count"] == 0
    assert body["items"] == []
    assert FILTER_AGENT_UNKNOWN in _codes(body)


# ---------------------------------------------------------------------------
# Failure of the probe must not break search
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_a_failing_probe_leaves_search_working(
    client, monkeypatch, pipeline_search
):
    """Degrade to today's behaviour — an unexplained result, not a 500.

    A diagnostic hint must never be able to fail a working search.
    """
    from core_api.services import agent_scope

    tenant_id, headers = get_test_auth(new_tenant_id())
    await _write(client, headers, tenant_id, REAL)

    class _Boom:
        async def agent_scope_probe(self, data):
            raise RuntimeError("storage down")

    monkeypatch.setattr(agent_scope, "get_storage_client", lambda: _Boom())

    resp = await _search(client, headers, tenant_id, REAL)
    assert resp.status_code == 200, resp.text
    assert len(resp.json()["items"]) == 1
    assert resp.json().get("warnings") is None


# ---------------------------------------------------------------------------
# The cost contract (PR #1434 review)
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_a_successful_search_makes_no_probe_call(
    client, monkeypatch, pipeline_search, tenant_scoped
):
    """The finding this design answers: no cost on the healthy path.

    The first draft probed BEFORE the search, so every agent-filtered request
    paid a round trip and two queries to buy a saving that only lands on
    requests already broken. Now the probe fires only on an empty result — and
    the deregistered case is answered from ``get_or_create_agent``'s
    ``registration_ctx``, so it costs nothing either.

    Asserted by counting calls rather than by reading the code, because this is
    a property of the call graph and a future edit could reintroduce the
    round trip without touching anything this file names.
    """
    from core_api.services import agent_scope

    calls: list[dict] = []
    inner = agent_scope.get_storage_client()

    class _Counting:
        def __getattr__(self, name):
            return getattr(inner, name)

        async def agent_scope_probe(self, data):
            calls.append(data)
            return await inner.agent_scope_probe(data)

    monkeypatch.setattr(agent_scope, "get_storage_client", lambda: _Counting())

    tenant_id, headers = get_test_auth(new_tenant_id())
    await _write(client, headers, tenant_id, REAL)
    scoped = tenant_scoped(tenant_id)

    # 1. Results came back — nothing to explain, nothing to ask.
    hit = await _search(client, scoped, tenant_id, REAL)
    assert len(hit.json()["items"]) == 1
    assert calls == [], f"a successful agent-filtered search probed storage: {calls}"

    # 2. No filter at all — the probe is not even a candidate.
    await _search(client, scoped, tenant_id)
    assert calls == [], "a tenant-wide search probed storage"

    # 3. Empty result — now, and only now, one probe.
    miss = await _search(client, scoped, tenant_id, TYPO)
    assert miss.json()["items"] == []
    assert len(calls) == 1, (
        f"expected exactly one probe on the empty path, got {len(calls)}"
    )
    # And it skips the redundant ``agents`` lookup, because the route already
    # learned the answer from ``get_or_create_agent``.
    assert calls[0]["include_agent_registered"] is False, (
        "the route knew whether the agent pre-existed; the probe must not ask again"
    )


# ---------------------------------------------------------------------------
# Which field gets explained (PR #1434 review, round 2)
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_a_typod_filter_is_reported_even_beside_a_valid_caller_id(
    client, pipeline_search, tenant_scoped
):
    """The gap: keying on the resolved identity explained the wrong id.

    ``_resolve_read_identity`` resolves ``caller_agent_id or filter_agent_id``,
    so a valid ``caller_agent_id`` beside a typo'd ``filter_agent_id`` made
    ``eff_agent_id`` the VALID one — which has memories, so nothing was
    reported — while the typo was the thing actually restricting the SQL and
    emptying the result. Measured before the fix:

        filter=TYPO only           -> ['filter_agent_unknown']
        caller=REAL + filter=TYPO  -> []
    """
    tenant_id, headers = get_test_auth(new_tenant_id())
    await _write(client, headers, tenant_id, REAL)
    scoped = tenant_scoped(tenant_id)

    resp = await _search(client, scoped, tenant_id, TYPO, caller_agent_id=REAL)
    body = resp.json()

    assert resp.status_code == 200, resp.text
    assert body["items"] == []
    assert FILTER_AGENT_UNKNOWN in _codes(body), (
        "the row-restricting field must be explained, not the resolved identity"
    )
    detail = body["warnings"][0]["details"]
    assert detail["field"] == "filter_agent_id"
    assert detail["filter_agent_id"] == TYPO, (
        "the warning must name the typo, not the valid id"
    )


@pytest.mark.integration
async def test_a_caller_id_alone_is_reported_as_visibility_not_filtering(
    client, pipeline_search, tenant_scoped
):
    """``caller_agent_id`` restricts no rows, so its warning must not claim to.

    It only decides which ``scope_agent`` rows are visible. Reporting it as
    "the filter matched nothing" would name a cause that did not act, and
    ``details`` keyed on ``filter_agent_id`` would send a client to correct the
    wrong knob.
    """
    tenant_id, headers = get_test_auth(new_tenant_id())
    await _write(client, headers, tenant_id, REAL)
    scoped = tenant_scoped(tenant_id)

    resp = await _search(
        client, scoped, tenant_id, None, caller_agent_id=TYPO, query="zzz nothing"
    )
    body = resp.json()
    assert resp.status_code == 200, resp.text

    if _codes(body):
        detail = body["warnings"][0]["details"]
        assert detail["field"] == "caller_agent_id"
        assert "caller_agent_id" in detail
        assert "filter_agent_id" not in detail
        assert "matched nothing" not in body["warnings"][0]["message"], (
            "caller_agent_id filters no rows; the message must not say it did"
        )


@pytest.mark.integration
async def test_a_repeated_typo_still_warns_and_never_reads_as_benign(
    client, pipeline_search, tenant_scoped
):
    """The same wrong id twice, against a fresh tenant.

    The read path registers whatever id it is handed, so the FIRST request
    carrying a typo both reports it correctly and creates the row that makes
    the SECOND report differently. That downgrade is real and cannot be fixed
    from here — the root fix is to stop registering on reads (CAURA-724), which
    is security-adjacent because the route needs the row for trust-level fleet
    forcing and ``enforce_fleet_read_many``.

    What IS required, and is asserted here:

      * the second request still warns — it must never fall back to the silent
        empty this whole feature exists to remove;
      * its message does not read as a benign "new agent, nothing yet". A
        caller who reads "registered" as "the id is right" stops looking, which
        is exactly the outcome the warning is meant to prevent.

    Pinned as a test rather than left in a commit message because a future
    change to registration should make this visible, whichever way it moves it.
    """
    tenant_id, headers = get_test_auth(new_tenant_id())
    await _write(client, headers, tenant_id, REAL)
    scoped = tenant_scoped(tenant_id)

    first = (await _search(client, scoped, tenant_id, TYPO)).json()
    assert first["items"] == []
    assert FILTER_AGENT_UNKNOWN in _codes(first), (
        "first sighting must name it as unknown"
    )

    second = (await _search(client, scoped, tenant_id, TYPO)).json()
    assert second["items"] == []

    codes = _codes(second)
    assert codes, "a repeated typo must still be explained, not silently empty"
    assert codes[0] in (FILTER_AGENT_UNKNOWN, FILTER_AGENT_EMPTY)
    assert (
        second["warnings"][0]["details"][second["warnings"][0]["details"]["field"]]
        == TYPO
    ), "the warning must still name the offending id"

    if FILTER_AGENT_EMPTY in codes:
        # The downgrade happened (today's behaviour). Then the message has to
        # carry the ambiguity rather than presenting a registered id as fine.
        msg = second["warnings"][0]["message"]
        assert "repeated typo" in msg and "does not mean the id is correct" in msg, (
            f"a downgraded warning must say registration is weak evidence; got: {msg}"
        )
