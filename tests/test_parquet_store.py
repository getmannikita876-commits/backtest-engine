"""Real file-based Arrow/Parquet round-trip tests.

Every test here writes an actual Parquet file to a temporary directory and
reads it back. Nothing is mocked. Before this phase the storage layer's
guarantees were exercised only in memory, so they were assertions about
conversion functions rather than about persistence.

The property the suite exists to establish:

    valid domain record -> write -> read -> equal domain record

Boundary values are generated from the storage contract's own constants, so a
change to the scale or bounds moves the grid with it. No Hypothesis dependency.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import polars as pl
import pyarrow as pa  # type: ignore[import-untyped]
import pyarrow.parquet as pq  # type: ignore[import-untyped]
import pytest

from quant_research_terminal.data import (
    BAR_ARROW_SCHEMA,
    MAX_FIXED_POINT_VALUE,
    MAX_TIMESTAMP_MICROSECONDS,
    MIN_TIMESTAMP_MICROSECONDS,
    PRICE_QUANTUM,
    SCHEMA_NAME,
    TRADE_ARROW_SCHEMA,
    UINT64_MAX,
    StorageContractError,
    read_bars,
    read_quotes,
    read_schema_metadata,
    read_trades,
    write_bars,
    write_quotes,
    write_trades,
)
from quant_research_terminal.data.parquet_store import PARTIAL_SUFFIX
from quant_research_terminal.domain.models import Bar, Quote, Trade, TradeSide

BASE_TIME = datetime(2024, 1, 2, 12, 0, 0, tzinfo=UTC)
ONE_MINUTE = timedelta(minutes=1)

MIN_PRICE = PRICE_QUANTUM
MAX_PRICE = Decimal(MAX_FIXED_POINT_VALUE).scaleb(-6)


def _trade(**overrides: object) -> Trade:
    values: dict[str, object] = {
        "instrument_symbol": "ES",
        "timestamp": BASE_TIME,
        "price": Decimal("5000.25"),
        "size": Decimal("2"),
        "side": TradeSide.BUY,
    }
    values.update(overrides)
    return Trade(**values)  # type: ignore[arg-type]


def _quote(**overrides: object) -> Quote:
    values: dict[str, object] = {
        "instrument_symbol": "ES",
        "timestamp": BASE_TIME,
        "bid": Decimal("5000.00"),
        "ask": Decimal("5000.25"),
        "bid_size": Decimal("1"),
        "ask_size": Decimal("1"),
    }
    values.update(overrides)
    return Quote(**values)  # type: ignore[arg-type]


def _bar(**overrides: object) -> Bar:
    values: dict[str, object] = {
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
    return Bar(**values)  # type: ignore[arg-type]


# ==========================================================================
# Trade round-trip
# ==========================================================================


def test_trade_round_trip_is_exact(tmp_path: Path) -> None:
    path = tmp_path / "trades.parquet"
    trades = [_trade()]

    write_trades(path, trades)

    assert path.exists()
    assert read_trades(path) == tuple(trades)


@pytest.mark.parametrize(
    "price",
    [MIN_PRICE, Decimal("0.000002"), Decimal("1"), Decimal("5000.25"), MAX_PRICE],
)
def test_trade_price_boundaries_survive_a_real_file(tmp_path: Path, price: Decimal) -> None:
    path = tmp_path / "trades.parquet"
    trade = _trade(price=price)

    write_trades(path, [trade])
    (restored,) = read_trades(path)

    assert restored.price == price
    assert restored == trade


@pytest.mark.parametrize("size", [Decimal(1), Decimal(2), Decimal(10**9), Decimal(UINT64_MAX)])
def test_trade_quantity_boundaries_survive_a_real_file(tmp_path: Path, size: Decimal) -> None:
    # uint64 has no native Parquet type; it is stored as INT64 with an unsigned
    # logical annotation, so the maximum is the value most likely to be lost.
    path = tmp_path / "trades.parquet"

    write_trades(path, [_trade(size=size)])
    (restored,) = read_trades(path)

    assert restored.size == size


@pytest.mark.parametrize("side", list(TradeSide))
def test_every_trade_side_survives_a_real_file(tmp_path: Path, side: TradeSide) -> None:
    path = tmp_path / "trades.parquet"

    write_trades(path, [_trade(side=side)])
    (restored,) = read_trades(path)

    assert restored.side is side
    assert isinstance(restored.side, TradeSide)


def test_repeated_identical_trades_remain_repeated(tmp_path: Path) -> None:
    # Storage must not deduplicate: two genuine executions can be identical in
    # every field, and collapsing them would understate volume (ADR-003).
    path = tmp_path / "trades.parquet"
    trades = [_trade(), _trade(), _trade()]

    write_trades(path, trades)
    restored = read_trades(path)

    assert len(restored) == 3
    assert sum(trade.size for trade in restored) == Decimal("6")


def test_trade_order_is_preserved(tmp_path: Path) -> None:
    path = tmp_path / "trades.parquet"
    trades = [
        _trade(price=Decimal("3"), timestamp=BASE_TIME + timedelta(seconds=2)),
        _trade(price=Decimal("1"), timestamp=BASE_TIME),
        _trade(price=Decimal("2"), timestamp=BASE_TIME + timedelta(seconds=1)),
    ]

    write_trades(path, trades)
    restored = read_trades(path)

    # Written order, not sorted order: storage never reorders.
    assert [trade.price for trade in restored] == [Decimal("3"), Decimal("1"), Decimal("2")]


def test_microsecond_timestamps_survive_a_real_file(tmp_path: Path) -> None:
    path = tmp_path / "trades.parquet"
    stamps = [
        BASE_TIME,
        BASE_TIME + timedelta(microseconds=1),
        BASE_TIME + timedelta(microseconds=999_999),
        datetime(1970, 1, 1, tzinfo=UTC),
    ]

    write_trades(path, [_trade(timestamp=stamp) for stamp in stamps])
    restored = read_trades(path)

    assert [trade.timestamp for trade in restored] == stamps
    assert all(trade.timestamp.tzinfo is UTC for trade in restored)


def test_empty_file_round_trips(tmp_path: Path) -> None:
    path = tmp_path / "trades.parquet"

    write_trades(path, [])

    assert read_trades(path) == ()


# ==========================================================================
# Quote round-trip
# ==========================================================================


def test_quote_round_trip_is_exact(tmp_path: Path) -> None:
    path = tmp_path / "quotes.parquet"
    quote = _quote(bid=Decimal("4999.999999"), ask=Decimal("5000.000001"))

    write_quotes(path, [quote])
    (restored,) = read_quotes(path)

    assert restored == quote
    assert restored.bid == Decimal("4999.999999")
    assert restored.ask == Decimal("5000.000001")


def test_quote_size_boundaries_survive_a_real_file(tmp_path: Path) -> None:
    path = tmp_path / "quotes.parquet"
    quote = _quote(bid_size=Decimal(1), ask_size=Decimal(UINT64_MAX))

    write_quotes(path, [quote])
    (restored,) = read_quotes(path)

    assert restored.bid_size == Decimal(1)
    assert restored.ask_size == Decimal(UINT64_MAX)


def test_quote_order_is_preserved(tmp_path: Path) -> None:
    path = tmp_path / "quotes.parquet"
    quotes = [_quote(bid_size=Decimal(index + 1)) for index in range(5)]

    write_quotes(path, quotes)

    assert [q.bid_size for q in read_quotes(path)] == [Decimal(i + 1) for i in range(5)]


# ==========================================================================
# Bar round-trip
# ==========================================================================


@pytest.mark.parametrize(
    "interval",
    [
        timedelta(microseconds=1),
        timedelta(seconds=1),
        ONE_MINUTE,
        timedelta(hours=4),
        timedelta(days=1),
    ],
)
def test_bar_interval_survives_a_real_file(tmp_path: Path, interval: timedelta) -> None:
    path = tmp_path / "bars.parquet"
    bar = _bar(interval=interval)

    write_bars(path, [bar])
    (restored,) = read_bars(path)

    assert restored == bar
    assert restored.interval == interval
    assert restored.interval_start == BASE_TIME


def test_bar_derived_availability_time_is_correct_after_reconstruction(tmp_path: Path) -> None:
    # Availability is a derived property, so reconstruction must land on an
    # interval_start that reproduces it exactly (ADR-002).
    path = tmp_path / "bars.parquet"
    bar = _bar(interval=ONE_MINUTE)

    write_bars(path, [bar])
    (restored,) = read_bars(path)

    assert restored.availability_time == BASE_TIME + ONE_MINUTE
    assert restored.availability_time == bar.availability_time
    assert restored.timestamp == restored.availability_time


def test_bar_order_is_preserved(tmp_path: Path) -> None:
    path = tmp_path / "bars.parquet"
    bars = [
        _bar(interval_start=BASE_TIME + timedelta(minutes=index), volume=Decimal(index + 1))
        for index in range(4)
    ]

    write_bars(path, bars)

    assert [bar.volume for bar in read_bars(path)] == [Decimal(i + 1) for i in range(4)]


def test_mixed_bar_intervals_in_one_file(tmp_path: Path) -> None:
    path = tmp_path / "bars.parquet"
    bars = [_bar(interval=ONE_MINUTE), _bar(interval=timedelta(days=1))]

    write_bars(path, bars)
    restored = read_bars(path)

    assert [bar.interval for bar in restored] == [ONE_MINUTE, timedelta(days=1)]


# ==========================================================================
# Determinism
# ==========================================================================


def test_same_records_read_back_equal(tmp_path: Path) -> None:
    trades = [_trade(price=Decimal("1")), _trade(price=Decimal("2"))]
    first = tmp_path / "a.parquet"
    second = tmp_path / "b.parquet"

    write_trades(first, trades)
    write_trades(second, trades)

    assert read_trades(first) == read_trades(second) == tuple(trades)


def test_repeated_writes_produce_identical_bytes(tmp_path: Path) -> None:
    # Byte identity is a stronger claim than semantic determinism and is only
    # asserted because it is verified here, for one library version.
    trades = [_trade(price=Decimal("1")), _trade(price=Decimal("2"))]
    first = tmp_path / "a.parquet"
    second = tmp_path / "b.parquet"

    write_trades(first, trades)
    write_trades(second, trades)

    assert first.read_bytes() == second.read_bytes()


def test_written_metadata_matches_the_contract(tmp_path: Path) -> None:
    path = tmp_path / "trades.parquet"
    write_trades(path, [_trade()])

    metadata = read_schema_metadata(path)

    assert metadata["schema_name"] == SCHEMA_NAME
    # A literal, deliberately: the point of this assertion is that the
    # version-2 writer keeps emitting version 2. Comparing against the current
    # constant would make the test pass no matter what the writer emitted.
    assert metadata["schema_version"] == "2"
    assert metadata["timestamp_timezone"] == "UTC"
    assert metadata["price_encoding"] == "fixed_scale_decimal"
    assert metadata["price_scale"] == "6"


def test_no_write_time_metadata_is_recorded(tmp_path: Path) -> None:
    # Nothing may embed a wall-clock value: it would make two runs differ.
    path = tmp_path / "trades.parquet"
    write_trades(path, [_trade()])

    keys = set(read_schema_metadata(path))

    assert not {key for key in keys if "time" in key.lower()} - {"timestamp_timezone"}


# ==========================================================================
# Atomic write behaviour
# ==========================================================================


def test_successful_write_leaves_no_temporary_file(tmp_path: Path) -> None:
    path = tmp_path / "trades.parquet"

    write_trades(path, [_trade()])

    assert list(tmp_path.iterdir()) == [path]


def test_failed_write_preserves_the_existing_file(tmp_path: Path) -> None:
    path = tmp_path / "trades.parquet"
    write_trades(path, [_trade(price=Decimal("1"))])
    original = path.read_bytes()

    # A value the conversion layer refuses: the write must fail before replace.
    with pytest.raises(ValueError):
        write_trades(path, [_trade(price=Decimal("0.0000001"))])

    assert path.read_bytes() == original
    assert read_trades(path)[0].price == Decimal("1")


def test_failed_write_leaves_no_orphaned_temporary_file(tmp_path: Path) -> None:
    path = tmp_path / "trades.parquet"

    with pytest.raises(ValueError):
        write_trades(path, [_trade(price=Decimal("0.0000001"))])

    assert not (tmp_path / (path.name + PARTIAL_SUFFIX)).exists()
    assert list(tmp_path.iterdir()) == []


def test_write_replaces_an_existing_file(tmp_path: Path) -> None:
    path = tmp_path / "trades.parquet"
    write_trades(path, [_trade(price=Decimal("1"))])

    write_trades(path, [_trade(price=Decimal("2")), _trade(price=Decimal("3"))])

    assert [trade.price for trade in read_trades(path)] == [Decimal("2"), Decimal("3")]


# ==========================================================================
# Contract failures on read
# ==========================================================================


def _write_raw(path: Path, table: pa.Table) -> None:
    pq.write_table(table, path, compression="snappy")


def test_unsupported_schema_version_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "trades.parquet"
    write_trades(path, [_trade()])
    table = pq.read_table(path)
    metadata = dict(table.schema.metadata)
    metadata[b"schema_version"] = b"1"
    _write_raw(path, table.replace_schema_metadata(metadata))

    with pytest.raises(StorageContractError, match="schema version is incompatible"):
        read_trades(path)


def test_missing_metadata_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "trades.parquet"
    write_trades(path, [_trade()])
    table = pq.read_table(path)
    _write_raw(path, table.replace_schema_metadata(None))

    with pytest.raises(StorageContractError, match="no schema metadata"):
        read_trades(path)


def test_wrong_timezone_metadata_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "trades.parquet"
    write_trades(path, [_trade()])
    table = pq.read_table(path)
    metadata = dict(table.schema.metadata)
    metadata[b"timestamp_timezone"] = b"America/New_York"
    _write_raw(path, table.replace_schema_metadata(metadata))

    with pytest.raises(StorageContractError, match="timezone is incompatible"):
        read_trades(path)


def test_wrong_price_scale_metadata_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "trades.parquet"
    write_trades(path, [_trade()])
    table = pq.read_table(path)
    metadata = dict(table.schema.metadata)
    metadata[b"price_scale"] = b"2"
    _write_raw(path, table.replace_schema_metadata(metadata))

    with pytest.raises(StorageContractError, match="price scale is incompatible"):
        read_trades(path)


def test_altered_arrow_type_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "trades.parquet"
    write_trades(path, [_trade()])
    table = pq.read_table(path)
    altered = table.set_column(
        table.schema.get_field_index("size"),
        pa.field("size", pa.int64()),
        table.column("size").cast(pa.int64()),
    )
    _write_raw(path, altered.replace_schema_metadata(table.schema.metadata))

    with pytest.raises(StorageContractError, match="has type int64, expected uint64"):
        read_trades(path)


def test_reading_a_trade_file_as_bars_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "trades.parquet"
    write_trades(path, [_trade()])

    with pytest.raises(StorageContractError, match="does not hold bar records"):
        read_bars(path)


def test_invalid_enum_value_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "trades.parquet"
    write_trades(path, [_trade()])
    table = pq.read_table(path)
    corrupted = table.set_column(
        table.schema.get_field_index("side"),
        pa.field("side", pa.utf8()),
        pa.array(["sideways"], type=pa.utf8()),
    )
    _write_raw(path, corrupted.replace_schema_metadata(table.schema.metadata))

    with pytest.raises(StorageContractError, match="not a valid trade record"):
        read_trades(path)


def test_invalid_fixed_point_value_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "trades.parquet"
    write_trades(path, [_trade()])
    table = pq.read_table(path)
    corrupted = table.set_column(
        table.schema.get_field_index("price"),
        pa.field("price", pa.int64()),
        pa.array([-1], type=pa.int64()),
    )
    _write_raw(path, corrupted.replace_schema_metadata(table.schema.metadata))

    with pytest.raises(StorageContractError, match="not a valid trade record"):
        read_trades(path)


def test_invalid_bar_interval_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "bars.parquet"
    write_bars(path, [_bar()])
    table = pq.read_table(path)
    corrupted = table.set_column(
        table.schema.get_field_index("interval_microseconds"),
        pa.field("interval_microseconds", pa.uint64()),
        pa.array([0], type=pa.uint64()),
    )
    _write_raw(path, corrupted.replace_schema_metadata(table.schema.metadata))

    with pytest.raises(StorageContractError, match="not a valid bar record"):
        read_bars(path)


def test_null_in_a_required_column_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "trades.parquet"
    write_trades(path, [_trade()])
    table = pq.read_table(path)
    corrupted = table.set_column(
        table.schema.get_field_index("price"),
        pa.field("price", pa.int64()),
        pa.array([None], type=pa.int64()),
    )
    _write_raw(path, corrupted.replace_schema_metadata(table.schema.metadata))

    with pytest.raises(StorageContractError, match="null value"):
        read_trades(path)


def test_corrupted_file_is_rejected_as_a_contract_error(tmp_path: Path) -> None:
    path = tmp_path / "trades.parquet"
    path.write_bytes(b"this is not parquet")

    with pytest.raises(StorageContractError, match="not a readable Parquet file"):
        read_trades(path)


def test_truncated_file_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "trades.parquet"
    write_trades(path, [_trade()])
    data = path.read_bytes()
    path.write_bytes(data[: len(data) // 2])

    with pytest.raises(StorageContractError):
        read_trades(path)


def test_missing_file_raises_filesystem_error_not_contract_error(tmp_path: Path) -> None:
    # A genuine filesystem failure keeps its own exception; wrapping it would
    # say less than the original.
    with pytest.raises(FileNotFoundError):
        read_trades(tmp_path / "absent.parquet")


# ==========================================================================
# Unrepresentable stored values
#
# Arrow holds a timestamp as int64 microseconds and a bar interval as uint64
# microseconds. Both types span far more than a Python datetime or a derived
# interval start can represent, so a syntactically valid file can carry a value
# that cannot be rebuilt. The property under test:
#
#     no public function in the storage layer raises OverflowError
#
# OverflowError is an ArithmeticError, not a ValueError, so it is not covered
# incidentally by the handlers that catch malformed values.
# ==========================================================================


def _corrupt_timestamp(path: Path, microseconds: int) -> None:
    """Rewrite the file's single timestamp with a raw microsecond count.

    The column keeps its declared Arrow type, so the file remains schema-valid
    and the failure can only come from reconstruction.
    """
    table = pq.read_table(path)
    field = table.schema.field("timestamp")
    corrupted = table.set_column(
        table.schema.get_field_index("timestamp"),
        field,
        pa.array([microseconds], type=pa.int64()).cast(field.type),
    )
    _write_raw(path, corrupted.replace_schema_metadata(table.schema.metadata))


def _corrupt_bar_interval(path: Path, microseconds: int) -> None:
    """Rewrite the file's single bar interval with a raw microsecond count."""
    table = pq.read_table(path)
    field = table.schema.field("interval_microseconds")
    corrupted = table.set_column(
        table.schema.get_field_index("interval_microseconds"),
        field,
        pa.array([microseconds], type=pa.uint64()),
    )
    _write_raw(path, corrupted.replace_schema_metadata(table.schema.metadata))


