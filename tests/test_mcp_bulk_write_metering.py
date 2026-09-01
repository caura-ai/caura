"""MCP batch write bills the write counter, flag-gated (caura-ai/caura#1220).

``caura_write(items=[...])`` reached ``create_memories_bulk`` with no metering
call of any kind, while REST's ``POST /memories/bulk`` charges one unit per
item. Same tenant, same N memories, different bill — and the counters it missed
are the ones over-plan mode is computed from, so a batch-heavy tenant could
never trip its plan limit.

Gated behind ``settings.meter_mcp_bulk_writes``, default OFF, mirroring the D13
``meter_recall_as_recall`` precedent: this starts charging for writes that have
been free, which is a billing decision rather than a deploy side effect.

Both states are pinned below. The off case is not a placeholder — while it is
the default it IS the shipped behaviour, and a silent flip would bill live
tenants without anyone choosing to.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from core_api import mcp_server
from core_api.config import settings
from tests._mcp_test_helpers import parse_envelope

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]


class _OutStub:
    def model_dump(self, mode: str = "python"):
        return {"id": "batch-1", "status": "created"}


@pytest.fixture
def bulk_meter(monkeypatch):
    """Patch the bulk meter and hand back the mock for call assertions."""
    meter = AsyncMock(return_value=None)
    monkeypatch.setattr(mcp_server, "bulk_check_and_increment", meter)
    return meter


async def test_batch_write_is_not_billed_while_the_flag_is_off(
    mcp_env, bulk_meter, monkeypatch
):
    """The shipped default. Pins it deliberately rather than by omission."""
    monkeypatch.setattr(settings, "meter_mcp_bulk_writes", False)
    mcp_env["service"]("create_memories_bulk").return_value = _OutStub()

    out = await mcp_server.caura_write(items=[{"content": "one"}, {"content": "two"}])

    assert "error" not in parse_envelope(out)
    bulk_meter.assert_not_awaited()


async def test_batch_write_bills_one_unit_per_item_when_enabled(
    mcp_env, bulk_meter, monkeypatch
):
    """The fix: N items cost N, matching REST's POST /memories/bulk."""
    monkeypatch.setattr(settings, "meter_mcp_bulk_writes", True)
    mcp_env["service"]("create_memories_bulk").return_value = _OutStub()

    out = await mcp_server.caura_write(
        items=[{"content": "one"}, {"content": "two"}, {"content": "three"}]
    )

    assert "error" not in parse_envelope(out)
    bulk_meter.assert_awaited_once()
    tenant_id, count = bulk_meter.await_args.args
    assert count == 3, "must charge per item, not once per call"
    assert tenant_id == mcp_env["tenant"]


async def test_billing_happens_before_the_write(mcp_env, bulk_meter, monkeypatch):
    """REST charges before the write, so a batch that fails partway still costs
    what it attempted. Two orderings for one operation across two surfaces is
    the drift this area keeps producing, so the ordering is pinned, not assumed.
    """
    monkeypatch.setattr(settings, "meter_mcp_bulk_writes", True)
    order: list[str] = []
    bulk_meter.side_effect = lambda *a, **k: order.append("meter")

    async def _write(*args, **kwargs):
        order.append("write")
        return _OutStub()

    mcp_env["service"]("create_memories_bulk").side_effect = _write

    await mcp_server.caura_write(items=[{"content": "one"}])

    assert order == ["meter", "write"]


async def test_the_single_write_path_is_untouched(mcp_env, bulk_meter, monkeypatch):
    """Scope guard: this change is the batch path only.

    The single-content path has always metered through ``check_and_increment``
    and must not start double-charging through the bulk meter as well.
    """
    monkeypatch.setattr(settings, "meter_mcp_bulk_writes", True)
    mcp_env["service"]("create_memory").return_value = _OutStub()

    await mcp_server.caura_write(content="a single fact")

    bulk_meter.assert_not_awaited()


@pytest.mark.asyncio
async def test_the_flag_defaults_off():
    """A rebuilt image with no env change must bill exactly as before."""
    assert settings.meter_mcp_bulk_writes is False
