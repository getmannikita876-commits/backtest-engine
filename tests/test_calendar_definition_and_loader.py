"""Tests for authored calendar definitions and the strict TOML loader.

A definition is data another engineer must be able to audit a year later, so
the loader and models are tested as gatekeepers: unknown keys, missing keys,
timezone abbreviations, fixed offsets, dangling evidence references, and
partially-authored exceptions must all fail loudly rather than load quietly.
"""

from __future__ import annotations

from datetime import date, time, timedelta, timezone
from datetime import time as time_type

import pytest
from pydantic import ValidationError

from quant_research_terminal.calendars import (
    load_cme_equity_index_v1,
    load_packaged_definition,
    parse_calendar_definition,
)
from quant_research_terminal.domain.calendar_definition import (
    CalendarDefinition,
    DatedException,
    EvidenceRecord,
    ExceptionWindow,
    WeeklyWindowRule,
)
from quant_research_terminal.domain.exchange_calendar import (
    CalendarDefinitionError,
    CalendarId,
    CalendarVersion,
    ExchangeTradingState,
    VerificationStatus,
)

EVIDENCE = EvidenceRecord(
    evidence_id="test-evidence",
    source_type="test",
    title="A test claim",
    source_url="https://example.invalid/source",
    archive_url="",
    accessed=date(2026, 8, 10),
    claim="A synthetic fact used only by tests.",
)


def weekly_rule(
    state: ExchangeTradingState = ExchangeTradingState.TRADING,
    start_day_offset: int = -1,
    start_time: time = time(17, 0),
    end_day_offset: int = 0,
    end_time: time = time(16, 0),
) -> WeeklyWindowRule:
    return WeeklyWindowRule(
        state=state,
        start_day_offset=start_day_offset,
        start_time=start_time,
        end_day_offset=end_day_offset,
        end_time=end_time,
        provenance_status=VerificationStatus.VERIFIED_PRIMARY,
        evidence_ids=("test-evidence",),
    )


def definition(
    *,
    timezone_name: str = "America/Chicago",
    weekly_rules: tuple[tuple[str, tuple[WeeklyWindowRule, ...]], ...] | None = None,
    exceptions: tuple[DatedException, ...] = (),
    evidence: tuple[EvidenceRecord, ...] = (EVIDENCE,),
) -> CalendarDefinition:
    if weekly_rules is None:
        weekly_rules = (("monday", (weekly_rule(),)),)
    return CalendarDefinition(
        calendar_id=CalendarId(token="SYNTHETIC_TEST"),
        version=CalendarVersion(number=1),
        timezone_name=timezone_name,
        first_trading_date=date(2024, 1, 8),
        last_trading_date=date(2024, 1, 12),
        weekly_rules=weekly_rules,
        exceptions=exceptions,
        timezone_probes=(),
        evidence=evidence,
    )


# --------------------------------------------------------------------------
# Timezone policy
# --------------------------------------------------------------------------


def test_definition_accepts_a_canonical_iana_zone() -> None:
    assert definition().timezone_name == "America/Chicago"


@pytest.mark.parametrize(
    "zone",
    [
        "CST",  # abbreviation
        "EST",
        "UTC",  # bare word — a calendar is authored in a civil zone
        "UTC-6",  # fixed offset
        "GMT+6",
        "Etc/GMT+6",  # POSIX-style fixed offset inside the tzdb
        "CST6CDT",  # POSIX composite
        "America/Chicago ",  # padding is never trimmed
        " America/Chicago",
        "America/NotAZone",  # not in the tzdb
    ],
)
def test_definition_rejects_non_iana_or_unknown_zones(zone: str) -> None:
    with pytest.raises(ValidationError):
        definition(timezone_name=zone)


def test_weekly_rule_rejects_timezone_tagged_times() -> None:
    with pytest.raises(ValidationError):
        weekly_rule(start_time=time_type(17, 0, tzinfo=timezone(timedelta(hours=-6))))


