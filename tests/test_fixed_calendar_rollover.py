"""The calendar-and-lifecycle roll mechanism.

The properties under attack: the count-back must be decided by the calendar
rather than by weekday arithmetic, the effective boundary must be explicit,
a missing or contradictory lifecycle fact must be an error rather than a
guess, and the whole derivation must be deterministic.
"""

from __future__ import annotations

from datetime import UTC, date, datetime

import pytest
from pydantic import ValidationError
from synthetic_calendars import removed_trading_date, synthetic_definition, synthetic_resolver

from quant_research_terminal.domain.calendar_materialization import materialize
from quant_research_terminal.domain.contract_lifecycle import (
    ContractLifecycle,
    LifecycleProvenance,
)
from quant_research_terminal.domain.contract_month import ContractMonth
from quant_research_terminal.domain.exchange_calendar import (
    CalendarResolver,
    TradingDate,
)
from quant_research_terminal.domain.futures_contract import (
    FuturesContractId,
    FuturesProduct,
    Venue,
)
from quant_research_terminal.domain.rollover import (
    CalendarMismatchError,
    InsufficientCalendarCoverageError,
    LifecycleCalendarMismatchError,
    NoTradingWindowError,
    RollDerivationKind,
    RollMaterializationError,
)
from quant_research_terminal.domain.rollover_definition import (
    EffectiveBoundary,
    FixedCalendarRollDefinition,
)
from quant_research_terminal.domain.rollover_materialization import (
    materialize_fixed_calendar,
    roll_effective_instant,
    roll_target_trading_date,
)

ES = FuturesProduct(venue=Venue(code="CME"), root="ES")
NQ = FuturesProduct(venue=Venue(code="CME"), root="NQ")
PROVENANCE = LifecycleProvenance(evidence_ids=("synthetic-lifecycle",))


def contract(
    month: ContractMonth, year: int = 2024, product: FuturesProduct = ES
) -> FuturesContractId:
    return FuturesContractId(product=product, contract_month=month, contract_year=year)


def lifecycle(month: ContractMonth, ltd: str, product: FuturesProduct = ES) -> ContractLifecycle:
    return ContractLifecycle(
        contract=contract(month, product=product),
        last_trade_date=TradingDate.from_iso(ltd),
        provenance=PROVENANCE,
    )


@pytest.fixture(scope="module")
def resolver() -> CalendarResolver:
    return synthetic_resolver(first=date(2024, 1, 1), last=date(2024, 3, 29))


@pytest.fixture(scope="module")
def holiday_resolver() -> CalendarResolver:
    """A calendar whose Monday 2024-02-19 is not a trading date."""
    definition = synthetic_definition(
        first=date(2024, 1, 1),
        last=date(2024, 3, 29),
        exceptions=(removed_trading_date(date(2024, 2, 19)),),
        token="SYNTHETIC_HOLIDAY",
    )
    return CalendarResolver(materialize(definition))


def definition_for(
    resolver: CalendarResolver,
    *,
    lifecycles: tuple[ContractLifecycle, ...],
    offset: int = 1,
    boundary: EffectiveBoundary = EffectiveBoundary.FIRST_TRADING_WINDOW_START,
    product: FuturesProduct = ES,
) -> FixedCalendarRollDefinition:
    calendar = resolver.calendar
    return FixedCalendarRollDefinition(
        product=product,
        calendar_id=calendar.calendar_id,
        calendar_version=calendar.version,
        calendar_content_hash=calendar.content_hash,
        supported_start=calendar.coverage_start,
        supported_end=calendar.coverage_end,
        first_trading_date=calendar.first_trading_date,
        last_trading_date=calendar.last_trading_date,
        lifecycles=lifecycles,
        roll_offset_trading_dates=offset,
        effective_boundary=boundary,
    )


# --------------------------------------------------------------------------
# Counting back trading dates — the calendar decides, not arithmetic
# --------------------------------------------------------------------------


def test_offset_zero_returns_the_last_trade_date(resolver: CalendarResolver) -> None:
    target = roll_target_trading_date(
        resolver, last_trade_date=TradingDate.from_iso("2024-01-17"), offset=0
    )
    assert target == TradingDate.from_iso("2024-01-17")


def test_offset_one_returns_the_previous_trading_date(resolver: CalendarResolver) -> None:
    target = roll_target_trading_date(
        resolver, last_trade_date=TradingDate.from_iso("2024-01-17"), offset=1
    )
    assert target == TradingDate.from_iso("2024-01-16")


