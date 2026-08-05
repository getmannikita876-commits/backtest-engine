from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from quant_research_terminal.data_import import (
    CsvMarketDataProvider,
    DatabentoMarketDataProvider,
    DatabentoSchema,
    ImportRecordType,
    MarketDataProvider,
    ProviderCapabilities,
    ProviderCapabilityError,
    ProviderDecodeError,
    ProviderError,
    ProviderNotConfiguredError,
    ProviderRequest,
    ThetaDataMarketDataProvider,
    ThetaDataSchema,
)

TRADE_HEADER = "timestamp,instrument_symbol,price,size,side"


def _write_csv(path: Path, *lines: str) -> Path:
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def _trade_csv(tmp_path: Path, *rows: str) -> Path:
    return _write_csv(tmp_path / "trades.csv", TRADE_HEADER, *rows)


def _trade_provider(path: Path) -> CsvMarketDataProvider:
    return CsvMarketDataProvider(path=path, record_type=ImportRecordType.TRADE)


def _trade_request(**overrides: object) -> ProviderRequest:
    values: dict[str, object] = {
        "record_type": ImportRecordType.TRADE,
        "instrument_symbol": "ES",
    }
    values.update(overrides)
    return ProviderRequest(**values)  # type: ignore[arg-type]


# --------------------------------------------------------------------------
# ProviderRequest
# --------------------------------------------------------------------------


def test_request_rejects_naive_bound() -> None:
    with pytest.raises(ValueError, match="timezone-aware UTC"):
        _trade_request(start=datetime(2024, 1, 2, 12, 0, 0))


def test_request_rejects_non_utc_bound() -> None:
    with pytest.raises(ValueError, match="fixed UTC offset"):
        _trade_request(start=datetime(2024, 1, 2, 12, 0, 0, tzinfo=timezone(timedelta(hours=1))))


def test_request_rejects_empty_symbol() -> None:
    with pytest.raises(ValueError, match="must not be empty"):
        _trade_request(instrument_symbol="")


def test_request_rejects_untrimmed_symbol() -> None:
    with pytest.raises(ValueError, match="surrounding whitespace"):
        _trade_request(instrument_symbol=" ES")


def test_request_rejects_end_at_or_before_start() -> None:
    moment = datetime(2024, 1, 2, 12, 0, 0, tzinfo=UTC)
    with pytest.raises(ValueError, match="strictly after"):
        _trade_request(start=moment, end=moment)


def test_request_window_is_half_open() -> None:
    start = datetime(2024, 1, 2, 12, 0, 0, tzinfo=UTC)
    end = datetime(2024, 1, 2, 12, 1, 0, tzinfo=UTC)
    request = _trade_request(start=start, end=end)

    assert request.contains(start) is True
    assert request.contains(end) is False
    assert request.contains(start - timedelta(microseconds=1)) is False


def test_unbounded_request_contains_everything() -> None:
    request = _trade_request()

    assert request.contains(datetime(1990, 1, 1, tzinfo=UTC)) is True
    assert request.contains(datetime(2090, 1, 1, tzinfo=UTC)) is True


def test_request_is_immutable() -> None:
    request = _trade_request()

    with pytest.raises(ValueError, match="frozen"):
        request.instrument_symbol = "NQ"


# --------------------------------------------------------------------------
# Capabilities
# --------------------------------------------------------------------------


def test_capabilities_report_supported_record_types() -> None:
    capabilities = ProviderCapabilities(
        provider_name="example",
        record_types=frozenset({ImportRecordType.TRADE}),
    )

    assert capabilities.supports(ImportRecordType.TRADE) is True
    assert capabilities.supports(ImportRecordType.QUOTE) is False


def test_every_provider_satisfies_the_interface(tmp_path: Path) -> None:
    providers: list[MarketDataProvider] = [
        _trade_provider(_trade_csv(tmp_path)),
        DatabentoMarketDataProvider(path=tmp_path / "databento.csv", schema=DatabentoSchema.TRADES),
        ThetaDataMarketDataProvider(
            path=tmp_path / "thetadata.csv",
            schema=ThetaDataSchema.TRADE,
            session_timezone=UTC,
        ),
    ]

    for provider in providers:
        assert isinstance(provider, MarketDataProvider)


# --------------------------------------------------------------------------
# CSV provider decoding
# --------------------------------------------------------------------------


