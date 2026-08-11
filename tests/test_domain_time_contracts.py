"""Contract tests for the Phase 1.5 domain time and trade-side hardening.

These pin the invariants recorded in ADR-002: a bar's availability time is
derived rather than supplied, so a bar that becomes visible before its interval
closes cannot be constructed at all; and a trade's side is a closed vocabulary
in which ``UNKNOWN`` is a recorded fact rather than a guess.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, cast

import pyarrow as pa  # type: ignore[import-untyped]
import pytest
from pydantic import ValidationError

from quant_research_terminal.data import (
    BAR_ARROW_SCHEMA,
    SCHEMA_NAME,
    SCHEMA_VERSION,
    bar_from_storage_row,
    bar_to_storage_row,
    trade_from_storage_row,
    trade_to_storage_row,
    validate_storage_schema,
)
from quant_research_terminal.domain.models import Bar, Trade, TradeSide, parse_trade_side

BASE_TIME = datetime(2024, 1, 2, 12, 0, 0, tzinfo=UTC)
ONE_MINUTE = timedelta(minutes=1)


def _bar(**overrides: Any) -> Bar:
    values: dict[str, Any] = {
        "instrument_symbol": "ES",
        "interval_start": BASE_TIME,
        "interval": ONE_MINUTE,
        "open": Decimal("4999.50"),
        "high": Decimal("5002.00"),
        "low": Decimal("4998.75"),
        "close": Decimal("5001.25"),
        "volume": Decimal("12345"),
    }
    values.update(overrides)
    return Bar(**values)


def _trade(**overrides: Any) -> Trade:
    values: dict[str, Any] = {
        "instrument_symbol": "ES",
        "timestamp": BASE_TIME,
        "price": Decimal("5000.25"),
        "size": Decimal("2"),
        "side": TradeSide.BUY,
    }
    values.update(overrides)
    return Trade(**values)


# --------------------------------------------------------------------------
# Bar temporal invariants
# --------------------------------------------------------------------------


def test_availability_time_is_the_interval_close() -> None:
    bar = _bar()

    assert bar.interval_end == BASE_TIME + ONE_MINUTE
    assert bar.availability_time == bar.interval_end
    assert bar.timestamp == bar.availability_time


@pytest.mark.parametrize(
    "interval",
    [timedelta(microseconds=1), timedelta(seconds=1), ONE_MINUTE, timedelta(days=1)],
)
def test_availability_is_always_strictly_after_interval_start(interval: timedelta) -> None:
    bar = _bar(interval=interval)

    assert bar.availability_time > bar.interval_start


def test_completed_bar_cannot_be_made_available_at_interval_start() -> None:
    # The core safety property. Availability is derived, so there is no field
    # in which to place a value that would expose the close early.
    with pytest.raises(ValidationError):
        Bar(  # type: ignore[call-arg]
            instrument_symbol="ES",
            interval_start=BASE_TIME,
            interval=ONE_MINUTE,
            timestamp=BASE_TIME,
            open=Decimal("4999.50"),
            high=Decimal("5002.00"),
            low=Decimal("4998.75"),
            close=Decimal("5001.25"),
            volume=Decimal("12345"),
        )


def test_availability_time_cannot_be_supplied_directly() -> None:
    with pytest.raises(ValidationError):
        _bar(availability_time=BASE_TIME)


def test_bar_is_immutable() -> None:
    bar = _bar()

    with pytest.raises(ValidationError):
        bar.interval_start = BASE_TIME + ONE_MINUTE


@pytest.mark.parametrize("interval", [timedelta(0), timedelta(seconds=-1), timedelta(days=-1)])
def test_non_positive_interval_is_rejected(interval: timedelta) -> None:
    # A zero or negative interval would put availability at or before the
    # bar's own start, which is exactly the look-ahead being prevented.
    with pytest.raises(ValidationError, match="strictly positive"):
        _bar(interval=interval)


def test_interval_must_be_a_timedelta() -> None:
    with pytest.raises(ValidationError):
        _bar(interval=cast(Any, 60))


def test_naive_interval_start_is_rejected() -> None:
    with pytest.raises(ValidationError, match="timezone-aware UTC"):
        _bar(interval_start=datetime(2024, 1, 2, 12, 0, 0))


def test_non_utc_interval_start_is_rejected() -> None:
    with pytest.raises(ValidationError, match="UTC"):
        _bar(interval_start=BASE_TIME.astimezone(timezone(timedelta(hours=1))))


def test_bars_of_different_intervals_order_by_availability() -> None:
    # A daily bar starting earlier still becomes knowable later than a minute
    # bar starting later, and must order accordingly.
    daily = _bar(interval_start=BASE_TIME, interval=timedelta(days=1))
    minute = _bar(interval_start=BASE_TIME + ONE_MINUTE, interval=ONE_MINUTE)

    assert minute.timestamp < daily.timestamp
    assert minute.interval_start > daily.interval_start


# --------------------------------------------------------------------------
# TradeSide
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (TradeSide.BUY, TradeSide.BUY),
        ("buy", TradeSide.BUY),
        ("BUY", TradeSide.BUY),
        ("  sell  ", TradeSide.SELL),
        ("unknown", TradeSide.UNKNOWN),
    ],
)
def test_parse_trade_side_accepts_canonical_values(value: object, expected: TradeSide) -> None:
    assert parse_trade_side(value) is expected


@pytest.mark.parametrize("value", ["", "N", "b", "long", "none"])
def test_parse_trade_side_rejects_unrecognised_values(value: str) -> None:
    # Notably 'N' is rejected here: mapping a vendor's private code to UNKNOWN
    # is the provider's documented job, not a domain-level guess.
    with pytest.raises(ValueError, match="side must be one of"):
        parse_trade_side(value)


def test_parse_trade_side_rejects_non_string_values() -> None:
    with pytest.raises(ValueError, match="side must be a TradeSide"):
        parse_trade_side(1)


def test_trade_side_field_is_always_the_enum() -> None:
    trade = _trade(side="sell")

    assert trade.side is TradeSide.SELL
    assert isinstance(trade.side, TradeSide)


def test_trade_rejects_arbitrary_side_strings() -> None:
    with pytest.raises(ValidationError):
        _trade(side="aggressive")


def test_unknown_side_is_preserved_and_not_directional() -> None:
    trade = _trade(side=TradeSide.UNKNOWN)

    assert trade.side is TradeSide.UNKNOWN
    assert trade.side.is_directional is False


def test_directional_sides_report_as_directional() -> None:
    assert TradeSide.BUY.is_directional is True
    assert TradeSide.SELL.is_directional is True


# --------------------------------------------------------------------------
# Storage round-trip
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "interval",
    [
        timedelta(microseconds=1),
        timedelta(milliseconds=500),
        timedelta(seconds=1),
        ONE_MINUTE,
        timedelta(hours=4),
        timedelta(days=1),
    ],
)
def test_bar_round_trip_preserves_both_temporal_coordinates(interval: timedelta) -> None:
    bar = _bar(interval=interval)

    round_trip = bar_from_storage_row(bar_to_storage_row(bar), schema=BAR_ARROW_SCHEMA)

    assert round_trip == bar
    assert round_trip.interval == interval
    assert round_trip.interval_start == bar.interval_start
    assert round_trip.availability_time == bar.availability_time


def test_bar_storage_row_persists_availability_and_interval() -> None:
    row = bar_to_storage_row(_bar())

    assert row["timestamp"] == BASE_TIME + ONE_MINUTE
    assert row["interval_microseconds"] == 60_000_000


def test_bar_storage_rejects_non_positive_stored_interval() -> None:
    row = dict(bar_to_storage_row(_bar()))
    row["interval_microseconds"] = 0

    with pytest.raises(ValueError, match="strictly positive"):
        bar_from_storage_row(row)


def test_bar_storage_rejects_non_integer_interval() -> None:
    row: dict[str, Any] = dict(bar_to_storage_row(_bar()))
    row["interval_microseconds"] = "60000000"

    with pytest.raises(TypeError, match="stored as an int"):
        bar_from_storage_row(row)


@pytest.mark.parametrize("side", list(TradeSide))
def test_trade_round_trip_preserves_every_side(side: TradeSide) -> None:
    trade = _trade(side=side)

    round_trip = trade_from_storage_row(trade_to_storage_row(trade))

    assert round_trip == trade
    assert round_trip.side is side


def test_trade_side_persists_as_its_canonical_value() -> None:
    row = trade_to_storage_row(_trade(side=TradeSide.UNKNOWN))

    assert row["side"] == "unknown"
    assert isinstance(row["side"], str)


def test_stored_side_outside_the_vocabulary_is_rejected() -> None:
    row = dict(trade_to_storage_row(_trade()))
    row["side"] = "N"

    with pytest.raises(ValueError, match="side must be one of"):
        trade_from_storage_row(row)


# --------------------------------------------------------------------------
# Schema version compatibility
# --------------------------------------------------------------------------


def test_legacy_schema_version_is_two_and_current_is_three() -> None:
    from quant_research_terminal.data.contracts import LEGACY_SCHEMA_VERSION

    assert LEGACY_SCHEMA_VERSION == 2
    assert SCHEMA_VERSION == 3


def test_bar_schema_metadata_declares_version_two() -> None:
    assert BAR_ARROW_SCHEMA.metadata[b"schema_version"] == b"2"
    validate_storage_schema(BAR_ARROW_SCHEMA)


def test_version_one_data_is_rejected_rather_than_read() -> None:
    # A version-1 bar records no interval, so its single timestamp cannot be
    # resolved into interval start and availability. Reading it would require
    # guessing the interval.
    legacy = pa.schema(
        [
            pa.field("timestamp", pa.timestamp("us", tz="UTC")),
            pa.field("instrument_symbol", pa.utf8()),
            pa.field("open", pa.int64()),
            pa.field("high", pa.int64()),
            pa.field("low", pa.int64()),
            pa.field("close", pa.int64()),
            pa.field("volume", pa.uint64()),
        ],
        metadata={
            b"schema_name": SCHEMA_NAME.encode("utf-8"),
            b"schema_version": b"1",
            b"timestamp_timezone": b"UTC",
            b"price_encoding": b"fixed_scale_decimal",
            b"price_precision": b"18",
            b"price_scale": b"6",
        },
    )

    with pytest.raises(ValueError, match="schema version is incompatible"):
        validate_storage_schema(legacy)


def test_reading_a_version_one_bar_row_fails_on_the_missing_interval() -> None:
    legacy_row = {
        "timestamp": BASE_TIME,
        "instrument_symbol": "ES",
        "open": 4999500000,
        "high": 5002000000,
        "low": 4998750000,
        "close": 5001250000,
        "volume": 12345,
    }

    with pytest.raises(KeyError):
        bar_from_storage_row(legacy_row)
