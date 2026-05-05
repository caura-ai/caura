"""Typed payloads for ``memclaw.lifecycle.<action>-requested`` topics.

Both archive ops (CAURA-655) share one model — the per-action
behaviour is parameterised by the topic, not by payload fields.
``LifecycleRequestBase`` exposes the four fields every lifecycle
payload carries; per-action subclasses (e.g.
:class:`~common.events.lifecycle_purge_request.LifecyclePurgeRequest`)
add their action-specific fields without redefining the shared ones.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class LifecycleRequestBase(BaseModel):
    """Fields every lifecycle Pub/Sub payload carries: the audit-row
    pointer (``audit_id``), the org scope (``org_id``), provenance
    (``triggered_by``), and an optional fleet narrowing.

    The shared handler reads only these fields and is generic over
    subclasses. New actions extend by subclassing and adding their
    own action-specific Pydantic fields.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    audit_id: int
    org_id: str
    triggered_by: str
    fleet_id: str | None = None


class LifecycleArchiveRequest(LifecycleRequestBase):
    """No additional fields beyond the base — both archive ops are
    keyed entirely by their topic, with no per-action data.
    """
