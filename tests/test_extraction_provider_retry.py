"""An extraction every provider failed is asked again, then recorded.

Staging, 2026-10-07: a held write was released while Vertex answered
``429 RESOURCE_EXHAUSTED``. The release replay's entity extraction fell through
to the regex heuristic, which found nothing in the memory, and the worker
returned before its audit entry without recording anything. The memory kept no
entities, nothing retried it, and nothing said so.

Now a provider failure that asking again may fix makes the extractor raise
``ProvidersUnavailableError`` to the worker, which asks again after each of
``ENTITY_EXTRACTION_PROVIDER_RETRY_DELAYS_S`` before it settles for the
heuristic. A run the heuristic finished is recorded
(``entity_extraction_degraded``) and its audit entry says ``degraded``.
"""

from __future__ import annotations

import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import httpx
import openai
import pytest
from google.genai import errors as genai_errors

from common.llm.constants import LLM_RETRY_JITTER_FRACTION
from common.llm.providers import ProviderResponseShapeError
from common.llm.providers._unsupported import UnsupportedStructuredOutputError
from core_api.constants import ENTITY_EXTRACTION_PROVIDER_RETRY_DELAYS_S
from core_api.services import entity_extraction as ee
from core_api.services import entity_extraction_worker as w
from core_api.services.entity_extraction import (
    ExtractedEntity,
    ExtractedGraph,
    ProvidersUnavailableError,
)
from tests.conftest import close_scheduled_coro

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]

_DEGRADED_LINE = "entity_extraction_degraded_to_heuristic"
_REQUEST = httpx.Request("POST", "https://llm.invalid/v1")


def _vertex(code: int) -> genai_errors.APIError:
    """What google-genai raises for Vertex and Gemini: staging's was the 429."""
    cls = genai_errors.ClientError if code < 500 else genai_errors.ServerError
    return cls(code, {"error": {"code": code, "message": "from the provider"}}, None)


def _httpx(code: int) -> httpx.HTTPStatusError:
    """A status carried only on ``.response``, as httpx raises it."""
    return httpx.HTTPStatusError(
        "from the provider",
        request=_REQUEST,
        response=httpx.Response(code, request=_REQUEST),
    )


def _openai(code: int) -> openai.APIStatusError:
    return openai.APIStatusError(
        "from the provider", response=httpx.Response(code, request=_REQUEST), body=None
    )


# ── The extractor: which failures it hands back ──


class _Provider:
    """An LLM provider whose ``complete_json`` answers or raises as it is told."""

    def __init__(self, outcome, *, is_fake: bool = False):
        self.outcome = outcome
        self.is_fake = is_fake
        self.calls = 0

    async def complete_json(self, prompt, **_kw):
        self.calls += 1
        if isinstance(self.outcome, BaseException):
            raise self.outcome
        return self.outcome


@pytest.fixture
def chain(monkeypatch):
    """The real fallback chain over one provider, with no fallback and no backoff."""

    def _install(outcome, **kw) -> _Provider:
        provider = _Provider(outcome, **kw)
        monkeypatch.setattr(
            "common.llm.registry.get_llm_provider", lambda *_a, **_k: provider
        )
        monkeypatch.setattr(ee.settings, "entity_extraction_provider", "openai")
        monkeypatch.setattr("asyncio.sleep", AsyncMock())
        return provider

    return _install


async def test_a_rate_limited_provider_is_handed_back_instead_of_the_heuristic(
    chain, caplog
):
    rate_limited = _vertex(429)
    provider = chain(rate_limited)

    with caplog.at_level(logging.ERROR, logger=ee.__name__):
        with pytest.raises(ProvidersUnavailableError) as caught:
            await ee.extract_entities_from_content(
                "Anna Bergstrom joined Acme Corp.",
                "fact",
                raise_on_provider_failure=True,
            )

    assert caught.value.__cause__ is rate_limited
    assert provider.calls > 0
    assert not [r for r in caplog.records if _DEGRADED_LINE in r.getMessage()]


async def test_without_asking_for_it_the_extractor_still_degrades(chain, caplog):
    """Every other caller keeps the old contract: a graph, never a raise."""
    chain(_vertex(429))

    with caplog.at_level(logging.ERROR, logger=ee.__name__):
        graph = await ee.extract_entities_from_content(
            "Anna Bergstrom joined Acme Corp.", "fact"
        )

    assert {e.canonical_name for e in graph.entities} == {"anna bergstrom", "acme corp"}
    assert [r for r in caplog.records if _DEGRADED_LINE in r.getMessage()]


