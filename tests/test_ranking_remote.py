"""RemoteRanker + registry tests — HTTP /rerank via httpx.MockTransport.

No network: a MockTransport stands in for the sidecar so we verify the
request body we send, the response parse (TEI bare-list AND Cohere-wrapped),
re-projection onto INPUT order, and the registry's remote wiring / no-URL
guard. This tests the provider CODE against the assumed contract — a real
TEI wet test still happens at sidecar-deploy time (see PR notes).
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import httpx
import pytest

import common.ranking._service as svc_mod
from common.ranking import PermanentRankError, RankCandidate, get_ranking
from common.ranking.protocols import RankProvider
from common.ranking.providers.remote import RemoteRanker


def _cands(*contents):
    return [
        RankCandidate(id=str(i), content=c, similarity=0.5)
        for i, c in enumerate(contents)
    ]


def _client_with(handler, base_url="http://sidecar:80", max_batch=32):
    """Build a RemoteRanker whose httpx client uses a MockTransport handler."""
    r = RemoteRanker(base_url=base_url, model="test-model", max_batch=max_batch)
    r._client = httpx.AsyncClient(
        base_url=base_url, transport=httpx.MockTransport(handler)
    )
    return r


def test_remote_conforms_to_protocol():
    assert isinstance(RemoteRanker(base_url="http://x"), RankProvider)


@pytest.mark.asyncio
async def test_tei_bare_list_response_reprojected_to_input_order():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["path"] = request.url.path
        captured["body"] = json.loads(request.content)
        # TEI-native: bare list, ranked (best first), index into input texts.
        return httpx.Response(
            200, json=[{"index": 2, "score": 0.9}, {"index": 0, "score": 0.1}]
        )

    r = _client_with(handler)
    scores = await r.rank("q", _cands("a", "b", "c"))
    # request shape
    assert captured["path"] == "/rerank"
    assert captured["body"] == {"query": "q", "texts": ["a", "b", "c"]}
    # scores re-projected to INPUT order: idx0=0.1, idx1=missing→0.0, idx2=0.9
    assert scores == [0.1, 0.0, 0.9]


@pytest.mark.asyncio
async def test_cohere_wrapped_response_and_relevance_score():
    def handler(request):
        return httpx.Response(
            200,
            json={
                "results": [
                    {"index": 0, "relevance_score": 0.8},
                    {"index": 1, "relevance_score": 0.3},
                ]
            },
        )

    r = _client_with(handler)
    scores = await r.rank("q", _cands("x", "y"))
    assert scores == [0.8, 0.3]


@pytest.mark.asyncio
async def test_empty_candidates_short_circuits():
    r = _client_with(lambda req: httpx.Response(500))  # would error if called
    assert await r.rank("q", []) == []


def test_registry_remote_requires_base_url():
    from common.ranking._registry import get_rank_provider

    # No RANK_BASE_URL and no tenant override → ValueError (→ service degrades).
    with pytest.raises(ValueError, match="requires a base URL"):
        get_rank_provider("remote", SimpleNamespace(rank_base_url=None))


def test_registry_remote_builds_with_tenant_base_url():
    from common.ranking._registry import get_rank_provider

    p = get_rank_provider(
        "remote", SimpleNamespace(rank_base_url="http://tei:80", rank_model="m")
    )
    assert isinstance(p, RemoteRanker)
    assert p.model == "m"


# --- failure classification: permanent (config-class) vs transient ----------
#
# Both kinds degrade to first-stage order, so `out is None` cannot tell them
# apart. What these assert is the difference that matters operationally: a
# permanent fault must NOT burn the retry budget, and must leave an ERROR
# carrying enough detail to fix it without reading the sidecar's own logs.


@pytest.fixture(autouse=True)
def _isolate_permanent_dedup(monkeypatch):
    """``_permanent_logged`` is module-global; give each test a fresh set.

    Per-TEST, not per-call: the dedup tests below drive get_ranking twice and
    need the state to survive between those calls.
    """
    monkeypatch.setattr("common.ranking._service._permanent_logged", set())
    # _RankStats accumulates process-wide; its "degraded: N consecutive
    # failures" trip-wire would add ERROR records that make the counts below
    # depend on test order.
    monkeypatch.setattr("common.ranking._service._stats", svc_mod._RankStats())


def _counting_handler(status, text="", json_body=None):
    """Handler that records how many times the sidecar was called."""
    calls = []

    def handler(request):
        calls.append(request)
        if json_body is not None:
            return httpx.Response(status, json=json_body)
        return httpx.Response(status, text=text)

    return handler, calls


async def _run_service(ranker, monkeypatch, attempts=3, candidates=None):
    """Drive get_ranking against `ranker` with a known retry budget."""
    # RANK_RETRY_ATTEMPTS defaults to 1 (no retry), which makes "permanent vs
    # transient" invisible — both would call once. Raise it so the difference
    # is observable. Do NOT "simplify" this back to the default: it would gut
    # every retry-budget assertion below into a tautology.
    monkeypatch.setattr("common.ranking._service.RANK_RETRY_ATTEMPTS", attempts)
    monkeypatch.setattr("common.ranking._service.RANK_RETRY_DELAY_S", 0.0)
    monkeypatch.setattr(
        "common.ranking._service.get_rank_provider", lambda name, tc=None: ranker
    )
    return await get_ranking(
        "q",
        candidates if candidates is not None else _cands("a", "b"),
        SimpleNamespace(rank_provider="remote"),
    )


@pytest.mark.asyncio
async def test_413_raises_permanent_error_with_actionable_detail():
    handler, _ = _counting_handler(
        413, text='{"error":"batch size 51 > maximum allowed batch size 32"}'
    )
    r = _client_with(handler)
    with pytest.raises(PermanentRankError) as ei:
        await r.rank("q", _cands("a", "b"))
    msg = str(ei.value)
    # status, endpoint, candidate count, and the sidecar's own words
    assert "413" in msg
    assert "http://sidecar:80/rerank" in msg
    assert "2 candidate(s)" in msg
    assert "maximum allowed batch size 32" in msg
    # the fix is not guessable from "413", so it is spelled out
    assert "max-client-batch-size" in msg
    await r._client.aclose()


@pytest.mark.asyncio
async def test_permanent_failure_is_not_retried_and_logs_error(monkeypatch, caplog):
    handler, calls = _counting_handler(413, text="too big")
    r = _client_with(handler)
    with caplog.at_level("WARNING"):
        out = await _run_service(r, monkeypatch, attempts=3)
    assert out is None  # still degrades to first-stage order
    # One call, not three. NB this saving only materialises where
    # RANK_RETRY_ATTEMPTS is raised above its default of 1 — at the default the
    # deliverable is the ERROR level and the actionable detail, asserted below.
    assert len(calls) == 1
    errors = [rec for rec in caplog.records if rec.levelname == "ERROR"]
    assert errors, "a permanent rerank failure must log at ERROR"
    joined = " ".join(rec.getMessage() for rec in errors)
    assert "413" in joined and "not retrying" in joined
    await r._client.aclose()


@pytest.mark.asyncio
async def test_transient_5xx_still_uses_the_full_retry_budget(monkeypatch, caplog):
    handler, calls = _counting_handler(503, text="unavailable")
    r = _client_with(handler)
    with caplog.at_level("WARNING"):
        out = await _run_service(r, monkeypatch, attempts=3)
    assert out is None
    assert len(calls) == 3, "a 5xx may succeed on retry — budget must be spent"
    assert any(
        "attempt" in rec.getMessage() and rec.levelname == "WARNING"
        for rec in caplog.records
    )
    await r._client.aclose()


@pytest.mark.asyncio
async def test_429_is_transient_despite_being_4xx(monkeypatch):
    handler, calls = _counting_handler(429, text="slow down")
    r = _client_with(handler)
    out = await _run_service(r, monkeypatch, attempts=3)
    assert out is None
    assert len(calls) == 3, "rate limiting is about timing, not the request"
    await r._client.aclose()


@pytest.mark.asyncio
async def test_408_is_transient_despite_being_4xx(monkeypatch):
    handler, calls = _counting_handler(408, text="request timeout")
    r = _client_with(handler)
    out = await _run_service(r, monkeypatch, attempts=3)
    assert out is None
    assert len(calls) == 3
    await r._client.aclose()


@pytest.mark.asyncio
async def test_200_that_is_not_a_ranked_list_is_permanent(monkeypatch):
    # Wrong service behind RANK_BASE_URL: 200s, but not a /rerank shape.
    handler, calls = _counting_handler(200, json_body={"detail": "not a reranker"})
    r = _client_with(handler)
    out = await _run_service(r, monkeypatch, attempts=3)
    assert out is None
    assert len(calls) == 1, "a wrong endpoint returns the same shape on retry"
    await r._client.aclose()


@pytest.mark.asyncio
async def test_permanent_error_logs_once_then_debug_until_next_success(
    monkeypatch, caplog
):
    """A permanent fault recurs every search, so it must not ERROR every time.

    Without dedup, ERROR volume tracks traffic instead of the fault, and
    alerting on ERROR rate pages on QPS.
    """
    handler, _ = _counting_handler(413, text="too big")
    r = _client_with(handler)
    with caplog.at_level("DEBUG"):
        await _run_service(r, monkeypatch, attempts=1)
        first = [rec for rec in caplog.records if rec.levelname == "ERROR"]
        assert len(first) == 1, "first occurrence reports in full"

        caplog.clear()
        await _run_service(r, monkeypatch, attempts=1)
        assert not [rec for rec in caplog.records if rec.levelname == "ERROR"], (
            "a repeat of the same condition must not ERROR again"
        )
        assert [rec for rec in caplog.records if rec.levelname == "DEBUG"]
    await r._client.aclose()


@pytest.mark.asyncio
async def test_success_re_arms_the_permanent_error(monkeypatch, caplog):
    """A fixed-then-regressed backend must report again, not stay silent.

    Recovery is per backend, so this drives ONE ranker whose sidecar flips
    413 -> 200 -> 413. (A *different* backend recovering must NOT re-arm this
    one — see test_one_backend_recovering_does_not_re_arm_another.)
    """
    state = {"ok": False}

    def flipping(request):
        if state["ok"]:
            return httpx.Response(200, json=[{"index": 0, "score": 1.0}])
        return httpx.Response(413, text="too big")

    r = _client_with(flipping)
    def permanent():
        return [
            rec for rec in caplog.records if "failed permanently" in rec.getMessage()
        ]
    with caplog.at_level("ERROR"):
        await _run_service(r, monkeypatch, attempts=1)
        assert len(permanent()) == 1
        # the same backend recovers, clearing its own dedup key...
        state["ok"] = True
        assert await _run_service(r, monkeypatch, attempts=1) is not None
        caplog.clear()
        # ...so a regression reports in full again rather than at DEBUG.
        state["ok"] = False
        await _run_service(r, monkeypatch, attempts=1)
        assert len(permanent()) == 1, "post-recovery regression must re-report"
    await r._client.aclose()


@pytest.mark.asyncio
async def test_dedup_is_scoped_per_backend_not_process_wide(monkeypatch, caplog):
    """Two tenants on two broken sidecars must each get their own ERROR.

    The registry caches a ranker per (base_url, api_key, model), so one
    process can hold several. A process-wide dedup key would let the first
    tenant's 413 silently suppress the second tenant's unrelated 413.
    """
    a_handler, _ = _counting_handler(413, text="tenant A sidecar too small")
    b_handler, _ = _counting_handler(413, text="tenant B sidecar too small")
    a = _client_with(a_handler, base_url="http://sidecar-a:80")
    b = _client_with(b_handler, base_url="http://sidecar-b:80")

    with caplog.at_level("ERROR"):
        await _run_service(a, monkeypatch, attempts=1)
        await _run_service(b, monkeypatch, attempts=1)
    msgs = [
        rec.getMessage()
        for rec in caplog.records
        if rec.levelname == "ERROR" and "failed permanently" in rec.getMessage()
    ]
    assert len(msgs) == 2, "each backend reports its own fault"
    assert any("sidecar-a" in m for m in msgs)
    assert any("sidecar-b" in m for m in msgs)
    await a._client.aclose()
    await b._client.aclose()


@pytest.mark.asyncio
async def test_one_backend_recovering_does_not_re_arm_another(monkeypatch, caplog):
    """A healthy tenant's success must not re-arm a still-broken tenant.

    Guards the failure mode where a global clear() puts ERROR volume back on a
    traffic curve — driven by *unrelated* tenants succeeding.
    """
    broken_handler, _ = _counting_handler(413, text="still broken")
    broken = _client_with(broken_handler, base_url="http://sidecar-broken:80")
    healthy = _client_with(
        lambda req: httpx.Response(200, json=[{"index": 0, "score": 1.0}]),
        base_url="http://sidecar-healthy:80",
    )

    with caplog.at_level("ERROR"):
        await _run_service(broken, monkeypatch, attempts=1)
        assert len(caplog.records) == 1
        caplog.clear()
        # unrelated backend succeeds repeatedly...
        for _ in range(3):
            assert await _run_service(healthy, monkeypatch, attempts=1) is not None
        # ...and the broken one stays deduped.
        await _run_service(broken, monkeypatch, attempts=1)
        assert not caplog.records, (
            "another backend's success must not re-arm this one's ERROR"
        )
    await broken._client.aclose()
    await healthy._client.aclose()


@pytest.mark.asyncio
async def test_dedup_scope_separates_same_url_different_model(monkeypatch, caplog):
    """Same base_url, different rank_model = two backends, two ERRORs.

    The registry caches per (base_url, api_key, model), so a per-tenant
    rank_model override yields a distinct instance. A URL-only dedup scope
    would collide these and hide one tenant's fault.
    """
    h1, _ = _counting_handler(413, text="model-a too big")
    h2, _ = _counting_handler(413, text="model-b too big")
    a = _client_with(h1)
    b = _client_with(h2)
    # Same URL, distinct instances (what the registry hands out for distinct
    # rank_model / rank_api_key) → distinct dedup scopes.
    assert a.dedup_scope != b.dedup_scope

    with caplog.at_level("ERROR"):
        await _run_service(a, monkeypatch, attempts=1)
        await _run_service(b, monkeypatch, attempts=1)
    permanent = [
        rec for rec in caplog.records if "failed permanently" in rec.getMessage()
    ]
    assert len(permanent) == 2
    await a._client.aclose()
    await b._client.aclose()


def test_dedup_scope_carries_no_credential_but_still_separates_keys():
    """No secret in the scope, yet two credentials still get distinct scopes.

    Deriving the scope from the api_key (even hashed) would be a fast hash of a
    secret; instance identity gets the same separation for free.
    """
    a = RemoteRanker(base_url="http://sidecar:80", api_key="super-secret-token")
    b = RemoteRanker(base_url="http://sidecar:80", api_key="a-different-token")
    assert "super-secret-token" not in a.dedup_scope
    assert "a-different-token" not in b.dedup_scope
    assert a.dedup_scope != b.dedup_scope


@pytest.mark.asyncio
async def test_200_with_non_json_body_is_permanent(monkeypatch):
    # Misrouted RANK_BASE_URL returning an HTML page: same wrong-endpoint
    # fault as a non-list body, so it must not burn the retry budget either.
    def handler(request):
        return httpx.Response(200, text="<html><body>nginx 200</body></html>")

    calls = []

    def counting(request):
        calls.append(request)
        return handler(request)

    r = _client_with(counting)
    out = await _run_service(r, monkeypatch, attempts=3)
    assert out is None
    assert len(calls) == 1, "a wrong endpoint returns the same body on retry"
    await r._client.aclose()


# --- chunking the candidate pool across requests ---------------------------


def _chunk_recorder(status=200, score_for=None):
    """Async handler recording each request's texts and peak in-flight count."""
    state = {"inflight": 0, "peak": 0, "batches": []}

    async def handler(request):
        body = json.loads(request.content)
        state["batches"].append(body["texts"])
        state["inflight"] += 1
        state["peak"] = max(state["peak"], state["inflight"])
        # Yield so a concurrent sibling can be observed in flight; a sequential
        # implementation can never overlap here no matter how long we sleep.
        await asyncio.sleep(0.02)
        state["inflight"] -= 1
        if status != 200:
            return httpx.Response(status, text="nope")
        n = len(body["texts"])
        return httpx.Response(
            200,
            json=[
                {"index": i, "score": (score_for(body["texts"][i]) if score_for else 1.0)}
                for i in range(n)
            ],
        )

    return handler, state


