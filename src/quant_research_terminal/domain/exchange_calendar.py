"""Core value types for the exchange-calendar domain.

An exchange calendar answers one deterministic question: *given a UTC instant,
what was the exchange's trading state, and which trading date did that instant
belong to?* Everything in this module exists to make the answer reproducible —
the same pinned calendar returns byte-identical answers on every machine, under
every hash seed, at every wall-clock time, and under every future tzdb update.

The layering, from authored facts to runtime answers::

    CalendarDefinition          authored, versioned facts (calendar_definition)
        │  deterministic materialization (calendar_materialization)
        ▼
    MaterializedCalendar        explicit UTC windows, content-hashed  (here)
        │
        ▼
    CalendarResolver            pure lookups over materialized windows (here)

Two deliberate separations govern the design:

* **Exchange trading state is not research segmentation.** ``TRADING``,
  ``HALT``, ``MAINTENANCE``, and ``CLOSED`` describe what the exchange's
  matching engine was physically doing. RTH/ETH-style research sessions are a
  different, later concept and must never determine tradability.
* **A trading date is not a civil date.** The calendar assigns
  :class:`TradingDate`; nothing derives one from a timestamp's ``.date()``.
  CME's own holiday tables show a Sunday-evening trade belonging to the
  following *Tuesday* when Monday is a holiday — weekday arithmetic cannot
  produce that answer.

No wall clock, no network, no local timezone is read anywhere in this module.
"""

from __future__ import annotations

import re
from bisect import bisect_right
from datetime import date, datetime
from enum import StrEnum, unique
from typing import Final, final

from pydantic import field_validator, model_validator

from quant_research_terminal.domain.futures_contract import _CanonicalModel
from quant_research_terminal.domain.time import validate_utc_datetime

#: Character set for a calendar identity token. Deliberately mirrors the
#: futures-identity policy (ADR-009): ASCII upper-case letters, digits, and the
#: underscore, at least one letter, nothing normalized. A purely numeric token
#: is rejected so an id from some vendor's database can never become a
#: calendar identity by accident.
_CALENDAR_ID_PATTERN: Final[re.Pattern[str]] = re.compile(r"[A-Z0-9_]{1,32}")
_HAS_LETTER_PATTERN: Final[re.Pattern[str]] = re.compile("[A-Z]")

#: A materialized content hash is a SHA-256 hex digest, nothing looser.
_CONTENT_HASH_PATTERN: Final[re.Pattern[str]] = re.compile(r"[0-9a-f]{64}")


class CalendarError(Exception):
    """Base class for every calendar-domain error."""


class CalendarDefinitionError(CalendarError):
    """An authored calendar definition is invalid or internally inconsistent."""


class MaterializationError(CalendarError):
    """A definition could not be deterministically materialized."""


class NonexistentLocalTimeError(MaterializationError):
    """A rule boundary names a local wall time skipped by a DST transition.

    Never silently normalized: a nonexistent local time in a rule is an
    authoring error, and shifting it forward would silently move a session
    boundary.
    """


class AmbiguousLocalTimeError(MaterializationError):
    """A rule boundary names a local wall time that occurs twice (DST fold).

    Never silently resolved with ``fold=0``: an ambiguous boundary must be
    re-authored so it is unambiguous, because either implicit choice moves the
    boundary by an hour for half of its readers.
    """


class UnsupportedTimestampError(CalendarError):
    """A timestamp lies outside the calendar's materialized coverage.

    Unknown is not "normal": outside the supported range the calendar refuses
    to answer rather than extrapolating a schedule.
    """


class CalendarRangeError(CalendarError):
    """A trading date lies outside the calendar's supported date range."""


