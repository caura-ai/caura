"""MCP plan-limit observation — the DEFAULT half of caura-ai/caura#1205.

An org over its plan limit is refused a write on REST and allowed the same
write on MCP, because the MCP middleware never read ``x-org-read-only``. Step
one plumbed the signal and reported what WOULD be refused without refusing.

The refusal now exists, behind ``settings.enforce_mcp_plan_limits`` — see
``test_mcp_plan_limit_enforcement.py``. This file did NOT become historical
when that landed: the flag is off by default, so observe-and-allow is still the
SHIPPED behaviour, and these tests are what stop it changing by accident.

The second half below is the load-bearing one: **the write must still
succeed**. A suite that only proved the logging works would go green on an
accidental enforcement — including one caused by flipping the flag's default,
which is exactly what it catches.
"""

from __future__ import annotations

import json
import logging

import pytest

from core_api import mcp_server

pytestmark = pytest.mark.unit


class _OutStub:
    """Minimal stand-in for what ``create_memory`` returns (see test_mcp_write)."""

    def model_dump(self, mode: str = "python"):
        return {"id": "m-over-plan", "status": "created"}


@pytest.fixture(autouse=True)
def _reset_org_read_only():
    """Leave the flag clean — the session shares one context across tests."""
    yield
    mcp_server._org_read_only_var.set(False)


def _warnings(caplog) -> list[logging.LogRecord]:
    return [r for r in caplog.records if r.message == "mcp_plan_limit_would_refuse"]


# --- what gets observed -----------------------------------------------------


@pytest.mark.parametrize("op", ["create", "bulk_create"])
def test_a_gated_op_over_plan_is_reported(caplog, op):
    mcp_server._org_read_only_var.set(True)
    with caplog.at_level(logging.WARNING, logger="core_api.mcp_server"):
        mcp_server._check_plan_limit(op, "tenant-over-plan")
    records = _warnings(caplog)
    assert len(records) == 1
    assert records[0].tenant_id == "tenant-over-plan"
    assert records[0].mcp_operation == op
    # Pinned so the log itself says which era it came from: a line without
    # this is from a build that enforces, and means something different.
    assert records[0].enforced is False


def test_nothing_is_reported_when_the_org_is_within_plan(caplog):
    mcp_server._org_read_only_var.set(False)
    with caplog.at_level(logging.WARNING, logger="core_api.mcp_server"):
        mcp_server._check_plan_limit("create", "tenant-ok")
    assert _warnings(caplog) == []


@pytest.mark.parametrize("op", ["transition", "delete", "update", "bulk_delete"])
def test_an_ungated_op_is_not_reported_even_over_plan(caplog, op):
    """The observation reads the same table REST gates on, so it cannot report
    a refusal REST would not make. ``transition`` and the deletes are free by
    decision (an over-plan org must be able to delete its way back under), and
    ``update`` rewrites a row rather than adding one."""
    mcp_server._org_read_only_var.set(True)
    with caplog.at_level(logging.WARNING, logger="core_api.mcp_server"):
        mcp_server._check_plan_limit(op, "tenant-over-plan")
    assert _warnings(caplog) == []


# --- and, critically, nothing is refused ------------------------------------


@pytest.mark.asyncio
async def test_an_over_plan_write_still_succeeds(mcp_env, caplog):
    """Observe, do not enforce. This is the behavioural contract of this PR.

    If someone later adds the refusal without meaning to, this fails — which
    is the point. Flipping it is a deliberate act with a blast radius that
    wants measuring first.
    """
    mcp_env["service"]("create_memory").return_value = _OutStub()
    mcp_server._org_read_only_var.set(True)

    with caplog.at_level(logging.WARNING, logger="core_api.mcp_server"):
        out = await mcp_server.caura_write(content="written while over plan")

    payload = json.loads(out)
    assert "error" not in payload, payload
    # The write happened AND was reported — both, not either.
    assert len(_warnings(caplog)) == 1
