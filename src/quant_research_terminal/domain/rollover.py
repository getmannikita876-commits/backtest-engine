"""Rollover mapping: which **listed** contract a research series held, and when.

A continuous futures series is a research construct. No order was ever filled
in one, and every backtest fill must name a real listed contract. This module
answers exactly one question — *at this UTC instant, which listed contract was
active?* — and refuses to answer any other. In particular it builds **no
prices**: back-adjustment, ratio and Panama methods, roll-gap smoothing, and
synthetic OHLC are not implemented here and have no placeholder API.

The layering mirrors the exchange calendar (ADR-010)::

    Explicit / FixedCalendar RollDefinition     authored facts
        │  deterministic materialization (rollover_materialization)
        ▼
    RollSchedule                                explicit UTC events, hashed
        │
        ▼
    RollResolver                                pure bisect lookups

Two invariants carry the design.

**Total, single-valued coverage.** Within ``[supported_start, supported_end)``
exactly one contract is active at every instant. This is not a validated
property but a structural one: the mapping is
``bisect_right`` over strictly-increasing effective times, which is total and
single-valued on any list. The validators exist to stop *silent loss* — a
duplicate effective time would make an authored event unobservable while the
schedule still validated — and to stop a ``from_contract`` naming a contract
that was never active.

**A trading date is never collapsed.** A roll can land in the middle of a
trading date, so :func:`segments_for_trading_date` returns *ordered segments*
rather than one contract. Any API that answered "the contract for trading date
T" with a single value would silently pick one side of an intraday roll.

Structural versus factual invariants — stated plainly
-----------------------------------------------------
:class:`RollSchedule` guarantees **structural** well-formedness and is
unconstructible if violated: chained, strictly ordered, observable, hash-matched.
It does **not** and cannot guarantee **factual** consistency with a calendar or
with contract lifecycles, because those need inputs the frozen model does not
hold. Those checks live in the materializers. A caller who constructs a
``RollSchedule`` directly gets the structural guarantees only —
:class:`~quant_research_terminal.domain.exchange_calendar.MaterializedCalendar`
has the same shape and the same limitation, and neither pretends otherwise.
"""

from __future__ import annotations

import re
from bisect import bisect_right
from datetime import datetime
from enum import StrEnum, unique
from typing import Final, final

from pydantic import field_validator, model_validator

from quant_research_terminal.domain.contract_lifecycle import _require_exact_trading_date
from quant_research_terminal.domain.exchange_calendar import (
    WINDOW_STATES,
    CalendarId,
    CalendarRangeError,
    CalendarResolver,
    CalendarVersion,
    ExchangeTradingState,
    MaterializedCalendar,
    TradingDate,
)
from quant_research_terminal.domain.futures_contract import (
    FuturesContractId,
    FuturesProduct,
    _CanonicalModel,
    require_listed_contract,
)
from quant_research_terminal.domain.time import validate_utc_datetime

#: A content hash is a SHA-256 hex digest, nothing looser.
_CONTENT_HASH_PATTERN: Final[re.Pattern[str]] = re.compile(r"[0-9a-f]{64}")


class RolloverError(Exception):
    """Base class for every rollover-domain error."""


class UnsupportedRollTimestampError(RolloverError):
    """An instant lies outside a schedule's supported range.

    Unknown is not "the front month": outside its declared range the schedule
    refuses to answer rather than extrapolating a mapping.
    """


class RollScheduleRangeError(RolloverError):
    """A trading date lies outside a schedule's supported trading-date range."""


class RollCalendarRangeError(RolloverError):
    """The calendar cannot answer for a date the roll computation needs."""


class CalendarMismatchError(RolloverError):
    """A schedule was asked to segment against a calendar it was not built on.

    Roll instants are only meaningful relative to the trade-date labelling
    that produced them, so segmenting against a different calendar would
    silently produce wrong answers rather than obviously wrong ones.
    """


class RollMaterializationError(RolloverError):
    """A roll definition could not be deterministically materialized."""


class LifecycleCalendarMismatchError(RollMaterializationError):
    """An authored lifecycle fact contradicts the authored calendar."""


class InsufficientCalendarCoverageError(RollMaterializationError):
    """The calendar does not reach far enough back to resolve a roll date."""


class NoTradingWindowError(RollMaterializationError):
    """A target trading date has no TRADING window for the chosen boundary."""


class RollOutsideSupportedRangeError(RollMaterializationError):
    """A derived roll instant falls outside the declared supported range."""


