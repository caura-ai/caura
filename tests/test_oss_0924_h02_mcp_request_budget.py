"""oss-0924-h-02 — the MCP transport had no request budget at all.

``per_tenant_storage_slot`` queues UNBOUNDED and justifies that with *"the
outer request budget already caps total wall time"*. On REST that is true
(``RequestTimeoutMiddleware`` at 45s, or an opted-out route's own
``asyncio.wait_for``). On MCP it was false: ``is_mcp_path`` skips the whole
mount, nothing else enforced a deadline, and ``caura_recall`` reaches that
exact semaphore through ``search_memories``. The code asserted a cap that did
not exist on the surface agents use.

So these tests are not about a new feature. They pin that the claim the code
already makes is now TRUE on both transports, and that making it true did not
cost anything #1715 bought:

* a stalled tool call ends, with the envelope and the phase a REST 504 carries;
* the budget comes from settings, so an operator can move it;
* our deadline is never confused with a client disconnect, in either direction;
* a ``TimeoutError`` our clock did not produce is not reported as our budget.

Driven through ``mcp.call_tool`` — the single dispatch point a JSON-RPC
``tools/call`` runs (see ``test_mcp_call_tool_e2e``), not the handler
coroutine, because the budget lives in the dispatch frame and calling the
handler directly would skip it entirely. Only the deadline is lowered; the
stalled hop is patched, so no provider and no LLM call runs.
"""

from __future__ import annotations

import asyncio
import logging
from types import SimpleNamespace

import pytest

from core_api import mcp_server, request_phase
from core_api.config import settings
from tests._mcp_test_helpers import (
    is_error_envelope,
    parse_envelope,
    stub_storage_client,
)

# ``asyncio`` per-test rather than in ``pytestmark``: the startup-validator
# test below is synchronous, and a blanket mark makes pytest warn on it.
# Same shape as ``test_mcp_call_tool_e2e``.
pytestmark = pytest.mark.unit

# Far past the budget the tests set: a stall that could finish on its own
# would make every assertion here a race.
_STALL_S = 30.0

# Low enough to keep the suite fast, high enough that ordinary scheduling
# jitter cannot make a hop that never yields look like a timeout.
_BUDGET_S = 0.4

# The hop the row is about: the unbounded ``per_tenant_storage_slot`` acquire
# that ``search_memories`` reaches and that nothing on this transport capped.
_SLOT = "slot_acquire.storage_search"


# The stalled hop is released in teardown rather than left running — an
# abandoned sleep outliving its test is the same hazard
# ``test_ax_h01_h02_attribution_coverage`` documents.
_release: asyncio.Event | None = None


@pytest.fixture(autouse=True)
def stall_gate():
    global _release
    _release = asyncio.Event()
    yield
    _release.set()
    _release = None


async def _stall_forever() -> None:
    gate = _release
    if gate is None:
        await asyncio.sleep(_STALL_S)
        return
    await asyncio.wait_for(gate.wait(), _STALL_S)


@pytest.fixture
def budget(monkeypatch):
    """Lower the real setting the dispatch frame reads.

    ``call_tool`` resolves ``settings.mcp_request_timeout_seconds`` at call
    time, so this is the production path rather than a way around it — and
    that is itself part of what is under test: a literal in the dispatch frame
    would be unreachable from here for the same reason it would be unreachable
    from an operator's env.
    """
    monkeypatch.setattr(settings, "mcp_request_timeout_seconds", _BUDGET_S)
    return _BUDGET_S


@pytest.fixture
def hop(monkeypatch, mcp_env):
    """Wire ``caura_recall`` to reach ``search_memories``, and stall it there.

    ``mcp_env`` bypasses auth, trust and metering; recall additionally reads
    the tenant config and looks its agent up through the storage client. Same
    wiring as the #1715 MCP tests, kept identical on purpose so a difference
    between the two files is a difference in behaviour, not in setup.

    Returns an installer: ``hop(body, phase=...)`` replaces the search hop
    with ``body`` running inside a named ``request_phase.phase``.
    """

    async def _config(_tenant_id):
        return SimpleNamespace(
            require_agent_approval=False,
            recall_boost=False,
            graph_expand=False,
            entity_retrieval=True,
        )

    monkeypatch.setattr(mcp_server, "resolve_config", _config)
    stub_storage_client(monkeypatch, get_agent=None)

    def _install(body, phase: str = _SLOT):
        async def _hop(*_a, **_kw):
            with request_phase.phase(phase):
                return await body()

        monkeypatch.setattr(mcp_server, "search_memories", _hop)

    return _install


async def _recall():
    return await mcp_server.mcp.call_tool(
        "caura_recall", {"query": "who runs fleet ops"}
    )


def _log(caplog, message: str) -> logging.LogRecord:
    hits = [r for r in caplog.records if r.getMessage().startswith(message)]
    assert hits, f"no {message!r} log; saw {[r.getMessage() for r in caplog.records]}"
    return hits[-1]


