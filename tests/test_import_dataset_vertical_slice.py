"""True integration tests for the first vertical slice (Phase 1.9, ADR-007).

Everything here is real: a real ``CsvMarketDataProvider`` reading a real file,
the real validation pipeline, the real normalizer, the real deterministic
ordering, the real Parquet writer and reader, and a real temporary output
directory. No mocks, no monkeypatching, no in-memory storage substitutes.
Until this phase, every one of those components was verified only in
isolation; these tests are the first proof that they compose.

The committed fixture ``tests/fixtures/esm6_trades.csv`` uses the vendor symbol
**ESM6**. It is a plain string, and it is *only* a string: it is not canonical
instrument identity, and it does **not** establish a contract year. ``M6``
abbreviates the delivery year to one digit, so the same symbol denotes June
2006, 2016, 2026, or 2036, and the fixture's own rows are dated March 2024 —
consistent with a 2026 listing and equally consistent with a re-used symbol.
Nothing in this file may be read as evidence that ``ESM6`` means 2026; ADR-009
requires the full year to be stated explicitly rather than inferred, and no
code in the repository resolves the abbreviation without an explicit decade.

Canonical identity lives in
:class:`~quant_research_terminal.domain.futures_contract.FuturesContractId`.
Schema v2 does not persist it — see
:func:`test_storage_v2_persists_only_the_legacy_vendor_symbol` below, which
asserts that limit rather than leaving it to prose. The
fixture's rows are deliberately out of timestamp order, contain a
same-timestamp cluster with two *identical* trades, a trailing-zero price,
and all three trade sides, so a single pass exercises ordering, tiebreaks,
ADR-003 trade preservation, the numeric envelope, and side reconstruction.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from quant_research_terminal.application import (
    ImportDatasetError,
    ImportDatasetResult,
    ImportDatasetUseCase,
    VerificationError,
)
from quant_research_terminal.application.ports import (
    MarketDataProvider,
    ProviderRequest,
    RecordStream,
)
from quant_research_terminal.data_import.contracts import ImportRecordType
from quant_research_terminal.data_import.providers.csv_provider import CsvMarketDataProvider
from quant_research_terminal.data_import.providers.provider import (
    ProviderCapabilities,
    ProviderDecodeError,
)
from quant_research_terminal.data_import.raw_record import RawRecord
from quant_research_terminal.domain.models import Trade, TradeSide

FIXTURE_DIR = Path(__file__).resolve().parent / "fixtures"
TRADES_FIXTURE = FIXTURE_DIR / "esm6_trades.csv"

SYMBOL = "ESM6"
BASE = datetime(2024, 3, 4, 14, 30, 0, tzinfo=UTC)

QUOTE_HEADER = "timestamp,instrument_symbol,bid,ask,bid_size,ask_size"
BAR_HEADER = "timestamp,instrument_symbol,interval,open,high,low,close,volume"


def _use_case() -> ImportDatasetUseCase:
    return ImportDatasetUseCase()


def _trade_provider(path: Path = TRADES_FIXTURE) -> CsvMarketDataProvider:
    return CsvMarketDataProvider(path=path, record_type=ImportRecordType.TRADE)


def _request(record_type: ImportRecordType = ImportRecordType.TRADE) -> ProviderRequest:
    return ProviderRequest(record_type=record_type, instrument_symbol=SYMBOL)


def _write_csv(path: Path, header: str, *rows: str) -> Path:
    path.write_text("\n".join([header, *rows]) + "\n", encoding="utf-8")
    return path


def _run_trades(tmp_path: Path, name: str = "esm6.parquet") -> tuple[ImportDatasetResult, Path]:
    output = tmp_path / name
    result = _use_case().run(provider=_trade_provider(), request=_request(), output_path=output)
    return result, output


#: The fixture's six trades in the deterministic order the slice must emit:
#: sorted by timestamp, ties broken by source position (file order).
EXPECTED_TRADES = [
    Trade(
        timestamp=BASE - timedelta(microseconds=1),
        instrument_symbol=SYMBOL,
        price=Decimal("5102.00"),
        size=Decimal("4"),
        side=TradeSide.SELL,
    ),
    Trade(
        timestamp=BASE + timedelta(microseconds=100),
        instrument_symbol=SYMBOL,
        price=Decimal("5102.25"),
        size=Decimal("2"),
        side=TradeSide.BUY,
    ),
    Trade(
        timestamp=BASE + timedelta(microseconds=100),
        instrument_symbol=SYMBOL,
        price=Decimal("5102.25"),
        size=Decimal("1"),
        side=TradeSide.BUY,
    ),
    Trade(
        timestamp=BASE + timedelta(microseconds=100),
        instrument_symbol=SYMBOL,
        price=Decimal("5102.25"),
        size=Decimal("1"),
        side=TradeSide.BUY,
    ),
    Trade(
        timestamp=BASE + timedelta(microseconds=250),
        instrument_symbol=SYMBOL,
        price=Decimal("5102.50"),
        size=Decimal("3"),
        side=TradeSide.SELL,
    ),
    Trade(
        timestamp=BASE + timedelta(seconds=1, microseconds=500_000),
        instrument_symbol=SYMBOL,
        price=Decimal("5102.750000"),
        size=Decimal("5"),
        side=TradeSide.UNKNOWN,
    ),
]


# ==========================================================================
# 1. Trades: the full provider -> Parquet -> domain path
# ==========================================================================


def test_trades_flow_end_to_end_with_exact_values(tmp_path: Path) -> None:
    result, output = _run_trades(tmp_path)

    assert output.is_file()
    assert result.success is True
    assert result.record_type is ImportRecordType.TRADE
    assert result.output_path == output
    assert result.total_rows == 6
    assert result.accepted_rows == 6
    assert result.rejected_rows == 0
    assert result.error_count == 0
    assert result.records_written == 6
    assert result.records_verified == 6
    assert result.validation_report.success is True

    from quant_research_terminal.data import read_trades

    restored = read_trades(output)
    assert list(restored) == EXPECTED_TRADES


def test_trades_are_deterministically_ordered_not_file_ordered(tmp_path: Path) -> None:
    # The fixture's first data row is NOT the earliest trade; a slice that
    # preserved file order would fail here.
    _, output = _run_trades(tmp_path)
    from quant_research_terminal.data import read_trades

    stamps = [trade.timestamp for trade in read_trades(output)]
    assert stamps == sorted(stamps)
    assert stamps[0] == BASE - timedelta(microseconds=1)


def test_trade_fields_survive_exactly(tmp_path: Path) -> None:
    _, output = _run_trades(tmp_path)
    from quant_research_terminal.data import read_trades

    restored = read_trades(output)

    assert [t.price for t in restored] == [t.price for t in EXPECTED_TRADES]
    assert all(isinstance(t.price, Decimal) for t in restored)
    assert [t.size for t in restored] == [t.size for t in EXPECTED_TRADES]
    assert [t.side for t in restored] == [t.side for t in EXPECTED_TRADES]
    assert {TradeSide.BUY, TradeSide.SELL, TradeSide.UNKNOWN} == {t.side for t in restored}
    assert all(t.timestamp.utcoffset() == timedelta(0) for t in restored)
    assert all(t.instrument_symbol == SYMBOL for t in restored)


def test_repeated_identical_trades_remain_repeated(tmp_path: Path) -> None:
    # ADR-003 end to end: the fixture holds two indistinguishable one-lot
    # fills at the same microsecond. Both must reach the file and come back.
    _, output = _run_trades(tmp_path)
    from quant_research_terminal.data import read_trades

    restored = read_trades(output)
    identical = [
        t
        for t in restored
        if t.size == Decimal("1") and t.price == Decimal("5102.25") and t.side is TradeSide.BUY
    ]
    assert len(identical) == 2
    assert identical[0] == identical[1]
    assert sum(t.size for t in restored) == Decimal("16")


# ==========================================================================
# 2. Quotes: full round-trip
# ==========================================================================


def test_quotes_flow_end_to_end(tmp_path: Path) -> None:
    source = _write_csv(
        tmp_path / "quotes.csv",
        QUOTE_HEADER,
        "2024-03-04T14:30:00.000200+00:00,ESM6,5102.25,5102.50,7,9",
        "2024-03-04T14:30:00.000100+00:00,ESM6,5102.00,5102.25,3,4",
    )
    provider = CsvMarketDataProvider(path=source, record_type=ImportRecordType.QUOTE)
    output = tmp_path / "quotes.parquet"

    result = _use_case().run(
        provider=provider, request=_request(ImportRecordType.QUOTE), output_path=output
    )

    assert result.success is True
    assert result.records_verified == 2

    from quant_research_terminal.data import read_quotes

    first, second = read_quotes(output)
    # Deterministic order: the later file row is the earlier quote.
    assert first.timestamp == BASE + timedelta(microseconds=100)
    assert (first.bid, first.ask) == (Decimal("5102.00"), Decimal("5102.25"))
    assert (first.bid_size, first.ask_size) == (Decimal("3"), Decimal("4"))
    assert second.timestamp == BASE + timedelta(microseconds=200)
    assert (second.bid, second.ask) == (Decimal("5102.25"), Decimal("5102.50"))
    assert (second.bid_size, second.ask_size) == (Decimal("7"), Decimal("9"))


# ==========================================================================
# 3. Bars: interval semantics through the whole slice
# ==========================================================================


def test_bars_flow_end_to_end(tmp_path: Path) -> None:
    # CSV bar timestamps are availability times (interval close, ADR-002);
    # the interval column is in seconds.
    source = _write_csv(
        tmp_path / "bars.csv",
        BAR_HEADER,
        "2024-03-04T14:31:00+00:00,ESM6,60,5102.00,5103.25,5101.75,5102.50,120",
        "2024-03-04T14:32:00+00:00,ESM6,60,5102.50,5104.00,5102.25,5103.75,95",
    )
    provider = CsvMarketDataProvider(path=source, record_type=ImportRecordType.BAR)
    output = tmp_path / "bars.parquet"

    result = _use_case().run(
        provider=provider, request=_request(ImportRecordType.BAR), output_path=output
    )

    assert result.success is True

    from quant_research_terminal.data import read_bars

    first, second = read_bars(output)
    assert first.interval_start == BASE
    assert first.interval == timedelta(minutes=1)
    assert first.availability_time == BASE + timedelta(minutes=1)
    assert (first.open, first.high, first.low, first.close) == (
        Decimal("5102.00"),
        Decimal("5103.25"),
        Decimal("5101.75"),
        Decimal("5102.50"),
    )
    assert first.volume == Decimal("120")
    assert second.interval_start == BASE + timedelta(minutes=1)
    assert second.volume == Decimal("95")


# ==========================================================================
# 4. Conflicting bars: rejected end to end, no output (ADR-005)
# ==========================================================================


def test_conflicting_bars_fail_and_create_no_file(tmp_path: Path) -> None:
    source = _write_csv(
        tmp_path / "bars.csv",
        BAR_HEADER,
        "2024-03-04T14:31:00+00:00,ESM6,60,5102.00,5103.25,5101.75,5102.50,120",
        "2024-03-04T14:31:00+00:00,ESM6,60,5102.00,5103.25,5101.75,5102.50,999",
    )
    provider = CsvMarketDataProvider(path=source, record_type=ImportRecordType.BAR)
    output = tmp_path / "bars.parquet"

    with pytest.raises(ImportDatasetError) as caught:
        _use_case().run(
            provider=provider, request=_request(ImportRecordType.BAR), output_path=output
        )

    assert not output.exists()
    report = caught.value.report
    assert report is not None
    assert "conflicting_bar" in [issue.code.value for issue in report.issues]
    assert report.success is False


# ==========================================================================
# 5. Invalid numeric row: diagnosable rejection, nothing persisted
# ==========================================================================


def test_invalid_numeric_value_fails_with_a_diagnosable_report(tmp_path: Path) -> None:
    source = _write_csv(
        tmp_path / "trades.csv",
        "timestamp,instrument_symbol,price,size,side",
        "2024-03-04T14:30:00+00:00,ESM6,not-a-price,2,buy",
    )
    output = tmp_path / "trades.parquet"

    with pytest.raises(ImportDatasetError) as caught:
        _use_case().run(provider=_trade_provider(source), request=_request(), output_path=output)

    assert not output.exists()
    report = caught.value.report
    assert report is not None
    assert report.error_count >= 1
    issue = next(issue for issue in report.issues if issue.field_name == "price")
    assert issue.code.value == "non_decimal_price"
    assert issue.row_index == 0


def test_naive_timestamp_fails_with_a_diagnosable_report(tmp_path: Path) -> None:
    # The CSV format can express a timestamp without an offset; the provider
    # carries it through naive and validation must reject it.
    source = _write_csv(
        tmp_path / "trades.csv",
        "timestamp,instrument_symbol,price,size,side",
        "2024-03-04T14:30:00,ESM6,5102.25,2,buy",
    )
    output = tmp_path / "trades.parquet"

    with pytest.raises(ImportDatasetError) as caught:
        _use_case().run(provider=_trade_provider(source), request=_request(), output_path=output)

    assert not output.exists()
    report = caught.value.report
    assert report is not None
    assert "naive_datetime" in [issue.code.value for issue in report.issues]


# ==========================================================================
# 6. Structural source defect: provider error, no output
# ==========================================================================


def test_duplicate_csv_header_fails_structurally_and_creates_no_file(tmp_path: Path) -> None:
    source = _write_csv(
        tmp_path / "trades.csv",
        "timestamp,instrument_symbol,price,size,side,price",
        "2024-03-04T14:30:00+00:00,ESM6,5102.25,2,buy,5102.25",
    )
    output = tmp_path / "trades.parquet"

    with pytest.raises(ProviderDecodeError, match="duplicate column names"):
        _use_case().run(provider=_trade_provider(source), request=_request(), output_path=output)

    assert not output.exists()


def test_empty_source_fails_through_the_validation_contract(tmp_path: Path) -> None:
    source = _write_csv(tmp_path / "trades.csv", "timestamp,instrument_symbol,price,size,side")
    output = tmp_path / "trades.parquet"

    with pytest.raises(ImportDatasetError) as caught:
        _use_case().run(provider=_trade_provider(source), request=_request(), output_path=output)

    assert not output.exists()
    report = caught.value.report
    assert report is not None
    assert "empty_batch" in [issue.code.value for issue in report.issues]


# ==========================================================================
# 7. Determinism: a second run is semantically and byte identical
# ==========================================================================


def test_repeated_runs_produce_identical_results_and_files(tmp_path: Path) -> None:
    first_result, first_path = _run_trades(tmp_path, "first.parquet")
    second_result, second_path = _run_trades(tmp_path, "second.parquet")

    from quant_research_terminal.data import read_trades

    assert read_trades(first_path) == read_trades(second_path)
    assert first_result.model_dump(exclude={"output_path"}) == second_result.model_dump(
        exclude={"output_path"}
    )
    # Byte identity holds in the pinned environment: the storage layer already
    # verifies repeated writes are byte-identical (test_parquet_store), and the
    # pinned constraints fix the writing library. No claim is made across
    # PyArrow versions.
    assert first_path.read_bytes() == second_path.read_bytes()


# ==========================================================================
# 8. Resource ownership: the provider stream is always closed
# ==========================================================================


class _StreamObservingProvider:
    """A real provider with one addition: it remembers the streams it hands out.

    This is observation, not substitution — every record, decode rule, and
    resource comes from the real ``CsvMarketDataProvider`` it wraps.
    """

    def __init__(self, inner: CsvMarketDataProvider) -> None:
        self._inner = inner
        self.streams: list[RecordStream] = []

    @property
    def capabilities(self) -> ProviderCapabilities:
        return self._inner.capabilities

    def fetch(self, request: ProviderRequest) -> RecordStream:
        stream = self._inner.fetch(request)
        self.streams.append(stream)
        return stream


def test_stream_is_closed_after_a_successful_run(tmp_path: Path) -> None:
    provider = _StreamObservingProvider(_trade_provider())

    _use_case().run(provider=provider, request=_request(), output_path=tmp_path / "out.parquet")

    assert len(provider.streams) == 1
    assert provider.streams[0].closed


def test_stream_is_closed_after_a_failed_run(tmp_path: Path) -> None:
    source = _write_csv(
        tmp_path / "trades.csv",
        "timestamp,instrument_symbol,price,size,side",
        "2024-03-04T14:30:00+00:00,ESM6,not-a-price,2,buy",
    )
    provider = _StreamObservingProvider(_trade_provider(source))

    with pytest.raises(ImportDatasetError):
        _use_case().run(provider=provider, request=_request(), output_path=tmp_path / "out.parquet")

    assert len(provider.streams) == 1
    assert provider.streams[0].closed


# ==========================================================================
# 9. An existing target survives every failed run
# ==========================================================================


def test_failed_import_leaves_an_existing_dataset_untouched(tmp_path: Path) -> None:
    _, output = _run_trades(tmp_path)
    original_bytes = output.read_bytes()

    bad_source = _write_csv(
        tmp_path / "bad.csv",
        "timestamp,instrument_symbol,price,size,side",
        "2024-03-04T14:30:00+00:00,ESM6,not-a-price,2,buy",
    )
    with pytest.raises(ImportDatasetError):
        _use_case().run(
            provider=_trade_provider(bad_source), request=_request(), output_path=output
        )

    assert output.read_bytes() == original_bytes
    leftovers = [p.name for p in tmp_path.iterdir() if p.suffix in {".importing", ".partial"}]
    assert leftovers == []


# ==========================================================================
# 10. Record-type policy: a stream that violates one-type-per-dataset
# ==========================================================================


class _ConcatenatingProvider:
    """Chains two real CSV providers' streams into one.

    The protocol permits an implementation to yield any record types; this is
    the smallest real provider that exercises that freedom. Every record is
    genuine ``CsvMarketDataProvider`` output.
    """

    def __init__(self, first: CsvMarketDataProvider, second: CsvMarketDataProvider) -> None:
        self._first = first
        self._second = second

    @property
    def capabilities(self) -> ProviderCapabilities:
        return self._first.capabilities

    def fetch(self, request: ProviderRequest) -> RecordStream:
        def _chain() -> Iterator[RawRecord]:
            yield from self._first.fetch(
                ProviderRequest(
                    record_type=next(iter(self._first.capabilities.record_types)),
                    instrument_symbol=request.instrument_symbol,
                )
            )
            yield from self._second.fetch(
                ProviderRequest(
                    record_type=next(iter(self._second.capabilities.record_types)),
                    instrument_symbol=request.instrument_symbol,
                )
            )

        return RecordStream(_chain())


def test_mixed_record_types_are_refused_before_validation(tmp_path: Path) -> None:
    quotes = _write_csv(
        tmp_path / "quotes.csv",
        QUOTE_HEADER,
        "2024-03-04T14:30:00+00:00,ESM6,5102.00,5102.25,3,4",
    )
    provider = _ConcatenatingProvider(
        _trade_provider(),
        CsvMarketDataProvider(path=quotes, record_type=ImportRecordType.QUOTE),
    )
    output = tmp_path / "mixed.parquet"

    with pytest.raises(ImportDatasetError, match="exactly one record type"):
        _use_case().run(provider=provider, request=_request(), output_path=output)

    assert not output.exists()


# ==========================================================================
# The use case satisfies the provider protocol boundary
# ==========================================================================


def test_real_csv_provider_satisfies_the_application_port() -> None:
    assert isinstance(_trade_provider(), MarketDataProvider)


def test_verification_error_is_exported_and_distinct() -> None:
    # The error contract is part of the public API even though a verification
    # failure is not constructible from valid inputs (it would indicate a
    # storage defect, and the storage layer's own suite guards those).
    assert issubclass(VerificationError, Exception)
    assert not issubclass(VerificationError, ImportDatasetError)


# ==========================================================================
# Storage schema-v2 compatibility with canonical identity (Phase 2.0, ADR-009)
#
# Phase 2.0 introduces canonical futures identity in the domain and does *not*
# migrate persistence. These tests assert that limit so the claim in the docs
# is checkable rather than merely written down — and so that any future change
# to what is persisted must change a test that says why.
# ==========================================================================


def test_storage_v2_persists_only_the_legacy_vendor_symbol(tmp_path: Path) -> None:
    """A written dataset carries the flat symbol and nothing more.

    No venue column, no product column, no delivery month, no contract year.
    Canonical identity therefore cannot survive a round trip through schema v2,
    which is exactly why ADR-009 defers persistence to a future schema decision
    instead of reconstructing identity from ``"ESM6"`` by guessing.
    """
    import pyarrow.parquet as pq  # type: ignore[import-untyped]

    output = tmp_path / "esm6.parquet"
    _use_case().run(provider=_trade_provider(), request=_request(), output_path=output)

    table = pq.read_table(output)

    assert table.column_names == ["timestamp", "instrument_symbol", "price", "size", "side"]
    assert set(table.column("instrument_symbol").to_pylist()) == {SYMBOL}
    assert not {"venue", "product", "root", "contract_month", "contract_year"} & set(
        table.column_names
    )


def test_schema_version_is_unchanged_by_the_identity_phase(tmp_path: Path) -> None:
    from quant_research_terminal.data.contracts import SCHEMA_VERSION
    from quant_research_terminal.data.parquet_store import read_schema_metadata

    assert SCHEMA_VERSION == 2

    output = tmp_path / "esm6.parquet"
    _use_case().run(provider=_trade_provider(), request=_request(), output_path=output)

    assert read_schema_metadata(output)["schema_version"] == "2"


def test_the_stored_symbol_does_not_determine_a_contract_year(tmp_path: Path) -> None:
    """The fixture must never become evidence that ``ESM6`` means 2026.

    Everything the file records is compatible with several delivery years, so
    no code may derive one from it. The repository offers exactly one way to
    expand an abbreviated year, and it refuses to answer without being told the
    decade.
    """
    from quant_research_terminal.data.parquet_store import read_trades
    from quant_research_terminal.domain.futures_contract import (
        resolve_abbreviated_contract_year,
    )

    output = tmp_path / "esm6.parquet"
    _use_case().run(provider=_trade_provider(), request=_request(), output_path=output)
    restored = read_trades(output)

    # The observation dates say nothing about the delivery year either.
    assert all(trade.timestamp.year == 2024 for trade in restored)
    assert {trade.instrument_symbol for trade in restored} == {"ESM6"}

    # The one expansion API has no default and yields different, equally valid
    # answers for the different decades a caller might declare.
    assert resolve_abbreviated_contract_year("6", cycle_start=2020) == 2026
    assert resolve_abbreviated_contract_year("6", cycle_start=2010) == 2016
    with pytest.raises(TypeError):
        resolve_abbreviated_contract_year("6")  # type: ignore[call-arg]