def _require_listed(value: object, *, label: str) -> FuturesContractId:
    try:
        return require_listed_contract(value)
    except TypeError as error:
        raise ValueError(f"{label}: {error}") from error


@unique
class RollDerivationKind(StrEnum):
    """How a roll decision was reached — a *mechanism*, not an evidence grade.

    Deliberately not
    :class:`~quant_research_terminal.domain.exchange_calendar.VerificationStatus`.
    That enum grades how strongly a published *exchange fact* is evidenced
    (``verified-primary`` / ``corroborated``); a roll is a research *decision*,
    and grading a decision as "verified-primary" would be the same overclaim
    ADR-010's defect D10 removed from the calendar. Mixing the two ontologies
    would also mean adding a member here could weaken the calendar's evidence
    policy, since a calendar rule could then be authored with it.
    """

    #: The operator stated this roll explicitly.
    OPERATOR_DECLARED = "operator-declared"
    #: Derived deterministically from a lifecycle fact plus a calendar.
    CALENDAR_DERIVED = "calendar-derived"


@final
class RollProvenance(_CanonicalModel):
    """How one roll was derived, and what evidence stands behind it."""

    derivation_kind: RollDerivationKind
    evidence_ids: tuple[str, ...]

    @field_validator("evidence_ids")
    @classmethod
    def _validate_evidence_ids(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if not value:
            raise ValueError("roll provenance must reference at least one evidence record")
        for item in value:
            if not isinstance(item, str) or not item:
                raise ValueError(f"evidence id must be a non-empty string; got {item!r}")
        return value


@final
class RollEvent(_CanonicalModel):
    """One materialized roll: the instant a schedule stops holding A and holds B.

    ``decision_time`` and ``effective_time`` are deliberately **not** collapsed.
    A roll can never become effective before the decision that produced it
    exists, and keeping the two coordinates apart is what lets a future
    data-driven rule prove it consumed no information published after the
    decision. ``decision_time == effective_time`` is permitted and is what the
    fixed-calendar rule emits — see
    :mod:`quant_research_terminal.domain.rollover_materialization` for exactly
    what that equality does and does not claim.

    ``rule_key`` links the event back to the rule that produced it, which is how
    provenance reaches a consumer without being copied onto every event, and how
    an auditor tells a calendar-derived roll from an operator-declared one.
    """

    from_contract: FuturesContractId
    to_contract: FuturesContractId
    decision_time: datetime
    effective_time: datetime
    rule_key: str

    @field_validator("from_contract", "to_contract")
    @classmethod
    def _validate_contracts(cls, value: FuturesContractId) -> FuturesContractId:
        return _require_listed(value, label="a roll event names a listed contract")

    @field_validator("decision_time", "effective_time", mode="before")
    @classmethod
    def _validate_instants(cls, value: object) -> datetime:
        return validate_utc_datetime(value)

    @field_validator("rule_key")
    @classmethod
    def _validate_rule_key(cls, value: str) -> str:
        if not value:
            raise ValueError("rule_key must not be empty")
        return value

    @model_validator(mode="after")
    def _validate_event(self) -> RollEvent:
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
                f"a roll cannot take effect before the decision that produced it; "
                f"decision {self.decision_time.isoformat()} is after effective "
                f"{self.effective_time.isoformat()}"
            )
        return self


@final
class ContractSegment(_CanonicalModel):
    """One maximal interval of a trading date held by a single contract.

    Produced by intersecting a trading date's calendar windows with the roll
    partition, so a segment never spans a gap between windows and never spans a
    roll. ``state`` is the exchange state of the window the segment came from —
    carried so a consumer cannot mistake a pre-open or halt interval for
    tradable time — and is restricted to :data:`WINDOW_STATES` for the same
    reason ``MaterializedWindow`` is: CLOSED is the complement of windows, so a
    segment derived from a window can never be CLOSED.

    ``roll_rule_key`` is ``None`` when the segment is held by the schedule's
    initial contract, because no roll produced it.
    """

    start: datetime
    end: datetime
    contract: FuturesContractId
    state: ExchangeTradingState
    trading_date: TradingDate
    window_rule_key: str
    roll_rule_key: str | None

    @field_validator("start", "end", mode="before")
    @classmethod
    def _validate_instants(cls, value: object) -> datetime:
        return validate_utc_datetime(value)

    @field_validator("contract")
    @classmethod
    def _validate_contract(cls, value: FuturesContractId) -> FuturesContractId:
        return _require_listed(value, label="a segment names a listed contract")

    @field_validator("trading_date")
    @classmethod
    def _validate_trading_date(cls, value: TradingDate) -> TradingDate:
        return _require_exact_trading_date(value)

    @field_validator("state")
    @classmethod
    def _validate_state(cls, value: ExchangeTradingState) -> ExchangeTradingState:
        if value not in WINDOW_STATES:
            raise ValueError(
                f"a segment's state must be one of "
                f"{sorted(s.value for s in WINDOW_STATES)}; a segment is derived from a "
                f"window, and CLOSED is the complement of windows, got {value!r}"
            )
        return value

    @model_validator(mode="after")
    def _validate_span(self) -> ContractSegment:
        if self.start >= self.end:
            raise ValueError(
                f"a segment must span forward; got [{self.start.isoformat()}, "
                f"{self.end.isoformat()}). Zero-length segments are never emitted"
            )
        return self


