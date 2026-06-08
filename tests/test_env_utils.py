"""Unit tests for common.env_utils helpers (CAURA-627)."""

from __future__ import annotations

import pytest

from common.env_utils import clamp_keepalive, read_int_env


def test_unset_returns_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CAURA_TEST_KEY", raising=False)
    assert read_int_env("CAURA_TEST_KEY", 42) == 42


def test_valid_int_returns_parsed_value(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CAURA_TEST_KEY", "100")
    assert read_int_env("CAURA_TEST_KEY", 42) == 100


def test_invalid_string_falls_back(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("CAURA_TEST_KEY", "200abc")
    assert read_int_env("CAURA_TEST_KEY", 42) == 42
    err = capsys.readouterr().err
    assert "CAURA_TEST_KEY" in err
    assert "not a valid int" in err
    assert "42" in err  # default surfaced in the warning


def test_unit_suffix_falls_back(monkeypatch: pytest.MonkeyPatch) -> None:
    """``"25s"`` is the realistic copy-paste-from-config bug shape."""
    monkeypatch.setenv("CAURA_TEST_KEY", "25s")
    assert read_int_env("CAURA_TEST_KEY", 42) == 42


def test_zero_falls_back(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """``int("0")`` parses cleanly but ``httpx.Limits(max_connections=0)``
    is a semaphore with zero permits — every request blocks forever.
    The guard rejects values < 1 explicitly (CAURA-627 round-2)."""
    monkeypatch.setenv("CAURA_TEST_KEY", "0")
    assert read_int_env("CAURA_TEST_KEY", 42) == 42
    err = capsys.readouterr().err
    assert ">= 1" in err


def test_negative_falls_back(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("CAURA_TEST_KEY", "-5")
    assert read_int_env("CAURA_TEST_KEY", 42) == 42
    err = capsys.readouterr().err
    assert ">= 1" in err


def test_one_is_accepted(monkeypatch: pytest.MonkeyPatch) -> None:
    """Boundary: 1 is the smallest valid value (httpx.Limits accepts it)."""
    monkeypatch.setenv("CAURA_TEST_KEY", "1")
    assert read_int_env("CAURA_TEST_KEY", 42) == 1


def test_whitespace_around_int_falls_back(monkeypatch: pytest.MonkeyPatch) -> None:
    """``int("  100  ")`` succeeds in Python — the env var would parse and
    return 100, not the default. This documents that surrounding
    whitespace is tolerated rather than rejected."""
    monkeypatch.setenv("CAURA_TEST_KEY", "  100  ")
    assert read_int_env("CAURA_TEST_KEY", 42) == 100


# ─── clamp_keepalive ──────────────────────────────────────────────────


def test_clamp_keepalive_passthrough_when_within_max(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """If keepalive <= max, return keepalive unchanged with no warning."""
    assert clamp_keepalive(max_connections=200, max_keepalive=50) == 50
    assert capsys.readouterr().err == ""


def test_clamp_keepalive_equal_is_passthrough(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Boundary: keepalive == max passes through silently (the docs only
    promise warning when keepalive STRICTLY exceeds max, matching
    httpx's actual clamp behaviour)."""
    assert clamp_keepalive(max_connections=100, max_keepalive=100) == 100
    assert capsys.readouterr().err == ""


def test_clamp_keepalive_warns_and_clamps_when_above_max(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """If an operator sets keepalive > max (typically by env var), we
    return ``max_connections`` and emit a stderr warning naming both
    values + the env var so they can self-diagnose."""
    assert clamp_keepalive(max_connections=100, max_keepalive=500) == 100
    err = capsys.readouterr().err
    assert "OPENAI_HTTPX_MAX_KEEPALIVE_CONNECTIONS" in err
    assert "OPENAI_HTTPX_MAX_CONNECTIONS" in err
    assert "500" in err
    assert "100" in err
    assert "clamping" in err


def test_clamp_keepalive_uses_custom_var_names_in_warning(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A future provider with different env-var names can override the
    var names in the warning. This regression-guards the kwargs path so
    a refactor that drops them surfaces immediately."""
    assert (
        clamp_keepalive(
            max_connections=10,
            max_keepalive=99,
            max_connections_var="ANTHROPIC_HTTPX_MAX_CONNECTIONS",
            max_keepalive_var="ANTHROPIC_HTTPX_MAX_KEEPALIVE",
        )
        == 10
    )
    err = capsys.readouterr().err
    assert "ANTHROPIC_HTTPX_MAX_KEEPALIVE" in err
    assert "ANTHROPIC_HTTPX_MAX_CONNECTIONS" in err
    # Verify the OpenAI defaults DON'T leak through when overrides are set.
    assert "OPENAI_HTTPX" not in err
