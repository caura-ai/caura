"""What search() and recall() send and return (audit 2026-10-01, B33 and B35).

L-173: recall() reads ``memories`` but never sent ``items_alias=false``, so every
response carried the same list a second time under ``items``, about half the
body.

L-94: search() returned only the items, so the envelope around them
(``recall_tracked``, ``warnings``, ``diagnostic``) was unreachable: a
``diagnostic=True`` search could not be read, and a warning about an ignored
parameter was dropped.

L-03: a tenant-key client could read its own ``scope_agent`` memories only by
asserting ``caller_agent_id``, which neither method named. Both take it now, as
an opt-in keyword: unset, the body is as before.
"""

from __future__ import annotations

import inspect
import json

import httpx
import pytest

from caura_client import Caura


def _client(handler, **kwargs):
    return Caura(
        "mc_test",
        tenant_id="t1",
        base_url="https://example.test",
        transport=httpx.MockTransport(handler),
        **kwargs,
    )


def _recall_body(memories):
    return {
        "query": "q",
        "summary": "S",
        "memory_count": len(memories),
        "memories": memories,
        "recall_ms": 12,
    }


def _sent(bodies):
    """A handler that records each request body and answers like the server."""

    def handler(request):
        bodies.append(json.loads(request.content))
        if request.url.path == "/api/v1/search":
            return httpx.Response(200, json={"items": []})
        return httpx.Response(200, json=_recall_body([{"id": "m1", "content": "a"}]))

    return handler


def test_l173_recall_asks_for_the_list_once_unless_told_otherwise():
    bodies = []
    mc = _client(_sent(bodies))

    result = mc.recall("q")
    mc.recall("q", items_alias=True)

    assert bodies[0]["items_alias"] is False
    assert [m.id for m in result.supporting_memories] == ["m1"]
    assert bodies[1]["items_alias"] is True


def test_l94_search_keeps_the_envelope_around_its_results():
    envelope = {
        "items": [{"id": "m1", "content": "a"}],
        "recall_tracked": True,
        "diagnostic": {"candidates": 3},
        "warnings": [
            {
                "code": "unrecognized_parameters",
                "message": "bogus is not a /search parameter",
                "details": {"params": ["bogus"]},
            },
        ],
    }
    mc = _client(lambda request: httpx.Response(200, json=envelope))

    results = mc.search("q", diagnostic=True, bogus=1)

    assert isinstance(results, list)
    assert [m.id for m in results] == ["m1"]
    assert results.recall_tracked is True
    assert results.diagnostic == {"candidates": 3}
    assert results.warnings == envelope["warnings"]
    assert results.raw == envelope


def test_l94_an_envelope_without_the_optional_fields_reads_as_none():
    results = _client(lambda request: httpx.Response(200, json={"items": []})).search("q")

    assert results == []
    assert results.recall_tracked is None
    assert results.diagnostic is None
    assert results.warnings is None


@pytest.mark.parametrize("method", ["search", "recall"])
def test_l03_caller_agent_id_is_an_opt_in_keyword(method):
    assert "caller_agent_id" in inspect.signature(getattr(Caura, method)).parameters
    bodies = []
    mc = _client(_sent(bodies), agent_id="a1")

    getattr(mc, method)("q")
    getattr(mc, method)("q", caller_agent_id="a1")

    # Unset, the client's agent_id is not asserted: the server would narrow the
    # read to that agent's fleet and trust (Eldad, 2026-10-08).
    assert "caller_agent_id" not in bodies[0]
    assert bodies[1]["caller_agent_id"] == "a1"
