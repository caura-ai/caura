"""Unit tests for the reserved-agent-id write guard (`main` identity fix, Phase 1).

Covers `services.agent_identity.reserved_write_refusal` / `enforce_reserved_write_id`
across the allow/warn/reject policy ladder, the escape hatch (a unique agent_id
always passes), and the standalone-safety carve-out for `mcp-agent`.
"""

import pytest

from core_api.config import settings
from core_api.services.agent_identity import (
    ReservedAgentIdError,
    enforce_reserved_write_id,
    reserved_write_refusal,
)


@pytest.fixture
def policy(monkeypatch):
    """Set settings.reserved_agent_id_policy for a test."""

    def _set(value: str):
        monkeypatch.setattr(settings, "reserved_agent_id_policy", value)

    return _set


@pytest.mark.parametrize(
    "value,refuses",
    [("allow", False), ("warn", False), ("reject", True)],
)
def test_main_per_policy(policy, value, refuses):
    policy(value)
    assert (reserved_write_refusal("main") is not None) is refuses


@pytest.mark.parametrize("value", ["allow", "warn", "reject"])
@pytest.mark.parametrize("agent_id", ["webclaw", "main-7765cca229ab", "quantclaw-prod"])
def test_unique_id_always_passes_escape_hatch(policy, value, agent_id):
    """Any non-reserved id proceeds under every policy — including the plugin
    default `main-<install_id>`. This is the escape hatch the 409 advertises."""
    policy(value)
    assert reserved_write_refusal(agent_id) is None


def test_mcp_agent_not_unconditionally_reserved(policy):
    """`mcp-agent` is legitimate on the standalone path and is guarded
    *conditionally* by the MCP gateway guard — never by this unconditional
    write-path guard. Reserving it here would regress standalone."""
    policy("reject")
    assert reserved_write_refusal("mcp-agent") is None


@pytest.mark.parametrize("agent_id", [None, ""])
def test_missing_id_passes_here(policy, agent_id):
    """A missing id is the upstream `if not data.agent_id: raise ValueError`
    contract's job, not this guard's — return None (no false 409)."""
    policy("reject")
    assert reserved_write_refusal(agent_id) is None


def test_reject_raises_domain_error_with_actionable_guidance(policy):
    policy("reject")
    with pytest.raises(ReservedAgentIdError) as exc:
        enforce_reserved_write_id("main")
    detail = str(exc.value)
    # The message must tell the agent where its identity comes from.
    assert "MEMCLAW_AGENT_ID" in detail
    assert "install.json" in detail
    assert 'main-<install_id>' in detail


@pytest.mark.parametrize("value", ["allow", "warn"])
def test_no_raise_under_allow_or_warn(policy, value):
    policy(value)
    enforce_reserved_write_id("main")  # must not raise


def test_warn_emits_structured_log(policy, caplog):
    policy("warn")
    with caplog.at_level("WARNING"):
        assert reserved_write_refusal("main") is None
    assert any(r.message == "reserved_agent_write" for r in caplog.records)
