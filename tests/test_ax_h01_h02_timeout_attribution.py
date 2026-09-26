"""ax-0917-h-01 / h-02 — does the 45s 504 say WHICH layer ate the budget?

#1633 gave ``RequestTimeoutMiddleware`` the canonical envelope
(``REQUEST_BUDGET_EXCEEDED`` plus ``budget_seconds`` / ``elapsed_seconds`` /
``path``), a structured log line and ``Retry-After``. Both rows then closed on
the plan "the next occurrence will say which layer ate the budget" — so that
plan's premise is what these tests check, before an occurrence depends on it.

They drive the REAL app stack: the real ``/api/v1/search`` route, the real
pipeline, the real middleware order, with only the budget lowered so it fires
in under a second. One hop is stalled at a time, as deep as the hop goes —
the embedding provider call, the per-tenant embed slot, the storage service's
own query method — and the evidence the two produce is then COMPARED. The
question is not "does the middleware emit an envelope" (``test_request_timeout``
pins that against a synthetic app) but "given the evidence, can an operator
name the layer".

Before ``core_api.request_phase``, the answer was no: a stalled embedding
provider and a stalled storage read returned byte-identical bodies and
byte-identical log records. ``test_the_504_names_the_layer_that_ate_the_budget``
is the test that failed.

No provider calls and no LLM calls — every stalled hop is patched.
"""

from __future__ import annotations

import asyncio
import logging

import pytest

from core_api.middleware.request_timeout import RequestTimeoutMiddleware
from tests.conftest import get_test_auth

pytestmark = pytest.mark.integration

_BUDGET_S = 0.4
# Far past the budget: a stall that could finish would make the test a race.
_STALL_S = 30.0
_FAKE_VECTOR = [0.0] * 8


def _timeout_middleware():
    """The live ``RequestTimeoutMiddleware`` instance inside the built stack.

    The budget is captured at ``add_middleware`` time from
    ``app_settings.request_timeout_seconds``, so patching the settings object
    does nothing once the stack exists. Reaching the instance is what lets
    these tests exercise the production path at test speed rather than
    re-assembling an app that resembles it.
    """
    from core_api.app import app

    if app.middleware_stack is None:
        app.middleware_stack = app.build_middleware_stack()
    node = app.middleware_stack
    while node is not None:
        if isinstance(node, RequestTimeoutMiddleware):
            return node
        node = getattr(node, "app", None)
    raise AssertionError(
        "RequestTimeoutMiddleware is not in the app's middleware stack"
    )


@pytest.fixture
def tight_budget(monkeypatch):
    monkeypatch.setattr(_timeout_middleware(), "timeout_seconds", _BUDGET_S)


# Released in teardown so a stalled hop does not outlive its test.
# ``storage_client._cancel_safe`` SHIELDS the in-flight request on purpose (a
# cancelled httpx call strands its pooled connection — incident 2026-06-16), so
# cancelling the caller does not stop the stall; under the session-scoped test
# loop it would keep sleeping through later tests and be collected mid-flight.
_release: asyncio.Event | None = None


@pytest.fixture(autouse=True)
def stall_gate():
    global _release
    _release = asyncio.Event()
    yield
    _release.set()
    _release = None


async def _stall_forever(*_a, **_kw):
    gate = _release
    if gate is None:
        await asyncio.sleep(_STALL_S)
    else:
        # Past the budget by two orders of magnitude, so the request is
        # cancelled long before this returns; the wait only ends at teardown.
        await asyncio.wait_for(gate.wait(), _STALL_S)
    # Whatever awaited this is gone by now — a shielded task draining at
    # teardown must unwind quietly, not raise into a done-callback.
    return []


async def _ready(value):
    return value


def _stall_embedding_provider(monkeypatch):
    """Hang the SHARED query-embedding hop — the h-02 shape.

    ``memory_service.get_query_embedding`` is the provider call both
    ``/search`` and ``/recall`` funnel into and the one CRUD never touches,
    which is why "both semantic endpoints down, CRUD healthy" points here.
    """
    monkeypatch.setattr(
        "core_api.services.memory_service.get_query_embedding", _stall_forever
    )