@pytest.mark.parametrize(
    "error",
    [
        pytest.param(_vertex(429), id="vertex-429"),
        pytest.param(_vertex(503), id="vertex-503"),
        pytest.param(_openai(500), id="openai-500"),
        pytest.param(_openai(408), id="openai-408"),
        pytest.param(_httpx(502), id="httpx-502"),
        pytest.param(openai.APITimeoutError(request=_REQUEST), id="openai-timeout"),
        pytest.param(
            openai.APIConnectionError(request=_REQUEST), id="openai-connection"
        ),
        pytest.param(httpx.ConnectTimeout("t", request=_REQUEST), id="httpx-timeout"),
        pytest.param(TimeoutError(), id="timeout"),
        pytest.param(ConnectionResetError(), id="connection-reset"),
    ],
)
async def test_every_kind_of_transient_failure_is_handed_back(chain, error):
    chain(error)

    with pytest.raises(ProvidersUnavailableError) as caught:
        await ee.extract_entities_from_content(
            "Anna Bergstrom joined Acme Corp.", "fact", raise_on_provider_failure=True
        )

    assert caught.value.__cause__ is error


@pytest.mark.parametrize(
    ("outcome", "is_fake"),
    [
        pytest.param(["not", "a", "graph"], False, id="output-that-does-not-parse"),
        pytest.param(
            ProviderResponseShapeError("vertex", "not json", "str"),
            False,
            id="response-shape",
        ),
        pytest.param(
            UnsupportedStructuredOutputError("no json mode"),
            False,
            id="no-structured-output",
        ),
        pytest.param({}, True, id="no-usable-provider"),
        pytest.param(_openai(401), False, id="bad-key"),
        pytest.param(_vertex(403), False, id="forbidden"),
        pytest.param(_vertex(404), False, id="unknown-model"),
        pytest.param(_openai(400), False, id="rejected-request"),
        pytest.param(_httpx(404), False, id="httpx-404"),
        pytest.param(
            RuntimeError("an error nobody classified"), False, id="unknown-error"
        ),
    ],
)
async def test_a_failure_asking_again_cannot_fix_degrades_at_once(
    chain, caplog, outcome, is_fake
):
    """A bad key, an unknown model or a rejected request fails the same way
    later; the seed is pinned, so bad output repeats; with no usable provider
    there is no one to ask; and an error nobody classified is not assumed to
    pass. Waiting minutes for any of them would only delay the heuristic."""
    chain(outcome, is_fake=is_fake)

    with caplog.at_level(logging.ERROR, logger=ee.__name__):
        graph = await ee.extract_entities_from_content(
            "Anna Bergstrom joined Acme Corp.", "fact", raise_on_provider_failure=True
        )

    assert graph.entities
    assert [r for r in caplog.records if _DEGRADED_LINE in r.getMessage()]


# ── The worker: asking again, then recording ──

_CONFIG = SimpleNamespace(
    entity_blocklist=frozenset(),
    auto_entity_linking_enabled=False,
    entity_extraction_provider="openai",
)
# No capitalised phrase, so the heuristic finds nothing: the staging case.
_PLAIN = "g2.10 held-write fix check 1: a test memory for the gate panel."
_NAMED = "Anna Bergstrom joined the platform team."


def _unavailable() -> ProvidersUnavailableError:
    exc = ProvidersUnavailableError(
        "every entity-extraction provider failed; the last with ClientError"
    )
    exc.__cause__ = RuntimeError("429 RESOURCE_EXHAUSTED")
    return exc


def _sc(content: str, *, row: dict | str | None = "live") -> MagicMock:
    sc = MagicMock()
    live = {"id": "m", "deleted_at": None, "content": content}
    sc.get_memory = AsyncMock(return_value=live if row == "live" else row)
    sc.bulk_resolve_entities = AsyncMock(return_value=[None])
    sc.bulk_upsert_entities = AsyncMock(
        return_value=[{"input_idx": 0, "entity_id": str(uuid4()), "action": "created"}]
    )
    sc.bulk_upsert_entity_links = AsyncMock(
        side_effect=lambda tenant_id, items: [
            {"input_idx": i["input_idx"], "created": True} for i in items
        ]
    )
    sc.set_subject_entity_if_null = AsyncMock(return_value=True)
    sc.set_predicate_if_null = AsyncMock(return_value=True)
    return sc


