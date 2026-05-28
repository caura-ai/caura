"""Typed payload for ``memclaw.org.suppression-changed`` (CAURA-694).

Published by enterprise platform-admin-api on every soft-delete /
restore of an organization. Carries the list of tenant_ids the action
applies to plus an ``action`` discriminator so a single topic covers
both directions of the lifecycle. The OSS subscriber upserts a row in
``public.tenant_suppression`` per tenant_id: ``suppress`` sets
``suppressed_at = now()``; ``restore`` clears it.

Out of band: the **synchronous** check happens at the auth-api layer
(CAURA-690) so an unexpired JWT cannot reach a soft-deleted org's data
even before this event lands. This event is the **durable** mirror —
core-api uses it to reject **API-key**-credentialed callers (which
don't traverse auth-api) and to defend OSS standalone deployments
where there is no auth-api in front.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class OrgSuppressionEvent(BaseModel):
    """Payload of ``memclaw.org.suppression-changed``.

    ``tenant_ids`` is the explicit list of tenants the action covers —
    one org has many tenants, and the publisher (platform-admin-api)
    already knows the list at decision time, so we don't make the
    consumer re-resolve it. Empty list is valid (a soft-delete of an
    org with zero tenants is a no-op for the mirror but still
    well-formed).

    ``action`` is the discriminator. Using ``Literal`` rather than a
    free string means Pydantic validates the value and consumers can
    pattern-match safely without a defensive ``else: raise``.
    """

    tenant_ids: list[str] = Field(default_factory=list)
    action: Literal["suppress", "restore"]
