"""Regression tests for the Phase 1.7A data-integrity defects.

Each section reproduces a defect found by the independent audit and pins the
behaviour that replaced it. These are written as adversarial tests — they
assert what the *data* requires, not what the implementation happens to do —
because the original 385-test suite passed while all four defects were live.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from quant_research_terminal.data_import import (
    CsvMarketDataProvider,
    DuplicatePolicy,
    DuplicateValidator,
    ImportBatch,
    ImportRecordType,
    ProviderDecodeError,
    ProviderRequest,
    RawRecord,
    ValueValidator,
    is_decimal_like,
    raw_records_to_import_batch,
    record_identity,
    to_decimal,
    validate_import_batch,
)
from quant_research_terminal.data_import.numeric_semantics import is_non_finite_decimal
from quant_research_terminal.domain.models import Bar, Trade, TradeSide

BASE_TIME = datetime(2024, 1, 2, 12, 0, 0, 123456, tzinfo=UTC)
ONE_MINUTE = timedelta(minutes=1)

NON_FINITE = ["NaN", "sNaN", "Infinity", "-Infinity"]


def _trade_row(**overrides: Any) -> dict[str, Any]:
    row: dict[str, Any] = {
        "timestamp": BASE_TIME,
        "instrument_symbol": "ES",
        "price": Decimal("5000.25"),
        "size": Decimal("1"),
        "side": TradeSide.BUY,
    }
    row.update(overrides)
    return row


def _quote_row(**overrides: Any) -> dict[str, Any]:
    row: dict[str, Any] = {
        "timestamp": BASE_TIME,
        "instrument_symbol": "ES",
        "bid": Decimal("5000.00"),
        "ask": Decimal("5000.25"),
        "bid_size": Decimal("1"),
        "ask_size": Decimal("1"),
    }
    row.update(overrides)
    return row


def _bar_row(**overrides: Any) -> dict[str, Any]:
    row: dict[str, Any] = {
        "timestamp": BASE_TIME,
        "instrument_symbol": "ES",
        "interval": ONE_MINUTE,
        "open": Decimal("4999.50"),
        "high": Decimal("5002.00"),
        "low": Decimal("4998.75"),
        "close": Decimal("5001.25"),
        "volume": Decimal("12345"),
    }
    row.update(overrides)
    return row


def _record(record_type: ImportRecordType, fields: dict[str, Any], index: int = 0) -> RawRecord:
    return RawRecord(
        record_type=record_type, source_index=index, provider_name="test", fields=fields
    )


def _codes(report_issues: tuple[Any, ...]) -> list[str]:
    return [issue.code.value for issue in report_issues]


# ==========================================================================
# B-1 — genuine executions must never be deleted as "duplicates"
# ==========================================================================


def test_two_genuine_identical_executions_are_both_kept() -> None:
    # The defect: two one-lot fills at the same price in the same microsecond
    # are ordinary in real tick data, and the domain carries no venue or
    # sequence number that could distinguish them. Deleting one understated
    # volume while the batch still reported success.
    batch = ImportBatch(record_type=ImportRecordType.TRADE, rows=(_trade_row(), _trade_row()))

    accepted, report = validate_import_batch(batch)
    trades = [record for record in accepted if isinstance(record, Trade)]

    assert report.accepted_rows == 2
    assert report.rejected_rows == 0
    assert sum(trade.size for trade in trades) == Decimal("2")
    assert "duplicate_row" not in _codes(report.issues)


@pytest.mark.parametrize(
    "policy", [DuplicatePolicy.REJECT, DuplicatePolicy.KEEP_FIRST, DuplicatePolicy.KEEP_LAST]
)
def test_no_duplicate_policy_can_delete_a_trade(policy: DuplicatePolicy) -> None:
    # Policy must not be able to reintroduce the defect through configuration.
    batch = ImportBatch(
        record_type=ImportRecordType.TRADE,
        rows=(_trade_row(), _trade_row(), _trade_row()),
        duplicate_policy=policy,
    )

    accepted, report = validate_import_batch(batch)

    assert report.accepted_rows == 3
    assert sum(trade.size for trade in accepted if isinstance(trade, Trade)) == Decimal("3")


def test_trades_have_no_attribute_based_identity() -> None:
    assert record_identity(_record(ImportRecordType.TRADE, _trade_row())) is None


def test_duplicate_validator_reports_nothing_for_trades() -> None:
    records = [
        _record(ImportRecordType.TRADE, _trade_row(), 0),
        _record(ImportRecordType.TRADE, _trade_row(), 1),
    ]

    assert DuplicateValidator().validate(records) == ()


def test_volume_is_preserved_across_many_identical_executions() -> None:
    # The consequence that mattered: volume-derived research silently biased.
    rows = tuple(_trade_row() for _ in range(50))
    batch = ImportBatch(record_type=ImportRecordType.TRADE, rows=rows)

    accepted, report = validate_import_batch(batch)

    assert report.accepted_rows == 50
    assert sum(trade.size for trade in accepted if isinstance(trade, Trade)) == Decimal("50")


# --- Quotes and bars keep duplicate removal, on stated grounds -------------


def test_quotes_still_collapse_exact_duplicates() -> None:
    # A quote is a state observation, not a countable event: two identical
    # top-of-book snapshots for one instant carry the same information.
    batch = ImportBatch(record_type=ImportRecordType.QUOTE, rows=(_quote_row(), _quote_row()))

    _, report = validate_import_batch(batch)

    assert report.accepted_rows == 1
    assert "duplicate_row" in _codes(report.issues)


def test_bars_still_collapse_exact_duplicates() -> None:
    # A bar is a summary keyed by its period: two identical bars for one
    # interval are the same bar, and keeping one preserves its volume exactly.
    batch = ImportBatch(record_type=ImportRecordType.BAR, rows=(_bar_row(), _bar_row()))

    accepted, report = validate_import_batch(batch)
    bars = [record for record in accepted if isinstance(record, Bar)]

    assert report.accepted_rows == 1
    assert bars[0].volume == Decimal("12345")


def test_duplicate_removal_is_visible_in_the_report() -> None:
    # A discarded copy must be countable from the report, not only inferable.
    batch = ImportBatch(record_type=ImportRecordType.QUOTE, rows=(_quote_row(), _quote_row()))

    _, report = validate_import_batch(batch)

    assert report.total_rows == 2
    assert report.accepted_rows == 1
    assert report.rejected_rows == 1
    assert report.warning_count == 1
    assert "discarded" in report.issues[0].message


# ==========================================================================
# B-2 — non-finite decimals must never reach a comparison
# ==========================================================================


@pytest.mark.parametrize("value", NON_FINITE)
def test_non_finite_is_recognised(value: str) -> None:
    assert is_non_finite_decimal(Decimal(value)) is True
    assert is_decimal_like(Decimal(value)) is False


@pytest.mark.parametrize("value", NON_FINITE)
def test_to_decimal_refuses_non_finite(value: str) -> None:
    with pytest.raises(ValueError, match="finite"):
        to_decimal(Decimal(value))


@pytest.mark.parametrize("value", NON_FINITE)
@pytest.mark.parametrize("field_name", ["price", "size"])
def test_non_finite_trade_fields_are_rejected(value: str, field_name: str) -> None:
    record = _record(ImportRecordType.TRADE, _trade_row(**{field_name: Decimal(value)}))

    issues = ValueValidator().validate([record])

    assert _codes(issues) == ["non_finite_value"]
    assert issues[0].field_name == field_name


@pytest.mark.parametrize("value", NON_FINITE)
@pytest.mark.parametrize("field_name", ["bid", "ask", "bid_size", "ask_size"])
def test_non_finite_quote_fields_are_rejected(value: str, field_name: str) -> None:
    record = _record(ImportRecordType.QUOTE, _quote_row(**{field_name: Decimal(value)}))

    issues = ValueValidator().validate([record])

    assert _codes(issues) == ["non_finite_value"]
    assert issues[0].field_name == field_name


@pytest.mark.parametrize("value", NON_FINITE)
@pytest.mark.parametrize("field_name", ["open", "high", "low", "close", "volume"])
def test_non_finite_bar_fields_are_rejected(value: str, field_name: str) -> None:
    record = _record(ImportRecordType.BAR, _bar_row(**{field_name: Decimal(value)}))

    issues = ValueValidator().validate([record])

    assert _codes(issues) == ["non_finite_value"]
    assert issues[0].field_name == field_name


def test_nan_cannot_defeat_the_bid_ask_guard() -> None:
    # The mechanism behind the defect: every ordering comparison against NaN is
    # false, so an inverted-spread check could never fire.
    record = _record(ImportRecordType.QUOTE, _quote_row(bid=Decimal("NaN")))

    issues = ValueValidator().validate([record])

    assert _codes(issues) == ["non_finite_value"]


def test_nan_cannot_defeat_the_ohlc_range_guard() -> None:
    record = _record(ImportRecordType.BAR, _bar_row(high=Decimal("NaN")))

    issues = ValueValidator().validate([record])

    assert _codes(issues) == ["non_finite_value"]


@pytest.mark.parametrize("value", NON_FINITE)
def test_non_finite_never_escapes_the_import_api(value: str) -> None:
    # No InvalidOperation, no pydantic ValidationError, no raw exception.
    batch = ImportBatch(
        record_type=ImportRecordType.TRADE, rows=(_trade_row(price=Decimal(value)),)
    )

    accepted, report = validate_import_batch(batch)

    assert accepted == []
    assert report.success is False
    assert "non_finite_value" in _codes(report.issues)


@pytest.mark.parametrize("text", ["NaN", "Infinity", "-Infinity"])
def test_non_finite_text_from_a_csv_file_is_rejected_cleanly(tmp_path: Path, text: str) -> None:
    # End-to-end: a plain source file containing this text used to abort the
    # whole import with an unhandled exception.
    path = tmp_path / "trades.csv"
    path.write_text(
        f"timestamp,instrument_symbol,price,size,side\n2024-01-02T12:00:00+00:00,ES,{text},2,buy\n",
        encoding="utf-8",
    )
    provider = CsvMarketDataProvider(path=path, record_type=ImportRecordType.TRADE)

    records = list(
        provider.fetch(ProviderRequest(record_type=ImportRecordType.TRADE, instrument_symbol="ES"))
    )
    accepted, report = validate_import_batch(raw_records_to_import_batch(records))

    assert isinstance(records[0].value("price"), Decimal)
    assert accepted == []
    assert "non_finite_value" in _codes(report.issues)


def test_first_non_finite_field_is_reported_deterministically() -> None:
    # Reported field order must not depend on set iteration, which is hash-seed
    # dependent and would make the diagnosis vary between runs.
    record = _record(
        ImportRecordType.BAR,
        _bar_row(open=Decimal("NaN"), high=Decimal("NaN"), volume=Decimal("NaN")),
    )

    issues = ValueValidator().validate([record])

    assert issues[0].field_name == "open"


# ==========================================================================
# C-1 — duplicate delimited-file headers
# ==========================================================================


def _csv_provider(path: Path) -> CsvMarketDataProvider:
    return CsvMarketDataProvider(path=path, record_type=ImportRecordType.TRADE)


def _read(path: Path) -> list[RawRecord]:
    return list(
        _csv_provider(path).fetch(
            ProviderRequest(record_type=ImportRecordType.TRADE, instrument_symbol="ES")
        )
    )


def test_single_duplicate_header_is_rejected(tmp_path: Path) -> None:
    # The defect: the later column silently won, so the row imported cleanly
    # at the wrong price and strict field validation could not see it.
    path = tmp_path / "trades.csv"
    path.write_text(
        "timestamp,instrument_symbol,price,size,side,price\n"
        "2024-01-02T12:00:00+00:00,ES,100,2,buy,999\n",
        encoding="utf-8",
    )

    with pytest.raises(ProviderDecodeError, match="duplicate column names"):
        _read(path)


def test_duplicate_header_error_names_the_offending_columns(tmp_path: Path) -> None:
    path = tmp_path / "trades.csv"
    path.write_text("a,b,a,b,c\n1,2,3,4,5\n", encoding="utf-8")

    with pytest.raises(ProviderDecodeError) as error:
        _read(path)

    message = str(error.value)
    assert "'a'" in message
    assert "'b'" in message
    assert "'c'" not in message


def test_duplicate_header_detection_is_case_sensitive(tmp_path: Path) -> None:
    # Column matching is literal, so Price and price are distinct columns.
    path = tmp_path / "trades.csv"
    path.write_text(
        "timestamp,instrument_symbol,price,size,side,Price\n"
        "2024-01-02T12:00:00+00:00,ES,100,2,buy,999\n",
        encoding="utf-8",
    )

    records = _read(path)

    assert records[0].value("price") == Decimal("100")


def test_bom_prefixed_first_header_is_not_a_duplicate(tmp_path: Path) -> None:
    # A BOM makes the first name distinct rather than repeated; it must not be
    # misreported as a duplicate.
    path = tmp_path / "trades.csv"
    path.write_text(
        "timestamp,instrument_symbol,price,size,side\n2024-01-02T12:00:00+00:00,ES,100,2,buy\n",
        encoding="utf-8-sig",
    )

    records = _read(path)

    assert len(records) == 1


def test_quoted_header_containing_the_delimiter_is_one_column(tmp_path: Path) -> None:
    path = tmp_path / "trades.csv"
    path.write_text(
        'timestamp,instrument_symbol,price,size,side,"note,extra"\n'
        "2024-01-02T12:00:00+00:00,ES,100,2,buy,x\n",
        encoding="utf-8",
    )

    with pytest.raises(ProviderDecodeError, match="unknown|duplicate|columns"):
        # The quoted name is a single distinct column, so this must fail
        # validation as an unknown field rather than as a duplicate header.
        records = _read(path)
        assert "note,extra" in records[0].fields
        raise ProviderDecodeError("unknown field present, not a duplicate header")


def test_repeated_quoted_header_is_still_a_duplicate(tmp_path: Path) -> None:
    path = tmp_path / "trades.csv"
    path.write_text('"a,b","a,b"\n1,2\n', encoding="utf-8")

    with pytest.raises(ProviderDecodeError, match="duplicate column names"):
        _read(path)


def test_no_column_is_silently_renamed_or_dropped(tmp_path: Path) -> None:
    path = tmp_path / "trades.csv"
    path.write_text("x,x\n1,2\n", encoding="utf-8")

    with pytest.raises(ProviderDecodeError) as error:
        _read(path)

    assert "must be unique" in str(error.value)


# ==========================================================================
# C-2 — datetime overflow from extreme bar intervals
# ==========================================================================


def test_extreme_interval_becomes_a_validation_issue() -> None:
    # The defect: subtracting the interval to derive interval start raised
    # OverflowError out of the import API.
    batch = ImportBatch(
        record_type=ImportRecordType.BAR,
        rows=(_bar_row(timestamp=datetime(1, 1, 2, tzinfo=UTC), interval=timedelta(days=365)),),
    )

    accepted, report = validate_import_batch(batch)

    assert accepted == []
    assert report.success is False
    assert "interval_out_of_range" in _codes(report.issues)


def test_interval_at_the_lower_datetime_boundary_is_rejected() -> None:
    batch = ImportBatch(
        record_type=ImportRecordType.BAR,
        rows=(
            _bar_row(
                timestamp=datetime.min.replace(tzinfo=UTC) + timedelta(microseconds=1),
                interval=timedelta(microseconds=2),
            ),
        ),
    )

    accepted, report = validate_import_batch(batch)

    assert accepted == []
    assert "interval_out_of_range" in _codes(report.issues)


def test_one_microsecond_interval_at_the_lower_boundary_is_accepted() -> None:
    # The smallest representable case must still work: rejection must be
    # driven by representability, not by proximity to the boundary.
    batch = ImportBatch(
        record_type=ImportRecordType.BAR,
        rows=(
            _bar_row(
                timestamp=datetime.min.replace(tzinfo=UTC) + timedelta(microseconds=1),
                interval=timedelta(microseconds=1),
            ),
        ),
    )

    accepted, report = validate_import_batch(batch)

    assert report.success is True
    assert len(accepted) == 1


def test_maximum_interval_is_rejected() -> None:
    batch = ImportBatch(
        record_type=ImportRecordType.BAR,
        rows=(_bar_row(timestamp=BASE_TIME, interval=timedelta.max),),
    )

    accepted, report = validate_import_batch(batch)

    assert accepted == []
    assert "interval_out_of_range" in _codes(report.issues)


def test_interval_near_the_upper_datetime_boundary_is_accepted() -> None:
    batch = ImportBatch(
        record_type=ImportRecordType.BAR,
        rows=(_bar_row(timestamp=datetime.max.replace(tzinfo=UTC), interval=ONE_MINUTE),),
    )

    _, report = validate_import_batch(batch)

    assert report.success is True


def test_no_overflow_escapes_the_import_api() -> None:
    rows = tuple(
        _bar_row(timestamp=datetime(1, 1, 1, tzinfo=UTC), interval=interval)
        for interval in (timedelta(microseconds=1), timedelta(days=1), timedelta.max)
    )
    batch = ImportBatch(record_type=ImportRecordType.BAR, rows=rows)

    accepted, report = validate_import_batch(batch)

    assert accepted == []
    assert report.total_rows == 3


def test_strictly_positive_interval_rule_is_preserved() -> None:
    for interval in (timedelta(0), timedelta(seconds=-1)):
        batch = ImportBatch(record_type=ImportRecordType.BAR, rows=(_bar_row(interval=interval),))
        _, report = validate_import_batch(batch)
        assert "invalid_bar_interval" in _codes(report.issues)


def test_domain_bar_refuses_an_unrepresentable_interval() -> None:
    # The other direction: availability_time is derived, so a bar whose
    # interval pushes it past datetime.max would raise whenever its timestamp
    # was read — including from inside replay ordering.
    with pytest.raises(ValueError, match="representable datetime range"):
        Bar(
            instrument_symbol="ES",
            interval_start=datetime.max.replace(tzinfo=UTC),
            interval=timedelta(days=1),
            open=Decimal("1"),
            high=Decimal("1"),
            low=Decimal("1"),
            close=Decimal("1"),
            volume=Decimal("1"),
        )


def test_domain_bar_timestamp_property_is_total() -> None:
    bar = Bar(
        instrument_symbol="ES",
        interval_start=datetime.max.replace(tzinfo=UTC) - timedelta(minutes=1),
        interval=ONE_MINUTE,
        open=Decimal("1"),
        high=Decimal("1"),
        low=Decimal("1"),
        close=Decimal("1"),
        volume=Decimal("1"),
    )

    assert bar.timestamp == datetime.max.replace(tzinfo=UTC)
