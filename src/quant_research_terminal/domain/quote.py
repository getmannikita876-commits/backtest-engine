from __future__ import annotations

from pydantic import Field, model_validator

from quant_research_terminal.domain.common import PositiveDecimal, _UtcTimestampModel


class Quote(_UtcTimestampModel):
    """Immutable quote contract with bid/ask validation."""

    instrument_symbol: str = Field(min_length=1)
    bid: PositiveDecimal
    ask: PositiveDecimal
    bid_size: PositiveDecimal
    ask_size: PositiveDecimal

    @model_validator(mode="after")
    def validate_bid_ask(self) -> Quote:
        # Reject inverted quotes so downstream code can rely on a valid spread.
        if self.bid > self.ask:
            raise ValueError("bid must be less than or equal to ask")
        return self
