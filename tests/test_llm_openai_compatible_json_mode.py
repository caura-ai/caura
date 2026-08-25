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
        assert out["properties"]["entities"]["items"] == {"$ref": "#/$defs/Entity"}
        assert out["properties"]["note"]["anyOf"] == [
            {"type": "string"},
            {"type": "null"},
        ]

    def test_does_not_modify_its_input(self):
        before = json.dumps(SCHEMA, sort_keys=True)
        _strict_schema(SCHEMA)
        assert json.dumps(SCHEMA, sort_keys=True) == before


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
