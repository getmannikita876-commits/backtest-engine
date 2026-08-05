"""Regression and integration tests for the archived ThetaData provider.

The decoding tests exercise vendor field semantics directly, with no file
involved. The integration tests drive fixtures through the full import path —
provider, validation, normalization — to prove the provider composes with the
frozen contracts.

Timezone handling is tested against locally defined zones rather than
``zoneinfo``: the platform's zone database is an optional dependency that is
absent here, and a test that silently skips would leave the DST rules
unverified.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime, timedelta, tzinfo
from decimal import Decimal
from pathlib import Path

import pytest

from quant_research_terminal.data_import import (
    BarTimestampMeaning,
    ImportRecordType,
    MarketDataProvider,
    ProviderCapabilityError,
    ProviderDecodeError,
    ProviderRequest,
    ThetaDataMarketDataProvider,
    ThetaDataSchema,
    default_validation_pipeline,
    raw_records_to_import_batch,
    thetadata_record_type,
    validate_import_batch,
)
from quant_research_terminal.data_import.providers.thetadata_decoding import (
    AMBIGUOUS_LOCAL_TIME_MARKER,
    NONEXISTENT_LOCAL_TIME_MARKER,
    decode_decimal,
    decode_session_timestamp,
    decode_trade_side,
)
from quant_research_terminal.data_import.providers.thetadata_provider import INPUT_FORMAT
from quant_research_terminal.domain.models import Bar, Quote, Trade, TradeSide

EASTERN_STANDARD = timedelta(hours=-5)
EASTERN_DAYLIGHT = timedelta(hours=-4)

# 2024-01-02, 12:00:00 local (43_200_000 ms since local midnight).
NOON_MS = 43_200_000

TRADE_HEADER = "date,ms_of_day,price,size"
QUOTE_HEADER = "date,ms_of_day,bid,ask,bid_size,ask_size"
OHLC_HEADER = "date,ms_of_day,open,high,low,close,volume"


class _EasternLikeZone(tzinfo):
    """A US-Eastern-shaped zone with a real DST gap and a real ambiguity.

    Defined here rather than taken from ``zoneinfo`` so the transition rules
    under test are fixed and available without the platform zone database.
    """

    GAP_START = datetime(2024, 3, 10, 2, 0)
    GAP_END = datetime(2024, 3, 10, 3, 0)
    AMBIGUOUS_START = datetime(2024, 11, 3, 1, 0)
    AMBIGUOUS_END = datetime(2024, 11, 3, 2, 0)

    # The same two transitions expressed in UTC, which is what makes the gap a
    # real gap: local readings inside it map to a UTC instant that converts
    # back to a *different* local reading.
    SPRING_FORWARD_UTC = datetime(2024, 3, 10, 7, 0)
    FALL_BACK_UTC = datetime(2024, 11, 3, 6, 0)

    def utcoffset(self, dt: datetime | None) -> timedelta:
        if dt is None:
            return EASTERN_STANDARD
        naive = dt.replace(tzinfo=None)
        if self.AMBIGUOUS_START <= naive < self.AMBIGUOUS_END:
            # The hour repeats: the first pass is still daylight time.
            return EASTERN_DAYLIGHT if dt.fold == 0 else EASTERN_STANDARD
        if self.GAP_START <= naive < self.GAP_END:
            # These readings never occurred. Reporting standard time is what a
            # real zone does; the round-trip below is what exposes the gap.
            return EASTERN_STANDARD
        if self.GAP_END <= naive < self.AMBIGUOUS_START:
            return EASTERN_DAYLIGHT
        return EASTERN_STANDARD

    def fromutc(self, dt: datetime) -> datetime:
        # Driven by the UTC transition instants rather than derived from
        # utcoffset(), so the gap hour genuinely has no local representation.
        naive_utc = dt.replace(tzinfo=None)
        if self.SPRING_FORWARD_UTC <= naive_utc < self.FALL_BACK_UTC:
            return (dt + EASTERN_DAYLIGHT).replace(tzinfo=self)
        repeated = self.FALL_BACK_UTC <= naive_utc < self.FALL_BACK_UTC + timedelta(hours=1)
        return (dt + EASTERN_STANDARD).replace(tzinfo=self, fold=1 if repeated else 0)

    def dst(self, dt: datetime | None) -> timedelta:
        offset = self.utcoffset(dt)
        return timedelta(hours=1) if offset == EASTERN_DAYLIGHT else timedelta(0)

    def tzname(self, dt: datetime | None) -> str:
        return "EDT" if self.utcoffset(dt) == EASTERN_DAYLIGHT else "EST"


EASTERN = _EasternLikeZone()


def _write(path: Path, header: str, *rows: str) -> Path:
    path.write_text("\n".join((header, *rows)) + "\n", encoding="utf-8")
    return path


def _trades_file(tmp_path: Path, *rows: str) -> Path:
    return _write(tmp_path / "trades.csv", TRADE_HEADER, *rows)


def _provider(
    path: Path,
    *,
    schema: ThetaDataSchema = ThetaDataSchema.TRADE,
    session_timezone: tzinfo = UTC,
    instrument_symbol: str | None = "ES",
    side_by_vendor_code: Mapping[str, TradeSide] | None = None,
) -> ThetaDataMarketDataProvider:
    return ThetaDataMarketDataProvider(
        path=path,
        schema=schema,
        session_timezone=session_timezone,
        instrument_symbol=instrument_symbol,
        side_by_vendor_code=side_by_vendor_code,
    )


def _request(
    record_type: ImportRecordType = ImportRecordType.TRADE, symbol: str = "ES"
) -> ProviderRequest:
    return ProviderRequest(record_type=record_type, instrument_symbol=symbol)


# --------------------------------------------------------------------------
# Scope
# --------------------------------------------------------------------------


def test_input_format_states_the_implemented_scope(tmp_path: Path) -> None:
    provider = _provider(_trades_file(tmp_path))

    assert provider.input_format == INPUT_FORMAT == "archived-delimited-export"


def test_provider_needs_no_credentials(tmp_path: Path) -> None:
    assert _provider(_trades_file(tmp_path)).capabilities.requires_credentials is False


def test_each_schema_maps_to_its_record_type() -> None:
    assert thetadata_record_type(ThetaDataSchema.TRADE) is ImportRecordType.TRADE
    assert thetadata_record_type(ThetaDataSchema.QUOTE) is ImportRecordType.QUOTE
    assert thetadata_record_type(ThetaDataSchema.OHLC) is ImportRecordType.BAR


def test_schema_constrains_the_served_record_type(tmp_path: Path) -> None:
    with pytest.raises(ProviderCapabilityError, match="does not serve"):
        _provider(_trades_file(tmp_path)).fetch(_request(ImportRecordType.QUOTE))


# --------------------------------------------------------------------------
# Temporal semantics
# --------------------------------------------------------------------------


def test_date_and_ms_of_day_combine_into_a_utc_instant() -> None:
    decoded = decode_session_timestamp("20240102", str(NOON_MS), UTC)

    assert decoded == datetime(2024, 1, 2, 12, 0, 0, tzinfo=UTC)


def test_local_wall_clock_is_converted_using_the_supplied_zone() -> None:
    # Noon Eastern in January is 17:00 UTC. The provider never assumes the
    # zone; the caller declares it.
    decoded = decode_session_timestamp("20240102", str(NOON_MS), EASTERN)

    assert decoded == datetime(2024, 1, 2, 17, 0, 0, tzinfo=UTC)


def test_daylight_saving_offset_is_honoured() -> None:
    # Noon Eastern in July is 16:00 UTC, an hour earlier than in January.
    decoded = decode_session_timestamp("20240701", str(NOON_MS), EASTERN)

    assert decoded == datetime(2024, 7, 1, 16, 0, 0, tzinfo=UTC)


def test_millisecond_precision_is_preserved() -> None:
    decoded = decode_session_timestamp("20240102", str(NOON_MS + 123), UTC)

    assert decoded == datetime(2024, 1, 2, 12, 0, 0, 123_000, tzinfo=UTC)


def test_ambiguous_local_time_is_rejected() -> None:
    # 01:30 on the fall-back date occurs twice and the row says which.
    ambiguous_ms = 90 * 60 * 1_000

    decoded = decode_session_timestamp("20241103", str(ambiguous_ms), EASTERN)

    assert decoded == AMBIGUOUS_LOCAL_TIME_MARKER


def test_nonexistent_local_time_is_rejected() -> None:
    # 02:30 on the spring-forward date never occurred.
    gap_ms = 150 * 60 * 1_000

    decoded = decode_session_timestamp("20240310", str(gap_ms), EASTERN)

    assert decoded == NONEXISTENT_LOCAL_TIME_MARKER


def test_unresolvable_local_time_is_rejected_by_validation(tmp_path: Path) -> None:
    ambiguous_ms = 90 * 60 * 1_000
    path = _trades_file(tmp_path, f"20241103,{ambiguous_ms},5000.25,2")

    records = list(_provider(path, session_timezone=EASTERN).fetch(_request()))
    accepted, report = validate_import_batch(raw_records_to_import_batch(records))

    assert accepted == []
    assert report.success is False
    assert "non_datetime_timestamp" in [issue.code.value for issue in report.issues]


@pytest.mark.parametrize("ms_of_day", ["-1", "86400000", "not-a-number"])
def test_out_of_range_or_unparseable_time_passes_through(ms_of_day: str) -> None:
    # A millisecond offset outside the day would silently roll the date.
    decoded = decode_session_timestamp("20240102", ms_of_day, UTC)

    assert decoded == "20240102"


def test_invalid_calendar_date_passes_through() -> None:
    assert decode_session_timestamp("20240230", str(NOON_MS), UTC) == "20240230"


# --------------------------------------------------------------------------
# Numeric semantics
# --------------------------------------------------------------------------


def test_prices_decode_to_exact_decimals() -> None:
    decoded = decode_decimal("5000.25")

    assert isinstance(decoded, Decimal)
    assert decoded == Decimal("5000.25")


def test_price_decoding_never_uses_float() -> None:
    decoded = decode_decimal("0.1")

    assert isinstance(decoded, Decimal)
    assert not isinstance(decoded, float)
    assert decoded == Decimal("0.1")
    assert str(decoded) == "0.1"


def test_unparseable_numeric_passes_through() -> None:
    assert decode_decimal("not-a-price") == "not-a-price"


def test_zero_price_sentinel_is_rejected_cleanly(tmp_path: Path) -> None:
    # ThetaData writes 0 where it has no value. The domain requires strictly
    # positive prices, so it is rejected with a diagnosable issue rather than
    # imported as a real zero-priced trade.
    path = _trades_file(tmp_path, f"20240102,{NOON_MS},0,2")

    records = list(_provider(path).fetch(_request()))
    accepted, report = validate_import_batch(raw_records_to_import_batch(records))

    assert accepted == []
    assert "non_decimal_price" in [issue.code.value for issue in report.issues]


# --------------------------------------------------------------------------
# Trade side
# --------------------------------------------------------------------------


def test_trades_are_unknown_side_without_a_mapping() -> None:
    # The archived trade schema publishes no aggressor, so UNKNOWN is an
    # accurate record of what the vendor supplied rather than a guess.
    assert decode_trade_side(None, None) is TradeSide.UNKNOWN
    assert decode_trade_side("anything", None) is TradeSide.UNKNOWN


def test_supplied_mapping_is_applied() -> None:
    mapping = {"B": TradeSide.BUY, "S": TradeSide.SELL}

    assert decode_trade_side("B", mapping) is TradeSide.BUY
    assert decode_trade_side("s", mapping) is TradeSide.SELL


def test_code_outside_a_supplied_mapping_stays_distinguishable() -> None:
    # Not folded into UNKNOWN: "the source does not publish a side" and "we did
    # not understand the code it published" are different failures.
    assert decode_trade_side("Z", {"B": TradeSide.BUY}) == "Z"


def test_unmapped_side_code_is_rejected_by_validation(tmp_path: Path) -> None:
    path = _write(
        tmp_path / "trades.csv",
        "date,ms_of_day,price,size,side",
        f"20240102,{NOON_MS},5000.25,2,Z",
    )
    provider = _provider(path, side_by_vendor_code={"B": TradeSide.BUY})

    records = list(provider.fetch(_request()))
    accepted, report = validate_import_batch(raw_records_to_import_batch(records))

    assert accepted == []
    assert "invalid_trade_side" in [issue.code.value for issue in report.issues]


# --------------------------------------------------------------------------
# Instrument identity
# --------------------------------------------------------------------------


def test_configured_symbol_resolves(tmp_path: Path) -> None:
    path = _trades_file(tmp_path, f"20240102,{NOON_MS},5000.25,2")

    record = next(iter(_provider(path, instrument_symbol="ES").fetch(_request())))

    assert record.value("instrument_symbol") == "ES"


def test_embedded_symbol_column_resolves(tmp_path: Path) -> None:
    path = _write(
        tmp_path / "trades.csv",
        "date,ms_of_day,price,size,symbol",
        f"20240102,{NOON_MS},5000.25,2,ES",
    )

    record = next(iter(_provider(path, instrument_symbol=None).fetch(_request())))

    assert record.value("instrument_symbol") == "ES"


def test_conflicting_symbol_sources_are_refused(tmp_path: Path) -> None:
    path = _write(
        tmp_path / "trades.csv",
        "date,ms_of_day,price,size,symbol",
        f"20240102,{NOON_MS},5000.25,2,NQ",
    )

    with pytest.raises(ProviderDecodeError, match="conflicting instrument identity"):
        list(_provider(path, instrument_symbol="ES").fetch(_request()))


def test_agreeing_symbol_sources_resolve(tmp_path: Path) -> None:
    path = _write(
        tmp_path / "trades.csv",
        "date,ms_of_day,price,size,symbol",
        f"20240102,{NOON_MS},5000.25,2,ES",
    )

    record = next(iter(_provider(path, instrument_symbol="ES").fetch(_request())))

    assert record.value("instrument_symbol") == "ES"


def test_missing_symbol_is_refused(tmp_path: Path) -> None:
    path = _trades_file(tmp_path, f"20240102,{NOON_MS},5000.25,2")

    with pytest.raises(ProviderDecodeError, match="cannot be identified"):
        list(_provider(path, instrument_symbol=None).fetch(_request()))


def test_empty_embedded_symbol_falls_back_to_the_configured_one(tmp_path: Path) -> None:
    path = _write(
        tmp_path / "trades.csv",
        "date,ms_of_day,price,size,symbol",
        f"20240102,{NOON_MS},5000.25,2,",
    )

    record = next(iter(_provider(path, instrument_symbol="ES").fetch(_request())))

    assert record.value("instrument_symbol") == "ES"


def test_whitespace_only_embedded_symbol_is_not_an_identity(tmp_path: Path) -> None:
    path = _write(
        tmp_path / "trades.csv",
        "date,ms_of_day,price,size,symbol",
        f"20240102,{NOON_MS},5000.25,2,   ",
    )

    with pytest.raises(ProviderDecodeError, match="cannot be identified"):
        list(_provider(path, instrument_symbol=None).fetch(_request()))


def test_whitespace_only_configured_symbol_is_refused_at_construction(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="is blank"):
        _provider(tmp_path / "trades.csv", instrument_symbol="   ")


# --------------------------------------------------------------------------
# Bars
# --------------------------------------------------------------------------


def _ohlc_file(tmp_path: Path, *rows: str) -> Path:
    return _write(tmp_path / "ohlc.csv", OHLC_HEADER, *rows)


def _ohlc_row(ms_of_day: int = NOON_MS) -> str:
    return f"20240102,{ms_of_day},4999.50,5002.00,4998.75,5001.25,12345"


def _bar_provider(
    path: Path, meaning: BarTimestampMeaning, interval: timedelta = timedelta(minutes=1)
) -> ThetaDataMarketDataProvider:
    return ThetaDataMarketDataProvider(
        path=path,
        schema=ThetaDataSchema.OHLC,
        session_timezone=UTC,
        instrument_symbol="ES",
        bar_interval=interval,
        bar_timestamp_meaning=meaning,
    )


def test_interval_start_timestamps_become_available_at_interval_close(tmp_path: Path) -> None:
    path = _ohlc_file(tmp_path, _ohlc_row())

    record = next(
        iter(
            _bar_provider(path, BarTimestampMeaning.INTERVAL_START).fetch(
                _request(ImportRecordType.BAR)
            )
        )
    )

    noon = datetime(2024, 1, 2, 12, 0, 0, tzinfo=UTC)
    assert record.value("timestamp") == noon + timedelta(minutes=1)
    assert record.value("timestamp") != noon


def test_interval_end_timestamps_are_already_availability(tmp_path: Path) -> None:
    path = _ohlc_file(tmp_path, _ohlc_row())

    record = next(
        iter(
            _bar_provider(path, BarTimestampMeaning.INTERVAL_END).fetch(
                _request(ImportRecordType.BAR)
            )
        )
    )

    assert record.value("timestamp") == datetime(2024, 1, 2, 12, 0, 0, tzinfo=UTC)


def test_bar_interval_travels_with_the_record(tmp_path: Path) -> None:
    path = _ohlc_file(tmp_path, _ohlc_row())

    record = next(
        iter(
            _bar_provider(path, BarTimestampMeaning.INTERVAL_START).fetch(
                _request(ImportRecordType.BAR)
            )
        )
    )

    assert record.value("interval") == timedelta(minutes=1)


def test_missing_bar_interval_is_refused(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="requires an explicit bar_interval"):
        ThetaDataMarketDataProvider(
            path=tmp_path / "ohlc.csv",
            schema=ThetaDataSchema.OHLC,
            session_timezone=UTC,
            bar_timestamp_meaning=BarTimestampMeaning.INTERVAL_START,
        )


def test_missing_bar_timestamp_meaning_is_refused(tmp_path: Path) -> None:
    # Reading an interval-end stamp as a start would shift every bar by one
    # interval, so there is no default.
    with pytest.raises(ValueError, match="requires an explicit bar_timestamp_meaning"):
        ThetaDataMarketDataProvider(
            path=tmp_path / "ohlc.csv",
            schema=ThetaDataSchema.OHLC,
            session_timezone=UTC,
            bar_interval=timedelta(minutes=1),
        )


@pytest.mark.parametrize("interval", [timedelta(0), timedelta(seconds=-1)])
def test_non_positive_bar_interval_is_refused(tmp_path: Path, interval: timedelta) -> None:
    with pytest.raises(ValueError, match="strictly positive"):
        ThetaDataMarketDataProvider(
            path=tmp_path / "ohlc.csv",
            schema=ThetaDataSchema.OHLC,
            session_timezone=UTC,
            bar_interval=interval,
            bar_timestamp_meaning=BarTimestampMeaning.INTERVAL_START,
        )


def test_bar_settings_are_refused_for_non_bar_schemas(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="do not apply"):
        ThetaDataMarketDataProvider(
            path=tmp_path / "trades.csv",
            schema=ThetaDataSchema.TRADE,
            session_timezone=UTC,
            bar_interval=timedelta(minutes=1),
        )


def test_undecodable_bar_timestamp_is_not_shifted(tmp_path: Path) -> None:
    path = _ohlc_file(tmp_path, "20240102,not-a-time,4999.50,5002.00,4998.75,5001.25,12345")

    record = next(
        iter(
            _bar_provider(path, BarTimestampMeaning.INTERVAL_START).fetch(
                _request(ImportRecordType.BAR)
            )
        )
    )

    assert record.value("timestamp") == "20240102"


# --------------------------------------------------------------------------
# Streaming and resource ownership
# --------------------------------------------------------------------------


def test_reading_is_deterministic(tmp_path: Path) -> None:
    path = _trades_file(
        tmp_path,
        f"20240102,{NOON_MS},5000.25,2",
        f"20240102,{NOON_MS + 1000},5000.50,1",
    )
    provider = _provider(path)

    assert list(provider.fetch(_request())) == list(provider.fetch(_request()))


def test_source_index_reflects_position_in_the_export(tmp_path: Path) -> None:
    path = _trades_file(
        tmp_path,
        f"20240102,{NOON_MS},5000.25,2",
        f"20240102,{NOON_MS + 1000},5000.50,1",
    )

    records = list(_provider(path).fetch(_request()))

    assert [record.source_index for record in records] == [0, 1]


def test_stream_closes_on_decode_failure(tmp_path: Path) -> None:
    path = _trades_file(tmp_path, "20240102")

    stream = _provider(path).fetch(_request())
    with pytest.raises(ProviderDecodeError, match="columns"):
        list(stream)

    assert stream.closed is True


def test_stream_closes_when_the_consumer_raises(tmp_path: Path) -> None:
    path = _trades_file(tmp_path, f"20240102,{NOON_MS},5000.25,2")

    class _ConsumerError(RuntimeError):
        pass

    provider: MarketDataProvider = _provider(path)
    with pytest.raises(_ConsumerError):
        with provider.fetch(_request()) as stream:
            for _record in stream:
                raise _ConsumerError

    assert stream.closed is True


def test_structurally_broken_export_is_rejected(tmp_path: Path) -> None:
    path = _write(tmp_path / "trades.csv", TRADE_HEADER, "1,2")

    with pytest.raises(ProviderDecodeError, match="columns"):
        list(_provider(path).fetch(_request()))


# --------------------------------------------------------------------------
# Integration: provider -> validation -> normalization
# --------------------------------------------------------------------------


def test_trades_flow_through_validation_into_domain_objects(tmp_path: Path) -> None:
    path = _trades_file(
        tmp_path,
        f"20240102,{NOON_MS},5000.25,2",
        f"20240102,{NOON_MS + 1000},5000.50,1",
    )

    records = list(_provider(path, session_timezone=EASTERN).fetch(_request()))
    issues = default_validation_pipeline().validate(records)
    accepted, report = validate_import_batch(raw_records_to_import_batch(records))
    trades = [record for record in accepted if isinstance(record, Trade)]

    assert issues == ()
    assert report.success is True
    assert len(trades) == 2
    assert trades[0].timestamp == datetime(2024, 1, 2, 17, 0, 0, tzinfo=UTC)
    assert [trade.price for trade in trades] == [Decimal("5000.25"), Decimal("5000.50")]
    assert all(trade.side is TradeSide.UNKNOWN for trade in trades)


def test_quotes_flow_through_validation_into_domain_objects(tmp_path: Path) -> None:
    path = _write(
        tmp_path / "quotes.csv",
        QUOTE_HEADER,
        f"20240102,{NOON_MS},5000.00,5000.25,4,7",
    )

    records = list(
        _provider(path, schema=ThetaDataSchema.QUOTE).fetch(_request(ImportRecordType.QUOTE))
    )
    accepted, report = validate_import_batch(raw_records_to_import_batch(records))
    quotes = [record for record in accepted if isinstance(record, Quote)]

    assert report.success is True
    assert quotes[0].bid == Decimal("5000.00")
    assert quotes[0].ask == Decimal("5000.25")
    assert quotes[0].bid_size == Decimal("4")


def test_bars_flow_through_validation_into_domain_objects(tmp_path: Path) -> None:
    path = _ohlc_file(tmp_path, _ohlc_row())

    records = list(
        _bar_provider(path, BarTimestampMeaning.INTERVAL_START).fetch(
            _request(ImportRecordType.BAR)
        )
    )
    accepted, report = validate_import_batch(raw_records_to_import_batch(records))
    bars = [record for record in accepted if isinstance(record, Bar)]

    noon = datetime(2024, 1, 2, 12, 0, 0, tzinfo=UTC)
    assert report.success is True
    assert bars[0].interval_start == noon
    assert bars[0].interval == timedelta(minutes=1)
    assert bars[0].availability_time == noon + timedelta(minutes=1)
    assert bars[0].close == Decimal("5001.25")