# --------------------------------------------------------------------------
# Weekly rules
# --------------------------------------------------------------------------


def test_weekly_rule_rejects_the_closed_state() -> None:
    with pytest.raises(ValidationError):
        weekly_rule(state=ExchangeTradingState.CLOSED)


@pytest.mark.parametrize("offset", [-2, 1, 7])
def test_weekly_rule_rejects_out_of_range_offsets(offset: int) -> None:
    with pytest.raises(ValidationError):
        weekly_rule(start_day_offset=offset)


def test_weekly_rule_rejects_a_backwards_span() -> None:
    with pytest.raises(ValidationError):
        weekly_rule(
            start_day_offset=0, start_time=time(16, 0), end_day_offset=0, end_time=time(15, 0)
        )


def test_weekly_rules_must_not_overlap_within_a_weekday() -> None:
    overlapping = (
        (
            "monday",
            (
                weekly_rule(start_time=time(17, 0), end_time=time(16, 0)),
                weekly_rule(
                    start_day_offset=0,
                    start_time=time(15, 0),
                    end_day_offset=0,
                    end_time=time(15, 30),
                ),
            ),
        ),
    )
    with pytest.raises(ValidationError):
        definition(weekly_rules=overlapping)


def test_weekly_rules_reject_unknown_weekday_tokens() -> None:
    with pytest.raises(ValidationError):
        definition(weekly_rules=(("monntag", (weekly_rule(),)),))


def test_weekly_rules_reject_duplicate_weekdays() -> None:
    with pytest.raises(ValidationError):
        definition(weekly_rules=(("monday", (weekly_rule(),)), ("monday", (weekly_rule(),))))


def test_weekly_rules_reject_an_empty_weekday() -> None:
    with pytest.raises(ValidationError):
        definition(weekly_rules=(("monday", ()),))


def test_weekly_rules_reject_unknown_evidence_ids() -> None:
    rule = WeeklyWindowRule(
        state=ExchangeTradingState.TRADING,
        start_day_offset=-1,
        start_time=time(17, 0),
        end_day_offset=0,
        end_time=time(16, 0),
        provenance_status=VerificationStatus.VERIFIED_PRIMARY,
        evidence_ids=("no-such-record",),
    )
    with pytest.raises(ValidationError):
        definition(weekly_rules=(("monday", (rule,)),))


# --------------------------------------------------------------------------
# Dated exceptions
# --------------------------------------------------------------------------


def exception_window(
    start_time: time = time(17, 0), end_time: time = time(12, 0)
) -> ExceptionWindow:
    return ExceptionWindow(
        state=ExchangeTradingState.TRADING,
        start_date=date(2024, 1, 9),
        start_time=start_time,
        end_date=date(2024, 1, 10),
        end_time=end_time,
    )


def test_removed_exception_must_not_carry_windows() -> None:
    with pytest.raises(ValidationError):
        DatedException(
            trading_date=date(2024, 1, 10),
            kind="removed",
            windows=(exception_window(),),
            provenance_status=VerificationStatus.VERIFIED_PRIMARY,
            evidence_ids=("test-evidence",),
        )


def test_replaced_exception_must_carry_windows() -> None:
    with pytest.raises(ValidationError):
        DatedException(
            trading_date=date(2024, 1, 10),
            kind="replaced",
            windows=(),
            provenance_status=VerificationStatus.VERIFIED_PRIMARY,
            evidence_ids=("test-evidence",),
        )


def test_exception_kind_vocabulary_is_closed() -> None:
    with pytest.raises(ValidationError):
        DatedException(
            trading_date=date(2024, 1, 10),
            kind="modified",
            windows=(),
            provenance_status=VerificationStatus.VERIFIED_PRIMARY,
            evidence_ids=("test-evidence",),
        )


