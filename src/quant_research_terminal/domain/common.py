from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Annotated

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, field_validator

from quant_research_terminal.domain.time import validate_utc_datetime


def _coerce_decimal(value: object) -> Decimal:
    """Coerce decimal-like inputs into Decimal without permitting float conversion."""
    if isinstance(value, Decimal):
        return value
    if isinstance(value, bool):
        raise ValueError("value must be a decimal-like number")
    if isinstance(value, int):
        return Decimal(value)
    if isinstance(value, str):
        return Decimal(value)
    raise ValueError("value must be a decimal-like number")


PositiveDecimal = Annotated[Decimal, BeforeValidator(_coerce_decimal), Field(gt=0)]


class _BaseDomainModel(BaseModel):
    """Base model with immutable semantics for the domain package.

    ``extra="forbid"`` matters as much as ``frozen``: a field that a model no
    longer stores must raise rather than be silently discarded, so a caller
    still passing a retired keyword learns immediately instead of constructing
    a model that quietly ignored part of its input.
    """

    model_config = ConfigDict(frozen=True, strict=True, extra="forbid")


class _UtcTimestampModel(_BaseDomainModel):
    """Shared timestamp model enforcing UTC-only semantics for domain events."""

    timestamp: datetime = Field(...)

    @field_validator("timestamp", mode="before")
    @classmethod
    def validate_timestamp(cls, value: object) -> datetime:
        # Enforce UTC-only timestamps so replay and backtest semantics remain consistent.
        return validate_utc_datetime(value)