def test_the_count_back_skips_a_weekend(resolver: CalendarResolver) -> None:
    """Monday minus one trading date is Friday, and no weekday rule says so."""
    target = roll_target_trading_date(
        resolver, last_trade_date=TradingDate.from_iso("2024-01-15"), offset=1
    )
    assert target == TradingDate.from_iso("2024-01-12")


def test_the_count_back_skips_a_holiday_and_a_weekend(
    holiday_resolver: CalendarResolver,
) -> None:
    """Wed 2024-02-21 minus two trading dates, with Monday removed, is Friday 02-16."""
    target = roll_target_trading_date(
        holiday_resolver, last_trade_date=TradingDate.from_iso("2024-02-21"), offset=2
    )
    assert target == TradingDate.from_iso("2024-02-16")


def test_a_last_trade_date_the_calendar_calls_a_non_trading_date_is_a_contradiction(
    holiday_resolver: CalendarResolver,
) -> None:
    """Never nudged to a neighbouring date: two authored facts disagree."""
    with pytest.raises(LifecycleCalendarMismatchError):
        roll_target_trading_date(
            holiday_resolver, last_trade_date=TradingDate.from_iso("2024-02-19"), offset=1
        )


def test_a_weekend_last_trade_date_is_a_contradiction(resolver: CalendarResolver) -> None:
    with pytest.raises(LifecycleCalendarMismatchError):
        roll_target_trading_date(
            resolver, last_trade_date=TradingDate.from_iso("2024-01-13"), offset=1
        )


def test_counting_past_the_calendar_start_is_unsupported_not_clamped(
    resolver: CalendarResolver,
) -> None:
    with pytest.raises(InsufficientCalendarCoverageError):
        roll_target_trading_date(
            resolver, last_trade_date=TradingDate.from_iso("2024-01-03"), offset=50
        )


def test_a_last_trade_date_outside_the_calendar_raises(resolver: CalendarResolver) -> None:
    from quant_research_terminal.domain.rollover import RollCalendarRangeError

    with pytest.raises(RollCalendarRangeError):
        roll_target_trading_date(
            resolver, last_trade_date=TradingDate.from_iso("2025-01-06"), offset=1
        )


@pytest.mark.parametrize("offset", [-1, True, 1.0, "1"])
def test_a_bad_offset_is_rejected(resolver: CalendarResolver, offset: object) -> None:
    with pytest.raises(RollMaterializationError):
        roll_target_trading_date(
            resolver,
            last_trade_date=TradingDate.from_iso("2024-01-17"),
            offset=offset,  # type: ignore[arg-type]
        )


def test_the_count_back_requires_an_exact_trading_date(resolver: CalendarResolver) -> None:
    with pytest.raises(TypeError):
        roll_target_trading_date(resolver, last_trade_date=date(2024, 1, 17), offset=1)  # type: ignore[arg-type]


# --------------------------------------------------------------------------
# Effective boundary policies — explicit, with no fallback
# --------------------------------------------------------------------------


def test_the_two_boundary_policies_give_different_instants(
    resolver: CalendarResolver,
) -> None:
    trading_date = TradingDate.from_iso("2024-01-12")
    first_window = roll_effective_instant(
        resolver, trading_date=trading_date, boundary=EffectiveBoundary.FIRST_WINDOW_START
    )
    first_trading = roll_effective_instant(
        resolver,
        trading_date=trading_date,
        boundary=EffectiveBoundary.FIRST_TRADING_WINDOW_START,
    )
    # The pre-open starts on the PREVIOUS civil day; the open is 15 minutes later.
    assert first_window == datetime(2024, 1, 11, 22, 45, tzinfo=UTC)
    assert first_trading == datetime(2024, 1, 11, 23, 0, tzinfo=UTC)
    assert first_window < first_trading


def test_a_trading_date_with_no_trading_window_has_no_fallback() -> None:
    """FIRST_TRADING_WINDOW_START must raise rather than become windows[0]."""
    from quant_research_terminal.domain.calendar_definition import WeeklyWindowRule
    from quant_research_terminal.domain.exchange_calendar import (
        ExchangeTradingState,
        VerificationStatus,
    )

    halt_only = (
        WeeklyWindowRule(
            state=ExchangeTradingState.HALT,
            start_day_offset=0,
            start_time=__import__("datetime").time(9, 0),
            end_day_offset=0,
            end_time=__import__("datetime").time(16, 0),
            provenance_status=VerificationStatus.VERIFIED_PRIMARY,
            evidence_ids=("synthetic-evidence",),
        ),
    )
    halt_resolver = CalendarResolver(
        materialize(
            synthetic_definition(
                first=date(2024, 1, 1),
                last=date(2024, 1, 31),
                weekday_rules=halt_only,
                token="SYNTHETIC_HALT_ONLY",
            )
        )
    )
    with pytest.raises(NoTradingWindowError):
        roll_effective_instant(
            halt_resolver,
            trading_date=TradingDate.from_iso("2024-01-10"),
            boundary=EffectiveBoundary.FIRST_TRADING_WINDOW_START,
        )


