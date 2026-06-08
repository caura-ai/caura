"""Payment gateway protocol — thin abstraction over payment providers."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable


@dataclass
class NormalizedWebhookEvent:
    """Provider-agnostic webhook event."""

    event_type: str  # subscription.activated | .updated | .past_due | .canceled
    event_id: str
    customer_id: str | None
    custom_data: dict[str, Any] = field(default_factory=dict)
    raw: dict[str, Any] = field(default_factory=dict)


@runtime_checkable
class PaymentProvider(Protocol):
    provider_name: str

    def get_billing_config(self) -> dict[str, Any]: ...

    def verify_webhook(self, body: bytes, headers: dict[str, str]) -> bool: ...

    def normalize_webhook(self, payload: dict[str, Any]) -> NormalizedWebhookEvent: ...

    async def create_checkout(self, variant_id: str, custom_data: dict[str, str]) -> str | None: ...
