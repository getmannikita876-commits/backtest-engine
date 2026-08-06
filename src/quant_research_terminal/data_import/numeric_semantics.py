"""The import layer's view of the canonical numeric envelope.

The envelope itself — what counts as a representable price or quantity — is
defined once in :mod:`quant_research_terminal.domain.numeric`. This module
holds no rules of its own; it re-exports the parts the import stages need, so
those stages have one place to import from and no opportunity to restate a
rule.

Translating a violation into this layer's issue-code vocabulary belongs to
:mod:`~quant_research_terminal.data_import.validation`, which owns those codes.
Keeping that mapping out of here leaves this module a leaf that depends on
nothing above the domain.

Keeping the rules in the domain is what upholds the property the import layer
exists to serve: a record that passes validation constructs as a domain object,
and that object's numeric fields encode exactly into the storage schema.
"""

from __future__ import annotations

from decimal import Decimal

from quant_research_terminal.domain.numeric import (
    NumericEnvelopeError,
    NumericViolation,
    as_envelope_decimal,
    check_price,
    check_quantity,
    validate_price,
    validate_quantity,
    violation_message,
)

__all__ = [
    "NumericEnvelopeError",
    "NumericViolation",
    "check_price",
    "check_quantity",
    "is_decimal_like",
    "is_non_finite_decimal",
    "to_decimal",
    "validate_price",
    "validate_quantity",
    "violation_message",
]


def is_non_finite_decimal(value: object) -> bool:
    """Return whether ``value`` is a :class:`Decimal` that is not finite.

    Covers ``NaN``, ``sNaN``, ``Infinity``, and ``-Infinity``. Uses
    :meth:`Decimal.is_finite`, which inspects the value rather than comparing
    it: ordering comparisons against ``NaN`` are false and against ``sNaN``
    raise, so any guard built on comparison is defeated by exactly the values
    this predicate exists to catch.
    """
    return isinstance(value, Decimal) and not value.is_finite()


def is_decimal_like(value: object) -> bool:
    """Return whether ``value`` is a finite :class:`Decimal` or :class:`int`.

    A shape check only — it says nothing about positivity, magnitude, or scale.
    Use :func:`check_price` or :func:`check_quantity` for the full envelope.
    """
    number = as_envelope_decimal(value)
    return number is not None and number.is_finite()


def to_decimal(value: object) -> Decimal:
    """Convert an accepted numeric value to a finite :class:`Decimal`.

    Raises:
        ValueError: if the value is not a finite ``Decimal`` or ``int``.
            Callers are expected to have validated first; reaching this error
            means a validation stage was skipped.
    """
    number = as_envelope_decimal(value)
    if number is None:
        raise ValueError("value must be a Decimal")
    if not number.is_finite():
        raise ValueError("value must be a finite Decimal")
    return number
