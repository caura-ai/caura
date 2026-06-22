"""Tests for fast-mode post-write governance remediation (eToro).

The fast-mode counterpart to ``GovernanceDecision``: in the default fast mode
enrichment is deferred to the worker, which PATCHes the LLM's ``contains_pii`` /
``business_relevance`` onto the already-persisted row; the enriched-event
consumer then calls ``remediate_after_enrichment`` to apply the tenant's
configured action on that free-form signal. Storage + audit are stubbed so
these stay deterministic (no async audit queue / storage round-trip).
"""

import pytest

from core_api.services import governance_remediation
from core_api.services.organization_settings import ResolvedConfig

pytestmark = pytest.mark.asyncio


@pytest.fixture
def emitted(monkeypatch):
    """Capture governance audit emissions; returns the list of captured kwargs."""
    calls: list[dict] = []

    async def _record(*args, **kwargs):
        calls.append(kwargs)

    monkeypatch.setattr(governance_remediation, "emit_governance_audit", _record)
    return calls


@pytest.fixture
def storage(monkeypatch):
    """Stub the storage client; record soft-delete / update calls."""
    actions: list[tuple] = []

    class _SC:
        async def soft_delete_memory(self, mid):
            actions.append(("soft_delete", mid))

        async def update_memory(self, mid, tenant_id, patch):
            actions.append(("update", mid, tenant_id, patch))

    monkeypatch.setattr(governance_remediation, "get_storage_client", lambda: _SC())
    return actions


def _cfg(*, pii: dict | None = None, nb: dict | None = None) -> ResolvedConfig:
    gov: dict = {}
    if pii is not None:
        gov["pii"] = pii
    if nb is not None:
        gov["non_business"] = nb
    return ResolvedConfig({"governance": gov})


def _mem(**kw) -> dict:
    return {
        "id": kw.get("id", "m1"),
        "tenant_id": "t1",
        "agent_id": "a1",
        "content": kw.get("content", "free-form detail"),
        "metadata": kw.get("metadata", {}),
    }


async def test_disabled_is_noop(emitted, storage):
    dropped = await governance_remediation.remediate_after_enrichment(_mem(), _cfg())
    assert dropped is False
    assert emitted == []
    assert storage == []


async def test_missing_id_skips_without_side_effects(emitted, storage):
    # A malformed enriched-event payload without an id must not soft-delete the
    # literal "None" or stamp resource_id="None" on an audit row.
    cfg = _cfg(pii={"enabled": True, "action": "drop"})
    mem = _mem(id=None, metadata={"contains_pii": True})
    dropped = await governance_remediation.remediate_after_enrichment(mem, cfg)
    assert dropped is False
    assert storage == []
    assert emitted == []


async def test_pii_drop_soft_deletes_and_audits(emitted, storage):
    cfg = _cfg(pii={"enabled": True, "action": "drop"})
    mem = _mem(metadata={"contains_pii": True, "pii_types": ["health"]})
    dropped = await governance_remediation.remediate_after_enrichment(mem, cfg)
    assert dropped is True
    assert ("soft_delete", "m1") in storage
    assert any(c["action"] == "pii_drop" for c in emitted)


async def test_pii_mask_config_flags_but_records_intent(emitted, storage):
    # Fast mode can't redact a free-form LLM span; a mask policy flags the row
    # but stays distinguishable from a genuine flag policy in the audit.
    cfg = _cfg(pii={"enabled": True, "action": "mask"})
    mem = _mem(metadata={"contains_pii": True, "pii_types": ["health"]})
    dropped = await governance_remediation.remediate_after_enrichment(mem, cfg)
    assert dropped is False
    assert storage == []  # nothing redacted/dropped
    flag = next(c for c in emitted if c["action"] == "pii_flag")
    assert flag["detail"]["configured_action"] == "pii_mask"


async def test_pii_flag_config_records_no_configured_action(emitted, storage):
    cfg = _cfg(pii={"enabled": True, "action": "flag"})
    mem = _mem(metadata={"contains_pii": True})
    await governance_remediation.remediate_after_enrichment(mem, cfg)
    flag = next(c for c in emitted if c["action"] == "pii_flag")
    assert "configured_action" not in flag["detail"]


async def test_nonbusiness_keep_private_updates_visibility(emitted, storage):
    cfg = _cfg(nb={"enabled": True, "disposition": "keep_private"})
    mem = _mem(metadata={"business_relevance": "personal"})
    dropped = await governance_remediation.remediate_after_enrichment(mem, cfg)
    assert dropped is False
    assert ("update", "m1", "t1", {"visibility": "scope_agent"}) in storage
    assert any(c["action"] == "nonbusiness_keep_private" for c in emitted)


async def test_nonbusiness_drop_soft_deletes(emitted, storage):
    cfg = _cfg(nb={"enabled": True, "disposition": "drop"})
    mem = _mem(metadata={"business_relevance": "personal"})
    dropped = await governance_remediation.remediate_after_enrichment(mem, cfg)
    assert dropped is True
    assert ("soft_delete", "m1") in storage
    assert any(c["action"] == "nonbusiness_drop" for c in emitted)


async def test_business_content_is_noop(emitted, storage):
    cfg = _cfg(nb={"enabled": True, "disposition": "drop"})
    mem = _mem(metadata={"business_relevance": "business"})
    dropped = await governance_remediation.remediate_after_enrichment(mem, cfg)
    assert dropped is False
    assert storage == []
    assert emitted == []


@pytest.mark.parametrize(
    ("cfg_kwargs", "metadata", "drop_action"),
    [
        (
            {"pii": {"enabled": True, "action": "drop"}},
            {"contains_pii": True},
            "pii_drop",
        ),
        (
            {"nb": {"enabled": True, "disposition": "drop"}},
            {"business_relevance": "personal"},
            "nonbusiness_drop",
        ),
    ],
)
async def test_drop_audits_before_soft_delete(
    monkeypatch, cfg_kwargs, metadata, drop_action
):
    # Compliance invariant: the audit must be recorded BEFORE the destructive
    # soft-delete, so a delete that succeeds before a failing audit can't leave
    # an untracked deletion in the tamper-evident log (mirrors the audit-before-
    # mutate ordering in GovernanceScanContent). Capture both into one ordered log.
    order: list[str] = []

    async def _audit(*_a, **kw):
        order.append(f"audit:{kw['action']}")

    class _SC:
        async def soft_delete_memory(self, _mid):
            order.append("soft_delete")

    monkeypatch.setattr(governance_remediation, "emit_governance_audit", _audit)
    monkeypatch.setattr(governance_remediation, "get_storage_client", lambda: _SC())

    dropped = await governance_remediation.remediate_after_enrichment(
        _mem(metadata=metadata), _cfg(**cfg_kwargs)
    )
    assert dropped is True
    assert order == [f"audit:{drop_action}", "soft_delete"]
