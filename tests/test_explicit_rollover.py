"""The operator-declared roll path.

The explicit definition is the foundational oracle: it infers nothing, so it is
the mechanism every other one is checked against. What must hold is that
authored facts survive materialization unchanged, authoring errors are
*reported* rather than repaired, and structural failures surface as typed
rollover errors instead of leaking Pydantic internals.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pytest
from pydantic import ValidationError
from synthetic_calendars import synthetic_resolver

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
    RollDerivationKind,
    RollMaterializationError,
    RollOutsideSupportedRangeError,
    RollResolver,
)
from quant_research_terminal.domain.rollover_definition import (
    AuthoredRollEvent,
    ExplicitRollDefinition,
)
from quant_research_terminal.domain.rollover_materialization import materialize_explicit

ES = FuturesProduct(venue=Venue(code="CME"), root="ES")
MICRO = timedelta(microseconds=1)


def contract(month: ContractMonth) -> FuturesContractId:
    return FuturesContractId(product=ES, contract_month=month, contract_year=2024)


H, M, U = (contract(m) for m in (ContractMonth.MARCH, ContractMonth.JUNE, ContractMonth.SEPTEMBER))


def lifecycle(month: ContractMonth, ltd: str) -> ContractLifecycle:
    return ContractLifecycle(
        contract=contract(month),
        last_trade_date=TradingDate.from_iso(ltd),
        provenance=LifecycleProvenance(evidence_ids=("note-a",)),
    )


LIFECYCLES = (
    lifecycle(ContractMonth.MARCH, "2024-01-15"),
    lifecycle(ContractMonth.JUNE, "2024-02-15"),
    lifecycle(ContractMonth.SEPTEMBER, "2024-03-15"),
)

ROLL_ONE = datetime(2024, 1, 12, 15, 0, tzinfo=UTC)
ROLL_TWO = datetime(2024, 2, 13, 15, 0, tzinfo=UTC)


@pytest.fixture(scope="module")
def resolver() -> CalendarResolver:
    return synthetic_resolver(first=date(2024, 1, 1), last=date(2024, 3, 29))


def authored(
    from_contract: FuturesContractId,
    to_contract: FuturesContractId,
    effective: datetime,
    *,
    decision: datetime | None = None,
) -> AuthoredRollEvent:
    return AuthoredRollEvent(
        from_contract=from_contract,
        to_contract=to_contract,
        decision_time=effective if decision is None else decision,
        effective_time=effective,
        evidence_ids=("research-note-2026-08",),
    )


def definition_for(
    resolver: CalendarResolver,
    *,
    events: tuple[AuthoredRollEvent, ...],
    lifecycles: tuple[ContractLifecycle, ...] = LIFECYCLES,
    supported_start: datetime | None = None,
) -> ExplicitRollDefinition:
    calendar = resolver.calendar
    return ExplicitRollDefinition(
        product=ES,
        calendar_id=calendar.calendar_id,
        calendar_version=calendar.version,
        calendar_content_hash=calendar.content_hash,
        supported_start=(calendar.coverage_start if supported_start is None else supported_start),
        supported_end=calendar.coverage_end,
        first_trading_date=calendar.first_trading_date,
        last_trading_date=calendar.last_trading_date,
        lifecycles=lifecycles,
        events=events,
    )


def test_authored_rolls_survive_materialization_unchanged(
    resolver: CalendarResolver,
) -> None:
    decision = ROLL_ONE - timedelta(days=3)
    definition = definition_for(resolver, events=(authored(H, M, ROLL_ONE, decision=decision),))
    schedule = materialize_explicit(definition, resolver)

    assert schedule.initial_contract == H
    assert len(schedule.events) == 1
    event = schedule.events[0]
    assert (event.from_contract, event.to_contract) == (H, M)
    assert event.effective_time == ROLL_ONE
    assert event.decision_time == decision  # not collapsed into effective_time
    provenance = schedule.provenance_for(event.rule_key)
    assert provenance.derivation_kind is RollDerivationKind.OPERATOR_DECLARED
    assert provenance.evidence_ids == ("research-note-2026-08",)


def test_the_mapping_matches_the_authored_rolls(resolver: CalendarResolver) -> None:
    definition = definition_for(
        resolver, events=(authored(H, M, ROLL_ONE), authored(M, U, ROLL_TWO))
    )
    roll = RollResolver(materialize_explicit(definition, resolver))
    assert roll.active_contract_at(ROLL_ONE - MICRO) == H
    assert roll.active_contract_at(ROLL_ONE) == M
    assert roll.active_contract_at(ROLL_TWO - MICRO) == M
    assert roll.active_contract_at(ROLL_TWO) == U


def test_an_explicit_schedule_with_no_events_is_constant(
    resolver: CalendarResolver,
) -> None:
    schedule = materialize_explicit(definition_for(resolver, events=()), resolver)
    assert schedule.events == ()
    assert schedule.contracts() == (H,)


def test_materialization_is_repeatable(resolver: CalendarResolver) -> None:
    definition = definition_for(resolver, events=(authored(H, M, ROLL_ONE),))
    first = materialize_explicit(definition, resolver)
    second = materialize_explicit(definition, resolver)
    assert first == second
    assert first.content_hash == second.content_hash


def test_out_of_order_authored_events_are_rejected_not_sorted(
    resolver: CalendarResolver,
) -> None:
    """Authored order is a fact; sorting it would be the silent repair."""
    with pytest.raises(ValidationError, match="rejected rather than reordered"):
        definition_for(resolver, events=(authored(M, U, ROLL_TWO), authored(H, M, ROLL_ONE)))


def test_duplicate_authored_effective_times_are_rejected(
    resolver: CalendarResolver,
) -> None:
    with pytest.raises(ValidationError):
        definition_for(resolver, events=(authored(H, M, ROLL_ONE), authored(M, U, ROLL_ONE)))


def test_a_broken_chain_surfaces_as_a_typed_rollover_error(
    resolver: CalendarResolver,
) -> None:
    """A structural failure must not leak a raw Pydantic ValidationError.

    The materializer promises rollover errors; a caller should not have to
    catch two vocabularies from one call.
    """
    definition = definition_for(resolver, events=(authored(U, M, ROLL_ONE),))
    with pytest.raises(RollMaterializationError, match="invalid schedule"):
        materialize_explicit(definition, resolver)


def test_a_roll_outside_the_supported_range_is_typed(resolver: CalendarResolver) -> None:
    definition = definition_for(
        resolver,
        events=(authored(H, M, ROLL_ONE),),
        supported_start=ROLL_ONE + timedelta(days=1),
    )
    with pytest.raises(RollOutsideSupportedRangeError):
        materialize_explicit(definition, resolver)


def test_materializing_against_a_different_calendar_is_refused(
    resolver: CalendarResolver,
) -> None:
    definition = definition_for(resolver, events=(authored(H, M, ROLL_ONE),))
    other = synthetic_resolver(first=date(2024, 1, 1), last=date(2024, 3, 29), version=2)
    with pytest.raises(CalendarMismatchError):
        materialize_explicit(definition, other)


def test_an_event_naming_a_contract_with_no_lifecycle_is_rejected(
    resolver: CalendarResolver,
) -> None:
    """Falsification pass 2 made the lifecycles load-bearing on this path.

    They were previously required, evidence-cited, and then almost entirely
    unused — only ``lifecycles[0].contract`` was read. Now every contract a
    schedule can hold must have its lifetime recorded.
    """
    with pytest.raises(ValidationError, match="no declared lifecycle"):
        definition_for(
            resolver,
            events=(authored(H, M, ROLL_ONE),),
            lifecycles=(lifecycle(ContractMonth.MARCH, "2024-01-15"),),
        )


def test_an_authored_event_requires_evidence(resolver: CalendarResolver) -> None:
    with pytest.raises(ValidationError):
        AuthoredRollEvent(
            from_contract=H,
            to_contract=M,
            decision_time=ROLL_ONE,
            effective_time=ROLL_ONE,
            evidence_ids=(),
        )


def test_an_authored_event_cannot_take_effect_before_its_decision() -> None:
    with pytest.raises(ValidationError):
        authored(H, M, ROLL_ONE, decision=ROLL_ONE + MICRO)


def test_editing_authored_evidence_moves_the_content_hash(
    resolver: CalendarResolver,
) -> None:
    baseline = materialize_explicit(
        definition_for(resolver, events=(authored(H, M, ROLL_ONE),)), resolver
    )
    edited_event = AuthoredRollEvent(
        from_contract=H,
        to_contract=M,
        decision_time=ROLL_ONE,
        effective_time=ROLL_ONE,
        evidence_ids=("a-different-note",),
    )
    edited = materialize_explicit(definition_for(resolver, events=(edited_event,)), resolver)
    assert baseline.events == edited.events
    assert baseline.content_hash != edited.content_hash