def _stall_storage_query(monkeypatch):
    """Embedding resolves instantly; the storage read hangs instead.

    Patched at ``postgres_service.memory_scored_search`` — past the storage
    client, past its retry policy, past the storage app's own router — so the
    request really does traverse every layer between core-api and the query.
    """
    monkeypatch.setattr(
        "core_api.pipeline.steps.search.parallel_embed_entity_boost._get_or_cache_embedding",
        lambda *_a, **_kw: _ready(_FAKE_VECTOR),
    )
    from core_storage_api.services.postgres_service import PostgresService

    monkeypatch.setattr(PostgresService, "memory_scored_search", _stall_forever)


class _NeverAcquires:
    """A semaphore nobody ever gets into."""

    def locked(self) -> bool:
        return True

    async def acquire(self) -> bool:
        await _stall_forever()
        return True

    def release(self) -> None:  # pragma: no cover - never reached
        pass


def _stall_storage_bulkhead(monkeypatch):
    """Saturate the storage-search bulkhead so the QUEUE is what blocks.

    ``per_tenant_storage_slot`` is the one wait on this path that is
    deliberately unbounded — its own docstring names the request budget as the
    only thing capping it — so it is the hop most able to eat 45s, and it had
    no INFO-level signal at all when it did. A queue with no free slots and a
    slow storage backend look identical end to end and are fixed differently
    (capacity vs the backend).
    """
    monkeypatch.setattr(
        "core_api.pipeline.steps.search.parallel_embed_entity_boost._get_or_cache_embedding",
        lambda *_a, **_kw: _ready(_FAKE_VECTOR),
    )
    import core_api.middleware.per_tenant_concurrency as ptc

    real = ptc._get_semaphore
    monkeypatch.setattr(
        ptc,
        "_get_semaphore",
        lambda scope, tenant_id: (
            _NeverAcquires() if scope == "storage_search" else real(scope, tenant_id)
        ),
    )


async def _timed_out_search(client) -> tuple[int, dict, dict]:
    tenant_id, headers = get_test_auth()
    resp = await client.post(
        "/api/v1/search",
        json={"tenant_id": tenant_id, "query": "who runs fleet ops"},
        headers=headers,
    )
    return resp.status_code, resp.json(), dict(resp.headers)


def _budget_log(caplog) -> logging.LogRecord:
    hits = [r for r in caplog.records if r.getMessage() == "request exceeded budget"]
    assert hits, (
        f"no budget log emitted; saw {[r.getMessage() for r in caplog.records]}"
    )
    assert len(hits) == 1, "one timeout must emit exactly one budget log"
    return hits[0]


async def _timeout_with(client, caplog, stall) -> tuple[dict, logging.LogRecord]:
    caplog.clear()
    with caplog.at_level(logging.WARNING):
        stall()
        status, body, headers = await _timed_out_search(client)
    assert status == 504, body
    assert headers["retry-after"] == "1"
    return body, _budget_log(caplog)


# ---------------------------------------------------------------------------
# 1 — the timeout path really fires through the production stack
# ---------------------------------------------------------------------------


async def test_the_real_app_returns_the_budget_envelope_on_a_stalled_hop(
    client, tight_budget, monkeypatch, caplog
):
    caplog.clear()
    with caplog.at_level(logging.WARNING):
        _stall_embedding_provider(monkeypatch)
        status, body, headers = await _timed_out_search(client)

    assert status == 504, body
    err = body["error"]
    assert err["code"] == "REQUEST_BUDGET_EXCEEDED"
    assert err["details"]["budget_seconds"] == _BUDGET_S
    assert err["details"]["path"] == "/api/v1/search"
    assert err["details"]["elapsed_seconds"] >= _BUDGET_S
    assert headers["retry-after"] == "1"
    assert headers["content-type"] == "application/json"

    rec = _budget_log(caplog)
    assert rec.levelno == logging.WARNING
    assert rec.path == "/api/v1/search"
    assert rec.method == "POST"
    assert rec.budget_seconds == _BUDGET_S
    assert rec.elapsed_seconds >= _BUDGET_S


async def test_the_declared_content_length_matches_the_body_it_sends(
    client, tight_budget, monkeypatch, caplog
):
    """The body now carries the phase list, so its length varies with what was
    running. A raw ASGI ``http.response.start`` sends exactly the headers it is
    given — nothing downstream recomputes this."""
    with caplog.at_level(logging.WARNING):
        _stall_embedding_provider(monkeypatch)
        tenant_id, headers = get_test_auth()
        resp = await client.post(
            "/api/v1/search",
            json={"tenant_id": tenant_id, "query": "q"},
            headers=headers,
        )
    assert resp.status_code == 504
    assert int(resp.headers["content-length"]) == len(resp.content)


