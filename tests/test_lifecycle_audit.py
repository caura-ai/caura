"""Regression tests for _CoreApiLifecycleAdapter.crystallize (CAURA-657).

The lifecycle crystallize adapter previously called run_crystallization as
``run_crystallization(None, org_id, fleet_id, trigger="lifecycle")`` — a stray
leading positional that shifted every arg and passed ``trigger`` both
positionally and by keyword, so every lifecycle-triggered crystallization raised
``TypeError: got multiple values for argument 'trigger'`` and the pubsub handler
nacked + redelivered forever in prod (observed ~20x/day). The existing
lifecycle-handler tests use a fully-faked adapter, so the real call site was
never exercised. These tests hit the real adapter method with an ``autospec``'d
run_crystallization, so the enforced signature makes a regression fail here
rather than in a live pubsub handler.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest

from core_api.services.lifecycle_audit import (
    _CRYSTALLIZE_MIN_ACTIVE_MEMORIES,
    _CoreApiLifecycleAdapter,
)


class _FakeStorage:
    def __init__(self, active: int) -> None:
        self._active = active

    async def count_active(self, org_id: str, fleet_id: str | None) -> int:
        return self._active


class _Cfg:
    auto_crystallize_enabled = True


@pytest.mark.asyncio
async def test_crystallize_calls_run_crystallization_with_correct_args() -> None:
    adapter = _CoreApiLifecycleAdapter(
        _FakeStorage(active=_CRYSTALLIZE_MIN_ACTIVE_MEMORIES + 1)
    )
    report_id = uuid4()
    with (
        patch(
            "core_api.services.lifecycle_audit.resolve_config",
            new=AsyncMock(return_value=_Cfg()),
        ),
        patch(
            "core_api.services.crystallizer_service.run_crystallization",
            autospec=True,
        ) as mock_run,
    ):
        mock_run.return_value = report_id
        result = await adapter.crystallize(org_id="t1", fleet_id="f1")

    assert result == 1
    # org_id maps to run_crystallization's tenant_id (translated at the boundary);
    # trigger is passed exactly once. Under autospec the old buggy positional call
    # (None, org_id, fleet_id, trigger=...) would raise TypeError here.
    mock_run.assert_awaited_once_with(
        tenant_id="t1", fleet_id="f1", trigger="lifecycle"
    )


@pytest.mark.asyncio
async def test_crystallize_skips_when_auto_disabled() -> None:
    class _Off:
        auto_crystallize_enabled = False

    adapter = _CoreApiLifecycleAdapter(_FakeStorage(active=10_000))
    with patch(
        "core_api.services.lifecycle_audit.resolve_config",
        new=AsyncMock(return_value=_Off()),
    ):
        assert await adapter.crystallize(org_id="t1", fleet_id=None) == 0


@pytest.mark.asyncio
async def test_crystallize_skips_below_active_threshold() -> None:
    adapter = _CoreApiLifecycleAdapter(
        _FakeStorage(active=_CRYSTALLIZE_MIN_ACTIVE_MEMORIES)
    )
    with patch(
        "core_api.services.lifecycle_audit.resolve_config",
        new=AsyncMock(return_value=_Cfg()),
    ):
        assert await adapter.crystallize(org_id="t1", fleet_id=None) == 0
