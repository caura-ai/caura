"""D13 — recall operations bill the recall counter (flag-gated).

Plans have always defined separate ``searches`` / ``recalls`` limits and the
platform hook maps ``"recall"`` → the recalls counter, but both recall call
sites (REST /recall and MCP caura_recall) passed ``"search"`` — so the recalls
counter never moved and the per-plan recall cap could never fire.

The correct literal is gated behind ``settings.meter_recall_as_recall``
(default OFF) because the recalls counter feeds over-plan enforcement
(x-org-read-only → 403 on write routes); enabling it is a billing decision.
"""

from core_api.config import settings
from core_api.services import usage_service
from core_api.services.usage_service import recall_operation


def test_default_is_off_and_preserves_search_billing(monkeypatch):
    monkeypatch.setattr(settings, "meter_recall_as_recall", False)
    assert recall_operation() == "search"


def test_flag_on_bills_recall(monkeypatch):
    monkeypatch.setattr(settings, "meter_recall_as_recall", True)
    assert recall_operation() == "recall"


def test_default_setting_value_is_off():
    # The deploy-time zero-impact guarantee: a rebuilt image with no env
    # change must bill exactly as before.
    assert settings.model_fields["meter_recall_as_recall"].default is False


async def test_operation_reaches_the_usage_hook(monkeypatch):
    """End-to-end through check_and_increment: the hook sees the gated literal."""
    seen = {}

    async def fake_hook(*, tenant_id, operation, count):
        seen["operation"] = operation

    class Hooks:
        usage_meter = staticmethod(fake_hook)

    monkeypatch.setattr(usage_service, "get_hooks", lambda: Hooks)
    monkeypatch.setattr(settings, "meter_recall_as_recall", True)
    await usage_service.check_and_increment("t1", recall_operation())
    assert seen["operation"] == "recall"

    monkeypatch.setattr(settings, "meter_recall_as_recall", False)
    await usage_service.check_and_increment("t1", recall_operation())
    assert seen["operation"] == "search"


def test_no_bare_search_literal_left_on_recall_sites():
    """Regression guard: the two recall call sites must use recall_operation().

    A grep-style assertion so a revert to the hardcoded literal cannot slip
    back in silently — the exact failure mode that kept the recalls counter
    at zero from the initial schema until D13.
    """
    import pathlib

    root = pathlib.Path(__file__).resolve().parents[1] / "core-api" / "src" / "core_api"
    recall_route = (root / "routes" / "memories.py").read_text()
    # the /recall endpoint body sits between its decorator and the next route;
    # anchor on the path literal only so decorator kwargs (e.g. C33's
    # ``responses=``) don't break the split
    recall_section = recall_route.split('@router.post("/recall"')[1].split("@router.post")[0]
    assert 'check_and_increment(body.tenant_id, recall_operation())' in recall_section
    assert 'check_and_increment(body.tenant_id, "search")' not in recall_section

    mcp = (root / "mcp_server.py").read_text()
    assert "check_and_increment(tenant_id, recall_operation())" in mcp
