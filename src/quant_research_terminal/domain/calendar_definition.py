"""Authored exchange-calendar definitions: versioned facts, not behavior.

A :class:`CalendarDefinition` is the *input* to deterministic materialization.
It carries schedule facts — recurring weekly window rules, dated exceptions,
the exchange-local IANA timezone, the supported trading-date range — together
with the evidence citations those facts rest on. It contains no lookup logic
and no conversion logic; those live in ``calendar_materialization`` and
``exchange_calendar``.

Definitions are authored as declarative data (TOML, loaded by the
``calendars`` package) and validated through these models. Nothing here reads
a file, the network, or the clock; the models only judge whatever structure
they are handed. No real exchange's schedule fact appears in this module —
facts live in versioned definition data with evidence attached.

Timezone policy: the one authored local-time reference is an IANA zone
identifier of ``Area/Location`` form that resolves in the tzdb.
Abbreviations (CST/EST), bare zone words (UTC), POSIX composites (CST6CDT),
and offset-bearing names (UTC-6, Etc/GMT+6) are rejected structurally — they
are ambiguous, politically unstable, or not civil zones at all. What the
pattern deliberately does **not** do is judge between tzdb spellings: an
alias such as ``US/Central`` that resolves in the tzdb is accepted exactly as
written and never silently canonicalized to ``America/Chicago`` — the
definition says what its author wrote, and the author is expected to write
the canonical zone.
"""

from __future__ import annotations

import re
from datetime import date, datetime, time, timedelta
from typing import Final, final
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import field_validator, model_validator

from quant_research_terminal.domain.exchange_calendar import (
    WINDOW_STATES,
    CalendarId,
    CalendarVersion,
    ExchangeTradingState,
    VerificationStatus,
)
from quant_research_terminal.domain.futures_contract import _CanonicalModel
from quant_research_terminal.domain.time import validate_utc_datetime

#: An authored IANA zone must be ``Area/Location`` (optionally deeper, e.g.
#: ``America/Argentina/Ushuaia``). This structurally rejects abbreviations
#: (``CST``), bare words (``UTC``), and POSIX-style zones (``CST6CDT``,
#: ``Etc/GMT+6`` is rejected by the digit ban in the final component). It does
#: not reject tzdb aliases (``US/Central``): those resolve, are kept exactly
#: as written, and are never canonicalized — see the module docstring.
_IANA_ZONE_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"[A-Za-z]+(?:/[A-Za-z_+-]*[A-Za-z][A-Za-z_+-]*)+"
)

#: Evidence ids are short stable tokens usable as cross-references from both
#: the definition data and the durable evidence register document.
_EVIDENCE_ID_PATTERN: Final[re.Pattern[str]] = re.compile(r"[a-z0-9][a-z0-9-]{0,63}")

#: Day offsets in weekly rules are relative to the trading date. An exchange
#: session can begin on the evening before its trading date (offset -1); wider
#: offsets have no evidenced use and are rejected rather than speculated about.
_MIN_DAY_OFFSET: Final[int] = -1
_MAX_DAY_OFFSET: Final[int] = 0

#: How far before its trading date an exception window may begin. The widest
#: evidenced CME shape is a holiday trade date whose session opens two civil
#: days earlier (e.g. trade date Friday 2023-11-24 opening Wednesday evening);
#: three days admits a weekend-anchored variant without admitting typos.
_MAX_EXCEPTION_LOOKBACK: Final[timedelta] = timedelta(days=3)

#: Weekday tokens, indexed by ``date.weekday()`` order (Monday = 0).
WEEKDAY_TOKENS: Final[tuple[str, ...]] = (
    "monday",
    "tuesday",
    "wednesday",
    "thursday",
    "friday",
    "saturday",
    "sunday",
)


def _validate_local_time(value: object, *, label: str) -> time:
    """Return a plain local wall time, rejecting anything timezone-tagged."""
    if type(value) is not time:
        raise ValueError(f"{label} must be a datetime.time; got {type(value).__name__}: {value!r}")
    if value.tzinfo is not None:
        raise ValueError(
            f"{label} must be a plain local wall time; the timezone comes from the "
            f"definition's IANA zone, got tzinfo={value.tzinfo!r}"
        )
    return value


