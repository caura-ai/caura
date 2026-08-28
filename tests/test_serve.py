"""Unit tests for common.serve — the uvicorn launcher wrapper.

These pin the launcher's contract without starting a server: it must read the
service's own settings object, configure structlog from those fields, and start
uvicorn with ``log_config=None`` (so the uvicorn.* loggers propagate to the
structlog root handler — see the module docstring in common/serve.py).
"""

from __future__ import annotations

import inspect
import json
import types
from unittest import mock

import pytest
import uvicorn

import common.serve as serve


def test_load_returns_module_attribute() -> None:
    assert serve._load("json:dumps") is json.dumps


@pytest.mark.parametrize("bad", ["json", "json:", ""])
def test_load_rejects_paths_missing_module_or_attr(bad: str) -> None:
    with pytest.raises(ValueError):
        serve._load(bad)


def test_main_configures_logging_from_settings_and_disables_uvicorn_log_config(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_settings = types.SimpleNamespace(
        environment="production",
        log_level="INFO",
        log_format_json=True,
        log_file="",
    )
    monkeypatch.setattr(serve, "_load", lambda _path: fake_settings)
    configure = mock.MagicMock()
    run = mock.MagicMock()
    monkeypatch.setattr(serve, "configure_logging", configure)
    monkeypatch.setattr(serve.uvicorn, "run", run)

    serve.main(
        [
            "core_api.app:app",
            "--settings",
            "core_api.config:settings",
            "--host",
            "0.0.0.0",
            "--port",
            "8000",
            "--workers",
            "2",
            "--timeout-keep-alive",
            "65",
        ]
    )

    # Logging configured from the settings object's fields; empty log_file -> None.
    configure.assert_called_once_with("production", "INFO", json_logs=True, log_file=None)

    # Server started with uvicorn's own logging config disabled.
    run.assert_called_once()
    args, kwargs = run.call_args
    assert args == ("core_api.app:app",)
    assert kwargs["log_config"] is None
    assert kwargs["host"] == "0.0.0.0"
    assert kwargs["port"] == 8000
    assert kwargs["workers"] == 2
    assert kwargs["timeout_keep_alive"] == 65

    # Absent from argv -> uvicorn's own default, forwarded explicitly rather
    # than left to uvicorn so the launcher's contract is the same either way.
    assert kwargs["timeout_worker_healthcheck"] == 5


def test_main_forwards_timeout_worker_healthcheck(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The supervisor healthcheck timeout must be reachable from a service CMD.

    Pre-this-test the parser had a fixed flag list and rejected the flag, so a
    Dockerfile that passed it made argparse exit 2 and the container never
    started. Raising it above uvicorn's 5s default is what keeps a slow-to-pong
    worker from being SIGKILLed mid-lifespan, dropping its shutdown hooks (and
    with them this service's ephemeral Pub/Sub broadcast subscriptions).
    """
    fake_settings = types.SimpleNamespace(
        environment="production",
        log_level="INFO",
        log_format_json=True,
        log_file="",
    )
    monkeypatch.setattr(serve, "_load", lambda _path: fake_settings)
    monkeypatch.setattr(serve, "configure_logging", mock.MagicMock())
    run = mock.MagicMock()
    monkeypatch.setattr(serve.uvicorn, "run", run)

    serve.main(
        [
            "core_api.app:app",
            "--settings",
            "core_api.config:settings",
            "--port",
            "8000",
            "--workers",
            "2",
            "--timeout-worker-healthcheck",
            "30",
        ]
    )

    _args, kwargs = run.call_args
    assert kwargs["timeout_worker_healthcheck"] == 30


def test_uvicorn_really_accepts_the_healthcheck_kwarg() -> None:
    """Guard the one thing the mocked tests above structurally cannot check.

    Every other test here monkeypatches ``serve.uvicorn.run`` with a
    MagicMock, and a MagicMock accepts any keyword silently. So if uvicorn
    ever renames or drops this parameter, those tests keep passing and the
    only place it fails is the deployed container — ``TypeError`` on every
    startup, i.e. the service does not come up at all. Assert against the
    real signature so that failure lands in CI instead.

    ``timeout_worker_healthcheck`` landed in uvicorn 0.37.0; it is absent in
    0.36.0. That boundary is why requirements.txt floors at >=0.37 — below it
    the declared range would include versions common/serve.py cannot run on.
    """
    assert (
        "timeout_worker_healthcheck"
        in inspect.signature(uvicorn.Config.__init__).parameters
    )
    assert "timeout_worker_healthcheck" in inspect.signature(uvicorn.run).parameters


def _run_main_capturing_uvicorn(
    monkeypatch: pytest.MonkeyPatch,
) -> mock.MagicMock:
    """Drive serve.main() with everything external mocked; return the run mock."""
    fake_settings = types.SimpleNamespace(
        environment="production",
        log_level="INFO",
        log_format_json=True,
        log_file="",
    )
    monkeypatch.setattr(serve, "_load", lambda _path: fake_settings)
    monkeypatch.setattr(serve, "configure_logging", mock.MagicMock())
    run = mock.MagicMock()
    monkeypatch.setattr(serve.uvicorn, "run", run)

    serve.main(
        [
            "core_api.app:app",
            "--settings",
            "core_api.config:settings",
            "--host",
            "0.0.0.0",
            "--port",
            "8000",
            "--workers",
            "2",
            "--timeout-keep-alive",
            "65",
        ]
    )
    return run


def test_access_log_defaults_on_when_env_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Absent env var must leave access logging ON.

    This default is the whole reason the knob is opt-out. OSS, on-prem and
    local runs have neither an APM span nor Cloud Run's httpRequest entry, so
    the uvicorn access line is their only per-request visibility. Silencing it
    by default would take that away from every self-hosted install to solve a
    cost problem that only the managed deploy has.
    """
    monkeypatch.delenv("UVICORN_ACCESS_LOG", raising=False)

    _args, kwargs = _run_main_capturing_uvicorn(monkeypatch).call_args

    assert kwargs["access_log"] is True


@pytest.mark.parametrize("raw", ["0", "false", "FALSE", "No", " off ", "OFF"])
def test_access_log_off_for_falsy_spellings(
    monkeypatch: pytest.MonkeyPatch, raw: str
) -> None:
    """The managed deploy sets this; accept the spellings people actually type.

    Case and surrounding whitespace are normalised because this value is
    threaded through a comma-joined --update-env-vars string in the deploy
    workflows, where a stray space is easy to introduce and impossible to see
    in review.
    """
    monkeypatch.setenv("UVICORN_ACCESS_LOG", raw)

    _args, kwargs = _run_main_capturing_uvicorn(monkeypatch).call_args

    assert kwargs["access_log"] is False


@pytest.mark.parametrize("raw", ["1", "true", "yes", "on", "", "maybe"])
def test_access_log_on_for_everything_else(
    monkeypatch: pytest.MonkeyPatch, raw: str
) -> None:
    """Anything not clearly falsy keeps logging.

    Including junk like "maybe": the failure modes are not symmetric. Reading
    an unrecognised value as "off" silently drops request visibility and looks
    identical to a healthy quiet service, whereas reading it as "on" costs
    some log volume and is immediately obvious. Fail toward the loud side.
    """
    monkeypatch.setenv("UVICORN_ACCESS_LOG", raw)

    _args, kwargs = _run_main_capturing_uvicorn(monkeypatch).call_args

    assert kwargs["access_log"] is True


def test_uvicorn_really_accepts_the_access_log_kwarg() -> None:
    """Same guard as the healthcheck kwarg above, for the same reason.

    The tests above assert against a MagicMock, which swallows any keyword.
    If uvicorn ever renames or drops ``access_log``, they all keep passing
    and the only place it surfaces is a TypeError on container startup.
    """
    assert "access_log" in inspect.signature(uvicorn.Config.__init__).parameters
    assert "access_log" in inspect.signature(uvicorn.run).parameters