# ---------------------------------------------------------------------------
# 2 — the question both rows are actually waiting on
# ---------------------------------------------------------------------------


def _evidence(body: dict, rec: logging.LogRecord) -> dict:
    """Everything an operator holds after ONE 504, minus the wall-clock noise.

    Every duration is dropped, not just ``elapsed_seconds``: on a timeout the
    in-flight phases all read within a few ms of the budget by construction,
    so letting a duration discriminate would claim an attribution that a real
    incident — where every 504 lands at the same ~45s — does not have. What is
    left is only the NAMES, which is the claim being tested.
    """
    details = dict(body["error"]["details"])
    details.pop("elapsed_seconds", None)
    for key in ("phases_cancelled", "phases_completed", "phases_open"):
        if key in details:
            details[key] = [p["phase"] for p in details[key]]
    return {
        "code": body["error"]["code"],
        "message": body["error"]["message"],
        "details": details,
        "log_phase": getattr(rec, "phase", None),
        "log_cancelled": [p["phase"] for p in getattr(rec, "phases_cancelled", [])],
    }


async def test_the_504_names_the_layer_that_ate_the_budget(
    client, tight_budget, monkeypatch, caplog
):
    """Two different incidents must not produce one indistinguishable 504.

    A stalled embedding provider and a stalled storage read have different
    owners and different fixes. Before ``request_phase`` the evidence for both
    was the same three fields — code, budget, path — and the plan h-01/h-02
    closed on ("the next occurrence will say which layer") was not met by the
    code that closed them.
    """
    embed_body, embed_log = await _timeout_with(
        client, caplog, lambda: _stall_embedding_provider(monkeypatch)
    )
    embed_evidence = _evidence(embed_body, embed_log)

    monkeypatch.undo()
    monkeypatch.setattr(_timeout_middleware(), "timeout_seconds", _BUDGET_S)

    storage_body, storage_log = await _timeout_with(
        client, caplog, lambda: _stall_storage_query(monkeypatch)
    )
    storage_evidence = _evidence(storage_body, storage_log)

    assert embed_evidence != storage_evidence, (
        "a stalled embedding hop and a stalled storage hop emit identical "
        f"evidence; the 504 cannot attribute the budget to a layer:\n{embed_evidence}"
    )
    assert embed_evidence["details"]["phase"] == "embed.query"
    assert (
        storage_evidence["details"]["phase"] == "storage.POST /memories/scored-search"
    )


async def test_the_log_line_carries_the_phase_for_aggregation(
    client, tight_budget, monkeypatch, caplog
):
    """Body attribution only helps the one caller who got the 504. "Which layer
    is eating budgets, how often" is a log question, so the phase has to be a
    flat top-level field on the record, not something to parse out of text."""
    _, rec = await _timeout_with(
        client, caplog, lambda: _stall_storage_query(monkeypatch)
    )
    assert rec.phase == "storage.POST /memories/scored-search"
    assert [p["phase"] for p in rec.phases_cancelled][:2] == [
        "storage.POST /memories/scored-search",
        "search.execute_scored_search",
    ]
    # What already finished is the other half of the answer: it rules the
    # completed hops OUT, which is how "the pipeline was fine until here" gets
    # said at all.
    assert "search.classify_query" in [p["phase"] for p in rec.phases_completed]


async def test_a_saturated_bulkhead_is_not_reported_as_a_slow_backend(
    client, tight_budget, monkeypatch, caplog
):
    """h-02's shape — both semantic endpoints down, CRUD healthy — fits BOTH a
    stalled storage backend and a storage bulkhead with no free slots, and
    those have opposite fixes. The queue wait is where the time goes, so the
    queue is what the 504 has to name — and it is the one hop whose only prior
    signal was a DEBUG line that prod never emits."""
    body, rec = await _timeout_with(
        client, caplog, lambda: _stall_storage_bulkhead(monkeypatch)
    )
    assert body["error"]["details"]["phase"] == "slot_acquire.storage_search"
    assert rec.phase == "slot_acquire.storage_search"
    assert "storage.POST /memories/scored-search" not in [
        p["phase"] for p in rec.phases_cancelled
    ], "a request still queued for a slot never reached the storage call"


async def test_the_message_says_the_phase_in_words(
    client, tight_budget, monkeypatch, caplog
):
    """A caller that logs ``error.message`` and nothing else — the common case
    for an agent SDK — still gets the layer."""
    body, _ = await _timeout_with(
        client, caplog, lambda: _stall_embedding_provider(monkeypatch)
    )
    assert "embed.query" in body["error"]["message"]


