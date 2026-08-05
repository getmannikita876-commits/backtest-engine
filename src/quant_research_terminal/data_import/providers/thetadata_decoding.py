"""ThetaData vendor field semantics.

Pure decoding: this module turns ThetaData's archived export encodings into the
provider-neutral values a :class:`RawRecord` carries. It performs no IO, so
every rule below is testable without a file, a network, or a vendor client.

Decoding is not validation. A value that cannot be decoded is passed through
unchanged rather than repaired, dropped, or defaulted, so the validation stage
reports it against a specific row. That is the contract every provider in this
package follows.

Assumed vendor encodings — **UNVERIFIED**
-----------------------------------------
Everything below is an **assumption**. None of it has been confirmed against an
official ThetaData schema or a real export; it was written from recollection of
the vendor's conventions. The project's ThetaData tests were written from this
module, so they verify internal consistency and say nothing about vendor
fidelity.

The assumption whose failure would be least visible is the price encoding: a
scaled-integer vendor format would be wrong by a constant factor rather than
malformed, so it would import cleanly and silently. See ``docs/data-import.md``
for the full assumption register, and
:mod:`~quant_research_terminal.data_import.providers.thetadata_inspection` for
the tool that settles it against a real file.

* Time is split across two columns: ``date`` as ``YYYYMMDD`` and ``ms_of_day``
  as whole milliseconds since **local midnight**.
* Those are **exchange-local** wall-clock values, not UTC. ThetaData publishes
  US Eastern times; this module never assumes that, and requires the caller to
  supply the zone.
* Prices and sizes are decimal text, not fixed-point integers.
* A price or size of ``0`` means "no value" rather than a real zero. It needs
  no special marker here: the domain requires strictly positive quantities, so
  a zero is rejected by the ordinary positivity rule with a diagnosable issue.

Deliberately not decoded
------------------------
ThetaData's archived trade schema carries **no documented aggressor side**, so
every trade decodes to :attr:`TradeSide.UNKNOWN` unless the caller supplies an
explicit code mapping. Inferring a direction from condition or exchange codes
would fabricate order flow the vendor never published.

Options data — the vendor's primary product — is out of scope: the domain
models no option contract, and pre-declaring one here would invent semantics
the rest of the system cannot represent.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime, timedelta, tzinfo
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from typing import Any, Final

from quant_research_terminal.data_import.contracts import ImportRecordType
from quant_research_terminal.data_import.record_fields import (
    INSTRUMENT_FIELD,
    INTERVAL_FIELD,
    TIMESTAMP_FIELD,
)
from quant_research_terminal.domain.models import TradeSide

MILLISECONDS_PER_DAY: Final = 24 * 60 * 60 * 1_000

#: Emitted when a local wall-clock time occurs twice because the zone stepped
#: backwards. The archived row carries no indicator of which occurrence it is,
#: so the instant is genuinely unrecoverable and must not be guessed.
AMBIGUOUS_LOCAL_TIME_MARKER: Final = "AMBIGUOUS_LOCAL_TIME"

#: Emitted when a local wall-clock time never occurred because the zone stepped
#: forwards over it. Such a value cannot describe a real instant.
NONEXISTENT_LOCAL_TIME_MARKER: Final = "NONEXISTENT_LOCAL_TIME"


class ThetaDataSchema(StrEnum):
    """The ThetaData archived schemas this provider can decode."""

    TRADE = "trade"
    QUOTE = "quote"
    OHLC = "ohlc"


#: Which import record type each schema produces.
SCHEMA_RECORD_TYPES: Final[Mapping[ThetaDataSchema, ImportRecordType]] = {
    ThetaDataSchema.TRADE: ImportRecordType.TRADE,
    ThetaDataSchema.QUOTE: ImportRecordType.QUOTE,
    ThetaDataSchema.OHLC: ImportRecordType.BAR,
}


class BarTimestampMeaning(StrEnum):
    """What an OHLC row's timestamp refers to.

    ThetaData's convention for this is **not** documented to a standard this
    project is willing to assume, and getting it wrong shifts every bar by one
    interval — silently, and in the direction that creates look-ahead if the
    timestamp is really the interval end but is read as the start.

    The caller must therefore declare it. There is no default, because a wrong
    default would be invisible.
    """

    INTERVAL_START = "interval_start"
    INTERVAL_END = "interval_end"


# Vendor column names.
DATE_COLUMN: Final = "date"
MS_OF_DAY_COLUMN: Final = "ms_of_day"
SYMBOL_COLUMN: Final = "symbol"
PRICE_COLUMN: Final = "price"
SIZE_COLUMN: Final = "size"
SIDE_COLUMN: Final = "side"
BID_COLUMN: Final = "bid"
ASK_COLUMN: Final = "ask"
BID_SIZE_COLUMN: Final = "bid_size"
ASK_SIZE_COLUMN: Final = "ask_size"
OHLC_COLUMNS: Final[tuple[str, ...]] = ("open", "high", "low", "close", "volume")

#: Columns every schema needs in order to place a record in time.
COMMON_REQUIRED_COLUMNS: Final[tuple[str, ...]] = (DATE_COLUMN, MS_OF_DAY_COLUMN)

#: Columns each schema needs beyond the common ones.
SCHEMA_REQUIRED_COLUMNS: Final[Mapping[ThetaDataSchema, tuple[str, ...]]] = {
    ThetaDataSchema.TRADE: (PRICE_COLUMN, SIZE_COLUMN),
    ThetaDataSchema.QUOTE: (BID_COLUMN, ASK_COLUMN, BID_SIZE_COLUMN, ASK_SIZE_COLUMN),
    ThetaDataSchema.OHLC: OHLC_COLUMNS,
}

#: Columns that may be present and are used when they are.
OPTIONAL_COLUMNS: Final[tuple[str, ...]] = (SYMBOL_COLUMN, SIDE_COLUMN)


def _as_int(value: object) -> int | None:
    """Return ``value`` as an int, or ``None`` when it is not an exact integer."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        try:
            return int(value.strip())
        except ValueError:
            return None
    return None


