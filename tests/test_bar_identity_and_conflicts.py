"""Regression tests for ADR-005: bar identity and conflict semantics.

The defect this file pins: bar identity used to include OHLCV, so two records
for the same instrument and period with *different* values looked like two
distinct records and were both silently accepted — while two *identical*
records were correctly flagged. The validator caught the harmless case and
missed the harmful one.

Under ADR-005 a bar is identified by its period — instrument, interval start,
interval — and two records sharing that identity are two claims about one bar:
identical claims are an exact duplicate (warning, policy-resolved), differing
claims are a conflict (error, no copy survives).

Trade and quote semantics are pinned here too, because the fix must not regress
ADR-003: trades are never deduplicated, and quote behaviour is unchanged.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest

from quant_research_terminal.data_import import (
    DuplicatePolicy,
    DuplicateValidator,
    ImportBatch,
    ImportRecordType,
    RawRecord,
    ValidationSeverity,
    default_validation_pipeline,
    record_identity,
    validate_import_batch,
)
from quant_research_terminal.domain.models import Bar, Trade

BASE_TIME = datetime(2024, 1, 2, 12, 0, 0, tzinfo=UTC)
ONE_MINUTE = timedelta(minutes=1)

ALL_POLICIES = [DuplicatePolicy.REJECT, DuplicatePolicy.KEEP_FIRST, DuplicatePolicy.KEEP_LAST]


def _bar_row(**overrides: Any) -> dict[str, Any]:
    row: dict[str, Any] = {
        "timestamp": BASE_TIME,
        "instrument_symbol": "ES",
        "interval": ONE_MINUTE,
        "open": Decimal("4999.50"),
        "high": Decimal("5002.00"),
        "low": Decimal("4998.75"),
        "close": Decimal("5001.25"),
        "volume": Decimal("5"),
    }
    row.update(overrides)
    return row


def _trade_row(**overrides: Any) -> dict[str, Any]:
    row: dict[str, Any] = {
        "timestamp": BASE_TIME,
        "instrument_symbol": "ES",
        "price": Decimal("5000.25"),
        "size": Decimal("1"),
        "side": "BUY",
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


def _bar_batch(*rows: dict[str, Any], **kwargs: Any) -> ImportBatch:
    return ImportBatch(record_type=ImportRecordType.BAR, rows=tuple(rows), **kwargs)


def _record(record_type: ImportRecordType, fields: dict[str, Any], index: int) -> RawRecord:
    return RawRecord(
        record_type=record_type, source_index=index, provider_name="test", fields=fields
    )


def _codes(issues: tuple[Any, ...]) -> list[str]:
    return [issue.code.value for issue in issues]


# ==========================================================================
# The defect, reproduced: conflicting bars must not be silently accepted
# ==========================================================================


def test_conflicting_bars_are_not_silently_accepted() -> None:
    # Before ADR-005 this batch produced accepted=2, success=True, issues=[]:
    # both claims about one period survived, double-counting volume.
    accepted, report = validate_import_batch(
        _bar_batch(_bar_row(volume=Decimal("5")), _bar_row(volume=Decimal("9")))
    )

    assert accepted == []
    assert report.accepted_rows == 0
    assert report.rejected_rows == 2
    assert report.success is False
    assert "conflicting_bar" in _codes(report.issues)


@pytest.mark.parametrize("field", ["open", "high", "low", "close", "volume"])
def test_a_conflict_in_any_single_value_field_rejects_both(field: str) -> None:
    # A revision may touch any value; identity must ignore all of them.
    # The override nudges high upward so OHLC stays internally consistent and
    # the only reportable defect is the conflict itself.
    revised = _bar_row(high=Decimal("5003.00"))
    revised[field] = Decimal("5002.50") if field != "volume" else Decimal("9")

    accepted, report = validate_import_batch(_bar_batch(_bar_row(), revised))

    assert report.accepted_rows == 0
    assert report.success is False
    conflict_issues = [issue for issue in report.issues if issue.code.value == "conflicting_bar"]
    assert len(conflict_issues) == 2


def test_a_conflict_in_multiple_fields_names_them_all_in_field_order() -> None:
    revised = _bar_row(close=Decimal("5000.00"), volume=Decimal("9"))

    _, report = validate_import_batch(_bar_batch(_bar_row(), revised))

    conflict = next(issue for issue in report.issues if issue.code.value == "conflicting_bar")
    assert "close, volume" in conflict.message
    assert conflict.field_name == "close"


def test_conflict_is_an_error_attributed_to_every_member() -> None:
    # Rejection is index-driven, so the first occurrence must carry the error
    # too — otherwise it would survive, silently preferring earlier data.
    _, report = validate_import_batch(_bar_batch(_bar_row(), _bar_row(volume=Decimal("9"))))

    conflicts = [issue for issue in report.issues if issue.code.value == "conflicting_bar"]
    assert [issue.row_index for issue in conflicts] == [0, 1]
    assert all(issue.severity is ValidationSeverity.ERROR for issue in conflicts)
    assert all("no copy is retained" in issue.message for issue in conflicts)


@pytest.mark.parametrize("policy", ALL_POLICIES)
def test_no_duplicate_policy_can_resurrect_a_conflicted_bar(policy: DuplicatePolicy) -> None:
    # Policy resolves duplicates, not disagreements. KEEP_LAST especially must
    # not become a silent prefer-later rule.
    accepted, report = validate_import_batch(
        _bar_batch(_bar_row(), _bar_row(volume=Decimal("9")), duplicate_policy=policy)
    )

    assert accepted == []
    assert report.accepted_rows == 0
    assert report.success is False


def test_conflicting_volume_is_not_counted_at_all() -> None:
    # Neither 5 nor 9 is known to be true, so neither may enter an aggregate.
    accepted, _ = validate_import_batch(
        _bar_batch(_bar_row(volume=Decimal("5")), _bar_row(volume=Decimal("9")))
    )

    assert sum(bar.volume for bar in accepted if isinstance(bar, Bar)) == Decimal("0")


def test_a_mixed_group_rejects_identical_copies_too() -> None:
    # Two identical copies plus one revision: copy count measures duplication,
    # not truth, so agreement between copies must not outvote the revision.
    accepted, report = validate_import_batch(
        _bar_batch(_bar_row(), _bar_row(), _bar_row(close=Decimal("5000.00")))
    )

    assert accepted == []
    assert report.accepted_rows == 0
    assert _codes(report.issues) == ["conflicting_bar", "conflicting_bar", "conflicting_bar"]


def test_a_conflicted_group_emits_no_duplicate_warnings() -> None:
    # The group's diagnosis is the conflict; a warning promising that "one
    # copy will be discarded by policy" would be false there.
    _, report = validate_import_batch(
        _bar_batch(_bar_row(), _bar_row(), _bar_row(volume=Decimal("9")))
    )

    assert "duplicate_row" not in _codes(report.issues)
    assert report.warning_count == 0


def test_an_unrelated_bar_survives_a_conflict_in_the_same_batch() -> None:
    # Conflict rejection is per identity group, not per batch.
    unrelated = _bar_row(timestamp=BASE_TIME + ONE_MINUTE)

    accepted, report = validate_import_batch(
        _bar_batch(_bar_row(), _bar_row(volume=Decimal("9")), unrelated)
    )

    assert report.accepted_rows == 1
    assert len(accepted) == 1
    assert isinstance(accepted[0], Bar)
    assert accepted[0].volume == Decimal("5")


# ==========================================================================
# Bar identity is the period, nothing else
# ==========================================================================


def test_bar_identity_ignores_every_value_field() -> None:
    plain = _record(ImportRecordType.BAR, _bar_row(), 0)
    revised = _record(
        ImportRecordType.BAR,
        _bar_row(
            open=Decimal("5000.00"),
            high=Decimal("5003.00"),
            low=Decimal("4999.00"),
            close=Decimal("5002.00"),
            volume=Decimal("99"),
        ),
        1,
    )

    assert record_identity(plain) == record_identity(revised)


@pytest.mark.parametrize(
    "override",
    [
        {"instrument_symbol": "NQ"},
        {"timestamp": BASE_TIME + ONE_MINUTE},
        {"interval": timedelta(minutes=5)},
    ],
    ids=["instrument", "timestamp", "interval"],
)
def test_bar_identity_distinguishes_each_period_component(override: dict[str, Any]) -> None:
    base = _record(ImportRecordType.BAR, _bar_row(), 0)
    other = _record(ImportRecordType.BAR, _bar_row(**override), 1)

    assert record_identity(base) != record_identity(other)


def test_bars_for_different_periods_never_conflict() -> None:
    # Same values, different periods: two different bars, no issue at all.
    rows = [_bar_row(), _bar_row(timestamp=BASE_TIME + ONE_MINUTE)]

    accepted, report = validate_import_batch(_bar_batch(*rows))

    assert report.accepted_rows == 2
    assert report.issues == ()
    assert report.success is True


# ==========================================================================
# Exact duplicates keep their existing policy semantics
# ==========================================================================


@pytest.mark.parametrize("policy", ALL_POLICIES)
def test_exact_duplicate_bars_collapse_to_one_under_every_policy(
    policy: DuplicatePolicy,
) -> None:
    accepted, report = validate_import_batch(
        _bar_batch(_bar_row(), _bar_row(), duplicate_policy=policy)
    )

    assert report.accepted_rows == 1
    assert report.success is True
    assert _codes(report.issues) == ["duplicate_row"]
    assert report.issues[0].severity is ValidationSeverity.WARNING


def test_exact_duplicate_bars_do_not_double_count_volume() -> None:
    accepted, _ = validate_import_batch(_bar_batch(_bar_row(), _bar_row(), _bar_row()))

    assert sum(bar.volume for bar in accepted if isinstance(bar, Bar)) == Decimal("5")


def test_duplicate_and_conflict_counts_are_separated_in_the_report() -> None:
    # One exact-duplicate pair and one conflicting pair in the same batch:
    # the duplicate stays a warning, the conflict stays an error, and the
    # accounting reflects one survivor and two rejections.
    other_period = _bar_row(timestamp=BASE_TIME + ONE_MINUTE)

    _, report = validate_import_batch(
        _bar_batch(_bar_row(), _bar_row(), other_period, {**other_period, "volume": Decimal("9")})
    )

    assert report.total_rows == 4
    assert report.accepted_rows == 1
    assert report.rejected_rows == 3
    assert report.warning_count == 1
    assert report.error_count == 2
    assert report.success is False


# ==========================================================================
# Deterministic reporting
# ==========================================================================


def test_conflict_issue_order_is_deterministic_and_ascending() -> None:
    batch = _bar_batch(_bar_row(), _bar_row(volume=Decimal("9")), _bar_row(volume=Decimal("7")))

    _, first_report = validate_import_batch(batch)
    _, second_report = validate_import_batch(batch)

    assert first_report.issues == second_report.issues
    rows = [issue.row_index for issue in first_report.issues if issue.row_index is not None]
    assert len(rows) == len(first_report.issues)
    assert rows == sorted(rows)


def test_streaming_pipeline_reports_the_same_conflict() -> None:
    # The provider-stream path shares the DuplicateValidator, so a conflict is
    # not a batch-API-only guarantee.
    records = [
        _record(ImportRecordType.BAR, _bar_row(), 0),
        _record(ImportRecordType.BAR, _bar_row(volume=Decimal("9")), 1),
    ]

    issues = default_validation_pipeline().validate(records)

    assert "conflicting_bar" in [issue.code.value for issue in issues]


def test_validator_alone_reports_conflict_for_every_member() -> None:
    records = [
        _record(ImportRecordType.BAR, _bar_row(), 0),
        _record(ImportRecordType.BAR, _bar_row(volume=Decimal("9")), 1),
    ]

    issues = DuplicateValidator().validate(records)

    assert [issue.code.value for issue in issues] == ["conflicting_bar", "conflicting_bar"]
    assert [issue.row_index for issue in issues] == [0, 1]
    assert "source index(es) 1" in issues[0].message
    assert "source index(es) 0" in issues[1].message


# ==========================================================================
# Non-regression: trades (ADR-003) and quotes are unchanged
# ==========================================================================


@pytest.mark.parametrize("policy", ALL_POLICIES)
def test_identical_trades_all_survive_under_every_policy(policy: DuplicatePolicy) -> None:
    batch = ImportBatch(
        record_type=ImportRecordType.TRADE,
        rows=(_trade_row(), _trade_row(), _trade_row()),
        duplicate_policy=policy,
    )

    accepted, report = validate_import_batch(batch)

    assert report.accepted_rows == 3
    assert report.success is True
    assert "duplicate_row" not in _codes(report.issues)
    assert "conflicting_bar" not in _codes(report.issues)
    assert sum(trade.size for trade in accepted if isinstance(trade, Trade)) == Decimal("3")


def test_trades_differing_only_in_price_are_not_a_conflict() -> None:
    # Trades have no identity, so the bar conflict concept must not leak.
    batch = ImportBatch(
        record_type=ImportRecordType.TRADE,
        rows=(_trade_row(), _trade_row(price=Decimal("5000.50"))),
    )

    _, report = validate_import_batch(batch)

    assert report.accepted_rows == 2
    assert report.issues == ()


def test_identical_quotes_still_collapse_as_duplicates() -> None:
    batch = ImportBatch(record_type=ImportRecordType.QUOTE, rows=(_quote_row(), _quote_row()))

    _, report = validate_import_batch(batch)

    assert report.accepted_rows == 1
    assert _codes(report.issues) == ["duplicate_row"]
    assert report.success is True


def test_quotes_differing_in_a_value_remain_distinct_records() -> None:
    # Quote identity spans every field, so a differing quote is a different
    # record — never a conflict. Broadening quote identity is out of scope
    # (ADR-005) unless a confirmed defect requires it.
    batch = ImportBatch(
        record_type=ImportRecordType.QUOTE,
        rows=(_quote_row(), _quote_row(ask=Decimal("5000.50"))),
    )

    _, report = validate_import_batch(batch)

    assert report.accepted_rows == 2
    assert report.issues == ()
    assert "conflicting_bar" not in _codes(report.issues)
