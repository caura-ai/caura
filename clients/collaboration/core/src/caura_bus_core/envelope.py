"""Envelope schema for caura-bus.

The wire shape is stable across V1; see SPEC.md "Envelope" section.
"""

from __future__ import annotations

import time
from typing import Literal

import ulid
from pydantic import BaseModel, ConfigDict, Field

Kind = Literal["info", "request", "response", "ack"]


def new_msg_id() -> str:
    return f"msg_{ulid.new()!s}"


def new_thread_id() -> str:
    return f"t_{ulid.new()!s}"


def now_ms() -> int:
    return int(time.time() * 1000)


class Envelope(BaseModel):
    # `from` is reserved in Python, so the attribute is `from_` and the wire
    # name is set via alias. populate_by_name lets callers pass either form.
    model_config = ConfigDict(populate_by_name=True)

    id: str
    from_: str = Field(alias="from")
    to: list[str]
    kind: Kind = "info"
    thread_id: str
    ts: int
    body: str
    parts: list[dict] = Field(default_factory=list)
    correlation_id: str | None = None
    tenant_id: str | None = None
    fleet_id: str | None = None

    @classmethod
    def new(
        cls,
        *,
        from_: str,
        to: list[str],
        body: str,
        thread_id: str | None = None,
        kind: Kind = "info",
        correlation_id: str | None = None,
        tenant_id: str | None = None,
        fleet_id: str | None = None,
    ) -> Envelope:
        return cls(
            id=new_msg_id(),
            from_=from_,
            to=list(to),
            kind=kind,
            thread_id=thread_id or new_thread_id(),
            ts=now_ms(),
            body=body,
            parts=[],
            correlation_id=correlation_id,
            tenant_id=tenant_id,
            fleet_id=fleet_id,
        )
