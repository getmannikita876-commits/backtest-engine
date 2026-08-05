"""Tests for the ThetaData export inspector and the experimental marking.

These test the *inspector*, not the vendor. Fixtures here are hand-written and
deliberately include shapes the decoder does not expect, so the tool is shown
to report what a file actually contains rather than what the decoder hopes it
contains. No vendor market data is committed.
"""

from __future__ import annotations

from datetime import UTC, timedelta
from pathlib import Path

import pytest

from quant_research_terminal.data_import import (
    BarTimestampMeaning,
    ThetaDataMarketDataProvider,
    ThetaDataSchema,
)
from quant_research_terminal.data_import.providers.thetadata_inspection import (
    DEFAULT_MAX_ROWS,
    ExportInspection,
    format_inspection_report,
    inspect_thetadata_export,
    main,
)
from quant_research_terminal.data_import.providers.thetadata_provider import (
    VERIFICATION_STATUS,
)


def _write(path: Path, *lines: str) -> Path:
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def _trade_export(tmp_path: Path) -> Path:
    return _write(
        tmp_path / "trades.csv",
        "date,ms_of_day,price,size",
        "20240102,43200000,5000.25,2",
        "20240102,43201000,5000.50,1",
    )


def _inspect(path: Path) -> ExportInspection:
    return inspect_thetadata_export(path)


# --------------------------------------------------------------------------
# Experimental marking
# --------------------------------------------------------------------------


def test_provider_declares_itself_unverified(tmp_path: Path) -> None:
    provider = ThetaDataMarketDataProvider(
        path=tmp_path / "trades.csv",
        schema=ThetaDataSchema.TRADE,
        session_timezone=UTC,
        instrument_symbol="ES",
    )

    assert provider.verification_status == VERIFICATION_STATUS == "experimental-unverified"


def test_verification_status_is_not_satisfied_by_passing_tests() -> None:
    # The status is evidence-gated, not test-gated: this suite was written from
    # the implementation, so it cannot verify vendor fidelity.
    assert "unverified" in VERIFICATION_STATUS


# --------------------------------------------------------------------------
# Structure reporting
# --------------------------------------------------------------------------


def test_header_and_row_count_are_reported(tmp_path: Path) -> None:
    inspection = _inspect(_trade_export(tmp_path))

    assert inspection.header == ("date", "ms_of_day", "price", "size")
    assert inspection.rows_inspected == 2
    assert inspection.truncated is False


def test_missing_header_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "empty.csv"
    path.write_text("", encoding="utf-8")

    with pytest.raises(ValueError, match="no header row"):
        _inspect(path)


def test_unrecognised_columns_are_listed(tmp_path: Path) -> None:
    # A real export is expected to carry columns the decoder ignores; the
    # operator needs to see them to judge whether any of them matter.
    path = _write(
        tmp_path / "trades.csv",
        "date,ms_of_day,price,size,exchange,condition",
        "20240102,43200000,5000.25,2,N,@",
    )

    inspection = _inspect(path)

    assert inspection.unrecognised_columns == ("exchange", "condition")
    assert "date" in inspection.recognised_columns


def test_schema_requirements_are_reported_per_schema(tmp_path: Path) -> None:
    inspection = _inspect(_trade_export(tmp_path))

    assert "trade" in inspection.satisfied_schemas
    assert "quote" not in inspection.satisfied_schemas

    quote = next(e for e in inspection.schema_expectations if e.schema_name == "quote")
    assert set(quote.missing_required_columns) == {"bid", "ask", "bid_size", "ask_size"}


def test_row_limit_is_reported_as_truncation(tmp_path: Path) -> None:
    rows = [f"20240102,{43200000 + index},5000.25,1" for index in range(5)]
    path = _write(tmp_path / "trades.csv", "date,ms_of_day,price,size", *rows)

    inspection = inspect_thetadata_export(path, max_rows=3)

    assert inspection.rows_inspected == 3
    assert inspection.truncated is True


def test_default_row_limit_is_bounded() -> None:
    assert DEFAULT_MAX_ROWS > 0


# --------------------------------------------------------------------------
# Value shape reporting
# --------------------------------------------------------------------------


