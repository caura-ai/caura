"""STM reports what actually happened.

Eight findings, one shape: short-term memory told the caller something that
was not true. A write the backend dropped came back 201 with an entry id and a
TTL. The TTL quoted was a setting the in-memory backend did not apply. The
published spec named one response shape for an endpoint with two, and told
self-hosted readers that STM could not be written over REST at all — on the
one configuration where it can. A promote with a bad ``memory_type`` answered
500, blaming the server for the caller's argument.

None of these is a wrong guard. Each is a true statement that stopped being
true and had nothing checking it, which is why the write path is pinned here
by what it RETURNS rather than by what it logs.
"""

from __future__ import annotations

import time

import pytest

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]


def _stm_ctx(target: str = "notes"):
    """A pipeline context for one STM write. Three tests needed the same one."""
    from core_api.pipeline.context import PipelineContext
    from core_api.schemas import MemoryCreate

    return PipelineContext(
        data={
            "input": MemoryCreate(
                tenant_id="t1", agent_id="a1", content="note", write_mode="stm"
            ),
            "stm_target": target,
            "t0": time.perf_counter(),
        }
    )


# ---------------------------------------------------------------------------
# oss-0902-m-19 / oss-0814-m-10 — a dropped write must not read as a stored one
# ---------------------------------------------------------------------------


class _Pipe:
    """Minimal redis pipeline stub; ``fail=True`` raises on execute."""

    def __init__(self, fail: bool):
        self._fail = fail

    def lpush(self, *a): ...
    def ltrim(self, *a): ...
    def expire(self, *a): ...

    async def execute(self):
        if self._fail:
            raise ConnectionError("redis went away mid-write")
        return [1, True, True]


class _Redis:
    def __init__(self, fail: bool = False):
        self._fail = fail

    def pipeline(self, transaction=False):
        return _Pipe(self._fail)

    async def delete(self, *keys):
        if self._fail:
            raise ConnectionError("redis went away mid-delete")
        return len(keys)


async def _redis_stm(monkeypatch, redis):
    from core_api.providers.redis_stm import RedisSTM

    stm = RedisSTM()
    monkeypatch.setattr(stm, "_redis", staticmethod(lambda: _async(redis)))
    return stm


async def _async(value):
    return value


@pytest.mark.parametrize(
    "redis,expected,why",
    [
        (None, False, "no connection at all"),
        (_Redis(fail=True), False, "connection present, write raised"),
        (_Redis(fail=False), True, "stored"),
    ],
)
async def test_redis_writes_report_whether_they_stored(
    monkeypatch, redis, expected, why
):
    """The return value is the whole fix.

    Both failure paths previously returned None, exactly as the success path
    did, so no caller could tell a dropped entry from a stored one.
    """
    stm = await _redis_stm(monkeypatch, redis)

    assert await stm.post_note("t1", "a1", {"id": "x"}) is expected, why
    assert await stm.post_bulletin("t1", "f1", {"id": "x"}) is expected, why


async def test_a_dropped_write_is_refused_not_receipted(monkeypatch):
    """The pipeline step must not mint a receipt for an entry nobody stored.

    503 rather than 500: the request was fine and retrying is the right move.
    A 201 here is the one answer that stops the caller retrying — which is what
    made this worth a medium rather than a logging nit.
    """
    from fastapi import HTTPException

    from core_api.pipeline.steps.write.write_stm_note import WriteSTMNote

    class _Dropping:
        async def post_note(self, *a, **k):
            return False

        async def post_bulletin(self, *a, **k):
            return False

    monkeypatch.setattr(
        "core_api.services.stm_service.get_stm_backend_instance", lambda: _Dropping()
    )
    ctx = _stm_ctx()

    with pytest.raises(HTTPException) as exc:
        await WriteSTMNote().execute(ctx)

    assert exc.value.status_code == 503, (
        "a dropped STM write must not be a 5xx-free 201"
    )
    assert "stm_response" not in ctx.data, "no receipt may be built for a dropped write"


# ---------------------------------------------------------------------------
# oss-0814-m-10 (second half) — a transient failure must not be permanent
# ---------------------------------------------------------------------------


async def test_redis_reconnects_after_the_cooldown(monkeypatch):
    """One refused connection used to disable Redis for the process lifetime.

    core-api racing Redis on a cold start is the ordinary way to get one. The
    assertion is that a LATER call tries again — ``redis_healthy`` already
    bypassed this function to dodge the latch, which is the tell that the latch
    was wrong rather than that the health gate was special.
    """
    import core_api.cache as cache

    monkeypatch.setattr(
        cache.settings, "redis_url", "redis://localhost:1/0", raising=False
    )
    monkeypatch.setattr(cache, "_redis", None, raising=False)
    monkeypatch.setattr(cache, "_redis_available", None, raising=False)
    monkeypatch.setattr(cache, "_redis_retry_after", 0.0, raising=False)

    attempts = {"n": 0}

    def _boom(*a, **k):
        attempts["n"] += 1
        raise ConnectionError("refused")

    monkeypatch.setattr(cache, "from_url", _boom)

    assert await cache._get_redis() is None
    assert attempts["n"] == 1

    # Inside the cooldown: no second attempt.
    assert await cache._get_redis() is None
    assert attempts["n"] == 1, "cooldown should suppress a retry storm"

    # Past the cooldown: it tries again rather than staying latched forever.
    monkeypatch.setattr(
        cache, "_redis_retry_after", time.monotonic() - 1, raising=False
    )
    assert await cache._get_redis() is None
    assert attempts["n"] == 2, "a transient failure must not be permanent"


