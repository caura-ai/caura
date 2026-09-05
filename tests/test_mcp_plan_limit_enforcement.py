"""MCP refuses an over-plan write, flag-gated (caura-ai/caura#1205).

An org over its plan limit is refused a write on REST and was allowed the same
write on MCP. The signal has been readable on this surface since the middleware
started honouring ``x-org-read-only``; what was missing was the refusal. This
supplies it, behind ``settings.enforce_mcp_plan_limits``, default OFF.

Gated rather than switched on, because this is sharper than the two flags it
mirrors: ``meter_recall_as_recall`` and ``meter_mcp_bulk_writes`` change what is
COUNTED, this changes what is ALLOWED. An over-plan tenant that can write over
MCP today stops being able to.

The OFF case stays pinned in ``test_mcp_plan_limit_observation.py``, which is
not redundant with this file — while the flag is off, observe-and-allow IS the
shipped behaviour, and that file fails if a refusal ever appears by accident.
This file pins what happens when someone chooses it on purpose.
"""

from __future__ import annotations

import json
import logging
from unittest.mock import AsyncMock

import pytest

from core_api import mcp_server
from core_api.config import settings
from tests._mcp_test_helpers import parse_envelope

pytestmark = pytest.mark.unit

_PLAN_LIMIT_CODE = "PLAN_LIMIT_READ_ONLY"


class _OutStub:
    def model_dump(self, mode: str = "python"):
        return {"id": "m-1", "status": "created"}


@pytest.fixture(autouse=True)
def _reset_org_read_only():
    """The context var is shared across the session; leave it clean."""
    yield
    mcp_server._org_read_only_var.set(False)


@pytest.fixture
def enforcing(monkeypatch):
    monkeypatch.setattr(settings, "enforce_mcp_plan_limits", True)


def _refusals(caplog) -> list[logging.LogRecord]:
    return [r for r in caplog.records if r.message == "mcp_plan_limit_refused"]


# --- the helper -------------------------------------------------------------


@pytest.mark.parametrize("op", ["create", "bulk_create"])
def test_a_gated_op_over_plan_is_refused(caplog, enforcing, op):
    mcp_server._org_read_only_var.set(True)

    with caplog.at_level(logging.WARNING, logger="core_api.mcp_server"):
        envelope = mcp_server._check_plan_limit(op, "tenant-over-plan")

    assert envelope is not None, "an over-plan gated op must be refused"
    assert json.loads(envelope)["error"]["code"] == _PLAN_LIMIT_CODE
    records = _refusals(caplog)
    assert len(records) == 1
    assert records[0].enforced is True
    assert records[0].mcp_operation == op


def test_the_refusal_uses_a_distinct_event_name(caplog, enforcing):
    """An enforced refusal must not land under the observation's event name.

    Reusing one name and flipping ``enforced`` would leave every existing query
    counting refusals as observations, and "did we actually refuse anyone" is
    the question the rollout turns on.
    """
    mcp_server._org_read_only_var.set(True)

    with caplog.at_level(logging.WARNING, logger="core_api.mcp_server"):
        mcp_server._check_plan_limit("create", "tenant-over-plan")

    assert [
        r for r in caplog.records if r.message == "mcp_plan_limit_would_refuse"
    ] == []
    assert len(_refusals(caplog)) == 1


@pytest.mark.parametrize("op", ["transition", "delete", "update", "bulk_delete"])
def test_an_ungated_op_is_allowed_even_when_enforcing(enforcing, op):
    """Enforcement reads the same table REST gates on, so it cannot refuse what
    REST permits. The deletes above all must stay open — an over-plan org gets
    back under the limit by deleting, and refusing that traps it there."""
    mcp_server._org_read_only_var.set(True)

    assert mcp_server._check_plan_limit(op, "tenant-over-plan") is None


def test_a_within_plan_org_is_not_refused(enforcing):
    mcp_server._org_read_only_var.set(False)

    assert mcp_server._check_plan_limit("create", "tenant-ok") is None


# --- end to end through the tool -------------------------------------------


@pytest.mark.asyncio
async def test_an_over_plan_single_write_is_refused(mcp_env, enforcing):
    svc = mcp_env["service"]("create_memory")
    svc.return_value = _OutStub()
    mcp_server._org_read_only_var.set(True)

    out = await mcp_server.caura_write(content="written while over plan")

    assert parse_envelope(out)["error"]["code"] == _PLAN_LIMIT_CODE
    # Refused means not written. Returning an error while still persisting the
    # row would be the worst of both.
    svc.assert_not_awaited()


@pytest.mark.asyncio
async def test_an_over_plan_batch_write_is_refused_whole(mcp_env, enforcing):
    """No partial batches. A trimmed batch reports success for a call that did
    not do what it was asked, and the caller cannot tell which items landed."""
    svc = mcp_env["service"]("create_memories_bulk")
    svc.return_value = _OutStub()
    mcp_server._org_read_only_var.set(True)

    out = await mcp_server.caura_write(
        items=[{"content": "one"}, {"content": "two"}, {"content": "three"}]
    )

    assert parse_envelope(out)["error"]["code"] == _PLAN_LIMIT_CODE
    svc.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_refused_write_does_not_charge_quota(mcp_env, enforcing, monkeypatch):
    """The gate runs BEFORE the meter, and the ordering is load-bearing.

    The counter this would charge is the one over-plan mode is computed FROM, so
    charging a refused write would push the tenant further over the limit it was
    just refused for — the org digs deeper by being told no.
    """
    meter = AsyncMock(return_value=None)
    monkeypatch.setattr(mcp_server, "check_and_increment", meter)
    mcp_env["service"]("create_memory").return_value = _OutStub()
    mcp_server._org_read_only_var.set(True)

    await mcp_server.caura_write(content="written while over plan")

    meter.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_refused_batch_does_not_charge_quota(mcp_env, enforcing, monkeypatch):
    """Same property on the batch path, where the charge is per item and the
    over-charge would therefore scale with batch size."""
    meter = AsyncMock(return_value=None)
    monkeypatch.setattr(mcp_server, "bulk_check_and_increment", meter)
    monkeypatch.setattr(settings, "meter_mcp_bulk_writes", True)
    mcp_env["service"]("create_memories_bulk").return_value = _OutStub()
    mcp_server._org_read_only_var.set(True)

    await mcp_server.caura_write(items=[{"content": "one"}, {"content": "two"}])

    meter.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_within_plan_write_still_succeeds_while_enforcing(mcp_env, enforcing):
    """The guard against over-reach: enabling enforcement must refuse the
    over-plan case and nothing else."""
    mcp_env["service"]("create_memory").return_value = _OutStub()
    mcp_server._org_read_only_var.set(False)

    out = await mcp_server.caura_write(content="a normal write")

    assert "error" not in parse_envelope(out)


# --- the default ------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_flag_defaults_off():
    """A rebuilt image with no env change must refuse exactly as before: not at
    all. This is the difference between shipping a capability and shipping a
    behaviour change."""
    assert settings.enforce_mcp_plan_limits is False