async def _run(extract: AsyncMock, sc: MagicMock, content: str) -> SimpleNamespace:
    seen = SimpleNamespace(log=AsyncMock(), record=AsyncMock(), sleep=AsyncMock())
    with (
        patch(
            "core_api.services.organization_settings.resolve_config",
            new=AsyncMock(return_value=_CONFIG),
        ),
        patch.object(w, "extract_entities_from_content", new=extract),
        patch.object(w, "get_storage_client", return_value=sc),
        patch.object(w, "get_embedding", new=AsyncMock(return_value=None)),
        patch.object(w, "log_action", new=seen.log),
        patch.object(w, "record_task_failure", new=seen.record),
        patch.object(w, "upsert_relation", new=AsyncMock()),
        patch.object(w.asyncio, "sleep", new=seen.sleep),
        patch("core_api.tasks.track_task", side_effect=close_scheduled_coro),
    ):
        await w.process_entity_extraction(uuid4(), "t1", None, "a1", content, "fact")
    return seen


def _waits(seen: SimpleNamespace) -> list[float]:
    return [c.args[0] for c in seen.sleep.await_args_list]


def _within_jitter(waits: list[float], delays) -> bool:
    """Each wait is its delay, lengthened by at most the retry layer's jitter."""
    return len(waits) == len(delays) and all(
        d <= w_ <= d * (1 + LLM_RETRY_JITTER_FRACTION)
        for w_, d in zip(waits, delays, strict=True)
    )


def _audits(seen: SimpleNamespace) -> list[dict]:
    return [
        c.kwargs["detail"]
        for c in seen.log.await_args_list
        if c.kwargs.get("action") == "entity_extraction"
    ]


async def test_a_provider_failure_is_asked_again_and_the_answer_used():
    extract = AsyncMock(side_effect=[_unavailable(), ExtractedGraph(entities=[])])

    seen = await _run(extract, _sc(_PLAIN), _PLAIN)

    assert extract.await_count == 2
    assert all(
        c.kwargs.get("raise_on_provider_failure") is True
        for c in extract.await_args_list
    )
    assert _within_jitter(_waits(seen), ENTITY_EXTRACTION_PROVIDER_RETRY_DELAYS_S[:1])
    seen.record.assert_not_awaited()
    assert _audits(seen) == []


async def test_providers_failing_on_every_try_are_recorded_and_audited_as_degraded():
    """The staging case: the heuristic finds nothing in this text."""
    extract = AsyncMock(side_effect=_unavailable())

    seen = await _run(extract, _sc(_PLAIN), _PLAIN)

    assert extract.await_count == len(ENTITY_EXTRACTION_PROVIDER_RETRY_DELAYS_S) + 1
    assert _within_jitter(_waits(seen), ENTITY_EXTRACTION_PROVIDER_RETRY_DELAYS_S)
    seen.record.assert_awaited_once()
    task, _memory_id, tenant, error = seen.record.await_args.args
    assert (task, tenant) == ("entity_extraction_degraded", "t1")
    assert "regex heuristic" in str(error)
    assert "Last error: RuntimeError: 429 RESOURCE_EXHAUSTED" in str(error)
    assert "429 RESOURCE_EXHAUSTED" in seen.record.await_args.kwargs["tb"]
    assert _audits(seen) == [
        {"entities_count": 0, "relations_count": 0, "degraded": True}
    ]


async def test_a_graph_the_heuristic_found_is_written_and_audited_as_degraded():
    extract = AsyncMock(side_effect=_unavailable())
    sc = _sc(_NAMED)

    seen = await _run(extract, sc, _NAMED)

    [item] = sc.bulk_upsert_entities.call_args.kwargs["items"]
    assert item["canonical_name"] == "anna bergstrom"
    assert seen.record.await_args.args[0] == "entity_extraction_degraded"
    [audit] = _audits(seen)
    assert audit["degraded"] is True
    assert audit["entities_count"] == 1


