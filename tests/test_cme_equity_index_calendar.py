"""Evidence-driven tests for the CME_EQUITY_INDEX v1 calendar.

Every factual instant asserted here traces to the evidence records in
``cme_equity_index_v1.toml`` and the register in ``docs/calendar-evidence.md``:
CME's 2023 per-holiday Globex schedules (EQUITIES rows, U.S. Central Time) and
the CME Micro E-mini FAQ trading-hours statement. Expected UTC values are the
CT wall times converted at CDT (UTC-5) or CST (UTC-6) as the date requires —
the November DST transition falls inside the supported range on purpose.

The one microsecond on either side of every asserted boundary is load-bearing:
windows are half-open ``[start, end)``, so the boundary instant itself always
belongs to the *following* state.
"""

from __future__ import annotations

import os
import subprocess
import sys
from datetime import UTC, date, datetime, timedelta

import pytest

from quant_research_terminal.calendars import load_cme_equity_index_v1
from quant_research_terminal.domain.calendar_materialization import materialize
from quant_research_terminal.domain.exchange_calendar import (
    CalendarRangeError,
    CalendarResolver,
    ExchangeTradingState,
    MaterializedCalendar,
    TradingDate,
    UnsupportedTimestampError,
    VerificationStatus,
)

#: Regression pin for the released v1 materialization. If this test fails, the
#: definition's semantics changed: that is a correction, and corrections are
#: new versions with new hashes — v1 is immutable. Do not update this constant
#: without bumping the calendar version and recording why.
V1_CONTENT_HASH = "aa2dde0ac4e66480f5125d84048516ce6c5b3a680dcad5c2a3d222d68828c6e2"

MICROSECOND = timedelta(microseconds=1)


@pytest.fixture(scope="module")
def calendar() -> MaterializedCalendar:
    return materialize(load_cme_equity_index_v1())


@pytest.fixture(scope="module")
def resolver(calendar: MaterializedCalendar) -> CalendarResolver:
    return CalendarResolver(calendar)


def state_at(resolver: CalendarResolver, instant: datetime) -> ExchangeTradingState:
    return resolver.resolve(instant).state


def trading_date_at(resolver: CalendarResolver, instant: datetime) -> date | None:
    context = resolver.resolve(instant)
    return context.trading_date.value if context.trading_date else None


# --------------------------------------------------------------------------
# Coverage and version pinning
# --------------------------------------------------------------------------


def test_coverage_matches_the_supported_range(calendar: MaterializedCalendar) -> None:
    # First covered instant: Sunday 2023-05-21 16:00 CDT pre-open (21:00 UTC).
    assert calendar.coverage_start == datetime(2023, 5, 21, 21, 0, tzinfo=UTC)
    # Last covered instant ends at Friday 2023-12-29 16:00 CST (22:00 UTC).
    assert calendar.coverage_end == datetime(2023, 12, 29, 22, 0, tzinfo=UTC)
    assert calendar.first_trading_date.canonical() == "2023-05-22"
    assert calendar.last_trading_date.canonical() == "2023-12-29"


def test_v1_content_hash_is_pinned(calendar: MaterializedCalendar) -> None:
    assert calendar.content_hash == V1_CONTENT_HASH


def test_rematerialization_is_identical(calendar: MaterializedCalendar) -> None:
    again = materialize(load_cme_equity_index_v1())
    assert again == calendar
    assert again.content_hash == calendar.content_hash


# --------------------------------------------------------------------------
# Normal weekday: half-open boundaries at one-microsecond resolution
# --------------------------------------------------------------------------

# Wednesday 2023-06-14 (CDT, UTC-5): pre-open 21:45Z, open 22:00Z (previous
# day), halt 20:15-20:30Z, final close 21:00Z.
WED_PREOPEN = datetime(2023, 6, 13, 21, 45, tzinfo=UTC)
WED_OPEN = datetime(2023, 6, 13, 22, 0, tzinfo=UTC)
WED_HALT_START = datetime(2023, 6, 14, 20, 15, tzinfo=UTC)
WED_HALT_END = datetime(2023, 6, 14, 20, 30, tzinfo=UTC)
WED_CLOSE = datetime(2023, 6, 14, 21, 0, tzinfo=UTC)


def test_open_boundary_is_half_open(resolver: CalendarResolver) -> None:
    assert state_at(resolver, WED_OPEN - MICROSECOND) == ExchangeTradingState.HALT
    assert state_at(resolver, WED_OPEN) == ExchangeTradingState.TRADING
    assert state_at(resolver, WED_OPEN + MICROSECOND) == ExchangeTradingState.TRADING