def test_timestamp_below_minimum_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "trades.parquet"
    write_trades(path, [_trade()])
    _corrupt_timestamp(path, MIN_TIMESTAMP_MICROSECONDS - 1)

    with pytest.raises(StorageContractError, match="outside the representable range"):
        read_trades(path)


def test_timestamp_above_maximum_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "trades.parquet"
    write_trades(path, [_trade()])
    _corrupt_timestamp(path, MAX_TIMESTAMP_MICROSECONDS + 1)

    with pytest.raises(StorageContractError, match="outside the representable range"):
        read_trades(path)


@pytest.mark.parametrize("microseconds", [-(2**63), 2**63 - 1])
def test_extreme_int64_timestamps_are_rejected(tmp_path: Path, microseconds: int) -> None:
    # The physical limits of the storage type, which reach some 292 000 years
    # either side of the epoch.
    path = tmp_path / "trades.parquet"
    write_trades(path, [_trade()])
    _corrupt_timestamp(path, microseconds)

    with pytest.raises(StorageContractError, match="outside the representable range"):
        read_trades(path)


@pytest.mark.parametrize("microseconds", [MIN_TIMESTAMP_MICROSECONDS, MAX_TIMESTAMP_MICROSECONDS])
def test_timestamps_at_the_representable_bounds_are_accepted(
    tmp_path: Path, microseconds: int
) -> None:
    # The guard must reject only what cannot be rebuilt. A bound that is one
    # microsecond too tight would silently make legitimate archives unreadable,
    # which is the failure this test exists to prevent.
    path = tmp_path / "trades.parquet"
    write_trades(path, [_trade()])
    _corrupt_timestamp(path, microseconds)

    (restored,) = read_trades(path)

    expected = datetime(1970, 1, 1, tzinfo=UTC) + timedelta(microseconds=microseconds)
    assert restored.timestamp == expected


