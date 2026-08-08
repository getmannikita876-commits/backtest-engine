"""The authoritative rule for whether a decoded value is usable as a symbol.

This is a leaf rule module in the same sense as
:mod:`~quant_research_terminal.data_import.time_semantics` and
:mod:`~quant_research_terminal.data_import.numeric_semantics`: it answers one
question about one decoded value, depends on no other stage, and is consumed by
both the validator that reports the defect and the normalizer that would
otherwise have to guess.

Why this module exists
----------------------
Normalization used to build a domain object with
``str(record.value("instrument_symbol"))``. Duplicate detection, meanwhile,
compared the **raw** decoded value. The two therefore disagreed about what an
instrument is, and the disagreement was exploitable:

* a record carrying ``None`` and a record carrying ``"None"`` have *different*
  raw identities, so neither duplicate nor conflict detection related them;
* but both normalize to the instrument ``"None"``.

Two bars for the same period and the same instrument with **different volumes**
were therefore accepted together, with ``success=True`` and no issue raised —
precisely the silent double-counting ADR-005 exists to prevent, reached through
the instrument field instead of the period fields. The same holds for ``123``
versus ``"123"`` and ``True`` versus ``"True"``.

The rule below removes the disagreement by making the unusable value a
diagnosable rejection *before* either stage runs, so the coercion that hid it is
no longer needed anywhere.

Scope, stated honestly
----------------------
The rule rejects a value that is **not a string** and a string carrying **no
non-whitespace character**. It deliberately does *not* reject surrounding
whitespace, case variation, or any other spelling: ``" ESM6 "`` and ``"ESM6"``
remain two distinct instruments here, as they always have. That is a real
limitation, and it is the reason canonical futures identity is a separate,
strictly validated type
(:class:`~quant_research_terminal.domain.futures_contract.FuturesContractId`)
rather than a tightened string. Narrowing the legacy string field further would
change what previously written schema-v2 files mean on read-back, which is a
storage decision and not an import-layer one. See ADR-009.
"""

from __future__ import annotations

from enum import StrEnum
from typing import cast


class InstrumentSymbolViolation(StrEnum):
    """Why a decoded value cannot serve as an instrument symbol."""

    NOT_A_STRING = "not_a_string"
    BLANK = "blank"


def check_instrument_symbol(value: object) -> InstrumentSymbolViolation | None:
    """Return the violation ``value`` commits, or ``None`` when it is usable.

    Returns rather than raises, so a single defective record cannot abort
    validation of a whole batch.
    """
    if not isinstance(value, str):
        # A bare ``bool``, ``int``, ``None``, or list is not a symbol. Converting
        # one with ``str()`` does not recover an identity, it fabricates one.
        return InstrumentSymbolViolation.NOT_A_STRING
    if not value.strip():
        # ``"   "`` satisfies the domain model's ``min_length=1`` while carrying
        # no instrument identity at all.
        return InstrumentSymbolViolation.BLANK
    return None


def violation_message(violation: InstrumentSymbolViolation, field_name: str) -> str:
    """Return the human-readable explanation for ``violation``."""
    if violation is InstrumentSymbolViolation.NOT_A_STRING:
        return (
            f"{field_name} must be a string; a non-string value cannot identify an "
            f"instrument and is never converted to one"
        )
    return f"{field_name} must contain a non-whitespace value"


def require_instrument_symbol(value: object, field_name: str) -> str:
    """Return the canonical plain-``str`` form of a usable symbol, or raise.

    Called by **both** the duplicate-identity function and the normalizer, which
    is the point: the value duplicate detection compares must be the value the
    domain object ends up holding, or the two disagree and a conflicting record
    slips through the gap between them.

    ``str(...)`` here is not the coercion this module exists to remove. It runs
    only *after* :func:`check_instrument_symbol` has established that the value
    is a string, so it can never fabricate an identity out of ``None`` or an
    integer. What it does do is collapse a ``str`` **subclass** to an exact
    ``str``. That matters because a subclass may override ``__eq__`` and
    ``__hash__``: two records carrying such a value compared unequal in the
    import layer while Pydantic stored ordinary equal strings in the domain, so
    two conflicting bars for one period were accepted together — the same
    failure as the ``None``/``"None"`` case, one level down. Reproduced before
    this line existed.

    An ordinary symbol is returned unchanged: nothing is trimmed, case-folded,
    or otherwise altered.

    Raises:
        ValueError: if ``value`` cannot serve as an instrument symbol.
    """
    violation = check_instrument_symbol(value)
    if violation is not None:
        raise ValueError(violation_message(violation, field_name))
    return str(cast("str", value))