def test_csv_decodes_utc_timestamp_and_decimal_price(tmp_path: Path) -> None:
    path = _trade_csv(tmp_path, "2024-01-02T12:00:00+00:00,ES,5000.25,2,buy")

    records = list(_trade_provider(path).fetch(_trade_request()))

    assert len(records) == 1
    record = records[0]
    assert record.value("timestamp") == datetime(2024, 1, 2, 12, 0, 0, tzinfo=UTC)
    assert record.value("price") == Decimal("5000.25")
    assert record.value("size") == Decimal("2")
    assert record.value("side") == "buy"
    assert record.provider_name == "csv"
    assert record.record_type is ImportRecordType.TRADE


def test_csv_price_never_passes_through_float(tmp_path: Path) -> None:
    # 0.1 is not exactly representable in binary floating point; decoding
    # straight from text must preserve the exact decimal the source recorded.
    path = _trade_csv(tmp_path, "2024-01-02T12:00:00+00:00,ES,0.1,1,buy")

    record = next(iter(_trade_provider(path).fetch(_trade_request())))

    price = record.value("price")
    assert isinstance(price, Decimal)
    assert price == Decimal("0.1")
    assert str(price) == "0.1"


def test_csv_preserves_file_order_and_source_index(tmp_path: Path) -> None:
    path = _trade_csv(
        tmp_path,
        "2024-01-02T12:00:00+00:00,ES,5000.25,2,buy",
        "2024-01-02T12:00:01+00:00,ES,5000.50,1,sell",
        "2024-01-02T12:00:02+00:00,ES,5000.75,3,buy",
    )

    records = list(_trade_provider(path).fetch(_trade_request()))

    assert [record.source_index for record in records] == [0, 1, 2]
    assert [record.value("side") for record in records] == ["buy", "sell", "buy"]


def test_csv_does_not_attach_utc_to_naive_timestamp(tmp_path: Path) -> None:
    path = _trade_csv(tmp_path, "2024-01-02T12:00:00,ES,5000.25,2,buy")

    record = next(iter(_trade_provider(path).fetch(_trade_request())))

    timestamp = record.value("timestamp")
    assert isinstance(timestamp, datetime)
    assert timestamp.tzinfo is None


def test_csv_preserves_non_utc_offset_without_converting(tmp_path: Path) -> None:
    path = _trade_csv(tmp_path, "2024-01-02T12:00:00+01:00,ES,5000.25,2,buy")

    record = next(iter(_trade_provider(path).fetch(_trade_request())))

    timestamp = record.value("timestamp")
    assert isinstance(timestamp, datetime)
    assert timestamp.utcoffset() == timedelta(hours=1)
    assert timestamp.hour == 12


def test_csv_keeps_unparseable_values_as_text(tmp_path: Path) -> None:
    path = _trade_csv(tmp_path, "not-a-timestamp,ES,not-a-price,2,buy")

    record = next(iter(_trade_provider(path).fetch(_trade_request())))

    assert record.value("timestamp") == "not-a-timestamp"
    assert record.value("price") == "not-a-price"


def test_csv_is_deterministic_across_repeated_reads(tmp_path: Path) -> None:
    path = _trade_csv(
        tmp_path,
        "2024-01-02T12:00:00+00:00,ES,5000.25,2,buy",
        "2024-01-02T12:00:01+00:00,ES,5000.50,1,sell",
    )
    provider = _trade_provider(path)

    first = list(provider.fetch(_trade_request()))
    second = list(provider.fetch(_trade_request()))

    assert first == second


# --------------------------------------------------------------------------
# CSV provider filtering
# --------------------------------------------------------------------------


def test_csv_filters_by_instrument(tmp_path: Path) -> None:
    path = _trade_csv(
        tmp_path,
        "2024-01-02T12:00:00+00:00,ES,5000.25,2,buy",
        "2024-01-02T12:00:01+00:00,NQ,17000.00,1,buy",
    )

    records = list(_trade_provider(path).fetch(_trade_request()))

    assert [record.value("instrument_symbol") for record in records] == ["ES"]


def test_csv_applies_half_open_time_window(tmp_path: Path) -> None:
    path = _trade_csv(
        tmp_path,
        "2024-01-02T12:00:00+00:00,ES,5000.25,2,buy",
        "2024-01-02T12:00:01+00:00,ES,5000.50,1,sell",
        "2024-01-02T12:00:02+00:00,ES,5000.75,3,buy",
    )
    request = _trade_request(
        start=datetime(2024, 1, 2, 12, 0, 1, tzinfo=UTC),
        end=datetime(2024, 1, 2, 12, 0, 2, tzinfo=UTC),
    )

    records = list(_trade_provider(path).fetch(request))

    assert len(records) == 1
    assert records[0].value("price") == Decimal("5000.50")


