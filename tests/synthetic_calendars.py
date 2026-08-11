"""Synthetic exchange calendars for tests that need one but assert nothing about it.

Several rollover test modules need *a* calendar in order to exercise trading
dates, windows, and boundary policies. Building one inline in each would
duplicate forty lines four times, so the builder lives here and is imported
explicitly — not installed as a ``conftest.py`` fixture, because ADR-007
rejected ambient wiring and an explicit import says where the calendar came
from.

These calendars are **synthetic**: a real IANA zone, an invented schedule. No
CME fact is encoded here, so nothing in this module can drift out of agreement
with the evidence register. Tests that want realistic weekend and holiday
shapes use the committed ``CME_EQUITY_INDEX`` calendar read-only instead.
"""

from __future__ import annotations

from datetime import date, time

from quant_research_terminal.domain.calendar_definition import (
    CalendarDefinition,
    DatedException,
    EvidenceRecord,
    WeeklyWindowRule,
)
from quant_research_terminal.domain.calendar_materialization import materialize
from quant_research_terminal.domain.exchange_calendar import (
    CalendarId,
    CalendarResolver,
    CalendarVersion,
    ExchangeTradingState,
    MaterializedCalendar,
    VerificationStatus,
)

SYNTHETIC_EVIDENCE = EvidenceRecord(
    evidence_id="synthetic-evidence",
    source_type="synthetic",
    title="A synthetic schedule used only by tests",
    source_url="https://example.invalid/synthetic",
    archive_url="",
    accessed=date(2026, 8, 10),
    claim="An invented schedule; it asserts nothing about any real exchange.",
)

WEEKDAYS = ("monday", "tuesday", "wednesday", "thursday", "friday")


def session_rules(
    *,
    preopen_start: time = time(16, 45),
    open_time: time = time(17, 0),
    close_time: time = time(16, 0),
) -> tuple[WeeklyWindowRule, ...]:
    """Return one weekday's windows: an evening pre-open, then a session.

    Shaped like a real overnight futures session — the session opens on the
    civil day *before* its trading date — because that is precisely the case a
    civil-date assumption would get wrong.
    """
    return (
        WeeklyWindowRule(
            state=ExchangeTradingState.HALT,
            start_day_offset=-1,
            start_time=preopen_start,
            end_day_offset=-1,
            end_time=open_time,
            provenance_status=VerificationStatus.VERIFIED_PRIMARY,
            evidence_ids=("synthetic-evidence",),
        ),
        WeeklyWindowRule(
            state=ExchangeTradingState.TRADING,
            start_day_offset=-1,
            start_time=open_time,
            end_day_offset=0,
            end_time=close_time,
            provenance_status=VerificationStatus.VERIFIED_PRIMARY,
            evidence_ids=("synthetic-evidence",),
        ),
    )


def synthetic_definition(
    *,
    first: date,
    last: date,
    version: int = 1,
    weekday_rules: tuple[WeeklyWindowRule, ...] | None = None,
    exceptions: tuple[DatedException, ...] = (),
    token: str = "SYNTHETIC_TEST",
) -> CalendarDefinition:
    """Return a Monday-to-Friday synthetic calendar definition."""
    rules = session_rules() if weekday_rules is None else weekday_rules
    return CalendarDefinition(
        calendar_id=CalendarId(token=token),
        version=CalendarVersion(number=version),
        timezone_name="America/Chicago",
        first_trading_date=first,
        last_trading_date=last,
        weekly_rules=tuple((weekday, rules) for weekday in WEEKDAYS),
        exceptions=exceptions,
        timezone_probes=(),
        evidence=(SYNTHETIC_EVIDENCE,),
    )


def synthetic_calendar(**kwargs: object) -> MaterializedCalendar:
    """Materialize a synthetic calendar. Keyword arguments match the definition."""
    return materialize(synthetic_definition(**kwargs))  # type: ignore[arg-type]


def synthetic_resolver(**kwargs: object) -> CalendarResolver:
    """Return a resolver over a synthetic calendar."""
    return CalendarResolver(synthetic_calendar(**kwargs))


def removed_trading_date(trading_date: date) -> DatedException:
    """Return an exception removing one trading date, as a holiday would."""
    return DatedException(
        trading_date=trading_date,
        kind="removed",
        windows=(),
        provenance_status=VerificationStatus.VERIFIED_PRIMARY,
        evidence_ids=("synthetic-evidence",),
    )