def test_exception_windows_must_stay_local_to_their_trading_date() -> None:
    """Falsification defect D2, regression.

    A replaced session's windows must lie near the trading date that labels
    them: no evidenced CME session opens more than two civil days before its
    trade date, so a window months away is an authoring error (a typo'd month
    or year), not a schedule.
    """
    far_away = ExceptionWindow(
        state=ExchangeTradingState.TRADING,
        start_date=date(2024, 1, 2),
        start_time=time(17, 0),
        end_date=date(2024, 1, 3),
        end_time=time(12, 0),
    )
    with pytest.raises(ValidationError):
        DatedException(
            trading_date=date(2024, 1, 10),
            kind="replaced",
            windows=(far_away,),
            provenance_status=VerificationStatus.VERIFIED_PRIMARY,
            evidence_ids=("test-evidence",),
        )


def test_exception_windows_must_not_outlive_their_trading_date() -> None:
    overruns = ExceptionWindow(
        state=ExchangeTradingState.TRADING,
        start_date=date(2024, 1, 10),
        start_time=time(17, 0),
        end_date=date(2024, 1, 11),
        end_time=time(12, 0),
    )
    with pytest.raises(ValidationError):
        DatedException(
            trading_date=date(2024, 1, 10),
            kind="replaced",
            windows=(overruns,),
            provenance_status=VerificationStatus.VERIFIED_PRIMARY,
            evidence_ids=("test-evidence",),
        )


def test_duplicate_exception_dates_are_rejected() -> None:
    removed = DatedException(
        trading_date=date(2024, 1, 10),
        kind="removed",
        windows=(),
        provenance_status=VerificationStatus.VERIFIED_PRIMARY,
        evidence_ids=("test-evidence",),
    )
    with pytest.raises(ValidationError):
        definition(exceptions=(removed, removed))


def test_exceptions_outside_the_supported_range_are_rejected() -> None:
    outside = DatedException(
        trading_date=date(2025, 6, 2),
        kind="removed",
        windows=(),
        provenance_status=VerificationStatus.VERIFIED_PRIMARY,
        evidence_ids=("test-evidence",),
    )
    with pytest.raises(ValidationError):
        definition(exceptions=(outside,))


# --------------------------------------------------------------------------
# The strict TOML loader
# --------------------------------------------------------------------------

MINIMAL_TOML = """
schema = "qrt-calendar-definition/1"
timezone_probe = []
exception = []

[calendar]
id = "SYNTHETIC_TEST"
version = 1
timezone = "America/Chicago"
first_trading_date = 2024-01-08
last_trading_date = 2024-01-12

[[weekly.monday]]
state = "trading"
start_day_offset = -1
start_time = 17:00:00
end_day_offset = 0
end_time = 16:00:00
verification = "verified-primary"
evidence = ["test-evidence"]

[[evidence]]
id = "test-evidence"
source_type = "test"
title = "A test claim"
source_url = "https://example.invalid/source"
archive_url = ""
accessed = 2026-08-10
claim = "A synthetic fact used only by tests."
"""


def test_loader_parses_a_minimal_document() -> None:
    parsed = parse_calendar_definition(MINIMAL_TOML)
    assert parsed.calendar_id.canonical() == "SYNTHETIC_TEST"
    assert parsed.version.canonical() == "v1"
    assert parsed.timezone_name == "America/Chicago"
    assert parsed.weekly_rules_for("monday") is not None
    assert parsed.weekly_rules_for("tuesday") is None


def test_loader_is_deterministic_for_identical_text() -> None:
    assert parse_calendar_definition(MINIMAL_TOML) == parse_calendar_definition(MINIMAL_TOML)


def test_loader_rejects_invalid_toml() -> None:
    with pytest.raises(CalendarDefinitionError):
        parse_calendar_definition("this is not toml [")


def test_loader_rejects_a_wrong_schema_token() -> None:
    with pytest.raises(CalendarDefinitionError):
        parse_calendar_definition(MINIMAL_TOML.replace("qrt-calendar-definition/1", "v2"))


