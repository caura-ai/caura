"""Agent identity and per-agent local config schemas."""

from __future__ import annotations

from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator


class AgentInfo(BaseModel):
    """Expected identity; the platform credential is authoritative."""

    model_config = ConfigDict(extra="ignore")

    agent_id: str
    tenant_id: str
    fleet_id: str | None = None
    description: str = ""


class AgentConfig(BaseModel):
    """API location and expected identity. Keys are supplied through the environment."""

    model_config = ConfigDict(extra="forbid")

    agent: AgentInfo
    peers: list[str] = Field(default_factory=list)
    api_url: str
    allow_insecure_http: bool = False

    @field_validator("api_url")
    @classmethod
    def valid_url(cls, value: str) -> str:
        url = urlsplit(value)
        if (
            url.scheme not in {"https", "http"}
            or not url.hostname
            or url.username
            or url.password
            or url.query
            or url.fragment
        ):
            raise ValueError("api_url must be a Caura HTTP(S) origin without credentials, query or fragment")
        if url.path not in {"", "/"}:
            raise ValueError("api_url must be the gateway origin, without /api/v1")
        return value.rstrip("/")
