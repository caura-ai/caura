"""L-130: evolve names a rule skipped because every supplied id was out of scope.

``report_outcome`` and ``caura_evolve`` filter ``related_ids`` by scope before
rule synthesis and hand the filtered list to ``_maybe_generate_rule``, whose
first check answered ``no_related_ids`` for an empty list. When the caller DID
supply ids and the filter dropped them all, the response paired
``rule_skipped_reason='no_related_ids'`` (A10: "no memories supplied") with
``weight_adjustment_skipped_reason='agent_id_mismatch'``, and tooling that
branches on the rule slug read a scope violation as an omission. The rule side
now says ``all_out_of_scope``; an empty request still says ``no_related_ids``.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from core_api import mcp_server
from core_api.services import evolve_service

pytestmark = pytest.mark.asyncio

_ID = "00000000-0000-0000-0000-000000000aaa"


async def _report(related_ids, filtered):
    with (
        patch.multiple(
            "core_api.services.evolve_service",
            _filter_by_scope=AsyncMock(return_value=filtered),
            _adjust_weights=AsyncMock(return_value=(None, [], [])),
            _persist_outcome=AsyncMock(
                return_value="00000000-0000-0000-0000-000000000001"
            ),
        ),
        patch(
            "core_api.services.organization_settings.resolve_config",
            new=AsyncMock(return_value=SimpleNamespace()),
        ),
    ):
        return await evolve_service.report_outcome(
            tenant_id="t1",
            outcome="report",
            outcome_type="failure",
            related_ids=related_ids,
            agent_id="a1",
        )


async def test_report_outcome_names_a_failure_whose_ids_were_all_filtered():
    result = await _report([_ID], ([], 1))

    assert result["rule_skipped_reason"] == "all_out_of_scope"
    assert result["weight_adjustment_skipped_reason"] == "agent_id_mismatch"


async def test_report_outcome_still_says_no_related_ids_when_none_were_supplied():
    """The control: an empty request keeps its A10 slug."""
    result = await _report(None, ([], 0))

    assert result["rule_skipped_reason"] == "no_related_ids"


async def test_caura_evolve_names_a_failure_whose_ids_were_all_filtered(
    mcp_env, monkeypatch
):
    captured: dict = {}

    @asynccontextmanager
    async def _session():
        yield None

    async def _spy_apply(**kwargs):
        captured.update(kwargs)
        return {
            "outcome_id": "00000000-0000-0000-0000-000000000001",
            "outcome_type": "failure",
            "scope": "agent",
            "weight_adjustments": [],
            "rules_generated": [],
            "rule_skipped_reason": kwargs["rule_skipped_reason"],
            "out_of_scope_count": kwargs["out_of_scope_count"],
            "weight_adjustment_skipped_reason": kwargs[
                "weight_adjustment_skipped_reason"
            ],
            "evolve_ms": 1,
        }

    monkeypatch.setattr(mcp_server, "_no_db", _session)
    monkeypatch.setattr(
        mcp_server, "_require_trust", AsyncMock(return_value=(3, False, None))
    )
    monkeypatch.setattr(mcp_server, "check_and_increment", AsyncMock())
    monkeypatch.setattr(
        "core_api.services.evolve_service._filter_by_scope",
        AsyncMock(return_value=([], 1)),
    )
    monkeypatch.setattr(
        "core_api.services.organization_settings.resolve_config",
        AsyncMock(return_value=SimpleNamespace()),
    )
    monkeypatch.setattr(
        "core_api.services.evolve_service._apply_outcome_to_db", _spy_apply
    )

    await mcp_server.caura_evolve(
        outcome="a thing happened",
        outcome_type="failure",
        related_ids=[_ID],
        scope="agent",
        agent_id="a1",
    )

    assert captured["rule_skipped_reason"] == "all_out_of_scope"