def _local_naive_datetime(date_value: object, ms_of_day: object) -> datetime | None:
    """Combine a ``YYYYMMDD`` date and a millisecond offset into naive local time."""
    date_number = _as_int(date_value)
    milliseconds = _as_int(ms_of_day)
    if date_number is None or milliseconds is None:
        return None
    if not 0 <= milliseconds < MILLISECONDS_PER_DAY:
        # A value outside the day would silently roll the calendar date.
        return None

    year, remainder = divmod(date_number, 10_000)
    month, day = divmod(remainder, 100)
    try:
        midnight = datetime(year, month, day)
    except ValueError:
        return None
    return midnight + timedelta(milliseconds=milliseconds)


def local_to_utc(naive_local: datetime, session_timezone: tzinfo) -> datetime | str:
    """Convert an exchange-local wall-clock time to UTC.

    This is a *decode*, not a normalization: the vendor encoding is defined as
    local wall-clock, so producing the instant it denotes is the only way to
    read it at all. What the function refuses to do is guess.

    Two local times cannot be resolved and are rejected rather than resolved
    arbitrarily:

    * **Ambiguous** — the zone stepped backwards, so the wall-clock reading
      occurs twice and the archived row carries no indicator of which. Picking
      one would place the record up to an hour from where it belongs.
    * **Nonexistent** — the zone stepped forwards over the reading, so it never
      occurred. Snapping it to a neighbouring instant would invent a time.

    Returns:
        The UTC instant, or a marker string naming why it could not be
        resolved. The marker reaches validation, which rejects the row.
    """
    first = naive_local.replace(tzinfo=session_timezone, fold=0)
    second = naive_local.replace(tzinfo=session_timezone, fold=1)
    if first.utcoffset() != second.utcoffset():
        return AMBIGUOUS_LOCAL_TIME_MARKER

    instant = first.astimezone(UTC)
    if instant.astimezone(session_timezone).replace(tzinfo=None) != naive_local:
        return NONEXISTENT_LOCAL_TIME_MARKER
    return instant


def decode_session_timestamp(
    date_value: object, ms_of_day: object, session_timezone: tzinfo
) -> Any:
    """Decode a ThetaData ``date`` / ``ms_of_day`` pair into a UTC instant.

    A pair that cannot be parsed is returned unchanged as the original date
    value, so validation reports the row rather than the file losing it.
    """
    naive_local = _local_naive_datetime(date_value, ms_of_day)
    if naive_local is None:
        return date_value
    return local_to_utc(naive_local, session_timezone)


def decode_decimal(value: object) -> Any:
    """Decode decimal text into an exact :class:`Decimal`.

    Parsing goes straight from text to :class:`Decimal`, so a tick value never
    passes through binary floating point and is never rounded on the way in.
    """
    if isinstance(value, Decimal):
        return value
    if isinstance(value, str):
        try:
            return Decimal(value.strip())
        except InvalidOperation:
            return value
    return value