def test_halt_start_boundary_is_half_open(resolver: CalendarResolver) -> None:
    assert state_at(resolver, WED_HALT_START - MICROSECOND) == ExchangeTradingState.TRADING
    assert state_at(resolver, WED_HALT_START) == ExchangeTradingState.HALT
    assert state_at(resolver, WED_HALT_START + MICROSECOND) == ExchangeTradingState.HALT


def test_halt_end_boundary_is_half_open(resolver: CalendarResolver) -> None:
    assert state_at(resolver, WED_HALT_END - MICROSECOND) == ExchangeTradingState.HALT
    assert state_at(resolver, WED_HALT_END) == ExchangeTradingState.TRADING


def test_close_boundary_is_half_open(resolver: CalendarResolver) -> None:
    assert state_at(resolver, WED_CLOSE - MICROSECOND) == ExchangeTradingState.TRADING
    assert state_at(resolver, WED_CLOSE) == ExchangeTradingState.CLOSED
    assert state_at(resolver, WED_CLOSE + MICROSECOND) == ExchangeTradingState.CLOSED


def test_preopen_boundary_is_half_open(resolver: CalendarResolver) -> None:
    assert state_at(resolver, WED_PREOPEN - MICROSECOND) == ExchangeTradingState.CLOSED
    assert state_at(resolver, WED_PREOPEN) == ExchangeTradingState.HALT


def test_the_whole_session_carries_one_trading_date(resolver: CalendarResolver) -> None:
    for instant in (WED_OPEN, WED_HALT_START, WED_HALT_END, WED_CLOSE - MICROSECOND):
        assert trading_date_at(resolver, instant) == date(2023, 6, 14)


def test_next_transition_is_the_window_end(resolver: CalendarResolver) -> None:
    context = resolver.resolve(WED_OPEN)
    assert context.next_transition == WED_HALT_START
    closed = resolver.resolve(WED_CLOSE)
    # The gap after Wednesday's close ends at Thursday's 16:45 CDT pre-open.
    assert closed.next_transition == datetime(2023, 6, 14, 21, 45, tzinfo=UTC)


# --------------------------------------------------------------------------
# Sunday evening and the trading-date chain (amendment category E)
# --------------------------------------------------------------------------


def test_a_normal_sunday_evening_belongs_to_monday(resolver: CalendarResolver) -> None:
    # Sunday 2023-06-11 17:30 CDT (22:30 UTC): the UTC civil date and the
    # exchange-local civil date are both 2023-06-11 — the trading date is
    # neither; it is Monday 2023-06-12, assigned by the calendar.
    instant = datetime(2023, 6, 11, 22, 30, tzinfo=UTC)
    assert state_at(resolver, instant) == ExchangeTradingState.TRADING
    assert trading_date_at(resolver, instant) == date(2023, 6, 12)
    assert instant.date() != date(2023, 6, 12)


def test_a_pre_holiday_sunday_evening_belongs_to_tuesday(resolver: CalendarResolver) -> None:
    # Sunday 2023-05-28 17:30 CDT: Monday 29 May was Memorial Day, so CME's
    # own table assigns TRADE DATE TUES 30 MAY. Weekday arithmetic (next
    # weekday = Monday) produces the wrong answer by construction.
    instant = datetime(2023, 5, 28, 22, 30, tzinfo=UTC)
    assert state_at(resolver, instant) == ExchangeTradingState.TRADING
    assert trading_date_at(resolver, instant) == date(2023, 5, 30)


def test_holiday_monday_morning_trades_on_tuesdays_date(resolver: CalendarResolver) -> None:
    # Memorial Day Monday 09:00 CDT (14:00 UTC): physically trading, and the
    # prints belong to Tuesday's trade date.
    instant = datetime(2023, 5, 29, 14, 0, tzinfo=UTC)
    assert state_at(resolver, instant) == ExchangeTradingState.TRADING
    assert trading_date_at(resolver, instant) == date(2023, 5, 30)


def test_holiday_monday_afternoon_is_a_halt_not_closed(resolver: CalendarResolver) -> None:
    # Memorial Day Monday 13:00 CDT (18:00 UTC): PREOPEN/HALT per CME, still
    # Tuesday's trade date, and emphatically not CLOSED.
    instant = datetime(2023, 5, 29, 18, 0, tzinfo=UTC)
    assert state_at(resolver, instant) == ExchangeTradingState.HALT
    assert trading_date_at(resolver, instant) == date(2023, 5, 30)


