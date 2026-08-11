from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Final, TypedDict

import pyarrow as pa  # type: ignore[import-untyped]

from quant_research_terminal.data.contracts import (
    CANONICAL_IDENTITY_METADATA_KEY,
    LEGACY_SCHEMA_VERSION,
    PRICE_ENCODING,
    PRICE_PRECISION,
    PRICE_QUANTUM,
    PRICE_SCALE,
    RECORD_TYPE_METADATA_KEY,
    SCHEMA_NAME,
    SCHEMA_VERSION,
    TIMESTAMP_TIMEZONE,
    UINT64_MAX,
)
from quant_research_terminal.domain.dataset_identity import RecordType
from quant_research_terminal.domain.futures_contract import (
    FuturesContractId,
    require_listed_contract,
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


class TradeStorageRowV3(TypedDict):
    timestamp: datetime
    canonical_identity: str
    vendor_symbol: str
    price: int
    size: int
    side: str


class QuoteStorageRowV3(TypedDict):
    timestamp: datetime
    canonical_identity: str
    vendor_symbol: str
    bid: int
    ask: int
    bid_size: int
    ask_size: int


class BarStorageRowV3(TypedDict):
    timestamp: datetime
    canonical_identity: str
    vendor_symbol: str
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


def validate_storage_schema(
    schema: pa.Schema, *, expected_version: int = LEGACY_SCHEMA_VERSION
) -> None:
    """Check the six shared metadata keys against one schema version.

    ``expected_version`` defaults to the **legacy** version rather than to
    ``SCHEMA_VERSION``. That is deliberate: defaulting to "whatever the current
    version happens to be" is exactly how a version-2 file would one day be
    validated as version 3 the moment someone bumps the constant. Callers that
    mean version 3 say so.

    Unknown extra keys are tolerated here, as they always have been; version-3
    artifacts get an exact key-set check in :func:`validate_storage_schema_v3`
    instead, because files already exist under the lenient version-2 rule and
    tightening it retroactively would reject them.
    """
    metadata = schema.metadata
    if metadata is None:
        raise ValueError("schema metadata is required")
    if metadata.get(b"schema_name") != SCHEMA_NAME.encode("utf-8"):
        raise ValueError("schema name is incompatible")
    if metadata.get(b"schema_version") != str(expected_version).encode("utf-8"):
        raise ValueError("schema version is incompatible")
    if metadata.get(b"timestamp_timezone") != TIMESTAMP_TIMEZONE.encode("utf-8"):
        raise ValueError("schema timestamp timezone is incompatible")
    if metadata.get(b"price_encoding") != PRICE_ENCODING.encode("utf-8"):
        raise ValueError("schema price encoding is incompatible")
    if metadata.get(b"price_precision") != str(PRICE_PRECISION).encode("utf-8"):
        raise ValueError("schema price precision is incompatible")
    if metadata.get(b"price_scale") != str(PRICE_SCALE).encode("utf-8"):
        raise ValueError("schema price scale is incompatible")


#: The complete metadata key set a version-3 schema carries.
_V3_METADATA_KEYS: Final[frozenset[bytes]] = frozenset(
    {
        b"schema_name",
        b"schema_version",
        b"timestamp_timezone",
        b"price_encoding",
        b"price_precision",
        b"price_scale",
        RECORD_TYPE_METADATA_KEY,
        CANONICAL_IDENTITY_METADATA_KEY,
    }
)


def validate_storage_schema_v3(schema: pa.Schema) -> tuple[RecordType, FuturesContractId]:
    """Validate a version-3 schema and return what it says it holds.

    Metadata-only: no row is touched, so a file naming the wrong contract costs
    one footer read rather than a full scan.

    The canonical identity must **round-trip** — ``parse(s).canonical() == s`` —
    not merely parse. Without that, a metadata value of ``"cme:es:m2026"`` whose
    every row matches would pass a naive equality check while being a string
    the platform's own parser rejects: a catalogue key written today that
    cannot be read back tomorrow, which is precisely the defect ADR-009's
    re-validating ``model_copy`` exists to prevent.

    Raises:
        ValueError: for any metadata violation, described specifically.
    """
    metadata = schema.metadata
    if metadata is None:
        raise ValueError("schema metadata is required")

    actual_keys = frozenset(metadata)
    if actual_keys != _V3_METADATA_KEYS:
        missing = sorted(key.decode("ascii", "replace") for key in _V3_METADATA_KEYS - actual_keys)
        unexpected = sorted(
            key.decode("ascii", "replace") for key in actual_keys - _V3_METADATA_KEYS
        )
        raise ValueError(
            f"a version-3 schema carries exactly the documented metadata keys; "
            f"missing {missing}, unexpected {unexpected}"
        )

    validate_storage_schema(schema, expected_version=SCHEMA_VERSION)

    raw_type = metadata[RECORD_TYPE_METADATA_KEY]
    try:
        record_type = RecordType(raw_type.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as error:
        raise ValueError(f"schema record type {raw_type!r} is not a known record type") from error

    raw_identity = metadata[CANONICAL_IDENTITY_METADATA_KEY]
    try:
        identity = raw_identity.decode("utf-8")
    except UnicodeDecodeError as error:
        raise ValueError("schema canonical identity is not valid UTF-8") from error
    contract = FuturesContractId.parse(identity)
    if contract.canonical() != identity:
        raise ValueError(
            f"schema canonical identity {identity!r} is not in canonical form; it would "
            f"serialize as {contract.canonical()!r}"
        )
    require_listed_contract(contract)
    return record_type, contract


#: ``vendor_symbol`` is the record's own ``instrument_symbol`` — the alias the
#: source used — preserved verbatim. It is provenance and never determines the
#: canonical identity, which the caller supplies once for the whole dataset.
def trade_to_storage_row_v3(trade: Trade, contract: FuturesContractId) -> TradeStorageRowV3:
    row = trade_to_storage_row(trade)
    return {
        "timestamp": row["timestamp"],
        "canonical_identity": contract.canonical(),
        "vendor_symbol": trade.instrument_symbol,
        "price": row["price"],
        "size": row["size"],
        "side": row["side"],
    }


def quote_to_storage_row_v3(quote: Quote, contract: FuturesContractId) -> QuoteStorageRowV3:
    row = quote_to_storage_row(quote)
    return {
        "timestamp": row["timestamp"],
        "canonical_identity": contract.canonical(),
        "vendor_symbol": quote.instrument_symbol,
        "bid": row["bid"],
        "ask": row["ask"],
        "bid_size": row["bid_size"],
        "ask_size": row["ask_size"],
    }


def bar_to_storage_row_v3(bar: Bar, contract: FuturesContractId) -> BarStorageRowV3:
    row = bar_to_storage_row(bar)
    return {
        "timestamp": row["timestamp"],
        "canonical_identity": contract.canonical(),
        "vendor_symbol": bar.instrument_symbol,
        "interval_microseconds": row["interval_microseconds"],
        "open": row["open"],
        "high": row["high"],
        "low": row["low"],
        "close": row["close"],
        "volume": row["volume"],
    }


def _as_v2_row(row: Mapping[str, object]) -> dict[str, object]:
    """Return a version-3 row shaped as the version-2 decoder expects.

    The vendor symbol becomes ``instrument_symbol``, which is exactly what it
    is: the alias the source used. The canonical identity is *not* placed on
    the record — domain records carry no canonical identity (ADR-009), and the
    dataset's contract is returned alongside the records instead.
    """
    converted = {key: value for key, value in row.items() if key != "canonical_identity"}
    converted["instrument_symbol"] = converted.pop("vendor_symbol")
    return converted


def trade_from_storage_row_v3(row: Mapping[str, object]) -> Trade:
    return trade_from_storage_row(_as_v2_row(row))


def quote_from_storage_row_v3(row: Mapping[str, object]) -> Quote:
    return quote_from_storage_row(_as_v2_row(row))


def bar_from_storage_row_v3(row: Mapping[str, object]) -> Bar:
    return bar_from_storage_row(_as_v2_row(row))


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
