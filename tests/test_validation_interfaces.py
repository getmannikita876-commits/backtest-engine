from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone, tzinfo
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from quant_research_terminal.data_import import (
    CsvMarketDataProvider,
    DuplicateValidator,
    ImportRecordType,
    OrderingValidator,
    ProviderRequest,
    RawRecord,
    RecordBatchValidator,
    RecordNormalizer,
    SchemaValidator,
    TimestampValidator,
    ValidationPipeline,
    ValidationSeverity,
    batch_validation_pipeline,
    default_validation_pipeline,
    raw_records_to_import_batch,
    validate_import_batch,
)
from quant_research_terminal.domain.models import Trade, parse_trade_side

BASE_TIME = datetime(2024, 1, 2, 12, 0, 0, tzinfo=UTC)


class _ZeroOffsetZone(tzinfo):
    """A non-fixed zone that currently reports a zero offset.

    Stands in for zones such as Europe/London in winter without requiring the
    tzdata package to be installed.
    """

    def utcoffset(self, dt: datetime | None) -> timedelta:
        return timedelta(0)

    def dst(self, dt: datetime | None) -> timedelta:
        return timedelta(0)

    def tzname(self, dt: datetime | None) -> str:
        return "ZERO"


def _trade_fields(**overrides: object) -> dict[str, Any]:
    fields: dict[str, Any] = {
        "timestamp": BASE_TIME,
        "instrument_symbol": "ES",
        "price": Decimal("5000.25"),
        "size": Decimal("2"),
        "side": "buy",
    }
    fields.update(overrides)
    return fields


def _record(source_index: int = 0, **overrides: object) -> RawRecord:
    return RawRecord(
        record_type=ImportRecordType.TRADE,
        source_index=source_index,
        provider_name="test",
        fields=_trade_fields(**overrides),
    )


def _codes(issues: tuple[Any, ...]) -> list[str]:
    return [issue.code.value for issue in issues]


# --------------------------------------------------------------------------
# Protocol conformance
# --------------------------------------------------------------------------


def test_every_validator_satisfies_the_interface() -> None:
    validators = [
        SchemaValidator(),
        TimestampValidator(),
        DuplicateValidator(),
        OrderingValidator(),
    ]

    for validator in validators:
        assert isinstance(validator, RecordBatchValidator)


def test_validators_have_distinct_names() -> None:
    names = [validator.name for validator in default_validation_pipeline().validators]

    assert names == ["schema", "timestamp", "value", "duplicate", "ordering"]
    assert len(set(names)) == len(names)


def test_batch_pipeline_omits_ordering_enforcement() -> None:
    # The batch API sorts its output rather than rejecting unsorted input, so
    # reporting descending timestamps there would flag rows the caller never
    # claimed were ordered.
    names = [validator.name for validator in batch_validation_pipeline().validators]

    assert names == ["schema", "timestamp", "value", "duplicate"]


# --------------------------------------------------------------------------
# SchemaValidator
# --------------------------------------------------------------------------


def test_schema_validator_accepts_a_complete_record() -> None:
    assert SchemaValidator().validate([_record()]) == ()


def test_schema_validator_reports_missing_field() -> None:
    fields = _trade_fields()
    del fields["price"]
    record = RawRecord(
        record_type=ImportRecordType.TRADE,
        source_index=0,
        provider_name="test",
        fields=fields,
    )

    issues = SchemaValidator().validate([record])

    assert _codes(issues) == ["missing_required_field"]
    assert issues[0].field_name == "price"
    assert issues[0].severity is ValidationSeverity.ERROR


def test_schema_validator_reports_unknown_field_when_strict() -> None:
    issues = SchemaValidator().validate([_record(exchange="CME")])

    assert _codes(issues) == ["unknown_field"]
    assert issues[0].field_name == "exchange"


def test_schema_validator_allows_extra_fields_when_not_strict() -> None:
    assert SchemaValidator(strict_fields=False).validate([_record(exchange="CME")]) == ()


def test_schema_validator_reports_row_index_from_source_index() -> None:
    fields = _trade_fields()
    del fields["side"]
    record = RawRecord(
        record_type=ImportRecordType.TRADE,
        source_index=7,
        provider_name="test",
        fields=fields,
    )

    issues = SchemaValidator().validate([record])

    assert issues[0].row_index == 7


# --------------------------------------------------------------------------
# TimestampValidator
# --------------------------------------------------------------------------


def test_timestamp_validator_accepts_utc() -> None:
    assert TimestampValidator().validate([_record()]) == ()


def test_timestamp_validator_reports_naive_datetime() -> None:
    issues = TimestampValidator().validate([_record(timestamp=datetime(2024, 1, 2, 12, 0, 0))])

    assert _codes(issues) == ["naive_datetime"]


