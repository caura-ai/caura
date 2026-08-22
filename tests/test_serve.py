"""Unit tests for common.serve — the uvicorn launcher wrapper.

These pin the launcher's contract without starting a server: it must read the
service's own settings object, configure structlog from those fields, and start
uvicorn with ``log_config=None`` (so the uvicorn.* loggers propagate to the
structlog root handler — see the module docstring in common/serve.py).
"""

from __future__ import annotations

import json
import types
from unittest import mock

import pytest

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
