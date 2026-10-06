"""A read-only verdict nobody could determine (caura-ai/caura#1638).

``platform-auth-api``'s ``/_auth`` fails OPEN on its own storage lookup: an
exception returns without the header, and the request proceeds. That is the
right call for an AUTH path — refusing every write through a storage blip is
far worse than letting a few over-plan ones through — and the wrong one for a
MEASUREMENT path, because the observation that feeds the
``enforce_mcp_plan_limits`` decision then cannot tell a tenant who is under
their limit from one nobody could look up. One code path was both.

The fix keeps the fail-open and makes the blindness visible: ``/_auth`` reports
``x-org-read-only: unknown``, and ``_check_plan_limit`` emits
``mcp_plan_limit_verdict_unknown`` for those requests. A blind sample stops
being silence and starts being a countable thing, so an observation window full
of them reads as NOT YET MEASURED rather than as quiet.

What these pin is that ``unknown`` never becomes a refusal by accident. It is
one string away from ``"true"`` in a header comparison, and the direction of
that mistake is a storage outage refusing every write on the platform.
"""

from __future__ import annotations

import logging

import pytest

from core_api import mcp_server

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _reset():
    yield
    mcp_server._org_read_only_var.set(False)
    mcp_server._org_read_only_unknown_var.set(False)


def _records(caplog, name: str) -> list[logging.LogRecord]:
    return [r for r in caplog.records if r.message == name]


def test_an_undeterminable_verdict_is_counted(caplog):
    """The whole point: a blind sample leaves a trace."""
    mcp_server._org_read_only_var.set(False)
    mcp_server._org_read_only_unknown_var.set(True)

    with caplog.at_level(logging.INFO, logger="core_api.mcp_server"):
        assert mcp_server._check_plan_limit("create", "tenant-blind") is None

    records = _records(caplog, "mcp_plan_limit_verdict_unknown")
    assert len(records) == 1
    assert records[0].tenant_id == "tenant-blind"
    assert records[0].mcp_operation == "create"


def test_an_undeterminable_verdict_never_refuses(caplog):
    """``unknown`` is one string away from ``"true"`` in a header comparison,
    and getting that wrong means a storage outage refuses every write on the
    platform. Pinned with enforcement ON, which is the only state where the
    mistake would be visible."""
    mcp_server._org_read_only_unknown_var.set(True)

    with caplog.at_level(logging.INFO, logger="core_api.mcp_server"):
        assert mcp_server._check_plan_limit("create", "tenant-blind") is None
        assert mcp_server._check_plan_limit("bulk_create", "tenant-blind") is None


def test_a_known_under_limit_verdict_is_not_counted_as_blind(caplog):
    """The distinction this exists to make. A real "under limit" answer must not
    inflate the blind count, or the observation window looks unmeasurable when
    it is simply clean."""
    mcp_server._org_read_only_var.set(False)
    mcp_server._org_read_only_unknown_var.set(False)

    with caplog.at_level(logging.INFO, logger="core_api.mcp_server"):
        assert mcp_server._check_plan_limit("create", "tenant-ok") is None

    assert _records(caplog, "mcp_plan_limit_verdict_unknown") == []


def test_a_real_over_plan_verdict_wins_over_unknown(caplog):
    """If both are somehow set, the determined verdict is the one that counts —
    reporting a known over-plan tenant as unmeasured would lose the only
    observation that matters."""
    mcp_server._org_read_only_var.set(True)
    mcp_server._org_read_only_unknown_var.set(True)

    with caplog.at_level(logging.INFO, logger="core_api.mcp_server"):
        mcp_server._check_plan_limit("create", "tenant-over")

    assert _records(caplog, "mcp_plan_limit_verdict_unknown") == []
    assert len(_records(caplog, "mcp_plan_limit_would_refuse")) == 1


@pytest.mark.parametrize("op", ["delete", "bulk_delete", "update", "transition"])
def test_an_ungated_op_is_not_counted_as_blind(caplog, op):
    """Blindness only matters where the verdict would have been used.

    Deletes and updates are never gated — they are the carve-out that lets a
    tenant get back under their limit — so counting blind samples on them would
    make an observation window look far more unmeasured than it is, and the
    delete path is a busy one.
    """
    mcp_server._org_read_only_unknown_var.set(True)

    with caplog.at_level(logging.INFO, logger="core_api.mcp_server"):
        mcp_server._check_plan_limit(op, "tenant-blind")

    assert _records(caplog, "mcp_plan_limit_verdict_unknown") == []
