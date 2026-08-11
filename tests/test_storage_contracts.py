from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal

import polars as pl
import pyarrow as pa  # type: ignore[import-untyped]
import pytest
from pydantic import ValidationError

from quant_research_terminal.data import (
    BAR_ARROW_SCHEMA,
    BAR_POLARS_SCHEMA,
    QUOTE_ARROW_SCHEMA,
    QUOTE_POLARS_SCHEMA,
    SCHEMA_NAME,
    TIMESTAMP_TIMEZONE,
    TRADE_ARROW_SCHEMA,
    TRADE_POLARS_SCHEMA,
    bar_from_storage_row,
    bar_to_storage_row,
    quote_from_storage_row,
    quote_to_storage_row,
    trade_from_storage_row,
    trade_to_storage_row,
    validate_storage_schema,
)
from quant_research_terminal.data.conversion import (
    _decimal_to_fixed_point,
    _decimal_to_unsigned_int,
)
from quant_research_terminal.domain.models import Bar, Quote, Trade, TradeSide


def test_trade_arrow_schema_fields_and_types() -> None:
    assert [field.name for field in TRADE_ARROW_SCHEMA] == [
        "timestamp",
        "instrument_symbol",
        "price",
        "size",
        "side",
    ]
    assert TRADE_ARROW_SCHEMA.field("timestamp").type == pa.timestamp("us", tz="UTC")
    assert TRADE_ARROW_SCHEMA.field("price").type == pa.int64()
    assert TRADE_ARROW_SCHEMA.field("size").type == pa.uint64()
    assert TRADE_ARROW_SCHEMA.metadata is not None
    assert TRADE_ARROW_SCHEMA.metadata[b"schema_name"] == SCHEMA_NAME.encode("utf-8")
    # Literal, not the constant: this pins that the version-2 schema stays
    # version 2 after the platform's current version moved on.
    assert TRADE_ARROW_SCHEMA.metadata[b"schema_version"] == b"2"
    assert TRADE_ARROW_SCHEMA.metadata[b"timestamp_timezone"] == TIMESTAMP_TIMEZONE.encode("utf-8")


def test_quote_arrow_schema_fields_and_types() -> None:
    assert [field.name for field in QUOTE_ARROW_SCHEMA] == [
        "timestamp",
        "instrument_symbol",
        "bid",
        "ask",
        "bid_size",
        "ask_size",
    ]
    assert QUOTE_ARROW_SCHEMA.field("bid").type == pa.int64()
    assert QUOTE_ARROW_SCHEMA.field("ask_size").type == pa.uint64()


def test_bar_arrow_schema_fields_and_types() -> None:
    assert [field.name for field in BAR_ARROW_SCHEMA] == [
        "timestamp",
        "instrument_symbol",
        "interval_microseconds",
        "open",
        "high",
        "low",
        "close",
        "volume",
    ]
    assert BAR_ARROW_SCHEMA.field("volume").type == pa.uint64()
    assert BAR_ARROW_SCHEMA.field("interval_microseconds").type == pa.uint64()


def test_trade_polars_schema_fields_and_types() -> None:
    assert list(dict(TRADE_POLARS_SCHEMA).keys()) == [
        "timestamp",
        "instrument_symbol",
        "price",
        "size",
        "side",
    ]
    expected_timestamp_dtype = pl.Datetime(time_unit="us", time_zone="UTC")
    dtype_map = dict(TRADE_POLARS_SCHEMA)
    assert dtype_map["timestamp"] == expected_timestamp_dtype
    assert dtype_map["price"] == pl.Int64
    assert dtype_map["size"] == pl.UInt64


def test_quote_polars_schema_fields_and_types() -> None:
    assert list(dict(QUOTE_POLARS_SCHEMA).keys()) == [
        "timestamp",
        "instrument_symbol",
        "bid",
        "ask",
        "bid_size",
        "ask_size",
    ]
    dtype_map = dict(QUOTE_POLARS_SCHEMA)
    assert dtype_map["bid"] == pl.Int64
    assert dtype_map["ask_size"] == pl.UInt64


