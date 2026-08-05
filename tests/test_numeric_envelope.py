"""Contract tests for the shared numeric envelope.

The invariant these exist to pin: **every successfully constructed domain
object is persistable**. Before Phase 1.7B the domain-valid and storage-valid
sets were different, so research could run to completion on values that could
never be saved — and therefore never reproduced.

Test values are generated deterministically from the envelope's own constants
rather than hard-coded, so a future change to the scale or bounds moves the
boundaries under test with it. No Hypothesis dependency is introduced.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
from pydantic import ValidationError

from quant_research_terminal.data import (
    bar_from_storage_row,
    bar_to_storage_row,
    quote_from_storage_row,
    quote_to_storage_row,
    trade_from_storage_row,
    trade_to_storage_row,
)
from quant_research_terminal.data.conversion import (
    _decimal_to_fixed_point,
    _decimal_to_unsigned_int,
)
from quant_research_terminal.domain.models import Bar, Quote, Trade, TradeSide
from quant_research_terminal.domain.numeric import (
    MAX_PRICE,
    MAX_PRICE_FIXED_POINT,
    MAX_QUANTITY,
    MAX_QUANTITY_INTEGER,
    MIN_PRICE,
    MIN_QUANTITY,
    PRICE_QUANTUM,
    PRICE_SCALE,
    NumericViolation,
    check_price,
    check_quantity,
)

BASE_TIME = datetime(2024, 1, 2, 12, 0, 0, tzinfo=UTC)
ONE_MINUTE = timedelta(minutes=1)

NON_FINITE = ["NaN", "sNaN", "Infinity", "-Infinity"]

# Boundary values derived from the envelope, not hard-coded.
JUST_BELOW_MIN_PRICE = MIN_PRICE / 10
JUST_ABOVE_MAX_PRICE = MAX_PRICE + PRICE_QUANTUM
SIX_DECIMAL_PLACES = Decimal("1.234567")
SEVEN_DECIMAL_PLACES = Decimal("1.2345678")


def _trade(**overrides: Any) -> Trade:
    values: dict[str, Any] = {
        "instrument_symbol": "ES",
        "timestamp": BASE_TIME,
        "price": Decimal("5000.25"),
        "size": Decimal("2"),
        "side": TradeSide.BUY,
    }
    values.update(overrides)
    return Trade(**values)


def _quote(**overrides: Any) -> Quote:
    values: dict[str, Any] = {
        "instrument_symbol": "ES",
        "timestamp": BASE_TIME,
        "bid": Decimal("5000.00"),
        "ask": Decimal("5000.25"),
        "bid_size": Decimal("1"),
        "ask_size": Decimal("1"),
    }
    values.update(overrides)
    return Quote(**values)


def _bar(**overrides: Any) -> Bar:
    values: dict[str, Any] = {
        "instrument_symbol": "ES",
        "interval_start": BASE_TIME,
        "interval": ONE_MINUTE,
        "open": Decimal("4999.50"),
        "high": Decimal("5002.00"),
        "low": Decimal("4998.75"),
        "close": Decimal("5001.25"),
        "volume": Decimal("12345"),
    }
    values.update(overrides)
    return Bar(**values)


# ==========================================================================
# The envelope's own boundaries
# ==========================================================================


def test_minimum_positive_price_is_accepted() -> None:
    assert check_price(MIN_PRICE) is None
    assert MIN_PRICE == PRICE_QUANTUM


def test_below_minimum_price_is_too_precise() -> None:
    # One decimal place finer than the scale, not "too small": magnitude is
    # not the problem, representability is.
    assert check_price(JUST_BELOW_MIN_PRICE) is NumericViolation.TOO_MANY_FRACTIONAL_DIGITS


def test_maximum_price_is_accepted() -> None:
    assert check_price(MAX_PRICE) is None


def test_one_quantum_above_maximum_price_is_rejected() -> None:
    assert check_price(JUST_ABOVE_MAX_PRICE) is NumericViolation.MAGNITUDE_TOO_LARGE


def test_exactly_six_decimal_places_is_accepted() -> None:
    assert check_price(SIX_DECIMAL_PLACES) is None


def test_seven_decimal_places_is_rejected() -> None:
    assert check_price(SEVEN_DECIMAL_PLACES) is NumericViolation.TOO_MANY_FRACTIONAL_DIGITS


def test_trailing_zeros_beyond_the_scale_are_accepted() -> None:
    # The rule is about information, not digit count. Every Databento price
    # arrives at scale 9 with trailing zeros.
    assert check_price(Decimal("5000.250000000")) is None
    assert check_price(Decimal("1.000000000000000")) is None


def test_very_large_exponent_is_magnitude_not_precision() -> None:
    # Reported as magnitude, not as "too many fractional digits" — the
    # misleading diagnosis the audit called out.
    assert check_price(Decimal("1E+400")) is NumericViolation.MAGNITUDE_TOO_LARGE


def test_very_small_exponent_is_precision() -> None:
    assert check_price(Decimal("1E-400")) is NumericViolation.TOO_MANY_FRACTIONAL_DIGITS


@pytest.mark.parametrize("value", NON_FINITE)
def test_non_finite_prices_and_quantities_are_rejected(value: str) -> None:
    assert check_price(Decimal(value)) is NumericViolation.NON_FINITE
    assert check_quantity(Decimal(value)) is NumericViolation.NON_FINITE


@pytest.mark.parametrize("value", [Decimal("0"), Decimal("-0"), Decimal("-1")])
def test_non_positive_values_are_rejected(value: Decimal) -> None:
    assert check_price(value) is NumericViolation.NOT_POSITIVE
    assert check_quantity(value) is NumericViolation.NOT_POSITIVE


def test_int_inputs_are_accepted() -> None:
    assert check_price(5000) is None
    assert check_quantity(2) is None


@pytest.mark.parametrize("value", [True, False])
def test_bool_is_rejected(value: bool) -> None:
    # bool is an int subclass, but True is never a price or a count.
    assert check_price(value) is NumericViolation.NOT_A_NUMBER
    assert check_quantity(value) is NumericViolation.NOT_A_NUMBER


@pytest.mark.parametrize("value", [1.5, "5000.25", None, object()])
def test_non_numeric_inputs_are_rejected(value: object) -> None:
    assert check_price(value) is NumericViolation.NOT_A_NUMBER
    assert check_quantity(value) is NumericViolation.NOT_A_NUMBER


# --- Quantities are counts -------------------------------------------------


def test_minimum_quantity_is_one() -> None:
    assert check_quantity(MIN_QUANTITY) is None
    assert MIN_QUANTITY == Decimal(1)


def test_maximum_quantity_is_accepted() -> None:
    assert check_quantity(MAX_QUANTITY) is None
    assert MAX_QUANTITY == Decimal(MAX_QUANTITY_INTEGER)


def test_one_above_maximum_quantity_is_rejected() -> None:
    assert check_quantity(MAX_QUANTITY + 1) is NumericViolation.MAGNITUDE_TOO_LARGE


@pytest.mark.parametrize("value", ["0.5", "1.1", "0.000001"])
def test_fractional_quantities_are_rejected(value: str) -> None:
    assert check_quantity(Decimal(value)) is NumericViolation.NOT_WHOLE


def test_whole_quantity_written_with_decimals_is_accepted() -> None:
    assert check_quantity(Decimal("2.000")) is None


# ==========================================================================
# Domain construction enforces the envelope
# ==========================================================================


@pytest.mark.parametrize(
    "bad", [Decimal("1E+400"), SEVEN_DECIMAL_PLACES, Decimal("0"), Decimal("-1")]
)
def test_trade_price_outside_the_envelope_is_refused(bad: Decimal) -> None:
    with pytest.raises(ValidationError):
        _trade(price=bad)


@pytest.mark.parametrize("bad", [Decimal("0.5"), Decimal("0"), MAX_QUANTITY + 1])
def test_trade_size_outside_the_envelope_is_refused(bad: Decimal) -> None:
    with pytest.raises(ValidationError):
        _trade(size=bad)


@pytest.mark.parametrize("field_name", ["bid", "ask"])
def test_quote_prices_are_enveloped(field_name: str) -> None:
    with pytest.raises(ValidationError):
        _quote(**{field_name: SEVEN_DECIMAL_PLACES})


@pytest.mark.parametrize("field_name", ["bid_size", "ask_size"])
def test_quote_sizes_are_enveloped(field_name: str) -> None:
    with pytest.raises(ValidationError):
        _quote(**{field_name: Decimal("0.5")})


@pytest.mark.parametrize("field_name", ["open", "high", "low", "close"])
def test_bar_prices_are_enveloped(field_name: str) -> None:
    with pytest.raises(ValidationError):
        _bar(**{field_name: SEVEN_DECIMAL_PLACES})


def test_bar_volume_is_enveloped() -> None:
    with pytest.raises(ValidationError):
        _bar(volume=Decimal("0.5"))


def test_domain_never_rounds_a_value_to_make_it_fit() -> None:
    # A value that does not fit is refused, never adjusted.
    with pytest.raises(ValidationError):
        _trade(price=Decimal("5000.1234567"))

    # And an accepted value is stored exactly as given.
    assert _trade(price=Decimal("5000.250000000")).price == Decimal("5000.25")


# ==========================================================================
# The invariant: every valid domain object is persistable
# ==========================================================================


def _boundary_prices() -> list[Decimal]:
    return [
        MIN_PRICE,
        PRICE_QUANTUM,
        SIX_DECIMAL_PLACES,
        Decimal("5000.25"),
        Decimal("5000.250000000"),
        Decimal(1),
        MAX_PRICE,
        MAX_PRICE - PRICE_QUANTUM,
    ]


def _boundary_quantities() -> list[Decimal]:
    return [
        MIN_QUANTITY,
        Decimal(2),
        Decimal("2.000"),
        Decimal(10**9),
        MAX_QUANTITY,
        MAX_QUANTITY - 1,
    ]


@pytest.mark.parametrize("price", _boundary_prices())
@pytest.mark.parametrize("quantity", [MIN_QUANTITY, MAX_QUANTITY])
def test_every_valid_trade_round_trips_through_storage(price: Decimal, quantity: Decimal) -> None:
    trade = _trade(price=price, size=quantity)

    round_trip = trade_from_storage_row(trade_to_storage_row(trade))

    assert round_trip == trade
    assert round_trip.price == price
    assert round_trip.size == quantity


@pytest.mark.parametrize("price", _boundary_prices())
def test_every_valid_quote_round_trips_through_storage(price: Decimal) -> None:
    quote = _quote(bid=price, ask=price, bid_size=MIN_QUANTITY, ask_size=MAX_QUANTITY)

    round_trip = quote_from_storage_row(quote_to_storage_row(quote))

    assert round_trip == quote


@pytest.mark.parametrize("quantity", _boundary_quantities())
def test_every_valid_bar_round_trips_through_storage(quantity: Decimal) -> None:
    bar = _bar(volume=quantity)

    round_trip = bar_from_storage_row(bar_to_storage_row(bar))

    assert round_trip == bar
    assert round_trip.volume == quantity


def test_boundary_price_grid_is_persistable_without_exception() -> None:
    # The invariant stated directly: construction implies persistability.
    for price in _boundary_prices():
        for quantity in _boundary_quantities():
            trade = _trade(price=price, size=quantity)
            row = trade_to_storage_row(trade)
            assert isinstance(row["price"], int)
            assert isinstance(row["size"], int)
            assert 0 < row["price"] <= MAX_PRICE_FIXED_POINT
            assert 0 < row["size"] <= MAX_QUANTITY_INTEGER


def test_fixed_point_encoding_is_exact_at_the_scale() -> None:
    assert _decimal_to_fixed_point(MIN_PRICE, "price") == 1
    assert _decimal_to_fixed_point(Decimal(1), "price") == 10**PRICE_SCALE
    assert _decimal_to_fixed_point(MAX_PRICE, "price") == MAX_PRICE_FIXED_POINT


def test_quantity_encoding_is_exact() -> None:
    assert _decimal_to_unsigned_int(MIN_QUANTITY, "size") == 1
    assert _decimal_to_unsigned_int(MAX_QUANTITY, "size") == MAX_QUANTITY_INTEGER


# ==========================================================================
# The converse: storage rejects what the domain would reject
# ==========================================================================


@pytest.mark.parametrize(
    ("value", "fragment"),
    [
        (Decimal("NaN"), "finite"),
        (Decimal("Infinity"), "finite"),
        (Decimal("0"), "strictly positive"),
        (Decimal("-1"), "strictly positive"),
        (JUST_ABOVE_MAX_PRICE, "representable range"),
        (SEVEN_DECIMAL_PLACES, "decimal places"),
    ],
)
def test_storage_rejects_every_out_of_envelope_price(value: Decimal, fragment: str) -> None:
    with pytest.raises(ValueError, match=fragment):
        _decimal_to_fixed_point(value, "price")


@pytest.mark.parametrize(
    ("value", "fragment"),
    [
        (Decimal("NaN"), "finite"),
        (Decimal("0"), "strictly positive"),
        (Decimal("-1"), "strictly positive"),
        (MAX_QUANTITY + 1, "representable range"),
        (Decimal("0.5"), "whole number"),
    ],
)
def test_storage_rejects_every_out_of_envelope_quantity(value: Decimal, fragment: str) -> None:
    with pytest.raises(ValueError, match=fragment):
        _decimal_to_unsigned_int(value, "size")


def test_storage_errors_name_the_offending_field() -> None:
    with pytest.raises(ValueError, match="^bid_size"):
        _decimal_to_unsigned_int(Decimal("-1"), "bid_size")


def test_magnitude_overflow_is_not_reported_as_a_precision_problem() -> None:
    # The specific misleading message the audit identified.
    with pytest.raises(ValueError) as error:
        _decimal_to_fixed_point(Decimal("1E+400"), "price")

    assert "decimal places" not in str(error.value)
    assert "representable range" in str(error.value)


# ==========================================================================
# One definition, not three
# ==========================================================================


def test_import_layer_delegates_to_the_domain_envelope() -> None:
    from quant_research_terminal.data_import import numeric_semantics
    from quant_research_terminal.domain import numeric

    assert numeric_semantics.check_price is numeric.check_price
    assert numeric_semantics.check_quantity is numeric.check_quantity


def test_storage_constants_are_the_domain_constants() -> None:
    from quant_research_terminal.data import contracts as storage_contracts
    from quant_research_terminal.domain import numeric

    assert storage_contracts.PRICE_SCALE == numeric.PRICE_SCALE
    assert storage_contracts.PRICE_PRECISION == numeric.PRICE_PRECISION
    assert storage_contracts.MAX_FIXED_POINT_VALUE == numeric.MAX_PRICE_FIXED_POINT
    assert storage_contracts.UINT64_MAX == numeric.MAX_QUANTITY_INTEGER
