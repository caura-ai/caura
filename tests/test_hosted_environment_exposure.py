"""A hosted deployment does not expose debugging surfaces (M-78, L-68).

Staging and every sandbox run core-api as ``ENVIRONMENT=sandbox``. The catch-all
500 handler returned the exception's message and class whenever the environment
was not ``production``, so a hosted sandbox handed internal error text, hostnames
included, to any caller. Only ``development`` gets them now.

The time-warp route was guarded only by ``TESTING=1``, the same variable that
registers it, so a value inherited from a CI image left it working in
production. Its guard now refuses production on its own as well.
"""

from __future__ import annotations

import json

import pytest
from fastapi import HTTPException
from starlette.requests import Request

from core_api.app import global_exception_handler
from core_api.config import settings
from core_api.routes.testing import _require_testing_mode

_ERROR = "connect to 10.0.0.8:5432 refused"


async def _500_body(exc: Exception) -> dict:
    request = Request(
        {
            "type": "http",
            "method": "GET",
            "scheme": "http",
            "path": "/api/v1/memories",
            "query_string": b"",
            "server": ("core-api", 8000),
            "headers": [],
        }
    )
    response = await global_exception_handler(request, exc)
    return json.loads(response.body)


async def test_a_sandbox_500_does_not_return_the_exception(monkeypatch):
    monkeypatch.setattr(settings, "environment", "sandbox")

    body = await _500_body(RuntimeError(_ERROR))

    assert body["detail"] == "Internal Server Error"
    assert "10.0.0.8" not in json.dumps(body)
    assert "RuntimeError" not in json.dumps(body)


async def test_development_still_returns_the_exception(monkeypatch):
    monkeypatch.setattr(settings, "environment", "development")

    body = await _500_body(RuntimeError(_ERROR))

    assert body["detail"] == _ERROR
    assert body["error_type"] == "RuntimeError"


def test_time_warp_refuses_production_even_with_testing_set(monkeypatch):
    monkeypatch.setenv("TESTING", "1")
    monkeypatch.setattr(settings, "environment", "production")

    with pytest.raises(HTTPException) as refused:
        _require_testing_mode()

    assert refused.value.status_code == 403
