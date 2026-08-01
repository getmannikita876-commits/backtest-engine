from __future__ import annotations

from pydantic import Field, model_validator

from quant_research_terminal.domain.common import PositiveDecimal, _UtcTimestampModel


class Bar(_UtcTimestampModel):
    """Immutable OHLCV bar contract for time-series data."""

    instrument_symbol: str = Field(min_length=1)
    open: PositiveDecimal
    high: PositiveDecimal
    low: PositiveDecimal
    close: PositiveDecimal
    volume: PositiveDecimal

    @model_validator(mode="after")
    def validate_ohlc(self) -> Bar:
        # Preserve bar integrity by ensuring the range and OHLC values are internally consistent.
        if self.high < self.low:
            raise ValueError("high must be greater than or equal to low")
        if self.open < self.low or self.open > self.high:
            raise ValueError("open must be within the bar range")
        if self.close < self.low or self.close > self.high:
            raise ValueError("close must be within the bar range")
        return self
