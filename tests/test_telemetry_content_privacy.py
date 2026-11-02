"""Content and credential data stay out of operational telemetry."""

import ast
import json
import logging
import subprocess
import sys
from pathlib import Path

import pytest
import structlog

from common.llm.providers._shape_error import ProviderResponseShapeError
from common.structlog_config import _reset_for_testing, configure_logging

pytestmark = pytest.mark.unit


def test_long_log_fields_without_email_do_not_stall_logging():
    # Bound the regression itself: the original regex takes quadratic time.
    subprocess.run(
        [
            sys.executable,
            "-c",
            "from common.structlog_config import _scrub; "
            "value = 'a' * 1_000_000; "
            "assert _scrub(value) == value; "
            "assert _scrub(type('Value', (), {'__str__': lambda self: value})()) == value",
        ],
        check=True,
        timeout=3,
    )


@pytest.mark.parametrize(
    "key",
    [
        "auth_token",
        "bearer_token",
        "token_hash",
        "access_token",
        "refresh_token",
        "id_token",
        "client_secret",
        "x-api-key",
        "cookie",
        "set_cookie",
        "private_key",
    ],
)
def test_log_scrubber_masks_credential_variants_and_preserves_metrics(key):
    from common.structlog_config import _scrub

    fields = {
        key: "private-credential",
        "input_tokens": 17,
        "output_tokens": 3,
        "total_tokens": 20,
    }
    scrubbed = _scrub(fields)
    assert scrubbed.pop(key) == "[REDACTED]"
    assert scrubbed == {"input_tokens": 17, "output_tokens": 3, "total_tokens": 20}


@pytest.mark.parametrize("email", ["person@example.test", "person%40example.test"])
def test_log_scrubber_masks_literal_and_encoded_email(email):
    from common.structlog_config import _scrub

    assert _scrub(f"Registered {email}") == "Registered [REDACTED]"


@pytest.mark.parametrize("render", [str, repr])
def test_provider_shape_error_does_not_render_model_output(render):
    private = "private-model-output-unique-marker"
    error = ProviderResponseShapeError("OpenAI", private, "list")
    assert private not in render(error)
    assert error.content == private
    assert "OpenAI" in str(error)
    assert "list" in str(error)


@pytest.mark.parametrize("logger_kind", ["stdlib", "structlog"])
def test_json_logs_redact_nested_payloads_and_email(logger_kind, capsys):
    _reset_for_testing()
    try:
        configure_logging("prod", "INFO", json_logs=True)
        fields = {
            "payload": {
                "content": "private memory marker",
                "nested": [
                    {
                        "query": "private search marker",
                        "Authorization": "credential marker",
                    }
                ],
                "input_tokens": 17,
            },
            "email": "person@example.test",
        }
        if logger_kind == "stdlib":
            logging.getLogger("privacy-test").info(
                "Registered person@example.test", extra=fields
            )
        else:
            structlog.get_logger().info("Registered person@example.test", **fields)
        rendered = capsys.readouterr().out
        for private in (
            "private memory marker",
            "private search marker",
            "credential marker",
            "person@example.test",
        ):
            assert private not in rendered
        event = json.loads(rendered.strip().splitlines()[-1])
        assert event["payload"]["input_tokens"] == 17
        assert event["severity"] == "INFO"
    finally:
        _reset_for_testing()


def test_json_exception_logging_omits_model_output(capsys):
    _reset_for_testing()
    private = "private-response-exception-marker"
    try:
        configure_logging("prod", "INFO", json_logs=True)
        try:
            raise ProviderResponseShapeError("Vertex", private, "list")
        except ProviderResponseShapeError:
            logging.getLogger("privacy-test").exception("Provider shape failure")
        rendered = capsys.readouterr().out
        assert private not in rendered
        event = json.loads(rendered.strip().splitlines()[-1])
        assert "Vertex" in event["exception"]
        assert event["severity"] == "ERROR"
    finally:
        _reset_for_testing()


@pytest.mark.parametrize(
    "entrypoint", ["core-api/src/core_api/app.py", "core-worker/src/core_worker/app.py"]
)
def test_llmobs_privacy_registered_at_both_service_entrypoints(entrypoint):
    tree = ast.parse(Path(entrypoint).read_text())
    calls = [
        node.func.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    ]
    assert "configure_llmobs_privacy" in calls


