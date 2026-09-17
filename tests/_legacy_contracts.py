"""Frozen compatibility identifiers shared by root-level tests."""

FROZEN_PLUGIN_SLUG = "memclaw"  # legacy-name-ok: existing plugin and skill identifier
FROZEN_TOPIC_PREFIX = "memclaw"  # legacy-name-ok: deployed Pub/Sub wire namespace
LEGACY_API_KEY_FIELD = "memclaw_api_key"  # legacy-name-ok: Settings compatibility field
INSIGHTS_AGENT_ID = "memclaw-insighter"  # legacy-name-ok: persisted service-agent identity
LEGACY_KEYSTONES_ROUTE = "GET /api/v1/memclaw/keystones"  # legacy-name-floor: served mirror route, named here not declared


def frozen_topic(suffix: str) -> str:
    """Build an independently pinned deployed topic name."""
    return f"{FROZEN_TOPIC_PREFIX}.{suffix}"
