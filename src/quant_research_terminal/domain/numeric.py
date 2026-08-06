"""The canonical numeric envelope for market-data values.

One definition, used everywhere
-------------------------------
This module is the single source of truth for what counts as a representable
price or quantity. The domain models enforce it on construction, the import
validation stage consults it when judging a raw record, and the storage
conversion applies it defensively at the encoding boundary. No other module
defines a numeric rule or restates a bound.

It lives in the domain package because the domain is the foundation and may not
depend on storage, while every layer above may depend on the domain. It imports
nothing from this project, so placing it here inverts no dependency.

The invariant it exists to guarantee:

    Every domain object accepted by this envelope has numeric fields that can
    be encoded exactly by the storage schema — as fixed-point integers and
    unsigned integers — without rounding, truncation, overflow, or coercion
    through float.

Two envelopes, not one
----------------------
Prices and quantities are different kinds of number and are modelled as such:

* A **price** is a fractional decimal on a fixed scale, carried to persistence
  as a signed fixed-point integer of ``value * 10**PRICE_SCALE``.
* A **quantity** — a trade size, a quote size, a bar volume — is a *count*,
  carried as an unsigned integer, so it must be a whole number.

Collapsing them would either strip prices of their fractional scale or admit
fractional contract counts that cannot be persisted. Rejecting fractional
quantities is a futures-first decision: contracts are whole. Fractional units
would need a storage schema change, not a looser validator.

Maximum exact fractional precision: 6 decimal places
----------------------------------------------------
The precision rule is about *information*, not raw digit count. A value wider
than six decimal places is accepted when every digit beyond the sixth place is
a trailing zero, because removing those loses nothing:

* ``5000.250000000`` — accepted. Nine decimal places, but the digits past the
  sixth are all zero, so it represents exactly as ``5000.250000``.
* ``5000.2500001`` — rejected. Representing it at six decimal places would
  require discarding a non-zero fractional digit.

This is implemented by trapping :data:`decimal.Inexact` and **not**
:data:`decimal.Rounded`. ``Rounded`` is signalled whenever *any* digit is
discarded, including trailing zeros; trapping it rejects values that are
exactly representable. Vendor decoders that emit fixed-point values commonly
produce them — a Databento price arrives at scale nine — so trapping ``Rounded``
made every such price unstorable.

No rounding, ever
-----------------
A value outside the envelope is **rejected**, never adjusted. Nothing here
quantizes, truncates, clamps, or coerces. Silently altering a price is a
data-integrity failure that would propagate into every downstream result, so
the only safe response to an unrepresentable value is to refuse it.
"""

from __future__ import annotations

import decimal
from decimal import Decimal
from enum import StrEnum
from typing import Final

#: Decimal places retained for prices. A price persists as
#: ``value * 10**PRICE_SCALE`` in a signed 64-bit integer.
PRICE_SCALE: Final[int] = 6

#: Total digits allowed in the fixed-point price representation.
PRICE_PRECISION: Final[int] = 18

#: The smallest price increment the envelope can represent.
PRICE_QUANTUM: Final[Decimal] = Decimal(1).scaleb(-PRICE_SCALE)

#: Largest fixed-point integer a price may occupy. Comfortably inside the
#: signed 64-bit range the Arrow schema declares.
MAX_PRICE_FIXED_POINT: Final[int] = 10**PRICE_PRECISION - 1

#: Inclusive price bounds.
MIN_PRICE: Final[Decimal] = PRICE_QUANTUM
MAX_PRICE: Final[Decimal] = Decimal(MAX_PRICE_FIXED_POINT).scaleb(-PRICE_SCALE)

#: Largest quantity a record may carry, matching unsigned 64-bit storage.
MAX_QUANTITY_INTEGER: Final[int] = 2**64 - 1

#: Inclusive quantity bounds. Quantities are counts, so the smallest is one.
MIN_QUANTITY: Final[Decimal] = Decimal(1)
MAX_QUANTITY: Final[Decimal] = Decimal(MAX_QUANTITY_INTEGER)

#: Working precision for the exactness check. Generous enough for the widest
#: in-range value, fixed so the result never depends on ambient context.
_CHECK_PRECISION: Final[int] = PRICE_PRECISION + PRICE_SCALE + 8


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
        f"must be representable within {PRICE_SCALE} decimal places"
    ),
    NumericViolation.NOT_WHOLE: "must be a whole number",
}


class NumericEnvelopeError(ValueError):
    """Raised when a value falls outside the numeric envelope.

    Carries the machine-readable :attr:`violation` so a caller can branch on
    the reason rather than parse a message.
    """

    def __init__(self, violation: NumericViolation, field_name: str) -> None:
        super().__init__(violation_message(violation, field_name))
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
    accepted either — binary floating point cannot represent decimal tick values
    exactly, so admitting one would corrupt the fixed-point encoding before any
    check could observe it.
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

    Traps :data:`decimal.Inexact` only. :data:`decimal.Rounded` fires whenever
    any digit is dropped — including trailing zeros, which carry no information
    — so trapping it would reject exactly representable values.
    """
    with decimal.localcontext() as context:
        context.prec = _CHECK_PRECISION
        context.traps[decimal.Inexact] = True
        try:
            value.quantize(Decimal(1).scaleb(-scale), context=context)
        except (decimal.InvalidOperation, decimal.Inexact):
            return True
    return False


def check_price(value: object) -> NumericViolation | None:
    """Return why ``value`` is not a representable price, or ``None`` if it is.

    Checks run from most to least fundamental. Magnitude is tested **before**
    precision so an enormous value is reported as out of range rather than as
    having too many decimal places — which is how a quantize-based check would
    misdescribe it, because ``quantize`` signals an invalid operation when the
    result would need more digits than the context allows.
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
    """Return why ``value`` is not a representable quantity, or ``None``."""
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
    established that the scaling discards nothing.
    """
    return int(value.scaleb(PRICE_SCALE).to_integral_value())


def fixed_point_to_price(value: int) -> Decimal:
    """Decode a fixed-point integer back into a price."""
    return Decimal(value).scaleb(-PRICE_SCALE)