@unique
class ExchangeTradingState(StrEnum):
    """What the exchange's matching engine is doing at an instant.

    Values follow CME Globex's own published vocabulary where one exists
    (OPEN / PREOPEN-HALT / CLOSED); ``MAINTENANCE`` is reserved for a
    schedule that publishes an explicit maintenance interval. This is
    *physical* exchange state — research session labels (RTH, overnight,
    custom windows) are a separate concept and must never be encoded here.
    """

    #: Continuous trading; order matching is running.
    TRADING = "trading"
    #: A scheduled non-matching interval published as part of the trading
    #: schedule (CME "PREOPEN (HALT)": order entry allowed, no matching).
    HALT = "halt"
    #: An explicitly published maintenance interval. Distinct from CLOSED so a
    #: maintenance instant is never misreported as an exchange holiday.
    MAINTENANCE = "maintenance"
    #: No published window covers the instant: after the final close, weekend,
    #: or holiday closure inside the supported range.
    CLOSED = "closed"


#: States that are represented by explicit materialized windows. CLOSED is the
#: complement of these within the supported coverage and is never materialized
#: as thousands of explicit windows.
WINDOW_STATES: Final[frozenset[ExchangeTradingState]] = frozenset(
    {
        ExchangeTradingState.TRADING,
        ExchangeTradingState.HALT,
        ExchangeTradingState.MAINTENANCE,
    }
)


@unique
class VerificationStatus(StrEnum):
    """How strongly a calendar fact is supported by evidence.

    Only the statuses actually used by the shipped definitions exist. A status
    can describe limited evidence for a fact that *is* encoded; it can never
    legitimize encoding a fact for which there is no evidence — an unknown
    schedule is an unsupported range, not an "unverified" rule.
    """

    #: Stated directly by an official primary source for this rule.
    VERIFIED_PRIMARY = "verified-primary"
    #: Supported by primary-source instances, with the rule's general
    #: application inferred from their consistency rather than stated once.
    CORROBORATED = "corroborated"


@final
class CalendarId(_CanonicalModel):
    """The logical identity of an exchange calendar.

    A calendar id names a *schedule class* — one published trading schedule —
    not a venue: one venue operates many schedules (equities, energy, grains
    all differ), so ``Venue("CME")`` can never stand in for a calendar
    identity, and no venue registry is implied.
    """

    token: str

    @field_validator("token")
    @classmethod
    def _validate_token(cls, value: str) -> str:
        if not value:
            raise ValueError("calendar id must not be empty")
        if _CALENDAR_ID_PATTERN.fullmatch(value) is None:
            raise ValueError(
                f"calendar id must consist only of ASCII upper-case letters, digits, and "
                f"underscores (max 32 characters); got {value!r}. Values are never trimmed "
                f"or upper-cased — supply the canonical spelling"
            )
        if _HAS_LETTER_PATTERN.search(value) is None:
            raise ValueError(
                f"calendar id must contain at least one ASCII letter so a bare numeric "
                f"identifier cannot become a calendar identity; got {value!r}"
            )
        return value

    def canonical(self) -> str:
        """Return the deterministic canonical form of this calendar id."""
        return self.token


@final
class CalendarVersion(_CanonicalModel):
    """The logical version of a calendar definition.

    Versions are corrections, not mutations: a released version is immutable,
    and a corrected holiday or rule produces a *new* version with a new
    materialized content hash while the old version keeps reproducing its old
    answers.
    """

    number: int

    @field_validator("number")
    @classmethod
    def _validate_number(cls, value: int) -> int:
        if value < 1:
            raise ValueError(f"calendar version must be a positive integer; got {value!r}")
        return value

    def canonical(self) -> str:
        """Return the deterministic canonical form, e.g. ``v1``."""
        return f"v{self.number}"


