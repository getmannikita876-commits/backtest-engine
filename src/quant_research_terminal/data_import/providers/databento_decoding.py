"""Databento vendor field semantics.

Pure decoding: this module turns Databento's documented field encodings into
the provider-neutral values a :class:`RawRecord` carries. It performs no IO, so
every rule below is testable without a file, a network, or a vendor SDK.

Decoding is not validation. A value that cannot be decoded is passed through
unchanged rather than repaired, dropped, or defaulted, so the validation stage
reports it against a specific row. That is the same contract every provider in
this package follows.

Documented vendor encodings
---------------------------
These are the DBN field semantics the mapping relies on. They are stated here
explicitly because the project forbids inventing vendor behaviour silently, and
because they have **not** been verified against live Databento output in this
phase — see ``docs/data-import.md``.

* Timestamps (``ts_recv``, ``ts_event``) are int64 **nanoseconds** since the
  UNIX epoch, always UTC.
* Prices are int64 **fixed-point with a 1e-9 scale**. ``5000.25`` is carried as
  ``5000250000000``.
* ``UNDEF_PRICE`` is ``INT64_MAX`` and means *no price*, not a large price.
* ``instrument_id`` is a uint32 vendor-local identifier, not a symbol.
* ``side`` is a single character: ``A`` (ask — a sell aggressor), ``B`` (bid —
  a buy aggressor), or ``N`` (none).
* ``size``/``volume`` are unsigned integers, counts of contracts.

Precision
---------
Python's :class:`~datetime.datetime` resolves to microseconds, and the storage
contract persists ``timestamp[us, tz=UTC]``. A Databento timestamp carrying
sub-microsecond detail therefore has no exact representation anywhere in this
system. Rather than truncate silently — which would be a silent mutation of
immutable market data — the caller must choose a
:class:`SubMicrosecondPolicy`.

Prices convert exactly. ``Decimal(raw).scaleb(-9)`` is an exact decimal
operation with no float involved, so a tick value survives the conversion
bit-for-bit.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from enum import StrEnum
from typing import Any, Final

from quant_research_terminal.data_import.contracts import ImportRecordType
from quant_research_terminal.data_import.record_fields import (
    INSTRUMENT_FIELD,
    INTERVAL_FIELD,
    TIMESTAMP_FIELD,
)
from quant_research_terminal.domain.models import TradeSide

#: Common Databento OHLCV interval durations, for callers that prefer a name
#: over a literal. The provider accepts any strictly positive interval.
ONE_SECOND: Final = timedelta(seconds=1)
ONE_MINUTE: Final = timedelta(minutes=1)
ONE_HOUR: Final = timedelta(hours=1)
ONE_DAY: Final = timedelta(days=1)

#: Decimal exponent of Databento's fixed-point price encoding.
DATABENTO_PRICE_SCALE: Final = 9

#: Sentinel meaning "no price". Never a quantity.
UNDEF_PRICE: Final = 2**63 - 1

#: Sentinel meaning "no timestamp".
UNDEF_TIMESTAMP: Final = 2**64 - 1

#: Emitted in place of a sentinel so validation rejects the row with a
#: diagnosable issue instead of importing the sentinel as a real number.
UNDEF_MARKER: Final = "UNDEF"

NANOSECONDS_PER_MICROSECOND: Final = 1_000

_EPOCH: Final = datetime(1970, 1, 1, tzinfo=UTC)


class DatabentoSchema(StrEnum):
    """The Databento schemas this provider can decode."""

    TRADES = "trades"
    MBP_1 = "mbp-1"
    OHLCV = "ohlcv"


#: Which import record type each schema produces.
SCHEMA_RECORD_TYPES: Final[Mapping[DatabentoSchema, ImportRecordType]] = {
    DatabentoSchema.TRADES: ImportRecordType.TRADE,
    DatabentoSchema.MBP_1: ImportRecordType.QUOTE,
    DatabentoSchema.OHLCV: ImportRecordType.BAR,
}


class DatabentoTimestampField(StrEnum):
    """Which vendor timestamp becomes the record's timestamp.

    This choice decides whether the import can leak future information.

    ``TS_RECV`` is the capture server's receive time — when the data became
    *observable*. ``TS_EVENT`` is the matching engine's event time, which is
    always earlier. Stamping a record with ``ts_event`` and replaying it at that
    instant lets a strategy act on information before it could have arrived,
    which is look-ahead bias and a critical defect under this project's rules.

    ``TS_RECV`` is therefore the default wherever the schema provides it.
    ``TS_EVENT`` remains available for research that deliberately studies
    exchange sequencing rather than tradable behaviour.
    """

    TS_RECV = "ts_recv"
    TS_EVENT = "ts_event"


#: Default timestamp field per schema.
#:
#: The OHLCV schema carries no ``ts_recv`` column at all, so bars can only use
#: ``ts_event``. See ``docs/data-import.md`` for the look-ahead consequence.
DEFAULT_TIMESTAMP_FIELDS: Final[Mapping[DatabentoSchema, DatabentoTimestampField]] = {
    DatabentoSchema.TRADES: DatabentoTimestampField.TS_RECV,
    DatabentoSchema.MBP_1: DatabentoTimestampField.TS_RECV,
    DatabentoSchema.OHLCV: DatabentoTimestampField.TS_EVENT,
}


class SubMicrosecondPolicy(StrEnum):
    """What to do with a timestamp carrying sub-microsecond detail.

    ``REJECT`` passes the raw nanosecond text through undecoded, so the
    timestamp validator reports ``non_datetime_timestamp`` and the row is
    rejected with its original value intact. Nothing is silently lost.

    ``TRUNCATE`` discards the sub-microsecond remainder, flooring toward the
    past. This is lossy and irreversible, which is why it must be chosen
    explicitly. Most real Databento data carries nanosecond detail, so an
    operator importing live-captured data will normally need it — but they opt
    in knowing exactly what is discarded.
    """

    REJECT = "reject"
    TRUNCATE = "truncate"


#: Databento's aggressor-side encoding.
#:
#: ``A`` (ask) marks a sell aggressor and ``B`` (bid) a buy aggressor.
#: ``N`` maps to :attr:`TradeSide.UNKNOWN` because that is exactly what the
#: vendor is asserting — it could not attribute the aggressor. Recording that
#: is not the same as inferring a direction, and nothing downstream promotes
#: ``UNKNOWN`` to ``BUY`` or ``SELL``. Codes outside this table are passed
#: through unchanged so validation rejects them, keeping "the vendor said it
#: does not know" distinct from "we did not understand the vendor".
TRADE_SIDE_BY_VENDOR_CODE: Final[Mapping[str, TradeSide]] = {
    "A": TradeSide.SELL,
    "B": TradeSide.BUY,
    "N": TradeSide.UNKNOWN,
}

#: The sides that identify an actual aggressor.
#:
#: Prefer :attr:`TradeSide.is_directional` on a validated domain object; this
#: set is for code inspecting raw records before normalization.
KNOWN_TRADE_SIDES: Final[frozenset[TradeSide]] = frozenset({TradeSide.BUY, TradeSide.SELL})

# Vendor column names.
INSTRUMENT_ID_COLUMN: Final = "instrument_id"
SYMBOL_COLUMN: Final = "symbol"
PRICE_COLUMN: Final = "price"
SIZE_COLUMN: Final = "size"
SIDE_COLUMN: Final = "side"
BID_PRICE_COLUMN: Final = "bid_px_00"
ASK_PRICE_COLUMN: Final = "ask_px_00"
BID_SIZE_COLUMN: Final = "bid_sz_00"
ASK_SIZE_COLUMN: Final = "ask_sz_00"
OHLCV_COLUMNS: Final[tuple[str, ...]] = ("open", "high", "low", "close", "volume")


def _as_int(text: object) -> int | None:
    """Return ``text`` as an int, or ``None`` when it is not an exact integer."""
    if isinstance(text, bool):
        return None
    if isinstance(text, int):
        return text
    if isinstance(text, str):
        try:
            return int(text.strip())
        except ValueError:
            return None
    return None


def decode_nanosecond_timestamp(value: object, policy: SubMicrosecondPolicy) -> Any:
    """Decode a Databento nanosecond timestamp.

    Returns a timezone-aware UTC :class:`~datetime.datetime` when the value is
    exactly representable, and otherwise returns the input unchanged so the
    validation stage can report it. No timezone is ever attached, shifted, or
    inferred: the vendor encoding is defined as UTC, so the decoded instant is
    UTC by construction rather than by conversion.
    """
    nanoseconds = _as_int(value)
    if nanoseconds is None:
        return value
    if nanoseconds == UNDEF_TIMESTAMP:
        return UNDEF_MARKER
    if nanoseconds < 0:
        return value

    remainder = nanoseconds % NANOSECONDS_PER_MICROSECOND
    if remainder and policy is SubMicrosecondPolicy.REJECT:
        # Carry the exact vendor value through; validation rejects the row
        # rather than this function quietly discarding the remainder.
        return value

    microseconds = nanoseconds // NANOSECONDS_PER_MICROSECOND
    try:
        return _EPOCH + timedelta(microseconds=microseconds)
    except OverflowError:
        return value


def decode_fixed_point_price(value: object) -> Any:
    """Decode a Databento 1e-9 fixed-point price into an exact :class:`Decimal`.

    The sentinel ``UNDEF_PRICE`` becomes :data:`UNDEF_MARKER` — a string, so it
    can never be mistaken for a very large price by a numeric validator. Any
    value that is not an integer is returned unchanged for validation to report.
    """
    raw = _as_int(value)
    if raw is None:
        return value
    if raw == UNDEF_PRICE:
        return UNDEF_MARKER
    return Decimal(raw).scaleb(-DATABENTO_PRICE_SCALE)


def decode_quantity(value: object) -> Any:
    """Decode an unsigned integer size or volume into an exact :class:`Decimal`."""
    raw = _as_int(value)
    if raw is None:
        return value
    return Decimal(raw)


def decode_trade_side(value: object) -> Any:
    """Decode Databento's aggressor-side character into a :class:`TradeSide`.

    ``A`` becomes ``SELL``, ``B`` becomes ``BUY``, and ``N`` becomes
    ``UNKNOWN`` — the vendor stating it could not attribute the trade, which
    the domain can now record faithfully.

    An unrecognised code is returned unchanged so validation reports it. It is
    deliberately *not* mapped to ``UNKNOWN``, which would erase the difference
    between a vendor's explicit "no side" and a decoding failure.
    """
    if isinstance(value, str):
        return TRADE_SIDE_BY_VENDOR_CODE.get(value.strip().upper(), value)
    return value


def decode_trade_fields(
    row: Mapping[str, object],
    *,
    symbol: str,
    timestamp_field: DatabentoTimestampField,
    policy: SubMicrosecondPolicy,
) -> dict[str, Any]:
    """Map a Databento ``trades`` row onto the import trade fields."""
    return {
        TIMESTAMP_FIELD: decode_nanosecond_timestamp(row.get(timestamp_field.value), policy),
        INSTRUMENT_FIELD: symbol,
        "price": decode_fixed_point_price(row.get(PRICE_COLUMN)),
        "size": decode_quantity(row.get(SIZE_COLUMN)),
        "side": decode_trade_side(row.get(SIDE_COLUMN)),
    }


def decode_quote_fields(
    row: Mapping[str, object],
    *,
    symbol: str,
    timestamp_field: DatabentoTimestampField,
    policy: SubMicrosecondPolicy,
) -> dict[str, Any]:
    """Map a Databento ``mbp-1`` row onto the import quote fields.

    Only the top-of-book columns are used. An ``mbp-1`` row also carries
    ``price``, ``size``, and ``side`` describing the *event* that changed the
    book; those describe a trade or order action, not the resulting quote, and
    folding them into a quote would misrepresent the book.
    """
    return {
        TIMESTAMP_FIELD: decode_nanosecond_timestamp(row.get(timestamp_field.value), policy),
        INSTRUMENT_FIELD: symbol,
        "bid": decode_fixed_point_price(row.get(BID_PRICE_COLUMN)),
        "ask": decode_fixed_point_price(row.get(ASK_PRICE_COLUMN)),
        "bid_size": decode_quantity(row.get(BID_SIZE_COLUMN)),
        "ask_size": decode_quantity(row.get(ASK_SIZE_COLUMN)),
    }


def bar_availability_timestamp(interval_start: object, interval: timedelta) -> Any:
    """Return when a bar's completed values first become available.

    A Databento ``ohlcv`` record is stamped at the **start** of its interval,
    but its open, high, low, close, and volume are not determined until the
    interval **ends**. Publishing the completed bar at its start timestamp
    would expose the closing price before the period was over, which is
    look-ahead bias and a critical defect under this project's rules.

    ``Bar.timestamp`` is the coordinate the replay ordering key uses to decide
    when an event enters the stream, so for a bar it must carry the
    information-availability time. Mapping interval start onto it is therefore
    a documented decode of a vendor encoding — the same kind of translation as
    mapping ``B`` onto ``buy`` — not a mutation of the vendor's value.

    Nothing is lost. The transformation is exact and invertible: the original
    vendor timestamp is always ``timestamp - interval``, and the interval is
    explicit provider configuration rather than an inferred guess.

    A value that did not decode to a datetime is returned unchanged, so a
    defective timestamp still reaches validation intact.
    """
    if isinstance(interval_start, datetime):
        return interval_start + interval
    return interval_start


def decode_bar_fields(
    row: Mapping[str, object],
    *,
    symbol: str,
    timestamp_field: DatabentoTimestampField,
    policy: SubMicrosecondPolicy,
    interval: timedelta,
) -> dict[str, Any]:
    """Map a Databento ``ohlcv`` row onto the import bar fields.

    The emitted timestamp is the interval's **close**, computed by
    :func:`bar_availability_timestamp`, so a completed bar can never be
    observed at the instant its interval opened.

    ``volume`` is an unsigned count; the four price columns use the same
    fixed-point encoding as every other Databento price.
    """
    interval_start = decode_nanosecond_timestamp(row.get(timestamp_field.value), policy)
    fields: dict[str, Any] = {
        TIMESTAMP_FIELD: bar_availability_timestamp(interval_start, interval),
        INSTRUMENT_FIELD: symbol,
        INTERVAL_FIELD: interval,
    }
    for column in OHLCV_COLUMNS:
        if column == "volume":
            fields[column] = decode_quantity(row.get(column))
        else:
            fields[column] = decode_fixed_point_price(row.get(column))
    return fields
