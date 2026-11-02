"""Startup checks for settings encryption use synthetic keys only."""

from __future__ import annotations

import logging

import pytest

from core_api.app import _validate_startup_settings
from tests.test_production_startup_guards import _prod

pytestmark = pytest.mark.unit


def test_production_refuses_passphrase_key(monkeypatch):
    monkeypatch.delenv("TESTING", raising=False)
    with pytest.raises(RuntimeError, match="Fernet"):
        _validate_startup_settings(_prod(settings_encryption_key="short-passphrase"))


@pytest.mark.parametrize("key", [None, "", "short-passphrase"])
def test_nonproduction_warns_when_encryption_is_unavailable(key, caplog):
    with caplog.at_level(logging.WARNING):
        _validate_startup_settings(
            _prod(environment="development", settings_encryption_key=key)
        )
    assert "SETTINGS_ENCRYPTION_KEY" in caplog.text
    assert "short-passphrase" not in caplog.text


@pytest.mark.parametrize("key", [None, "", "short-passphrase"])
def test_compose_can_require_encryption_at_service_startup(key, monkeypatch):
    monkeypatch.setenv("REQUIRE_SETTINGS_ENCRYPTION", "true")
    with pytest.raises(RuntimeError, match="Fernet"):
        _validate_startup_settings(
            _prod(environment="development", settings_encryption_key=key)
        )


@pytest.mark.parametrize(
    "environment,required", [("production", False), ("development", True)]
)
def test_startup_refuses_invalid_previous_key(environment, required, monkeypatch):
    monkeypatch.setenv("SETTINGS_ENCRYPTION_PREVIOUS_KEYS", "synthetic-invalid-reader")
    monkeypatch.setenv("REQUIRE_SETTINGS_ENCRYPTION", str(required).lower())
    with pytest.raises(RuntimeError, match="Fernet") as caught:
        _validate_startup_settings(_prod(environment=environment))
    assert "synthetic-invalid-reader" not in str(caught.value)


def test_development_warns_for_invalid_previous_key(monkeypatch, caplog):
    monkeypatch.setenv("SETTINGS_ENCRYPTION_PREVIOUS_KEYS", "synthetic-invalid-reader")
    with caplog.at_level(logging.WARNING):
        _validate_startup_settings(_prod(environment="development"))
    assert "encryption is unavailable" in caplog.text
    assert "synthetic-invalid-reader" not in caplog.text


@pytest.mark.parametrize("key", [None, "", "short-passphrase"])
def test_pubsub_startup_requires_encryption_outside_production(key, monkeypatch):
    monkeypatch.setenv("EVENT_BUS_BACKEND", "pubsub")
    with pytest.raises(RuntimeError, match="Fernet"):
        _validate_startup_settings(
            _prod(environment="development", settings_encryption_key=key)
        )