@final
class TradingDate(_CanonicalModel):
    """A trading date assigned by an exchange calendar.

    Not a civil date: a trading date is a label the exchange attaches to a
    session, and the session it labels may start on the *previous* civil day
    (a Sunday 17:00 open belongs to Monday — or to Tuesday across a holiday).
    The only authority that assigns one to a timestamp is a calendar resolver;
    ``timestamp.date()`` is never a trading date, and the validator below
    rejects ``datetime`` (which *is-a* ``date``) precisely so that a datetime
    cannot slide through where a calendar-assigned date is required.
    """

    value: date

    @field_validator("value", mode="before")
    @classmethod
    def _validate_value(cls, value: object) -> date:
        # ``datetime`` is a subclass of ``date``; accepting one would let
        # ``TradingDate(value=some_timestamp)`` silently truncate a timestamp
        # into a civil date, which is exactly the misuse this type exists to
        # prevent. An exact type check also rejects hostile date subclasses.
        if type(value) is not date:
            raise ValueError(
                f"trading date value must be a datetime.date (and not a datetime); "
                f"got {type(value).__name__}: {value!r}. A trading date is assigned by "
                f"a calendar, never derived from a timestamp"
            )
        return value

    @classmethod
    def from_iso(cls, text: str) -> TradingDate:
        """Return the trading date written as an ISO ``YYYY-MM-DD`` string."""
        if not isinstance(text, str):
            raise ValueError(f"trading date must be an ISO date string; got {text!r}")
        try:
            parsed = date.fromisoformat(text)
        except ValueError as error:
            raise ValueError(
                f"trading date must be an ISO YYYY-MM-DD string; got {text!r}"
            ) from error
        return cls(value=parsed)

    def canonical(self) -> str:
        """Return the deterministic canonical form, ISO ``YYYY-MM-DD``."""
        return self.value.isoformat()


