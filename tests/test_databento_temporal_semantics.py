"""Audit regression tests for Databento temporal semantics and scope.

These pin the decisions recorded in ADR-002 and ``docs/data-import.md``:
a completed bar can never be observed at its interval start, sub-microsecond
precision is never discarded silently, an instrument is never identified by a
vendor id, and an unattributed trade side is never guessed.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from quant_research_terminal.data_import import (
    KNOWN_TRADE_SIDES,
    DatabentoMarketDataProvider,
    DatabentoSchema,
    ImportRecordType,
    ProviderDecodeError,
    ProviderRequest,
    SubMicrosecondPolicy,
    bar_availability_timestamp,
    raw_records_to_import_batch,
    validate_import_batch,
)
from quant_research_terminal.data_import.providers.databento_decoding import (
    ONE_DAY,
    ONE_MINUTE,
    decode_nanosecond_timestamp,
)
from quant_research_terminal.data_import.providers.databento_provider import INPUT_FORMAT
from quant_research_terminal.domain.models import Bar, Trade

BASE_NS = 1_704_196_800_000_000_000
BASE_TIME = datetime(2024, 1, 2, 12, 0, 0, tzinfo=UTC)

TRADES_HEADER = "ts_recv,ts_event,instrument_id,action,side,price,size,sequence,symbol"
OHLCV_HEADER = "ts_event,instrument_id,open,high,low,close,volume,symbol"

OHLCV_VALUES = "4999500000000,5002000000000,4998750000000,5001250000000,12345"


def _write(path: Path, header: str, *rows: str) -> Path:
    path.write_text("\n".join((header, *rows)) + "\n", encoding="utf-8")
    return path


def _bars_file(tmp_path: Path, *rows: str) -> Path:
    return _write(tmp_path / "ohlcv.csv", OHLCV_HEADER, *rows)


def _bar_row(*, ts_event: int = BASE_NS, symbol: str = "ESH4") -> str:
    return f"{ts_event},42,{OHLCV_VALUES},{symbol}"


def _trades_file(tmp_path: Path, *rows: str) -> Path:
    return _write(tmp_path / "trades.csv", TRADES_HEADER, *rows)


def _trade_row(*, side: str = "B", instrument_id: int = 42, symbol: str = "ESH4") -> str:
    return f"{BASE_NS},{BASE_NS},{instrument_id},T,{side},5000250000000,2,1,{symbol}"


def _bar_provider(path: Path, interval: timedelta = ONE_MINUTE) -> DatabentoMarketDataProvider:
    return DatabentoMarketDataProvider(
        path=path, schema=DatabentoSchema.OHLCV, bar_interval=interval
    )


def _request(record_type: ImportRecordType, symbol: str = "ESH4") -> ProviderRequest:
    return ProviderRequest(record_type=record_type, instrument_symbol=symbol)


# --------------------------------------------------------------------------
# Scope precision
# --------------------------------------------------------------------------


def test_input_format_states_the_implemented_scope(tmp_path: Path) -> None:
    # The class name says "Databento"; only archived delimited exports are
    # actually decoded, and that limitation must be introspectable.
    provider = _bar_provider(_bars_file(tmp_path, _bar_row()))

    assert provider.input_format == INPUT_FORMAT == "archived-delimited-export"


def test_provider_needs_no_credentials(tmp_path: Path) -> None:
    # Acquisition needs credentials; decoding an archived file does not.
    provider = _bar_provider(_bars_file(tmp_path, _bar_row()))

    assert provider.capabilities.requires_credentials is False


# --------------------------------------------------------------------------
# Bar availability time (ADR-002)
# --------------------------------------------------------------------------


def test_completed_bar_is_not_available_at_interval_start(tmp_path: Path) -> None:
    # The core safety property. A bar covering 12:00:00-12:01:00 must not be
    # observable at 12:00:00, or a strategy could read its close a minute early.
    path = _bars_file(tmp_path, _bar_row(ts_event=BASE_NS))

    record = next(iter(_bar_provider(path, ONE_MINUTE).fetch(_request(ImportRecordType.BAR))))

    assert record.value("timestamp") != BASE_TIME
    assert record.value("timestamp") == BASE_TIME + ONE_MINUTE


def test_bar_availability_is_strictly_after_interval_start() -> None:
    for interval in (timedelta(seconds=1), ONE_MINUTE, timedelta(hours=4), ONE_DAY):
        assert bar_availability_timestamp(BASE_TIME, interval) > BASE_TIME


def test_bar_reaches_the_domain_stamped_at_interval_close(tmp_path: Path) -> None:
    path = _bars_file(tmp_path, _bar_row(ts_event=BASE_NS))

    records = list(_bar_provider(path, ONE_MINUTE).fetch(_request(ImportRecordType.BAR)))
    accepted, report = validate_import_batch(raw_records_to_import_batch(records))
    bars = [record for record in accepted if isinstance(record, Bar)]

    assert report.success is True
    assert bars[0].timestamp == BASE_TIME + ONE_MINUTE
    assert bars[0].close == Decimal("5001.25")


def test_original_vendor_timestamp_remains_recoverable(tmp_path: Path) -> None:
    # The shift is a reversible decode, not a mutation: subtracting the
    # interval returns exactly what the vendor supplied.
    provider = _bar_provider(_bars_file(tmp_path, _bar_row(ts_event=BASE_NS)), ONE_MINUTE)

    record = next(iter(provider.fetch(_request(ImportRecordType.BAR))))
    interval = provider.bar_interval
    assert interval is not None

    assert record.value("timestamp") - interval == BASE_TIME


def test_arbitrary_intervals_are_supported(tmp_path: Path) -> None:
    interval = timedelta(minutes=17, seconds=30)
    path = _bars_file(tmp_path, _bar_row(ts_event=BASE_NS))

    record = next(iter(_bar_provider(path, interval).fetch(_request(ImportRecordType.BAR))))

    assert record.value("timestamp") == BASE_TIME + interval


def test_daily_bar_uses_nominal_session_close(tmp_path: Path) -> None:
    # Without an exchange calendar the close is nominal. Erring later than the
    # true session close cannot create look-ahead; erring earlier could.
    path = _bars_file(tmp_path, _bar_row(ts_event=BASE_NS))

    record = next(iter(_bar_provider(path, ONE_DAY).fetch(_request(ImportRecordType.BAR))))

    assert record.value("timestamp") == BASE_TIME + ONE_DAY


def test_missing_interval_metadata_is_refused(tmp_path: Path) -> None:
    # No fallback is safe: an assumed interval would silently reintroduce bias.
    with pytest.raises(ValueError, match="requires an explicit bar_interval"):
        DatabentoMarketDataProvider(path=tmp_path / "ohlcv.csv", schema=DatabentoSchema.OHLCV)


def test_non_positive_interval_is_refused(tmp_path: Path) -> None:
    for interval in (timedelta(0), timedelta(seconds=-1)):
        with pytest.raises(ValueError, match="strictly positive"):
            DatabentoMarketDataProvider(
                path=tmp_path / "ohlcv.csv",
                schema=DatabentoSchema.OHLCV,
                bar_interval=interval,
            )


def test_interval_is_rejected_for_non_bar_schemas(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="does not apply"):
        DatabentoMarketDataProvider(
            path=tmp_path / "trades.csv",
            schema=DatabentoSchema.TRADES,
            bar_interval=ONE_MINUTE,
        )


def test_undecodable_bar_timestamp_is_not_shifted(tmp_path: Path) -> None:
    # A value that never became a datetime must reach validation untouched
    # rather than have an interval added to it.
    path = _bars_file(tmp_path, f"not-a-timestamp,42,{OHLCV_VALUES},ESH4")

    record = next(iter(_bar_provider(path, ONE_MINUTE).fetch(_request(ImportRecordType.BAR))))

    assert record.value("timestamp") == "not-a-timestamp"


def test_bar_ordering_follows_availability_not_interval_start(tmp_path: Path) -> None:
    # A daily bar opening before a minute bar still becomes available later,
    # and must therefore be replayed later.
    early_start_long_bar = bar_availability_timestamp(BASE_TIME, ONE_DAY)
    later_start_short_bar = bar_availability_timestamp(BASE_TIME + ONE_MINUTE, ONE_MINUTE)

    assert later_start_short_bar < early_start_long_bar


# --------------------------------------------------------------------------
# Nanosecond policy boundaries
# --------------------------------------------------------------------------


@pytest.mark.parametrize("remainder", [1, 499, 500, 999])
def test_any_sub_microsecond_remainder_is_rejected_by_default(remainder: int) -> None:
    raw = str(BASE_NS + remainder)

    assert decode_nanosecond_timestamp(raw, SubMicrosecondPolicy.REJECT) == raw


def test_exact_microsecond_boundary_decodes(tmp_path: Path) -> None:
    decoded = decode_nanosecond_timestamp(str(BASE_NS + 1_000), SubMicrosecondPolicy.REJECT)

    assert decoded == BASE_TIME + timedelta(microseconds=1)


@pytest.mark.parametrize(
    ("remainder", "expected_microseconds"),
    [(1, 0), (999, 0), (1_000, 1), (1_999, 1), (2_000, 2)],
)
def test_truncation_floors_toward_the_past(remainder: int, expected_microseconds: int) -> None:
    # Flooring never moves a record forward in time, which would be a
    # look-ahead shift however small.
    decoded = decode_nanosecond_timestamp(str(BASE_NS + remainder), SubMicrosecondPolicy.TRUNCATE)

    assert decoded == BASE_TIME + timedelta(microseconds=expected_microseconds)


def test_rejected_precision_surfaces_as_a_validation_issue(tmp_path: Path) -> None:
    # Operator-visible behaviour: the row is reported, not dropped in silence.
    path = _trades_file(tmp_path, f"{BASE_NS + 500},{BASE_NS},42,T,B,5000250000000,2,1,ESH4")

    records = list(
        DatabentoMarketDataProvider(path=path, schema=DatabentoSchema.TRADES).fetch(
            _request(ImportRecordType.TRADE)
        )
    )
    accepted, report = validate_import_batch(raw_records_to_import_batch(records))

    assert accepted == []
    assert "non_datetime_timestamp" in [issue.code.value for issue in report.issues]


# --------------------------------------------------------------------------
# Symbol resolution
# --------------------------------------------------------------------------


def _trade_provider(
    path: Path, *, symbol_by_instrument_id: Mapping[int, str] | None = None
) -> DatabentoMarketDataProvider:
    return DatabentoMarketDataProvider(
        path=path,
        schema=DatabentoSchema.TRADES,
        symbol_by_instrument_id=symbol_by_instrument_id,
    )


def test_embedded_symbol_resolves(tmp_path: Path) -> None:
    path = _trades_file(tmp_path, _trade_row(symbol="ESH4"))

    record = next(iter(_trade_provider(path).fetch(_request(ImportRecordType.TRADE))))

    assert record.value("instrument_symbol") == "ESH4"


def test_caller_mapping_resolves_when_export_has_no_symbol(tmp_path: Path) -> None:
    path = _trades_file(tmp_path, _trade_row(instrument_id=7, symbol=""))
    provider = _trade_provider(path, symbol_by_instrument_id={7: "ESH4"})

    record = next(iter(provider.fetch(_request(ImportRecordType.TRADE))))

    assert record.value("instrument_symbol") == "ESH4"


def test_missing_mapping_is_refused(tmp_path: Path) -> None:
    path = _trades_file(tmp_path, _trade_row(instrument_id=7, symbol=""))

    with pytest.raises(ProviderDecodeError, match="unmapped instrument_id 7"):
        list(_trade_provider(path).fetch(_request(ImportRecordType.TRADE)))


def test_conflicting_identity_is_refused(tmp_path: Path) -> None:
    # Two sources disagreeing about which instrument this is must stop the
    # read, not be quietly resolved in favour of either one.
    path = _trades_file(tmp_path, _trade_row(instrument_id=7, symbol="ESH4"))
    provider = _trade_provider(path, symbol_by_instrument_id={7: "NQH4"})

    with pytest.raises(ProviderDecodeError, match="conflicting instrument identity"):
        list(provider.fetch(_request(ImportRecordType.TRADE)))


def test_agreeing_sources_resolve_without_complaint(tmp_path: Path) -> None:
    path = _trades_file(tmp_path, _trade_row(instrument_id=7, symbol="ESH4"))
    provider = _trade_provider(path, symbol_by_instrument_id={7: "ESH4"})

    record = next(iter(provider.fetch(_request(ImportRecordType.TRADE))))

    assert record.value("instrument_symbol") == "ESH4"


def test_empty_symbol_and_no_instrument_id_is_refused(tmp_path: Path) -> None:
    path = _write(
        tmp_path / "trades.csv",
        TRADES_HEADER,
        f"{BASE_NS},{BASE_NS},,T,B,5000250000000,2,1,",
    )

    with pytest.raises(ProviderDecodeError, match="cannot be identified"):
        list(_trade_provider(path).fetch(_request(ImportRecordType.TRADE)))


def test_whitespace_only_mapped_symbol_is_refused_at_construction(tmp_path: Path) -> None:
    # "   " satisfies the domain's min_length check while carrying no identity
    # at all, so it must never reach a record.
    with pytest.raises(ValueError, match="is blank"):
        _trade_provider(tmp_path / "trades.csv", symbol_by_instrument_id={7: "   "})


def test_instrument_id_never_becomes_the_symbol(tmp_path: Path) -> None:
    path = _trades_file(tmp_path, _trade_row(instrument_id=12345, symbol=""))

    with pytest.raises(ProviderDecodeError):
        list(_trade_provider(path).fetch(_request(ImportRecordType.TRADE)))


# --------------------------------------------------------------------------
# Trade side
# --------------------------------------------------------------------------


def test_unattributed_side_is_never_mapped_to_a_direction(tmp_path: Path) -> None:
    path = _trades_file(tmp_path, _trade_row(side="N"))

    records = list(_trade_provider(path).fetch(_request(ImportRecordType.TRADE)))
    accepted, _ = validate_import_batch(raw_records_to_import_batch(records))
    trades = [record for record in accepted if isinstance(record, Trade)]

    assert trades[0].side not in KNOWN_TRADE_SIDES
    assert trades[0].side not in {"buy", "sell"}


def test_known_sides_are_the_only_decoded_directions() -> None:
    # The supported way for a consumer to detect an unattributed side, until
    # the domain contract can express it as a type.
    assert KNOWN_TRADE_SIDES == frozenset({"buy", "sell"})
