"""The committed events manifest must match what the registration code produces.

If this fails, a service's Pub/Sub subscriptions changed (e.g. a new
``bus.subscribe`` in a lifecycle helper) without regenerating the manifest. The
manifest is the contract the infra repo uses to verify every consumed topic has
a provisioned subscription, so a stale manifest can let an unprovisioned topic
ship — exactly the failure mode that took staging down on the insights-requested
rollout. Regenerate with: ``python scripts/gen_events_manifest.py``.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from common.events.topics import Topics
from scripts.gen_events_manifest import (
    _MEMORY_DIRECT,
    MANIFEST_PATH,
    _serialize,
    build_manifest,
)

_REPO_ROOT = Path(__file__).resolve().parents[2]
_CONSUMER_FILES = {
    "core-api": _REPO_ROOT / "core-api" / "src" / "core_api" / "consumer.py",
    "core-worker": _REPO_ROOT / "core-worker" / "src" / "core_worker" / "consumer.py",
}
_MEMORY_SUBSCRIBE_RE = re.compile(r"bus\.subscribe\(\s*Topics\.Memory\.([A-Z_]+)")


def test_events_manifest_is_in_sync() -> None:
    assert MANIFEST_PATH.exists(), (
        "common/events/events_manifest.json is missing — "
        "run: python scripts/gen_events_manifest.py"
    )
    expected = _serialize(build_manifest())
    actual = MANIFEST_PATH.read_text()
    assert actual == expected, (
        "events_manifest.json is stale. A consumed Pub/Sub topic set changed "
        "without regenerating the manifest. Run: python scripts/gen_events_manifest.py"
    )


def test_events_manifest_is_well_formed() -> None:
    manifest = json.loads(MANIFEST_PATH.read_text())
    services = manifest["services"]
    assert services, "manifest lists no services"
    for service, topics in services.items():
        assert topics, f"{service} subscribes to no topics"
        assert topics == sorted(set(topics)), (
            f"{service} topics must be sorted and unique"
        )
        assert all(t.startswith("memclaw.") for t in topics), (
            f"{service} has a malformed topic"
        )


def test_memory_direct_matches_consumer_files() -> None:
    """Close the _MEMORY_DIRECT blind spot.

    Memory-pipeline subscribes are hand-listed in the generator (the worker
    package isn't importable in OSS CI), so the drift test alone can't tell when
    that list falls out of sync with the actual ``register_consumers()``. Grep
    each consumer file for its ``bus.subscribe(Topics.Memory.*)`` calls and
    assert they match _MEMORY_DIRECT — a new subscribe added without updating the
    list now fails here instead of silently shipping an incomplete manifest.
    """
    for service, path in _CONSUMER_FILES.items():
        names = _MEMORY_SUBSCRIBE_RE.findall(path.read_text(encoding="utf-8"))
        found = sorted(str(getattr(Topics.Memory, name)) for name in names)
        expected = sorted(_MEMORY_DIRECT[service])
        assert found == expected, (
            f"{service}: _MEMORY_DIRECT in scripts/gen_events_manifest.py is out of sync "
            f"with {path.relative_to(_REPO_ROOT)}. Found {found}, expected {expected}. "
            "Update _MEMORY_DIRECT to match the bus.subscribe(Topics.Memory.*) calls."
        )
