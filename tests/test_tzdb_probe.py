"""Deterministic timezone-database probe (Phase 2.1).

CI previously verified only that ``ZoneInfo("UTC")`` resolves, which says
nothing about whether the *historical rules* for an exchange zone match the
tzdata release the calendars were authored against. Windows resolves zones
from the pinned ``tzdata`` wheel; Linux may prefer a system database. This
probe makes any divergence loud on both, in plain pytest, by asserting the
effective offsets at standard time, daylight time, both 2023 DST transitions,
and the edges of the supported CME calendar range.

The same expectations are embedded declaratively in the calendar definition
(``[[timezone_probe]]``), so materialization itself re-checks them; this test
exists so a divergent environment fails loudly even when no calendar is being
materialized.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

CHICAGO = ZoneInfo("America/Chicago")

PROBES = [
    # (UTC instant, expected UTC offset in minutes, meaning)
    (datetime(2023, 1, 15, 12, 0, tzinfo=UTC), -360, "winter standard time (CST)"),
    (datetime(2023, 6, 15, 12, 0, tzinfo=UTC), -300, "summer daylight time (CDT)"),
    (datetime(2023, 3, 12, 7, 59, 59, tzinfo=UTC), -360, "instant before spring-forward"),
    (datetime(2023, 3, 12, 8, 0, tzinfo=UTC), -300, "instant at spring-forward"),
    (datetime(2023, 11, 5, 6, 59, 59, tzinfo=UTC), -300, "instant before fall-back"),
    (datetime(2023, 11, 5, 7, 0, tzinfo=UTC), -360, "instant at fall-back"),
    (datetime(2023, 5, 21, 21, 0, tzinfo=UTC), -300, "start of the supported CME range"),
    (datetime(2023, 12, 29, 22, 0, tzinfo=UTC), -360, "end of the supported CME range"),
]


@pytest.mark.parametrize(("instant", "expected_minutes", "meaning"), PROBES)
def test_america_chicago_offsets_match_the_authored_expectations(
    instant: datetime, expected_minutes: int, meaning: str
) -> None:
    offset = instant.astimezone(CHICAGO).utcoffset()
    assert offset is not None
    assert offset == timedelta(minutes=expected_minutes), meaning


def test_the_effective_tzdata_source_is_reportable() -> None:
    """The probe's diagnostics must be able to say *which* database answered.

    ``zoneinfo`` prefers a system tzdb where one exists and falls back to the
    ``tzdata`` package. Both are acceptable — the offset assertions above are
    the contract — but the package must at least be importable everywhere so
    the fallback is guaranteed to exist and its version is diagnosable.
    """
    import tzdata  # type: ignore[import-untyped]

    assert isinstance(tzdata.IANA_VERSION, str) and tzdata.IANA_VERSION