# ---------------------------------------------------------------------------
# oss-0902-l-21 / oss-0814-l-29 — the TTL reported is the TTL applied
# ---------------------------------------------------------------------------


async def test_the_ttl_in_the_receipt_is_the_one_the_backend_applies(monkeypatch):
    """Both ends of the same defect.

    The response quotes ``settings``; the backend applies its own. This pins
    that they are the same number, which is the property the caller actually
    relies on when it decides how long the entry will live.
    """
    from core_api.pipeline.steps.write.write_stm_note import WriteSTMNote
    from core_api.providers.inmemory_stm import InMemorySTM

    monkeypatch.setattr("core_api.config.settings.stm_notes_ttl", 4321, raising=False)
    monkeypatch.setattr(
        "core_api.config.settings.stm_bulletin_ttl", 8765, raising=False
    )
    backend = InMemorySTM()
    monkeypatch.setattr(
        "core_api.services.stm_service.get_stm_backend_instance", lambda: backend
    )
    ctx = _stm_ctx()

    await WriteSTMNote().execute(ctx)

    # Also the don't-over-correct guard: a receipt IS produced for a stored
    # write, so the 503 above cannot be refusing everyone.
    assert ctx.data["stm_response"].ttl == backend._notes_ttl == 4321
    assert backend._bulletin_ttl == 8765, "the bulletin TTL is read from settings too"


# ---------------------------------------------------------------------------
# oss-0814-l-35 — keys that are never read again are still reclaimed
# ---------------------------------------------------------------------------


async def test_a_key_never_read_again_is_eventually_reclaimed(monkeypatch):
    """``_prune`` only ever ran on the key being touched.

    An agent that posts once and never reads kept its dict entry for the life
    of the process. Redis reclaims its own keys by TTL whether or not anyone
    returns; this is the in-memory backend paying for that itself.
    """
    from core_api.providers import inmemory_stm as mod

    stm = mod.InMemorySTM(notes_ttl=1)
    await stm.post_note("t1", "abandoned", {"id": "x"})
    assert stm._key("t1", "abandoned") in stm._notes

    # Expire the entry without anyone reading that key back.
    stm._notes[stm._key("t1", "abandoned")] = [({"id": "x"}, time.monotonic() - 10)]

    # A write to an UNRELATED key, once the sweep interval has elapsed.
    monkeypatch.setattr(mod, "_SWEEP_INTERVAL_SECONDS", 0)
    await stm.post_note("t1", "someone-else", {"id": "y"})

    assert stm._key("t1", "abandoned") not in stm._notes, (
        "stale key was never reclaimed"
    )
    assert stm._key("t1", "someone-else") in stm._notes, "the live key must survive"


# ---------------------------------------------------------------------------
# oss-0902-l-25 / oss-0902-l-27 — the published contract matches the server
# ---------------------------------------------------------------------------


async def test_the_spec_documents_both_201_shapes():
    """``POST /memories`` returns ``STMWriteResponse`` for an STM write.

    A generated client discovers a one-shape spec by failing to parse a
    SUCCESSFUL write, which is the worst place to find out.
    """
    from core_api.app import app

    schema = app.openapi()["paths"]["/api/v1/memories"]["post"]["responses"]["201"]
    refs = {
        opt.get("$ref", "").rsplit("/", 1)[-1]
        for opt in schema["content"]["application/json"]["schema"].get("anyOf", [])
    }

    assert {"MemoryOut", "STMWriteResponse"} <= refs, f"201 documents only {refs}"


async def test_the_stm_description_does_not_deny_the_write_path():
    """The published text told self-hosted readers STM cannot be written over
    REST. It can — ``POST /memories`` with ``write_mode='stm'`` — and those
    readers are exactly the ones for whom it is reachable."""
    from core_api.routes.stm import _PLUGIN_ONLY

    lowered = _PLUGIN_ONLY.lower()
    assert "nothing can be put into short-term memory over rest" not in lowered
    assert "write_mode='stm'" in lowered, (
        "the description must name the path that works"
    )


