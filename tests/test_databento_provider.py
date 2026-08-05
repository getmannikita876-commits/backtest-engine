"""Regression and integration tests for the Databento provider.

The decoding tests exercise the vendor field semantics directly, with no file
involved. The integration tests drive real export fixtures through the full
import path — provider, validation, normalization — to prove the provider
composes with the frozen Phase 1.3 contracts.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from quant_research_terminal.data_import import (
    DatabentoMarketDataProvider,
    DatabentoSchema,
    DatabentoTimestampField,
    ImportRecordType,
    MarketDataProvider,
    ProviderDecodeError,
    ProviderRequest,
    RawRecord,
    SubMicrosecondPolicy,
    databento_record_type,
    default_validation_pipeline,
    raw_records_to_import_batch,
    validate_import_batch,
)
from quant_research_terminal.data_import.providers.databento_decoding import (
    ONE_MINUTE,
    UNDEF_MARKER,
    UNDEF_PRICE,
    UNDEF_TIMESTAMP,
    decode_fixed_point_price,
    decode_nanosecond_timestamp,
    decode_quantity,
    decode_trade_side,
)
from quant_research_terminal.domain.models import Bar, Quote, Trade, TradeSide

# 2024-01-02T12:00:00Z expressed in nanoseconds since the epoch.
BASE_NS = 1_704_196_800_000_000_000
BASE_TIME = datetime(2024, 1, 2, 12, 0, 0, tzinfo=UTC)

TRADES_HEADER = "ts_recv,ts_event,instrument_id,action,side,price,size,sequence,symbol"
MBP1_HEADER = (
    "ts_recv,ts_event,instrument_id,action,side,price,size,"
    "bid_px_00,ask_px_00,bid_sz_00,ask_sz_00,symbol"
)
OHLCV_HEADER = "ts_event,instrument_id,open,high,low,close,volume,symbol"


def _write(path: Path, header: str, *rows: str) -> Path:
    path.write_text("\n".join((header, *rows)) + "\n", encoding="utf-8")
    return path


def _trades_file(tmp_path: Path, *rows: str) -> Path:
    return _write(tmp_path / "trades.csv", TRADES_HEADER, *rows)


def _trade_row(
    *,
    ts_recv: int = BASE_NS,
    ts_event: int = BASE_NS - 1_000_000,
    instrument_id: int = 42,
    side: str = "B",
    price: int = 5_000_250_000_000,
    size: int = 2,
    symbol: str = "ESH4",
) -> str:
    return f"{ts_recv},{ts_event},{instrument_id},T,{side},{price},{size},1,{symbol}"


def _provider(
    path: Path,
    *,
    schema: DatabentoSchema = DatabentoSchema.TRADES,
    **kwargs: object,
) -> DatabentoMarketDataProvider:
    return DatabentoMarketDataProvider(path=path, schema=schema, **kwargs)  # type: ignore[arg-type]


def _request(
    record_type: ImportRecordType = ImportRecordType.TRADE,
    symbol: str = "ESH4",
    **kwargs: object,
) -> ProviderRequest:
    return ProviderRequest(record_type=record_type, instrument_symbol=symbol, **kwargs)  # type: ignore[arg-type]


# --------------------------------------------------------------------------
# Timestamp decoding
# --------------------------------------------------------------------------


def test_nanosecond_timestamp_decodes_to_utc() -> None:
    decoded = decode_nanosecond_timestamp(str(BASE_NS), SubMicrosecondPolicy.REJECT)

    assert decoded == BASE_TIME
    assert isinstance(decoded, datetime)
    assert decoded.tzinfo is UTC


def test_microsecond_detail_is_preserved_exactly() -> None:
    decoded = decode_nanosecond_timestamp(str(BASE_NS + 123_000), SubMicrosecondPolicy.REJECT)

    assert decoded == BASE_TIME + timedelta(microseconds=123)


def test_sub_microsecond_timestamp_is_rejected_by_default() -> None:
    # 500 ns cannot be represented by datetime or by the storage schema. The
    # raw vendor value passes through untouched so nothing is lost silently.
    raw = str(BASE_NS + 500)

    decoded = decode_nanosecond_timestamp(raw, SubMicrosecondPolicy.REJECT)

    assert decoded == raw
    assert not isinstance(decoded, datetime)


def test_sub_microsecond_timestamp_truncates_only_when_opted_in() -> None:
    decoded = decode_nanosecond_timestamp(str(BASE_NS + 1_500), SubMicrosecondPolicy.TRUNCATE)

    # Floors toward the past: 1500 ns -> 1 us, never rounding up into the future.
    assert decoded == BASE_TIME + timedelta(microseconds=1)


def test_undefined_timestamp_sentinel_never_becomes_a_time() -> None:
    decoded = decode_nanosecond_timestamp(str(UNDEF_TIMESTAMP), SubMicrosecondPolicy.TRUNCATE)

    assert decoded == UNDEF_MARKER


def test_unparseable_timestamp_passes_through() -> None:
    assert decode_nanosecond_timestamp("not-a-number", SubMicrosecondPolicy.TRUNCATE) == (
        "not-a-number"
    )


def test_negative_timestamp_passes_through() -> None:
    assert decode_nanosecond_timestamp("-1", SubMicrosecondPolicy.TRUNCATE) == "-1"


# --------------------------------------------------------------------------
# Price and quantity decoding
# --------------------------------------------------------------------------


def test_fixed_point_price_decodes_exactly() -> None:
    decoded = decode_fixed_point_price("5000250000000")

    assert isinstance(decoded, Decimal)
    assert decoded == Decimal("5000.25")


def test_price_decoding_never_uses_float() -> None:
    # 0.1 has no exact binary float representation; the decimal path must keep
    # it exact all the way through.
    decoded = decode_fixed_point_price("100000000")

    assert isinstance(decoded, Decimal)
    assert not isinstance(decoded, float)
    assert decoded == Decimal("0.1")


def test_full_nanosecond_price_precision_survives_decoding() -> None:
    decoded = decode_fixed_point_price("1")

    assert decoded == Decimal("0.000000001")


def test_undefined_price_sentinel_never_becomes_a_price() -> None:
    # INT64_MAX means "no price". Decoding it as a number would import
    # 9.22e18 as a real quote.
    decoded = decode_fixed_point_price(str(UNDEF_PRICE))

    assert decoded == UNDEF_MARKER
    assert not isinstance(decoded, Decimal)


def test_unparseable_price_passes_through() -> None:
    assert decode_fixed_point_price("") == ""


def test_quantity_decodes_to_exact_decimal() -> None:
    decoded = decode_quantity("2")

    assert isinstance(decoded, Decimal)
    assert decoded == Decimal("2")


def test_unparseable_quantity_passes_through() -> None:
    assert decode_quantity("abc") == "abc"


# --------------------------------------------------------------------------
# Side decoding
# --------------------------------------------------------------------------


@pytest.mark.parametrize(("code", "expected"), [("B", "buy"), ("A", "sell")])
def test_documented_aggressor_sides_are_mapped(code: str, expected: str) -> None:
    assert decode_trade_side(code) == expected


def test_unattributed_side_decodes_to_unknown() -> None:
    # 'N' means the vendor could not attribute a side. The domain records that
    # explicitly; it is never promoted to a direction.
    assert decode_trade_side("N") is TradeSide.UNKNOWN


def test_unrecognised_side_code_is_not_mapped_to_unknown() -> None:
    # Keeps "the vendor said it does not know" distinct from "we failed to
    # understand the vendor"; validation rejects the latter.
    assert decode_trade_side("X") == "X"


# --------------------------------------------------------------------------
# Symbology
# --------------------------------------------------------------------------


def test_symbol_column_is_used_when_present(tmp_path: Path) -> None:
    path = _trades_file(tmp_path, _trade_row(symbol="ESH4"))

    records = list(_provider(path).fetch(_request()))

    assert records[0].value("instrument_symbol") == "ESH4"


def test_instrument_id_is_resolved_through_the_supplied_mapping(tmp_path: Path) -> None:
    path = _trades_file(tmp_path, _trade_row(instrument_id=42, symbol=""))
    provider = _provider(path, symbol_by_instrument_id={42: "ESH4"})

    records = list(provider.fetch(_request()))

    assert records[0].value("instrument_symbol") == "ESH4"


def test_unmapped_instrument_id_fails_loudly(tmp_path: Path) -> None:
    # A numeric vendor id is not a symbol. Emitting it as one would corrupt
    # instrument identity, and nothing downstream validates symbol shape.
    path = _trades_file(tmp_path, _trade_row(instrument_id=99, symbol=""))

    with pytest.raises(ProviderDecodeError, match="unmapped instrument_id 99"):
        list(_provider(path).fetch(_request()))


def test_non_numeric_instrument_id_fails_loudly(tmp_path: Path) -> None:
    path = _write(
        tmp_path / "trades.csv",
        TRADES_HEADER,
        f"{BASE_NS},{BASE_NS},not-an-id,T,B,5000250000000,2,1,",
    )

    with pytest.raises(ProviderDecodeError, match="non-numeric instrument_id"):
        list(_provider(path).fetch(_request()))


# --------------------------------------------------------------------------
# Timestamp field selection and look-ahead bias
# --------------------------------------------------------------------------


def test_trades_default_to_receive_time(tmp_path: Path) -> None:
    # ts_event is earlier than ts_recv. Defaulting to ts_event would let a
    # strategy act on data before it could have been received.
    path = _trades_file(tmp_path, _trade_row(ts_recv=BASE_NS, ts_event=BASE_NS - 5_000_000))
    provider = _provider(path)

    assert provider.timestamp_field is DatabentoTimestampField.TS_RECV
    assert list(provider.fetch(_request()))[0].value("timestamp") == BASE_TIME


def test_event_time_can_be_selected_explicitly(tmp_path: Path) -> None:
    path = _trades_file(tmp_path, _trade_row(ts_recv=BASE_NS, ts_event=BASE_NS - 5_000_000))
    provider = _provider(path, timestamp_field=DatabentoTimestampField.TS_EVENT)

    records = list(provider.fetch(_request()))

    assert records[0].value("timestamp") == BASE_TIME - timedelta(milliseconds=5)


def test_bars_use_event_time_because_no_receive_time_exists(tmp_path: Path) -> None:
    path = _write(tmp_path / "ohlcv.csv", OHLCV_HEADER, "")
    provider = _provider(path, schema=DatabentoSchema.OHLCV, bar_interval=ONE_MINUTE)

    assert provider.timestamp_field is DatabentoTimestampField.TS_EVENT


# --------------------------------------------------------------------------
# Schema mapping
# --------------------------------------------------------------------------


def test_each_schema_maps_to_its_record_type() -> None:
    assert databento_record_type(DatabentoSchema.TRADES) is ImportRecordType.TRADE
    assert databento_record_type(DatabentoSchema.MBP_1) is ImportRecordType.QUOTE
    assert databento_record_type(DatabentoSchema.OHLCV) is ImportRecordType.BAR


def test_trades_decode_into_trade_records(tmp_path: Path) -> None:
    path = _trades_file(tmp_path, _trade_row(side="B", price=5_000_250_000_000, size=3))

    record = next(iter(_provider(path).fetch(_request())))

    assert record.record_type is ImportRecordType.TRADE
    assert record.provider_name == "databento"
    assert record.value("price") == Decimal("5000.25")
    assert record.value("size") == Decimal("3")
    assert record.value("side") == "buy"


def test_mbp1_decodes_top_of_book_into_quote_records(tmp_path: Path) -> None:
    path = _write(
        tmp_path / "mbp1.csv",
        MBP1_HEADER,
        f"{BASE_NS},{BASE_NS},42,A,B,5000250000000,1,5000000000000,5000250000000,4,7,ESH4",
    )

    record = next(
        iter(_provider(path, schema=DatabentoSchema.MBP_1).fetch(_request(ImportRecordType.QUOTE)))
    )

    assert record.record_type is ImportRecordType.QUOTE
    assert record.value("bid") == Decimal("5000.00")
    assert record.value("ask") == Decimal("5000.25")
    assert record.value("bid_size") == Decimal("4")
    assert record.value("ask_size") == Decimal("7")


def test_quote_ignores_the_event_price_columns(tmp_path: Path) -> None:
    # An mbp-1 row's own price/size describe the event that changed the book,
    # not the resulting quote.
    path = _write(
        tmp_path / "mbp1.csv",
        MBP1_HEADER,
        f"{BASE_NS},{BASE_NS},42,A,B,9999000000000,999,5000000000000,5000250000000,4,7,ESH4",
    )

    record = next(
        iter(_provider(path, schema=DatabentoSchema.MBP_1).fetch(_request(ImportRecordType.QUOTE)))
    )

    assert set(record.fields) == {
        "timestamp",
        "instrument_symbol",
        "bid",
        "ask",
        "bid_size",
        "ask_size",
    }
    assert record.value("bid") == Decimal("5000.00")


def test_ohlcv_decodes_into_bar_records(tmp_path: Path) -> None:
    path = _write(
        tmp_path / "ohlcv.csv",
        OHLCV_HEADER,
        f"{BASE_NS},42,4999500000000,5002000000000,4998750000000,5001250000000,12345,ESH4",
    )

    record = next(
        iter(
            _provider(path, schema=DatabentoSchema.OHLCV, bar_interval=ONE_MINUTE).fetch(
                _request(ImportRecordType.BAR)
            )
        )
    )

    assert record.record_type is ImportRecordType.BAR
    # Stamped at interval close, not interval start. See ADR-002.
    assert record.value("timestamp") == BASE_TIME + ONE_MINUTE
    assert record.value("open") == Decimal("4999.50")
    assert record.value("high") == Decimal("5002.00")
    assert record.value("low") == Decimal("4998.75")
    assert record.value("close") == Decimal("5001.25")
    assert record.value("volume") == Decimal("12345")


def test_decoded_fields_match_the_required_set_exactly(tmp_path: Path) -> None:
    # Strict schema validation rejects unknown fields, so the provider must
    # emit the required set and nothing else.
    path = _trades_file(tmp_path, _trade_row())

    record = next(iter(_provider(path).fetch(_request())))

    assert set(record.fields) == {"timestamp", "instrument_symbol", "price", "size", "side"}


# --------------------------------------------------------------------------
# Capability, filtering and streaming
# --------------------------------------------------------------------------


def test_schema_constrains_the_served_record_type(tmp_path: Path) -> None:
    path = _trades_file(tmp_path, _trade_row())

    with pytest.raises(Exception, match="does not serve"):
        _provider(path).fetch(_request(ImportRecordType.QUOTE))


def test_records_are_filtered_by_instrument(tmp_path: Path) -> None:
    path = _trades_file(
        tmp_path,
        _trade_row(symbol="ESH4"),
        _trade_row(symbol="NQH4"),
    )

    records = list(_provider(path).fetch(_request(symbol="ESH4")))

    assert [record.value("instrument_symbol") for record in records] == ["ESH4"]


def test_source_index_reflects_position_in_the_export(tmp_path: Path) -> None:
    path = _trades_file(
        tmp_path,
        _trade_row(symbol="NQH4"),
        _trade_row(symbol="ESH4"),
    )

    records = list(_provider(path).fetch(_request(symbol="ESH4")))

    assert [record.source_index for record in records] == [1]


def test_time_window_filter_is_half_open(tmp_path: Path) -> None:
    path = _trades_file(
        tmp_path,
        _trade_row(ts_recv=BASE_NS),
        _trade_row(ts_recv=BASE_NS + 1_000_000),
        _trade_row(ts_recv=BASE_NS + 2_000_000),
    )
    request = _request(
        start=BASE_TIME + timedelta(milliseconds=1),
        end=BASE_TIME + timedelta(milliseconds=2),
    )

    records = list(_provider(path).fetch(request))

    assert [record.source_index for record in records] == [1]


def test_undecodable_timestamp_survives_window_filtering(tmp_path: Path) -> None:
    # A row that cannot be placed on the timeline must reach validation rather
    # than vanish through a filter.
    path = _trades_file(tmp_path, _trade_row(ts_recv=BASE_NS + 500))
    request = _request(start=BASE_TIME, end=BASE_TIME + timedelta(hours=1))

    records = list(_provider(path).fetch(request))

    assert len(records) == 1


def test_provider_streams_are_closeable(tmp_path: Path) -> None:
    provider: MarketDataProvider = _provider(_trades_file(tmp_path, _trade_row()))

    with provider.fetch(_request()) as stream:
        assert isinstance(next(iter(stream)), RawRecord)

    assert stream.closed is True


def test_reading_is_deterministic(tmp_path: Path) -> None:
    path = _trades_file(tmp_path, _trade_row(), _trade_row(ts_recv=BASE_NS + 1_000_000))
    provider = _provider(path)

    assert list(provider.fetch(_request())) == list(provider.fetch(_request()))


def test_structurally_broken_export_is_rejected(tmp_path: Path) -> None:
    path = _write(tmp_path / "trades.csv", TRADES_HEADER, "1,2,3")

    with pytest.raises(ProviderDecodeError, match="columns"):
        list(_provider(path).fetch(_request()))


# --------------------------------------------------------------------------
# Integration: provider -> validation -> normalization
# --------------------------------------------------------------------------


def test_trades_flow_through_validation_into_domain_objects(tmp_path: Path) -> None:
    path = _trades_file(
        tmp_path,
        _trade_row(ts_recv=BASE_NS, side="B", price=5_000_250_000_000, size=2),
        _trade_row(ts_recv=BASE_NS + 1_000_000, side="A", price=5_000_500_000_000, size=1),
    )

    records = list(_provider(path).fetch(_request()))
    issues = default_validation_pipeline().validate(records)
    accepted, report = validate_import_batch(raw_records_to_import_batch(records))
    trades = [record for record in accepted if isinstance(record, Trade)]

    assert issues == ()
    assert report.success is True
    assert len(trades) == 2
    assert [trade.side for trade in trades] == ["buy", "sell"]
    assert [trade.price for trade in trades] == [Decimal("5000.25"), Decimal("5000.50")]
    assert trades[0].timestamp == BASE_TIME


def test_quotes_flow_through_validation_into_domain_objects(tmp_path: Path) -> None:
    path = _write(
        tmp_path / "mbp1.csv",
        MBP1_HEADER,
        f"{BASE_NS},{BASE_NS},42,A,B,5000250000000,1,5000000000000,5000250000000,4,7,ESH4",
    )
    provider = _provider(path, schema=DatabentoSchema.MBP_1)

    records = list(provider.fetch(_request(ImportRecordType.QUOTE)))
    accepted, report = validate_import_batch(raw_records_to_import_batch(records))
    quotes = [record for record in accepted if isinstance(record, Quote)]

    assert report.success is True
    assert len(quotes) == 1
    assert quotes[0].bid == Decimal("5000.00")
    assert quotes[0].ask == Decimal("5000.25")


def test_bars_flow_through_validation_into_domain_objects(tmp_path: Path) -> None:
    path = _write(
        tmp_path / "ohlcv.csv",
        OHLCV_HEADER,
        f"{BASE_NS},42,4999500000000,5002000000000,4998750000000,5001250000000,12345,ESH4",
    )
    provider = _provider(path, schema=DatabentoSchema.OHLCV, bar_interval=ONE_MINUTE)

    records = list(provider.fetch(_request(ImportRecordType.BAR)))
    accepted, report = validate_import_batch(raw_records_to_import_batch(records))
    bars = [record for record in accepted if isinstance(record, Bar)]

    assert report.success is True
    assert len(bars) == 1
    assert bars[0].volume == Decimal("12345")
    assert bars[0].timestamp == BASE_TIME + ONE_MINUTE


def test_sub_microsecond_row_is_rejected_by_validation_not_silently_altered(
    tmp_path: Path,
) -> None:
    path = _trades_file(tmp_path, _trade_row(ts_recv=BASE_NS + 500))

    records = list(_provider(path).fetch(_request()))
    accepted, report = validate_import_batch(raw_records_to_import_batch(records))

    assert accepted == []
    assert report.success is False
    assert "non_datetime_timestamp" in [issue.code.value for issue in report.issues]


def test_undefined_price_row_is_rejected_by_validation(tmp_path: Path) -> None:
    path = _trades_file(tmp_path, _trade_row(price=UNDEF_PRICE))

    records = list(_provider(path).fetch(_request()))
    accepted, report = validate_import_batch(raw_records_to_import_batch(records))

    assert accepted == []
    assert report.success is False
    assert "non_decimal_price" in [issue.code.value for issue in report.issues]


def test_unattributed_side_is_preserved_as_unknown(tmp_path: Path) -> None:
    # The domain can now record an unattributed aggressor, so the trade is
    # imported rather than discarded, and is never counted as a direction.
    path = _trades_file(tmp_path, _trade_row(side="N"))

    records = list(_provider(path).fetch(_request()))
    accepted, report = validate_import_batch(raw_records_to_import_batch(records))
    trades = [record for record in accepted if isinstance(record, Trade)]

    assert report.success is True
    assert [trade.side for trade in trades] == [TradeSide.UNKNOWN]
    assert trades[0].side.is_directional is False


def test_unrecognised_side_code_is_rejected_by_validation(tmp_path: Path) -> None:
    path = _trades_file(tmp_path, _trade_row(side="X"))

    records = list(_provider(path).fetch(_request()))
    accepted, report = validate_import_batch(raw_records_to_import_batch(records))

    assert accepted == []
    assert "invalid_trade_side" in [issue.code.value for issue in report.issues]