@final
class EvidenceRecord(_CanonicalModel):
    """A citation for one or more calendar facts.

    Concise by design: the record carries the claim and where it can be
    re-verified, never a copied source document. The durable narrative
    register lives in project documentation; these records key into it.
    """

    evidence_id: str
    source_type: str
    title: str
    source_url: str
    archive_url: str
    accessed: date
    claim: str

    @field_validator("evidence_id")
    @classmethod
    def _validate_evidence_id(cls, value: str) -> str:
        if _EVIDENCE_ID_PATTERN.fullmatch(value) is None:
            raise ValueError(
                f"evidence id must be a lower-case token of letters, digits, and hyphens; "
                f"got {value!r}"
            )
        return value

    @field_validator("source_type", "title", "source_url", "claim")
    @classmethod
    def _validate_non_empty(cls, value: str) -> str:
        if not value.strip() or value != value.strip():
            raise ValueError(
                f"evidence fields must be non-empty and untrimmed-clean; got {value!r}"
            )
        return value

    @field_validator("archive_url")
    @classmethod
    def _validate_archive_url(cls, value: str) -> str:
        # May be empty when the source itself is the durable artifact, but
        # never whitespace-padded.
        if value != value.strip():
            raise ValueError(f"archive_url must not carry surrounding whitespace; got {value!r}")
        return value

    @field_validator("accessed", mode="before")
    @classmethod
    def _validate_accessed(cls, value: object) -> date:
        if type(value) is not date:
            raise ValueError(f"accessed must be a datetime.date; got {value!r}")
        return value


