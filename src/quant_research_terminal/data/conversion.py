from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import TypedDict

import pyarrow as pa  # type: ignore[import-untyped]

from quant_research_terminal.data.contracts import (
    PRICE_ENCODING,
    PRICE_PRECISION,
    PRICE_QUANTUM,
    PRICE_SCALE,
    SCHEMA_NAME,
    SCHEMA_VERSION,
    TIMESTAMP_TIMEZONE,
    UINT64_MAX,
)
from quant_research_terminal.domain.models import Bar, Quote, Trade, parse_trade_side
from quant_research_terminal.domain.numeric import (
    MAX_PRICE_FIXED_POINT,
    MAX_QUANTITY_INTEGER,
    fixed_point_to_price,
    price_to_fixed_point,
    validate_price,
    validate_quantity,
)


class TradeStorageRow(TypedDict):
    timestamp: datetime
    instrument_symbol: str
    price: int
    size: int
    side: str


class QuoteStorageRow(TypedDict):
    timestamp: datetime
    instrument_symbol: str
    bid: int
    ask: int
    bid_size: int
    ask_size: int


class BarStorageRow(TypedDict):
    timestamp: datetime
    instrument_symbol: str
    interval_microseconds: int
    open: int
    high: int
    low: int
    close: int
    volume: int


_MICROSECOND = timedelta(microseconds=1)


def _timedelta_to_microseconds(value: object, field_name: str) -> int:
    """Encode an interval as whole microseconds.

    ``timedelta`` is already microsecond-resolution, so the encoding is exact
    and no rounding rule is needed.
    """
    if not isinstance(value, timedelta):
        raise TypeError(f"{field_name} must be a timedelta")
    if value <= timedelta(0):
        raise ValueError(f"{field_name} must be strictly positive")
    microseconds = value // _MICROSECOND
    if microseconds > UINT64_MAX:
        raise OverflowError(f"{field_name} exceeds unsigned 64-bit integer storage")
    return microseconds


def _microseconds_to_timedelta(value: object, field_name: str) -> timedelta:
    """Decode a stored interval back into a :class:`~datetime.timedelta`."""
    if not isinstance(value, int) or isinstance(value, bool):
        raise TypeError(f"{field_name} must be stored as an int")
    if value <= 0:
        raise ValueError(f"{field_name} must be strictly positive")
    return timedelta(microseconds=value)


def _validate_storage_timestamp(value: object, field_name: str) -> datetime:
    if not isinstance(value, datetime):
        raise TypeError(f"{field_name} must be a datetime")
    if value.tzinfo is None:
        raise ValueError(f"{field_name} must be timezone-aware UTC")
    if value.utcoffset() != UTC.utcoffset(value):
        raise ValueError(f"{field_name} must be timezone-aware UTC")
    if value.tzinfo != UTC:
        raise ValueError(f"{field_name} must use UTC timezone exactly")
    return value.astimezone(UTC)


def _decimal_to_fixed_point(value: Decimal, field_name: str) -> int:
    """Encode a price as its fixed-point integer.

    Enforces the canonical envelope defensively rather than defining a second
    one. A value reaching here should already have satisfied
    :func:`~quant_research_terminal.domain.numeric.check_price` at construction,
    so a violation means a layer was bypassed. The value is scaled, never
    rounded — the envelope has already established the scaling discards nothing.

    Raises:
        NumericEnvelopeError: naming the specific violation. Magnitude and
            precision are distinguished, so an out-of-range value is no longer
            reported as having too many decimal places.
    """
    return price_to_fixed_point(validate_price(value, field_name))


def _decimal_to_unsigned_int(value: Decimal, field_name: str) -> int:
    """Encode a quantity as its unsigned integer.

    Enforces the canonical envelope defensively; see
    :func:`_decimal_to_fixed_point`.
    """
    return int(validate_quantity(value, field_name).to_integral_value())


def _fixed_point_to_decimal(value: object, field_name: str) -> Decimal:
    if not isinstance(value, int) or isinstance(value, bool):
        raise TypeError(f"{field_name} must be stored as an int")
    if value < 0:
        raise ValueError(f"{field_name} must be non-negative")
    if value > MAX_PRICE_FIXED_POINT:
        raise ValueError(f"{field_name} exceeds the representable price range")
    return fixed_point_to_price(value).quantize(PRICE_QUANTUM)


def _unsigned_int_to_decimal(value: object, field_name: str) -> Decimal:
    if not isinstance(value, int) or isinstance(value, bool):
        raise TypeError(f"{field_name} must be stored as an int")
    if value < 0:
        raise ValueError(f"{field_name} must be non-negative")
    if value > MAX_QUANTITY_INTEGER:
        raise ValueError(f"{field_name} exceeds unsigned 64-bit integer storage")
    return Decimal(value)