def test_decimal_and_integer_columns_are_distinguished(tmp_path: Path) -> None:
    inspection = _inspect(_trade_export(tmp_path))
    columns = {column.name: column for column in inspection.columns}

    assert columns["price"].decimal_count == 2
    assert columns["price"].integer_count == 0
    assert columns["price"].maximum_decimal_places == 2
    assert columns["size"].integer_count == 2
    assert columns["size"].decimal_count == 0


def test_non_numeric_values_are_counted_not_coerced(tmp_path: Path) -> None:
    path = _write(
        tmp_path / "trades.csv",
        "date,ms_of_day,price,size",
        "20240102,43200000,N/A,2",
    )

    inspection = _inspect(path)
    price = next(column for column in inspection.columns if column.name == "price")

    assert price.text_count == 1
    assert "N/A" in price.distinct_examples


def test_raw_values_are_echoed_verbatim(tmp_path: Path) -> None:
    # Nothing is stripped, normalized, or rewritten on the way into the report.
    path = _write(
        tmp_path / "trades.csv",
        "date,ms_of_day,price,size",
        "20240102,43200000, 5000.250 ,2",
    )

    inspection = _inspect(path)
    price = next(column for column in inspection.columns if column.name == "price")

    assert price.distinct_examples == (" 5000.250 ",)


def test_integer_range_is_reported(tmp_path: Path) -> None:
    inspection = _inspect(_trade_export(tmp_path))
    ms_of_day = next(column for column in inspection.columns if column.name == "ms_of_day")

    assert ms_of_day.minimum_integer == 43_200_000
    assert ms_of_day.maximum_integer == 43_201_000


def test_short_rows_do_not_abort_the_inspection(tmp_path: Path) -> None:
    # The inspector must survive a ragged file; diagnosing one is the point.
    path = _write(
        tmp_path / "trades.csv",
        "date,ms_of_day,price,size",
        "20240102,43200000",
    )

    inspection = _inspect(path)
    size = next(column for column in inspection.columns if column.name == "size")

    assert inspection.rows_inspected == 1
    assert size.values_seen == 0


# --------------------------------------------------------------------------
# Findings are observations, never conclusions
# --------------------------------------------------------------------------


def test_date_shaped_column_is_reported_as_consistent_not_confirmed(tmp_path: Path) -> None:
    inspection = _inspect(_trade_export(tmp_path))

    date_findings = [f for f in inspection.findings if "'date'" in f]
    assert date_findings
    assert any("consistent with a YYYYMMDD date" in f for f in date_findings)
    assert any("not confirmed" in f for f in date_findings)


def test_millisecond_range_finding_does_not_rule_out_other_units(tmp_path: Path) -> None:
    inspection = _inspect(_trade_export(tmp_path))

    ms_findings = [f for f in inspection.findings if "'ms_of_day'" in f]
    assert any("cannot be ruled out" in f for f in ms_findings)


def test_large_integer_price_column_triggers_a_scale_warning(tmp_path: Path) -> None:
    # The failure mode that would otherwise be silent: a scaled-integer price
    # imports cleanly but is wrong by a constant factor.
    path = _write(
        tmp_path / "trades.csv",
        "date,ms_of_day,price,size",
        "20240102,43200000,5000250000,2",
    )

    inspection = _inspect(path)

    assert any(
        "a scale factor may be in use" in finding and "'price'" in finding
        for finding in inspection.findings
    )


def test_millisecond_offset_is_not_reported_as_a_candidate_date(tmp_path: Path) -> None:
    # Eight digits alone is not evidence of a date: 43200000 is also eight
    # digits. Structure — a plausible month and day — is the discriminator.
    inspection = _inspect(_trade_export(tmp_path))

    assert not any(
        "'ms_of_day'" in finding and "YYYYMMDD" in finding for finding in inspection.findings
    )


def test_small_integer_column_is_not_reported_as_a_time_offset(tmp_path: Path) -> None:
    # A size of 2 is consistent with almost anything; reporting it would be
    # noise rather than evidence.
    inspection = _inspect(_trade_export(tmp_path))

    assert not any(
        "'size'" in finding and "milliseconds" in finding for finding in inspection.findings
    )


