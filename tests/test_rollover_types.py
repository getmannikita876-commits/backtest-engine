"""Adversarial tests for roll value types and schedule structural invariants.

These attack the properties a research platform cannot recover from silently:

* a schedule must never leave an instant unmapped or doubly mapped;
* a ``from_contract`` must never name a contract that was never active;
* a duplicate roll instant must never make an authored event vanish;
* a hashed field must never be unobservable, and an observable change must
  always move the hash.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from typing import Any

import pytest
from pydantic import ValidationError
from rollover_fixtures import build_roll_schedule

from quant_research_terminal.domain.contract_month import ContractMonth
from quant_research_terminal.domain.exchange_calendar import (
    CalendarId,
    CalendarVersion,
    ExchangeTradingState,
    TradingDate,
)
from quant_research_terminal.domain.futures_contract import (
    FuturesContractId,
    FuturesProduct,
    Venue,
)
from quant_research_terminal.domain.rollover import (
    ContractSegment,
    RollDerivationKind,
    RollEvent,
    RollProvenance,
    RollResolver,
    RollSchedule,
)
from quant_research_terminal.domain.rollover_materialization import (
    roll_schedule_content_hash,
)

ES = FuturesProduct(venue=Venue(code="CME"), root="ES")
NQ = FuturesProduct(venue=Venue(code="CME"), root="NQ")

CAL_ID = CalendarId(token="SYNTHETIC_TEST")
CAL_VERSION = CalendarVersion(number=1)
CAL_HASH = "a" * 64

START = datetime(2023, 1, 2, 0, 0, tzinfo=UTC)
END = datetime(2023, 12, 29, 0, 0, tzinfo=UTC)
R1 = datetime(2023, 3, 16, 14, 30, tzinfo=UTC)
R2 = datetime(2023, 6, 15, 14, 30, tzinfo=UTC)
MICRO = timedelta(microseconds=1)

PROVENANCE = RollProvenance(
    derivation_kind=RollDerivationKind.OPERATOR_DECLARED, evidence_ids=("note-1",)
)


def contract(
    month: ContractMonth, year: int = 2023, product: FuturesProduct = ES
) -> FuturesContractId:
    return FuturesContractId(product=product, contract_month=month, contract_year=year)


H, M, U, Z = (
    contract(m)
    for m in (
        ContractMonth.MARCH,
        ContractMonth.JUNE,
        ContractMonth.SEPTEMBER,
        ContractMonth.DECEMBER,
    )
)


def event(
    from_contract: FuturesContractId = H,
    to_contract: FuturesContractId = M,
    effective: datetime = R1,
    decision: datetime | None = None,
    rule_key: str = "explicit.0",
) -> RollEvent:
    return RollEvent(
        from_contract=from_contract,
        to_contract=to_contract,
        decision_time=effective if decision is None else decision,
        effective_time=effective,
        rule_key=rule_key,
    )


def schedule(
    *,
    events: tuple[RollEvent, ...] = (),
    initial: FuturesContractId = H,
    product: FuturesProduct = ES,
    supported_start: datetime = START,
    supported_end: datetime = END,
    first_trading_date: str = "2023-01-02",
    last_trading_date: str = "2023-12-29",
    provenance: tuple[tuple[str, RollProvenance], ...] | None = None,
) -> RollSchedule:
    return build_roll_schedule(
        product=product,
        calendar_id=CAL_ID,
        calendar_version=CAL_VERSION,
        calendar_content_hash=CAL_HASH,
        supported_start=supported_start,
        supported_end=supported_end,
        first_trading_date=TradingDate.from_iso(first_trading_date),
        last_trading_date=TradingDate.from_iso(last_trading_date),
        initial_contract=initial,
        events=events,
        provenance=provenance,
    )


# --------------------------------------------------------------------------
# RollEvent
# --------------------------------------------------------------------------


def test_a_roll_must_change_contract() -> None:
    with pytest.raises(ValidationError):
        event(from_contract=H, to_contract=H)


def test_a_roll_stays_within_one_product() -> None:
    with pytest.raises(ValidationError):
        event(to_contract=contract(ContractMonth.JUNE, product=NQ))


def test_effective_before_decision_is_rejected() -> None:
    with pytest.raises(ValidationError):
        event(effective=R1, decision=R1 + MICRO)


def test_decision_equal_to_effective_is_accepted() -> None:
    built = event(effective=R1, decision=R1)
    assert built.decision_time == built.effective_time


def test_decision_strictly_before_effective_is_accepted() -> None:
    built = event(effective=R1, decision=R1 - timedelta(days=2))
    assert built.decision_time < built.effective_time


@pytest.mark.parametrize("field", ["decision_time", "effective_time"])
def test_roll_event_rejects_naive_instants(field: str) -> None:
    kwargs: dict[str, Any] = {
        "from_contract": H,
        "to_contract": M,
        "decision_time": R1,
        "effective_time": R1,
        "rule_key": "explicit.0",
    }
    kwargs[field] = R1.replace(tzinfo=None)
    with pytest.raises(ValidationError):
        RollEvent(**kwargs)


def test_roll_event_rejects_a_contract_subclass() -> None:
    """ADR-009 defect D4, one level up.

    Pydantic admits subclass instances for a model-typed field and ``@final`` is
    type-checker-only, so a runtime subclass would ride all the way to the
    executable guard. It must fail at construction instead.
    """

    class Sneaky(FuturesContractId):  # type: ignore[misc]  # the attack under test
        pass

    forged = Sneaky(product=ES, contract_month=ContractMonth.JUNE, contract_year=2023)
    with pytest.raises(ValidationError):
        event(to_contract=forged)


def test_roll_event_rejects_an_empty_rule_key() -> None:
    with pytest.raises(ValidationError):
        event(rule_key="")


# --------------------------------------------------------------------------
# RollSchedule: chain, ordering, observability
# --------------------------------------------------------------------------


def test_a_schedule_with_no_events_is_valid_and_constant() -> None:
    built = schedule()
    resolver = RollResolver(built)
    assert resolver.active_contract_at(START) == H
    assert resolver.active_contract_at(END - MICRO) == H


def test_initial_contract_is_the_contract_active_at_supported_start() -> None:
    for events in ((), (event(),), (event(), event(M, U, R2, rule_key="explicit.1"))):
        built = schedule(events=events)
        assert RollResolver(built).active_contract_at(built.supported_start) == H


def test_a_broken_first_link_is_rejected() -> None:
    """D1: the first event must roll out of the initial contract."""
    with pytest.raises(ValidationError, match="broken roll chain"):
        schedule(events=(event(from_contract=M, to_contract=U),), initial=H)


def test_a_broken_later_link_is_rejected() -> None:
    """D2: initial=H, H->M, then U->Z is two locally valid events, one broken chain."""
    with pytest.raises(ValidationError, match="broken roll chain"):
        schedule(
            events=(
                event(H, M, R1, rule_key="explicit.0"),
                event(U, Z, R2, rule_key="explicit.1"),
            )
        )


def test_duplicate_effective_instants_are_rejected() -> None:
    """D3: a tiebreak would make one authored event unobservable."""
    with pytest.raises(ValidationError, match="strictly increasing"):
        schedule(
            events=(
                event(H, M, R1, rule_key="explicit.0"),
                event(M, U, R1, rule_key="explicit.1"),
            )
        )


def test_non_monotonic_events_are_rejected_not_sorted() -> None:
    with pytest.raises(ValidationError, match="strictly increasing"):
        schedule(
            events=(
                event(H, M, R2, rule_key="explicit.0"),
                event(M, U, R1, rule_key="explicit.1"),
            )
        )


def test_an_event_at_supported_start_is_rejected() -> None:
    """It would leave ``initial_contract`` unreachable by any instant lookup.

    Precisely: the contract would still be *listed* by ``contracts()``, but no
    instant would ever resolve to it, so two schedules differing only in that
    field would answer identically while carrying different hashes.
    """
    with pytest.raises(ValidationError, match="unreachable by any instant lookup"):
        schedule(events=(event(effective=START),))


def test_an_event_at_supported_end_is_rejected() -> None:
    with pytest.raises(ValidationError, match="unreachable by any instant lookup"):
        schedule(events=(event(effective=END),))


def test_an_event_outside_the_supported_range_is_rejected() -> None:
    with pytest.raises(ValidationError):
        schedule(events=(event(effective=END + timedelta(days=1)),))


def test_a_chain_that_revisits_a_contract_is_rejected() -> None:
    with pytest.raises(ValidationError, match="revisits"):
        schedule(
            events=(
                event(H, M, R1, rule_key="explicit.0"),
                event(M, H, R2, rule_key="explicit.1"),
            )
        )


def test_a_chain_may_move_backwards_in_delivery_period() -> None:
    """Delivery-order monotonicity is deliberately NOT enforced.

    Requiring a chain to move forward in delivery period would encode an
    economic assumption about which way a research series rolls. Only
    distinctness is structural, and that is what the model enforces.
    """
    built = schedule(events=(event(from_contract=H, to_contract=contract(ContractMonth.JANUARY)),))
    assert RollResolver(built).active_contract_at(R1).contract_month is ContractMonth.JANUARY


def test_initial_contract_product_mismatch_is_rejected() -> None:
    with pytest.raises(ValidationError):
        schedule(initial=contract(ContractMonth.MARCH, product=NQ))


def test_an_inverted_supported_range_is_rejected() -> None:
    with pytest.raises(ValidationError):
        schedule(supported_start=END, supported_end=START)


def test_an_inverted_trading_date_range_is_rejected() -> None:
    with pytest.raises(ValidationError):
        schedule(first_trading_date="2023-12-29", last_trading_date="2023-01-02")


# --------------------------------------------------------------------------
# Provenance integrity and hashing
# --------------------------------------------------------------------------


def test_an_event_without_a_provenance_entry_is_rejected() -> None:
    with pytest.raises(ValidationError, match="no provenance entry"):
        schedule(events=(event(),), provenance=())


def test_an_orphan_provenance_entry_is_rejected() -> None:
    """The D1 inverse: it would move the hash without changing an answer."""
    with pytest.raises(ValidationError, match="referenced by no event"):
        schedule(
            events=(event(),),
            provenance=(("explicit.0", PROVENANCE), ("explicit.9", PROVENANCE)),
        )


def test_provenance_must_be_in_sorted_key_order() -> None:
    """Falsification pass 2: the tuple's order is public but cannot be hashed.

    The serialization emits provenance as a sorted JSON object, so authored
    order never reaches the hash. Two schedules differing only in that order
    would therefore be observably different (``schedule.provenance[0]``) while
    sharing a pin. Requiring canonical order makes the observable order a
    function of the keys, which *are* hashed.
    """
    events = (event(H, M, R1, rule_key="explicit.0"), event(M, U, R2, rule_key="explicit.1"))
    with pytest.raises(ValidationError, match="sorted key order"):
        schedule(
            events=events,
            provenance=(("explicit.1", PROVENANCE), ("explicit.0", PROVENANCE)),
        )


def test_a_roll_event_subclass_is_rejected_by_the_schedule() -> None:
    """Falsification pass 2: a subclass may carry state the hash cannot see.

    The guard existed on ``RollEvent``'s own contract fields but not on the
    schedule's ``events`` collection, so a subclass with an extra public field
    was admitted and hashed identically to the plain event.
    """

    class FatEvent(RollEvent):  # type: ignore[misc]  # the attack under test
        note: str = "public state nothing hashes"

    forged = FatEvent(
        from_contract=H,
        to_contract=M,
        decision_time=R1,
        effective_time=R1,
        rule_key="explicit.0",
    )
    with pytest.raises(ValidationError, match="exactly RollEvent"):
        schedule(events=(forged,))


def test_a_roll_provenance_subclass_is_rejected_by_the_schedule() -> None:
    class FatProvenance(RollProvenance):  # type: ignore[misc]  # the attack under test
        note: str = "public state nothing hashes"

    forged = FatProvenance(
        derivation_kind=RollDerivationKind.OPERATOR_DECLARED, evidence_ids=("note-1",)
    )
    with pytest.raises(ValidationError, match="exactly RollProvenance"):
        schedule(events=(event(),), provenance=(("explicit.0", forged),))


def test_a_wrong_content_hash_is_unconstructible() -> None:
    good = schedule(events=(event(),))
    with pytest.raises(ValidationError, match="content_hash does not match"):
        RollSchedule(**{**dict(good), "content_hash": "0" * 64})


def test_model_copy_revalidates_the_hash() -> None:
    """ADR-009 defect D2, one level up: ``model_copy`` skips validation."""
    good = schedule(events=(event(),))
    with pytest.raises(ValidationError):
        good.model_copy(update={"events": ()})


@pytest.mark.parametrize("bad", ["", "z" * 64, "A" * 64, "abc"])
def test_malformed_hashes_are_rejected(bad: str) -> None:
    good = schedule()
    with pytest.raises(ValidationError):
        RollSchedule(**{**dict(good), "calendar_content_hash": bad})


def test_the_calendar_pin_participates_in_the_hash() -> None:
    a = schedule(events=(event(),))
    other_hash = "b" * 64
    b_hash = roll_schedule_content_hash(
        product=ES,
        calendar_id=CAL_ID,
        calendar_version=CAL_VERSION,
        calendar_content_hash=other_hash,
        supported_start=START,
        supported_end=END,
        first_trading_date=a.first_trading_date,
        last_trading_date=a.last_trading_date,
        initial_contract=H,
        events=a.events,
        provenance=a.provenance,
    )
    assert a.content_hash != b_hash


def test_the_trading_date_range_participates_in_the_hash() -> None:
    a = schedule(events=(event(),))
    b = schedule(events=(event(),), last_trading_date="2023-12-28")
    assert a.events == b.events
    assert a.content_hash != b.content_hash


def test_the_initial_contract_participates_in_the_hash() -> None:
    a = schedule()
    b = schedule(initial=M)
    assert a.content_hash != b.content_hash


def test_the_supported_range_participates_in_the_hash() -> None:
    a = schedule()
    b = schedule(supported_end=END - timedelta(days=1))
    assert a.content_hash != b.content_hash


def test_provenance_participates_in_the_hash() -> None:
    a = schedule(events=(event(),))
    changed = RollProvenance(
        derivation_kind=RollDerivationKind.CALENDAR_DERIVED, evidence_ids=("note-1",)
    )
    b_hash = roll_schedule_content_hash(
        product=ES,
        calendar_id=CAL_ID,
        calendar_version=CAL_VERSION,
        calendar_content_hash=CAL_HASH,
        supported_start=START,
        supported_end=END,
        first_trading_date=a.first_trading_date,
        last_trading_date=a.last_trading_date,
        initial_contract=H,
        events=a.events,
        provenance=(("explicit.0", changed),),
    )
    assert a.content_hash != b_hash


def test_roll_provenance_requires_evidence() -> None:
    with pytest.raises(ValidationError):
        RollProvenance(derivation_kind=RollDerivationKind.OPERATOR_DECLARED, evidence_ids=())


def test_contracts_returns_the_chain_in_order() -> None:
    built = schedule(
        events=(event(H, M, R1, rule_key="explicit.0"), event(M, U, R2, rule_key="explicit.1"))
    )
    assert built.contracts() == (H, M, U)


# --------------------------------------------------------------------------
# ContractSegment
# --------------------------------------------------------------------------


def segment(**overrides: Any) -> ContractSegment:
    fields: dict[str, Any] = {
        "start": R1,
        "end": R2,
        "contract": H,
        "state": ExchangeTradingState.TRADING,
        "trading_date": TradingDate(value=date(2023, 3, 16)),
        "window_rule_key": "weekly.thursday.0",
        "roll_rule_key": None,
    }
    fields.update(overrides)
    return ContractSegment(**fields)


def test_a_zero_length_segment_is_unconstructible() -> None:
    with pytest.raises(ValidationError):
        segment(end=R1)


def test_a_closed_segment_is_unconstructible() -> None:
    with pytest.raises(ValidationError):
        segment(state=ExchangeTradingState.CLOSED)


def test_a_segment_rejects_a_civil_date_as_a_trading_date() -> None:
    with pytest.raises(ValidationError):
        segment(trading_date=date(2023, 3, 16))


# --------------------------------------------------------------------------
# RollResolver guards
# --------------------------------------------------------------------------


def test_resolver_rejects_a_non_schedule() -> None:
    with pytest.raises(TypeError):
        RollResolver(object())  # type: ignore[arg-type]


def test_resolver_rejects_naive_and_non_utc_timestamps() -> None:
    resolver = RollResolver(schedule())
    with pytest.raises(ValueError):
        resolver.active_contract_at(START.replace(tzinfo=None))
    with pytest.raises(TypeError):
        resolver.active_contract_at("2023-06-01T00:00:00Z")