def test_quote_timestamp_overflow_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "quotes.parquet"
    write_quotes(path, [_quote()])
    _corrupt_timestamp(path, MAX_TIMESTAMP_MICROSECONDS + 1)

    with pytest.raises(StorageContractError, match="outside the representable range"):
        read_quotes(path)


def test_bar_timestamp_overflow_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "bars.parquet"
    write_bars(path, [_bar()])
    _corrupt_timestamp(path, MIN_TIMESTAMP_MICROSECONDS - 1)

    with pytest.raises(StorageContractError, match="outside the representable range"):
        read_bars(path)


def test_bar_interval_overflowing_the_datetime_range_is_rejected(tmp_path: Path) -> None:
    # UINT64_MAX microseconds is a valid timedelta — roughly 584 000 years — so
    # the interval decodes cleanly and the failure only appears when interval
    # start is derived as availability_time - interval. No per-column check can
    # anticipate it, because it depends on both columns together.
    path = tmp_path / "bars.parquet"
    write_bars(path, [_bar()])
    _corrupt_bar_interval(path, UINT64_MAX)

    with pytest.raises(StorageContractError, match="not a valid bar record"):
        read_bars(path)


def _largest_representable_interval_microseconds() -> int:
    """Return the widest interval a bar at ``BASE_TIME + ONE_MINUTE`` may carry."""
    availability = BASE_TIME + ONE_MINUTE
    return (availability - datetime.min.replace(tzinfo=UTC)) // timedelta(microseconds=1)


