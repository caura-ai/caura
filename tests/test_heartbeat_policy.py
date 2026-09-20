"""Enablement policy for the anonymous heartbeat (docs/telemetry.md, "When it runs").

One test per row of the truth table, "any off wins", and the property the
whole feature hangs on: off means zero work. No task, no HTTP client, no
counter. mem0's PostHog integration still spawned threads with telemetry
disabled and it became two public GitHub issues; the last test here is what
keeps that from happening to us.
"""

from __future__ import annotations

import asyncio
import logging
from types import SimpleNamespace

import httpx
import pytest

from core_api.heartbeat import clients, policy
from core_api.heartbeat import sender as sender_mod
from core_api.heartbeat.policy import Disabled, Enabled, check_endpoint_url, evaluate

pytestmark = [pytest.mark.unit]

ENDPOINT = "https://telemetry.caura.ai/api/telemetry/heartbeat"


def _settings(**overrides):
    base = {
        "caura_telemetry": "on",
        "caura_telemetry_url": ENDPOINT,
        "gateway_shared_secret": None,
        "platform_llm_provider": "",
        "platform_embedding_provider": "",
        "is_standalone": True,
    }
    base.update(overrides)
    return SimpleNamespace(**base)


@pytest.fixture(autouse=True)
def _clean_sender_state():
    yield
    sender_mod._reset_for_tests()


# ── one test per row ─────────────────────────────────────────────────────


def test_default_is_on():
    decision = evaluate(_settings(), env={})
    assert decision == Enabled()
    assert decision.enabled is True
    assert decision.reason is None


@pytest.mark.parametrize("value", ["off", "0", "false", "OFF", " Off ", "False"])
def test_caura_telemetry_off_values(value):
    decision = evaluate(_settings(caura_telemetry=value), env={})
    assert decision == Disabled(policy.REASON_CAURA_TELEMETRY_OFF)
    assert decision.enabled is False


@pytest.mark.parametrize("value", ["on", "1", "true", "yes", "anything-else", ""])
def test_caura_telemetry_other_values_stay_on(value):
    assert evaluate(_settings(caura_telemetry=value), env={}) == Enabled()


@pytest.mark.parametrize("value", ["1", "true", "yes", "anything"])
def test_do_not_track_set(value):
    decision = evaluate(_settings(), env={"DO_NOT_TRACK": value})
    assert decision == Disabled(policy.REASON_DO_NOT_TRACK)


@pytest.mark.parametrize("value", ["0", "", "  "])
def test_do_not_track_zero_or_empty_is_unset(value):
    assert evaluate(_settings(), env={"DO_NOT_TRACK": value}) == Enabled()


@pytest.mark.parametrize("value", ["true", "1", "false", "github"])
def test_ci_set(value):
    assert evaluate(_settings(), env={"CI": value}) == Disabled(policy.REASON_CI)


def test_ci_empty_is_unset():
    assert evaluate(_settings(), env={"CI": ""}) == Enabled()


def test_enterprise_gateway_secret():
    decision = evaluate(_settings(gateway_shared_secret="s3cret"), env={})
    assert decision == Disabled(policy.REASON_ENTERPRISE_GATEWAY)


@pytest.mark.parametrize(
    "overrides",
    [
        {"platform_llm_provider": "vertex"},
        {"platform_embedding_provider": "openai"},
        {"platform_llm_provider": "openai", "platform_embedding_provider": "openai"},
    ],
)
def test_managed_platform_providers(overrides):
    decision = evaluate(_settings(**overrides), env={})
    assert decision == Disabled(policy.REASON_MANAGED_PLATFORM)


def test_pytest():
    decision = evaluate(_settings(), env={"PYTEST_CURRENT_TEST": "tests/x.py::t"})
    assert decision == Disabled(policy.REASON_PYTEST)


# ── the collector URL is a policy row ────────────────────────────────────


@pytest.mark.parametrize(
    "url",
    [
        "https://telemetry.caura.ai/api/telemetry/heartbeat",
        "https://collector.example.internal:8443/beat",
        "http://localhost:8010/api/telemetry/heartbeat",
        "http://127.0.0.1:8010/x",
        "http://[::1]:8010/x",
    ],
)
def test_check_endpoint_url_accepts(url):
    check_endpoint_url(url)
    assert evaluate(_settings(caura_telemetry_url=url), env={}) == Enabled()


@pytest.mark.parametrize(
    "url",
    [
        "http://telemetry.caura.ai/api/telemetry/heartbeat",
        "http://collector.example.internal/beat",
        "http://example.com/x",
        "http://host.docker.internal:8010/x",
        "ftp://localhost/x",
        "telemetry.caura.ai",
        "",
    ],
)
def test_invalid_endpoint_url_is_an_off_row(url):
    """A mistyped override is decided at boot, not refused silently at send time."""
    with pytest.raises(ValueError):
        check_endpoint_url(url)
    decision = evaluate(_settings(caura_telemetry_url=url), env={})
    assert decision == Disabled(policy.REASON_INVALID_ENDPOINT_URL)
    assert decision.enabled is False