# ===========================================================================
# 1 — the budget exists, and says which hop it cancelled
# ===========================================================================


@pytest.mark.asyncio
async def test_a_stalled_tool_call_ends_at_the_budget_with_the_hop_named(
    hop, budget, caplog
):
    """The row in one test.

    Before this, a ``caura_recall`` queued behind a saturated
    ``per_tenant_storage_slot`` had nothing to end it but the client hanging
    up — while that semaphore's own docstring said the outer request budget
    already capped the wait. The stall here is that acquire. That ``await``
    returns at all is the invariant being restored; that it returns NAMING
    the hop is #1715's attribution surviving the change.
    """
    hop(_stall_forever)

    with caplog.at_level(logging.WARNING):
        result = await asyncio.wait_for(_recall(), _BUDGET_S + 5.0)

    envelope = parse_envelope(result)["error"]
    assert envelope["code"] == "REQUEST_BUDGET_EXCEEDED"
    assert envelope["details"]["phase"] == _SLOT, (
        "a timed-out MCP call must name the layer that ate the budget, the "
        "same way a timed-out REST call does"
    )
    assert envelope["details"]["budget_seconds"] == _BUDGET_S
    assert envelope["details"]["elapsed_seconds"] >= _BUDGET_S
    assert envelope["details"]["tool"] == "caura_recall"
    assert _SLOT in envelope["message"]


@pytest.mark.asyncio
async def test_the_timeout_reaches_the_client_as_an_mcp_error_envelope(hop, budget):
    """``isError=True``, not a raised exception.

    A raise is turned by the SDK into ``UnexpectedToolError`` whose text is a
    prefix and nothing else — which is how the auth refusals lost their codes
    before B2 (``test_mcp_call_tool_e2e``). A caller can branch on
    ``REQUEST_BUDGET_EXCEEDED``; it cannot branch on a stringified crash.
    """
    hop(_stall_forever)

    result = await asyncio.wait_for(_recall(), _BUDGET_S + 5.0)

    assert is_error_envelope(result), (
        "the budget must surface through the same error channel every other "
        "MCP refusal uses"
    )
    assert "validation error" not in str(result)


@pytest.mark.asyncio
async def test_the_timeout_log_uses_the_same_field_names_as_the_rest_one(
    hop, budget, caplog
):
    """Flat, top-level ``phase`` / ``phases_cancelled`` / ``phases_completed``.

    #1715's whole reason for those names on the opted-out routes was that the
    two paths group together in the log backend instead of needing separate
    queries. A third spelling on the transport agents use most would undo it.
    """
    hop(_stall_forever)

    with caplog.at_level(logging.WARNING):
        await asyncio.wait_for(_recall(), _BUDGET_S + 5.0)

    rec = _log(caplog, "MCP tool call exceeded its request budget")
    assert rec.phase == _SLOT
    assert rec.tool == "caura_recall"
    assert rec.budget_seconds == _BUDGET_S
    assert rec.elapsed_seconds >= _BUDGET_S
    assert isinstance(rec.phases_cancelled, list)
    assert isinstance(rec.phases_completed, list)


# ===========================================================================
# 2 — the budget is configuration, not a literal
# ===========================================================================


@pytest.mark.asyncio
async def test_the_budget_is_read_from_settings_at_call_time(hop, monkeypatch, caplog):
    """An operator can move it by env without a redeploy.

    Captured at import or class-definition time it would be dead config — the
    failure mode ``request_timeout_seconds`` has to work around with a walk of
    ``app.middleware_stack``. Pinned by moving it and reading the number back
    out of the envelope the caller receives.
    """
    monkeypatch.setattr(settings, "mcp_request_timeout_seconds", 0.25)
    hop(_stall_forever)

    with caplog.at_level(logging.WARNING):
        result = await asyncio.wait_for(_recall(), 5.0)

    assert parse_envelope(result)["error"]["details"]["budget_seconds"] == 0.25


def test_a_budget_past_the_platform_ceiling_is_refused_at_startup():
    """Above the ceiling the deadline can never fire — nginx / Cloud Run sever
    the connection first while the tool keeps running.

    Worth catching at startup for this budget specifically: it exists to make
    a cap the code already CLAIMS actually exist, so a value that cannot fire
    would restore the claim in config and leave ``per_tenant_storage_slot``'s
    docstring lying exactly as it was.
    """
    from core_api.config import PLATFORM_REQUEST_CEILING_SECONDS, Settings

    with pytest.raises(ValueError, match="mcp_request_timeout_seconds"):
        Settings(mcp_request_timeout_seconds=PLATFORM_REQUEST_CEILING_SECONDS + 1)


# ===========================================================================
# 3 — cancellation: our deadline and a client disconnect are not each other
# ===========================================================================


