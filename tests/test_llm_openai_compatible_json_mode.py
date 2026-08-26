"""``complete_json`` sends a ``response_format`` the endpoint accepts.

OpenAI-compatible servers disagree on JSON mode. Hosted OpenAI takes
``json_object`` and a non-strict ``json_schema``. LM Studio rejects
``json_object``. Anthropic's compatible endpoint rejects ``json_object``,
rejects ``strict: false``, and rejects any strict schema whose objects
are not closed. These tests pin the shape sent to each kind of endpoint,
and the fence strip that makes a bare-prompt reply parse.

The provider is built by its own constructor and only the transport is
swapped, the same way ``test_llm_provider_sdk_retries.py`` does it.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import httpx
import pytest

from common.llm import constants
from common.llm.providers.openai import (
    OpenAILLMProvider,
    _drop_optional_nulls,
    _is_hosted_openai,
    _strict_schema,
    _strip_code_fence,
)

HOSTED = "https://api.openai.com/v1"
SELF_HOSTED = "http://localhost:1234/v1"


def _provider(base_url: str, reply: str, sent: list[dict]) -> OpenAILLMProvider:
    """A real provider whose socket records the request body and answers ``reply``."""

    def _handler(request: httpx.Request) -> httpx.Response:
        sent.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "id": "chatcmpl-test",
                "object": "chat.completion",
                "created": 0,
                "model": "test-model",
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": reply},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {
                    "prompt_tokens": 1,
                    "completion_tokens": 1,
                    "total_tokens": 2,
                },
            },
        )

    provider = OpenAILLMProvider(
        api_key="test-key", model="test-model", base_url=base_url
    )
    provider._client._client._transport = httpx.MockTransport(_handler)
    return provider


SCHEMA = {
    "type": "object",
    "title": "Graph",
    "properties": {
        "entities": {
            "type": "array",
            "default": [],
            "items": {"$ref": "#/$defs/Entity"},
        },
        "note": {"anyOf": [{"type": "string"}, {"type": "null"}], "default": None},
    },
    "$defs": {
        "Entity": {
            "type": "object",
            "title": "Entity",
            "properties": {"name": {"type": "string"}, "kind": {"type": "string"}},
            "required": ["name"],
        }
    },
}


class TestNoSchema:
    async def test_hosted_openai_keeps_json_object(self):
        sent: list[dict] = []
        provider = _provider(HOSTED, '{"ok": true}', sent)
        assert await provider.complete_json("give me json") == {"ok": True}
        assert sent[0]["response_format"] == {"type": "json_object"}

    async def test_other_endpoints_get_no_response_format(self):
        sent: list[dict] = []
        provider = _provider(SELF_HOSTED, '{"ok": true}', sent)
        assert await provider.complete_json("give me json") == {"ok": True}
        assert "response_format" not in sent[0]

    async def test_a_fenced_reply_parses(self):
        sent: list[dict] = []
        provider = _provider(SELF_HOSTED, '```json\n{"ok": true}\n```', sent)
        assert await provider.complete_json("give me json") == {"ok": True}


class TestWithSchema:
    async def test_hosted_openai_sends_the_schema_as_given(self):
        sent: list[dict] = []
        provider = _provider(HOSTED, '{"entities": []}', sent)
        await provider.complete_json("extract", response_schema=SCHEMA)
        fmt = sent[0]["response_format"]
        assert fmt["type"] == "json_schema"
        assert fmt["json_schema"]["strict"] is False
        assert fmt["json_schema"]["schema"] == SCHEMA

    async def test_other_endpoints_get_a_strict_closed_schema(self):
        sent: list[dict] = []
        provider = _provider(SELF_HOSTED, '{"entities": [], "note": null}', sent)
        await provider.complete_json("extract", response_schema=SCHEMA)
        fmt = sent[0]["response_format"]
        assert fmt["json_schema"]["strict"] is True
        schema = fmt["json_schema"]["schema"]
        assert schema["additionalProperties"] is False
        assert schema["required"] == ["entities", "note"]
        entity = schema["$defs"]["Entity"]
        assert entity["additionalProperties"] is False
        assert entity["required"] == ["name", "kind"]


class TestStrictSchema:
    def test_closes_every_object_and_drops_defaults_and_titles(self):
        out = _strict_schema(SCHEMA)
        assert "title" not in out
        assert "default" not in out["properties"]["entities"]
        assert "default" not in out["properties"]["note"]
        # ``entities`` was optional, so it is wrapped nullable; the array is inside.
        assert out["properties"]["entities"]["anyOf"][0]["items"] == {
            "$ref": "#/$defs/Entity"
        }
        assert out["properties"]["note"]["anyOf"] == [
            {"type": "string"},
            {"type": "null"},
        ]

    def test_does_not_modify_its_input(self):
        before = json.dumps(SCHEMA, sort_keys=True)
        _strict_schema(SCHEMA)
        assert json.dumps(SCHEMA, sort_keys=True) == before


class TestOptionalFieldsUnderStrictMode:
    """Strict mode requires every property; the source schema's optional ones
    become nullable on the wire and absent again after parsing."""

    def test_the_test_schema_optional_fields_become_nullable(self):
        out = _strict_schema(SCHEMA)
        entities = out["properties"]["entities"]
        assert entities["anyOf"][1] == {"type": "null"}
        assert entities["anyOf"][0]["type"] == "array"
        # ``note`` already admits null and is not wrapped twice.
        assert out["properties"]["note"]["anyOf"] == [
            {"type": "string"},
            {"type": "null"},
        ]
        kind = out["$defs"]["Entity"]["properties"]["kind"]
        assert kind == {"anyOf": [{"type": "string"}, {"type": "null"}]}
        assert out["$defs"]["Entity"]["properties"]["name"] == {"type": "string"}

    def test_extracted_graph_optional_fields_stay_optional_for_the_caller(self):
        from core_api.services.entity_extraction import ExtractedGraph

        source = ExtractedGraph.model_json_schema()
        strict = _strict_schema(source)
        mention = strict["$defs"]["Mention"]
        assert mention["required"] == ["surface", "cluster_id", "entity_canonical"]
        # The three list fields default to [] in the model and are not required
        # in the source schema; on the wire they are required but nullable.
        for name in ("entities", "relations", "mentions"):
            assert name in strict["required"]
            assert strict["properties"][name]["anyOf"][1] == {"type": "null"}
        reply = {
            "entities": None,
            "relations": [],
            "mentions": [
                {"surface": "ACME", "cluster_id": None, "entity_canonical": None}
            ],
        }
        cleaned = _drop_optional_nulls(reply, source)
        assert "entities" not in cleaned
        assert cleaned["relations"] == []
        # Mention's two optional fields are nullable in the model itself, so a
        # null is a value the model may send and is kept.
        assert cleaned["mentions"] == [
            {"surface": "ACME", "cluster_id": None, "entity_canonical": None}
        ]
        assert ExtractedGraph.model_validate(cleaned).entities == []

    def test_enrichment_result_survives_the_round_trip(self):
        from common.enrichment.schema import EnrichmentResult

        source = EnrichmentResult.model_json_schema()
        strict = _strict_schema(source)
        assert set(strict["required"]) == set(source["properties"])
        for name, prop in strict["properties"].items():
            assert prop.get("anyOf", [{}])[-1] == {"type": "null"}, name
        # A null is dropped for every field the model would have omitted, and
        # kept only where the source schema itself admits null.
        reply = {name: None for name in source["properties"]}
        nullable = {
            name
            for name, prop in source["properties"].items()
            if any(o.get("type") == "null" for o in prop.get("anyOf", []))
        }
        cleaned = _drop_optional_nulls(reply, source)
        assert set(cleaned) == nullable
        assert all(v is None for v in cleaned.values())
        assert EnrichmentResult.model_validate(cleaned).title == ""

    async def test_nulls_for_optional_fields_are_dropped_after_parsing(self):
        sent: list[dict] = []
        provider = _provider(
            SELF_HOSTED,
            '{"entities": [{"name": "a", "kind": null}], "note": null}',
            sent,
        )
        result = await provider.complete_json("extract", response_schema=SCHEMA)
        # ``kind`` was optional and not nullable: dropped. ``note`` admits null
        # in the source schema: kept as sent.
        assert result == {"entities": [{"name": "a"}], "note": None}

    async def test_hosted_openai_replies_are_returned_as_sent(self):
        sent: list[dict] = []
        provider = _provider(HOSTED, '{"entities": null, "note": null}', sent)
        result = await provider.complete_json("extract", response_schema=SCHEMA)
        assert result == {"entities": None, "note": None}


class TestHostedDetection:
    @pytest.mark.parametrize(
        ("url", "hosted"),
        [
            ("https://api.openai.com/v1", True),
            ("https://api.openai.com/v1/", True),
            ("http://api.openai.com/v1", True),
            ("https://API.OpenAI.com/v1", True),
            ("https://my-proxy.example.com/v1", False),
            ("http://localhost:1234/v1", False),
        ],
    )
    def test_decides_by_host(self, url: str, hosted: bool):
        assert _is_hosted_openai(url) is hosted

    @pytest.mark.parametrize(
        ("url", "warns"),
        [
            ("http://lmstudio.internal:1234/v1", True),
            ("http://localhost:1234/v1", False),
            ("http://127.0.0.1:1234/v1", False),
            ("https://my-proxy.example.com/v1", False),
        ],
    )
    def test_plaintext_off_host_warns_at_construction(
        self, caplog, url: str, warns: bool
    ):
        with caplog.at_level("WARNING", logger="common.llm.providers.openai"):
            OpenAILLMProvider(api_key="test-key", model="test-model", base_url=url)
        assert ("sent unencrypted" in caplog.text) is warns


class TestStripCodeFence:
    @pytest.mark.parametrize(
        "content",
        [
            '{"a": 1}',
            '```json\n{"a": 1}\n```',
            '```\n{"a": 1}\n```',
            '  ```json\n{"a": 1}\n```  ',
        ],
    )
    def test_the_json_inside_is_returned(self, content: str):
        assert json.loads(_strip_code_fence(content)) == {"a": 1}

    def test_a_bare_fence_marker_is_left_alone(self):
        assert _strip_code_fence("```") == "```"


class TestBaseUrlOverrides:
    """The constants read the environment at import time.

    A fresh interpreter is the honest way to test an import-time read:
    ``importlib.reload`` depends on ``sys.modules`` state that other tests
    in the suite rearrange.
    """

    @pytest.mark.parametrize(
        "variable",
        ["OPENAI_CHAT_BASE_URL", "ANTHROPIC_CHAT_BASE_URL", "OPENROUTER_CHAT_BASE_URL"],
    )
    def test_the_environment_wins(self, variable: str):
        code = (
            "from common.llm import constants as c; "
            f"print(c.{variable}); print(c.OPENAI_HOSTED_CHAT_BASE_URL)"
        )
        env = {k: v for k, v in os.environ.items() if not k.endswith("_CHAT_BASE_URL")}
        default = _constants_in_a_fresh_interpreter(code, env)
        env[variable] = SELF_HOSTED
        overridden = _constants_in_a_fresh_interpreter(code, env)
        assert default[0] == getattr(constants, variable)
        assert overridden[0] == SELF_HOSTED
        assert overridden[1] == HOSTED


def _constants_in_a_fresh_interpreter(code: str, env: dict[str, str]) -> list[str]:
    repo_root = Path(__file__).resolve().parents[1]
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=repo_root,
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.split()
