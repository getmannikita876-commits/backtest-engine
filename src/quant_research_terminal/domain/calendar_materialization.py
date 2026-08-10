"""Deterministic materialization of a calendar definition into UTC windows.

Materialization is the one place local-time arithmetic is allowed to happen.
Everything downstream — the resolver, replay, research — consumes the explicit
UTC windows produced here, so no replay-time answer can depend on the timezone
database present at replay time. The trade is deliberate: tzdb sensitivity is
concentrated at generation, where the definition's declarative probes make any
divergence loud, instead of being smeared across every query.

DST is handled adversarially, never silently:

* A rule boundary naming a **nonexistent** local time (spring-forward gap)
  raises :class:`NonexistentLocalTimeError`.
* A rule boundary naming an **ambiguous** local time (fall-back fold) raises
  :class:`AmbiguousLocalTimeError`. There is no implicit ``fold=0`` anywhere.

The content hash produced here is the strongest reproducibility pin for a
materialized calendar: it covers every observable field of the artifact —
identity, version, authored timezone, declared trading-date range, coverage
bounds, every window's microsecond UTC bounds, state, trading date, and rule
key, and the provenance table the resolver serves verification metadata
from. It deliberately excludes generation provenance (tzdb release,
generator version): two environments that produce byte-identical artifacts
have produced the same calendar, and pinning environment noise into the hash
would make identical semantics look different.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, date, datetime, time, timedelta
from typing import Final
from zoneinfo import ZoneInfo

from quant_research_terminal.domain.calendar_definition import (
    WEEKDAY_TOKENS,
    CalendarDefinition,
    DatedException,
)
from quant_research_terminal.domain.exchange_calendar import (
    AmbiguousLocalTimeError,
    CalendarId,
    CalendarVersion,
    MaterializationError,
    MaterializedCalendar,
    MaterializedWindow,
    NonexistentLocalTimeError,
    RuleProvenance,
    TradingDate,
)

_ONE_DAY: Final[timedelta] = timedelta(days=1)

#: Version of the canonical serialization format used for hashing. Bumping it
#: is a semantic event: every content hash changes, which is exactly what a
#: serialization change must do rather than silently re-keying artifacts.
_SERIALIZATION_FORMAT: Final[str] = "qrt-materialized-calendar/1"


def local_wall_time_to_utc(local_day: date, wall_time: time, zone: ZoneInfo) -> datetime:
    """Convert an exchange-local wall time to an exact UTC instant.

    Raises:
        NonexistentLocalTimeError: if the wall time falls in a DST gap.
        AmbiguousLocalTimeError: if the wall time falls in a DST fold. No
            implicit disambiguation exists; an ambiguous schedule boundary is
            an authoring defect that must be fixed in the definition.
    """
    naive = datetime.combine(local_day, wall_time)
    fold0 = naive.replace(tzinfo=zone, fold=0)
    fold1 = naive.replace(tzinfo=zone, fold=1)
    offset0 = fold0.utcoffset()
    offset1 = fold1.utcoffset()
    if offset0 is None or offset1 is None:  # pragma: no cover - ZoneInfo always offsets
        raise MaterializationError(f"zone {zone.key!r} produced no UTC offset for {naive}")

    if offset0 != offset1:
        # The wall time is either skipped (gap) or repeated (fold). Round-trip
        # through UTC: a skipped wall time cannot survive the trip intact.
        round_trip = fold0.astimezone(UTC).astimezone(zone)
        if round_trip.replace(tzinfo=None) != naive:
            raise NonexistentLocalTimeError(
                f"local time {naive.isoformat()} does not exist in zone {zone.key!r} "
                f"(DST spring-forward gap); the definition must not name it"
            )
        raise AmbiguousLocalTimeError(
            f"local time {naive.isoformat()} occurs twice in zone {zone.key!r} "
            f"(DST fall-back fold); the definition must name an unambiguous boundary"
        )

    return fold0.astimezone(UTC)


def materialized_content_hash(
    *,
    calendar_id: CalendarId,
    version: CalendarVersion,
    timezone_name: str,
    first_trading_date: TradingDate,
    last_trading_date: TradingDate,
    coverage_start: datetime,
    coverage_end: datetime,
    windows: tuple[MaterializedWindow, ...],
    provenance: tuple[tuple[str, RuleProvenance], ...],
) -> str:
    """Return the canonical SHA-256 content hash of a materialized calendar."""
    return hashlib.sha256(
        canonical_serialization(
            calendar_id=calendar_id,
            version=version,
            timezone_name=timezone_name,
            first_trading_date=first_trading_date,
            last_trading_date=last_trading_date,
            coverage_start=coverage_start,
            coverage_end=coverage_end,
            windows=windows,
            provenance=provenance,
        )
    ).hexdigest()


def canonical_serialization(
    *,
    calendar_id: CalendarId,
    version: CalendarVersion,
    timezone_name: str,
    first_trading_date: TradingDate,
    last_trading_date: TradingDate,
    coverage_start: datetime,
    coverage_end: datetime,
    windows: tuple[MaterializedWindow, ...],
    provenance: tuple[tuple[str, RuleProvenance], ...],
) -> bytes:
    """Return the canonical byte serialization of a materialized calendar.

    Deterministic by construction: instants are integer microseconds since the
    Unix epoch (the platform's canonical persisted precision), field order is
    fixed, separators are exact, and nothing consults a set, a dict iteration
    order, a locale, or the hash seed.

    The serialization covers **every observable field** of the artifact — not
    only the UTC windows but the declared trading-date range (defect D1: it
    gates the inverse API), each window's originating rule key and the
    provenance table (defect D3: `CalendarContext.verification` is public
    resolver output, so a changed evidence citation must change the pin), and
    the authored timezone name. What stays out is environment provenance —
    the tzdb release, platform, generator build — because two environments
    that produce byte-identical artifacts have produced the same calendar.
    """
    payload = {
        "format": _SERIALIZATION_FORMAT,
        "calendar_id": calendar_id.canonical(),
        "version": version.canonical(),
        "timezone": timezone_name,
        "trading_date_range": [
            first_trading_date.canonical(),
            last_trading_date.canonical(),
        ],
        "coverage": [
            _epoch_microseconds(coverage_start),
            _epoch_microseconds(coverage_end),
        ],
        "windows": [
            [
                _epoch_microseconds(window.start),
                _epoch_microseconds(window.end),
                window.state.value,
                window.trading_date.canonical(),
                window.rule_key,
            ]
            for window in windows
        ],
        "provenance": {
            key: [entry.status.value, list(entry.evidence_ids)] for key, entry in provenance
        },
    }
    return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("ascii")


def _epoch_microseconds(instant: datetime) -> int:
    return (instant - datetime(1970, 1, 1, tzinfo=UTC)) // timedelta(microseconds=1)


def materialize(definition: CalendarDefinition) -> MaterializedCalendar:
    """Deterministically materialize a definition into explicit UTC windows.

    The same definition and the same effective tzdb always produce the same
    windows, the same serialization, and the same content hash — on every
    platform, under every hash seed. A tzdb that disagrees with the
    definition's declarative probes fails materialization loudly.

    Raises:
        MaterializationError: for probe failures and structural inconsistency.
        NonexistentLocalTimeError | AmbiguousLocalTimeError: for DST-invalid
            rule boundaries.
    """
    zone = ZoneInfo(definition.timezone_name)
    _check_timezone_probes(definition, zone)

    windows: list[MaterializedWindow] = []
    provenance: dict[str, RuleProvenance] = {}

    day = definition.first_trading_date
    while day <= definition.last_trading_date:
        exception = definition.exception_for(day)
        if exception is not None:
            windows.extend(_exception_windows(exception, day, zone, provenance))
        else:
            windows.extend(_weekly_windows(definition, day, zone, provenance))
        day += _ONE_DAY

    if not windows:
        raise MaterializationError(
            f"definition {definition.calendar_id.canonical()} "
            f"{definition.version.canonical()} produced no windows over its supported range"
        )

    ordered = sorted(windows, key=lambda window: (window.start, window.end))
    previous: MaterializedWindow | None = None
    for window in ordered:
        if previous is not None and window.start < previous.end:
            raise MaterializationError(
                f"materialization produced overlapping windows: "
                f"[{previous.start.isoformat()}, {previous.end.isoformat()}) and "
                f"[{window.start.isoformat()}, {window.end.isoformat()})"
            )
        previous = window

    coverage_start = ordered[0].start
    coverage_end = ordered[-1].end
    window_tuple = tuple(ordered)
    provenance_tuple = tuple(sorted(provenance.items()))
    content_hash = materialized_content_hash(
        calendar_id=definition.calendar_id,
        version=definition.version,
        timezone_name=definition.timezone_name,
        first_trading_date=TradingDate(value=definition.first_trading_date),
        last_trading_date=TradingDate(value=definition.last_trading_date),
        coverage_start=coverage_start,
        coverage_end=coverage_end,
        windows=window_tuple,
        provenance=provenance_tuple,
    )
    return MaterializedCalendar(
        calendar_id=definition.calendar_id,
        version=definition.version,
        timezone_name=definition.timezone_name,
        first_trading_date=TradingDate(value=definition.first_trading_date),
        last_trading_date=TradingDate(value=definition.last_trading_date),
        coverage_start=coverage_start,
        coverage_end=coverage_end,
        windows=window_tuple,
        provenance=provenance_tuple,
        content_hash=content_hash,
    )


def _check_timezone_probes(definition: CalendarDefinition, zone: ZoneInfo) -> None:
    """Fail loudly if the effective tzdb disagrees with the declared probes."""
    for probe in definition.timezone_probes:
        instant = probe.utc_instant
        offset = instant.astimezone(zone).utcoffset()
        if offset is None:  # pragma: no cover - ZoneInfo always offsets
            raise MaterializationError(f"zone {zone.key!r} produced no offset for {instant}")
        # Exact timedelta comparison, no unit conversion: converting the
        # actual offset to whole minutes would quantize away a sub-minute
        # divergence — tzdb history contains second-precision offsets — and
        # a probe that rounds is a probe that can be lied to (defect D6).
        if offset != timedelta(minutes=probe.expected_utc_offset_minutes):
            raise MaterializationError(
                f"timezone probe failed for {zone.key!r} at {instant.isoformat()}: the "
                f"definition expects UTC offset {probe.expected_utc_offset_minutes} minutes "
                f"but the effective tzdb yields {offset}. The environment's "
                f"timezone data diverges from the one the calendar was authored against"
            )


def _weekly_windows(
    definition: CalendarDefinition,
    trading_day: date,
    zone: ZoneInfo,
    provenance: dict[str, RuleProvenance],
) -> list[MaterializedWindow]:
    weekday_token = WEEKDAY_TOKENS[trading_day.weekday()]
    rules = definition.weekly_rules_for(weekday_token)
    if rules is None:
        return []
    result: list[MaterializedWindow] = []
    trading_date = TradingDate(value=trading_day)
    for index, rule in enumerate(rules):
        rule_key = f"weekly.{weekday_token}.{index}"
        provenance.setdefault(
            rule_key,
            RuleProvenance(status=rule.provenance_status, evidence_ids=rule.evidence_ids),
        )
        start = local_wall_time_to_utc(
            trading_day + timedelta(days=rule.start_day_offset), rule.start_time, zone
        )
        end = local_wall_time_to_utc(
            trading_day + timedelta(days=rule.end_day_offset), rule.end_time, zone
        )
        result.append(
            MaterializedWindow(
                start=start,
                end=end,
                state=rule.state,
                trading_date=trading_date,
                rule_key=rule_key,
            )
        )
    return result


def _exception_windows(
    exception: DatedException,
    trading_day: date,
    zone: ZoneInfo,
    provenance: dict[str, RuleProvenance],
) -> list[MaterializedWindow]:
    rule_key = f"exception.{trading_day.isoformat()}"
    provenance.setdefault(
        rule_key,
        RuleProvenance(status=exception.provenance_status, evidence_ids=exception.evidence_ids),
    )
    if exception.kind == "removed":
        return []
    trading_date = TradingDate(value=trading_day)
    return [
        MaterializedWindow(
            start=local_wall_time_to_utc(window.start_date, window.start_time, zone),
            end=local_wall_time_to_utc(window.end_date, window.end_time, zone),
            state=window.state,
            trading_date=trading_date,
            rule_key=rule_key,
        )
        for window in exception.windows
    ]