@final
class RollSchedule(_CanonicalModel):
    """The materialized mapping from UTC instants to listed contracts.

    The supported range is half-open ``[supported_start, supported_end)`` and
    the mapping over it is total and single-valued. ``initial_contract`` is
    *defined* as the contract active at ``supported_start``.

    Why events must lie **strictly** inside the range
    -------------------------------------------------
    Every hashed field must make a difference to the time-indexed mapping, or
    the hash distinguishes artifacts that answer identically. An event
    effective exactly at ``supported_start`` would make ``initial_contract``
    active over an empty interval, so no instant would ever resolve to it; an
    event effective at ``supported_end`` would do the same to its own
    ``to_contract``. Both are rejected.

    (Precisely: such a contract is still *listed* by :meth:`contracts`, which
    enumerates the chain. What it is not is *reachable* through
    :meth:`RollResolver.active_contract_at`, and that mapping is what the
    schedule exists to define.)

    The calendar pin
    ----------------
    ``calendar_id``/``calendar_version``/``calendar_content_hash`` record the
    calendar the schedule's instants were derived against. Without it,
    :func:`segments_for_trading_date` would accept any resolver and silently
    answer against a different trade-date labelling — the same class of defect
    as ADR-010's D1, where a fact that gates the inverse API sat outside the
    pin. The three strings are exactly the shape ADR-010 blessed for pinning a
    calendar in a manifest.
    """

    product: FuturesProduct
    calendar_id: CalendarId
    calendar_version: CalendarVersion
    calendar_content_hash: str
    supported_start: datetime
    supported_end: datetime
    first_trading_date: TradingDate
    last_trading_date: TradingDate
    initial_contract: FuturesContractId
    events: tuple[RollEvent, ...]
    provenance: tuple[tuple[str, RollProvenance], ...]
    content_hash: str

    @field_validator("supported_start", "supported_end", mode="before")
    @classmethod
    def _validate_instants(cls, value: object) -> datetime:
        return validate_utc_datetime(value)

    @field_validator("initial_contract")
    @classmethod
    def _validate_initial_contract(cls, value: FuturesContractId) -> FuturesContractId:
        return _require_listed(value, label="the initial contract must be listed")

    @field_validator("first_trading_date", "last_trading_date")
    @classmethod
    def _validate_trading_dates(cls, value: TradingDate) -> TradingDate:
        return _require_exact_trading_date(value)

    @field_validator("calendar_content_hash", "content_hash")
    @classmethod
    def _validate_hashes(cls, value: str) -> str:
        if _CONTENT_HASH_PATTERN.fullmatch(value) is None:
            raise ValueError(
                f"a content hash must be 64 lower-case hexadecimal characters; got {value!r}"
            )
        return value

    @field_validator("events")
    @classmethod
    def _validate_event_types(cls, value: tuple[RollEvent, ...]) -> tuple[RollEvent, ...]:
        # Exact types, for the reason ADR-009's D4 gives one level down: a
        # subclass may carry extra public state, and the content hash covers
        # only the declared fields — so a forged subclass would be pinned by a
        # hash that does not describe it.
        for item in value:
            if type(item) is not RollEvent:
                raise ValueError(
                    f"events must be exactly RollEvent values; got {type(item).__name__}. A "
                    f"subclass could carry public state the content hash does not cover"
                )
        return value

    @field_validator("provenance")
    @classmethod
    def _validate_provenance_types(
        cls, value: tuple[tuple[str, RollProvenance], ...]
    ) -> tuple[tuple[str, RollProvenance], ...]:
        for key, entry in value:
            if type(entry) is not RollProvenance:
                raise ValueError(
                    f"provenance entries must be exactly RollProvenance values; got "
                    f"{type(entry).__name__} for {key!r}"
                )
        return value

    @model_validator(mode="after")
    def _validate_schedule(self) -> RollSchedule:
        from quant_research_terminal.domain.rollover_materialization import (
            roll_schedule_content_hash,
        )

        if self.supported_start >= self.supported_end:
            raise ValueError("supported_start must be strictly before supported_end")
        if self.first_trading_date.value > self.last_trading_date.value:
            raise ValueError("first_trading_date must not be after last_trading_date")
        if self.initial_contract.product != self.product:
            raise ValueError(
                f"the initial contract belongs to {self.initial_contract.product.canonical()}, "
                f"not to the schedule's product {self.product.canonical()}"
            )

        held = self.initial_contract
        previous_effective: datetime | None = None
        for event in self.events:
            if event.from_contract.product != self.product:
                raise ValueError(
                    f"event {event.from_contract.canonical()} -> "
                    f"{event.to_contract.canonical()} belongs to "
                    f"{event.from_contract.product.canonical()}, not to the schedule's "
                    f"product {self.product.canonical()}"
                )
            if not (self.supported_start < event.effective_time < self.supported_end):
                raise ValueError(
                    f"a roll effective at {event.effective_time.isoformat()} lies outside the "
                    f"open interval ({self.supported_start.isoformat()}, "
                    f"{self.supported_end.isoformat()}); an event at either bound would leave "
                    f"a declared contract unreachable by any instant lookup"
                )
            if previous_effective is not None and event.effective_time <= previous_effective:
                raise ValueError(
                    f"roll events must be strictly increasing in effective time; "
                    f"{event.effective_time.isoformat()} does not follow "
                    f"{previous_effective.isoformat()}. Duplicates are rejected rather than "
                    f"broken by a tiebreak, because one of the two would become unobservable"
                )
            if event.from_contract != held:
                raise ValueError(
                    f"broken roll chain: the event at {event.effective_time.isoformat()} rolls "
                    f"out of {event.from_contract.canonical()}, but the contract active "
                    f"immediately before it is {held.canonical()}"
                )
            previous_effective = event.effective_time
            held = event.to_contract

        # Distinctness, which is structural rather than economic: a chain that
        # revisits a contract makes "the interval during which X was active"
        # ill-defined for every downstream consumer. Delivery-order
        # monotonicity is deliberately NOT enforced — that would encode an
        # economic assumption about which way a series rolls (see ADR-011).
        chain = self.contracts()
        seen: set[FuturesContractId] = set()
        for contract in chain:
            if contract in seen:
                raise ValueError(
                    f"the roll chain revisits {contract.canonical()}; a contract may hold "
                    f"at most one interval in a schedule"
                )
            seen.add(contract)

        keys = [key for key, _ in self.provenance]
        if len(set(keys)) != len(keys):
            raise ValueError("provenance keys must be unique")
        # The tuple's ORDER is public (``schedule.provenance[0]``) but the
        # serialization emits provenance as a sorted JSON object, so order
        # cannot reach the hash. Rather than hash the order, require it to be
        # canonical: then the observable order is a function of the keys, which
        # are hashed, and two schedules that differ observably cannot share a
        # pin. The materializer already emits sorted order.
        if keys != sorted(keys):
            raise ValueError(
                f"provenance must be in sorted key order so that the observable order is "
                f"determined by the hashed keys; got {keys}"
            )
        known = set(keys)
        referenced = {event.rule_key for event in self.events}
        for event in self.events:
            if event.rule_key not in known:
                raise ValueError(f"event rule_key {event.rule_key!r} has no provenance entry")
        # An unreferenced provenance entry changes the content hash without
        # changing any answer, which is the inverse of ADR-010's D1. Sorted so
        # the message is identical on every run and in every process.
        orphans = sorted(known - referenced)
        if orphans:
            raise ValueError(
                f"provenance entries {orphans} are referenced by no event; an orphan entry "
                f"would move the content hash without changing any answer"
            )

        expected = roll_schedule_content_hash(
            product=self.product,
            calendar_id=self.calendar_id,
            calendar_version=self.calendar_version,
            calendar_content_hash=self.calendar_content_hash,
            supported_start=self.supported_start,
            supported_end=self.supported_end,
            first_trading_date=self.first_trading_date,
            last_trading_date=self.last_trading_date,
            initial_contract=self.initial_contract,
            events=self.events,
            provenance=self.provenance,
        )
        if self.content_hash != expected:
            raise ValueError(
                f"content_hash does not match the schedule; expected {expected}, "
                f"got {self.content_hash!r}"
            )
        return self

    def contracts(self) -> tuple[FuturesContractId, ...]:
        """Return every contract the schedule holds, in chain order."""
        return (self.initial_contract, *(event.to_contract for event in self.events))

    def provenance_for(self, rule_key: str) -> RollProvenance:
        """Return the derivation metadata for a rule key.

        Raises:
            KeyError: if the key names no provenance entry.
        """
        for key, value in self.provenance:
            if key == rule_key:
                return value
        raise KeyError(rule_key)