@pytest.mark.asyncio
async def test_pool_at_or_below_cap_stays_one_request():
    handler, state = _chunk_recorder()
    r = _client_with(handler, max_batch=32)
    await r.rank("q", _cands(*[f"c{i}" for i in range(32)]))
    assert len(state["batches"]) == 1, "exactly-at-cap must not split"
    await r._client.aclose()


@pytest.mark.asyncio
async def test_pool_above_cap_splits_and_preserves_input_order():
    # Score = position, so a mis-merged result is immediately visible.
    order = {f"c{i}": float(i) for i in range(50)}
    handler, state = _chunk_recorder(score_for=lambda t: order[t])
    r = _client_with(handler, max_batch=32)
    scores = await r.rank("q", _cands(*[f"c{i}" for i in range(50)]))

    assert len(state["batches"]) == 2
    assert [len(b) for b in state["batches"]] in ([32, 18], [18, 32])
    # every candidate sent exactly once, none duplicated or dropped
    sent = [t for b in state["batches"] for t in b]
    assert sorted(sent) == sorted(order)
    # merged back onto INPUT order, not response or chunk order
    assert scores == [float(i) for i in range(50)]
    # ...and issued CONCURRENTLY. Sequential chunks would blow
    # RANK_TIMEOUT_SECONDS, turning a batch-cap rejection into a silent timeout
    # — same symptom, harder to diagnose.
    assert state["peak"] >= 2, (
        f"chunks ran sequentially (peak in-flight {state['peak']}) — "
        "wall-clock would be the sum, not one chunk"
    )
    await r._client.aclose()


