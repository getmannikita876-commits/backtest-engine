"""The delivery month of a listed futures contract.

A futures contract's delivery month is published as a single-letter code, and
the mapping from code to calendar month is a market convention rather than
anything derivable. Modelling it as a closed vocabulary in one module is what
stops the mapping being restated — differently — in a rollover module, a
calendar module, and a vendor decoder.

The vocabulary is deliberately closed. There are twelve codes and no more; a
thirteenth would not be an unrecognised month but a misread symbol, and mapping
it onto something plausible would fabricate a contract that was never listed.

Convention and its verification status
--------------------------------------
The twelve codes below are the standard futures delivery-month codes in
general use across listed derivatives markets. They are recorded here as
**repository policy**: this project adopts them, and every module reads them
from this one place.

Verification actually performed, stated precisely so the status is not
overclaimed:

* **Corroborated against a CME Group source:** ``H`` March, ``M`` June,
  ``U`` September, ``Z`` December — four of the twelve.
* **Not verified against a primary source:** the remaining eight (``F``, ``G``,
  ``J``, ``K``, ``N``, ``Q``, ``V``, ``X``). Direct retrieval of CME Group's
  contract-month-code page was not possible from the environment in which this
  module was written, so those eight are adopted convention, not audited fact.

The distinction matters because a wrong entry shifts every affected contract in
the research universe by a month, silently. Anyone with access to the primary
specification should confirm the remaining eight; the mapping is in one place
and one table, so confirming it is a single review.

If a venue is ever found to publish a different mapping, that is a
venue-specific fact and belongs in a venue-aware layer, not in a silent edit
here.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Final

#: The number of calendar months, and therefore of delivery-month codes.
MONTHS_IN_YEAR: Final[int] = 12


class ContractMonth(StrEnum):
    """The delivery month of a listed futures contract.

    The enum's *value* is the published single-letter code, so a member
    stringifies to exactly what appears in a canonical contract identity. The
    member *name* is the calendar month, so code that reads better with a name
    can use one without introducing a second mapping.

    Construction from external text goes through :meth:`from_code`, which is
    strict: no case folding, no whitespace tolerance. See that method for why.
    """

    JANUARY = "F"
    FEBRUARY = "G"
    MARCH = "H"
    APRIL = "J"
    MAY = "K"
    JUNE = "M"
    JULY = "N"
    AUGUST = "Q"
    SEPTEMBER = "U"
    OCTOBER = "V"
    NOVEMBER = "X"
    DECEMBER = "Z"

    @property
    def code(self) -> str:
        """Return the published single-letter delivery-month code."""
        return self.value

    @property
    def month_number(self) -> int:
        """Return the calendar month number, 1 for January through 12 for December.

        This is the one place code and calendar month are related. A caller that
        needs to order contracts chronologically builds its own key from this
        and the contract year; the ordering is a property of the *delivery
        period*, not of instrument identity, so it is deliberately not offered
        as a comparison operator on an identity type.
        """
        return _MONTH_NUMBERS[self]

    @classmethod
    def from_code(cls, code: str) -> ContractMonth:
        """Return the month for a published delivery-month ``code``.

        Strict by design, and deliberately unlike
        :func:`~quant_research_terminal.domain.trade_side.parse_trade_side`,
        which tolerates case and surrounding whitespace. That tolerance is
        right for a *vocabulary field* read back from storage or configuration.
        It is wrong for an *identity component*: a delivery-month code is a
        single character, so accepting ``"m"`` alongside ``"M"`` would put a
        lower-case vendor spelling one step away from becoming canonical
        identity, and two spellings of one contract are how a research universe
        acquires a phantom instrument.

        Raises:
            ValueError: if ``code`` is not a :class:`str`, or is not exactly one
                of the twelve published codes.
        """
        if not isinstance(code, str):
            raise ValueError(f"contract month code must be a string; got {code!r}")
        month = _MONTHS_BY_CODE.get(code)
        if month is None:
            accepted = ", ".join(member.value for member in cls)
            raise ValueError(f"contract month code must be one of: {accepted}; got {code!r}")
        return month

    @classmethod
    def from_month_number(cls, month_number: int) -> ContractMonth:
        """Return the month for a calendar ``month_number`` in 1..12.

        The inverse of :attr:`month_number`, offered so that code holding a
        calendar month does not build a second code table to get back to a
        delivery month.

        Raises:
            ValueError: if ``month_number`` is not an :class:`int` in 1..12.
                ``bool`` is rejected explicitly: it is a subclass of ``int``, so
                ``ContractMonth.from_month_number(True)`` would otherwise
                silently mean January.
        """
        if isinstance(month_number, bool) or not isinstance(month_number, int):
            raise ValueError(f"month number must be an int in 1..{MONTHS_IN_YEAR}")
        month = _MONTHS_BY_NUMBER.get(month_number)
        if month is None:
            raise ValueError(
                f"month number must be an int in 1..{MONTHS_IN_YEAR}; got {month_number!r}"
            )
        return month


#: Calendar month number for each delivery-month code.
#:
#: Written out in declaration order rather than derived from ``enum`` position,
#: so the mapping is reviewable as data and a future reordering of the enum
#: cannot silently shift every month by one.
_MONTH_NUMBERS: Final[dict[ContractMonth, int]] = {
    ContractMonth.JANUARY: 1,
    ContractMonth.FEBRUARY: 2,
    ContractMonth.MARCH: 3,
    ContractMonth.APRIL: 4,
    ContractMonth.MAY: 5,
    ContractMonth.JUNE: 6,
    ContractMonth.JULY: 7,
    ContractMonth.AUGUST: 8,
    ContractMonth.SEPTEMBER: 9,
    ContractMonth.OCTOBER: 10,
    ContractMonth.NOVEMBER: 11,
    ContractMonth.DECEMBER: 12,
}

_MONTHS_BY_CODE: Final[dict[str, ContractMonth]] = {
    member.value: member for member in ContractMonth
}

_MONTHS_BY_NUMBER: Final[dict[int, ContractMonth]] = {
    number: member for member, number in _MONTH_NUMBERS.items()
}
