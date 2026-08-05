from __future__ import annotations

from pydantic import Field, model_validator

from quant_research_terminal.domain.common import (
    PriceDecimal,
    QuantityDecimal,
    _UtcTimestampModel,
)


class Quote(_UtcTimestampModel):
    """Immutable quote contract with bid/ask validation."""

    instrument_symbol: str = Field(min_length=1)
    bid: PriceDecimal
    ask: PriceDecimal
    bid_size: QuantityDecimal
    ask_size: QuantityDecimal

    @model_validator(mode="after")
    def validate_bid_ask(self) -> Quote:
        # Reject inverted quotes so downstream code can rely on a valid spread.
        if self.bid > self.ask:
            raise ValueError("bid must be less than or equal to ask")
        return self
