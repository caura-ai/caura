"""Typed payload for ``memclaw.org.settings-changed`` (CAURA-571).

Published by core-api after an org's settings row is written. Carries
the ``org_id`` whose settings changed so every core-api process can drop
that key from its per-process settings cache (``organization_settings``
TTLCache). The event intentionally carries no settings *values* — the
consumer just invalidates and the next read re-resolves from storage, so
there's nothing to keep in sync and no secret to leak on the wire.
"""

from __future__ import annotations

from pydantic import BaseModel


class OrgSettingsChangedEvent(BaseModel):
    """Payload of ``memclaw.org.settings-changed`` — the ``org_id`` whose
    cached settings every process should evict."""

    org_id: str