@final
class RollResolver:
    """Pure lookups over one materialized roll schedule.

    Mirrors :class:`~quant_research_terminal.domain.exchange_calendar.CalendarResolver`:
    it precomputes the effective-time index once (a frozen model cannot cache
    it), gives the exact-type guards a home, and performs no wall-clock read, no
    network call, and no timezone arithmetic at query time.
    """

    def __init__(self, schedule: RollSchedule) -> None:
        if type(schedule) is not RollSchedule:
            raise TypeError(f"RollResolver requires a RollSchedule; got {type(schedule).__name__}")
        self._schedule = schedule
        self._effective_times = [event.effective_time for event in schedule.events]

    @property
    def schedule(self) -> RollSchedule:
        """The schedule this resolver answers from."""
        return self._schedule

    def supports(self, timestamp: object) -> bool:
        """Return whether ``timestamp`` lies inside the supported range."""
        instant = validate_utc_datetime(timestamp)
        return self._schedule.supported_start <= instant < self._schedule.supported_end

    def active_contract_at(self, timestamp: object) -> FuturesContractId:
        """Return the listed contract active at a UTC instant.

        Half-open at every roll: for a roll effective at ``R`` the old contract
        is active on ``[…, R)`` and the new one from ``R`` inclusive.

        Raises:
            UnsupportedRollTimestampError: if the instant is outside the range.
            TypeError | ValueError: if the input is not timezone-aware UTC.
        """
        instant = validate_utc_datetime(timestamp)
        schedule = self._schedule
        if not (schedule.supported_start <= instant < schedule.supported_end):
            raise UnsupportedRollTimestampError(
                f"{instant.isoformat()} is outside the supported range "
                f"[{schedule.supported_start.isoformat()}, "
                f"{schedule.supported_end.isoformat()}) of the roll schedule for "
                f"{schedule.product.canonical()}"
            )
        index = bisect_right(self._effective_times, instant)
        if index == 0:
            return schedule.initial_contract
        return schedule.events[index - 1].to_contract

    def _rule_key_at(self, instant: datetime) -> str | None:
        index = bisect_right(self._effective_times, instant)
        return None if index == 0 else self._schedule.events[index - 1].rule_key


