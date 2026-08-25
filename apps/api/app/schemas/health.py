"""Health endpoint response contracts."""

from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class DependencyStatus(StrEnum):
    OK = "ok"
    UNAVAILABLE = "unavailable"


class HealthResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    status: Literal["ok"]
    service: str


class ComponentReadiness(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    status: DependencyStatus
    detail: Literal["reachable", "unreachable"]
    latency_ms: float = Field(ge=0)


class ReadinessComponents(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    postgresql: ComponentReadiness
    redis: ComponentReadiness


class ReadinessResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    status: DependencyStatus
    service: str
    components: ReadinessComponents

    @property
    def is_ready(self) -> bool:
        return self.status is DependencyStatus.OK