@final
class WeeklyWindowRule(_CanonicalModel):
    """One recurring window of a trading date's session, in local wall time.

    Offsets are civil-day offsets from the trading date itself: ``-1`` means
    the calendar day before the trading date. A rule whose span does not move
    strictly forward in local wall-clock terms is rejected here; DST-related
    validity of the *instants* is the materializer's job, where the zone is
    known.
    """

    state: ExchangeTradingState
    start_day_offset: int
    start_time: time
    end_day_offset: int
    end_time: time
    provenance_status: VerificationStatus
    evidence_ids: tuple[str, ...]

    @field_validator("state")
    @classmethod
    def _validate_state(cls, value: ExchangeTradingState) -> ExchangeTradingState:
        if value not in WINDOW_STATES:
            raise ValueError(
                f"a weekly rule's state must be one of "
                f"{sorted(s.value for s in WINDOW_STATES)}; CLOSED is never authored, "
                f"it is the complement, got {value!r}"
            )
        return value

    @field_validator("start_day_offset", "end_day_offset")
    @classmethod
    def _validate_offset(cls, value: int) -> int:
        if not _MIN_DAY_OFFSET <= value <= _MAX_DAY_OFFSET:
            raise ValueError(
                f"day offsets must be between {_MIN_DAY_OFFSET} and {_MAX_DAY_OFFSET}; "
                f"got {value!r}"
            )
        return value

    @field_validator("start_time", "end_time", mode="before")
    @classmethod
    def _validate_times(cls, value: object) -> time:
        return _validate_local_time(value, label="weekly rule time")

    @field_validator("evidence_ids")
    @classmethod
    def _validate_evidence_ids(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if not value:
            raise ValueError("a weekly rule must cite at least one evidence record")
        return value

    @model_validator(mode="after")
    def _validate_span(self) -> WeeklyWindowRule:
        start_key = (self.start_day_offset, self.start_time)
        end_key = (self.end_day_offset, self.end_time)
        if start_key >= end_key:
            raise ValueError(
                f"a weekly rule must span strictly forward in local time; got "
                f"day {self.start_day_offset} {self.start_time.isoformat()} to "
                f"day {self.end_day_offset} {self.end_time.isoformat()}"
            )
        return self


@final
class ExceptionWindow(_CanonicalModel):
    """One explicit local-time window of a dated exception."""

    state: ExchangeTradingState
    start_date: date
    start_time: time
    end_date: date
    end_time: time

    @field_validator("state")
    @classmethod
    def _validate_state(cls, value: ExchangeTradingState) -> ExchangeTradingState:
        if value not in WINDOW_STATES:
            raise ValueError(
                f"an exception window's state must be one of "
                f"{sorted(s.value for s in WINDOW_STATES)}; got {value!r}"
            )
        return value

    @field_validator("start_date", "end_date", mode="before")
    @classmethod
    def _validate_dates(cls, value: object) -> date:
        if type(value) is not date:
            raise ValueError(f"exception window dates must be datetime.date; got {value!r}")
        return value

    @field_validator("start_time", "end_time", mode="before")
    @classmethod
    def _validate_times(cls, value: object) -> time:
        return _validate_local_time(value, label="exception window time")

    @model_validator(mode="after")
    def _validate_span(self) -> ExceptionWindow:
        if (self.start_date, self.start_time) >= (self.end_date, self.end_time):
            raise ValueError(
                f"an exception window must span strictly forward in local time; got "
                f"{self.start_date.isoformat()} {self.start_time.isoformat()} to "
                f"{self.end_date.isoformat()} {self.end_time.isoformat()}"
            )
        return self


@final
class DatedException(_CanonicalModel):
    """A dated deviation from the weekly rules, backed by evidence.

    Two kinds exist, both explicit:

    * ``removed`` — the date is not a trading date at all. Its weekly-template
      windows (including any evening-before windows) simply do not exist.
    * ``replaced`` — the date is a trading date whose windows are the explicit
      list given here, in full. Nothing is merged with the weekly template:
      partial overrides invite silent drift between the template and the
      exception, so an exception restates its trading date's entire session.
    """

    trading_date: date
    kind: str
    windows: tuple[ExceptionWindow, ...]
    provenance_status: VerificationStatus
    evidence_ids: tuple[str, ...]

    @field_validator("trading_date", mode="before")
    @classmethod
    def _validate_trading_date(cls, value: object) -> date:
        if type(value) is not date:
            raise ValueError(f"exception trading_date must be a datetime.date; got {value!r}")
        return value

    @field_validator("kind")
    @classmethod
    def _validate_kind(cls, value: str) -> str:
        if value not in ("removed", "replaced"):
            raise ValueError(f"exception kind must be 'removed' or 'replaced'; got {value!r}")
        return value

    @field_validator("evidence_ids")
    @classmethod
    def _validate_evidence_ids(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if not value:
            raise ValueError("a dated exception must cite at least one evidence record")
        return value

    @model_validator(mode="after")
    def _validate_windows(self) -> DatedException:
        if self.kind == "removed" and self.windows:
            raise ValueError("a 'removed' exception must not carry windows")
        if self.kind == "replaced" and not self.windows:
            raise ValueError("a 'replaced' exception must carry at least one window")
        earliest_start = self.trading_date - _MAX_EXCEPTION_LOOKBACK
        for window in self.windows:
            # Locality guard (falsification defect D2): an exception restates
            # one trading date's session, and every evidenced CME session lies
            # within a few civil days ending on its trade date. A window far
            # from its date is an authoring error — most likely a typo'd year
            # or month — and must fail here, not materialize quietly.
            if window.start_date < earliest_start:
                raise ValueError(
                    f"exception window for trading date {self.trading_date.isoformat()} "
                    f"starts {window.start_date.isoformat()}, more than "
                    f"{_MAX_EXCEPTION_LOOKBACK.days} days before its trading date"
                )
            if window.end_date > self.trading_date:
                raise ValueError(
                    f"exception window for trading date {self.trading_date.isoformat()} "
                    f"ends {window.end_date.isoformat()}, after its trading date; a "
                    f"session never outlives the trade date that labels it"
                )
        previous: ExceptionWindow | None = None
        for window in self.windows:
            if previous is not None and (window.start_date, window.start_time) < (
                previous.end_date,
                previous.end_time,
            ):
                raise ValueError(
                    f"exception windows for {self.trading_date.isoformat()} must be ordered "
                    f"and non-overlapping in local time"
                )
            previous = window
        return self


@final
class TimezoneProbe(_CanonicalModel):
    """One declarative tzdb expectation checked during materialization.

    The probe pins what the effective timezone database must say for the
    definition's zone at a specific UTC instant. If the environment's tzdb
    disagrees — a stale system database, a divergent platform — materialization
    fails loudly instead of silently emitting shifted windows.
    """

    utc_instant: datetime
    expected_utc_offset_minutes: int

    @field_validator("utc_instant", mode="before")
    @classmethod
    def _validate_instant(cls, value: object) -> datetime:
        return validate_utc_datetime(value)


@final
class CalendarDefinition(_CanonicalModel):
    """A complete, versioned, evidence-carrying calendar definition."""

    calendar_id: CalendarId
    version: CalendarVersion
    timezone_name: str
    first_trading_date: date
    last_trading_date: date
    weekly_rules: tuple[tuple[str, tuple[WeeklyWindowRule, ...]], ...]
    exceptions: tuple[DatedException, ...]
    timezone_probes: tuple[TimezoneProbe, ...]
    evidence: tuple[EvidenceRecord, ...]

    @field_validator("timezone_name")
    @classmethod
    def _validate_timezone_name(cls, value: str) -> str:
        if _IANA_ZONE_PATTERN.fullmatch(value) is None:
            raise ValueError(
                f"timezone must be an IANA Region/City identifier; abbreviations (CST), "
                f"bare words (UTC), and fixed offsets (UTC-6, Etc/GMT+6) are rejected; "
                f"got {value!r}"
            )
        try:
            ZoneInfo(value)
        except ZoneInfoNotFoundError as error:
            raise ValueError(f"timezone {value!r} is not present in the tzdb") from error
        return value

    @field_validator("first_trading_date", "last_trading_date", mode="before")
    @classmethod
    def _validate_range_dates(cls, value: object) -> date:
        if type(value) is not date:
            raise ValueError(f"supported-range dates must be datetime.date; got {value!r}")
        return value

    @model_validator(mode="after")
    def _validate_consistency(self) -> CalendarDefinition:
        if self.first_trading_date > self.last_trading_date:
            raise ValueError("first_trading_date must not be after last_trading_date")

        weekday_names = [name for name, _ in self.weekly_rules]
        if len(set(weekday_names)) != len(weekday_names):
            raise ValueError("weekly rules must name each weekday at most once")
        for name, rules in self.weekly_rules:
            if name not in WEEKDAY_TOKENS:
                raise ValueError(
                    f"unknown weekday token {name!r}; expected one of {WEEKDAY_TOKENS}"
                )
            if not rules:
                raise ValueError(
                    f"weekday {name!r} lists no windows; omit the weekday instead of "
                    f"declaring an empty session"
                )
            previous: WeeklyWindowRule | None = None
            for rule in rules:
                if previous is not None and (rule.start_day_offset, rule.start_time) < (
                    previous.end_day_offset,
                    previous.end_time,
                ):
                    raise ValueError(
                        f"weekly rules for {name!r} must be ordered and non-overlapping "
                        f"in local time"
                    )
                previous = rule

        known_evidence = {record.evidence_id for record in self.evidence}
        if len(known_evidence) != len(self.evidence):
            raise ValueError("evidence ids must be unique")

        def _check_refs(ids: tuple[str, ...], owner: str) -> None:
            for evidence_id in ids:
                if evidence_id not in known_evidence:
                    raise ValueError(f"{owner} cites unknown evidence id {evidence_id!r}")

        for name, rules in self.weekly_rules:
            for rule in rules:
                _check_refs(rule.evidence_ids, f"weekly rule for {name!r}")

        seen_exception_dates: set[date] = set()
        for exception in self.exceptions:
            if exception.trading_date in seen_exception_dates:
                raise ValueError(
                    f"duplicate exception for trading date {exception.trading_date.isoformat()}"
                )
            seen_exception_dates.add(exception.trading_date)
            if not (self.first_trading_date <= exception.trading_date <= self.last_trading_date):
                raise ValueError(
                    f"exception trading date {exception.trading_date.isoformat()} lies "
                    f"outside the supported range"
                )
            _check_refs(
                exception.evidence_ids,
                f"exception for {exception.trading_date.isoformat()}",
            )
        return self

    def weekly_rules_for(self, weekday_token: str) -> tuple[WeeklyWindowRule, ...] | None:
        """Return the weekly rules for a weekday token, or ``None`` if absent."""
        for name, rules in self.weekly_rules:
            if name == weekday_token:
                return rules
        return None

    def exception_for(self, trading_date: date) -> DatedException | None:
        """Return the dated exception for a date, or ``None`` if there is none."""
        for exception in self.exceptions:
            if exception.trading_date == trading_date:
                return exception
        return None