def test_holiday_early_close_boundary(resolver: CalendarResolver) -> None:
    # Monday 2023-07-03 closed early at 12:15 CDT (17:15 UTC).
    early_close = datetime(2023, 7, 3, 17, 15, tzinfo=UTC)
    assert state_at(resolver, early_close - MICROSECOND) == ExchangeTradingState.TRADING
    assert trading_date_at(resolver, early_close - MICROSECOND) == date(2023, 7, 3)
    assert state_at(resolver, early_close) == ExchangeTradingState.CLOSED
    assert trading_date_at(resolver, early_close) is None
    # The 16:45 CDT pre-open that follows (21:45 UTC) belongs to Wednesday
    # 5 July: Tuesday 4 July is not a trading date at all.
    preopen = datetime(2023, 7, 3, 21, 45, tzinfo=UTC)
    assert state_at(resolver, preopen) == ExchangeTradingState.HALT
    assert trading_date_at(resolver, preopen) == date(2023, 7, 5)


def test_thanksgiving_friday_early_close_in_cst(resolver: CalendarResolver) -> None:
    # Friday 2023-11-24 closed at 12:15 CST (18:15 UTC) — CST because the
    # November DST transition is inside the supported range.
    early_close = datetime(2023, 11, 24, 18, 15, tzinfo=UTC)
    assert state_at(resolver, early_close - MICROSECOND) == ExchangeTradingState.TRADING
    assert trading_date_at(resolver, early_close - MICROSECOND) == date(2023, 11, 24)
    assert state_at(resolver, early_close) == ExchangeTradingState.CLOSED


def test_christmas_eve_sunday_has_no_session(resolver: CalendarResolver) -> None:
    # Sunday 2023-12-24 17:30 CST (23:30 UTC) would be open on a normal
    # Sunday; CME's Christmas schedule shows no session before Monday 16:00.
    assert state_at(resolver, datetime(2023, 12, 24, 23, 30, tzinfo=UTC)) == (
        ExchangeTradingState.CLOSED
    )


def test_christmas_monday_evening_belongs_to_tuesday(resolver: CalendarResolver) -> None:
    # Monday 2023-12-25 17:30 CST (23:30 UTC): open, trade date Tuesday.
    instant = datetime(2023, 12, 25, 23, 30, tzinfo=UTC)
    assert state_at(resolver, instant) == ExchangeTradingState.TRADING
    assert trading_date_at(resolver, instant) == date(2023, 12, 26)


# --------------------------------------------------------------------------
# Weekend closure
# --------------------------------------------------------------------------


def test_saturday_is_closed_with_no_trading_date(resolver: CalendarResolver) -> None:
    context = resolver.resolve(datetime(2023, 6, 17, 12, 0, tzinfo=UTC))
    assert context.state == ExchangeTradingState.CLOSED
    assert context.trading_date is None
    assert context.verification is None
    # The closed gap spans Friday's close to Sunday's pre-open, exactly.
    assert context.window_start == datetime(2023, 6, 16, 21, 0, tzinfo=UTC)
    assert context.window_end == datetime(2023, 6, 18, 21, 0, tzinfo=UTC)


def test_friday_close_boundary_enters_the_weekend(resolver: CalendarResolver) -> None:
    friday_close = datetime(2023, 6, 16, 21, 0, tzinfo=UTC)
    assert state_at(resolver, friday_close - MICROSECOND) == ExchangeTradingState.TRADING
    assert state_at(resolver, friday_close) == ExchangeTradingState.CLOSED


# --------------------------------------------------------------------------
# DST inside the range: the November fall-back
# --------------------------------------------------------------------------


def test_the_friday_before_fall_back_closes_on_cdt(resolver: CalendarResolver) -> None:
    # Friday 2023-11-03 16:00 CDT = 21:00 UTC.
    assert (
        state_at(resolver, datetime(2023, 11, 3, 21, 0, tzinfo=UTC) - MICROSECOND)
        == ExchangeTradingState.TRADING
    )
    assert state_at(resolver, datetime(2023, 11, 3, 21, 0, tzinfo=UTC)) == (
        ExchangeTradingState.CLOSED
    )


def test_the_sunday_after_fall_back_opens_on_cst(resolver: CalendarResolver) -> None:
    # Sunday 2023-11-05: pre-open 16:00 CST = 22:00 UTC (an hour later in UTC
    # than the previous Sunday), open 17:00 CST = 23:00 UTC.
    preopen = datetime(2023, 11, 5, 22, 0, tzinfo=UTC)
    open_instant = datetime(2023, 11, 5, 23, 0, tzinfo=UTC)
    assert state_at(resolver, preopen - MICROSECOND) == ExchangeTradingState.CLOSED
    assert state_at(resolver, preopen) == ExchangeTradingState.HALT
    assert state_at(resolver, open_instant) == ExchangeTradingState.TRADING
    assert trading_date_at(resolver, open_instant) == date(2023, 11, 6)


