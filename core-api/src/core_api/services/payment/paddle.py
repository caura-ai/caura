"""Paddle payment provider."""

import hashlib
import hmac
from typing import Any

from core_api.config import settings
from core_api.services.payment.protocol import NormalizedWebhookEvent


class PaddleProvider:
    provider_name = "paddle"

    def get_billing_config(self) -> dict[str, Any]:
        return {
            "provider": "paddle",
            "client_token": settings.paddle_client_token,
            "environment": settings.paddle_environment,
            "prices": {
                "pro_monthly": settings.paddle_pro_monthly_price_id,
                "pro_annual": settings.paddle_pro_annual_price_id,
                "business_monthly": settings.paddle_business_monthly_price_id,
                "business_annual": settings.paddle_business_annual_price_id,
            },
        }

    def verify_webhook(self, body: bytes, headers: dict[str, str]) -> bool:
        secret = settings.paddle_webhook_secret
        if not secret:
            return False
        sig_header = headers.get("paddle-signature", "")
        if not sig_header:
            return False
        try:
            parts = dict(p.split("=", 1) for p in sig_header.split(";"))
            ts = parts.get("ts", "")
            h1 = parts.get("h1", "")
            signed_payload = ts.encode() + b":" + body
            expected = hmac.new(secret.encode(), signed_payload, hashlib.sha256).hexdigest()
            return hmac.compare_digest(h1, expected)
        except Exception:
            return False

    def normalize_webhook(self, payload: dict[str, Any]) -> NormalizedWebhookEvent:
        data = payload.get("data", {})
        return NormalizedWebhookEvent(
            event_type=payload.get("event_type", ""),
            event_id=payload.get("event_id", ""),
            customer_id=data.get("customer_id"),
            custom_data=data.get("custom_data", {}),
            raw=payload,
        )

    async def create_checkout(self, variant_id: str, custom_data: dict[str, str]) -> str | None:
        return None  # Paddle checkout is client-side only
