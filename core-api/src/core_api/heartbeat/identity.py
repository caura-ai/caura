"""The server's persistent, anonymous deployment identity.

The server had no instance identity before this module: standalone mode uses
the constant tenant ``default``, and both existing "install" ids belong to
clients (the plugin's ``install_id`` and the daemon's ``install_uuid`` read
from ``x-install-uuid``). The server's id is therefore called
``deployment_id`` everywhere so the two are never conflated.

* ``deployment_id`` — ``uuid4()``, random. Not derived from hardware,
  hostname or MAC. Generated on the first send attempt after boot, never at
  import time.
* ``deployment_token`` — 32 random bytes, hex. Sent as a bearer on every
  beat and pinned by the collector on first sight; its only power is to send
  heartbeats for this id, so it lives beside the id rather than in a secret
  store.

Both are stored in a reserved ``organization_settings`` row with
``org_id = "__deployment__"`` through the existing org-settings client
methods. The primary key is free-form text, so no migration is needed, and
the settings audit trail records every write. Replicas racing on first boot
are resolved by read-after-write: whoever reads a different id than it wrote
adopts the stored one.

Rotation (``POST /api/v1/telemetry/rotate``) writes a fresh pair; the old id
simply goes stale on the collector side.
"""

from __future__ import annotations

import logging
import secrets
import uuid
from dataclasses import dataclass
from typing import Protocol

logger = logging.getLogger(__name__)

DEPLOYMENT_ORG_ID = "__deployment__"
KEY_ID = "deployment_id"
KEY_TOKEN = "deployment_token"
CHANGED_BY = "core-api:heartbeat"

_TOKEN_HEX_LEN = 64  # 32 random bytes


class _OrgSettingsClient(Protocol):
    async def get_org_settings(self, org_id: str) -> dict: ...

    async def update_org_settings(
        self, org_id: str, settings: dict, *, changed_by: str | None = None
    ) -> dict: ...


@dataclass(frozen=True)
class DeploymentIdentity:
    deployment_id: str
    deployment_token: str

    def as_settings(self) -> dict[str, str]:
        return {KEY_ID: self.deployment_id, KEY_TOKEN: self.deployment_token}


def generate() -> DeploymentIdentity:
    return DeploymentIdentity(
        deployment_id=str(uuid.uuid4()),
        deployment_token=secrets.token_hex(32),
    )


def _parse(stored: dict | None) -> DeploymentIdentity | None:
    """Return the identity in a settings dict, or ``None`` if absent/malformed."""
    if not isinstance(stored, dict):
        return None
    dep_id = stored.get(KEY_ID)
    token = stored.get(KEY_TOKEN)
    if not isinstance(dep_id, str) or not isinstance(token, str):
        return None
    try:
        uuid.UUID(dep_id)
    except ValueError:
        return None
    if len(token) != _TOKEN_HEX_LEN:
        return None
    try:
        bytes.fromhex(token)
    except ValueError:
        return None
    return DeploymentIdentity(deployment_id=dep_id, deployment_token=token)


async def load(client: _OrgSettingsClient) -> DeploymentIdentity | None:
    """Read the stored identity without creating one."""
    return _parse(await client.get_org_settings(DEPLOYMENT_ORG_ID))


async def _write_and_adopt(client: _OrgSettingsClient, fresh: DeploymentIdentity) -> DeploymentIdentity:
    await client.update_org_settings(DEPLOYMENT_ORG_ID, fresh.as_settings(), changed_by=CHANGED_BY)
    # Read-after-write. Two replicas booting together both write; whichever
    # landed last is the deployment's id, and the loser adopts it here rather
    # than beating under a phantom id that the next cycle would abandon.
    stored = _parse(await client.get_org_settings(DEPLOYMENT_ORG_ID))
    if stored is None:
        # The read raced a still-committing write or the row is unreadable;
        # use what we wrote for this cycle and re-read next time.
        return fresh
    if stored.deployment_id != fresh.deployment_id:
        logger.debug("[telemetry] adopted deployment_id written by another replica")
    return stored


async def load_or_create(client: _OrgSettingsClient) -> DeploymentIdentity:
    """Return the stored identity, generating and persisting one if absent."""
    existing = await load(client)
    if existing is not None:
        return existing
    return await _write_and_adopt(client, generate())


async def rotate(client: _OrgSettingsClient) -> DeploymentIdentity:
    """Replace the stored identity with a fresh id and token."""
    return await _write_and_adopt(client, generate())
