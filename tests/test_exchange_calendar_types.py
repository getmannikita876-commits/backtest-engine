"""Adversarial tests for the exchange-calendar value types.

The properties under attack are the ones deterministic replay cannot recover
from if they fail:

* a trading date must never be derivable from a timestamp by accident;
* calendar identity must never be forged, normalized, or numeric;
* a materialized window must be half-open, UTC-only, and microsecond-exact;
* a materialized calendar must be unconstructible with a wrong content hash.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta, timezone
from typing import Any

import pytest
from pydantic import ValidationError

from quant_research_terminal.domain.calendar_materialization import (
    materialized_content_hash,
)
from quant_research_terminal.domain.exchange_calendar import (
    CalendarContext,
    CalendarId,
    CalendarResolver,
    CalendarVersion,
    ExchangeTradingState,
    MaterializedCalendar,
    MaterializedWindow,
    RuleProvenance,
    TradingDate,
    VerificationStatus,
)

PROVENANCE = RuleProvenance(
    status=VerificationStatus.VERIFIED_PRIMARY, evidence_ids=("some-evidence",)
)


def window(
    start: datetime,
    end: datetime,
    state: ExchangeTradingState = ExchangeTradingState.TRADING,
    trading_date: date = date(2024, 1, 8),
    rule_key: str = "weekly.monday.0",
) -> MaterializedWindow:
    return MaterializedWindow(
        start=start,
        end=end,
        state=state,
        trading_date=TradingDate(value=trading_date),
        rule_key=rule_key,
    )


def calendar_with(windows: tuple[MaterializedWindow, ...]) -> MaterializedCalendar:
    calendar_id = CalendarId(token="SYNTHETIC_TEST")
    version = CalendarVersion(number=1)
    coverage_start = windows[0].start
    coverage_end = windows[-1].end
    provenance = tuple(sorted({w.rule_key: PROVENANCE for w in windows}.items()))
    return MaterializedCalendar(
        calendar_id=calendar_id,
        version=version,
        timezone_name="America/Chicago",
        first_trading_date=TradingDate(value=windows[0].trading_date.value),
        last_trading_date=TradingDate(value=windows[-1].trading_date.value),
        coverage_start=coverage_start,
        coverage_end=coverage_end,
        windows=windows,
        provenance=provenance,
        content_hash=materialized_content_hash(
            calendar_id=calendar_id,
            version=version,
            timezone_name="America/Chicago",
            first_trading_date=TradingDate(value=windows[0].trading_date.value),
            last_trading_date=TradingDate(value=windows[-1].trading_date.value),
            coverage_start=coverage_start,
            coverage_end=coverage_end,
            windows=windows,
            provenance=provenance,
        ),
    )


# --------------------------------------------------------------------------
# CalendarId
# --------------------------------------------------------------------------


def test_calendar_id_accepts_a_canonical_token() -> None:
    assert CalendarId(token="CME_EQUITY_INDEX").canonical() == "CME_EQUITY_INDEX"


@pytest.mark.parametrize(
    "token",
    [
        "",
        "cme_equity_index",  # lower case is never normalized
        " CME_EQUITY_INDEX",
        "CME_EQUITY_INDEX ",
        "CME EQUITY",  # whitespace
        "CME:EQ",  # separator smuggling
        "1234567890",  # purely numeric
        "CMÉ_EQ",  # non-ASCII look-alike
        "X" * 33,  # too long
    ],
)
def test_calendar_id_rejects_non_canonical_tokens(token: str) -> None:
    with pytest.raises(ValidationError):
        CalendarId(token=token)


def test_calendar_id_rejects_non_string_tokens() -> None:
    with pytest.raises(ValidationError):
        CalendarId(token=123)  # type: ignore[arg-type]


def test_calendar_id_model_copy_revalidates() -> None:
    identity = CalendarId(token="CME_EQUITY_INDEX")
    with pytest.raises(ValidationError):
        identity.model_copy(update={"token": "lowercase"})


# --------------------------------------------------------------------------
# CalendarVersion
# --------------------------------------------------------------------------


def test_calendar_version_canonical_form() -> None:
    assert CalendarVersion(number=3).canonical() == "v3"


@pytest.mark.parametrize("number", [0, -1, True, "1", 1.0])
def test_calendar_version_rejects_non_positive_and_non_int(number: Any) -> None:
    with pytest.raises(ValidationError):
        CalendarVersion(number=number)


# --------------------------------------------------------------------------
# TradingDate
# --------------------------------------------------------------------------


def test_trading_date_accepts_a_plain_date() -> None:
    assert TradingDate(value=date(2024, 1, 8)).canonical() == "2024-01-08"


def test_trading_date_rejects_a_datetime() -> None:
    """``datetime`` *is-a* ``date``; accepting one would let a timestamp be
    silently truncated into a calendar answer, which is the exact misuse the
    type exists to prevent."""
    with pytest.raises(ValidationError):
        TradingDate(value=datetime(2024, 1, 8, 12, 0, tzinfo=UTC))


def test_trading_date_rejects_a_date_subclass() -> None:
    class SneakyDate(date):
        pass

    with pytest.raises(ValidationError):
        TradingDate(value=SneakyDate(2024, 1, 8))


@pytest.mark.parametrize("text", ["2024/01/08", "08-01-2024", "2024-1-8x", "", "20240108T"])
def test_trading_date_from_iso_rejects_non_iso_text(text: str) -> None:
    with pytest.raises(ValueError):
        TradingDate.from_iso(text)


def test_trading_date_from_iso_round_trips() -> None:
    assert TradingDate.from_iso("2024-01-08") == TradingDate(value=date(2024, 1, 8))


def test_trading_date_model_copy_revalidates() -> None:
    trading_date = TradingDate(value=date(2024, 1, 8))
    with pytest.raises(ValidationError):
        trading_date.model_copy(update={"value": datetime(2024, 1, 8, tzinfo=UTC)})


# --------------------------------------------------------------------------
# RuleProvenance
# --------------------------------------------------------------------------


def test_rule_provenance_requires_evidence() -> None:
    with pytest.raises(ValidationError):
        RuleProvenance(status=VerificationStatus.VERIFIED_PRIMARY, evidence_ids=())


# --------------------------------------------------------------------------
# MaterializedWindow: half-open, UTC-only, strictly forward
# --------------------------------------------------------------------------

START = datetime(2024, 1, 8, 14, 0, tzinfo=UTC)
END = datetime(2024, 1, 8, 15, 0, tzinfo=UTC)


def test_window_accepts_utc_bounds() -> None:
    built = window(START, END)
    assert built.start == START
    assert built.end == END


def test_window_rejects_equal_bounds() -> None:
    with pytest.raises(ValidationError):
        window(START, START)


def test_window_rejects_inverted_bounds() -> None:
    with pytest.raises(ValidationError):
        window(END, START)


def test_window_rejects_naive_bounds() -> None:
    with pytest.raises(ValidationError):
        window(START.replace(tzinfo=None), END)


def test_window_rejects_non_utc_bounds() -> None:
    chicago_like = timezone(timedelta(hours=-6))
    with pytest.raises(ValidationError):
        window(START.astimezone(chicago_like).replace(tzinfo=chicago_like), END)


def test_window_rejects_the_closed_state() -> None:
    # CLOSED is the complement of materialized windows; materializing it would
    # invite thousands of curated windows and two sources of truth.
    with pytest.raises(ValidationError):
        window(START, END, state=ExchangeTradingState.CLOSED)


def test_window_rejects_an_empty_rule_key() -> None:
    with pytest.raises(ValidationError):
        window(START, END, rule_key="")


# --------------------------------------------------------------------------
# MaterializedCalendar: hash integrity and ordering
# --------------------------------------------------------------------------


def test_calendar_is_unconstructible_with_a_wrong_hash() -> None:
    good = calendar_with((window(START, END),))
    with pytest.raises(ValidationError):
        MaterializedCalendar(
            **{**dict(good), "content_hash": "0" * 64},
        )


def test_calendar_rejects_overlapping_windows() -> None:
    overlapping = (
        window(START, END),
        window(END - timedelta(minutes=1), END + timedelta(minutes=30)),
    )
    with pytest.raises(ValidationError):
        calendar_with(overlapping)


def test_calendar_rejects_windows_outside_coverage() -> None:
    good = calendar_with((window(START, END),))
    with pytest.raises(ValidationError):
        MaterializedCalendar(
            **{**dict(good), "coverage_start": START + timedelta(minutes=5)},
        )


def test_calendar_rejects_a_window_with_unknown_provenance_key() -> None:
    good = calendar_with((window(START, END),))
    with pytest.raises(ValidationError):
        MaterializedCalendar(**{**dict(good), "provenance": ()})


def test_calendar_model_copy_revalidates_the_hash() -> None:
    good = calendar_with((window(START, END),))
    with pytest.raises(ValidationError):
        good.model_copy(update={"content_hash": "f" * 64})


# --------------------------------------------------------------------------
# CalendarResolver input hardening
# --------------------------------------------------------------------------


def test_resolver_rejects_a_non_calendar() -> None:
    with pytest.raises(TypeError):
        CalendarResolver(object())  # type: ignore[arg-type]


def test_resolver_rejects_naive_timestamps() -> None:
    resolver = CalendarResolver(calendar_with((window(START, END),)))
    with pytest.raises(ValueError):
        resolver.resolve(START.replace(tzinfo=None))
    with pytest.raises(ValueError):
        resolver.supports(START.replace(tzinfo=None))


def test_resolver_rejects_non_utc_timestamps() -> None:
    resolver = CalendarResolver(calendar_with((window(START, END),)))
    eastern_like = timezone(timedelta(hours=-5))
    with pytest.raises(ValueError):
        resolver.resolve(START.astimezone(eastern_like).replace(tzinfo=eastern_like))


def test_resolver_rejects_non_datetime_inputs() -> None:
    resolver = CalendarResolver(calendar_with((window(START, END),)))
    with pytest.raises(TypeError):
        resolver.resolve("2024-01-08T14:00:00Z")


def test_windows_for_rejects_a_civil_date() -> None:
    resolver = CalendarResolver(calendar_with((window(START, END),)))
    with pytest.raises(TypeError):
        resolver.windows_for(date(2024, 1, 8))  # type: ignore[arg-type]


def test_windows_for_rejects_a_trading_date_subclass_lookalike() -> None:
    resolver = CalendarResolver(calendar_with((window(START, END),)))

    class FakeTradingDate:
        value = date(2024, 1, 8)

    with pytest.raises(TypeError):
        resolver.windows_for(FakeTradingDate())  # type: ignore[arg-type]


# --------------------------------------------------------------------------
# CalendarContext: internal coherence (falsification defect D7)
# --------------------------------------------------------------------------


def context(**overrides: Any) -> CalendarContext:
    fields: dict[str, Any] = {
        "timestamp": START,
        "state": ExchangeTradingState.TRADING,
        "trading_date": TradingDate(value=date(2024, 1, 8)),
        "window_start": START,
        "window_end": END,
        "next_transition": None,
        "calendar_id": CalendarId(token="SYNTHETIC_TEST"),
        "calendar_version": CalendarVersion(number=1),
        "content_hash": "a" * 64,
        "verification": PROVENANCE,
    }
    fields.update(overrides)
    return CalendarContext(**fields)


def test_context_rejects_naive_instants() -> None:
    with pytest.raises(ValidationError):
        context(timestamp=START.replace(tzinfo=None))


def test_a_closed_context_carries_no_trading_date_or_verification() -> None:
    with pytest.raises(ValidationError):
        context(state=ExchangeTradingState.CLOSED)
    built = context(state=ExchangeTradingState.CLOSED, trading_date=None, verification=None)
    assert built.trading_date is None


def test_a_trading_context_requires_trading_date_and_verification() -> None:
    with pytest.raises(ValidationError):
        context(trading_date=None)
    with pytest.raises(ValidationError):
        context(verification=None)


def test_the_timestamp_must_lie_inside_its_own_window() -> None:
    with pytest.raises(ValidationError):
        context(timestamp=END)  # end is exclusive
    with pytest.raises(ValidationError):
        context(timestamp=START - timedelta(microseconds=1))


def test_next_transition_must_be_the_window_end_or_none() -> None:
    assert context(next_transition=END).next_transition == END
    with pytest.raises(ValidationError):
        context(next_transition=END + timedelta(minutes=1))


def test_the_content_hash_must_be_a_sha256_digest() -> None:
    with pytest.raises(ValidationError):
        context(content_hash="")
    with pytest.raises(ValidationError):
        context(content_hash="A" * 64)  # upper case is not canonical


def test_a_window_labelled_outside_the_declared_range_is_unconstructible() -> None:
    """Falsification defect D8, regression.

    A window whose trading-date label lies outside the declared range would
    be unreachable through the inverse API — ``windows_for`` raises for the
    very date that owns the window — so the calendar must refuse to exist.
    """
    stray = (
        window(START, END),
        window(
            END + timedelta(hours=1),
            END + timedelta(hours=2),
            trading_date=date(2025, 6, 2),
        ),
    )
    good = calendar_with(stray)  # range derives from first/last window here
    with pytest.raises(ValidationError):
        MaterializedCalendar(
            **{**dict(good), "last_trading_date": TradingDate(value=date(2024, 1, 8))},
        )
