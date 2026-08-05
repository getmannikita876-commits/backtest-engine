from __future__ import annotations

from datetime import datetime, timedelta

from pydantic import Field, field_validator, model_validator

from quant_research_terminal.domain.common import PositiveDecimal, _BaseDomainModel
from quant_research_terminal.domain.time import validate_utc_datetime

_ZERO = timedelta(0)


class Bar(_BaseDomainModel):
    """Immutable OHLCV bar contract for time-series data.

    Temporal model
    --------------
    A bar has two independent temporal coordinates, and conflating them is how
    look-ahead bias enters a backtest unnoticed:

    * :attr:`interval_start` — the instant the period the bar summarises began.
    * :attr:`interval` — how long that period lasts.

    From those, :attr:`availability_time` is *derived*: a bar's open, high,
    low, close, and volume are not all determined until the period ends, so the
    completed bar cannot be known before ``interval_start + interval``.

    Availability is a property rather than a stored field on purpose. A bar
    whose availability precedes its interval close is not merely invalid, it is
    **unconstructible** — there is no field to put a wrong value in. That is
    what makes look-ahead impossible by construction rather than by validation.

    :attr:`timestamp` is an alias for :attr:`availability_time`, so every
    market event in the domain — trade, quote, bar — answers ``timestamp`` with
    "when this became knowable". Replay ordering reads that one name uniformly.

    Session boundaries
    ------------------
    ``interval_start + interval`` is a *nominal* close. The domain has no
    exchange calendar, so a shortened session (a half-day or an early holiday
    close) is not modelled and a daily bar's nominal close can fall later than
    the venue's actual close. Erring late is the safe direction: a bar can
    become visible later than it truly was, which costs realism, but never
    earlier, which would cost correctness. Calendar-aware refinement is future
    work and is recorded in ``docs/adr/ADR-002-bar-availability-time.md``.
    """

    instrument_symbol: str = Field(min_length=1)
    interval_start: datetime
    interval: timedelta
    open: PositiveDecimal
    high: PositiveDecimal
    low: PositiveDecimal
    close: PositiveDecimal
    volume: PositiveDecimal

    @field_validator("interval_start", mode="before")
    @classmethod
    def validate_interval_start(cls, value: object) -> datetime:
        # Enforce UTC-only timestamps so replay and backtest semantics remain
        # consistent. The value is never converted from another zone.
        return validate_utc_datetime(value)

    @field_validator("interval")
    @classmethod
    def validate_interval(cls, value: timedelta) -> timedelta:
        # A zero or negative interval would place a bar's availability at or
        # before its own start, which is exactly the look-ahead this model
        # exists to prevent.
        if value <= _ZERO:
            raise ValueError("interval must be strictly positive")
        return value

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

    @property
    def interval_end(self) -> datetime:
        """Return the instant the bar's period closes."""
        return self.interval_start + self.interval

    @property
    def availability_time(self) -> datetime:
        """Return when the completed bar first became knowable.

        Equal to :attr:`interval_end`: the values are not all determined until
        the period is over.
        """
        return self.interval_end

    @property
    def timestamp(self) -> datetime:
        """Return the event time used for replay ordering.

        Alias of :attr:`availability_time`, so a bar orders against trades and
        quotes by when it became knowable rather than by when its period began.
        """
        return self.availability_time