@pytest.mark.asyncio
async def test_one_failed_chunk_fails_the_whole_rank(monkeypatch):
    """Partial failure must degrade, never return a partially-scored list.

    Scoring the failed chunk 0.0 would bury real results at the bottom and
    call it success — worse than not reranking.
    """
    calls = {"n": 0}

    async def handler(request):
        calls["n"] += 1
        # first request in flight succeeds, the other 503s
        if calls["n"] == 1:
            texts = json.loads(request.content)["texts"]
            return httpx.Response(
                200, json=[{"index": i, "score": 1.0} for i in range(len(texts))]
            )
        return httpx.Response(503, text="unavailable")

    r = _client_with(handler, max_batch=32)
    with pytest.raises(httpx.HTTPStatusError):
        await r.rank("q", _cands(*[f"c{i}" for i in range(50)]))
    # and through the service layer a CHUNKED failure degrades rather than
    # raising into search (the default 2-candidate pool would take the fast
    # path and never exercise chunking at all)
    out = await _run_service(
        r, monkeypatch, attempts=1, candidates=_cands(*[f"c{i}" for i in range(50)])
    )
    assert out is None
    await r._client.aclose()


@pytest.mark.asyncio
async def test_permanent_chunk_failure_wins_over_a_transient_sibling(monkeypatch):
    """A 413 on one chunk must not be reported as a retryable failure.

    Misreporting it transient would spend the retry budget re-earning the same
    rejection.
    """
    async def handler(request):
        texts = json.loads(request.content)["texts"]
        await asyncio.sleep(0.01)
        # The SHORT TAIL chunk (18 of 50) is the permanently-rejected one, so a
        # naive `raise failures[0]` surfaces the transient 503 from chunk 0 and
        # this test fails. Keying off size rather than call order is what makes
        # the assertion actually guard the permanent-wins scan.
        return httpx.Response(413 if len(texts) == 18 else 503, text="cap exceeded")

    r = _client_with(handler, max_batch=32)
    with pytest.raises(PermanentRankError, match="RANK_REMOTE_MAX_BATCH"):
        await r.rank("q", _cands(*[f"c{i}" for i in range(50)]))
    await r._client.aclose()