def decode_trade_side(value: object, side_by_vendor_code: Mapping[str, TradeSide] | None) -> Any:
    """Decode a trade's aggressor side.

    ThetaData's archived trade schema publishes no documented aggressor field.
    Without a caller-supplied mapping every trade is therefore
    :attr:`TradeSide.UNKNOWN` — an accurate record of what the vendor supplied,
    not a guess.

    When a mapping *is* supplied, a code outside it is returned unchanged so
    validation rejects it. It is deliberately not folded into ``UNKNOWN``,
    which would erase the difference between "the source does not publish a
    side" and "we did not understand the code it published".
    """
    if side_by_vendor_code is None:
        return TradeSide.UNKNOWN
    if isinstance(value, str):
        return side_by_vendor_code.get(value.strip().upper(), value)
    return value


def bar_interval_start(timestamp: object, interval: timedelta, meaning: BarTimestampMeaning) -> Any:
    """Return the start of the interval an OHLC row describes.

    A value that did not decode to a datetime is returned unchanged so the
    defect reaches validation rather than having arithmetic applied to it.
    """
    if not isinstance(timestamp, datetime):
        return timestamp
    if meaning is BarTimestampMeaning.INTERVAL_START:
        return timestamp
    return timestamp - interval


def bar_availability_time(interval_start: object, interval: timedelta) -> Any:
    """Return when an OHLC row's completed values first become available.

    A bar's open, high, low, close, and volume are not all determined until its
    period ends, so the completed bar cannot be known before
    ``interval_start + interval``. Publishing it at its start would expose the
    closing price before the period elapsed. See
    ``docs/adr/ADR-002-bar-availability-time.md``.
    """
    if not isinstance(interval_start, datetime):
        return interval_start
    return interval_start + interval


def decode_trade_fields(
    row: Mapping[str, object],
    *,
    symbol: str,
    session_timezone: tzinfo,
    side_by_vendor_code: Mapping[str, TradeSide] | None,
) -> dict[str, Any]:
    """Map a ThetaData trade row onto the import trade fields."""
    return {
        TIMESTAMP_FIELD: decode_session_timestamp(
            row.get(DATE_COLUMN), row.get(MS_OF_DAY_COLUMN), session_timezone
        ),
        INSTRUMENT_FIELD: symbol,
        "price": decode_decimal(row.get(PRICE_COLUMN)),
        "size": decode_decimal(row.get(SIZE_COLUMN)),
        "side": decode_trade_side(row.get(SIDE_COLUMN), side_by_vendor_code),
    }


def decode_quote_fields(
    row: Mapping[str, object], *, symbol: str, session_timezone: tzinfo
) -> dict[str, Any]:
    """Map a ThetaData quote row onto the import quote fields."""
    return {
        TIMESTAMP_FIELD: decode_session_timestamp(
            row.get(DATE_COLUMN), row.get(MS_OF_DAY_COLUMN), session_timezone
        ),
        INSTRUMENT_FIELD: symbol,
        "bid": decode_decimal(row.get(BID_COLUMN)),
        "ask": decode_decimal(row.get(ASK_COLUMN)),
        "bid_size": decode_decimal(row.get(BID_SIZE_COLUMN)),
        "ask_size": decode_decimal(row.get(ASK_SIZE_COLUMN)),
    }


def decode_bar_fields(
    row: Mapping[str, object],
    *,
    symbol: str,
    session_timezone: tzinfo,
    interval: timedelta,
    meaning: BarTimestampMeaning,
) -> dict[str, Any]:
    """Map a ThetaData OHLC row onto the import bar fields.

    The emitted timestamp is the interval's **close**, so a completed bar can
    never be observed at the instant its interval opened. The interval travels
    with the record, so normalization can recover the interval start exactly.
    """
    vendor_timestamp = decode_session_timestamp(
        row.get(DATE_COLUMN), row.get(MS_OF_DAY_COLUMN), session_timezone
    )
    interval_start = bar_interval_start(vendor_timestamp, interval, meaning)

    fields: dict[str, Any] = {
        TIMESTAMP_FIELD: bar_availability_time(interval_start, interval),
        INSTRUMENT_FIELD: symbol,
        INTERVAL_FIELD: interval,
    }
    for column in OHLC_COLUMNS:
        fields[column] = decode_decimal(row.get(column))
    return fields