# ---------------------------------------------------------------------------
# 3 — the recorder must not leak between requests
# ---------------------------------------------------------------------------


async def test_phases_do_not_leak_from_one_request_into_the_next(
    client, tight_budget, monkeypatch, caplog
):
    """The recorder is bound to a ContextVar. Under an in-process ASGI
    transport the app runs in the CALLER's context, so a binding left behind
    would put one request's phases in the next request's 504 — attribution
    that is worse than none."""
    from core_api import request_phase

    await _timeout_with(client, caplog, lambda: _stall_embedding_provider(monkeypatch))
    assert request_phase.current() is None

    monkeypatch.undo()
    monkeypatch.setattr(_timeout_middleware(), "timeout_seconds", _BUDGET_S)

    _, rec = await _timeout_with(
        client, caplog, lambda: _stall_storage_query(monkeypatch)
    )
    assert "embed.query" not in [p["phase"] for p in rec.phases_cancelled]
    assert request_phase.current() is None


async def test_a_request_that_finishes_leaves_no_recorder_bound(client, monkeypatch):
    from core_api import request_phase

    monkeypatch.setattr(
        "core_api.routes.memories.search_memories",
        lambda *_a, **_kw: _ready([]),
    )
    tenant_id, headers = get_test_auth()
    resp = await client.post(
        "/api/v1/search",
        json={"tenant_id": tenant_id, "query": "q"},
        headers=headers,
    )
    assert resp.status_code == 200, resp.text
    assert request_phase.current() is None


# ---------------------------------------------------------------------------
# 4 — the evidence that pointed the WRONG way
# ---------------------------------------------------------------------------


async def test_a_budget_timeout_is_not_filed_as_a_500_in_the_access_log(
    client, tight_budget, monkeypatch, caplog
):
    """``http.request`` backs the per-endpoint dashboard — the first place an
    incident looks. ``RequestObservationMiddleware`` sits INSIDE the budget, so
    a cancelled request unwinds through it with no status line ever sent and
    fell to its 500 default. The caller held a 504 and the dashboard said
    crash, which is a different investigation."""
    caplog.clear()
    with caplog.at_level(logging.INFO, logger="core_api.access"):
        _stall_embedding_provider(monkeypatch)
        status, _, _ = await _timed_out_search(client)
    assert status == 504

    events = [r for r in caplog.records if r.getMessage() == "http.request"]
    assert len(events) == 1, [r.getMessage() for r in caplog.records]
    assert events[0].http_status_code == 504
    # Not asserted here: the route LABEL, which the six pre-existing
    # failures in ``test_request_observation.py`` already own.
    assert events[0].http_route.endswith("/search")


async def test_the_route_does_not_log_a_cancelled_search_as_a_completed_one(
    client, tight_budget, monkeypatch, caplog
):
    """``search request completed`` reported ``error=false, row_count=0`` on a
    request the server itself killed: ``CancelledError`` is not an
    ``Exception``, so the arm that sets ``success = False`` never ran. In this
    route's own telemetry a timed-out search was a legitimately empty one."""
    caplog.clear()
    with caplog.at_level(logging.INFO, logger="core_api.routes.memories"):
        _stall_embedding_provider(monkeypatch)
        status, _, _ = await _timed_out_search(client)
    assert status == 504

    done = [r for r in caplog.records if r.getMessage() == "search request completed"]
    assert len(done) == 1
    assert done[0].error is True
    assert done[0].cancelled is True


async def test_a_genuinely_empty_search_is_still_not_an_error(
    client, monkeypatch, caplog
):
    """The other side of the same line: matching nothing is a successful
    search, and must not be dragged into the error rate by the fix above."""
    monkeypatch.setattr(
        "core_api.routes.memories.search_memories", lambda *_a, **_kw: _ready([])
    )
    caplog.clear()
    with caplog.at_level(logging.INFO, logger="core_api.routes.memories"):
        tenant_id, headers = get_test_auth()
        resp = await client.post(
            "/api/v1/search",
            json={"tenant_id": tenant_id, "query": "q"},
            headers=headers,
        )
    assert resp.status_code == 200, resp.text
    done = [r for r in caplog.records if r.getMessage() == "search request completed"]
    assert done and done[-1].error is False and done[-1].cancelled is False
