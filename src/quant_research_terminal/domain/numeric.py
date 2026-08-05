"""The authoritative numeric envelope for market-data values.

One definition, used everywhere
-------------------------------
Every layer that accepts a price or a quantity enforces exactly these rules:
the domain models on construction, the import validation stage when judging a
raw record, and the storage conversion defensively at the persistence boundary.
There is deliberately no second definition anywhere — a value that constructs
as a domain object is guaranteed to persist, because both answer to this module.

This module sits in the domain package because the domain is the foundation and
may not depend on storage. It imports nothing from this project, so every layer
above can use it without inverting the dependency direction.

Two envelopes, not one
----------------------
Prices and quantities are different kinds of number and are treated as such:

* A **price** is a fractional decimal on a fixed scale. It is carried to
  persistence as a signed fixed-point integer of ``value * 10**PRICE_SCALE``.
* A **quantity** — a trade size, a quote size, a bar volume — is a *count*. It
  is carried as an unsigned integer, so it must be a whole number.

Collapsing these into one envelope would either strip prices of their fractional
scale or admit fractional contract counts that cannot be persisted. The
asymmetry is real, so it is modelled rather than hidden.

Fractional quantities are therefore rejected. That is a futures-first decision:
contracts are whole. Admitting fractional shares would require a storage schema
change, not merely a looser validator.

No rounding, ever
-----------------
A value that does not fit the envelope is **rejected**, never adjusted. Nothing
here quantizes, truncates, clamps, or coerces. Silently altering a price is a
data-integrity failure that would propagate into every downstream result, so
the only safe response to an unrepresentable value is to refuse it.

The maximum exact fractional precision is 6 decimal places. The rule is about
*information*, not raw digit count: a value carrying more than 6 decimal places
is accepted when the digits beyond the sixth place are all trailing zeros,
because removing them loses nothing. ``5000.250000000`` is nine decimal places
wide but every digit past the sixth is zero, so it is accepted unchanged as
``5000.250000``. ``5000.2500001`` is rejected, because representing it at 6
decimal places would require discarding a non-zero fractional digit. Judging by
raw digit count rather than by information would reject exactly representable
values — vendor decoders that emit fixed-point values commonly produce them.
"""

from __future__ import annotations

import decimal
from decimal import Decimal
from enum import StrEnum
from typing import Final

#: Decimal places retained for prices. Prices persist as
#: ``value * 10**PRICE_SCALE`` in a signed 64-bit integer.
PRICE_SCALE: Final[int] = 6

#: Total significant digits allowed in the fixed-point price representation.
PRICE_PRECISION: Final[int] = 18

#: The smallest price increment the envelope can represent.
PRICE_QUANTUM: Final[Decimal] = Decimal(1).scaleb(-PRICE_SCALE)

#: Largest fixed-point integer a price may occupy. Comfortably inside the
#: signed 64-bit range the Arrow schema declares.
MAX_PRICE_FIXED_POINT: Final[int] = 10**PRICE_PRECISION - 1

#: Inclusive bounds on a price.
MAX_PRICE: Final[Decimal] = Decimal(MAX_PRICE_FIXED_POINT).scaleb(-PRICE_SCALE)
MIN_PRICE: Final[Decimal] = PRICE_QUANTUM

#: Largest quantity a record may carry, matching unsigned 64-bit storage.
MAX_QUANTITY_INTEGER: Final[int] = 2**64 - 1
MAX_QUANTITY: Final[Decimal] = Decimal(MAX_QUANTITY_INTEGER)
MIN_QUANTITY: Final[Decimal] = Decimal(1)


class NumericViolation(StrEnum):
    """Why a value falls outside the envelope.

    These names are stable and machine-readable. Each layer maps them onto its
    own diagnostic vocabulary; the reason itself is decided once, here.
    """

    NOT_A_NUMBER = "not_a_number"
    NON_FINITE = "non_finite"
    NOT_POSITIVE = "not_positive"
    MAGNITUDE_TOO_LARGE = "magnitude_too_large"
    TOO_MANY_FRACTIONAL_DIGITS = "too_many_fractional_digits"
    NOT_WHOLE = "not_whole"


_VIOLATION_MESSAGES: Final[dict[NumericViolation, str]] = {
    NumericViolation.NOT_A_NUMBER: "must be a Decimal or int, not a float or bool",
    NumericViolation.NON_FINITE: "must be a finite number",
    NumericViolation.NOT_POSITIVE: "must be strictly positive",
    NumericViolation.MAGNITUDE_TOO_LARGE: "exceeds the representable range",
    NumericViolation.TOO_MANY_FRACTIONAL_DIGITS: (
        f"must be representable with at most {PRICE_SCALE} decimal places"
    ),
    NumericViolation.NOT_WHOLE: "must be a whole number",
}


