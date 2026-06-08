"""Config-file identity resolver for single-tenant OSS deployments.

Returns a fixed Identity from constructor arguments. No auth server needed.
Context dict may override tenant_id and user_id but not roles.
"""

from __future__ import annotations

import logging
from typing import Any

from core_api.protocols import Identity

logger = logging.getLogger(__name__)


class ConfigIdentity:
    """Static identity resolver driven by constructor args / config file."""

    def __init__(
        self,
        tenant_id: str = "default",
        user_id: str | None = None,
        roles: list[str] | None = None,
    ) -> None:
        self._tenant_id = tenant_id
        self._user_id = user_id
        self._roles = roles if roles is not None else ["admin"]

    async def resolve(self, context: dict[str, Any]) -> Identity:
        identity = Identity(
            tenant_id=context.get("tenant_id", self._tenant_id),
            user_id=context.get("user_id", self._user_id),
            roles=list(self._roles),
        )
        logger.debug(
            "ConfigIdentity resolved: tenant=%s user=%s",
            identity.tenant_id,
            identity.user_id,
        )
        return identity