def require_matching_calendar_pin(
    *,
    calendar_id: CalendarId,
    calendar_version: CalendarVersion,
    calendar_content_hash: str,
    calendar: MaterializedCalendar,
    action: str,
) -> None:
    """Verify a calendar pin against the calendar actually supplied.

    All **three** pinned fields are checked, not just the content hash. The id
    and version are hashed and publicly readable, so a schedule whose pin says
    one calendar while its instants came from another would be a hashed claim
    that no check ever tests — the same "hashed but never verified" shape the
    content hash exists to rule out.

    Raises:
        CalendarMismatchError: if any pinned field disagrees.
    """
    mismatches = []
    if calendar_id != calendar.calendar_id:
        mismatches.append(f"id {calendar_id.canonical()} != {calendar.calendar_id.canonical()}")
    if calendar_version != calendar.version:
        mismatches.append(
            f"version {calendar_version.canonical()} != {calendar.version.canonical()}"
        )
    if calendar_content_hash != calendar.content_hash:
        mismatches.append(f"content hash {calendar_content_hash} != {calendar.content_hash}")
    if mismatches:
        raise CalendarMismatchError(
            f"{action} was given a calendar that does not match the pinned one: "
            f"{'; '.join(mismatches)}. Roll instants are only meaningful against the "
            f"trade-date labelling that produced them"
        )