def test_an_equal_valued_string_is_not_an_effective_boundary(
    resolver: CalendarResolver,
) -> None:
    """Falsification pass 2: `EffectiveBoundary` is a StrEnum.

    ``"first-window-start" == EffectiveBoundary.FIRST_WINDOW_START`` is True
    while ``is`` is False, so a bare string used to fall through the identity
    test and silently select the *other* policy — a different instant, quietly.
    """
    trading_date = TradingDate.from_iso("2024-01-12")
    with pytest.raises(TypeError, match="not an equal-valued string"):
        roll_effective_instant(
            resolver,
            trading_date=trading_date,
            boundary="first-window-start",  # type: ignore[arg-type]
        )


def test_the_effective_instant_helper_wraps_the_calendars_range_error(
    resolver: CalendarResolver,
) -> None:
    from quant_research_terminal.domain.rollover import RollCalendarRangeError

    with pytest.raises(RollCalendarRangeError):
        roll_effective_instant(
            resolver,
            trading_date=TradingDate.from_iso("2025-06-02"),
            boundary=EffectiveBoundary.FIRST_WINDOW_START,
        )


def test_the_effective_boundary_has_no_default(resolver: CalendarResolver) -> None:
    calendar = resolver.calendar
    with pytest.raises(ValidationError):
        FixedCalendarRollDefinition(
            product=ES,
            calendar_id=calendar.calendar_id,
            calendar_version=calendar.version,
            calendar_content_hash=calendar.content_hash,
            supported_start=calendar.coverage_start,
            supported_end=calendar.coverage_end,
            first_trading_date=calendar.first_trading_date,
            last_trading_date=calendar.last_trading_date,
            lifecycles=(lifecycle(ContractMonth.MARCH, "2024-03-15"),),
            roll_offset_trading_dates=1,
        )  # type: ignore[call-arg]  # the omission under test


# --------------------------------------------------------------------------
# Materialization
# --------------------------------------------------------------------------


def test_a_normal_two_contract_roll_materializes(resolver: CalendarResolver) -> None:
    definition = definition_for(
        resolver,
        lifecycles=(
            lifecycle(ContractMonth.MARCH, "2024-01-15"),
            lifecycle(ContractMonth.JUNE, "2024-03-15"),
        ),
    )
    schedule = materialize_fixed_calendar(definition, resolver)
    assert schedule.initial_contract == contract(ContractMonth.MARCH)
    assert len(schedule.events) == 1
    event = schedule.events[0]
    assert event.to_contract == contract(ContractMonth.JUNE)
    # Roll one trading date before Mon 2024-01-15 is Fri 2024-01-12, at its open.
    assert event.effective_time == datetime(2024, 1, 11, 23, 0, tzinfo=UTC)
    assert event.decision_time == event.effective_time
    assert (
        schedule.provenance_for(event.rule_key).derivation_kind
        is RollDerivationKind.CALENDAR_DERIVED
    )


def test_a_single_lifecycle_produces_a_constant_schedule(resolver: CalendarResolver) -> None:
    definition = definition_for(
        resolver, lifecycles=(lifecycle(ContractMonth.MARCH, "2024-03-15"),)
    )
    schedule = materialize_fixed_calendar(definition, resolver)
    assert schedule.events == ()
    assert schedule.contracts() == (contract(ContractMonth.MARCH),)


def test_materialization_is_repeatable(resolver: CalendarResolver) -> None:
    definition = definition_for(
        resolver,
        lifecycles=(
            lifecycle(ContractMonth.MARCH, "2024-01-15"),
            lifecycle(ContractMonth.JUNE, "2024-03-15"),
        ),
    )
    first = materialize_fixed_calendar(definition, resolver)
    second = materialize_fixed_calendar(definition, resolver)
    assert first == second
    assert first.content_hash == second.content_hash


def test_the_boundary_policy_changes_the_derived_instants(
    resolver: CalendarResolver,
) -> None:
    lifecycles = (
        lifecycle(ContractMonth.MARCH, "2024-01-15"),
        lifecycle(ContractMonth.JUNE, "2024-03-15"),
    )
    at_open = materialize_fixed_calendar(definition_for(resolver, lifecycles=lifecycles), resolver)
    at_preopen = materialize_fixed_calendar(
        definition_for(
            resolver, lifecycles=lifecycles, boundary=EffectiveBoundary.FIRST_WINDOW_START
        ),
        resolver,
    )
    assert at_open.events[0].effective_time != at_preopen.events[0].effective_time
    assert at_open.content_hash != at_preopen.content_hash


