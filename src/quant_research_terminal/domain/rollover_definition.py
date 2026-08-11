"""Authored roll definitions: the two supported ways to state a roll schedule.

Phase 2.2 supports exactly two mechanisms, and both are *ex-ante*: an operator
states the rolls, or a deterministic rule derives them from a published
lifecycle fact plus a calendar. Neither observes market data.

Data-driven mechanisms — volume crossover, open interest, tie and oscillation
policies — are **not** implemented and have no skeleton here. There is no real
roll data pipeline yet, and an abstraction invented before its first consumer
would freeze the wrong shape around future volume/OI semantics. When one
arrives it must additionally satisfy a rule this phase cannot enforce:

    every observation's availability_time <= decision_time <= effective_time

Authored versus materialized
----------------------------
:class:`AuthoredRollEvent` carries what an *author* knows — the two contracts,
the two instants, and the evidence for the choice. :class:`RollEvent` (in
:mod:`quant_research_terminal.domain.rollover`) carries what a *materializer*
produced, including a generated ``rule_key``. Keeping them apart is the same
separation Phase 2.1 keeps between ``WeeklyWindowRule`` and
``MaterializedWindow``, and it is what stops ``rule_key`` from becoming a
free-text field an author types into a content hash.
"""

from __future__ import annotations

import re
from datetime import datetime
from enum import StrEnum, unique
from typing import Final, final

from pydantic import field_validator, model_validator

from quant_research_terminal.domain.contract_lifecycle import (
    ContractLifecycle,
    _require_exact_trading_date,
)
from quant_research_terminal.domain.exchange_calendar import (
    CalendarId,
    CalendarVersion,
    TradingDate,
)
from quant_research_terminal.domain.futures_contract import (
    FuturesContractId,
    FuturesProduct,
    _CanonicalModel,
    require_listed_contract,
)
from quant_research_terminal.domain.time import validate_utc_datetime

_CONTENT_HASH_PATTERN: Final[re.Pattern[str]] = re.compile(r"[0-9a-f]{64}")


@unique
class EffectiveBoundary(StrEnum):
    """How a lifecycle-derived trading date becomes a UTC effective instant.

    There is **no default**. A roll's effective instant is a research decision
    with real consequences — the two members below differ by the length of the
    pre-open — so a definition must name one. Silently defaulting to whichever
    is more common would be exactly the hidden policy this phase forbids.

    Both are calendar-backed and deterministic: each names a window of the
    target trading date and takes that window's start.
    """

    #: The start of the trading date's earliest materialized window. For a CME
    #: equity-index weekday that is the pre-open on the *previous civil day* —
    #: the payoff of a trading date not being a civil date.
    FIRST_WINDOW_START = "first-window-start"
    #: The start of the trading date's first window whose state is TRADING,
    #: i.e. the open, skipping any leading pre-open halt. A trading date with
    #: no TRADING window is an error, never silently the first window.
    FIRST_TRADING_WINDOW_START = "first-trading-window-start"


def _validate_calendar_pin(value: str) -> str:
    if _CONTENT_HASH_PATTERN.fullmatch(value) is None:
        raise ValueError(
            f"a calendar content hash must be 64 lower-case hexadecimal characters; got {value!r}"
        )
    return value