def test_the_sunday_before_fall_back_opened_on_cdt(resolver: CalendarResolver) -> None:
    # Sunday 2023-10-29 17:00 CDT = 22:00 UTC: still daylight time.
    assert state_at(resolver, datetime(2023, 10, 29, 22, 0, tzinfo=UTC)) == (
        ExchangeTradingState.TRADING
    )


# --------------------------------------------------------------------------
# Days that are normal because CME's own enumeration lists no deviation
# --------------------------------------------------------------------------


def test_columbus_day_is_a_normal_trading_date(resolver: CalendarResolver) -> None:
    windows = resolver.windows_for(TradingDate.from_iso("2023-10-09"))
    assert [w.state for w in windows] == [
        ExchangeTradingState.HALT,
        ExchangeTradingState.TRADING,
        ExchangeTradingState.HALT,
        ExchangeTradingState.TRADING,
    ]
    assert windows[1].start == datetime(2023, 10, 8, 22, 0, tzinfo=UTC)


def test_veterans_day_friday_is_a_normal_trading_date(resolver: CalendarResolver) -> None:
    assert len(resolver.windows_for(TradingDate.from_iso("2023-11-10"))) == 4


# --------------------------------------------------------------------------
# Inverse lookup
# --------------------------------------------------------------------------


def test_a_holiday_trading_date_spans_multiple_trading_windows(
    resolver: CalendarResolver,
) -> None:
    windows = resolver.windows_for(TradingDate.from_iso("2023-05-30"))
    assert [w.state.value for w in windows] == [
        "halt",
        "trading",
        "halt",
        "trading",
        "halt",
        "trading",
    ]
    trading = [w for w in windows if w.state == ExchangeTradingState.TRADING]
    assert len(trading) == 3
    assert trading[0].start == datetime(2023, 5, 28, 22, 0, tzinfo=UTC)
    assert trading[0].end == datetime(2023, 5, 29, 17, 0, tzinfo=UTC)
    assert trading[-1].end == datetime(2023, 5, 30, 21, 0, tzinfo=UTC)


def test_an_early_close_trading_date_has_two_windows(resolver: CalendarResolver) -> None:
    windows = resolver.windows_for(TradingDate.from_iso("2023-07-03"))
    assert [w.state for w in windows] == [
        ExchangeTradingState.HALT,
        ExchangeTradingState.TRADING,
    ]
    assert windows[-1].end == datetime(2023, 7, 3, 17, 15, tzinfo=UTC)


def test_a_removed_holiday_has_no_windows(resolver: CalendarResolver) -> None:
    assert resolver.windows_for(TradingDate.from_iso("2023-05-29")) == ()
    assert resolver.windows_for(TradingDate.from_iso("2023-07-04")) == ()
    assert resolver.windows_for(TradingDate.from_iso("2023-11-23")) == ()
    assert resolver.windows_for(TradingDate.from_iso("2023-12-25")) == ()


def test_weekends_have_no_windows(resolver: CalendarResolver) -> None:
    assert resolver.windows_for(TradingDate.from_iso("2023-06-17")) == ()
    assert resolver.windows_for(TradingDate.from_iso("2023-06-18")) == ()


def test_out_of_range_trading_dates_raise(resolver: CalendarResolver) -> None:
    with pytest.raises(CalendarRangeError):
        resolver.windows_for(TradingDate.from_iso("2023-05-19"))
    with pytest.raises(CalendarRangeError):
        resolver.windows_for(TradingDate.from_iso("2024-01-02"))


def test_every_weekday_in_range_is_either_a_trading_date_or_an_explicit_removal(
    resolver: CalendarResolver, calendar: MaterializedCalendar
) -> None:
    removed = {
        date(2023, 5, 29),
        date(2023, 6, 19),
        date(2023, 7, 4),
        date(2023, 9, 4),
        date(2023, 11, 23),
        date(2023, 12, 25),
    }
    day = calendar.first_trading_date.value
    while day <= calendar.last_trading_date.value:
        windows = resolver.windows_for(TradingDate(value=day))
        if day.weekday() >= 5 or day in removed:
            assert windows == (), day
        else:
            assert len(windows) >= 2, day
            assert any(w.state == ExchangeTradingState.TRADING for w in windows), day
        day += timedelta(days=1)


# --------------------------------------------------------------------------
# Unsupported range behavior
# --------------------------------------------------------------------------