class NumericEnvelopeError(ValueError):
    """Raised when a value falls outside the numeric envelope."""

    def __init__(self, violation: NumericViolation, field_name: str) -> None:
        super().__init__(f"{field_name} {_VIOLATION_MESSAGES[violation]}")
        self.violation = violation
        self.field_name = field_name


def violation_message(violation: NumericViolation, field_name: str) -> str:
    """Return the canonical message for a violation against a named field."""
    return f"{field_name} {_VIOLATION_MESSAGES[violation]}"


def as_envelope_decimal(value: object) -> Decimal | None:
    """Return ``value`` as a :class:`Decimal`, or ``None`` if it is not numeric.

    :class:`Decimal` and :class:`int` are accepted. ``bool`` is not: it is an
    ``int`` subclass, but a boolean is never a price or a count, and accepting
    it would let ``True`` import as the quantity ``1``. ``float`` is not
    accepted either — binary floating point cannot represent decimal tick
    values exactly, so admitting one would corrupt the fixed-point encoding
    before any check could see it.
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, Decimal):
        return value
    if isinstance(value, int):
        return Decimal(value)
    return None


def _loses_information_at_scale(value: Decimal, scale: int) -> bool:
    """Return whether representing ``value`` at ``scale`` would discard a digit.

    Traps ``Inexact`` only. ``Rounded`` fires whenever *any* digit is dropped,
    including trailing zeros, which carry no information — trapping it would
    reject values that are exactly representable.
    """
    with decimal.localcontext() as context:
        context.traps[decimal.Inexact] = True
        context.prec = max(len(value.as_tuple().digits) + scale + 1, decimal.MAX_PREC // 2)
        try:
            value.quantize(Decimal(1).scaleb(-scale), context=context)
        except (decimal.InvalidOperation, decimal.Inexact):
            return True
    return False


def check_price(value: object) -> NumericViolation | None:
    """Return why ``value`` is not a valid price, or ``None`` if it is.

    Checks run from most to least fundamental. Magnitude is tested before
    precision so an enormous value is reported as out of range rather than as
    having too many decimal places, which is what it would look like to a
    quantize-based check.
    """
    number = as_envelope_decimal(value)
    if number is None:
        return NumericViolation.NOT_A_NUMBER
    if not number.is_finite():
        return NumericViolation.NON_FINITE
    if number <= 0:
        return NumericViolation.NOT_POSITIVE
    if number > MAX_PRICE:
        return NumericViolation.MAGNITUDE_TOO_LARGE
    if _loses_information_at_scale(number, PRICE_SCALE):
        return NumericViolation.TOO_MANY_FRACTIONAL_DIGITS
    return None


def check_quantity(value: object) -> NumericViolation | None:
    """Return why ``value`` is not a valid quantity, or ``None`` if it is."""
    number = as_envelope_decimal(value)
    if number is None:
        return NumericViolation.NOT_A_NUMBER
    if not number.is_finite():
        return NumericViolation.NON_FINITE
    if number <= 0:
        return NumericViolation.NOT_POSITIVE
    if number > MAX_QUANTITY:
        return NumericViolation.MAGNITUDE_TOO_LARGE
    if number != number.to_integral_value():
        return NumericViolation.NOT_WHOLE
    return None


def validate_price(value: object, field_name: str = "price") -> Decimal:
    """Return ``value`` as a price, unchanged.

    Raises:
        NumericEnvelopeError: if the value falls outside the price envelope.
    """
    number = as_envelope_decimal(value)
    violation = check_price(value)
    if number is None or violation is not None:
        raise NumericEnvelopeError(violation or NumericViolation.NOT_A_NUMBER, field_name)
    return number


def validate_quantity(value: object, field_name: str = "quantity") -> Decimal:
    """Return ``value`` as a quantity, unchanged.

    Raises:
        NumericEnvelopeError: if the value falls outside the quantity envelope.
    """
    number = as_envelope_decimal(value)
    violation = check_quantity(value)
    if number is None or violation is not None:
        raise NumericEnvelopeError(violation or NumericViolation.NOT_A_NUMBER, field_name)
    return number


def price_to_fixed_point(value: Decimal) -> int:
    """Encode a validated price as its fixed-point integer.

    The value is scaled, never rounded: :func:`check_price` has already
    established that the scaling is exact.
    """
    return int(value.scaleb(PRICE_SCALE).to_integral_value())


def fixed_point_to_price(value: int) -> Decimal:
    """Decode a fixed-point integer back into a price."""
    return Decimal(value).scaleb(-PRICE_SCALE)