def test_environment_rows_win_over_the_url_row():
    decision = evaluate(
        _settings(caura_telemetry_url="http://example.com/x"), env={"DO_NOT_TRACK": "1"}
    )
    assert decision.reason == policy.REASON_DO_NOT_TRACK


def test_real_environment_under_pytest_is_off():
    """The suite itself never dials out: ``os.environ`` carries PYTEST_CURRENT_TEST."""
    decision = evaluate(_settings())
    assert decision.enabled is False
    assert decision.reason in {
        policy.REASON_PYTEST,
        policy.REASON_CI,
        policy.REASON_DO_NOT_TRACK,
    }


# ── any off wins ─────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("overrides", "env"),
    [
        ({"caura_telemetry": "off"}, {"CI": "true", "DO_NOT_TRACK": "1"}),
        ({"gateway_shared_secret": "x"}, {"PYTEST_CURRENT_TEST": "t"}),
        ({"platform_llm_provider": "vertex"}, {"DO_NOT_TRACK": "1"}),
        ({}, {"CI": "1", "PYTEST_CURRENT_TEST": "t"}),
        (
            {
                "caura_telemetry": "off",
                "gateway_shared_secret": "x",
                "platform_embedding_provider": "openai",
            },
            {"CI": "1", "DO_NOT_TRACK": "1", "PYTEST_CURRENT_TEST": "t"},
        ),
    ],
)
def test_any_off_wins(overrides, env):
    decision = evaluate(_settings(**overrides), env=env)
    assert isinstance(decision, Disabled)
    assert decision.enabled is False
    assert decision.reason is not None


def test_first_off_row_supplies_the_reason():
    """Rows are checked in table order, so the reason is deterministic."""
    decision = evaluate(
        _settings(caura_telemetry="off", gateway_shared_secret="x"),
        env={"CI": "1", "DO_NOT_TRACK": "1"},
    )
    assert decision.reason == policy.REASON_CAURA_TELEMETRY_OFF
    decision = evaluate(_settings(gateway_shared_secret="x"), env={"CI": "1"})
    assert decision.reason == policy.REASON_CI


def test_reason_strings_are_the_documented_set():
    assert {
        policy.REASON_CAURA_TELEMETRY_OFF,
        policy.REASON_DO_NOT_TRACK,
        policy.REASON_CI,
        policy.REASON_ENTERPRISE_GATEWAY,
        policy.REASON_MANAGED_PLATFORM,
        policy.REASON_PYTEST,
        policy.REASON_INVALID_ENDPOINT_URL,
    } == {
        "caura_telemetry_off",
        "do_not_track",
        "ci",
        "enterprise_gateway",
        "managed_platform",
        "pytest",
        "invalid_endpoint_url",
    }


# ── off means zero work ──────────────────────────────────────────────────


def test_off_means_zero_work(monkeypatch, caplog):
    """No task, no client, no DNS, no counter when the policy says off."""

    def _boom(*_a, **_k):
        raise AssertionError("must not be called while telemetry is off")

    monkeypatch.setattr(httpx, "AsyncClient", _boom)
    monkeypatch.setattr(asyncio, "create_task", _boom)
    monkeypatch.setattr("core_api.tasks.track_task", _boom)

    with caplog.at_level(logging.INFO, logger="core_api.heartbeat"):
        decision = sender_mod.install(_settings(caura_telemetry="off"))

    assert decision == Disabled(policy.REASON_CAURA_TELEMETRY_OFF)
    assert sender_mod.get_sender() is None
    assert sender_mod.get_decision() == decision
    assert clients.is_enabled() is False
    clients.record("caura-client-python/1.0.2")
    clients.record_mcp()
    assert all(v == 0 for v in clients.snapshot().values())
    assert "[telemetry] anonymous heartbeat OFF (caura_telemetry_off)." in caplog.text


def test_boot_line_on(caplog):
    with caplog.at_level(logging.INFO, logger="core_api.heartbeat"):
        sender_mod.log_boot_line(Enabled(), _settings())
    line = caplog.text
    assert "[telemetry] anonymous heartbeat ON" in line
    assert "one ping a day to telemetry.caura.ai" in line
    assert "Disable: CAURA_TELEMETRY=off or DO_NOT_TRACK=1" in line
    assert "Inspect: GET /api/v1/telemetry" in line
    assert "https://github.com/caura-ai/caura/blob/main/docs/telemetry.md" in line


def test_boot_line_off_names_the_reason(caplog):
    with caplog.at_level(logging.INFO, logger="core_api.heartbeat"):
        sender_mod.log_boot_line(Disabled(policy.REASON_DO_NOT_TRACK), _settings())
    assert "[telemetry] anonymous heartbeat OFF (do_not_track)." in caplog.text


def test_boot_line_uses_the_configured_host(caplog):
    with caplog.at_level(logging.INFO, logger="core_api.heartbeat"):
        sender_mod.log_boot_line(
            Enabled(), _settings(caura_telemetry_url="http://localhost:9/x")
        )
    assert "one ping a day to localhost" in caplog.text