async def test_both_stm_disabled_doors_refuse_with_the_same_text(monkeypatch):
    """One capability, two doors, one refusal.

    The read door's message was rewritten because "Set USE_STM=true" is advice
    a hosted caller cannot act on; the write door kept the old text, so the two
    disagreed about what to do next. Asserted by driving BOTH and comparing the
    details to the shared constant — an earlier version of this test scraped
    the two sources for a couple of phrases, which would have stayed green
    while the wording drifted around them.
    """
    from fastapi import HTTPException

    from core_api.constants import STM_DISABLED_DETAIL
    from core_api.routes.stm import _check_stm_enabled
    from core_api.schemas import MemoryCreate
    from core_api.services.memory_service import _run_write_pipeline

    monkeypatch.setattr("core_api.config.settings.use_stm", False, raising=False)

    with pytest.raises(HTTPException) as read_door:
        _check_stm_enabled()

    with pytest.raises(HTTPException) as write_door:
        await _run_write_pipeline(
            MemoryCreate(
                tenant_id="t1", agent_id="a1", content="note", write_mode="stm"
            )
        )

    assert read_door.value.status_code == write_door.value.status_code == 422
    assert read_door.value.detail == write_door.value.detail == STM_DISABLED_DETAIL


# ---------------------------------------------------------------------------
# oss-0902-l-28 — a bad argument is the caller's fault, not the server's
# ---------------------------------------------------------------------------


def _promote_client():
    """The STM router alone, with auth overridden.

    A cut-down version of ``test_stm_promote_rate_limit``'s harness — that one
    also wires the limiter, ``app.state`` and the rate-limit exception handler,
    none of which these cases reach. No DB and no Redis: every body below is
    refused while being parsed, before the handler runs.
    """
    from fastapi import FastAPI
    from httpx import ASGITransport, AsyncClient

    from core_api.auth import AuthContext, get_auth_context
    from core_api.routes import stm

    app = FastAPI()
    app.include_router(stm.router, prefix="/api/v1")

    async def _auth_dep():
        return AuthContext(
            tenant_id="t-acme", agent_id="agent-1", readable_tenant_ids=["t-acme"]
        )

    app.dependency_overrides[get_auth_context] = _auth_dep
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


@pytest.mark.parametrize(
    "field,bad",
    [
        ("memory_type", {"memory_type": "not_a_real_type"}),
        ("visibility", {"visibility": "scope_nonsense"}),
        ("content", {"content": "x" * 20000}),
    ],
)
async def test_promote_refuses_a_bad_argument_with_422(monkeypatch, field, bad):
    """These three reached ``MemoryCreate`` inside the handler, where a
    ``ValidationError`` is an unhandled exception — so the answer was 500: the
    server blaming itself for the caller's argument, and (being 5xx) inviting a
    retry that can never succeed.

    ``use_stm`` is left ON deliberately. With it off the handler's own 422
    would make this pass without the fix, so the assertion is on the offending
    FIELD being named — which only body validation can do.
    """
    monkeypatch.setattr("core_api.config.settings.use_stm", True, raising=False)
    body = {"agent_id": "agent-1", "content": "promote me"}
    body.update(bad)

    async with _promote_client() as client:
        r = await client.post("/api/v1/stm/promote", json=body)

    assert r.status_code == 422, f"{field}: got {r.status_code}, body={r.text[:200]}"
    assert field in r.text, f"the refusal should name {field}: {r.text[:200]}"


# ---------------------------------------------------------------------------
# The receipt rule applies to the other mutation too
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("method", ["clear_notes", "clear_bulletin"])
async def test_clears_report_whether_they_reached_the_backend(monkeypatch, method):
    """A clear is a mutation, so it owes the same receipt a write does.

    Its silent failure is the more alarming of the two: the caller is told the
    notes are gone and they are still there on the next read, which is the one
    outcome that gives it no reason to try again.
    """
    stm = await _redis_stm(monkeypatch, None)

    assert await getattr(stm, method)("t1", "x") is False

    stm_ok = await _redis_stm(monkeypatch, _Redis(fail=False))
    assert await getattr(stm_ok, method)("t1", "x") is True


async def test_the_503_survives_the_pipeline_runner(monkeypatch):
    """The step raising is not the same as the caller being told.

    ``_run_write_pipeline`` turns a failed run into a blanket
    ``500 STM write pipeline failed unexpectedly``, so a 503 raised inside a
    step is only useful if it is not swept into that. It is not — the runner
    re-raises ``HTTPException`` as-is and the generic 500 is reached via
    ``result.failed`` — but that is the runner's contract rather than this
    step's, so it is pinned here: the whole point of the fix is the answer the
    caller actually receives, and a step-level assertion stops one layer short
    of it.
    """
    from fastapi import HTTPException

    from core_api.schemas import MemoryCreate
    from core_api.services.memory_service import _run_write_pipeline

    class _Dropping:
        async def post_note(self, *a, **k):
            return False

    monkeypatch.setattr("core_api.config.settings.use_stm", True, raising=False)
    monkeypatch.setattr(
        "core_api.services.stm_service.get_stm_backend_instance", lambda: _Dropping()
    )

    with pytest.raises(HTTPException) as exc:
        await _run_write_pipeline(
            MemoryCreate(
                tenant_id="t1",
                agent_id="a1",
                content="a note long enough to clear the content-length gate",
                visibility="scope_agent",
                write_mode="stm",
            )
        )

    assert exc.value.status_code == 503, (
        f"the runner flattened the 503 to {exc.value.status_code}"
    )
    assert "not stored" in str(exc.value.detail)
