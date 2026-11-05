from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from common.llm.providers.openai import OpenAILLMProvider
from common.llm.registry import get_llm_provider, reset_provider_cache

pytestmark = pytest.mark.unit

_KEY = "sk-test-key-12345"


@pytest.fixture(autouse=True)
def _clean_cache():
    reset_provider_cache()
    yield
    reset_provider_cache()


def _openai(monkeypatch, *, key=_KEY, model="gpt-4o-mini", send_temp="true"):
    monkeypatch.setenv("OPENAI_API_KEY", key)
    if send_temp is None:
        monkeypatch.delenv("OPENAI_CHAT_SEND_TEMPERATURE", raising=False)
    else:
        monkeypatch.setenv("OPENAI_CHAT_SEND_TEMPERATURE", send_temp)
    return get_llm_provider(
        "openai",
        SimpleNamespace(openai_api_key=key, enrichment_model=model),
        model_override=model,
    )


@pytest.mark.parametrize("val", ["yes", "1", "trun", "0", "TRU", "enabled"])
def test_registry_rejects_non_canonical_send_temperature_value(
    monkeypatch: pytest.MonkeyPatch,
    val: str,
) -> None:
    """Strict bool parsing for OPENAI_CHAT_SEND_TEMPERATURE: only 'true'/'false' accepted."""
    with pytest.raises(
        ValueError,
        match=r"OPENAI_CHAT_SEND_TEMPERATURE=.*must be 'true' or 'false'",
    ):
        _openai(monkeypatch, send_temp=val)


@pytest.mark.parametrize("val", ["true", "TRUE", "True", "false", "FALSE", "False"])
def test_registry_accepts_case_insensitive_canonical_send_temperature(
    monkeypatch: pytest.MonkeyPatch,
    val: str,
) -> None:
    p = _openai(monkeypatch, send_temp=val)
    expected = val.lower() == "true"
    assert p._send_temperature is expected


def test_registry_defaults_send_temperature_to_true(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    p = _openai(monkeypatch, send_temp=None)
    assert p._send_temperature is True


def test_registry_cache_key_includes_send_temperature(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    a = _openai(monkeypatch, send_temp="true")
    b = _openai(monkeypatch, send_temp="false")
    assert a is not b


@pytest.mark.asyncio
async def test_complete_json_omits_temperature_when_send_temperature_false():
    p = OpenAILLMProvider(
        api_key=_KEY,
        model="reasoning-model",
        send_temperature=False,
    )
    fake_choice = SimpleNamespace(
        message=SimpleNamespace(content='{"result": "ok"}'),
        finish_reason="stop",
    )
    fake_response = SimpleNamespace(
        choices=[fake_choice],
        usage=SimpleNamespace(prompt_tokens=10, completion_tokens=5),
    )
    mock_create = AsyncMock(return_value=fake_response)

    with patch.object(p._client.chat.completions, "create", mock_create):
        await p.complete_json("test prompt", temperature=0.7)

    assert mock_create.await_count == 1
    call_kwargs = mock_create.call_args.kwargs
    assert "temperature" not in call_kwargs


@pytest.mark.asyncio
async def test_complete_json_sends_temperature_when_send_temperature_true():
    p = OpenAILLMProvider(
        api_key=_KEY,
        model="standard-model",
        send_temperature=True,
    )
    fake_choice = SimpleNamespace(
        message=SimpleNamespace(content='{"result": "ok"}'),
        finish_reason="stop",
    )
    fake_response = SimpleNamespace(
        choices=[fake_choice],
        usage=SimpleNamespace(prompt_tokens=10, completion_tokens=5),
    )
    mock_create = AsyncMock(return_value=fake_response)

    with patch.object(p._client.chat.completions, "create", mock_create):
        await p.complete_json("test prompt", temperature=0.7)

    assert mock_create.await_count == 1
    call_kwargs = mock_create.call_args.kwargs
    assert call_kwargs.get("temperature") == 0.7


@pytest.mark.asyncio
async def test_complete_text_omits_temperature_when_send_temperature_false():
    p = OpenAILLMProvider(
        api_key=_KEY,
        model="reasoning-model",
        send_temperature=False,
    )
    fake_choice = SimpleNamespace(
        message=SimpleNamespace(content="ok text"),
        finish_reason="stop",
    )
    fake_response = SimpleNamespace(
        choices=[fake_choice],
        usage=SimpleNamespace(prompt_tokens=10, completion_tokens=5),
    )
    mock_create = AsyncMock(return_value=fake_response)

    with patch.object(p._client.chat.completions, "create", mock_create):
        await p.complete_text("test prompt", temperature=0.5)

    assert mock_create.await_count == 1
    call_kwargs = mock_create.call_args.kwargs
    assert "temperature" not in call_kwargs


@pytest.mark.asyncio
async def test_complete_text_sends_temperature_when_send_temperature_true():
    p = OpenAILLMProvider(
        api_key=_KEY,
        model="standard-model",
        send_temperature=True,
    )
    fake_choice = SimpleNamespace(
        message=SimpleNamespace(content="ok text"),
        finish_reason="stop",
    )
    fake_response = SimpleNamespace(
        choices=[fake_choice],
        usage=SimpleNamespace(prompt_tokens=10, completion_tokens=5),
    )
    mock_create = AsyncMock(return_value=fake_response)

    with patch.object(p._client.chat.completions, "create", mock_create):
        await p.complete_text("test prompt", temperature=0.5)

    assert mock_create.await_count == 1
    call_kwargs = mock_create.call_args.kwargs
    assert call_kwargs.get("temperature") == 0.5
