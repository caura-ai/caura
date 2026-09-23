"""Agent discovery, runtime checkpoints and human intervention contracts."""

from typing import Literal

from pydantic import Field, field_validator

from .protocol import StrictModel


class Presence(StrictModel):
    session_id: str = Field(min_length=1, max_length=128)
    display_name: str = Field(min_length=1, max_length=120)
    description: str = Field(default="", max_length=2000)
    capabilities: list[str] = Field(default_factory=list, max_length=50)
    status: Literal["ready", "busy", "offline"] = "ready"
    supports_interrupt: bool = False

    @field_validator("capabilities")
    @classmethod
    def bounded_capabilities(cls, values):
        if any(not v.strip() or len(v) > 80 for v in values):
            raise ValueError("capabilities must contain 1–80 characters")
        return sorted(set(values))


class Checkpoint(StrictModel):
    delivery_id: str = Field(min_length=1, max_length=80)
    lease_token: str = Field(min_length=1, max_length=128)
    checkpoint_key: str = Field(min_length=1, max_length=128)
    summary: str = Field(min_length=1, max_length=2000)
    proposed_action: str = Field(min_length=1, max_length=4000)
    action_type: Literal["read", "write", "external", "destructive"] = "read"
    confidence: float = Field(default=1, ge=0, le=1)
    missing_information: list[str] = Field(default_factory=list, max_length=20)
    conflicting_results: bool = False
    request_human: bool = False


class HumanDecision(StrictModel):
    version: int = Field(ge=1)
    action: Literal["approve", "reject", "redirect"]
    instructions: str = Field(default="", max_length=4000)
    allow_unconfirmed: bool = False


class Interrupt(StrictModel):
    delivery_id: str = Field(min_length=1, max_length=80)
    reason: str = Field(min_length=1, max_length=2000)


class CollaborationPolicy(StrictModel):
    messaging_scope: Literal["same_fleet", "tenant"] = "same_fleet"
    processing_timeout_seconds: int = Field(default=600, ge=30, le=86400)
    max_extensions: int = Field(default=6, ge=0, le=100)
    minimum_confidence: float = Field(default=0.75, ge=0, le=1)
    require_human_for: list[Literal["read", "write", "external", "destructive"]] = Field(
        default_factory=lambda: ["external", "destructive"]
    )
    escalate_missing_information: bool = True
    escalate_conflicts: bool = True