def test_timestamp_validator_reports_non_utc_offset() -> None:
    stamped = datetime(2024, 1, 2, 12, 0, 0, tzinfo=timezone(timedelta(hours=1)))

    issues = TimestampValidator().validate([_record(timestamp=stamped)])

    assert _codes(issues) == ["non_utc_timestamp"]


def test_timestamp_validator_rejects_zero_offset_non_fixed_zone() -> None:
    # A zone whose offset merely happens to be zero today would reintroduce
    # daylight-saving ambiguity into event ordering.
    stamped = datetime(2024, 1, 2, 12, 0, 0, tzinfo=_ZeroOffsetZone())

    issues = TimestampValidator().validate([_record(timestamp=stamped)])

    assert _codes(issues) == ["non_utc_timestamp"]


def test_timestamp_validator_reports_non_datetime_value() -> None:
    # A distinct code from non_utc_timestamp: an unparsed string means the
    # source was never decoded, not that it carried the wrong zone.
    issues = TimestampValidator().validate([_record(timestamp="2024-01-02T12:00:00+00:00")])

    assert _codes(issues) == ["non_datetime_timestamp"]
    assert "datetime instance" in issues[0].message


def test_the_three_timestamp_failures_have_distinct_codes() -> None:
    not_datetime = TimestampValidator().validate([_record(timestamp="2024-01-02")])
    naive = TimestampValidator().validate([_record(timestamp=datetime(2024, 1, 2, 12))])
    non_utc = TimestampValidator().validate(
        [_record(timestamp=datetime(2024, 1, 2, 12, tzinfo=timezone(timedelta(hours=1))))]
    )

    codes = {
        _codes(not_datetime)[0],
        _codes(naive)[0],
        _codes(non_utc)[0],
    }

    assert codes == {"non_datetime_timestamp", "naive_datetime", "non_utc_timestamp"}


def test_timestamp_validator_defers_to_schema_when_field_absent() -> None:
    fields = _trade_fields()
    del fields["timestamp"]
    record = RawRecord(
        record_type=ImportRecordType.TRADE,
        source_index=0,
        provider_name="test",
        fields=fields,
    )

    assert TimestampValidator().validate([record]) == ()


# --------------------------------------------------------------------------
# DuplicateValidator
# --------------------------------------------------------------------------


def test_duplicate_validator_reports_repeated_identity() -> None:
    issues = DuplicateValidator().validate([_record(0), _record(1)])

    assert _codes(issues) == ["duplicate_row"]
    assert issues[0].row_index == 1
    assert issues[0].severity is ValidationSeverity.WARNING
    assert "source index 0" in issues[0].message


def test_duplicate_validator_accepts_records_differing_in_any_field() -> None:
    assert DuplicateValidator().validate([_record(0), _record(1, side="sell")]) == ()


def test_duplicate_validator_treats_same_timestamp_different_price_as_distinct() -> None:
    records = [_record(0), _record(1, price=Decimal("5000.50"))]

    assert DuplicateValidator().validate(records) == ()


def test_duplicate_validator_skips_incomplete_records() -> None:
    fields = _trade_fields()
    del fields["price"]
    incomplete = RawRecord(
        record_type=ImportRecordType.TRADE,
        source_index=0,
        provider_name="test",
        fields=fields,
    )

    assert DuplicateValidator().validate([incomplete, incomplete]) == ()


# --------------------------------------------------------------------------
# OrderingValidator
# --------------------------------------------------------------------------


def test_ordering_validator_accepts_ascending_timestamps() -> None:
    records = [
        _record(0, timestamp=BASE_TIME),
        _record(1, timestamp=BASE_TIME + timedelta(seconds=1)),
    ]

    assert OrderingValidator().validate(records) == ()


def test_ordering_validator_reports_descending_timestamp() -> None:
    records = [
        _record(0, timestamp=BASE_TIME + timedelta(seconds=1)),
        _record(1, timestamp=BASE_TIME),
    ]

    issues = OrderingValidator().validate(records)

    assert _codes(issues) == ["descending_timestamp"]
    assert issues[0].severity is ValidationSeverity.ERROR
    assert issues[0].row_index == 1


def test_equal_timestamps_are_informational_not_errors() -> None:
    # Several trades legitimately share one microsecond; source index provides
    # the deterministic tiebreak, so this is information rather than a defect.
    records = [_record(0), _record(1, side="sell")]

    issues = OrderingValidator().validate(records)

    assert _codes(issues) == ["ambiguous_timestamp_order"]
    assert issues[0].severity is ValidationSeverity.INFO


