"""Tests for deterministic materialization: DST hostility, hashing, repeatability.

These use synthetic definitions (real IANA zones, invented schedules) so the
mechanics are provable independently of any exchange's facts: nonexistent and
ambiguous local boundaries must raise typed errors, materialization must be
byte-repeatable, and the content hash must move exactly when the semantics
move.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, time
from zoneinfo import ZoneInfo

import pytest

from quant_research_terminal.domain.calendar_definition import (
    CalendarDefinition,
    DatedException,
    EvidenceRecord,
    ExceptionWindow,
    TimezoneProbe,
    WeeklyWindowRule,
)
from quant_research_terminal.domain.calendar_materialization import (
    canonical_serialization,
    local_wall_time_to_utc,
    materialize,
)
from quant_research_terminal.domain.exchange_calendar import (
    AmbiguousLocalTimeError,
    CalendarId,
    CalendarVersion,
    ExchangeTradingState,
    MaterializationError,
    NonexistentLocalTimeError,
    VerificationStatus,
)

CHICAGO = ZoneInfo("America/Chicago")

EVIDENCE = EvidenceRecord(
    evidence_id="test-evidence",
    source_type="test",
    title="A test claim",
    source_url="https://example.invalid/source",
    archive_url="",
    accessed=date(2026, 8, 10),
    claim="A synthetic fact used only by tests.",
)


def rule(
    state: ExchangeTradingState = ExchangeTradingState.TRADING,
    start_day_offset: int = 0,
    start_time: time = time(9, 0),
    end_day_offset: int = 0,
    end_time: time = time(16, 0),
) -> WeeklyWindowRule:
    return WeeklyWindowRule(
        state=state,
        start_day_offset=start_day_offset,
        start_time=start_time,
        end_day_offset=end_day_offset,
        end_time=end_time,
        provenance_status=VerificationStatus.VERIFIED_PRIMARY,
        evidence_ids=("test-evidence",),
    )


def synthetic_definition(
    *,
    version: int = 1,
    first: date = date(2023, 3, 6),
    last: date = date(2023, 3, 17),
    weekly_rules: tuple[tuple[str, tuple[WeeklyWindowRule, ...]], ...] | None = None,
    exceptions: tuple[DatedException, ...] = (),
    probes: tuple[TimezoneProbe, ...] = (),
) -> CalendarDefinition:
    if weekly_rules is None:
        weekly_rules = tuple(
            (weekday, (rule(),))
            for weekday in ("monday", "tuesday", "wednesday", "thursday", "friday")
        )
    return CalendarDefinition(
        calendar_id=CalendarId(token="SYNTHETIC_TEST"),
        version=CalendarVersion(number=version),
        timezone_name="America/Chicago",
        first_trading_date=first,
        last_trading_date=last,
        weekly_rules=weekly_rules,
        exceptions=exceptions,
        timezone_probes=probes,
        evidence=(EVIDENCE,),
    )


# --------------------------------------------------------------------------
# local_wall_time_to_utc: the only local arithmetic in the platform
# --------------------------------------------------------------------------


def test_standard_time_converts_exactly() -> None:
    # 2023-01-16 09:00 CST is UTC-6.
    result = local_wall_time_to_utc(date(2023, 1, 16), time(9, 0), CHICAGO)
    assert result == datetime(2023, 1, 16, 15, 0, tzinfo=UTC)


def test_daylight_time_converts_exactly() -> None:
    # 2023-06-15 09:00 CDT is UTC-5.
    result = local_wall_time_to_utc(date(2023, 6, 15), time(9, 0), CHICAGO)
    assert result == datetime(2023, 6, 15, 14, 0, tzinfo=UTC)


def test_a_nonexistent_spring_forward_time_raises() -> None:
    # 2023-03-12 02:30 was skipped in America/Chicago.
    with pytest.raises(NonexistentLocalTimeError):
        local_wall_time_to_utc(date(2023, 3, 12), time(2, 30), CHICAGO)


def test_an_ambiguous_fall_back_time_raises() -> None:
    # 2023-11-05 01:30 occurred twice in America/Chicago; no fold default.
    with pytest.raises(AmbiguousLocalTimeError):
        local_wall_time_to_utc(date(2023, 11, 5), time(1, 30), CHICAGO)


def test_boundaries_adjacent_to_the_gap_still_convert() -> None:
    before = local_wall_time_to_utc(date(2023, 3, 12), time(1, 59, 59), CHICAGO)
    after = local_wall_time_to_utc(date(2023, 3, 12), time(3, 0), CHICAGO)
    assert before == datetime(2023, 3, 12, 7, 59, 59, tzinfo=UTC)
    assert after == datetime(2023, 3, 12, 8, 0, tzinfo=UTC)


# --------------------------------------------------------------------------
# Materialization mechanics
# --------------------------------------------------------------------------


def test_materialization_is_repeatable_and_byte_identical() -> None:
    definition = synthetic_definition()
    first = materialize(definition)
    second = materialize(definition)
    assert first == second
    assert first.content_hash == second.content_hash
    assert canonical_serialization(
        calendar_id=first.calendar_id,
        version=first.version,
        timezone_name=first.timezone_name,
        first_trading_date=first.first_trading_date,
        last_trading_date=first.last_trading_date,
        coverage_start=first.coverage_start,
        coverage_end=first.coverage_end,
        windows=first.windows,
        provenance=first.provenance,
    ) == canonical_serialization(
        calendar_id=second.calendar_id,
        version=second.version,
        timezone_name=second.timezone_name,
        first_trading_date=second.first_trading_date,
        last_trading_date=second.last_trading_date,
        coverage_start=second.coverage_start,
        coverage_end=second.coverage_end,
        windows=second.windows,
        provenance=second.provenance,
    )


def test_windows_are_strictly_ordered_and_non_overlapping() -> None:
    calendar = materialize(synthetic_definition())
    for earlier, later in zip(calendar.windows, calendar.windows[1:], strict=False):
        assert earlier.start < earlier.end
        assert earlier.end <= later.start


def test_every_window_is_utc_aware_and_carries_no_invented_precision() -> None:
    calendar = materialize(synthetic_definition())
    for window in calendar.windows:
        for instant in (window.start, window.end):
            assert instant.tzinfo == UTC
            # The synthetic rules are authored at whole minutes, so a
            # materialized boundary carrying seconds or microseconds would
            # mean the materializer invented precision. (Sub-microsecond
            # values are structurally unrepresentable in datetime, which is
            # the platform's persisted precision.)
            assert instant.second == 0
            assert instant.microsecond == 0


def test_a_changed_boundary_changes_the_content_hash() -> None:
    original = materialize(synthetic_definition())
    changed_rules = tuple(
        (weekday, (rule(end_time=time(16, 1)),))
        for weekday in ("monday", "tuesday", "wednesday", "thursday", "friday")
    )
    changed = materialize(synthetic_definition(weekly_rules=changed_rules))
    assert original.content_hash != changed.content_hash


def test_the_declared_trading_date_range_participates_in_the_hash() -> None:
    """Falsification defect D1, regression.

    Two definitions whose only difference is the declared trading-date range
    can produce identical windows (the extra days contribute none), yet they
    answer the inverse API differently: an in-range non-trading date returns
    ``()`` while an out-of-range date raises. Semantics differ, so the hashes
    must differ. Before the fix they were equal.
    """
    rules = (("wednesday", (rule(),)),)
    wide = materialize(
        synthetic_definition(first=date(2023, 3, 6), last=date(2023, 3, 10), weekly_rules=rules)
    )
    narrow = materialize(
        synthetic_definition(first=date(2023, 3, 8), last=date(2023, 3, 8), weekly_rules=rules)
    )
    assert wide.windows == narrow.windows
    assert wide.content_hash != narrow.content_hash


def test_changed_verification_metadata_changes_the_content_hash() -> None:
    """Falsification defect D3, regression.

    ``CalendarContext.verification`` is public resolver output derived from
    the provenance table, so a released version whose evidence citations or
    statuses change must not keep its hash — otherwise "immutable v1" would
    pin only part of what v1 observably answers.
    """
    original = materialize(synthetic_definition())
    downgraded_rules = tuple(
        (
            weekday,
            (
                WeeklyWindowRule(
                    state=ExchangeTradingState.TRADING,
                    start_day_offset=0,
                    start_time=time(9, 0),
                    end_day_offset=0,
                    end_time=time(16, 0),
                    provenance_status=VerificationStatus.CORROBORATED,
                    evidence_ids=("test-evidence",),
                ),
            ),
        )
        for weekday in ("monday", "tuesday", "wednesday", "thursday", "friday")
    )
    downgraded = materialize(synthetic_definition(weekly_rules=downgraded_rules))
    assert original.windows == downgraded.windows
    assert original.content_hash != downgraded.content_hash


def test_a_changed_version_changes_the_content_hash() -> None:
    original = materialize(synthetic_definition(version=1))
    bumped = materialize(synthetic_definition(version=2))
    assert original.content_hash != bumped.content_hash


def test_v1_results_are_unchanged_by_the_existence_of_a_v2() -> None:
    """Version immutability: releasing a corrected v2 must not move v1."""
    v1_before = materialize(synthetic_definition(version=1))
    changed_rules = tuple(
        (weekday, (rule(end_time=time(15, 0)),))
        for weekday in ("monday", "tuesday", "wednesday", "thursday", "friday")
    )
    materialize(synthetic_definition(version=2, weekly_rules=changed_rules))
    v1_after = materialize(synthetic_definition(version=1))
    assert v1_before == v1_after
    assert v1_before.content_hash == v1_after.content_hash


def test_a_rule_boundary_in_the_dst_gap_fails_materialization() -> None:
    # A synthetic session boundary at 02:30 hits the 2023-03-12 spring gap on
    # the Sunday-anchored offset day of Monday 2023-03-13.
    rules = (("monday", (rule(start_day_offset=-1, start_time=time(2, 30)),)),)
    with pytest.raises(NonexistentLocalTimeError):
        materialize(
            synthetic_definition(
                first=date(2023, 3, 13), last=date(2023, 3, 13), weekly_rules=rules
            )
        )


def test_a_rule_boundary_in_the_dst_fold_fails_materialization() -> None:
    rules = (("monday", (rule(start_day_offset=-1, start_time=time(1, 30)),)),)
    with pytest.raises(AmbiguousLocalTimeError):
        materialize(
            synthetic_definition(
                first=date(2023, 11, 6), last=date(2023, 11, 6), weekly_rules=rules
            )
        )


def test_overlapping_sessions_across_trading_dates_fail_materialization() -> None:
    # Monday trades 09:00-16:00; Tuesday's session claims to start Monday
    # 15:00 — the materializer must refuse the overlap, not order around it.
    rules = (
        ("monday", (rule(),)),
        ("tuesday", (rule(start_day_offset=-1, start_time=time(15, 0)),)),
    )
    with pytest.raises(MaterializationError):
        materialize(
            synthetic_definition(first=date(2023, 3, 6), last=date(2023, 3, 7), weekly_rules=rules)
        )


def test_a_range_with_no_windows_fails_materialization() -> None:
    with pytest.raises(MaterializationError):
        materialize(
            synthetic_definition(first=date(2023, 3, 11), last=date(2023, 3, 12))
        )  # Saturday and Sunday only


def test_a_failing_timezone_probe_fails_materialization() -> None:
    lying_probe = TimezoneProbe(
        utc_instant=datetime(2023, 6, 15, 12, 0, tzinfo=UTC),
        expected_utc_offset_minutes=-360,  # summer Chicago is actually -300
    )
    with pytest.raises(MaterializationError):
        materialize(synthetic_definition(probes=(lying_probe,)))


def test_a_correct_timezone_probe_passes() -> None:
    honest_probes = (
        TimezoneProbe(
            utc_instant=datetime(2023, 6, 15, 12, 0, tzinfo=UTC),
            expected_utc_offset_minutes=-300,
        ),
        TimezoneProbe(
            utc_instant=datetime(2023, 1, 15, 12, 0, tzinfo=UTC),
            expected_utc_offset_minutes=-360,
        ),
    )
    materialize(synthetic_definition(probes=honest_probes))


def test_removed_exception_produces_no_windows_for_that_date() -> None:
    removed = DatedException(
        trading_date=date(2023, 3, 8),
        kind="removed",
        windows=(),
        provenance_status=VerificationStatus.VERIFIED_PRIMARY,
        evidence_ids=("test-evidence",),
    )
    calendar = materialize(synthetic_definition(exceptions=(removed,)))
    assert all(window.trading_date.value != date(2023, 3, 8) for window in calendar.windows)


def test_replaced_exception_overrides_the_weekly_template_completely() -> None:
    replaced = DatedException(
        trading_date=date(2023, 3, 8),
        kind="replaced",
        windows=(
            ExceptionWindow(
                state=ExchangeTradingState.MAINTENANCE,
                start_date=date(2023, 3, 8),
                start_time=time(7, 0),
                end_date=date(2023, 3, 8),
                end_time=time(8, 0),
            ),
            ExceptionWindow(
                state=ExchangeTradingState.TRADING,
                start_date=date(2023, 3, 8),
                start_time=time(8, 0),
                end_date=date(2023, 3, 8),
                end_time=time(12, 0),
            ),
        ),
        provenance_status=VerificationStatus.VERIFIED_PRIMARY,
        evidence_ids=("test-evidence",),
    )
    calendar = materialize(synthetic_definition(exceptions=(replaced,)))
    that_day = [w for w in calendar.windows if w.trading_date.value == date(2023, 3, 8)]
    assert [w.state for w in that_day] == [
        ExchangeTradingState.MAINTENANCE,
        ExchangeTradingState.TRADING,
    ]
    assert that_day[0].start == datetime(2023, 3, 8, 13, 0, tzinfo=UTC)
    assert that_day[1].end == datetime(2023, 3, 8, 18, 0, tzinfo=UTC)


def test_maintenance_windows_resolve_as_maintenance_not_closed() -> None:
    """A synthetic maintenance interval must never be reported as CLOSED."""
    from quant_research_terminal.domain.exchange_calendar import CalendarResolver

    replaced = DatedException(
        trading_date=date(2023, 3, 8),
        kind="replaced",
        windows=(
            ExceptionWindow(
                state=ExchangeTradingState.TRADING,
                start_date=date(2023, 3, 8),
                start_time=time(8, 0),
                end_date=date(2023, 3, 8),
                end_time=time(12, 0),
            ),
            ExceptionWindow(
                state=ExchangeTradingState.MAINTENANCE,
                start_date=date(2023, 3, 8),
                start_time=time(12, 0),
                end_date=date(2023, 3, 8),
                end_time=time(13, 0),
            ),
            ExceptionWindow(
                state=ExchangeTradingState.TRADING,
                start_date=date(2023, 3, 8),
                start_time=time(13, 0),
                end_date=date(2023, 3, 8),
                end_time=time(15, 0),
            ),
        ),
        provenance_status=VerificationStatus.VERIFIED_PRIMARY,
        evidence_ids=("test-evidence",),
    )
    resolver = CalendarResolver(materialize(synthetic_definition(exceptions=(replaced,))))
    context = resolver.resolve(datetime(2023, 3, 8, 18, 30, tzinfo=UTC))  # 12:30 CST
    assert context.state == ExchangeTradingState.MAINTENANCE
    assert context.trading_date is not None
    assert context.trading_date.value == date(2023, 3, 8)
