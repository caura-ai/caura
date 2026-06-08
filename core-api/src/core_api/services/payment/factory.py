"""Payment provider factory."""

from core_api.config import settings
from core_api.services.payment.protocol import PaymentProvider


def get_payment_provider() -> PaymentProvider:
    name = settings.payment_provider
    if name == "paddle":
        from core_api.services.payment.paddle import PaddleProvider

        return PaddleProvider()
    raise ValueError(f"Unknown payment provider: {name}")
