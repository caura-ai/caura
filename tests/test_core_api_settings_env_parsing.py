"""core-api Settings: boot-time floors on knobs."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from core_api.config import Settings

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