async def test_recall_span_export_contains_no_query_or_memory_text(
    mcp_env, monkeypatch
):
    from types import SimpleNamespace

    pytest.importorskip("ddtrace.llmobs")
    from ddtrace.llmobs import LLMObs
    from ddtrace.llmobs._writer import LLMObsSpanWriter

    from core_api import mcp_server
    from tests._mcp_test_helpers import parse_envelope, stub_storage_client

    # Exercise the installed SDK's final serializer without starting intake
    # writers or making any requests to Datadog.
    events = []
    monkeypatch.setattr(LLMObs, "_start_service", lambda self: None)
    monkeypatch.setattr(LLMObs, "_stop_service", lambda self: None)
    monkeypatch.setattr(
        LLMObsSpanWriter, "enqueue", lambda self, event: events.append(event)
    )
    LLMObs.enable(
        ml_app="test-privacy",
        env="prod",
        api_key="test-only",
        integrations_enabled=False,
        agentless_enabled=True,
    )
    try:
        monkeypatch.setenv("DD_LLMOBS_ENABLED", "true")
        from common import structlog_config

        configure_privacy = getattr(structlog_config, "configure_llmobs_privacy", None)
        if configure_privacy is not None:
            configure_privacy()
        config = SimpleNamespace(
            recall_boost=False, graph_expand=False, entity_retrieval=True
        )

        async def resolve(tenant):
            return config

        private_query = "private recall query unique marker"
        private_memory = "private memory text unique marker"

        async def search(*args, query, **kwargs):
            with LLMObs.embedding(model_name="test-embedding", model_provider="openai"):
                LLMObs.annotate(
                    input_data=[{"text": query}],
                    output_data="vector",
                    metrics={"input_tokens": 5},
                )
            with LLMObs.retrieval(name="recall.retrieve"):
                LLMObs.annotate(
                    input_data=query,
                    output_data=[{"text": private_memory, "name": private_memory}],
                    metadata={"prompt": private_memory},
                )
            with LLMObs.llm(model_name="test-llm", model_provider="openai"):
                LLMObs.annotate(
                    input_data=[{"role": "user", "content": private_memory}],
                    output_data=[{"role": "assistant", "content": private_memory}],
                    metrics={"input_tokens": 7, "output_tokens": 3},
                )
            return [
                SimpleNamespace(
                    model_dump=lambda **kwargs: {"id": "m1", "content": private_memory}
                )
            ]

        monkeypatch.setattr(mcp_server, "resolve_config", resolve)
        stub_storage_client(monkeypatch, get_agent=None)
        mcp_env["service"]("search_memories").side_effect = search
        payload = parse_envelope(
            await mcp_server.caura_recall(query=private_query, agent_id="test-agent")
        )
        assert payload["items"][0]["content"] == private_memory
        assert len(events) == 3
        rendered = json.dumps(events)
        assert private_query not in rendered
        assert private_memory not in rendered
        embedding = next(
            event for event in events if event["meta"]["span"]["kind"] == "embedding"
        )
        assert embedding["meta"]["model_name"] == "test-embedding"
        assert embedding["metrics"]["input_tokens"] == 5
    finally:
        LLMObs.disable()


def test_llmobs_processor_drops_unrecognized_or_unwritable_spans():
    from types import SimpleNamespace

    from common.structlog_config import redact_llmobs_span

    assert (
        redact_llmobs_span(SimpleNamespace(get_tag=lambda key: "future-span-kind"))
        is None
    )

    class ReadOnlySpan:
        def get_tag(self, key):
            return "embedding"

        @property
        def input(self):
            return [{"content": "private memory marker"}]

    assert redact_llmobs_span(ReadOnlySpan()) is None


def test_llmobs_registration_is_optional_but_cannot_silently_fail(monkeypatch):
    import sys
    from types import SimpleNamespace
    from unittest.mock import Mock

    from common.structlog_config import configure_llmobs_privacy

    register = Mock(side_effect=RuntimeError("processor unavailable"))
    sdk = SimpleNamespace(LLMObs=SimpleNamespace(register_processor=register))
    monkeypatch.setitem(sys.modules, "ddtrace", SimpleNamespace(llmobs=sdk))
    monkeypatch.setitem(sys.modules, "ddtrace.llmobs", sdk)
    monkeypatch.delenv("DD_LLMOBS_ENABLED", raising=False)
    configure_llmobs_privacy()
    register.assert_not_called()
    monkeypatch.setenv("DD_LLMOBS_ENABLED", "true")
    with pytest.raises(RuntimeError, match="processor unavailable"):
        configure_llmobs_privacy()


@pytest.mark.parametrize(
    "key",
    [
        "max_tokens",
        "token_count",
        "tokens_used",
        "cached_tokens",
        "reasoning_tokens",
        "cache_read_input_tokens",
        "cache_creation_input_tokens",
        "tokens_in",
        "tokens_out",
    ],
)
def test_log_scrubber_preserves_operational_token_counts(key):
    from common.structlog_config import _scrub

    assert _scrub({key: 17}) == {key: 17}


@pytest.mark.parametrize("name", ["_scrub", "scrub_log_event", "redact_llmobs_span"])
def test_telemetry_boundaries_use_concrete_annotations(name):
    from typing import Any, get_args, get_type_hints

    from common import structlog_config

    def contains_any(annotation):
        return annotation is Any or any(
            contains_any(arg) for arg in get_args(annotation)
        )

    hints = get_type_hints(getattr(structlog_config, name))
    assert hints
    assert not any(contains_any(hint) for hint in hints.values())
