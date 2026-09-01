"""C2 — the contradiction judge must be configurable apart from entity extraction.

Moving entity extraction to a cheaper model used to move the contradiction
judge with it, silently, because the judge read the entity-extraction
provider/model config directly.
"""

import pytest

from core_api.config import settings
from core_api.services.contradiction_detector import _judge_model_attr, _judge_provider

pytestmark = pytest.mark.unit


class _Cfg:
    entity_extraction_provider = "tenant-entity-provider"


def test_defaults_preserve_historical_behaviour(monkeypatch):
    monkeypatch.setattr(settings, "contradiction_provider", "")
    monkeypatch.setattr(settings, "contradiction_model", "")
    assert _judge_provider(_Cfg()) == "tenant-entity-provider"
    assert _judge_model_attr() == "entity_extraction_model"


def test_explicit_override_wins(monkeypatch):
    monkeypatch.setattr(settings, "contradiction_provider", "judge-provider")
    monkeypatch.setattr(settings, "contradiction_model", "judge-model")
    assert _judge_provider(_Cfg()) == "judge-provider"
    assert _judge_model_attr() == "contradiction_model"


def test_falls_back_to_global_when_no_tenant_config(monkeypatch):
    monkeypatch.setattr(settings, "contradiction_provider", "")
    monkeypatch.setattr(
        settings, "entity_extraction_provider", "global-entity-provider"
    )
    assert _judge_provider(None) == "global-entity-provider"


def test_every_judge_call_site_uses_the_helper():
    from pathlib import Path

    import core_api.services.contradiction_detector as cd

    src = Path(cd.__file__).read_text()
    # the entity-provider read survives in exactly ONE place — the helper
    # itself, which is the fallback. Any second occurrence is a call site
    # that bypassed the split.
    assert src.count("tenant_config.entity_extraction_provider if tenant_config") == 1
    assert src.count("_judge_provider(tenant_config)") >= 4
    assert 'model_attr="entity_extraction_model"' not in src
