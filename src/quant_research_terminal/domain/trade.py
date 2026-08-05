from __future__ import annotations

from pydantic import Field, field_validator

from quant_research_terminal.domain.common import PositiveDecimal, _UtcTimestampModel
from quant_research_terminal.domain.trade_side import TradeSide, parse_trade_side


class Trade(_UtcTimestampModel):
    """Immutable trade contract for a single execution event.

    ``side`` is a :class:`TradeSide`, not a string. The enum's canonical
    values are accepted on construction so stored rows and configuration need
    no special handling, but the validated field is always the enum, and an
    unrecognised value is rejected rather than coerced.
    """

    instrument_symbol: str = Field(min_length=1)
    price: PositiveDecimal
    size: PositiveDecimal
    side: TradeSide

    @field_validator("side", mode="before")
    @classmethod
    def validate_side(cls, value: object) -> TradeSide:
        # Strict mode would otherwise reject the plain strings that stored rows
        # and serialized configuration naturally carry.
        return parse_trade_side(value)
