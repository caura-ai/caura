"""Payload for an API-triggered crystallization run (OSS #817).

Deliberately NOT a :class:`~common.events.lifecycle_archive_request.LifecycleRequestBase`
subclass, and deliberately on its own topic rather than reusing
``CRYSTALLIZE_REQUESTED``. Two reasons, both about the nightly fanout's handler
rather than the payload:

* it requires an ``audit_id`` and writes progress back to that ``lifecycle_audit``
  row. An API trigger has no audit row, and inventing one would put manual runs
  into the record of the scheduled sweep;
* it dedups on a 24-hour window (``has_recent_lifecycle_success``). That is right
  for a nightly fanout and wrong for a person pressing "crystallize now" — the
  request would be silently skipped because last night's run succeeded.

So this carries what an on-demand run actually needs, including the id of the
report row the API ALREADY reserved. The consumer executes that row rather than
reserving its own, which is what lets the endpoint return a pollable id
synchronously while the work happens off the request.
"""

from __future__ import annotations

from pydantic import BaseModel


class CrystallizeOnDemandRequest(BaseModel):
    """One API-triggered crystallization run, for an already-reserved report.

    ``report_id`` is the contract with the publisher: the row exists and is
    ``status='running'``, so the consumer's job is to give it a terminal status.
    A consumer that reserved its own row instead would leave the caller polling an
    id that nothing ever finishes — the wedge this change exists to remove.
    """

    tenant_id: str
    report_id: str
    fleet_id: str | None = None
    # Read by the publisher from the org's resolved config and carried, rather
    # than re-resolved in the consumer: the value that applied when the caller
    # asked is the one their run should use, and re-resolving would let a settings
    # change between publish and delivery silently alter what was requested.
    auto_crystallize: bool = True