def test_bar_interval_at_the_representable_limit_is_accepted(tmp_path: Path) -> None:
    path = tmp_path / "bars.parquet"
    write_bars(path, [_bar()])
    _corrupt_bar_interval(path, _largest_representable_interval_microseconds())

    (restored,) = read_bars(path)

    assert restored.interval_start == datetime.min.replace(tzinfo=UTC)
    assert restored.availability_time == BASE_TIME + ONE_MINUTE


def test_bar_interval_one_microsecond_past_the_limit_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "bars.parquet"
    write_bars(path, [_bar()])
    _corrupt_bar_interval(path, _largest_representable_interval_microseconds() + 1)

    with pytest.raises(StorageContractError, match="not a valid bar record"):
        read_bars(path)


@pytest.mark.parametrize(
    "microseconds",
    [
        -(2**63),
        MIN_TIMESTAMP_MICROSECONDS - 1,
        MAX_TIMESTAMP_MICROSECONDS + 1,
        2**63 - 1,
    ],
)
def test_no_overflow_error_escapes_a_corrupted_timestamp(tmp_path: Path, microseconds: int) -> None:
    path = tmp_path / "trades.parquet"
    write_trades(path, [_trade()])
    _corrupt_timestamp(path, microseconds)

    try:
        read_trades(path)
    except StorageContractError:
        pass
    except BaseException as error:  # pragma: no cover - the regression itself
        raise AssertionError(
            f"read_trades leaked {type(error).__name__}; the storage API contract "
            f"promises StorageContractError"
        ) from error
    else:  # pragma: no cover - the regression itself
        raise AssertionError("an unrepresentable timestamp was accepted")