def test_non_monotonic_lifecycle_dates_are_reported_not_reordered(
    resolver: CalendarResolver,
) -> None:
    definition = definition_for(
        resolver,
        lifecycles=(
            lifecycle(ContractMonth.MARCH, "2024-03-15"),
            lifecycle(ContractMonth.JUNE, "2024-01-15"),
            lifecycle(ContractMonth.SEPTEMBER, "2024-03-22"),
        ),
    )
    with pytest.raises(RollMaterializationError, match="not in increasing order"):
        materialize_fixed_calendar(definition, resolver)


def test_an_inverted_trading_date_range_is_rejected_at_authoring_time(
    resolver: CalendarResolver,
) -> None:
    """Falsification pass 2: caught at construction, not much later."""
    calendar = resolver.calendar
    with pytest.raises(ValidationError, match="must not be after"):
        FixedCalendarRollDefinition(
            product=ES,
            calendar_id=calendar.calendar_id,
            calendar_version=calendar.version,
            calendar_content_hash=calendar.content_hash,
            supported_start=calendar.coverage_start,
            supported_end=calendar.coverage_end,
            first_trading_date=TradingDate.from_iso("2024-03-29"),
            last_trading_date=TradingDate.from_iso("2024-01-01"),
            lifecycles=(lifecycle(ContractMonth.MARCH, "2024-03-15"),),
            roll_offset_trading_dates=1,
            effective_boundary=EffectiveBoundary.FIRST_TRADING_WINDOW_START,
        )


def test_a_mismatched_calendar_id_or_version_is_refused(
    resolver: CalendarResolver,
) -> None:
    """Falsification pass 2: all three pinned fields are verified, not just the hash.

    A pin claiming a different calendar while carrying the right content hash
    was previously accepted, so a hashed, public claim went untested.
    """
    from quant_research_terminal.domain.exchange_calendar import CalendarId, CalendarVersion

    calendar = resolver.calendar
    definition = FixedCalendarRollDefinition(
        product=ES,
        calendar_id=CalendarId(token="TOTALLY_WRONG"),
        calendar_version=CalendarVersion(number=99),
        calendar_content_hash=calendar.content_hash,
        supported_start=calendar.coverage_start,
        supported_end=calendar.coverage_end,
        first_trading_date=calendar.first_trading_date,
        last_trading_date=calendar.last_trading_date,
        lifecycles=(
            lifecycle(ContractMonth.MARCH, "2024-01-15"),
            lifecycle(ContractMonth.JUNE, "2024-03-15"),
        ),
        roll_offset_trading_dates=1,
        effective_boundary=EffectiveBoundary.FIRST_TRADING_WINDOW_START,
    )
    with pytest.raises(CalendarMismatchError, match="id .*!=.*"):
        materialize_fixed_calendar(definition, resolver)


def test_a_lifecycle_from_another_product_is_rejected(resolver: CalendarResolver) -> None:
    with pytest.raises(ValidationError):
        definition_for(
            resolver,
            lifecycles=(
                lifecycle(ContractMonth.MARCH, "2024-01-15"),
                lifecycle(ContractMonth.JUNE, "2024-03-15", product=NQ),
            ),
        )


def test_a_repeated_contract_in_the_lifecycle_chain_is_rejected(
    resolver: CalendarResolver,
) -> None:
    with pytest.raises(ValidationError):
        definition_for(
            resolver,
            lifecycles=(
                lifecycle(ContractMonth.MARCH, "2024-01-15"),
                lifecycle(ContractMonth.MARCH, "2024-03-15"),
            ),
        )


def test_an_empty_lifecycle_chain_is_rejected(resolver: CalendarResolver) -> None:
    with pytest.raises(ValidationError):
        definition_for(resolver, lifecycles=())


def test_materializing_against_a_different_calendar_is_refused(
    resolver: CalendarResolver, holiday_resolver: CalendarResolver
) -> None:
    definition = definition_for(
        resolver,
        lifecycles=(
            lifecycle(ContractMonth.MARCH, "2024-01-15"),
            lifecycle(ContractMonth.JUNE, "2024-03-15"),
        ),
    )
    with pytest.raises(CalendarMismatchError):
        materialize_fixed_calendar(definition, holiday_resolver)


