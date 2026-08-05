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


def is_decimal_like(value: object) -> bool:
    """Return whether ``value`` can become a :class:`Decimal` without loss.

    :class:`Decimal` and :class:`int` qualify. ``bool`` does not: it is an
    ``int`` subclass, but a boolean is never a price, size, or volume, and
    accepting it would let ``True`` silently import as the quantity ``1``.
    ``float`` does not qualify, for the reason given in the module docstring.
    """
    if isinstance(value, bool):
        return False
    return isinstance(value, Decimal | int)


def to_decimal(value: object) -> Decimal:
    """Convert an accepted numeric value to :class:`Decimal`.

    Raises:
        ValueError: if ``value`` is not accepted by :func:`is_decimal_like`.
            Callers are expected to have validated first; reaching this error
            means a validation stage was skipped.
    """
    if isinstance(value, Decimal):
        return value
    if is_decimal_like(value) and isinstance(value, int):
        return Decimal(value)
    raise ValueError("value must be a Decimal")