@pytest.mark.parametrize(
    "microseconds",
    [
        2**63,
        2**64 - 1,
        _largest_representable_interval_microseconds() + 1,
    ],
)
def test_no_overflow_error_escapes_a_corrupted_interval(tmp_path: Path, microseconds: int) -> None:
    path = tmp_path / "bars.parquet"
    write_bars(path, [_bar()])
    _corrupt_bar_interval(path, microseconds)

    try:
        read_bars(path)
    except StorageContractError:
        pass
    except BaseException as error:  # pragma: no cover - the regression itself
        raise AssertionError(
            f"read_bars leaked {type(error).__name__}; the storage API contract "
            f"promises StorageContractError"
        ) from error
    else:  # pragma: no cover - the regression itself
        raise AssertionError("an unrepresentable interval was accepted")


# --------------------------------------------------------------------------
# Write-path symmetry: everything the write path can emit, the read path takes
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "timestamp",
    [
        datetime.min.replace(tzinfo=UTC),
        datetime.max.replace(tzinfo=UTC),
    ],
)
def test_extreme_but_representable_timestamps_round_trip(
    tmp_path: Path, timestamp: datetime
) -> None:
    # The read guard is derived from datetime's own limits, so the extremes a
    # domain record may legitimately carry must survive persistence.
    path = tmp_path / "trades.parquet"
    trade = _trade(timestamp=timestamp)

    write_trades(path, [trade])
    (restored,) = read_trades(path)

    assert restored == trade