def test_loader_rejects_unknown_root_keys() -> None:
    with pytest.raises(CalendarDefinitionError):
        parse_calendar_definition(MINIMAL_TOML + "\n[surprise]\nkey = 1\n")


def test_loader_rejects_unknown_calendar_keys() -> None:
    hostile = MINIMAL_TOML.replace(
        'timezone = "America/Chicago"',
        'timezone = "America/Chicago"\ntimezone_fallback = "UTC"',
    )
    with pytest.raises(CalendarDefinitionError):
        parse_calendar_definition(hostile)


def test_loader_rejects_unknown_rule_keys() -> None:
    hostile = MINIMAL_TOML.replace(
        'verification = "verified-primary"',
        'verification = "verified-primary"\nnotes = "should not exist"',
    )
    with pytest.raises(CalendarDefinitionError):
        parse_calendar_definition(hostile)


def test_loader_rejects_missing_required_keys() -> None:
    with pytest.raises(CalendarDefinitionError):
        parse_calendar_definition(MINIMAL_TOML.replace("version = 1\n", ""))


def test_loader_rejects_unknown_verification_statuses() -> None:
    with pytest.raises(CalendarDefinitionError):
        parse_calendar_definition(MINIMAL_TOML.replace('"verified-primary"', '"assumed-correct"'))


def test_loader_rejects_unknown_states() -> None:
    with pytest.raises(CalendarDefinitionError):
        parse_calendar_definition(MINIMAL_TOML.replace('state = "trading"', 'state = "open"'))


def test_loader_rejects_a_missing_packaged_resource() -> None:
    with pytest.raises(CalendarDefinitionError):
        load_packaged_definition("no_such_calendar.toml")


def test_loader_rejects_path_traversal_resource_names() -> None:
    for hostile in ("../secrets.toml", "..\\secrets.toml", "a/b.toml", ".hidden.toml", ""):
        with pytest.raises(CalendarDefinitionError):
            load_packaged_definition(hostile)


def test_loader_rejects_a_scalar_evidence_value() -> None:
    """Falsification defect D4, regression.

    ``tuple("ab")`` explodes a string character-by-character, so a forgotten
    pair of brackets would become citations the author never wrote. The
    loader must demand an array, with a message that says so.
    """
    hostile = MINIMAL_TOML.replace('evidence = ["test-evidence"]', 'evidence = "test-evidence"')
    with pytest.raises(CalendarDefinitionError, match="must be an array"):
        parse_calendar_definition(hostile)


def test_loader_requires_the_probe_and_exception_sections_explicitly() -> None:
    """Falsification defect D5, regression.

    A definition that silently loses its ``timezone_probe`` section loses its
    tzdb-divergence protection with no error anywhere; the loader therefore
    requires both sections to be stated, even as explicit empty arrays.
    """
    with pytest.raises(CalendarDefinitionError, match="timezone_probe"):
        parse_calendar_definition(MINIMAL_TOML.replace("timezone_probe = []\n", ""))
    with pytest.raises(CalendarDefinitionError, match="exception"):
        parse_calendar_definition(MINIMAL_TOML.replace("exception = []\n", ""))


# --------------------------------------------------------------------------
# The packaged CME definition loads and matches its declared identity
# --------------------------------------------------------------------------


def test_packaged_cme_equity_index_v1_loads() -> None:
    parsed = load_cme_equity_index_v1()
    assert parsed.calendar_id.canonical() == "CME_EQUITY_INDEX"
    assert parsed.version.canonical() == "v1"
    assert parsed.timezone_name == "America/Chicago"
    assert parsed.first_trading_date == date(2023, 5, 22)
    assert parsed.last_trading_date == date(2023, 12, 29)
    # Saturday and Sunday must never be trading dates in this definition.
    assert parsed.weekly_rules_for("saturday") is None
    assert parsed.weekly_rules_for("sunday") is None
    # Every fact carries evidence; every reference resolves (validated on
    # construction, asserted here as a fixture-level property).
    assert len(parsed.evidence) >= 8
