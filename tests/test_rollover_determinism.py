"""Cross-process determinism of roll materialization and mapping.

A roll schedule is a research pin: a manifest that records one must be readable
by the next process, on the next machine, next year. These tests assert the
*serialized* forms are byte-identical across hash seeds and process timezones —
deliberately not that CPython ``hash()`` integers match, which no contract
promises.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys

#: Emits every observable serialization plus several validator error messages.
#: Error text is included for the ADR-009 reason: a message built by iterating a
#: set would vary between runs and make a diagnostic irreproducible.
_DETERMINISM_SCRIPT = """
import sys
from datetime import UTC, date, datetime

sys.path.insert(0, r"{tests_dir}")
from synthetic_calendars import synthetic_resolver

from quant_research_terminal.domain.contract_lifecycle import (
    ContractLifecycle, LifecycleProvenance,
)
from quant_research_terminal.domain.continuous_series import (
    ContinuousSeriesId, build_continuous_series,
)
from quant_research_terminal.domain.contract_month import ContractMonth
from quant_research_terminal.domain.exchange_calendar import TradingDate
from quant_research_terminal.domain.futures_contract import (
    FuturesContractId, FuturesProduct, Venue,
)
from quant_research_terminal.domain.rollover import (
    RollResolver, segments_for_trading_date,
)
from quant_research_terminal.domain.rollover_definition import (
    EffectiveBoundary, FixedCalendarRollDefinition,
)
from quant_research_terminal.domain.rollover_materialization import (
    canonical_serialization, materialize_fixed_calendar,
)

ES = FuturesProduct(venue=Venue(code="CME"), root="ES")
resolver = synthetic_resolver(first=date(2024, 1, 1), last=date(2024, 3, 29))
cal = resolver.calendar


def lifecycle(month, ltd):
    return ContractLifecycle(
        contract=FuturesContractId(product=ES, contract_month=month, contract_year=2024),
        last_trade_date=TradingDate.from_iso(ltd),
        provenance=LifecycleProvenance(evidence_ids=("note-a", "note-b")),
    )


definition = FixedCalendarRollDefinition(
    product=ES,
    calendar_id=cal.calendar_id,
    calendar_version=cal.version,
    calendar_content_hash=cal.content_hash,
    supported_start=cal.coverage_start,
    supported_end=cal.coverage_end,
    first_trading_date=cal.first_trading_date,
    last_trading_date=cal.last_trading_date,
    lifecycles=(
        lifecycle(ContractMonth.MARCH, "2024-01-15"),
        lifecycle(ContractMonth.JUNE, "2024-02-15"),
        lifecycle(ContractMonth.SEPTEMBER, "2024-03-15"),
    ),
    roll_offset_trading_dates=2,
    effective_boundary=EffectiveBoundary.FIRST_TRADING_WINDOW_START,
)

schedule = materialize_fixed_calendar(definition, resolver)
print(schedule.content_hash)
print(canonical_serialization(
    product=schedule.product,
    calendar_id=schedule.calendar_id,
    calendar_version=schedule.calendar_version,
    calendar_content_hash=schedule.calendar_content_hash,
    supported_start=schedule.supported_start,
    supported_end=schedule.supported_end,
    first_trading_date=schedule.first_trading_date,
    last_trading_date=schedule.last_trading_date,
    initial_contract=schedule.initial_contract,
    events=schedule.events,
    provenance=schedule.provenance,
).decode())
print("|".join(c.canonical() for c in schedule.contracts()))

roll = RollResolver(schedule)
for iso in ("2024-01-11", "2024-01-12", "2024-02-13"):
    for seg in segments_for_trading_date(roll, resolver, TradingDate.from_iso(iso)):
        print(iso, seg.start.isoformat(), seg.end.isoformat(),
              seg.contract.canonical(), seg.state.value, seg.roll_rule_key)

series = build_continuous_series(
    series_id=ContinuousSeriesId(product=ES, token="ACTIVE"),
    schedule=schedule,
    supported_start=schedule.supported_start,
    supported_end=schedule.supported_end,
)
print(series.canonical(), series.content_hash)

# Error text must be reproducible too.
for bad in ("cme:es:continuous:active", "ES1!", "CME:ES:M2024"):
    try:
        ContinuousSeriesId.parse(bad)
    except ValueError as exc:
        print(str(exc))
"""


def _script() -> str:
    return _DETERMINISM_SCRIPT.format(tests_dir=os.path.dirname(os.path.abspath(__file__)))


def test_materialization_is_identical_across_hash_seeds_and_process_timezones() -> None:
    outputs = set()
    for seed, zone in (
        ("0", "UTC"),
        ("1", "America/New_York"),
        ("42", "Asia/Tokyo"),
        ("12345", "Australia/Eucla"),
        ("random", "Europe/Berlin"),
    ):
        completed = subprocess.run(  # noqa: S603
            [sys.executable, "-c", _script()],
            capture_output=True,
            text=True,
            check=True,
            env={**os.environ, "PYTHONHASHSEED": seed, "TZ": zone},
        )
        outputs.add(completed.stdout)
    assert len(outputs) == 1
    emitted = outputs.pop()
    assert "CME:ES:CONTINUOUS:ACTIVE" in emitted
    assert "qrt-roll-schedule/1" in emitted


def test_the_serialization_carries_exactly_the_semantic_fields() -> None:
    """Generation provenance must never enter a semantic hash.

    Asserted as an exact key set rather than by hunting for substrings: the
    payload is small and closed, so naming its fields states the contract
    directly — anything added later has to be a deliberate edit here, which is
    the point, since adding a field silently re-keys every artifact.
    """
    completed = subprocess.run(  # noqa: S603
        [sys.executable, "-c", _script()],
        capture_output=True,
        text=True,
        check=True,
        env={**os.environ, "PYTHONHASHSEED": "7"},
    )
    payload = json.loads(completed.stdout.split("\n")[1])
    assert set(payload) == {
        "calendar",
        "events",
        "format",
        "initial_contract",
        "product",
        "provenance",
        "supported_range",
        "trading_date_range",
    }
    assert payload["format"] == "qrt-roll-schedule/1"
    # Instants are integer microseconds, not formatted strings.
    assert all(isinstance(value, int) for value in payload["supported_range"])
    assert all(
        isinstance(event[2], int) and isinstance(event[3], int) for event in payload["events"]
    )