def validate_storage_schema(schema: pa.Schema) -> None:
    metadata = schema.metadata
    if metadata is None:
        raise ValueError("schema metadata is required")
    if metadata.get(b"schema_name") != SCHEMA_NAME.encode("utf-8"):
        raise ValueError("schema name is incompatible")
    if metadata.get(b"schema_version") != str(SCHEMA_VERSION).encode("utf-8"):
        raise ValueError("schema version is incompatible")
    if metadata.get(b"timestamp_timezone") != TIMESTAMP_TIMEZONE.encode("utf-8"):
        raise ValueError("schema timestamp timezone is incompatible")
    if metadata.get(b"price_encoding") != PRICE_ENCODING.encode("utf-8"):
        raise ValueError("schema price encoding is incompatible")
    if metadata.get(b"price_precision") != str(PRICE_PRECISION).encode("utf-8"):
        raise ValueError("schema price precision is incompatible")
    if metadata.get(b"price_scale") != str(PRICE_SCALE).encode("utf-8"):
        raise ValueError("schema price scale is incompatible")


def trade_to_storage_row(trade: Trade) -> TradeStorageRow:
    return {
        "timestamp": _validate_storage_timestamp(trade.timestamp, "timestamp"),
        "instrument_symbol": trade.instrument_symbol,
        "price": _decimal_to_fixed_point(trade.price, "price"),
        "size": _decimal_to_unsigned_int(trade.size, "size"),
        # Persist the enum's canonical value, never its repr.
        "side": trade.side.value,
    }


def trade_from_storage_row(row: Mapping[str, object], schema: pa.Schema | None = None) -> Trade:
    if schema is not None:
        validate_storage_schema(schema)
    return Trade(
        timestamp=_validate_storage_timestamp(row["timestamp"], "timestamp"),
        instrument_symbol=str(row["instrument_symbol"]),
        price=_fixed_point_to_decimal(row["price"], "price"),
        size=_unsigned_int_to_decimal(row["size"], "size"),
        # Trade validates the vocabulary; an unrecognised stored value is
        # rejected rather than silently becoming UNKNOWN.
        side=parse_trade_side(row["side"]),
    )


def quote_to_storage_row(quote: Quote) -> QuoteStorageRow:
    return {
        "timestamp": _validate_storage_timestamp(quote.timestamp, "timestamp"),
        "instrument_symbol": quote.instrument_symbol,
        "bid": _decimal_to_fixed_point(quote.bid, "bid"),
        "ask": _decimal_to_fixed_point(quote.ask, "ask"),
        "bid_size": _decimal_to_unsigned_int(quote.bid_size, "bid_size"),
        "ask_size": _decimal_to_unsigned_int(quote.ask_size, "ask_size"),
    }


def quote_from_storage_row(row: Mapping[str, object], schema: pa.Schema | None = None) -> Quote:
    if schema is not None:
        validate_storage_schema(schema)
    return Quote(
        timestamp=_validate_storage_timestamp(row["timestamp"], "timestamp"),
        instrument_symbol=str(row["instrument_symbol"]),
        bid=_fixed_point_to_decimal(row["bid"], "bid"),
        ask=_fixed_point_to_decimal(row["ask"], "ask"),
        bid_size=_unsigned_int_to_decimal(row["bid_size"], "bid_size"),
        ask_size=_unsigned_int_to_decimal(row["ask_size"], "ask_size"),
    )


def bar_to_storage_row(bar: Bar) -> BarStorageRow:
    """Encode a bar for persistence.

    ``timestamp`` is the bar's availability time. Interval start is not stored
    because it is exactly ``timestamp - interval``; persisting both would let
    the two disagree.
    """
    return {
        "timestamp": _validate_storage_timestamp(bar.availability_time, "timestamp"),
        "instrument_symbol": bar.instrument_symbol,
        "interval_microseconds": _timedelta_to_microseconds(bar.interval, "interval"),
        "open": _decimal_to_fixed_point(bar.open, "open"),
        "high": _decimal_to_fixed_point(bar.high, "high"),
        "low": _decimal_to_fixed_point(bar.low, "low"),
        "close": _decimal_to_fixed_point(bar.close, "close"),
        "volume": _decimal_to_unsigned_int(bar.volume, "volume"),
    }


def bar_from_storage_row(row: Mapping[str, object], schema: pa.Schema | None = None) -> Bar:
    """Decode a stored bar, recovering interval start from availability time."""
    if schema is not None:
        validate_storage_schema(schema)
    availability_time = _validate_storage_timestamp(row["timestamp"], "timestamp")
    interval = _microseconds_to_timedelta(row["interval_microseconds"], "interval_microseconds")
    return Bar(
        interval_start=availability_time - interval,
        interval=interval,
        instrument_symbol=str(row["instrument_symbol"]),
        open=_fixed_point_to_decimal(row["open"], "open"),
        high=_fixed_point_to_decimal(row["high"], "high"),
        low=_fixed_point_to_decimal(row["low"], "low"),
        close=_fixed_point_to_decimal(row["close"], "close"),
        volume=_unsigned_int_to_decimal(row["volume"], "volume"),
    )