def test_bar_polars_schema_fields_and_types() -> None:
    assert list(dict(BAR_POLARS_SCHEMA).keys()) == [
        "timestamp",
        "instrument_symbol",
        "interval_microseconds",
        "open",
        "high",
        "low",
        "close",
        "volume",
    ]
    dtype_map = dict(BAR_POLARS_SCHEMA)
    assert dtype_map["volume"] == pl.UInt64
    assert dtype_map["interval_microseconds"] == pl.UInt64


def test_domain_storage_domain_round_trip_trade() -> None:
    trade = Trade(
        instrument_symbol="ES",
        timestamp=datetime(2024, 1, 2, 12, 0, 0, 123456, tzinfo=UTC),
        price=Decimal("5000.250000"),
        size=Decimal("2"),
        side=TradeSide.BUY,
    )
    row = trade_to_storage_row(trade)
    round_trip = trade_from_storage_row(row, schema=TRADE_ARROW_SCHEMA)
    assert round_trip == trade


def test_domain_storage_domain_round_trip_quote() -> None:
    quote = Quote(
        instrument_symbol="ES",
        timestamp=datetime(2024, 1, 2, 12, 0, 0, 654321, tzinfo=UTC),
        bid=Decimal("5000.000000"),
        ask=Decimal("5000.250000"),
        bid_size=Decimal("1"),
        ask_size=Decimal("1"),
    )
    row = quote_to_storage_row(quote)
    round_trip = quote_from_storage_row(row, schema=QUOTE_ARROW_SCHEMA)
    assert round_trip == quote


def test_domain_storage_domain_round_trip_bar() -> None:
    bar = Bar(
        instrument_symbol="ES",
        interval_start=datetime(2024, 1, 2, 12, 0, 0, 500000, tzinfo=UTC),
        interval=timedelta(minutes=1),
        open=Decimal("4999.500000"),
        high=Decimal("5002.000000"),
        low=Decimal("4998.750000"),
        close=Decimal("5001.250000"),
        volume=Decimal("12345"),
    )
    row = bar_to_storage_row(bar)
    round_trip = bar_from_storage_row(row, schema=BAR_ARROW_SCHEMA)
    assert round_trip == bar


def test_naive_datetime_rejection_in_storage_row() -> None:
    row = {
        "timestamp": datetime(2024, 1, 2, 12, 0, 0),
        "instrument_symbol": "ES",
        "price": 5000250000,
        "size": 2,
        "side": "buy",
    }
    with pytest.raises(ValueError, match="timezone-aware UTC"):
        trade_from_storage_row(row)


def test_non_utc_datetime_rejection_in_storage_row() -> None:
    row = {
        "timestamp": datetime(
            2024,
            1,
            2,
            12,
            0,
            0,
            tzinfo=UTC,
        ).astimezone(timezone(timedelta(hours=1))),
        "instrument_symbol": "ES",
        "price": 5000250000,
        "size": 2,
        "side": "buy",
    }
    with pytest.raises(ValueError, match="UTC"):
        trade_from_storage_row(row)


def test_invalid_price_precision_rejection() -> None:
    # The envelope is enforced at construction, so an unencodable price never
    # becomes a domain object in the first place.
    with pytest.raises(ValidationError, match="decimal places"):
        Quote(
            instrument_symbol="ES",
            timestamp=datetime(2024, 1, 2, 12, 0, 0, tzinfo=UTC),
            bid=Decimal("5000.0000001"),
            ask=Decimal("5000.250000"),
            bid_size=Decimal("1"),
            ask_size=Decimal("1"),
        )

    # Storage still enforces it defensively if called directly.
    with pytest.raises(ValueError, match="decimal places"):
        _decimal_to_fixed_point(Decimal("5000.0000001"), "bid")


