"""Versioned API shapes. Identity and timestamps cannot be supplied by senders."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .envelope import Envelope, Kind


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class MemoryContextRequest(StrictModel):
    message_id: str | None = Field(default=None, min_length=1, max_length=80)
    delivery_id: str | None = Field(default=None, min_length=1, max_length=80)

    @model_validator(mode="after")
    def exactly_one_reference(self):
        if (self.message_id is None) == (self.delivery_id is None):
            raise ValueError("provide exactly one of message_id or delivery_id")
        return self


class SendMessage(StrictModel):
    to: list[str] = Field(min_length=1, max_length=100)
    body: str = Field(min_length=1, max_length=65536)
    kind: Kind = "info"
    thread_id: str | None = Field(default=None, max_length=80)
    reply_to: str | None = Field(default=None, max_length=80)

    expect_reply_within_seconds: int | None = Field(default=None, ge=60, le=604800)
    capability: str | None = Field(default=None, min_length=1, max_length=80)

    @field_validator("to")
    @classmethod
    def recipients(cls, value: list[str]) -> list[str]:
        if any(not p.strip() or len(p) > 256 or p == "*" for p in value):
            raise ValueError("provide explicit agent IDs; expand broadcasts before sending")
        if len(set(value)) != len(value):
            raise ValueError("duplicate recipient")
        return sorted(value)

    @model_validator(mode="after")
    def response_has_parent(self):
        if self.kind != "request" and (
            self.expect_reply_within_seconds is not None or self.capability is not None
        ):
            raise ValueError("reply timeout and capability require kind=request")
        if self.kind == "response" and not self.reply_to:
            raise ValueError("responses require reply_to")
        return self


class Receipt(StrictModel):
    message_id: str
    thread_id: str
    recipients: list[str]
    status: Literal["accepted"] = "accepted"
    duplicate: bool = False


class Claim(StrictModel):
    delivery_id: str
    lease_token: str | None = Field(default=None, repr=False)
    lease_expires_at: str | None = None
    attempt: int
    envelope: Envelope
    resume_context: dict | None = None
    event_cursor: int = 0
    state: str = "leased"
    processing_deadline: str | None = None
    extension_count: int = 0
    intervention: dict | None = None


class LeaseAction(StrictModel):
    lease_token: str | None = Field(default=None, min_length=1, max_length=128)


class ClaimRequest(StrictModel):
    lease_seconds: int = Field(default=30, ge=5, le=300)


class WaitRequest(ClaimRequest):
    session_id: str = Field(min_length=1, max_length=128)
    timeout: float = Field(default=20, ge=0, le=20)


class ReplyRequest(LeaseAction):
    body: str = Field(min_length=1, max_length=65536)
    reply_to: str | None = Field(default=None, max_length=80)
    ack: bool = True


class ProgressRequest(LeaseAction):
    idempotency_key: str = Field(min_length=1, max_length=128)
    summary: str = Field(min_length=1, max_length=2000)
