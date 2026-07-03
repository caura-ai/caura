"""Regression: an oversized OPTIONAL observability blob must not 422 the heartbeat.

``recall_metrics`` (4 KB) and ``reconcile`` (8 KB) are best-effort observability
fields on ``HeartbeatIn``. They used to ``raise`` in their field_validators when
oversized — which fails the whole request model → 422 → the node's registration
AND command channel are dropped over one bloated optional field.

eToro 2026-06-28: a node whose skill catalog made the reconcile summary exceed
8 KB 422'd every heartbeat, going stale + uncommandable (memory tooling, which
uses a different path, was unaffected). The validators now DROP the oversized
blob to a small marker instead of raising — the load-bearing heartbeat lands;
only the detailed snapshot for that tick is lost.
"""

from __future__ import annotations

import json

from core_api.routes.fleet import HeartbeatIn


def _oversized(min_bytes: int) -> dict:
    """A dict whose JSON comfortably exceeds ``min_bytes``."""
    blob = {"installed": [f"skill-{i:06d}" for i in range(min_bytes // 8 + 200)]}
    assert len(json.dumps(blob)) > min_bytes
    return blob


def test_oversized_reconcile_is_dropped_not_rejected():
    big = _oversized(8192)
    # Must NOT raise ValidationError.
    hb = HeartbeatIn(tenant_id="t", node_name="n", reconcile=big)
    assert hb.reconcile == {"_truncated": True, "_original_bytes": len(json.dumps(big))}


def test_oversized_recall_metrics_is_dropped_not_rejected():
    big = _oversized(4096)
    hb = HeartbeatIn(tenant_id="t", node_name="n", recall_metrics=big)
    assert hb.recall_metrics is not None
    assert hb.recall_metrics.get("_truncated") is True


def test_normal_reconcile_passes_through_unchanged():
    small = {"catalogCount": 1, "installed": ["memclaw"], "removed": []}
    hb = HeartbeatIn(tenant_id="t", node_name="n", reconcile=small)
    assert hb.reconcile == small


def test_absent_fields_stay_none():
    hb = HeartbeatIn(tenant_id="t", node_name="n")
    assert hb.reconcile is None
    assert hb.recall_metrics is None