def segments_for_trading_date(
    roll_resolver: RollResolver,
    calendar_resolver: CalendarResolver,
    trading_date: TradingDate,
) -> tuple[ContractSegment, ...]:
    """Return the ordered contract segments of one trading date.

    A roll may land inside a trading date, so this returns *segments* rather
    than a contract. Collapsing them would silently pick one side of an
    intraday roll — the single most damaging simplification available here,
    because the wrong side is a real contract and every downstream check would
    pass.

    Both resolvers are explicit arguments: the schedule does not own a calendar,
    and this phase introduces no instrument-to-calendar registry. The calendar
    passed must be the one the schedule was materialized against, which is
    verified against the schedule's calendar pin before anything else is
    computed.

    Behaviour worth knowing:

    * Segments are produced per calendar window, so one never spans a gap
      between two windows and none is ever zero-length.
    * If a roll falls inside a **closed gap** between two of the trading date's
      windows, the roll instant appears in no segment — the window before is
      wholly the old contract and the window after wholly the new. A trading
      date's segments are therefore not necessarily contiguous.
    * A date in range that the exchange did not assign returns ``()``, which is
      the calendar's own factual answer rather than an error.

    Raises:
        TypeError: for any argument of the wrong exact type.
        CalendarMismatchError: if the calendar is not the schedule's.
        RollScheduleRangeError: if the date is outside the schedule's
            trading-date range, or if any of its windows leaves the supported
            instant range. A trading date is answered in full or refused; a
            silently truncated answer is indistinguishable from a genuinely
            short session.
        RollCalendarRangeError: if the calendar cannot answer for the date.
    """
    if type(roll_resolver) is not RollResolver:
        raise TypeError(f"a RollResolver is required; got {type(roll_resolver).__name__}")
    if type(calendar_resolver) is not CalendarResolver:
        raise TypeError(f"a CalendarResolver is required; got {type(calendar_resolver).__name__}")
    if type(trading_date) is not TradingDate:
        raise TypeError(
            f"a TradingDate is required; got {type(trading_date).__name__}. A civil date is "
            f"not a trading date"
        )

    schedule = roll_resolver.schedule
    require_matching_calendar_pin(
        calendar_id=schedule.calendar_id,
        calendar_version=schedule.calendar_version,
        calendar_content_hash=schedule.calendar_content_hash,
        calendar=calendar_resolver.calendar,
        action="segmentation",
    )
    if not (
        schedule.first_trading_date.value <= trading_date.value <= schedule.last_trading_date.value
    ):
        raise RollScheduleRangeError(
            f"trading date {trading_date.canonical()} is outside the schedule's supported "
            f"range [{schedule.first_trading_date.canonical()}, "
            f"{schedule.last_trading_date.canonical()}]"
        )

    try:
        windows = calendar_resolver.windows_for(trading_date)
    except CalendarRangeError as error:
        raise RollCalendarRangeError(
            f"the calendar cannot answer for trading date {trading_date.canonical()}: {error}"
        ) from error

    segments: list[ContractSegment] = []
    for window in windows:
        if not (schedule.supported_start <= window.start and window.end <= schedule.supported_end):
            raise RollScheduleRangeError(
                f"window [{window.start.isoformat()}, {window.end.isoformat()}) of trading "
                f"date {trading_date.canonical()} leaves the schedule's supported range "
                f"[{schedule.supported_start.isoformat()}, "
                f"{schedule.supported_end.isoformat()}); a trading date is answered in full "
                f"or refused, never truncated"
            )
        cuts = [window.start]
        cuts.extend(
            event.effective_time
            for event in schedule.events
            if window.start < event.effective_time < window.end
        )
        cuts.append(window.end)
        for start, end in zip(cuts, cuts[1:], strict=False):
            segments.append(
                ContractSegment(
                    start=start,
                    end=end,
                    contract=roll_resolver.active_contract_at(start),
                    state=window.state,
                    trading_date=trading_date,
                    window_rule_key=window.rule_key,
                    roll_rule_key=roll_resolver._rule_key_at(start),
                )
            )
    return tuple(segments)


__all__ = [
    "CalendarMismatchError",
    "ContractSegment",
    "InsufficientCalendarCoverageError",
    "LifecycleCalendarMismatchError",
    "NoTradingWindowError",
    "RollCalendarRangeError",
    "RollDerivationKind",
    "RollEvent",
    "RollMaterializationError",
    "RollOutsideSupportedRangeError",
    "RollProvenance",
    "RollResolver",
    "RollSchedule",
    "RollScheduleRangeError",
    "RolloverError",
    "UnsupportedRollTimestampError",
    "require_matching_calendar_pin",
    "segments_for_trading_date",
]
