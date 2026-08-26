"""C36 — provider keys must never leave the server readable.

``GET /settings``'s docstring promised "(API keys masked)" while
``get_settings_for_display`` returned the stored bytes verbatim — any
tenant-scoped credential (a low-trust agent's included) could read the
tenant's provider keys, and ``PUT`` echoed the same tree.

Contract under test:
- display replaces every non-empty ``api_keys`` value with the constant
  ``****`` — nothing in the display tree derives from the stored key;
- the write path treats a masked value as "unchanged" and drops it (the
  dashboard sends the whole ``api_keys`` group when any one key is edited,
  so unedited siblings arrive masked on every save);
- an explicitly empty value still passes through (that is how a key is
  cleared);
- the raw path (``get_raw_settings`` / ``ResolvedConfig``) is untouched —
  masking is a display concern only.
"""

from unittest.mock import AsyncMock

import pytest
from core_api.services import organization_settings as os_svc

pytestmark = pytest.mark.unit

_REAL = "sk-proj-abcdef1234567890wxyz"
_MASK = "****"


@pytest.fixture(autouse=True)
def _reset_cache():
    os_svc._settings_cache.clear()
    yield
    os_svc._settings_cache.clear()


def _stub_raw(monkeypatch, raw):
    async def _get_raw(tenant_id):
        return raw

    monkeypatch.setattr(os_svc, "get_raw_settings", _get_raw)


async def test_display_masks_api_key_values(monkeypatch):
    _stub_raw(monkeypatch, {"api_keys": {"openai_api_key": _REAL}})
    out = await os_svc.get_settings_for_display("t1")
    assert out["api_keys"]["openai_api_key"] == _MASK
    assert _REAL not in str(out)


async def test_mask_contains_no_bytes_of_the_key(monkeypatch):
    """The mask is a constant — no byte of the real key may appear in the
    masked value (a last-4 slice or hash fingerprint is key-derived)."""
    _stub_raw(monkeypatch, {"api_keys": {"gemini_api_key": "abcd"}})
    out = await os_svc.get_settings_for_display("t1")
    masked = out["api_keys"]["gemini_api_key"]
    assert masked == "****"
    assert "abcd" not in masked


async def test_display_leaves_empty_and_absent_values_alone(monkeypatch):
    _stub_raw(monkeypatch, {"api_keys": {"openai_api_key": ""}})
    out = await os_svc.get_settings_for_display("t1")
    assert out["api_keys"]["openai_api_key"] == ""
    _stub_raw(monkeypatch, {})
    out = await os_svc.get_settings_for_display("t1")
    assert out["api_keys"] == {}


def _stub_update(monkeypatch):
    """Capture what update_settings forwards to storage; echo it back."""
    captured = {}

    async def _update_org_settings(tenant_id, new_settings, *, changed_by=None):
        captured["payload"] = new_settings
        return {"settings": new_settings, "changed": True}

    sc = os_svc.get_storage_client()
    monkeypatch.setattr(sc, "update_org_settings", _update_org_settings)
    monkeypatch.setattr(os_svc, "get_event_bus", lambda: AsyncMock())
    return captured


async def test_update_drops_masked_values_keeps_edited_one(monkeypatch):
    captured = _stub_update(monkeypatch)
    await os_svc.update_settings(
        "t1",
        {"api_keys": {"openai_api_key": _MASK, "anthropic_api_key": _REAL}},
    )
    assert captured["payload"]["api_keys"] == {"anthropic_api_key": _REAL}


async def test_update_drops_group_when_every_value_is_masked(monkeypatch):
    captured = _stub_update(monkeypatch)
    await os_svc.update_settings(
        "t1",
        {
            "api_keys": {"openai_api_key": _MASK},
            "dedup": {"semantic_dedup_enabled": True},
        },
    )
    assert "api_keys" not in captured["payload"]
    assert captured["payload"]["dedup"] == {"semantic_dedup_enabled": True}


async def test_update_lets_an_explicit_clear_through(monkeypatch):
    captured = _stub_update(monkeypatch)
    await os_svc.update_settings("t1", {"api_keys": {"openai_api_key": ""}})
    assert captured["payload"]["api_keys"] == {"openai_api_key": ""}


async def test_update_response_is_masked(monkeypatch):
    _stub_update(monkeypatch)
    out = await os_svc.update_settings("t1", {"api_keys": {"openai_api_key": _REAL}})
    assert out["api_keys"]["openai_api_key"] == _MASK
    assert _REAL not in str(out)


async def test_raw_path_still_returns_real_keys(monkeypatch):
    """ResolvedConfig and the raw read must keep the real value — masking is
    display-only, not storage-side."""
    _stub_raw(monkeypatch, {"api_keys": {"openai_api_key": _REAL}})
    raw = await os_svc.get_raw_settings("t1")
    assert raw["api_keys"]["openai_api_key"] == _REAL
    cfg = os_svc.ResolvedConfig({"api_keys": {"openai_api_key": _REAL}})
    assert cfg.openai_api_key == _REAL
