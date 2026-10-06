"""Caura Bus: all messaging crosses the authenticated Caura platform API."""

from .agent import AgentConfig, AgentInfo
from .bus import Bus, PlatformError
from .config import CONFIG_ENV_VAR, load_config, require_api_key
from .consult import (
    Answer,
    Collection,
    ConsultationBudget,
    ConsultationCycleError,
    ConsultationLimitError,
    PresentedResponses,
    ResponseCollector,
    collect_responses,
)
from .envelope import Envelope, Kind, new_msg_id, new_thread_id, now_ms
from .protocol import Claim, Receipt, SendMessage

__all__ = [
    "CONFIG_ENV_VAR",
    "AgentConfig",
    "AgentInfo",
    "Answer",
    "Bus",
    "Claim",
    "Collection",
    "ConsultationBudget",
    "ConsultationCycleError",
    "ConsultationLimitError",
    "Envelope",
    "Kind",
    "PlatformError",
    "PresentedResponses",
    "Receipt",
    "ResponseCollector",
    "SendMessage",
    "collect_responses",
    "load_config",
    "new_msg_id",
    "new_thread_id",
    "now_ms",
    "require_api_key",
]
