from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from decimal import Decimal
from typing import Annotated, Any, Self

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


class CanonicalModel(_BaseDomainModel):
    """A frozen model whose every instance is guaranteed already canonical.

    The base exists for one reason: :meth:`~pydantic.BaseModel.model_copy`
    **skips validation**, and for an identity type that is not a convenience,
    it is a hole. Reproduced before this override existed::

        forged = identity.model_copy(update={"contract_year": 6})
        forged.canonical()                  # 'CME:ES:M0006'
        FuturesContractId.parse(_)          # ValidationError

    That is a serializer emitting a string its own parser rejects — a
    catalogue key written today that cannot be read back tomorrow — reached
    through an ordinary-looking public method whose documentation gives no hint
    that validation is skipped. ``update={"contract_year": True}`` produced year
    1 the same way, and an unrelated key such as ``tick_size`` was accepted into
    the model despite ``extra="forbid"``.

    Re-validating the copy closes it. The cost is one extra validation pass on
    a method that is not on any hot path.

    What is deliberately **not** defended against, so the guarantee is not
    overstated: :meth:`~pydantic.BaseModel.model_construct` and
    ``object.__setattr__``. Both are explicit, documented bypasses that announce
    themselves at the call site — the equivalent of casting away a const — and
    a type that tried to block them would be fighting the language rather than
    protecting a caller from an easy mistake.

    Lives here rather than in ``futures_contract`` — where it was introduced by
    ADR-009 — because it is now the base for identity types in several packages,
    and reaching across packages for a leading-underscore name would make every
    such import a private-API import. ``futures_contract._CanonicalModel``
    remains as an alias so nothing that already used it had to change.
    """

    def model_copy(self, *, update: Mapping[str, Any] | None = None, deep: bool = False) -> Self:
        """Return a validated copy, unlike the unvalidated Pydantic default."""
        copied = super().model_copy(update=update, deep=deep)
        return type(self).model_validate(dict(copied.__dict__))


class _UtcTimestampModel(_BaseDomainModel):
    """Shared timestamp model enforcing UTC-only semantics for domain events."""

    timestamp: datetime = Field(...)

    @field_validator("timestamp", mode="before")
    @classmethod
    def validate_timestamp(cls, value: object) -> datetime:
        # Enforce UTC-only timestamps so replay and backtest semantics remain consistent.
        return validate_utc_datetime(value)