@pytest.mark.asyncio
async def test_a_client_disconnect_is_still_reported_as_a_disconnect(
    hop, budget, caplog
):
    """A dropped connection cancels the dispatch. ``asyncio.timeout``
    uncancels only the cancellation IT issued, so an external one still
    arrives as ``CancelledError`` — it must propagate, not be laundered into
    a ``REQUEST_BUDGET_EXCEEDED`` envelope that claims we gave up on time.

    This is the failure #1715 widened its handler to ``BaseException`` for;
    adding a deadline inside that handler is precisely how it would have been
    lost again.
    """

    async def _cancelled():
        raise asyncio.CancelledError

    hop(_cancelled)

    with caplog.at_level(logging.WARNING):
        with pytest.raises(asyncio.CancelledError):
            await _recall()

    rec = _log(caplog, "MCP tool call failed")
    assert rec.error_type == "CancelledError"
    assert rec.phase == _SLOT


@pytest.mark.asyncio
async def test_our_own_deadline_is_not_filed_as_a_disconnect(hop, budget, caplog):
    """The converse, and the reason the two handlers are NESTED rather than
    sibling clauses on one ``try``.

    ``asyncio.timeout`` converts its own cancel into ``TimeoutError`` at the
    ``async with`` boundary; the inner clause handles it and returns, so the
    ``except BaseException`` that catches disconnects never sees it. Were they
    siblings, clause ORDER alone would decide whether a timed-out call was
    reported as a hang-up — and ``BaseException`` first would swallow every
    one of them.
    """
    hop(_stall_forever)

    with caplog.at_level(logging.WARNING):
        await asyncio.wait_for(_recall(), _BUDGET_S + 5.0)

    assert not [
        r for r in caplog.records if r.getMessage().startswith("MCP tool call failed")
    ], "our deadline must not be reported as the client giving up"


@pytest.mark.asyncio
async def test_an_ordinary_failure_still_names_its_hop(hop, budget, caplog):
    """#1715's coverage, re-pinned because arming a budget could silently take
    it away.

    With no deadline armed, every unwind was filed as the in-flight stack. With
    one, a hop that fails BEFORE the deadline goes to ``phases_failed`` and
    stays out of ``phase`` — correct for a timeout report, and silence for this
    one, which is not a timeout. ``snapshot(attribute_failed=True)`` is what
    keeps the exception path reading as it did.
    """

    async def _boom():
        raise RuntimeError("storage read gave up")

    hop(_boom, phase="storage.POST /memories/scored-search")

    with caplog.at_level(logging.WARNING):
        # The SDK re-raises a handler crash as its own error type; what this
        # pins is that the phase survived to the log line regardless.
        with pytest.raises(Exception):  # noqa: B017
            await _recall()

    rec = _log(caplog, "MCP tool call failed")
    assert rec.phase == "storage.POST /memories/scored-search"
    assert rec.error_type != "TimeoutError"


@pytest.mark.asyncio
async def test_a_timeout_our_clock_did_not_produce_is_not_called_our_budget(
    hop, budget, monkeypatch, caplog
):
    """Hops below run their own ``wait_for`` (the embedding gate at 10s, the
    storage bulk cap at 25s) and raise the same class from well inside this
    budget.

    Today they cannot reach the dispatch frame wearing it — the SDK's
    ``Tool.run`` catches ``Exception`` and re-raises as
    ``UnexpectedToolError`` — which is why the raise is injected at the frame
    below this one instead: it is the only way to ask the question without
    depending on the wrapping staying as it is. That dependency is the point.
    A ``REQUEST_BUDGET_EXCEEDED`` whose correctness rests on a vendored
    dependency's exception handling is the same shape as the defect this
    change fixes one layer over, so the frame checks its own clock.
    """
    from mcp.server import MCPServer

    async def _inner_timeout(self, name, arguments, context=None):
        with request_phase.phase("embed.query"):
            raise TimeoutError("the embedding gate fired, exactly as designed")

    monkeypatch.setattr(MCPServer, "call_tool", _inner_timeout)

    with caplog.at_level(logging.WARNING):
        with pytest.raises(TimeoutError):
            await _recall()

    assert not [
        r
        for r in caplog.records
        if r.getMessage().startswith("MCP tool call exceeded its request budget")
    ], "an inner cap firing on time must not be reported as our deadline blowing"
    rec = _log(caplog, "MCP tool call failed")
    assert rec.phase == "embed.query"


# ===========================================================================
# 4 — the recorder is still per-call
# ===========================================================================


@pytest.mark.asyncio
async def test_the_recorder_does_not_leak_after_a_timed_out_call(hop, budget):
    """Under the in-process transport every call shares one context, so a
    binding leaked by the timeout path would put a cancelled call's phases in
    the NEXT call's report. #1715 pinned this for the success path; the
    timeout path is a second way out of the same ``with``.
    """
    hop(_stall_forever)

    assert request_phase.current() is None
    await asyncio.wait_for(_recall(), _BUDGET_S + 5.0)
    assert request_phase.current() is None