def test_temporal_columns_do_not_attract_the_scale_factor_caution(tmp_path: Path) -> None:
    inspection = _inspect(_trade_export(tmp_path))

    assert not any(
        "scale factor" in finding and ("'date'" in finding or "'ms_of_day'" in finding)
        for finding in inspection.findings
    )


def test_byte_order_mark_is_reported_not_stripped(tmp_path: Path) -> None:
    # Silently stripping it would hide that the decoder, which matches column
    # names literally, will not recognise the first column of this file.
    path = tmp_path / "trades.csv"
    path.write_text(
        "date,ms_of_day,price,size\n20240102,43200000,5000.25,2\n", encoding="utf-8-sig"
    )

    inspection = _inspect(path)

    assert any("byte-order mark" in finding for finding in inspection.findings)
    assert "date" not in inspection.recognised_columns


def test_zero_values_are_reported_as_candidate_sentinels(tmp_path: Path) -> None:
    path = _write(
        tmp_path / "quotes.csv",
        "date,ms_of_day,bid,ask,bid_size,ask_size",
        "20240102,43200000,0,0,0,0",
    )

    inspection = _inspect(path)

    assert any("candidate 'no value' sentinel" in finding for finding in inspection.findings)


def test_empty_values_are_reported(tmp_path: Path) -> None:
    path = _write(
        tmp_path / "trades.csv",
        "date,ms_of_day,price,size",
        "20240102,43200000,,2",
    )

    inspection = _inspect(path)

    assert any("values are empty" in finding for finding in inspection.findings)


# --------------------------------------------------------------------------
# Report rendering and entry point
# --------------------------------------------------------------------------


def test_report_states_that_it_verifies_nothing(tmp_path: Path) -> None:
    report = format_inspection_report(_inspect(_trade_export(tmp_path)))

    assert "does not verify the decoder's" in report
    assert "observations only" in report


def test_report_lists_columns_and_schema_status(tmp_path: Path) -> None:
    report = format_inspection_report(_inspect(_trade_export(tmp_path)))

    assert "date, ms_of_day, price, size" in report
    assert "trade: satisfied" in report
    assert "quote: missing columns" in report


def test_entry_point_reports_and_succeeds(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    exit_code = main([str(_trade_export(tmp_path))])

    assert exit_code == 0
    assert "ThetaData export inspection" in capsys.readouterr().out


def test_entry_point_fails_cleanly_on_a_missing_file(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    exit_code = main([str(tmp_path / "absent.csv")])

    assert exit_code == 1
    assert "error:" in capsys.readouterr().err


def test_entry_point_honours_the_row_limit(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    rows = [f"20240102,{43200000 + index},5000.25,1" for index in range(4)]
    path = _write(tmp_path / "trades.csv", "date,ms_of_day,price,size", *rows)

    assert main([str(path), "--max-rows", "2"]) == 0
    assert "(truncated)" in capsys.readouterr().out


# --------------------------------------------------------------------------
# The inspector must not modify what it reads
# --------------------------------------------------------------------------


def test_inspection_leaves_the_file_untouched(tmp_path: Path) -> None:
    path = _trade_export(tmp_path)
    before = path.read_bytes()
    modified_before = path.stat().st_mtime_ns

    _inspect(path)

    assert path.read_bytes() == before
    assert path.stat().st_mtime_ns == modified_before


def test_inspection_report_is_immutable(tmp_path: Path) -> None:
    inspection = _inspect(_trade_export(tmp_path))

    with pytest.raises(ValueError, match="frozen"):
        inspection.rows_inspected = 99


def test_inspecting_twice_gives_the_same_report(tmp_path: Path) -> None:
    path = _trade_export(tmp_path)

    assert _inspect(path) == _inspect(path)


def test_bar_interval_settings_are_unaffected_by_inspection(tmp_path: Path) -> None:
    # Guards the operator declarations the audit required be preserved.
    provider = ThetaDataMarketDataProvider(
        path=tmp_path / "ohlc.csv",
        schema=ThetaDataSchema.OHLC,
        session_timezone=UTC,
        instrument_symbol="ES",
        bar_interval=timedelta(minutes=1),
        bar_timestamp_meaning=BarTimestampMeaning.INTERVAL_START,
    )

    assert provider.bar_interval == timedelta(minutes=1)
    assert provider.verification_status == "experimental-unverified"