def test_reject_price_requiring_rounding() -> None:
    with pytest.raises(ValidationError, match="decimal places"):
        Trade(
            instrument_symbol="ES",
            timestamp=datetime(2024, 1, 2, 12, 0, 0, tzinfo=UTC),
            price=Decimal("5000.0000001"),
            size=Decimal("1"),
            side=TradeSide.BUY,
        )


def test_trailing_zeros_beyond_the_scale_are_accepted() -> None:
    # Regression: the previous rule trapped Rounded, which fires on trailing
    # zeros that carry no information. That rejected every Databento-decoded
    # price, which arrives at scale 9.
    trade = Trade(
        instrument_symbol="ES",
        timestamp=datetime(2024, 1, 2, 12, 0, 0, tzinfo=UTC),
        price=Decimal("5000.250000000"),
        size=Decimal("1"),
        side=TradeSide.BUY,
    )

    assert trade_to_storage_row(trade)["price"] == 5_000_250_000


def test_reject_price_nan_or_infinity() -> None:
    for value in ("NaN", "sNaN", "Infinity", "-Infinity"):
        with pytest.raises(ValueError, match="finite"):
            _decimal_to_fixed_point(Decimal(value), "price")


def test_reject_non_integer_size() -> None:
    with pytest.raises(ValidationError, match="whole number"):
        Trade(
            instrument_symbol="ES",
            timestamp=datetime(2024, 1, 2, 12, 0, 0, tzinfo=UTC),
            price=Decimal("5000.000000"),
            size=Decimal("1.1"),
            side=TradeSide.BUY,
        )

    with pytest.raises(ValueError, match="whole number"):
        _decimal_to_unsigned_int(Decimal("1.1"), "size")


def test_price_fixed_point_overflow_rejection() -> None:
    # Magnitude is reported as magnitude, not as a precision problem.
    with pytest.raises(ValidationError, match="representable range"):
        Trade(
            instrument_symbol="ES",
            timestamp=datetime(2024, 1, 2, 12, 0, 0, tzinfo=UTC),
            price=Decimal("1000000000000.000000"),
            size=Decimal("1"),
            side=TradeSide.BUY,
        )

    with pytest.raises(ValueError, match="representable range"):
        _decimal_to_fixed_point(Decimal("1000000000000.000000"), "price")


def test_size_uint64_overflow_rejection() -> None:
    with pytest.raises(ValidationError, match="representable range"):
        Trade(
            instrument_symbol="ES",
            timestamp=datetime(2024, 1, 2, 12, 0, 0, tzinfo=UTC),
            price=Decimal("5000.000000"),
            size=Decimal(str(2**64)),
            side=TradeSide.BUY,
        )

    with pytest.raises(ValueError, match="representable range"):
        _decimal_to_unsigned_int(Decimal(str(2**64)), "size")


def test_schema_version_rejection() -> None:
    bad_schema = TRADE_ARROW_SCHEMA.with_metadata(
        {
            **(TRADE_ARROW_SCHEMA.metadata or {}),
            b"schema_version": b"999",
        }
    )
    with pytest.raises(ValueError, match="schema version is incompatible"):
        validate_storage_schema(bad_schema)


def test_negative_size_and_volume_rejection() -> None:
    for field_name in ("size", "volume"):
        with pytest.raises(ValueError, match="strictly positive"):
            _decimal_to_unsigned_int(Decimal("-1"), field_name)
        with pytest.raises(ValueError, match="strictly positive"):
            _decimal_to_unsigned_int(Decimal("0"), field_name)


def test_deterministic_field_ordering() -> None:
    assert [field.name for field in TRADE_ARROW_SCHEMA] == [
        "timestamp",
        "instrument_symbol",
        "price",
        "size",
        "side",
    ]
    assert list(dict(TRADE_POLARS_SCHEMA).keys()) == [
        "timestamp",
        "instrument_symbol",
        "price",
        "size",
        "side",
    ]
