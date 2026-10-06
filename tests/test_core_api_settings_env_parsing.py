"""core-api Settings: env-form parsing and boot-time floors on knobs."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from core_api.config import Settings


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("ops@example.com,sec@example.com", ["ops@example.com", "sec@example.com"]),
        (" ops@example.com , sec@example.com ", ["ops@example.com", "sec@example.com"]),
        ("ops@example.com", ["ops@example.com"]),
        ("", []),
    ],
)
def test_alert_recipients_accept_the_documented_comma_separated_env_form(
    monkeypatch: pytest.MonkeyPatch, raw: str, expected: list[str]
) -> None:
    monkeypatch.setenv("SECURITY_AUDIT_ALERT_RECIPIENTS", raw)

    settings = Settings(_env_file=None)

    assert settings.security_audit_alert_recipients == expected


def test_alert_recipients_still_accept_a_python_list() -> None:
    settings = Settings(
        _env_file=None, security_audit_alert_recipients=["a@example.com"]
    )

    assert settings.security_audit_alert_recipients == ["a@example.com"]


_CONCURRENCY_CAPS = [
    "per_tenant_search_concurrency",
    "per_tenant_write_concurrency",
    "per_tenant_embed_concurrency",
    "per_tenant_storage_write_concurrency",
    "per_tenant_storage_search_concurrency",
    "contradiction_detection_concurrency",
]


@pytest.mark.parametrize("field", _CONCURRENCY_CAPS)
@pytest.mark.parametrize("value", [0, -1])
def test_concurrency_caps_reject_values_below_one(field: str, value: int) -> None:
    # Semaphore(0) never admits anyone: rejecting at load is the only
    # place the misconfig can surface before it stalls every request.
    with pytest.raises(ValidationError, match=field):
        Settings(_env_file=None, **{field: value})


@pytest.mark.parametrize("field", _CONCURRENCY_CAPS)
def test_concurrency_caps_accept_one(field: str) -> None:
    settings = Settings(_env_file=None, **{field: 1})

    assert getattr(settings, field) == 1
