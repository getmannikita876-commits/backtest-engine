from __future__ import annotations

from pydantic import Field

from quant_research_terminal.domain.common import PositiveDecimal, _UtcTimestampModel


class Trade(_UtcTimestampModel):
    """Immutable trade contract for a single execution event."""

    instrument_symbol: str = Field(min_length=1)
    price: PositiveDecimal
    size: PositiveDecimal
    side: str = Field(min_length=1)
