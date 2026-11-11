"""Internal rule-fetch evidence contract; no caller-selected match window."""

from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

BUCKET_SECONDS = 60
CACHE_SECONDS = 300
BUCKET_SLACK_SECONDS = 60
RETENTION_DAYS = 90
MAX_CHECKS = 100


class FetchBinding(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tenant_id: str = Field(min_length=1, max_length=255)
    install_uuid: UUID
    # The resolved rule recipient, not an authentication claim. Empty/None
    # means the tenant-wide principal; credential attribution is separate.
    principal: str | None = Field(default=None, max_length=255)
    fleet_id: str | None = Field(default=None, max_length=255)
    rule_set_hash: str = Field(pattern=r"^[0-9a-f]{64}$")


class FetchRecord(FetchBinding):
    credential_id: str = Field(min_length=1, max_length=255)
    served_at: AwareDatetime
    truncated: bool = False


class FetchCheck(FetchBinding):
    delivered_at: AwareDatetime


class FetchMatchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    checks: list[FetchCheck] = Field(min_length=1, max_length=MAX_CHECKS)


class ScopedFetchMatchRequest(FetchMatchRequest):
    tenant_id: str = Field(min_length=1, max_length=255)