def test_written_timestamps_are_always_inside_the_read_range(tmp_path: Path) -> None:
    # Symmetry stated as a property rather than by example: no datetime exists
    # outside the bounds the read path enforces, so the write path cannot emit
    # a value its own reader would refuse.
    path = tmp_path / "trades.parquet"
    write_trades(
        path,
        [
            _trade(timestamp=datetime.min.replace(tzinfo=UTC)),
            _trade(timestamp=BASE_TIME),
            _trade(timestamp=datetime.max.replace(tzinfo=UTC)),
        ],
    )
    table = pq.read_table(path)

    stored = table.column("timestamp").cast(pa.int64()).to_pylist()
    assert all(
        MIN_TIMESTAMP_MICROSECONDS <= value <= MAX_TIMESTAMP_MICROSECONDS for value in stored
    )


def test_bar_availability_time_is_always_representable(tmp_path: Path) -> None:
    # The domain refuses a bar whose interval pushes availability past the
    # representable range, so write_bars has no constructible input that would
    # store an out-of-range timestamp.
    with pytest.raises(ValueError, match="leaves the representable datetime range"):
        _bar(
            interval_start=datetime.max.replace(tzinfo=UTC) - ONE_MINUTE, interval=timedelta(days=1)
        )

    path = tmp_path / "bars.parquet"
    bar = _bar(interval_start=datetime.min.replace(tzinfo=UTC), interval=ONE_MINUTE)
    write_bars(path, [bar])

    (restored,) = read_bars(path)
    assert restored == bar