async def test_a_model_graph_is_audited_as_before():
    """No ``degraded`` key on a run the model answered: existing readers of the
    audit detail see exactly what they saw."""
    graph = ExtractedGraph(
        entities=[
            ExtractedEntity(canonical_name="anna bergstrom", entity_type="person")
        ]
    )
    seen = await _run(AsyncMock(return_value=graph), _sc(_NAMED), _NAMED)

    [audit] = _audits(seen)
    assert "degraded" not in audit
    seen.sleep.assert_not_awaited()
    seen.record.assert_not_awaited()


@pytest.mark.parametrize(
    "row",
    [
        pytest.param(None, id="gone-or-held"),
        pytest.param(
            {"id": "m", "deleted_at": "2026-10-07T15:35:00Z", "content": _PLAIN},
            id="deleted",
        ),
        pytest.param(
            {"id": "m", "deleted_at": None, "content": "edited text"}, id="edited"
        ),
    ],
)
async def test_a_row_that_no_longer_wants_the_extraction_is_not_asked_again(row):
    """A held row reads as gone and its release replays extraction; an edit
    schedules its own. Neither is worth minutes of waiting, or a record."""
    extract = AsyncMock(side_effect=_unavailable())

    seen = await _run(extract, _sc(_PLAIN, row=row), _PLAIN)

    assert extract.await_count == 1
    seen.sleep.assert_not_awaited()
    seen.record.assert_not_awaited()
    assert _audits(seen) == []


async def test_a_row_gone_during_the_last_wait_is_not_recorded_or_audited():
    """Deleted (or held, or edited) while the providers were down: the run ends
    as it would have if the model had answered, without a degraded record for
    a row nobody can read."""
    extract = AsyncMock(side_effect=_unavailable())
    sc = _sc(_PLAIN)
    live = {"id": "m", "deleted_at": None, "content": _PLAIN}
    sc.get_memory = AsyncMock(side_effect=[live, live, None])

    seen = await _run(extract, sc, _PLAIN)

    assert extract.await_count == len(ENTITY_EXTRACTION_PROVIDER_RETRY_DELAYS_S) + 1
    seen.record.assert_not_awaited()
    assert _audits(seen) == []


async def test_end_to_end_a_rate_limited_provider_is_asked_again_then_recorded(chain):
    """The staging sequence through the real extractor and fallback chain: every
    try is a 429, so the worker asks three times and then records the degrade."""
    provider = chain(_vertex(429))
    extract = AsyncMock(wraps=ee.extract_entities_from_content)

    seen = await _run(extract, _sc(_PLAIN), _PLAIN)

    tries = len(ENTITY_EXTRACTION_PROVIDER_RETRY_DELAYS_S) + 1
    assert extract.await_count == tries
    assert provider.calls >= tries
    assert seen.record.await_args.args[0] == "entity_extraction_degraded"
    assert "Last error: ClientError: 429" in str(seen.record.await_args.args[3])
    assert _audits(seen) == [
        {"entities_count": 0, "relations_count": 0, "degraded": True}
    ]


async def test_the_waits_are_jittered_so_a_bulk_write_does_not_retry_in_step():
    """A bulk write's extractions fail in the same second; unjittered, they would
    all ask the rate-limited provider again in the same second too."""
    extract = AsyncMock(side_effect=_unavailable())

    with patch.object(w.random, "uniform", side_effect=lambda _low, high: high):
        seen = await _run(extract, _sc(_PLAIN), _PLAIN)

    assert _waits(seen) == [
        d * (1 + LLM_RETRY_JITTER_FRACTION)
        for d in ENTITY_EXTRACTION_PROVIDER_RETRY_DELAYS_S
    ]


async def test_a_failed_row_read_does_not_cost_the_extraction():
    """A storage error while the providers are failing says nothing about the
    row: the run goes on to the heuristic, recorded and audited, instead of
    ending with no graph and no record."""
    extract = AsyncMock(side_effect=_unavailable())
    sc = _sc(_PLAIN)
    sc.get_memory = AsyncMock(side_effect=RuntimeError("storage 503"))

    seen = await _run(extract, sc, _PLAIN)

    assert extract.await_count == len(ENTITY_EXTRACTION_PROVIDER_RETRY_DELAYS_S) + 1
    assert seen.record.await_args.args[0] == "entity_extraction_degraded"
    assert _audits(seen) == [
        {"entities_count": 0, "relations_count": 0, "degraded": True}
    ]