@final
class RuleProvenance(_CanonicalModel):
    """Verification metadata for one authored rule or dated exception."""

    status: VerificationStatus
    evidence_ids: tuple[str, ...]

    @field_validator("evidence_ids")
    @classmethod
    def _validate_evidence_ids(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if not value:
            raise ValueError("rule provenance must reference at least one evidence record")
        for item in value:
            if not item or not isinstance(item, str):
                raise ValueError(f"evidence id must be a non-empty string; got {item!r}")
        return value


@final
class MaterializedWindow(_CanonicalModel):
    """One explicit half-open UTC window ``[start, end)`` of exchange activity.

    An instant exactly at ``end`` belongs to whatever follows, never to this
    window. Only :data:`WINDOW_STATES` are materialized; CLOSED is the
    complement. ``trading_date`` carries the exchange's own trade-date label
    for the window; the label is authored data, never derived.
    ``rule_key`` links the window back to the definition rule or dated
    exception that produced it, which is how effective verification metadata
    reaches a resolved context without being copied onto every window.
    """

    start: datetime
    end: datetime
    state: ExchangeTradingState
    trading_date: TradingDate
    rule_key: str

    @field_validator("start", "end", mode="before")
    @classmethod
    def _validate_bounds(cls, value: object) -> datetime:
        return validate_utc_datetime(value)

    @field_validator("state")
    @classmethod
    def _validate_state(cls, value: ExchangeTradingState) -> ExchangeTradingState:
        if value not in WINDOW_STATES:
            raise ValueError(
                f"a materialized window's state must be one of "
                f"{sorted(s.value for s in WINDOW_STATES)}; CLOSED is represented as the "
                f"complement of materialized windows, got {value!r}"
            )
        return value

    @field_validator("rule_key")
    @classmethod
    def _validate_rule_key(cls, value: str) -> str:
        if not value:
            raise ValueError("rule_key must not be empty")
        return value

    @model_validator(mode="after")
    def _validate_order(self) -> MaterializedWindow:
        if self.start >= self.end:
            raise ValueError(
                f"window start must be strictly before end; got "
                f"[{self.start.isoformat()}, {self.end.isoformat()})"
            )
        return self


@final
class MaterializedCalendar(_CanonicalModel):
    """The deterministic materialization of one calendar definition version.

    Every window is explicit UTC; windows are strictly ordered and
    non-overlapping; coverage bounds delimit the instants the calendar is
    willing to answer for. ``content_hash`` is the reproducibility pin for the
    schedule's semantics — see
    :func:`quant_research_terminal.domain.calendar_materialization.materialized_content_hash`.

    The hash stored here is re-derived and checked on construction, so a
    ``MaterializedCalendar`` carrying a hash that does not match its own
    windows is unconstructible rather than merely wrong.
    """

    calendar_id: CalendarId
    version: CalendarVersion
    timezone_name: str
    first_trading_date: TradingDate
    last_trading_date: TradingDate
    coverage_start: datetime
    coverage_end: datetime
    windows: tuple[MaterializedWindow, ...]
    provenance: tuple[tuple[str, RuleProvenance], ...]
    content_hash: str

    @field_validator("coverage_start", "coverage_end", mode="before")
    @classmethod
    def _validate_coverage(cls, value: object) -> datetime:
        return validate_utc_datetime(value)

    @model_validator(mode="after")
    def _validate_consistency(self) -> MaterializedCalendar:
        # Imported here to avoid a module cycle: the materializer imports these
        # types, and this validator needs only the canonical hash function.
        from quant_research_terminal.domain.calendar_materialization import (
            materialized_content_hash,
        )

        if not self.windows:
            raise ValueError("a materialized calendar must contain at least one window")
        if self.coverage_start >= self.coverage_end:
            raise ValueError("coverage_start must be strictly before coverage_end")
        if self.first_trading_date.value > self.last_trading_date.value:
            raise ValueError("first_trading_date must not be after last_trading_date")

        previous: MaterializedWindow | None = None
        for window in self.windows:
            if window.start < self.coverage_start or window.end > self.coverage_end:
                raise ValueError(
                    f"window [{window.start.isoformat()}, {window.end.isoformat()}) lies "
                    f"outside coverage"
                )
            if previous is not None and window.start < previous.end:
                raise ValueError(
                    f"windows must be strictly ordered and non-overlapping; "
                    f"[{previous.start.isoformat()}, {previous.end.isoformat()}) is followed "
                    f"by [{window.start.isoformat()}, {window.end.isoformat()})"
                )
            previous = window
            # A window labelled with a trading date outside the declared range
            # would be unreachable through the inverse API: windows_for would
            # raise CalendarRangeError for a date that demonstrably owns a
            # window, so forward and inverse lookups would disagree (D8).
            if not (
                self.first_trading_date.value
                <= window.trading_date.value
                <= self.last_trading_date.value
            ):
                raise ValueError(
                    f"window [{window.start.isoformat()}, {window.end.isoformat()}) is "
                    f"labelled with trading date {window.trading_date.canonical()}, outside "
                    f"the declared range [{self.first_trading_date.canonical()}, "
                    f"{self.last_trading_date.canonical()}]"
                )

        keys = [key for key, _ in self.provenance]
        if len(set(keys)) != len(keys):
            raise ValueError("provenance keys must be unique")
        known = set(keys)
        for window in self.windows:
            if window.rule_key not in known:
                raise ValueError(f"window rule_key {window.rule_key!r} has no provenance entry")

        expected = materialized_content_hash(
            calendar_id=self.calendar_id,
            version=self.version,
            timezone_name=self.timezone_name,
            first_trading_date=self.first_trading_date,
            last_trading_date=self.last_trading_date,
            coverage_start=self.coverage_start,
            coverage_end=self.coverage_end,
            windows=self.windows,
            provenance=self.provenance,
        )
        if self.content_hash != expected:
            raise ValueError(
                f"content_hash does not match the materialized windows; expected "
                f"{expected}, got {self.content_hash!r}"
            )
        return self

    def provenance_for(self, rule_key: str) -> RuleProvenance:
        """Return the verification metadata for a rule key.

        Raises:
            KeyError: if the key names no provenance entry.
        """
        for key, value in self.provenance:
            if key == rule_key:
                return value
        raise KeyError(rule_key)


@final
class CalendarContext(_CanonicalModel):
    """The calendar's complete answer for one UTC instant.

    ``state`` may be any :class:`ExchangeTradingState`, including CLOSED, in
    which case the window bounds delimit the closed gap and ``trading_date``
    and ``verification`` are absent — a closed gap belongs to no trade date
    and is derived from the absence of windows rather than from an authored
    rule. ``next_transition`` is the instant the state next changes, or
    ``None`` when the current window runs to the end of coverage — the
    calendar refuses to describe a transition it cannot see.
    """

    timestamp: datetime
    state: ExchangeTradingState
    trading_date: TradingDate | None
    window_start: datetime
    window_end: datetime
    next_transition: datetime | None
    calendar_id: CalendarId
    calendar_version: CalendarVersion
    content_hash: str
    verification: RuleProvenance | None

    @field_validator("timestamp", "window_start", "window_end", mode="before")
    @classmethod
    def _validate_instants(cls, value: object) -> datetime:
        return validate_utc_datetime(value)

    @field_validator("next_transition", mode="before")
    @classmethod
    def _validate_next_transition(cls, value: object) -> datetime | None:
        if value is None:
            return None
        return validate_utc_datetime(value)

    @field_validator("content_hash")
    @classmethod
    def _validate_content_hash(cls, value: str) -> str:
        if _CONTENT_HASH_PATTERN.fullmatch(value) is None:
            raise ValueError(
                f"content_hash must be 64 lower-case hexadecimal characters; got {value!r}"
            )
        return value

    @model_validator(mode="after")
    def _validate_coherence(self) -> CalendarContext:
        # A context is one internally consistent answer, not a bag of fields
        # (falsification defect D7): a CLOSED gap belongs to no trade date and
        # no authored rule; every other state came from a window that has
        # both. The instant must lie inside the window it is answered with,
        # and the next transition — when the calendar can see one — is by
        # definition the current window's end.
        if self.window_start >= self.window_end:
            raise ValueError("window_start must be strictly before window_end")
        if not (self.window_start <= self.timestamp < self.window_end):
            raise ValueError(
                f"timestamp {self.timestamp.isoformat()} lies outside its own window "
                f"[{self.window_start.isoformat()}, {self.window_end.isoformat()})"
            )
        if self.state is ExchangeTradingState.CLOSED:
            if self.trading_date is not None or self.verification is not None:
                raise ValueError(
                    "a CLOSED context carries no trading date and no verification "
                    "metadata; a closed gap belongs to no trade date and no rule"
                )
        else:
            if self.trading_date is None or self.verification is None:
                raise ValueError(
                    f"a {self.state.value} context must carry the window's trading date "
                    f"and the originating rule's verification metadata"
                )
        if self.next_transition is not None and self.next_transition != self.window_end:
            raise ValueError(
                f"next_transition must be the current window's end "
                f"({self.window_end.isoformat()}) or None at the coverage edge; got "
                f"{self.next_transition.isoformat()}"
            )
        return self


@final
class CalendarResolver:
    """Pure forward and inverse lookups over one materialized calendar.

    The resolver consumes materialized UTC windows only: no local-timezone
    arithmetic, no rule evaluation, and no wall clock happens at query time,
    so replay-time answers cannot depend on the tzdb present at replay time.

    The resolver reports temporal truth and nothing else. It exposes
    verification metadata but makes no policy decision — warning, blocking,
    or permitting research on weak evidence belongs to future application
    layers, never here.
    """

    def __init__(self, calendar: MaterializedCalendar) -> None:
        if type(calendar) is not MaterializedCalendar:
            raise TypeError(
                f"CalendarResolver requires a MaterializedCalendar; got {type(calendar).__name__}"
            )
        self._calendar = calendar
        self._starts = [window.start for window in calendar.windows]

    @property
    def calendar(self) -> MaterializedCalendar:
        """The materialized calendar this resolver answers from."""
        return self._calendar

    def supports(self, timestamp: object) -> bool:
        """Return whether ``timestamp`` lies inside the materialized coverage.

        The timestamp must still be a valid timezone-aware UTC datetime; only
        the *range* question is answered leniently.
        """
        instant = validate_utc_datetime(timestamp)
        calendar = self._calendar
        return calendar.coverage_start <= instant < calendar.coverage_end

    def resolve(self, timestamp: object) -> CalendarContext:
        """Return the calendar's answer for one UTC instant.

        Raises:
            UnsupportedTimestampError: if the instant lies outside coverage.
            TypeError | ValueError: if the input is not timezone-aware UTC.
        """
        instant = validate_utc_datetime(timestamp)
        calendar = self._calendar
        if not (calendar.coverage_start <= instant < calendar.coverage_end):
            raise UnsupportedTimestampError(
                f"{instant.isoformat()} is outside the supported coverage "
                f"[{calendar.coverage_start.isoformat()}, "
                f"{calendar.coverage_end.isoformat()}) of calendar "
                f"{calendar.calendar_id.canonical()} {calendar.version.canonical()}"
            )

        index = bisect_right(self._starts, instant) - 1
        window = calendar.windows[index] if index >= 0 else None

        if window is not None and instant < window.end:
            # The state changes at window.end: either a CLOSED gap begins there
            # or the next window starts exactly there. Either way the next
            # transition is window.end — unless the window runs to the end of
            # coverage, where the calendar refuses to look beyond what it knows.
            next_transition = window.end if window.end < calendar.coverage_end else None
            return CalendarContext(
                timestamp=instant,
                state=window.state,
                trading_date=window.trading_date,
                window_start=window.start,
                window_end=window.end,
                next_transition=next_transition,
                calendar_id=calendar.calendar_id,
                calendar_version=calendar.version,
                content_hash=calendar.content_hash,
                verification=calendar.provenance_for(window.rule_key),
            )

        # Inside coverage but inside no window: a CLOSED gap. Its bounds are
        # the surrounding windows' edges (or the coverage bounds).
        gap_start = window.end if window is not None else calendar.coverage_start
        gap_end = (
            calendar.windows[index + 1].start
            if index + 1 < len(calendar.windows)
            else calendar.coverage_end
        )
        next_transition = gap_end if gap_end < calendar.coverage_end else None
        return CalendarContext(
            timestamp=instant,
            state=ExchangeTradingState.CLOSED,
            trading_date=None,
            window_start=gap_start,
            window_end=gap_end,
            next_transition=next_transition,
            calendar_id=calendar.calendar_id,
            calendar_version=calendar.version,
            content_hash=calendar.content_hash,
            verification=None,
        )

    def windows_for(self, trading_date: TradingDate) -> tuple[MaterializedWindow, ...]:
        """Return the ordered UTC windows labelled with ``trading_date``.

        A trading date may own several disjoint windows — a holiday halt can
        split one trade date's trading into multiple OPEN windows — so the
        result is a tuple, in start order. An in-range date that the exchange
        did not assign as a trade date (weekends, holidays) returns an empty
        tuple: that is the calendar's factual answer, not an error.

        Raises:
            CalendarRangeError: if the date lies outside the supported
                trading-date range.
            TypeError: if the argument is not a :class:`TradingDate`.
        """
        if type(trading_date) is not TradingDate:
            raise TypeError(
                f"windows_for requires a TradingDate; got {type(trading_date).__name__}. "
                f"A civil date is not a trading date"
            )
        calendar = self._calendar
        if not (
            calendar.first_trading_date.value
            <= trading_date.value
            <= calendar.last_trading_date.value
        ):
            raise CalendarRangeError(
                f"trading date {trading_date.canonical()} is outside the supported range "
                f"[{calendar.first_trading_date.canonical()}, "
                f"{calendar.last_trading_date.canonical()}] of calendar "
                f"{calendar.calendar_id.canonical()} {calendar.version.canonical()}"
            )
        return tuple(window for window in calendar.windows if window.trading_date == trading_date)