# ==========================================================================
# Cross-library: Polars reads what PyArrow wrote
# ==========================================================================


def test_polars_reads_the_written_types(tmp_path: Path) -> None:
    path = tmp_path / "trades.parquet"
    write_trades(path, [_trade(price=Decimal("5000.25"), size=Decimal(UINT64_MAX))])

    frame = pl.read_parquet(path)

    assert frame.columns == list(TRADE_ARROW_SCHEMA.names)
    assert frame.schema["price"] == pl.Int64
    assert frame.schema["size"] == pl.UInt64
    assert frame.schema["timestamp"] == pl.Datetime(time_unit="us", time_zone="UTC")
    assert frame.schema["side"] == pl.String


def test_polars_reads_the_written_values_exactly(tmp_path: Path) -> None:
    path = tmp_path / "trades.parquet"
    write_trades(path, [_trade(price=Decimal("5000.25"), size=Decimal(UINT64_MAX))])

    frame = pl.read_parquet(path)

    # Fixed-point integers and the uint64 maximum survive the hand-off.
    assert frame["price"][0] == 5_000_250_000
    assert frame["size"][0] == UINT64_MAX
    assert frame["instrument_symbol"][0] == "ES"
    assert frame["side"][0] == "buy"


def test_polars_reads_bar_columns(tmp_path: Path) -> None:
    path = tmp_path / "bars.parquet"
    write_bars(path, [_bar(interval=ONE_MINUTE)])

    frame = pl.read_parquet(path)

    assert frame.schema["interval_microseconds"] == pl.UInt64
    assert frame["interval_microseconds"][0] == 60_000_000