def test_instants_before_coverage_are_unsupported(resolver: CalendarResolver) -> None:
    before = datetime(2023, 5, 21, 21, 0, tzinfo=UTC) - MICROSECOND
    assert not resolver.supports(before)
    with pytest.raises(UnsupportedTimestampError):
        resolver.resolve(before)


def test_the_coverage_end_instant_is_unsupported(resolver: CalendarResolver) -> None:
    end = datetime(2023, 12, 29, 22, 0, tzinfo=UTC)
    assert resolver.supports(end - MICROSECOND)
    assert not resolver.supports(end)
    with pytest.raises(UnsupportedTimestampError):
        resolver.resolve(end)


def test_a_2024_instant_is_never_extrapolated(resolver: CalendarResolver) -> None:
    with pytest.raises(UnsupportedTimestampError):
        resolver.resolve(datetime(2024, 1, 3, 15, 0, tzinfo=UTC))


# --------------------------------------------------------------------------
# Verification metadata reaches the context, and only as metadata
# --------------------------------------------------------------------------


def test_weekly_trading_windows_are_verified_primary(resolver: CalendarResolver) -> None:
    context = resolver.resolve(WED_OPEN)
    assert context.verification is not None
    assert context.verification.status == VerificationStatus.VERIFIED_PRIMARY
    assert "cme-micro-emini-faq-2023-12" in context.verification.evidence_ids


def test_the_sunday_preopen_is_honestly_corroborated(resolver: CalendarResolver) -> None:
    # The 16:00 Sunday pre-open instances in evidence are all holiday-week
    # Sundays; its application to every Sunday is inferred from consistency,
    # and the metadata says so instead of overclaiming.
    context = resolver.resolve(datetime(2023, 6, 11, 21, 30, tzinfo=UTC))
    assert context.state == ExchangeTradingState.HALT
    assert context.verification is not None
    assert context.verification.status == VerificationStatus.CORROBORATED


def test_exception_windows_are_verified_primary(resolver: CalendarResolver) -> None:
    context = resolver.resolve(datetime(2023, 5, 29, 14, 0, tzinfo=UTC))
    assert context.verification is not None
    assert context.verification.status == VerificationStatus.VERIFIED_PRIMARY
    assert "cme-memorial-day-2023-schedule" in context.verification.evidence_ids


# --------------------------------------------------------------------------
# Structural properties over every materialized window
# --------------------------------------------------------------------------


def test_all_windows_are_ordered_half_open_utc_and_labelled(
    calendar: MaterializedCalendar,
) -> None:
    previous_end: datetime | None = None
    for window in calendar.windows:
        assert window.start < window.end
        assert window.start.tzinfo == UTC
        assert window.end.tzinfo == UTC
        # Whole seconds in this calendar: every authored CT boundary is a
        # whole minute, so sub-second materialized boundaries would mean the
        # materializer invented precision.
        assert window.start.microsecond == 0 and window.start.second == 0
        assert window.end.microsecond == 0 and window.end.second == 0
        if previous_end is not None:
            assert window.start >= previous_end
        previous_end = window.end
        first = calendar.first_trading_date.value
        last = calendar.last_trading_date.value
        assert first <= window.trading_date.value <= last


def test_the_window_count_is_stable(calendar: MaterializedCalendar) -> None:
    assert len(calendar.windows) == 622


# --------------------------------------------------------------------------
# Cross-process determinism: hash seeds and the process timezone
# --------------------------------------------------------------------------

_DETERMINISM_SCRIPT = """
from quant_research_terminal.calendars import load_cme_equity_index_v1
from quant_research_terminal.domain.calendar_materialization import materialize

calendar = materialize(load_cme_equity_index_v1())
print(calendar.content_hash)
print(calendar.windows[0].start.isoformat(), calendar.windows[-1].end.isoformat())
print(len(calendar.windows))
"""


def test_materialization_is_identical_across_hash_seeds_and_process_timezones() -> None:
    outputs = set()
    for seed, zone in (
        ("0", "UTC"),
        ("1", "America/New_York"),
        ("42", "Asia/Tokyo"),
        ("random", "Australia/Eucla"),
    ):
        completed = subprocess.run(  # noqa: S603
            [sys.executable, "-c", _DETERMINISM_SCRIPT],
            capture_output=True,
            text=True,
            check=True,
            env={**os.environ, "PYTHONHASHSEED": seed, "TZ": zone},
        )
        outputs.add(completed.stdout)
    assert len(outputs) == 1
    assert V1_CONTENT_HASH in outputs.pop()