def test_ordering_validator_skips_records_without_valid_timestamps() -> None:
    records = [
        _record(0, timestamp=BASE_TIME),
        _record(1, timestamp="broken"),
        _record(2, timestamp=BASE_TIME + timedelta(seconds=1)),
    ]

    assert OrderingValidator().validate(records) == ()


# --------------------------------------------------------------------------
# ValidationPipeline
# --------------------------------------------------------------------------


def test_pipeline_reports_empty_batch() -> None:
    issues = default_validation_pipeline().validate([])

    assert _codes(issues) == ["empty_batch"]


def test_pipeline_accepts_a_clean_batch() -> None:
    records = [
        _record(0),
        _record(1, timestamp=BASE_TIME + timedelta(seconds=1)),
    ]

    assert default_validation_pipeline().validate(records) == ()


def test_pipeline_aggregates_issues_in_validator_order() -> None:
    # One record that is simultaneously unknown-field, naive, and out of order.
    records = [
        _record(0, timestamp=BASE_TIME + timedelta(seconds=5)),
        _record(1, timestamp=datetime(2024, 1, 2, 12, 0, 0), exchange="CME"),
        _record(2, timestamp=BASE_TIME),
    ]

    issues = default_validation_pipeline().validate(records)

    assert _codes(issues) == ["unknown_field", "naive_datetime", "descending_timestamp"]


def test_pipeline_runs_every_validator_without_short_circuiting() -> None:
    records = [_record(0, timestamp="broken", exchange="CME")]

    issues = default_validation_pipeline().validate(records)

    assert "unknown_field" in _codes(issues)
    assert "non_datetime_timestamp" in _codes(issues)


def test_pipeline_preserves_custom_validator_order() -> None:
    pipeline = ValidationPipeline((OrderingValidator(), SchemaValidator()))

    assert [validator.name for validator in pipeline.validators] == ["ordering", "schema"]


# --------------------------------------------------------------------------
# Normalization interface
# --------------------------------------------------------------------------


class _TradeNormalizer:
    """Minimal normalizer used to prove the protocol is implementable."""

    def normalize(self, record: RawRecord) -> Trade:
        return Trade(
            instrument_symbol=str(record.value("instrument_symbol")),
            timestamp=record.value("timestamp"),
            price=record.value("price"),
            size=record.value("size"),
            side=parse_trade_side(record.value("side")),
        )


def test_normalizer_satisfies_the_interface() -> None:
    assert isinstance(_TradeNormalizer(), RecordNormalizer)


def test_normalizer_produces_domain_objects() -> None:
    trade = _TradeNormalizer().normalize(_record())

    assert isinstance(trade, Trade)
    assert trade.price == Decimal("5000.25")
    assert trade.timestamp == BASE_TIME


def test_batch_adapter_preserves_record_order() -> None:
    records = [_record(0, side="buy"), _record(1, side="sell")]

    batch = raw_records_to_import_batch(records)

    assert batch.record_type is ImportRecordType.TRADE
    assert [row["side"] for row in batch.rows] == ["buy", "sell"]


def test_batch_adapter_rejects_empty_input() -> None:
    with pytest.raises(ValueError, match="zero records"):
        raw_records_to_import_batch([])


def test_batch_adapter_rejects_mixed_record_types() -> None:
    quote = RawRecord(
        record_type=ImportRecordType.QUOTE,
        source_index=1,
        provider_name="test",
        fields={},
    )

    with pytest.raises(ValueError, match="mixed record types"):
        raw_records_to_import_batch([_record(0), quote])


# --------------------------------------------------------------------------
# End-to-end: provider -> validation -> normalization stays compatible
# --------------------------------------------------------------------------


def test_csv_records_flow_through_validation_into_domain_objects(tmp_path: Path) -> None:
    path = tmp_path / "trades.csv"
    path.write_text(
        "timestamp,instrument_symbol,price,size,side\n"
        "2024-01-02T12:00:00+00:00,ES,5000.25,2,buy\n"
        "2024-01-02T12:00:01+00:00,ES,5000.50,1,sell\n",
        encoding="utf-8",
    )
    provider = CsvMarketDataProvider(path=path, record_type=ImportRecordType.TRADE)
    request = ProviderRequest(record_type=ImportRecordType.TRADE, instrument_symbol="ES")

    records = list(provider.fetch(request))
    issues = default_validation_pipeline().validate(records)
    accepted, report = validate_import_batch(raw_records_to_import_batch(records))

    trades = [record for record in accepted if isinstance(record, Trade)]

    assert issues == ()
    assert report.success is True
    assert len(trades) == len(accepted) == 2
    assert [trade.price for trade in trades] == [Decimal("5000.25"), Decimal("5000.50")]