# --------------------------------------------------------------------------
# The lifecycle fact itself is supplied, never inferred
# --------------------------------------------------------------------------


def test_a_lifecycle_without_a_last_trade_date_is_unconstructible() -> None:
    """Missing lifecycle fact is unsupported — there is no expiry formula."""
    with pytest.raises(ValidationError):
        ContractLifecycle(contract=contract(ContractMonth.MARCH), provenance=PROVENANCE)  # type: ignore[call-arg]


def test_a_lifecycle_rejects_a_civil_date_as_a_last_trade_date() -> None:
    with pytest.raises(ValidationError):
        ContractLifecycle(
            contract=contract(ContractMonth.MARCH),
            last_trade_date=date(2024, 3, 15),  # type: ignore[arg-type]
            provenance=PROVENANCE,
        )


def test_a_lifecycle_rejects_a_datetime_as_a_last_trade_date() -> None:
    with pytest.raises(ValidationError):
        ContractLifecycle(
            contract=contract(ContractMonth.MARCH),
            last_trade_date=datetime(2024, 3, 15, tzinfo=UTC),  # type: ignore[arg-type]
            provenance=PROVENANCE,
        )


def test_lifecycle_provenance_requires_evidence() -> None:
    with pytest.raises(ValidationError):
        LifecycleProvenance(evidence_ids=())


def test_a_lifecycle_rejects_a_contract_subclass() -> None:
    class Sneaky(FuturesContractId):  # type: ignore[misc]  # the attack under test
        pass

    forged = Sneaky(product=ES, contract_month=ContractMonth.MARCH, contract_year=2024)
    with pytest.raises(ValidationError):
        ContractLifecycle(
            contract=forged,
            last_trade_date=TradingDate.from_iso("2024-03-15"),
            provenance=PROVENANCE,
        )


def test_the_lifecycle_canonical_form_is_deterministic() -> None:
    built = lifecycle(ContractMonth.MARCH, "2024-03-15")
    assert built.canonical() == "CME:ES:H2024@2024-03-15"


def test_offset_across_a_holiday_produces_the_expected_roll(
    holiday_resolver: CalendarResolver,
) -> None:
    definition = definition_for(
        holiday_resolver,
        lifecycles=(
            lifecycle(ContractMonth.MARCH, "2024-02-21"),
            lifecycle(ContractMonth.JUNE, "2024-03-22"),
        ),
        offset=2,
    )
    schedule = materialize_fixed_calendar(definition, holiday_resolver)
    # Target trading date is Fri 2024-02-16; its session opens the previous
    # civil evening at 17:00 CST.
    assert schedule.events[0].effective_time == datetime(2024, 2, 15, 23, 0, tzinfo=UTC)


def test_a_roll_outside_the_supported_range_is_typed(resolver: CalendarResolver) -> None:
    from quant_research_terminal.domain.rollover import RollOutsideSupportedRangeError

    calendar = resolver.calendar
    definition = FixedCalendarRollDefinition(
        product=ES,
        calendar_id=calendar.calendar_id,
        calendar_version=calendar.version,
        calendar_content_hash=calendar.content_hash,
        supported_start=datetime(2024, 3, 1, tzinfo=UTC),
        supported_end=calendar.coverage_end,
        first_trading_date=calendar.first_trading_date,
        last_trading_date=calendar.last_trading_date,
        lifecycles=(
            lifecycle(ContractMonth.MARCH, "2024-01-15"),
            lifecycle(ContractMonth.JUNE, "2024-03-15"),
        ),
        roll_offset_trading_dates=1,
        effective_boundary=EffectiveBoundary.FIRST_TRADING_WINDOW_START,
    )
    with pytest.raises(RollOutsideSupportedRangeError):
        materialize_fixed_calendar(definition, resolver)


def test_editing_lifecycle_evidence_moves_the_content_hash(
    resolver: CalendarResolver,
) -> None:
    """Verification metadata is public output, so it must be pinned (ADR-010 D3)."""
    base = (
        lifecycle(ContractMonth.MARCH, "2024-01-15"),
        lifecycle(ContractMonth.JUNE, "2024-03-15"),
    )
    edited = (
        ContractLifecycle(
            contract=base[0].contract,
            last_trade_date=base[0].last_trade_date,
            provenance=LifecycleProvenance(evidence_ids=("a-different-note",)),
        ),
        base[1],
    )
    original = materialize_fixed_calendar(definition_for(resolver, lifecycles=base), resolver)
    changed = materialize_fixed_calendar(definition_for(resolver, lifecycles=edited), resolver)
    assert original.events == changed.events
    assert original.content_hash != changed.content_hash