def test_boot_line_for_a_bad_url_is_a_warning_naming_the_url(caplog):
    with caplog.at_level(logging.INFO, logger="core_api.heartbeat"):
        sender_mod.log_boot_line(
            Disabled(policy.REASON_INVALID_ENDPOINT_URL),
            _settings(caura_telemetry_url="http://example.com/x"),
        )
    [record] = caplog.records
    assert record.levelno == logging.WARNING
    line = record.getMessage()
    assert "anonymous heartbeat OFF (invalid_endpoint_url)" in line
    assert "'http://example.com/x'" in line
    assert "must be https://" in line
    assert "ON" not in line.replace("OFF", "")


def test_boot_line_follower(caplog):
    with caplog.at_level(logging.INFO, logger="core_api.heartbeat"):
        sender_mod.log_boot_line(Enabled(), _settings(), role="follower")
    assert "anonymous heartbeat follower" in caplog.text
    assert "anonymous heartbeat ON" not in caplog.text


# ── install(): roles and the state-dir fallback ──────────────────────────


def _install_on(monkeypatch, caplog, settings):
    """Run ``install`` with the policy forced ON and the loop tasks stubbed."""
    monkeypatch.setattr(sender_mod, "evaluate", lambda _s: Enabled())

    def _track(coro):
        coro.close()
        return SimpleNamespace(cancel=lambda: None)

    monkeypatch.setattr("core_api.tasks.track_task", _track)
    with caplog.at_level(logging.INFO, logger="core_api.heartbeat"):
        return sender_mod.install(settings)


def test_install_bad_url_is_off_at_boot(monkeypatch, caplog):
    """End to end: the policy says off, the WARNING prints, nothing starts."""

    def _boom(*_a, **_k):
        raise AssertionError("must not be called for a refused URL")

    monkeypatch.setattr("core_api.tasks.track_task", _boom)
    monkeypatch.setattr(sender_mod.SharedState, "open", _boom)
    # The real environment says off for its own reasons; clear them so the
    # URL row is the one that decides.
    for name in ("PYTEST_CURRENT_TEST", "CI", "DO_NOT_TRACK"):
        monkeypatch.delenv(name, raising=False)
    with caplog.at_level(logging.INFO, logger="core_api.heartbeat"):
        decision = sender_mod.install(
            _settings(caura_telemetry_url="http://example.com/x")
        )
    assert decision == Disabled(policy.REASON_INVALID_ENDPOINT_URL)
    assert sender_mod.get_sender() is None
    assert clients.is_enabled() is False
    assert any(
        r.levelno == logging.WARNING and "invalid_endpoint_url" in r.getMessage()
        for r in caplog.records
    )


def test_install_elects_a_leader_and_prints_one_on_line(monkeypatch, caplog, tmp_path):
    state_dir = tmp_path / "caura-heartbeat"
    decision = _install_on(
        monkeypatch, caplog, _settings(caura_telemetry_state_dir=str(state_dir))
    )
    assert decision.enabled
    sender = sender_mod.get_sender()
    assert sender is not None
    assert sender.role == "leader"
    assert (state_dir / "leader.lock").exists()
    assert caplog.text.count("anonymous heartbeat ON") == 1
    assert "follower" not in caplog.text
    assert not any(r.levelno >= logging.WARNING for r in caplog.records)


def test_install_second_worker_is_a_follower(monkeypatch, caplog, tmp_path):
    """Another worker of the same container (same dir, lock taken) prints the follower line."""
    state_dir = tmp_path / "caura-heartbeat"
    other_worker = sender_mod.SharedState.open(state_dir)
    assert other_worker is not None and other_worker.try_acquire_leader()
    try:
        _install_on(
            monkeypatch, caplog, _settings(caura_telemetry_state_dir=str(state_dir))
        )
        sender = sender_mod.get_sender()
        assert sender is not None
        assert sender.role == "follower"
        assert "anonymous heartbeat follower" in caplog.text
        assert "anonymous heartbeat ON" not in caplog.text
    finally:
        other_worker.release_leader()


def test_install_falls_back_to_single_process_when_the_dir_is_unwritable(
    monkeypatch, caplog, tmp_path
):
    blocker = tmp_path / "a-file"
    blocker.write_text("")
    _install_on(
        monkeypatch, caplog, _settings(caura_telemetry_state_dir=str(blocker / "state"))
    )
    sender = sender_mod.get_sender()
    assert sender is not None
    assert sender.role == "single"
    assert sender.shared is None
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert "not writable" in warnings[0].getMessage()
    assert "CAURA_TELEMETRY_STATE_DIR" in warnings[0].getMessage()
    assert "anonymous heartbeat ON" in caplog.text


def test_install_with_an_empty_state_dir_is_single_process_by_choice(
    monkeypatch, caplog
):
    _install_on(monkeypatch, caplog, _settings(caura_telemetry_state_dir=""))
    sender = sender_mod.get_sender()
    assert sender is not None and sender.role == "single"
    assert not any(r.levelno >= logging.WARNING for r in caplog.records)
