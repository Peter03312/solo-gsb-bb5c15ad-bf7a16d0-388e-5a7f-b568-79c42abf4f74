"""Request/response schemas."""
from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field, model_validator

MIN_TEMPERATURE = -273.15  # absolute zero
MAX_TEMPERATURE = 500.0


class ReadingIn(BaseModel):
    """Payload for submitting a new reading."""

    event_id: str = Field(min_length=1, max_length=128, examples=["r-0001"])
    sampled_at: datetime = Field(examples=["2026-10-01T08:00:00Z"])
    temperature: float = Field(
        allow_inf_nan=False, ge=MIN_TEMPERATURE, le=MAX_TEMPERATURE, examples=[9.5]
    )


class CorrectionIn(BaseModel):
    """Payload for correcting an existing reading (at least one field)."""

    sampled_at: datetime | None = None
    temperature: float | None = Field(
        default=None, allow_inf_nan=False, ge=MIN_TEMPERATURE, le=MAX_TEMPERATURE
    )

    @model_validator(mode="after")
    def _at_least_one_field(self) -> "CorrectionIn":
        if self.sampled_at is None and self.temperature is None:
            raise ValueError(
                "correction requires at least one of 'sampled_at' or 'temperature'"
            )
        return self


class ReadingOut(BaseModel):
    event_id: str
    sampled_at: datetime
    temperature: float


class Interval(BaseModel):
    """An over-temperature interval, identified by start_event_id."""

    start_event_id: str
    start_sampled_at: datetime
    end_event_id: str | None = None
    end_sampled_at: datetime | None = None


class MutationResult(BaseModel):
    """Result of a submit/correct/revoke call: the device's current
    intervals plus the delta caused by this call."""

    device_id: str
    duplicate: bool = False
    intervals: list[Interval]
    added: list[Interval]
    changed: list[Interval]
    invalidated: list[Interval]
