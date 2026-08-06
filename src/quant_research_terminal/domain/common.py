from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Annotated

from pydantic import (
    AfterValidator,
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    field_validator,
)

from quant_research_terminal.domain.numeric import validate_price, validate_quantity
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


def _validate_price(value: Decimal) -> Decimal:
    return validate_price(value)


def _validate_quantity(value: Decimal) -> Decimal:
    return validate_quantity(value)


#: A price, constrained to the canonical numeric envelope.
#:
#: Constructing a model with one of these guarantees the value encodes exactly
#: into storage's fixed-point representation: finite, strictly positive, within
#: the representable magnitude, and representable within six decimal places.
#: Nothing is rounded to make it fit.
PriceDecimal = Annotated[Decimal, BeforeValidator(_coerce_decimal), AfterValidator(_validate_price)]

#: A quantity — a size or a volume — constrained to the canonical envelope.
#:
#: Quantities are counts, so they must additionally be whole numbers within
#: unsigned 64-bit range. See :mod:`quant_research_terminal.domain.numeric`.
QuantityDecimal = Annotated[
    Decimal, BeforeValidator(_coerce_decimal), AfterValidator(_validate_quantity)
]

#: Retained name for values needing only strict positivity.
#:
#: Superseded by :data:`PriceDecimal` and :data:`QuantityDecimal`, which also
#: guarantee exact storage encodability. Kept for any future field that is
#: genuinely unconstrained beyond positivity.
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
