from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal

from quant_research_terminal.data_import import (
    DuplicatePolicy,
    ImportBatch,
    ImportRecordType,
    ValidationSeverity,
    validate_import_batch,
)
from quant_research_terminal.domain.models import Trade


def _trade_row(**overrides: object) -> dict[str, object]:
    row: dict[str, object] = {
        "timestamp": datetime(2024, 1, 2, 12, 0, 0, tzinfo=UTC),
        "instrument_symbol": "ES",
        "price": Decimal("5000.25"),
        "size": Decimal("2"),
        "side": "buy",
    }
    row.update(overrides)
    return row


def _quote_row(**overrides: object) -> dict[str, object]:
    row: dict[str, object] = {
        "timestamp": datetime(2024, 1, 2, 12, 0, 0, tzinfo=UTC),
        "instrument_symbol": "ES",
        "bid": Decimal("5000.00"),
        "ask": Decimal("5000.25"),
        "bid_size": Decimal("1"),
        "ask_size": Decimal("1"),
    }
    row.update(overrides)
    return row


def _bar_row(**overrides: object) -> dict[str, object]:
    row: dict[str, object] = {
        "timestamp": datetime(2024, 1, 2, 12, 0, 0, tzinfo=UTC),
        "instrument_symbol": "ES",
        "open": Decimal("4999.50"),
        "high": Decimal("5002.00"),
        "low": Decimal("4998.75"),
        "close": Decimal("5001.25"),
        "volume": Decimal("12345"),
    }
    row.update(overrides)
    return row


def test_rejects_naive_datetime() -> None:
    batch = ImportBatch(
        record_type=ImportRecordType.TRADE,
        rows=(_trade_row(timestamp=datetime(2024, 1, 2, 12, 0, 0)),),
    )

    accepted, report = validate_import_batch(batch)

    assert accepted == []
    assert report.success is False
    assert any(issue.code == "naive_datetime" for issue in report.issues)
    assert any(issue.severity is ValidationSeverity.ERROR for issue in report.issues)


def test_rejects_duplicate_rows_by_default() -> None:
    batch = ImportBatch(
        record_type=ImportRecordType.TRADE,
        rows=(_trade_row(), _trade_row()),
    )

    accepted, report = validate_import_batch(batch)

    assert len(accepted) == 1
    assert report.accepted_rows == 1
    assert report.rejected_rows == 1
    assert any(issue.code == "duplicate_row" for issue in report.issues)


def test_sorts_same_timestamp_records_by_original_position() -> None:
    batch = ImportBatch(
        record_type=ImportRecordType.TRADE,
        rows=(
            _trade_row(timestamp=datetime(2024, 1, 2, 12, 0, 0, tzinfo=UTC), side="buy"),
            _trade_row(timestamp=datetime(2024, 1, 2, 12, 0, 0, tzinfo=UTC), side="sell"),
        ),
    )

    accepted, _ = validate_import_batch(batch)

    assert isinstance(accepted[0], Trade)
    assert isinstance(accepted[1], Trade)
    assert accepted[0].side == "buy"
    assert accepted[1].side == "sell"


def test_rejects_non_utc_timestamp() -> None:
    batch = ImportBatch(
        record_type=ImportRecordType.TRADE,
        rows=(
            _trade_row(
                timestamp=datetime(2024, 1, 2, 12, 0, 0, tzinfo=UTC).astimezone(
                    timezone(timedelta(hours=1))
                )
            ),
        ),
    )

    _, report = validate_import_batch(batch)

    assert any(issue.code == "non_utc_timestamp" for issue in report.issues)


def test_rejects_float_price_input() -> None:
    batch = ImportBatch(
        record_type=ImportRecordType.TRADE,
        rows=(_trade_row(price=1.25),),
    )

    _, report = validate_import_batch(batch)

    assert any(issue.code == "non_decimal_price" for issue in report.issues)


def test_rejects_bid_greater_than_ask() -> None:
    batch = ImportBatch(
        record_type=ImportRecordType.QUOTE,
        rows=(_quote_row(bid=Decimal("5000.50"), ask=Decimal("5000.25")),),
    )

    _, report = validate_import_batch(batch)

    assert any(issue.code == "bid_ask_inversion" for issue in report.issues)


def test_rejects_invalid_ohlc() -> None:
    batch = ImportBatch(
        record_type=ImportRecordType.BAR,
        rows=(_bar_row(high=Decimal("4998.00"), low=Decimal("4999.00")),),
    )

    _, report = validate_import_batch(batch)

    assert any(issue.code == "invalid_ohlc" for issue in report.issues)


def test_rejects_schema_version_mismatch() -> None:
    batch = ImportBatch(
        record_type=ImportRecordType.TRADE,
        rows=(_trade_row(),),
        schema_version=999,
    )

    _, report = validate_import_batch(batch)

    assert any(issue.code == "unsupported_schema_version" for issue in report.issues)


def test_keep_first_duplicate_policy() -> None:
    batch = ImportBatch(
        record_type=ImportRecordType.TRADE,
        rows=(_trade_row(), _trade_row()),
        duplicate_policy=DuplicatePolicy.KEEP_FIRST,
    )

    accepted, report = validate_import_batch(batch)

    assert len(accepted) == 1
    assert report.accepted_rows == 1
    assert any(issue.code == "duplicate_row" for issue in report.issues)


def test_keep_last_duplicate_policy() -> None:
    batch = ImportBatch(
        record_type=ImportRecordType.TRADE,
        rows=(_trade_row(), _trade_row()),
        duplicate_policy=DuplicatePolicy.KEEP_LAST,
    )

    accepted, report = validate_import_batch(batch)

    assert len(accepted) == 1
    assert report.accepted_rows == 1
    assert any(issue.code == "duplicate_row" for issue in report.issues)


def test_caller_input_remains_unchanged() -> None:
    rows = [_trade_row()]
    batch = ImportBatch(record_type=ImportRecordType.TRADE, rows=tuple(rows))

    validate_import_batch(batch)

    assert rows[0]["price"] == Decimal("5000.25")
    assert rows[0]["instrument_symbol"] == "ES"
