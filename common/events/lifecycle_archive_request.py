"""Typed payload for the ``memclaw.lifecycle.<action>-requested`` topics
(CAURA-655). Both ``archive-expired`` and ``archive-stale`` share this
model — the consumer's per-action behaviour is parameterised by the
topic, not by payload fields. ``audit_id`` ties the message back to
the row pre-published by the core-api fanout endpoint.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class LifecycleArchiveRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    audit_id: int
    org_id: str
    triggered_by: str
    fleet_id: str | None = None
