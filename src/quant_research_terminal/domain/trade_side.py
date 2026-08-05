"""The aggressor side of a trade.

A trade's side records which party crossed the spread. It has exactly three
states, and the third is not a defect: many venues and vendors publish trades
they cannot attribute, and a research platform must be able to say so.

Before this type existed, ``Trade.side`` was an unconstrained string. That
allowed a vendor's private code to reach the domain verbatim, and left a
consumer testing ``side == "buy"`` silently treating an unattributed trade as a
sell. Making the vocabulary a type removes both failures.

``UNKNOWN`` is never inferred. A provider that cannot attribute a side records
``UNKNOWN``; nothing anywhere promotes it to a direction, because guessing
would fabricate order flow that the venue never reported.
"""

from __future__ import annotations

from enum import StrEnum


class TradeSide(StrEnum):
    """Which side initiated a trade.

    Attributes:
        BUY: A buy aggressor lifted the offer.
        SELL: A sell aggressor hit the bid.
        UNKNOWN: The source could not attribute the aggressor. This is a
            recorded fact, not a missing value, and must never be treated as a
            direction.
    """

    BUY = "buy"
    SELL = "sell"
    UNKNOWN = "unknown"

    @property
    def is_directional(self) -> bool:
        """Return whether this side identifies an actual aggressor.

        Order-flow calculations should consult this rather than comparing
        against ``BUY``/``SELL`` individually, so an unattributed trade is
        excluded explicitly instead of falling into whichever branch is last.
        """
        return self is not TradeSide.UNKNOWN


def parse_trade_side(value: object) -> TradeSide:
    """Coerce an external value to a :class:`TradeSide`.

    Accepts a :class:`TradeSide` or one of its canonical string values,
    case-insensitively and ignoring surrounding whitespace, so configuration
    and stored data need no special handling at the boundary.

    Anything else is rejected. An unrecognised code is *not* mapped to
    ``UNKNOWN``: that would erase the difference between "the source said it
    could not attribute this trade" and "we failed to understand the source".

    Raises:
        ValueError: if the value is not a recognised side.
    """
    if isinstance(value, TradeSide):
        return value
    if isinstance(value, str):
        try:
            return TradeSide(value.strip().lower())
        except ValueError as exc:
            accepted = ", ".join(side.value for side in TradeSide)
            raise ValueError(f"side must be one of: {accepted}; got {value!r}") from exc
    raise ValueError(f"side must be a TradeSide or one of its string values; got {value!r}")
