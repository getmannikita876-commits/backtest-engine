"""Deterministic materialization of roll definitions into hashed schedules.

Materialization is where **factual** consistency is checked — against the
calendar and against lifecycle facts — because those checks need inputs a
frozen :class:`~quant_research_terminal.domain.rollover.RollSchedule` does not
hold. The schedule itself guarantees only structural well-formedness. Both
guarantees are real; neither is the other, and the module docstring of
``rollover`` says so plainly rather than implying the factual checks are
unconstructible.

Nothing here reads the wall clock, the network, the process timezone, or a
random source. The same definition and the same calendar always produce the
same events, the same serialization, and the same content hash — on every
platform, under every hash seed.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta
from typing import Final

from quant_research_terminal.domain.contract_lifecycle import ContractLifecycle
from quant_research_terminal.domain.exchange_calendar import (
    CalendarId,
    CalendarRangeError,
    CalendarResolver,
    CalendarVersion,
    ExchangeTradingState,
    TradingDate,
)
from quant_research_terminal.domain.futures_contract import (
    FuturesContractId,
    FuturesProduct,
)
from quant_research_terminal.domain.rollover import (
    InsufficientCalendarCoverageError,
    LifecycleCalendarMismatchError,
    NoTradingWindowError,
    RollCalendarRangeError,
    RollDerivationKind,
    RollEvent,
    RollMaterializationError,
    RollOutsideSupportedRangeError,
    RollProvenance,
    RollSchedule,
    require_matching_calendar_pin,
)
from quant_research_terminal.domain.rollover_definition import (
    EffectiveBoundary,
    ExplicitRollDefinition,
    FixedCalendarRollDefinition,
    _BaseRollDefinition,
)
from quant_research_terminal.domain.time import epoch_microseconds

#: Bumping this is a semantic event: every roll-schedule hash changes, which is
#: exactly what a serialization change must do rather than silently re-keying.
_SERIALIZATION_FORMAT: Final[str] = "qrt-roll-schedule/1"

_ONE_DAY: Final[timedelta] = timedelta(days=1)


def roll_target_trading_date(
    resolver: CalendarResolver,
    *,
    last_trade_date: TradingDate,
    offset: int,
) -> TradingDate:
    """Return the trading date ``offset`` trading dates before ``last_trade_date``.

    ``offset == 0`` returns ``last_trade_date`` itself — rolling on the last
    trade date is legal and useful. ``offset == 1`` returns the immediately
    preceding trading date.

    The cursor steps one **civil** day at a time, but the *calendar* decides
    which of those civil days are trading dates: a candidate counts only when
    ``windows_for`` returns a non-empty tuple. This is emphatically not
    business-day arithmetic — no weekday rule and no holiday list appears here,
    and neither could produce CME's trade-date labelling anyway, where a
    Sunday-evening session belongs to Monday, or to Tuesday across a holiday.

    Termination is bounded by data, not by a magic constant: the walk stops at
    the calendar's first trading date. A ``range(offset * 5)``-style bound would
    fail across a long holiday run, so none appears.

    Raises:
        TypeError: if ``last_trade_date`` is not exactly a ``TradingDate``.
        RollMaterializationError: if ``offset`` is not a non-negative int.
        RollCalendarRangeError: if the last trade date is outside the calendar.
        LifecycleCalendarMismatchError: if the calendar says the authored last
            trade date is not a trading date at all — a contradiction between
            two authored facts, never nudged to a neighbouring date.
        InsufficientCalendarCoverageError: if the calendar does not reach back
            far enough. Never clamped to the earliest available date.
    """
    if type(last_trade_date) is not TradingDate:
        raise TypeError(f"a TradingDate is required; got {type(last_trade_date).__name__}")
    if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
        raise RollMaterializationError(f"roll offset must be a non-negative int; got {offset!r}")

    calendar = resolver.calendar
    try:
        windows = resolver.windows_for(last_trade_date)
    except CalendarRangeError as error:
        raise RollCalendarRangeError(
            f"last trade date {last_trade_date.canonical()} is outside the calendar's "
            f"supported range: {error}"
        ) from error
    if not windows:
        raise LifecycleCalendarMismatchError(
            f"the calendar says {last_trade_date.canonical()} is not a trading date, but a "
            f"lifecycle declares it as a last trade date. One of the two authored facts is "
            f"wrong; the date is not adjusted to a neighbouring one"
        )

    floor = calendar.first_trading_date.value
    cursor = last_trade_date.value
    remaining = offset
    while remaining > 0:
        cursor = cursor - _ONE_DAY  # civil-day cursor; the calendar decides below
        if cursor < floor:
            raise InsufficientCalendarCoverageError(
                f"counting back {offset} trading dates from {last_trade_date.canonical()} runs "
                f"past the calendar's first trading date "
                f"{calendar.first_trading_date.canonical()}; {offset - remaining} were found. "
                f"The roll date is not clamped to the earliest available date"
            )
        if resolver.windows_for(TradingDate(value=cursor)):
            remaining -= 1
    return TradingDate(value=cursor)


def roll_effective_instant(
    resolver: CalendarResolver,
    *,
    trading_date: TradingDate,
    boundary: EffectiveBoundary,
) -> datetime:
    """Return the UTC instant a roll becomes effective on ``trading_date``.

    Both policies name a window of the trading date and take its start, so the
    result is calendar-backed and deterministic. There is no fallback anywhere:
    a trading date with windows but no TRADING window raises rather than
    quietly becoming the first window, and a boundary value that is not one of
    the two members raises rather than falling through to whichever branch
    happens to be last. Both matter because the alternatives are *different
    instants*, which is the whole reason the policy is explicit.

    Raises:
        TypeError: if ``trading_date`` is not exactly a ``TradingDate``, or
            ``boundary`` is not exactly an :class:`EffectiveBoundary` member.
            ``EffectiveBoundary`` is a ``StrEnum``, so the equal-valued bare
            string ``"first-window-start"`` would otherwise compare equal while
            failing an ``is`` test and silently selecting the other policy.
        RollCalendarRangeError: if the calendar cannot answer for the date.
        LifecycleCalendarMismatchError: if the date is not a trading date.
        NoTradingWindowError: if the date has no TRADING window and the chosen
            boundary needs one.
    """
    if type(trading_date) is not TradingDate:
        raise TypeError(f"a TradingDate is required; got {type(trading_date).__name__}")
    if type(boundary) is not EffectiveBoundary:
        raise TypeError(
            f"an EffectiveBoundary member is required, not an equal-valued string; got "
            f"{type(boundary).__name__}: {boundary!r}"
        )
    try:
        windows = resolver.windows_for(trading_date)
    except CalendarRangeError as error:
        raise RollCalendarRangeError(
            f"the calendar cannot answer for trading date {trading_date.canonical()}: {error}"
        ) from error
    if not windows:
        raise LifecycleCalendarMismatchError(
            f"trading date {trading_date.canonical()} has no windows; it is not a trading date"
        )
    if boundary is EffectiveBoundary.FIRST_WINDOW_START:
        return windows[0].start
    if boundary is EffectiveBoundary.FIRST_TRADING_WINDOW_START:
        for window in windows:
            if window.state is ExchangeTradingState.TRADING:
                return window.start
        raise NoTradingWindowError(
            f"trading date {trading_date.canonical()} has no TRADING window, so "
            f"{EffectiveBoundary.FIRST_TRADING_WINDOW_START.value} cannot be resolved; there "
            f"is deliberately no fallback to the first window"
        )
    # Unreachable today, and deliberately explicit: a member added later must
    # fail loudly here rather than inherit the previous branch's behaviour.
    raise RollMaterializationError(f"unhandled effective boundary {boundary!r}")


def canonical_serialization(
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
    events: tuple[RollEvent, ...],
    provenance: tuple[tuple[str, RollProvenance], ...],
) -> bytes:
    """Return the canonical byte serialization of a roll schedule.

    Deterministic by construction: instants are integer epoch microseconds,
    field order is fixed, separators are exact, and nothing consults a set, a
    dict iteration order, a locale, or the hash seed.

    Covers every field that can change an observable answer — including the
    **calendar pin** (segmentation is only meaningful against the calendar that
    produced the trade-date labelling), the **supported trading-date range**
    (it decides whether an out-of-range date raises or returns nothing), each
    event's **rule key**, and the **provenance table** (verification metadata is
    public output, so an edited citation must move the pin — ADR-010's D3).

    Deliberately excluded: the effective-boundary policy and the roll offset.
    They determine the effective instants, which are already covered; two
    policies that yield identical instants yield identical mappings and must
    hash identically. They survive in the rule key, which *is* hashed. Also
    excluded, as always: generation provenance and any free-form prose.
    """
    payload = {
        "format": _SERIALIZATION_FORMAT,
        "product": product.canonical(),
        "calendar": [
            calendar_id.canonical(),
            calendar_version.canonical(),
            calendar_content_hash,
        ],
        "supported_range": [
            epoch_microseconds(supported_start),
            epoch_microseconds(supported_end),
        ],
        "trading_date_range": [
            first_trading_date.canonical(),
            last_trading_date.canonical(),
        ],
        "initial_contract": initial_contract.canonical(),
        "events": [
            [
                event.from_contract.canonical(),
                event.to_contract.canonical(),
                epoch_microseconds(event.decision_time),
                epoch_microseconds(event.effective_time),
                event.rule_key,
            ]
            for event in events
        ],
        "provenance": {
            key: [entry.derivation_kind.value, list(entry.evidence_ids)]
            for key, entry in provenance
        },
    }
    return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("ascii")


def roll_schedule_content_hash(
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
    events: tuple[RollEvent, ...],
    provenance: tuple[tuple[str, RollProvenance], ...],
) -> str:
    """Return the canonical SHA-256 content hash of a roll schedule."""
    return hashlib.sha256(
        canonical_serialization(
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
            provenance=provenance,
        )
    ).hexdigest()


def _require_matching_calendar(definition: _BaseRollDefinition, resolver: CalendarResolver) -> None:
    require_matching_calendar_pin(
        calendar_id=definition.calendar_id,
        calendar_version=definition.calendar_version,
        calendar_content_hash=definition.calendar_content_hash,
        calendar=resolver.calendar,
        action="materialization",
    )


def _assemble(
    definition: _BaseRollDefinition,
    events: tuple[RollEvent, ...],
    provenance: dict[str, RollProvenance],
) -> RollSchedule:
    for event in events:
        if not (definition.supported_start < event.effective_time < definition.supported_end):
            raise RollOutsideSupportedRangeError(
                f"a roll effective at {event.effective_time.isoformat()} lies outside the open "
                f"interval ({definition.supported_start.isoformat()}, "
                f"{definition.supported_end.isoformat()}) of the definition's supported range"
            )
    # Explicit sort key: bare sorted() over the items would fall through to
    # comparing RollProvenance, which defines no ordering.
    ordered_provenance = tuple(sorted(provenance.items(), key=lambda item: item[0]))
    content_hash = roll_schedule_content_hash(
        product=definition.product,
        calendar_id=definition.calendar_id,
        calendar_version=definition.calendar_version,
        calendar_content_hash=definition.calendar_content_hash,
        supported_start=definition.supported_start,
        supported_end=definition.supported_end,
        first_trading_date=definition.first_trading_date,
        last_trading_date=definition.last_trading_date,
        initial_contract=definition.initial_contract,
        events=events,
        provenance=ordered_provenance,
    )
    try:
        return RollSchedule(
            product=definition.product,
            calendar_id=definition.calendar_id,
            calendar_version=definition.calendar_version,
            calendar_content_hash=definition.calendar_content_hash,
            supported_start=definition.supported_start,
            supported_end=definition.supported_end,
            first_trading_date=definition.first_trading_date,
            last_trading_date=definition.last_trading_date,
            initial_contract=definition.initial_contract,
            events=events,
            provenance=ordered_provenance,
            content_hash=content_hash,
        )
    except ValueError as error:
        # Structural problems surface as typed rollover errors rather than a
        # leaked Pydantic ValidationError, mirroring the calendar materializer.
        raise RollMaterializationError(
            f"the definition produced an invalid schedule: {error}"
        ) from error


def _record(provenance: dict[str, RollProvenance], rule_key: str, entry: RollProvenance) -> None:
    """Record a provenance entry, refusing to silently drop a conflicting one.

    Not ``setdefault``. Today's two key schemes cannot collide — ``explicit.N``
    is unique by enumeration, and the fixed-calendar key varies by a contract
    the definition already requires to be distinct — so this branch is
    unreachable from any current input. It is kept because the alternative
    fails in the worst possible way: ``setdefault`` would silently keep the
    first entry and discard the second's evidence, and a future key scheme that
    happened to collide would lose provenance with no signal at all.
    """
    existing = provenance.get(rule_key)
    if existing is not None and existing != entry:
        raise RollMaterializationError(
            f"two different provenance entries claim rule key {rule_key!r}"
        )
    provenance[rule_key] = entry


def materialize_explicit(
    definition: ExplicitRollDefinition, resolver: CalendarResolver
) -> RollSchedule:
    """Materialize an operator-declared roll definition.

    The authored events are carried through in authored order — never sorted —
    and each becomes a ``RollEvent`` with a generated rule key so provenance
    reaches consumers without being copied onto every event.
    """
    _require_matching_calendar(definition, resolver)
    events: list[RollEvent] = []
    provenance: dict[str, RollProvenance] = {}
    for index, authored in enumerate(definition.events):
        rule_key = f"explicit.{index}"
        _record(
            provenance,
            rule_key,
            RollProvenance(
                derivation_kind=RollDerivationKind.OPERATOR_DECLARED,
                evidence_ids=authored.evidence_ids,
            ),
        )
        events.append(
            RollEvent(
                from_contract=authored.from_contract,
                to_contract=authored.to_contract,
                decision_time=authored.decision_time,
                effective_time=authored.effective_time,
                rule_key=rule_key,
            )
        )
    return _assemble(definition, tuple(events), provenance)


def materialize_fixed_calendar(
    definition: FixedCalendarRollDefinition, resolver: CalendarResolver
) -> RollSchedule:
    """Materialize a lifecycle-and-calendar-derived roll definition.

    For each consecutive pair of declared lifecycles, the roll out of the
    earlier contract is placed ``roll_offset_trading_dates`` trading dates
    before that contract's last trade date, at the instant named by the
    definition's effective boundary.

    ``decision_time == effective_time``, and this is a deliberate, bounded
    claim. It says: *the roll became actionable no later than the instant it
    took effect.* It is **not** the publication timestamp of the lifecycle fact,
    **not** when the exchange first announced the last trade date, and **not**
    an information-availability timestamp for any market observation. The
    equality is valid precisely because this rule consumes no market
    observation — the calendar and the last trade date are ex-ante facts — so it
    can never claim knowledge earlier than it existed. A future data-driven
    mechanism does not get to copy this: it must satisfy the stronger contract
    that every observation's availability time precedes its decision time.

    Derived instants are verified to be strictly increasing rather than sorted:
    non-monotone results mean the authored last trade dates were non-monotone,
    which is an authoring error to report, not to repair.
    """
    _require_matching_calendar(definition, resolver)
    events: list[RollEvent] = []
    provenance: dict[str, RollProvenance] = {}
    previous_effective: datetime | None = None
    lifecycles: tuple[ContractLifecycle, ...] = definition.lifecycles

    for index in range(len(lifecycles) - 1):
        current, following = lifecycles[index], lifecycles[index + 1]
        target = roll_target_trading_date(
            resolver,
            last_trade_date=current.last_trade_date,
            offset=definition.roll_offset_trading_dates,
        )
        effective = roll_effective_instant(
            resolver, trading_date=target, boundary=definition.effective_boundary
        )
        if previous_effective is not None and effective <= previous_effective:
            raise RollMaterializationError(
                f"the roll out of {current.contract.canonical()} resolves to "
                f"{effective.isoformat()}, which does not follow the previous roll at "
                f"{previous_effective.isoformat()}; the authored last trade dates are not in "
                f"increasing order. They are reported, not reordered"
            )
        previous_effective = effective
        rule_key = (
            f"fixed-calendar.offset-{definition.roll_offset_trading_dates}."
            f"{definition.effective_boundary.value}.{current.contract.canonical()}"
        )
        _record(
            provenance,
            rule_key,
            RollProvenance(
                derivation_kind=RollDerivationKind.CALENDAR_DERIVED,
                evidence_ids=current.provenance.evidence_ids,
            ),
        )
        events.append(
            RollEvent(
                from_contract=current.contract,
                to_contract=following.contract,
                decision_time=effective,
                effective_time=effective,
                rule_key=rule_key,
            )
        )
    return _assemble(definition, tuple(events), provenance)
