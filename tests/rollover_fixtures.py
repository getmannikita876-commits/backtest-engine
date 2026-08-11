"""Typed builders for roll schedules used across the rollover test modules.

One builder rather than three near-identical local copies: a schedule needs
eleven consistent fields plus a content hash derived from all of them, and
three hand-maintained copies of that is three chances for a test to pin a hash
that no longer matches what the code would produce.
"""

from __future__ import annotations

from datetime import datetime

from quant_research_terminal.domain.exchange_calendar import (
    CalendarId,
    CalendarVersion,
    MaterializedCalendar,
    TradingDate,
)
from quant_research_terminal.domain.futures_contract import (
    FuturesContractId,
    FuturesProduct,
)
from quant_research_terminal.domain.rollover import (
    RollDerivationKind,
    RollEvent,
    RollProvenance,
    RollSchedule,
)
from quant_research_terminal.domain.rollover_materialization import (
    roll_schedule_content_hash,
)

OPERATOR_PROVENANCE = RollProvenance(
    derivation_kind=RollDerivationKind.OPERATOR_DECLARED, evidence_ids=("note-1",)
)


def roll_events(
    rolls: tuple[tuple[FuturesContractId, FuturesContractId, datetime], ...],
) -> tuple[RollEvent, ...]:
    """Return materialized events for ``(from, to, effective)`` triples."""
    return tuple(
        RollEvent(
            from_contract=source,
            to_contract=target,
            decision_time=effective,
            effective_time=effective,
            rule_key=f"explicit.{index}",
        )
        for index, (source, target, effective) in enumerate(rolls)
    )


def provenance_for(events: tuple[RollEvent, ...]) -> tuple[tuple[str, RollProvenance], ...]:
    """Return one operator-declared provenance entry per distinct rule key."""
    return tuple(
        sorted(
            {event.rule_key: OPERATOR_PROVENANCE for event in events}.items(),
            key=lambda item: item[0],
        )
    )


def build_roll_schedule(
    *,
    product: FuturesProduct,
    calendar_id: CalendarId,
    calendar_version: CalendarVersion,
    calendar_content_hash: str,
    supported_start: datetime,
    supported_end: datetime,
    first_trading_date: TradingDate,
    last_trading_date: TradingDate,
    initial_contract: FuturesContractId,
    events: tuple[RollEvent, ...] = (),
    provenance: tuple[tuple[str, RollProvenance], ...] | None = None,
) -> RollSchedule:
    """Return a roll schedule with its content hash computed consistently."""
    resolved = provenance_for(events) if provenance is None else provenance
    return RollSchedule(
        product=product,
        calendar_id=calendar_id,
        calendar_version=calendar_version,
        calendar_content_hash=calendar_content_hash,
        supported_start=supported_start,
        supported_end=supported_end,
        first_trading_date=first_trading_date,
        last_trading_date=last_trading_date,
        initial_contract=initial_contract,
        events=events,
        provenance=resolved,
        content_hash=roll_schedule_content_hash(
            product=product,
            calendar_id=calendar_id,
            calendar_version=calendar_version,
            calendar_content_hash=calendar_content_hash,
            supported_start=supported_start,
            supported_end=supported_end,
            first_trading_date=first_trading_date,
            last_trading_date=last_trading_date,
            initial_contract=initial_contract,
            events=events,
            provenance=resolved,
        ),
    )


def schedule_over_calendar(
    calendar: MaterializedCalendar,
    *,
    product: FuturesProduct,
    initial_contract: FuturesContractId,
    rolls: tuple[tuple[FuturesContractId, FuturesContractId, datetime], ...] = (),
    supported_start: datetime | None = None,
    supported_end: datetime | None = None,
    first_trading_date: TradingDate | None = None,
) -> RollSchedule:
    """Return a schedule pinned to ``calendar`` and spanning its coverage."""
    events = roll_events(rolls)
    return build_roll_schedule(
        product=product,
        calendar_id=calendar.calendar_id,
        calendar_version=calendar.version,
        calendar_content_hash=calendar.content_hash,
        supported_start=calendar.coverage_start if supported_start is None else supported_start,
        supported_end=calendar.coverage_end if supported_end is None else supported_end,
        first_trading_date=(
            calendar.first_trading_date if first_trading_date is None else first_trading_date
        ),
        last_trading_date=calendar.last_trading_date,
        initial_contract=initial_contract,
        events=events,
    )