def test_filtered_records_keep_original_source_index(tmp_path: Path) -> None:
    path = _trade_csv(
        tmp_path,
        "2024-01-02T12:00:00+00:00,NQ,17000.00,1,buy",
        "2024-01-02T12:00:01+00:00,ES,5000.50,1,sell",
    )

    records = list(_trade_provider(path).fetch(_trade_request()))

    # Provenance points at the row's true position in the file, not at its
    # position in the filtered output.
    assert [record.source_index for record in records] == [1]


def test_window_filter_never_drops_an_undecodable_timestamp(tmp_path: Path) -> None:
    path = _trade_csv(tmp_path, "not-a-timestamp,ES,5000.25,2,buy")
    request = _trade_request(
        start=datetime(2024, 1, 2, 12, 0, 0, tzinfo=UTC),
        end=datetime(2024, 1, 2, 13, 0, 0, tzinfo=UTC),
    )

    records = list(_trade_provider(path).fetch(request))

    # The row cannot be placed on the timeline, so it must survive filtering
    # and be reported by validation rather than silently disappear.
    assert len(records) == 1


# --------------------------------------------------------------------------
# CSV provider failures
# --------------------------------------------------------------------------


def test_csv_rejects_unsupported_record_type(tmp_path: Path) -> None:
    path = _trade_csv(tmp_path)
    request = ProviderRequest(record_type=ImportRecordType.QUOTE, instrument_symbol="ES")

    with pytest.raises(ProviderCapabilityError, match="does not serve"):
        _trade_provider(path).fetch(request)


def test_capability_error_is_raised_eagerly(tmp_path: Path) -> None:
    # fetch() must fail at the call site, not on first iteration, so a caller
    # cannot hold a doomed iterator and discover the problem far from its cause.
    path = _trade_csv(tmp_path)
    request = ProviderRequest(record_type=ImportRecordType.BAR, instrument_symbol="ES")

    with pytest.raises(ProviderCapabilityError):
        _trade_provider(path).fetch(request)


def test_csv_rejects_row_with_wrong_column_count(tmp_path: Path) -> None:
    path = _trade_csv(tmp_path, "2024-01-02T12:00:00+00:00,ES,5000.25")

    with pytest.raises(ProviderDecodeError, match="columns"):
        list(_trade_provider(path).fetch(_trade_request()))


def test_csv_rejects_file_without_header(tmp_path: Path) -> None:
    path = tmp_path / "empty.csv"
    path.write_text("", encoding="utf-8")

    with pytest.raises(ProviderDecodeError, match="header"):
        list(_trade_provider(path).fetch(_trade_request()))


def test_header_only_file_yields_no_records(tmp_path: Path) -> None:
    path = _trade_csv(tmp_path)

    assert list(_trade_provider(path).fetch(_trade_request())) == []


# --------------------------------------------------------------------------
# Vendor capability declarations
# --------------------------------------------------------------------------


def test_databento_is_no_longer_a_stub(tmp_path: Path) -> None:
    # Phase 1.4 replaced the Databento stub with a real decoder. It reads
    # archived exports, so it needs no credentials of its own.
    capabilities = DatabentoMarketDataProvider(
        path=tmp_path / "databento.csv", schema=DatabentoSchema.TRADES
    ).capabilities

    assert capabilities.provider_name == "databento"
    assert capabilities.is_implemented is True
    assert capabilities.requires_credentials is False


def test_thetadata_is_no_longer_a_stub(tmp_path: Path) -> None:
    # Phase 1.6 replaced the ThetaData stub with a real archived-export
    # decoder, which likewise authenticates nothing.
    capabilities = ThetaDataMarketDataProvider(
        path=tmp_path / "thetadata.csv",
        schema=ThetaDataSchema.TRADE,
        session_timezone=UTC,
    ).capabilities

    assert capabilities.provider_name == "thetadata"
    assert capabilities.is_implemented is True
    assert capabilities.requires_credentials is False


def test_not_configured_error_remains_part_of_the_contract() -> None:
    # No provider is an interface-only stub any more, but the error stays in
    # the contract so a future vendor cannot signal "unimplemented" by quietly
    # returning no data.
    assert issubclass(ProviderNotConfiguredError, ProviderError)
