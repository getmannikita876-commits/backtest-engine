"""Decimal value semantics shared by the data-import stages.

This module is the authoritative definition of "a usable numeric value" for
imported market data, and is the counterpart to
:mod:`quant_research_terminal.data_import.time_semantics`. Validation asks
whether a value is acceptable; normalization converts it. Both derive from the
predicate here, so the two stages can never disagree about what a price is.

Floats are rejected outright. Binary floating point cannot represent decimal
tick values exactly, so admitting a float would silently corrupt the exact
fixed-point encoding the storage contract depends on. A caller holding a float
must decide how to convert it and record that decision, rather than have this
package guess.
"""

from __future__ import annotations

from decimal import Decimal


def is_non_finite_decimal(value: object) -> bool:
    """Return whether ``value`` is a :class:`Decimal` that is not a finite number.

    Covers ``NaN``, ``sNaN``, ``Infinity``, and ``-Infinity``. The check uses
    :meth:`Decimal.is_finite`, which inspects the value rather than comparing
    it: ordering comparisons against ``NaN`` are false and comparisons against
    ``sNaN`` raise, so any guard built on comparison is defeated by exactly the
    values this predicate exists to catch.
    """
    return isinstance(value, Decimal) and not value.is_finite()


def is_decimal_like(value: object) -> bool:
    """Return whether ``value`` can become a finite :class:`Decimal` without loss.

    Finite :class:`Decimal` and :class:`int` values qualify. ``bool`` does not:
    it is an ``int`` subclass, but a boolean is never a price, size, or volume,
    and accepting it would let ``True`` silently import as the quantity ``1``.
    ``float`` does not qualify, for the reason given in the module docstring.

    Non-finite decimals do not qualify either. Admitting them would make every
    downstream ordering check — positivity, bid-versus-ask, the OHLC range —
    vacuously true or raise, so the value has to be excluded here rather than
    relied upon to fail a later comparison.
    """
    if isinstance(value, bool):
        return False
    if isinstance(value, Decimal):
        return value.is_finite()
    return isinstance(value, int)


def to_decimal(value: object) -> Decimal:
    """Convert an accepted numeric value to a finite :class:`Decimal`.

    Raises:
        ValueError: if ``value`` is not accepted by :func:`is_decimal_like`,
            including when it is a non-finite decimal. Callers are expected to
            have validated first; reaching this error means a validation stage
            was skipped, so it fails loudly rather than propagating a value no
            comparison can order.
    """
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise ValueError("value must be a finite Decimal")
        return value
    if is_decimal_like(value) and isinstance(value, int):
        return Decimal(value)
    raise ValueError("value must be a Decimal")
