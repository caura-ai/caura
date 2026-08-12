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
    _DIRECT_SUBSCRIBES,
    MANIFEST_PATH,
    _serialize,
    build_manifest,
)

_REPO_ROOT = Path(__file__).resolve().parents[2]
_CONSUMER_FILES = {
    "core-api": _REPO_ROOT / "core-api" / "src" / "core_api" / "consumer.py",
    "core-worker": _REPO_ROOT / "core-worker" / "src" / "core_worker" / "consumer.py",
}
# Matches Memory AND Lifecycle families. It was Memory-only, which is how a
# directly-registered LIFECYCLE subscribe slipped past this guard: lifecycle
# topics normally arrive via a helper the generator invokes dynamically, so the
# combination "lifecycle topic + direct subscribe" fell in the seam between the
# two mechanisms and matched neither.
_DIRECT_SUBSCRIBE_RE = re.compile(
    r"bus\.subscribe\(\s*Topics\.(Memory|Lifecycle)\.([A-Z_]+)"
)


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


def test_direct_subscribes_match_consumer_files() -> None:
    """Close the _DIRECT_SUBSCRIBES blind spot.

    Directly-registered subscribes are hand-listed in the generator (the worker
    package isn't importable in OSS CI), so the drift test alone can't tell when
    that list falls out of sync with the actual ``register_consumers()``. Grep
    each consumer file for its ``bus.subscribe(Topics.<family>.*)`` calls and
    assert they match _DIRECT_SUBSCRIBES — a new subscribe added without updating
    the list fails here instead of silently shipping an incomplete manifest.

    Covers Lifecycle as well as Memory. While it matched Memory only, a lifecycle
    topic registered directly in a consumer satisfied neither mechanism: not the
    generator's dynamic helper capture, and not this guard. The manifest is what
    the enterprise ``check_pubsub_provisioning.py`` reads, so an omission there
    means a consumed topic can ship with no Terraform subscription — the failure
    that took staging down when ``insights-requested`` shipped unprovisioned.
    """
    for service, path in _CONSUMER_FILES.items():
        pairs = _DIRECT_SUBSCRIBE_RE.findall(path.read_text(encoding="utf-8"))
        found = sorted(str(getattr(getattr(Topics, family), name)) for family, name in pairs)
        expected = sorted(_DIRECT_SUBSCRIBES[service])
        assert found == expected, (
            f"{service}: _DIRECT_SUBSCRIBES in scripts/gen_events_manifest.py is out of "
            f"sync with {path.relative_to(_REPO_ROOT)}. Found {found}, expected {expected}. "
            "Update _DIRECT_SUBSCRIBES to match the bus.subscribe(Topics.*) calls."
        )