@final
class AuthoredRollEvent(_CanonicalModel):
    """One roll as an operator states it, with the evidence for the choice.

    ``decision_time`` has no default and is not derived: the author states when
    the decision existed. That is the coordinate a future replay engine needs
    in order to prove a roll was not acted on before it was decided, and
    defaulting it would quietly fabricate that fact.
    """

    from_contract: FuturesContractId
    to_contract: FuturesContractId
    decision_time: datetime
    effective_time: datetime
    evidence_ids: tuple[str, ...]

    @field_validator("from_contract", "to_contract")
    @classmethod
    def _validate_contracts(cls, value: FuturesContractId) -> FuturesContractId:
        try:
            return require_listed_contract(value)
        except TypeError as error:
            raise ValueError(f"an authored roll names listed contracts: {error}") from error

    @field_validator("decision_time", "effective_time", mode="before")
    @classmethod
    def _validate_instants(cls, value: object) -> datetime:
        return validate_utc_datetime(value)

    @field_validator("evidence_ids")
    @classmethod
    def _validate_evidence_ids(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if not value:
            raise ValueError("an authored roll must cite at least one evidence record")
        for item in value:
            if not isinstance(item, str) or not item:
                raise ValueError(f"evidence id must be a non-empty string; got {item!r}")
        return value

    @model_validator(mode="after")
    def _validate_event(self) -> AuthoredRollEvent:
        if self.from_contract == self.to_contract:
            raise ValueError(
                f"a roll must change contract; got {self.from_contract.canonical()} on both sides"
            )
        if self.from_contract.product != self.to_contract.product:
            raise ValueError(
                f"a roll stays within one product; got "
                f"{self.from_contract.product.canonical()} -> "
                f"{self.to_contract.product.canonical()}"
            )
        if self.decision_time > self.effective_time:
            raise ValueError(
                f"a roll cannot take effect before the decision that produced it; decision "
                f"{self.decision_time.isoformat()} is after effective "
                f"{self.effective_time.isoformat()}"
            )
        return self


class _BaseRollDefinition(_CanonicalModel):
    """Fields both definition kinds share.

    The calendar pin travels with the definition because a roll instant is only
    meaningful against the trade-date labelling it was derived from, and the
    supported trading-date range is authored rather than inferred so that an
    out-of-range trading date is distinguishable from a non-trading one.
    """

    product: FuturesProduct
    calendar_id: CalendarId
    calendar_version: CalendarVersion
    calendar_content_hash: str
    supported_start: datetime
    supported_end: datetime
    first_trading_date: TradingDate
    last_trading_date: TradingDate
    lifecycles: tuple[ContractLifecycle, ...]

    @field_validator("supported_start", "supported_end", mode="before")
    @classmethod
    def _validate_instants(cls, value: object) -> datetime:
        return validate_utc_datetime(value)

    @field_validator("calendar_content_hash")
    @classmethod
    def _validate_calendar_hash(cls, value: str) -> str:
        return _validate_calendar_pin(value)

    @field_validator("first_trading_date", "last_trading_date")
    @classmethod
    def _validate_trading_dates(cls, value: TradingDate) -> TradingDate:
        return _require_exact_trading_date(value)

    @field_validator("lifecycles")
    @classmethod
    def _validate_lifecycles(
        cls, value: tuple[ContractLifecycle, ...]
    ) -> tuple[ContractLifecycle, ...]:
        if not value:
            raise ValueError(
                "a roll definition must declare at least one contract lifecycle; the first "
                "is the contract active at supported_start"
            )
        for item in value:
            if type(item) is not ContractLifecycle:
                raise ValueError(
                    f"lifecycles must be ContractLifecycle values; got {type(item).__name__}"
                )
        return value

    @model_validator(mode="after")
    def _validate_common(self) -> _BaseRollDefinition:
        if self.supported_start >= self.supported_end:
            raise ValueError("supported_start must be strictly before supported_end")
        if self.first_trading_date.value > self.last_trading_date.value:
            raise ValueError(
                f"first_trading_date {self.first_trading_date.canonical()} must not be after "
                f"last_trading_date {self.last_trading_date.canonical()}"
            )

        seen: set[FuturesContractId] = set()
        for lifecycle in self.lifecycles:
            if lifecycle.contract.product != self.product:
                raise ValueError(
                    f"lifecycle {lifecycle.canonical()} belongs to "
                    f"{lifecycle.contract.product.canonical()}, not to the definition's "
                    f"product {self.product.canonical()}"
                )
            if lifecycle.contract in seen:
                raise ValueError(
                    f"lifecycle chain repeats {lifecycle.contract.canonical()}; each contract "
                    f"appears at most once"
                )
            seen.add(lifecycle.contract)
        return self

    @property
    def initial_contract(self) -> FuturesContractId:
        """The contract active at ``supported_start``.

        Derived from the first declared lifecycle rather than stored twice: one
        source of truth beats a field plus an equality check that could drift.
        """
        return self.lifecycles[0].contract


@final
class ExplicitRollDefinition(_BaseRollDefinition):
    """Rolls stated outright by the operator — the foundational oracle.

    Nothing is inferred: not the initial contract (it is the first declared
    lifecycle), not the supported range, not the roll instants. This is the
    definition kind to reach for when a research protocol fixes its own roll
    dates, and the one every other mechanism is checked against.

    What the lifecycles are for here, stated so it is not mistaken for more:
    they declare **which contracts the schedule may hold**, and their first
    entry is the initial contract. Every contract named by an event must have
    one, which is checked below. Their ``last_trade_date`` is **not** consumed
    on this path — an explicit definition states its roll instants outright, so
    there is nothing to count back from. The field is still required because a
    contract whose lifetime nobody recorded has no business being in a
    schedule; it is simply not load-bearing for *this* mechanism.
    """

    events: tuple[AuthoredRollEvent, ...]

    @field_validator("events")
    @classmethod
    def _validate_events(
        cls, value: tuple[AuthoredRollEvent, ...]
    ) -> tuple[AuthoredRollEvent, ...]:
        for item in value:
            if type(item) is not AuthoredRollEvent:
                raise ValueError(
                    f"events must be AuthoredRollEvent values; got {type(item).__name__}"
                )
        return value

    @model_validator(mode="after")
    def _validate_events_against_lifecycles(self) -> ExplicitRollDefinition:
        declared = {lifecycle.contract for lifecycle in self.lifecycles}
        missing = sorted(
            {
                contract.canonical()
                for event in self.events
                for contract in (event.from_contract, event.to_contract)
            }
            - {contract.canonical() for contract in declared}
        )
        if missing:
            raise ValueError(
                f"authored events name contracts with no declared lifecycle: {missing}. "
                f"Every contract a schedule can hold must have its lifetime recorded"
            )
        return self

    @model_validator(mode="after")
    def _validate_event_order(self) -> ExplicitRollDefinition:
        # Authored order is a fact, not an implementation artifact: out-of-order
        # events are an authoring error and are rejected, never sorted. Sorting
        # authored input is the silent repair this project forbids.
        previous: datetime | None = None
        for event in self.events:
            if previous is not None and event.effective_time <= previous:
                raise ValueError(
                    f"authored roll events must be listed in strictly increasing effective-time "
                    f"order; {event.effective_time.isoformat()} does not follow "
                    f"{previous.isoformat()}. They are rejected rather than reordered"
                )
            previous = event.effective_time
        return self


@final
class FixedCalendarRollDefinition(_BaseRollDefinition):
    """Rolls derived from lifecycle facts plus a calendar, deterministically.

    Reads: *roll ``roll_offset_trading_dates`` trading dates before each
    contract's last trade date, effective at the boundary named by
    ``effective_boundary``.*

    ``roll_offset_trading_dates`` counts **trading dates**, which the calendar
    decides — weekends, holidays, and removed dates are skipped because the
    calendar says they are not trading dates, never because of weekday
    arithmetic. ``0`` means roll on the last trade date itself.

    What this rule shape cannot express, stated rather than hidden:
    first-notice-date-anchored rolls (no FND fact exists in this repository)
    and anything data-driven. For a physically delivered product an
    LTD-anchored roll holds past first notice; ES and NQ are cash-settled, so
    that consequence does not arise for them, but it would for others.
    """

    roll_offset_trading_dates: int
    effective_boundary: EffectiveBoundary

    @field_validator("roll_offset_trading_dates")
    @classmethod
    def _validate_offset(cls, value: int) -> int:
        # Strict mode rejects bool for an int field, but say it anyway: a True
        # silently meaning 1 would be a one-trading-date roll nobody authored.
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError(f"roll offset must be an int; got {value!r}")
        if value < 0:
            raise ValueError(
                f"roll offset must be zero or more trading dates; got {value!r}. There is no "
                f"upper bound here — the calendar's own coverage is the honest ceiling"
            )
        return value