# ==========================================================================
# Deterministic boundary grid
# ==========================================================================


def _grid_prices() -> list[Decimal]:
    return [MIN_PRICE, Decimal("0.5"), Decimal("1"), Decimal("5000.25"), MAX_PRICE]


def _grid_quantities() -> list[Decimal]:
    return [Decimal(1), Decimal(2), Decimal(10**9), Decimal(UINT64_MAX)]


def _grid_timestamps() -> list[datetime]:
    return [
        datetime(1970, 1, 1, tzinfo=UTC),
        BASE_TIME,
        BASE_TIME + timedelta(microseconds=1),
        BASE_TIME + timedelta(microseconds=999_999),
    ]


def test_trade_boundary_grid_round_trips(tmp_path: Path) -> None:
    path = tmp_path / "trades.parquet"
    trades = [
        _trade(price=price, size=size, timestamp=stamp, side=side)
        for price in _grid_prices()
        for size in _grid_quantities()
        for stamp in _grid_timestamps()
        for side in TradeSide
    ]

    write_trades(path, trades)
    restored = read_trades(path)

    assert len(restored) == len(trades)
    assert restored == tuple(trades)


def test_quote_boundary_grid_round_trips(tmp_path: Path) -> None:
    path = tmp_path / "quotes.parquet"
    quotes = [
        _quote(bid=price, ask=price, bid_size=size, ask_size=size, timestamp=stamp)
        for price in _grid_prices()
        for size in _grid_quantities()
        for stamp in _grid_timestamps()
    ]

    write_quotes(path, quotes)

    assert read_quotes(path) == tuple(quotes)


def test_bar_boundary_grid_round_trips(tmp_path: Path) -> None:
    path = tmp_path / "bars.parquet"
    intervals = [timedelta(microseconds=1), timedelta(seconds=1), ONE_MINUTE, timedelta(days=1)]
    bars = [
        _bar(interval_start=stamp, interval=interval, volume=volume)
        for stamp in _grid_timestamps()
        for interval in intervals
        for volume in _grid_quantities()
    ]

    write_bars(path, bars)
    restored = read_bars(path)

    assert restored == tuple(bars)
    assert all(bar.availability_time == bar.interval_start + bar.interval for bar in restored)


def test_schema_is_unchanged_by_a_round_trip(tmp_path: Path) -> None:
    path = tmp_path / "bars.parquet"
    write_bars(path, [_bar()])

    schema = pq.read_schema(path)

    assert tuple(schema.names) == tuple(BAR_ARROW_SCHEMA.names)
    assert schema.types == BAR_ARROW_SCHEMA.types
