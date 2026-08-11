"""Boundary semantics, total coverage, and trading-date segmentation.

The properties under attack:

* half-open roll boundaries, asserted at one microsecond either side;
* total, single-valued coverage of the supported range;
* a roll inside a trading date must produce *ordered segments*, never one
  arbitrarily chosen contract;
* segments must tile their windows exactly, and forward and inverse lookups
  must agree.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pytest
from rollover_fixtures import schedule_over_calendar
from synthetic_calendars import synthetic_resolver

from quant_research_terminal.domain.contract_month import ContractMonth
from quant_research_terminal.domain.exchange_calendar import (
    CalendarResolver,
    ExchangeTradingState,
    TradingDate,
)
from quant_research_terminal.domain.futures_contract import (
    FuturesContractId,
    FuturesProduct,
    Venue,
    require_listed_contract,
)
from quant_research_terminal.domain.rollover import (
    CalendarMismatchError,
    RollCalendarRangeError,
    RollDerivationKind,
    RollProvenance,
    RollResolver,
    RollSchedule,
    RollScheduleRangeError,
    UnsupportedRollTimestampError,
    segments_for_trading_date,
)

MICRO = timedelta(microseconds=1)
ES = FuturesProduct(venue=Venue(code="CME"), root="ES")
PROVENANCE = RollProvenance(
    derivation_kind=RollDerivationKind.OPERATOR_DECLARED, evidence_ids=("note-1",)
)


def contract(month: ContractMonth, year: int = 2024) -> FuturesContractId:
    return FuturesContractId(product=ES, contract_month=month, contract_year=year)


H = contract(ContractMonth.MARCH)
M = contract(ContractMonth.JUNE)
U = contract(ContractMonth.SEPTEMBER)


@pytest.fixture(scope="module")
def calendar_resolver() -> CalendarResolver:
    return synthetic_resolver(first=date(2024, 1, 1), last=date(2024, 3, 29))


def build_schedule(
    calendar_resolver: CalendarResolver,
    *,
    rolls: tuple[tuple[FuturesContractId, FuturesContractId, datetime], ...],
    supported_start: datetime | None = None,
    supported_end: datetime | None = None,
) -> RollSchedule:
    return schedule_over_calendar(
        calendar_resolver.calendar,
        product=ES,
        initial_contract=H,
        rolls=rolls,
        supported_start=supported_start,
        supported_end=supported_end,
    )


# --------------------------------------------------------------------------
# Half-open boundaries at microsecond resolution
# --------------------------------------------------------------------------


def test_roll_boundary_is_half_open(calendar_resolver: CalendarResolver) -> None:
    roll_at = datetime(2024, 2, 14, 15, 0, tzinfo=UTC)
    resolver = RollResolver(build_schedule(calendar_resolver, rolls=((H, M, roll_at),)))
    assert resolver.active_contract_at(roll_at - MICRO) == H
    assert resolver.active_contract_at(roll_at) == M
    assert resolver.active_contract_at(roll_at + MICRO) == M


def test_the_first_supported_instant_holds_the_initial_contract(
    calendar_resolver: CalendarResolver,
) -> None:
    schedule = build_schedule(
        calendar_resolver, rolls=((H, M, datetime(2024, 2, 14, 15, 0, tzinfo=UTC)),)
    )
    assert RollResolver(schedule).active_contract_at(schedule.supported_start) == H


def test_the_last_supported_microsecond_is_answerable_and_the_end_is_not(
    calendar_resolver: CalendarResolver,
) -> None:
    schedule = build_schedule(calendar_resolver, rolls=())
    resolver = RollResolver(schedule)
    assert resolver.supports(schedule.supported_end - MICRO)
    assert resolver.active_contract_at(schedule.supported_end - MICRO) == H
    assert not resolver.supports(schedule.supported_end)
    with pytest.raises(UnsupportedRollTimestampError):
        resolver.active_contract_at(schedule.supported_end)


def test_instants_before_the_range_are_unsupported(calendar_resolver: CalendarResolver) -> None:
    schedule = build_schedule(calendar_resolver, rolls=())
    resolver = RollResolver(schedule)
    with pytest.raises(UnsupportedRollTimestampError):
        resolver.active_contract_at(schedule.supported_start - MICRO)


def test_coverage_is_total_and_single_valued(calendar_resolver: CalendarResolver) -> None:
    """Every instant in the range answers exactly one listed contract."""
    rolls = (
        (H, M, datetime(2024, 1, 25, 15, 0, tzinfo=UTC)),
        (M, U, datetime(2024, 2, 26, 15, 0, tzinfo=UTC)),
    )
    schedule = build_schedule(calendar_resolver, rolls=rolls)
    resolver = RollResolver(schedule)
    instant = schedule.supported_start
    step = timedelta(hours=7)
    seen: list[FuturesContractId] = []
    while instant < schedule.supported_end:
        answer = resolver.active_contract_at(instant)
        require_listed_contract(answer)  # never hands out a non-executable thing
        seen.append(answer)
        instant += step
    assert seen[0] == H
    assert seen[-1] == U
    # The mapping is monotone through the chain: it never returns to a
    # contract it has left.
    ordered = [c for index, c in enumerate(seen) if index == 0 or c != seen[index - 1]]
    assert ordered == [H, M, U]


# --------------------------------------------------------------------------
# Trading-date segmentation
# --------------------------------------------------------------------------


def test_a_trading_date_with_no_roll_is_one_contract_across_its_windows(
    calendar_resolver: CalendarResolver,
) -> None:
    schedule = build_schedule(calendar_resolver, rolls=())
    segments = segments_for_trading_date(
        RollResolver(schedule), calendar_resolver, TradingDate.from_iso("2024-01-10")
    )
    assert {segment.contract for segment in segments} == {H}
    assert [segment.state for segment in segments] == [
        ExchangeTradingState.HALT,
        ExchangeTradingState.TRADING,
    ]
    assert all(segment.roll_rule_key is None for segment in segments)


def test_an_intraday_roll_produces_two_ordered_segments(
    calendar_resolver: CalendarResolver,
) -> None:
    """The adversarial case: a roll inside one trading date must not collapse."""
    trading_date = TradingDate.from_iso("2024-01-10")
    roll_at = datetime(2024, 1, 10, 15, 0, tzinfo=UTC)  # inside the main session
    schedule = build_schedule(calendar_resolver, rolls=((H, M, roll_at),))
    segments = segments_for_trading_date(RollResolver(schedule), calendar_resolver, trading_date)

    assert [(s.contract, s.state) for s in segments] == [
        (H, ExchangeTradingState.HALT),
        (H, ExchangeTradingState.TRADING),
        (M, ExchangeTradingState.TRADING),
    ]
    # The session window is split exactly at the roll, and the pre-open on the
    # PREVIOUS civil day still belongs to this trading date.
    assert segments[0].start == datetime(2024, 1, 9, 22, 45, tzinfo=UTC)
    assert segments[1].start == datetime(2024, 1, 9, 23, 0, tzinfo=UTC)
    assert segments[1].end == roll_at
    assert segments[2].start == roll_at
    assert segments[2].end == datetime(2024, 1, 10, 22, 0, tzinfo=UTC)
    assert segments[1].roll_rule_key is None
    assert segments[2].roll_rule_key == "explicit.0"


def test_two_rolls_inside_one_window_produce_three_segments(
    calendar_resolver: CalendarResolver,
) -> None:
    rolls = (
        (H, M, datetime(2024, 1, 10, 12, 0, tzinfo=UTC)),
        (M, U, datetime(2024, 1, 10, 18, 0, tzinfo=UTC)),
    )
    schedule = build_schedule(calendar_resolver, rolls=rolls)
    segments = segments_for_trading_date(
        RollResolver(schedule), calendar_resolver, TradingDate.from_iso("2024-01-10")
    )
    trading = [s for s in segments if s.state is ExchangeTradingState.TRADING]
    assert [s.contract for s in trading] == [H, M, U]


def test_a_roll_exactly_at_a_window_boundary_emits_no_zero_length_segment(
    calendar_resolver: CalendarResolver,
) -> None:
    boundary = datetime(2024, 1, 9, 23, 0, tzinfo=UTC)  # the session open
    schedule = build_schedule(calendar_resolver, rolls=((H, M, boundary),))
    segments = segments_for_trading_date(
        RollResolver(schedule), calendar_resolver, TradingDate.from_iso("2024-01-10")
    )
    assert all(segment.start < segment.end for segment in segments)
    assert [(s.contract, s.state) for s in segments] == [
        (H, ExchangeTradingState.HALT),
        (M, ExchangeTradingState.TRADING),
    ]


def test_segments_tile_their_windows_and_agree_with_the_forward_lookup(
    calendar_resolver: CalendarResolver,
) -> None:
    rolls = ((H, M, datetime(2024, 1, 10, 15, 0, tzinfo=UTC)),)
    schedule = build_schedule(calendar_resolver, rolls=rolls)
    roll_resolver = RollResolver(schedule)
    for iso in ("2024-01-09", "2024-01-10", "2024-01-11"):
        trading_date = TradingDate.from_iso(iso)
        windows = calendar_resolver.windows_for(trading_date)
        segments = segments_for_trading_date(roll_resolver, calendar_resolver, trading_date)
        # Exact tiling, window by window.
        for window in windows:
            covering = [s for s in segments if window.start <= s.start < window.end]
            assert covering[0].start == window.start
            assert covering[-1].end == window.end
            for earlier, later in zip(covering, covering[1:], strict=False):
                assert earlier.end == later.start
        # Forward and inverse agree at every segment's midpoint.
        for segment in segments:
            midpoint = segment.start + (segment.end - segment.start) / 2
            assert roll_resolver.active_contract_at(midpoint) == segment.contract


def test_a_roll_inside_a_closed_gap_appears_in_no_segment() -> None:
    """Documented behaviour, now pinned: a trading date's segments may not tile.

    If a roll lands in a closed break *between* two windows of one trading
    date, the window before is wholly the old contract and the window after
    wholly the new — and the roll instant itself lies in no segment. A consumer
    that assumed the segments were contiguous would misplace it.
    """
    from datetime import time

    from synthetic_calendars import SYNTHETIC_EVIDENCE, WEEKDAYS

    from quant_research_terminal.domain.calendar_definition import (
        CalendarDefinition,
        WeeklyWindowRule,
    )
    from quant_research_terminal.domain.calendar_materialization import materialize
    from quant_research_terminal.domain.exchange_calendar import (
        CalendarId,
        CalendarVersion,
        VerificationStatus,
    )

    def window(start: time, end: time) -> WeeklyWindowRule:
        return WeeklyWindowRule(
            state=ExchangeTradingState.TRADING,
            start_day_offset=0,
            start_time=start,
            end_day_offset=0,
            end_time=end,
            provenance_status=VerificationStatus.VERIFIED_PRIMARY,
            evidence_ids=("synthetic-evidence",),
        )

    split_session = (window(time(8, 0), time(11, 0)), window(time(13, 0), time(16, 0)))
    calendar = materialize(
        CalendarDefinition(
            calendar_id=CalendarId(token="SYNTHETIC_GAP"),
            version=CalendarVersion(number=1),
            timezone_name="America/Chicago",
            first_trading_date=date(2024, 1, 1),
            last_trading_date=date(2024, 3, 29),
            weekly_rules=tuple((day, split_session) for day in WEEKDAYS),
            exceptions=(),
            timezone_probes=(),
            evidence=(SYNTHETIC_EVIDENCE,),
        )
    )
    resolver = CalendarResolver(calendar)
    roll_at = datetime(2024, 1, 10, 18, 0, tzinfo=UTC)  # 12:00 CST, inside the break
    schedule = schedule_over_calendar(
        calendar, product=ES, initial_contract=H, rolls=((H, M, roll_at),)
    )
    segments = segments_for_trading_date(
        RollResolver(schedule), resolver, TradingDate.from_iso("2024-01-10")
    )
    assert [segment.contract for segment in segments] == [H, M]
    assert not any(segment.start <= roll_at < segment.end for segment in segments)
    assert segments[0].end < roll_at < segments[1].start


def test_a_weekend_returns_no_segments(calendar_resolver: CalendarResolver) -> None:
    schedule = build_schedule(calendar_resolver, rolls=())
    assert (
        segments_for_trading_date(
            RollResolver(schedule), calendar_resolver, TradingDate.from_iso("2024-01-13")
        )
        == ()
    )


def test_a_trading_date_outside_the_schedule_range_raises(
    calendar_resolver: CalendarResolver,
) -> None:
    """Distinguishable from a non-trading date, which returns ``()``."""
    schedule = build_schedule(calendar_resolver, rolls=())
    with pytest.raises(RollScheduleRangeError):
        segments_for_trading_date(
            RollResolver(schedule), calendar_resolver, TradingDate.from_iso("2025-06-02")
        )


def test_a_window_leaving_the_supported_range_is_refused_not_truncated(
    calendar_resolver: CalendarResolver,
) -> None:
    calendar = calendar_resolver.calendar
    schedule = build_schedule(
        calendar_resolver,
        rolls=(),
        supported_start=calendar.coverage_start + timedelta(days=5),
    )
    with pytest.raises(RollScheduleRangeError, match="answered in full or refused"):
        segments_for_trading_date(
            RollResolver(schedule), calendar_resolver, calendar.first_trading_date
        )


def test_segmenting_against_a_different_calendar_is_refused(
    calendar_resolver: CalendarResolver,
) -> None:
    schedule = build_schedule(calendar_resolver, rolls=())
    other = synthetic_resolver(first=date(2024, 1, 1), last=date(2024, 3, 29), version=2)
    with pytest.raises(CalendarMismatchError):
        segments_for_trading_date(RollResolver(schedule), other, TradingDate.from_iso("2024-01-10"))


def test_a_calendar_narrower_than_the_schedule_raises_a_rollover_error(
    calendar_resolver: CalendarResolver,
) -> None:
    """The calendar's own error is wrapped, so one call raises one vocabulary."""
    schedule = schedule_over_calendar(
        calendar_resolver.calendar,
        product=ES,
        initial_contract=H,
        first_trading_date=TradingDate.from_iso("2023-01-02"),
    )
    with pytest.raises(RollCalendarRangeError):
        segments_for_trading_date(
            RollResolver(schedule), calendar_resolver, TradingDate.from_iso("2023-01-03")
        )


def test_segments_reject_wrong_argument_types(calendar_resolver: CalendarResolver) -> None:
    schedule = build_schedule(calendar_resolver, rolls=())
    resolver = RollResolver(schedule)
    with pytest.raises(TypeError):
        segments_for_trading_date(schedule, calendar_resolver, TradingDate.from_iso("2024-01-10"))  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        segments_for_trading_date(
            resolver,
            calendar_resolver.calendar,  # type: ignore[arg-type]
            TradingDate.from_iso("2024-01-10"),
        )
    with pytest.raises(TypeError):
        segments_for_trading_date(resolver, calendar_resolver, date(2024, 1, 10))  # type: ignore[arg-type]
