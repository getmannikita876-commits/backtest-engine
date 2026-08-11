"""Continuous-series identity, execution safety, and the Merkle hash link.

The single most valuable property here: **a synthetic series can never be
executed**. ADR-009 built ``require_listed_contract`` as an exact type check
precisely so a continuous identity added later would fail it without the guard
knowing this module exists. These tests prove that promise was kept.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pytest
from pydantic import ValidationError
from rollover_fixtures import schedule_over_calendar
from synthetic_calendars import synthetic_resolver

from quant_research_terminal.domain import continuous_series as continuous_module
from quant_research_terminal.domain.continuous_series import (
    CONTINUOUS_MARKER,
    ContinuousSeriesDefinition,
    ContinuousSeriesId,
    build_continuous_series,
    continuous_series_content_hash,
)
from quant_research_terminal.domain.contract_month import ContractMonth
from quant_research_terminal.domain.exchange_calendar import CalendarResolver
from quant_research_terminal.domain.futures_contract import (
    FuturesContractId,
    FuturesProduct,
    Venue,
    require_listed_contract,
)
from quant_research_terminal.domain.rollover import (
    RollResolver,
    RollSchedule,
    UnsupportedRollTimestampError,
)

ES = FuturesProduct(venue=Venue(code="CME"), root="ES")
NQ = FuturesProduct(venue=Venue(code="CME"), root="NQ")
MICRO = timedelta(microseconds=1)


def contract(month: ContractMonth, year: int = 2024) -> FuturesContractId:
    return FuturesContractId(product=ES, contract_month=month, contract_year=year)


H, M = contract(ContractMonth.MARCH), contract(ContractMonth.JUNE)


@pytest.fixture(scope="module")
def calendar_resolver() -> CalendarResolver:
    return synthetic_resolver(first=date(2024, 1, 1), last=date(2024, 3, 29))


def build_schedule(calendar_resolver: CalendarResolver, *, roll_at: datetime) -> RollSchedule:
    return schedule_over_calendar(
        calendar_resolver.calendar,
        product=ES,
        initial_contract=H,
        rolls=((H, M, roll_at),),
    )


@pytest.fixture(scope="module")
def schedule(calendar_resolver: CalendarResolver) -> RollSchedule:
    return build_schedule(calendar_resolver, roll_at=datetime(2024, 2, 14, 15, 0, tzinfo=UTC))


@pytest.fixture(scope="module")
def series(schedule: RollSchedule) -> ContinuousSeriesDefinition:
    return build_continuous_series(
        series_id=ContinuousSeriesId(product=ES, token="ACTIVE"),
        schedule=schedule,
        supported_start=schedule.supported_start,
        supported_end=schedule.supported_end,
    )


# --------------------------------------------------------------------------
# Identity
# --------------------------------------------------------------------------


def test_the_canonical_form_carries_the_continuous_marker() -> None:
    assert ContinuousSeriesId(product=ES, token="ACTIVE").canonical() == (
        "CME:ES:CONTINUOUS:ACTIVE"
    )


def test_parse_is_the_exact_inverse_of_canonical() -> None:
    identity = ContinuousSeriesId(product=ES, token="FRONT_MONTH")
    assert ContinuousSeriesId.parse(identity.canonical()) == identity


@pytest.mark.parametrize(
    "text",
    [
        "CME:ES:M2024",  # a listed contract
        "CME:ES",  # a product
        "CME:ES:ACTIVE",  # missing the marker
        "CME:ES:CONTINUOUS",  # missing the token
        "CME:ES:continuous:ACTIVE",  # lower case is never normalized
        "CME:ES:ROLLING:ACTIVE",  # wrong marker
        "ES1!",  # a vendor alias
        "NQ1!",
        " CME:ES:CONTINUOUS:ACTIVE",
        "CME:ES:CONTINUOUS:ACTIVE:EXTRA",
    ],
)
def test_parse_rejects_everything_that_is_not_canonical(text: str) -> None:
    with pytest.raises(ValueError):
        ContinuousSeriesId.parse(text)


@pytest.mark.parametrize("token", ["", "active", "FRONT MONTH", "CME:ES", "1234", "X" * 33])
def test_malformed_tokens_are_rejected(token: str) -> None:
    with pytest.raises(ValidationError):
        ContinuousSeriesId(product=ES, token=token)


def test_the_token_may_not_be_the_marker_itself() -> None:
    with pytest.raises(ValidationError):
        ContinuousSeriesId(product=ES, token=CONTINUOUS_MARKER)


def test_a_product_root_spelled_continuous_does_not_collide() -> None:
    """The discriminator is positional, not textual."""
    odd_product = FuturesProduct(venue=Venue(code="CME"), root=CONTINUOUS_MARKER)
    listed = FuturesContractId(
        product=odd_product, contract_month=ContractMonth.JUNE, contract_year=2024
    )
    series_id = ContinuousSeriesId(product=odd_product, token="ACTIVE")
    assert listed.canonical() == "CME:CONTINUOUS:M2024"
    assert series_id.canonical() == "CME:CONTINUOUS:CONTINUOUS:ACTIVE"
    assert listed.canonical() != series_id.canonical()
    with pytest.raises(ValueError):
        FuturesContractId.parse(series_id.canonical())
    with pytest.raises(ValueError):
        ContinuousSeriesId.parse(listed.canonical())


def test_series_identity_model_copy_revalidates() -> None:
    identity = ContinuousSeriesId(product=ES, token="ACTIVE")
    with pytest.raises(ValidationError):
        identity.model_copy(update={"token": "lower"})


# --------------------------------------------------------------------------
# Execution safety — the promise ADR-009 made
# --------------------------------------------------------------------------


def test_a_continuous_identity_fails_the_executable_guard() -> None:
    with pytest.raises(TypeError):
        require_listed_contract(ContinuousSeriesId(product=ES, token="ACTIVE"))


def test_a_continuous_series_definition_fails_the_executable_guard(
    series: ContinuousSeriesDefinition,
) -> None:
    with pytest.raises(TypeError):
        require_listed_contract(series)


def test_a_schedule_and_a_product_fail_the_executable_guard(
    schedule: RollSchedule,
) -> None:
    for value in (schedule, ES, "ES1!", "CME:ES:CONTINUOUS:ACTIVE", None):
        with pytest.raises(TypeError):
            require_listed_contract(value)


def test_a_listed_contract_still_passes_the_executable_guard() -> None:
    assert require_listed_contract(H) is H


def test_every_instant_the_series_maps_is_executable(
    series: ContinuousSeriesDefinition,
) -> None:
    """The end-to-end property: the mapping never hands out a non-executable thing."""
    instant = series.supported_start
    step = timedelta(hours=11)
    while instant < series.supported_end:
        require_listed_contract(series.active_contract_at(instant))
        instant += step


def test_a_continuous_identity_is_not_a_futures_contract_subclass() -> None:
    assert not issubclass(ContinuousSeriesId, FuturesContractId)
    assert not isinstance(ContinuousSeriesId(product=ES, token="ACTIVE"), FuturesContractId)


# --------------------------------------------------------------------------
# No price synthesis exists, not even as a placeholder
# --------------------------------------------------------------------------


def test_the_module_exposes_no_price_adjustment_surface() -> None:
    """Phase 2.2 maps contracts; it does not build prices.

    A ``NotImplementedError`` placeholder would advertise a capability the
    platform does not have and invite callers to write against it.
    """
    forbidden = ("adjust", "back_adjust", "backadjust", "ratio", "panama", "price", "ohlc")
    exported = [name for name in dir(continuous_module) if not name.startswith("_")]
    offenders = [name for name in exported if any(token in name.lower() for token in forbidden)]
    assert offenders == []
    series_surface = [name for name in dir(ContinuousSeriesDefinition) if not name.startswith("_")]
    assert [
        name for name in series_surface if any(token in name.lower() for token in forbidden)
    ] == []


# --------------------------------------------------------------------------
# Mapping and range
# --------------------------------------------------------------------------


def test_the_series_maps_instants_to_listed_contracts(
    series: ContinuousSeriesDefinition, schedule: RollSchedule
) -> None:
    roll_at = schedule.events[0].effective_time
    assert series.active_contract_at(roll_at - MICRO) == H
    assert series.active_contract_at(roll_at) == M


def test_outside_the_series_range_is_unsupported(series: ContinuousSeriesDefinition) -> None:
    with pytest.raises(UnsupportedRollTimestampError):
        series.active_contract_at(series.supported_end)
    with pytest.raises(UnsupportedRollTimestampError):
        series.active_contract_at(series.supported_start - MICRO)


def test_a_narrower_series_range_is_enforced_against_the_schedule(
    schedule: RollSchedule,
) -> None:
    narrowed = build_continuous_series(
        series_id=ContinuousSeriesId(product=ES, token="ACTIVE"),
        schedule=schedule,
        supported_start=schedule.supported_start + timedelta(days=10),
        supported_end=schedule.supported_end,
    )
    with pytest.raises(UnsupportedRollTimestampError):
        narrowed.active_contract_at(schedule.supported_start)
    assert RollResolver(schedule).active_contract_at(schedule.supported_start) == H


def test_a_series_range_wider_than_its_schedule_is_rejected(schedule: RollSchedule) -> None:
    with pytest.raises(ValidationError, match="not contained"):
        build_continuous_series(
            series_id=ContinuousSeriesId(product=ES, token="ACTIVE"),
            schedule=schedule,
            supported_start=schedule.supported_start - timedelta(days=1),
            supported_end=schedule.supported_end,
        )


def test_a_series_naming_another_product_than_its_schedule_is_rejected(
    schedule: RollSchedule,
) -> None:
    with pytest.raises(ValidationError):
        build_continuous_series(
            series_id=ContinuousSeriesId(product=NQ, token="ACTIVE"),
            schedule=schedule,
            supported_start=schedule.supported_start,
            supported_end=schedule.supported_end,
        )


def test_the_series_rejects_naive_instants(schedule: RollSchedule) -> None:
    series_def = build_continuous_series(
        series_id=ContinuousSeriesId(product=ES, token="ACTIVE"),
        schedule=schedule,
        supported_start=schedule.supported_start,
        supported_end=schedule.supported_end,
    )
    with pytest.raises(ValueError):
        series_def.active_contract_at(schedule.supported_start.replace(tzinfo=None))


# --------------------------------------------------------------------------
# The hash transitively pins the schedule
# --------------------------------------------------------------------------


def test_the_series_hash_changes_when_the_referenced_schedule_changes(
    calendar_resolver: CalendarResolver, series: ContinuousSeriesDefinition
) -> None:
    """The owner-required Merkle link: a different schedule means a different series."""
    other_schedule = build_schedule(
        calendar_resolver, roll_at=datetime(2024, 2, 15, 15, 0, tzinfo=UTC)
    )
    assert other_schedule.content_hash != series.schedule.content_hash
    other_series = build_continuous_series(
        series_id=series.series_id,
        schedule=other_schedule,
        supported_start=series.supported_start,
        supported_end=series.supported_end,
    )
    assert other_series.content_hash != series.content_hash


def test_the_series_hash_covers_its_identity_and_range(
    series: ContinuousSeriesDefinition,
) -> None:
    baseline = series.content_hash
    renamed = continuous_series_content_hash(
        series_id=ContinuousSeriesId(product=ES, token="SECONDARY"),
        roll_schedule_hash=series.schedule.content_hash,
        supported_start=series.supported_start,
        supported_end=series.supported_end,
    )
    narrowed = continuous_series_content_hash(
        series_id=series.series_id,
        roll_schedule_hash=series.schedule.content_hash,
        supported_start=series.supported_start,
        supported_end=series.supported_end - timedelta(days=1),
    )
    assert baseline != renamed
    assert baseline != narrowed


def test_a_wrong_series_hash_is_unconstructible(series: ContinuousSeriesDefinition) -> None:
    with pytest.raises(ValidationError, match="content_hash does not match"):
        ContinuousSeriesDefinition(**{**dict(series), "content_hash": "0" * 64})


def test_a_series_id_subclass_is_rejected(schedule: RollSchedule) -> None:
    """Falsification pass 2: the hash is built from ``series_id.canonical()``.

    A subclass overriding it would let the identity in the hash diverge from
    the identity in the object.
    """

    class SneakyId(ContinuousSeriesId):  # type: ignore[misc]  # the attack under test
        pass

    with pytest.raises(ValidationError, match="exact ContinuousSeriesId"):
        build_continuous_series(
            series_id=SneakyId(product=ES, token="ACTIVE"),
            schedule=schedule,
            supported_start=schedule.supported_start,
            supported_end=schedule.supported_end,
        )


def test_series_model_copy_revalidates(series: ContinuousSeriesDefinition) -> None:
    with pytest.raises(ValidationError):
        series.model_copy(update={"supported_end": series.supported_start + MICRO})


# --------------------------------------------------------------------------
# Storage is untouched by this phase
# --------------------------------------------------------------------------


def test_the_storage_schema_version_is_unchanged() -> None:
    """Phase 2.2 added no persistence; Phase 2.3 later added version 3.

    The claim this test makes is about *version-2 artifacts*, so it asserts the
    legacy constant. A literal is used for the current version so that a future
    bump has to come here deliberately rather than being ratified silently.
    """
    from quant_research_terminal.data.contracts import LEGACY_SCHEMA_VERSION, SCHEMA_VERSION

    assert LEGACY_SCHEMA_VERSION == 2
    assert SCHEMA_VERSION == 3


def test_no_roll_or_continuous_column_entered_the_storage_schemas() -> None:
    """Phase 2.2 adds no persistence; schema v2 files mean exactly what they did."""
    from quant_research_terminal.data.schemas import (
        BAR_ARROW_SCHEMA,
        QUOTE_ARROW_SCHEMA,
        TRADE_ARROW_SCHEMA,
    )

    forbidden = ("roll", "continuous", "series", "contract_id", "lifecycle")
    for schema_obj in (TRADE_ARROW_SCHEMA, QUOTE_ARROW_SCHEMA, BAR_ARROW_SCHEMA):
        for name in schema_obj.names:
            assert not any(token in name.lower() for token in forbidden), name
    assert TRADE_ARROW_SCHEMA.names == [
        "timestamp",
        "instrument_symbol",
        "price",
        "size",
        "side",
    ]


def test_a_continuous_identity_is_never_a_legacy_instrument_symbol() -> None:
    """``instrument_symbol`` stays a vendor alias; nothing reinterprets it.

    A continuous canonical form would be structurally rejected as a listed
    identity anyway, but the point is that this phase adds no code path that
    reads the legacy column as canonical identity in either direction.
    """
    from quant_research_terminal.domain.trade import Trade

    with pytest.raises(ValueError):
        FuturesContractId.parse("ESM6")
    # The legacy field remains a free string the domain does not interpret.
    from decimal import Decimal

    from quant_research_terminal.domain.trade_side import TradeSide

    trade = Trade(
        timestamp=datetime(2024, 1, 10, 15, 0, tzinfo=UTC),
        instrument_symbol="ESM6",
        price=Decimal("4500.25"),
        size=Decimal(1),
        side=TradeSide.BUY,
    )
    assert trade.instrument_symbol == "ESM6"
    with pytest.raises(TypeError):
        require_listed_contract(trade.instrument_symbol)
