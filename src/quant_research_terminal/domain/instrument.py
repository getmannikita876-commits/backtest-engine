from __future__ import annotations

from datetime import datetime

from pydantic import Field, field_validator

from quant_research_terminal.domain.common import _BaseDomainModel
from quant_research_terminal.domain.time import validate_utc_datetime


class Instrument(_BaseDomainModel):
    """Immutable instrument contract used to describe a tradable symbol."""

    symbol: str = Field(min_length=1)
    exchange: str = Field(min_length=1)
    asset_class: str = Field(min_length=1)
    currency: str = Field(min_length=1)
    created_at: datetime = Field(...)

    @field_validator("created_at", mode="before")
    @classmethod
    def validate_created_at(cls, value: object) -> datetime:
        # Instrument creation time must be timezone-aware UTC to preserve deterministic ordering.
        return validate_utc_datetime(value)
