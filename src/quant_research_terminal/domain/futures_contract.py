"""Canonical identity of a specific listed futures contract.

Until this module existed the platform identified an instrument by a bare
string — ``"ESM6"`` — carried on every trade, quote, and bar. A bare string
cannot distinguish the concepts futures research depends on:

* ``ES`` is a *product*, not something that can be bought or sold.
* ``ESM6`` and ``ESU6`` are different listed contracts with different
  expirations, different open interest, and different prices.
* ``ES1!`` and "back-adjusted continuous ES" are *synthetic series*. They are
  research constructs, not instruments; no order was ever filled in one.
* ``ESM6`` does not say **which** June. The year digit is an abbreviation, and
  ``6`` is June 2006, 2016, 2026, or 2036 depending on a decade the symbol does
  not carry.

This module answers exactly one question — *which listed futures contract is
this?* — and refuses to answer any other.

The model
---------
::

    Venue              a namespace token: which market's symbology this is
      +-- FuturesProduct     venue + product root, e.g. CME:ES
            +-- FuturesContractId   product + delivery month + full year

Each layer is a distinct type, so a product cannot be passed where a contract
is required. ``ES`` is a :class:`FuturesProduct`; there is no value of that
type that is executable, and :func:`require_listed_contract` accepts only a
:class:`FuturesContractId`.

What identity is *not*
----------------------
**Not specification.** Tick size, point value, multiplier, currency, fee
schedule, margin, settlement method, trading hours, first notice date, and last
trade date are properties *of* a contract, not part of *which* contract it is.
Several of them change over a contract's life, so folding them into identity
would make a contract stop being equal to itself after a rule change. No
specification type is introduced here; the repository holds no authoritative
source for one, and inventing one would be fake completeness.

**Not provenance.** Provider name, source file, source row, dataset id, vendor
sequence, and import id describe an *observation of* a contract, not the
contract. Identity and provenance are different dimensions and are kept in
different types (see ADR-007's provenance limitation and ADR-009).

**Not a vendor symbol.** A vendor's spelling is an alias. Two providers may use
different aliases for one contract and the same alias for different contracts;
mapping an alias onto canonical identity is explicit, caller-supplied data, and
this module deliberately contains no alias table and no vendor symbol parser.
:meth:`FuturesContractId.parse` accepts the canonical form and nothing else.

No wall clock, anywhere
-----------------------
Nothing in this module reads the current date, the current year, the machine's
timezone, or the locale. A contract year is always supplied in full, and the
one function that interprets an abbreviated year —
:func:`resolve_abbreviated_contract_year` — requires the caller to state the
cycle explicitly and has no default. Parsing ``"ESM6"`` therefore cannot mean
one thing today and another thing in 2036.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any, Final, Self, final

from pydantic import field_validator

from quant_research_terminal.domain.common import _BaseDomainModel
from quant_research_terminal.domain.contract_month import ContractMonth

#: Separator between the fields of a canonical identity.
#:
#: Excluded from every field's own character set, so splitting a canonical
#: string on it can never be ambiguous and no field can smuggle a separator in.
CANONICAL_SEPARATOR: Final[str] = ":"

#: Number of digits a canonical contract year is written with.
CONTRACT_YEAR_DIGITS: Final[int] = 4

#: Inclusive bounds on a contract year.
#:
#: These are derived from the serialization contract, not from a judgement
#: about market history. A canonical identity writes the year with exactly
#: :data:`CONTRACT_YEAR_DIGITS` digits, so a year that cannot be written in four
#: digits has no canonical form and must be rejected rather than serialized into
#: something that would not parse back. Choosing a "sensible" lower bound such
#: as 1970 or the founding year of an exchange would be an arbitrary market
#: judgement encoded as a type constraint; four-digit representability is not.
#: The range also sits inside :class:`~datetime.datetime`'s own year range, so
#: no contract year is unrepresentable to a future calendar layer.
MIN_CONTRACT_YEAR: Final[int] = 10 ** (CONTRACT_YEAR_DIGITS - 1)
MAX_CONTRACT_YEAR: Final[int] = 10**CONTRACT_YEAR_DIGITS - 1

_MAX_VENUE_LENGTH: Final[int] = 16
_MAX_ROOT_LENGTH: Final[int] = 12

#: Character sets for the two free-text identity fields.
#:
#: ASCII upper-case letters and digits only. The ranges are literal code-point
#: ranges rather than ``\w`` or ``\d``, which are Unicode-aware for ``str``
#: patterns: ``\d`` matches ``"٦"`` and ``\w`` matches ``"Ε"``, either of which
#: would let a visually convincing look-alike become a distinct instrument.
_VENUE_PATTERN: Final[re.Pattern[str]] = re.compile(f"[A-Z0-9]{{1,{_MAX_VENUE_LENGTH}}}")
_ROOT_PATTERN: Final[re.Pattern[str]] = re.compile(f"[A-Z0-9]{{1,{_MAX_ROOT_LENGTH}}}")
_HAS_LETTER_PATTERN: Final[re.Pattern[str]] = re.compile("[A-Z]")

#: Abbreviated year codes this project knows how to interpret, given a cycle.
_ABBREVIATED_YEAR_PATTERN: Final[re.Pattern[str]] = re.compile("[0-9]{1,2}")

#: The canonical delivery field: one month code followed by the full year.
_DELIVERY_PATTERN: Final[re.Pattern[str]] = re.compile(f"([A-Z])([0-9]{{{CONTRACT_YEAR_DIGITS}}})")


def _require_identity_token(
    value: str, *, label: str, pattern: re.Pattern[str], max_length: int
) -> str:
    """Return ``value`` unchanged, or raise describing why it is not canonical.

    Validation is strict and **nothing is normalized**. A value that is not
    already canonical is rejected rather than trimmed or upper-cased, because
    normalizing is how two source spellings silently become one instrument —
    and, worse, how one spelling silently becomes a *different* instrument than
    the operator wrote. Rejection is loud, local, and fixable at the call site;
    a normalized value is none of those.

    The "at least one letter" rule is not cosmetic. Databento identifies an
    instrument by a numeric, vendor-local ``instrument_id``, and the provider
    layer already documents that emitting that identifier as a symbol "would
    corrupt instrument identity across the whole platform". Requiring a letter
    means a bare vendor id can never be accepted as a product root or a venue,
    structurally rather than by a reviewer noticing.
    """
    if not isinstance(value, str):
        # Defence in depth, following the same pattern as the storage layer's
        # numeric re-validation: strict field validation rejects a non-string
        # before this runs, so reaching here means the helper was called
        # directly or a field's declared type was loosened.
        raise ValueError(f"{label} must be a string; got {value!r}")
    if not value:
        raise ValueError(f"{label} must not be empty")
    if len(value) > max_length:
        raise ValueError(
            f"{label} must be at most {max_length} characters; got {len(value)} in {value!r}"
        )
    if pattern.fullmatch(value) is None:
        raise ValueError(
            f"{label} must consist only of ASCII upper-case letters and digits, with no "
            f"whitespace, punctuation, or separator; got {value!r}. Values are never "
            f"trimmed or upper-cased — supply the canonical spelling"
        )
    if _HAS_LETTER_PATTERN.search(value) is None:
        raise ValueError(
            f"{label} must contain at least one ASCII letter so that a purely numeric "
            f"vendor identifier cannot become an instrument identity; got {value!r}"
        )
    return value


class _CanonicalModel(_BaseDomainModel):
    """A frozen model whose every instance is guaranteed already canonical.

    The base exists for one reason: :meth:`~pydantic.BaseModel.model_copy`
    **skips validation**, and for an identity type that is not a convenience,
    it is a hole. Reproduced before this override existed::

        forged = identity.model_copy(update={"contract_year": 6})
        forged.canonical()                  # 'CME:ES:M0006'
        FuturesContractId.parse(_)          # ValidationError

    That is a serializer emitting a string its own parser rejects — a
    catalogue key written today that cannot be read back tomorrow — reached
    through an ordinary-looking public method whose documentation gives no hint
    that validation is skipped. ``update={"contract_year": True}`` produced year
    1 the same way, and an unrelated key such as ``tick_size`` was accepted into
    the model despite ``extra="forbid"``.

    Re-validating the copy closes it. The cost is one extra validation pass on
    a method that is not on any hot path.

    What is deliberately **not** defended against, so the guarantee is not
    overstated: :meth:`~pydantic.BaseModel.model_construct` and
    ``object.__setattr__``. Both are explicit, documented bypasses that announce
    themselves at the call site — the equivalent of casting away a const — and
    a type that tried to block them would be fighting the language rather than
    protecting a caller from an easy mistake.
    """

    def model_copy(self, *, update: Mapping[str, Any] | None = None, deep: bool = False) -> Self:
        """Return a validated copy, unlike the unvalidated Pydantic default."""
        copied = super().model_copy(update=update, deep=deep)
        return type(self).model_validate(dict(copied.__dict__))


@final
class Venue(_CanonicalModel):
    """The market namespace a product's symbology belongs to.

    Venue is part of identity because a product root is only unique *within* a
    market: two exchanges may each list a root spelled ``ES``, and without a
    venue the two would be one instrument.

    Deliberately shallow — and honestly so
    --------------------------------------
    :attr:`code` is an **opaque namespace token**. This repository contains no
    authoritative venue registry, so the type does not claim to be any of the
    following, and must not be read as one:

    * an ISO 10383 MIC (operating or segment);
    * an exchange group's marketing name;
    * a vendor's venue or dataset code;
    * a matching-engine or technical venue identifier.

    Those are genuinely different concepts that a richer venue model would have
    to distinguish, and inventing the distinction from no evidence would be
    fake precision. What the type *does* guarantee is that the token is
    canonical, immutable, and part of equality, so identities from two different
    namespaces can never collide.

    The consequence, stated plainly: it is the operator's responsibility to use
    one token per market consistently. ``"CME"`` and ``"XCME"`` are two venues
    to this model, because it has no basis on which to know they are one.
    Resolving that belongs to a venue registry in a later phase.
    """

    code: str

    @field_validator("code")
    @classmethod
    def _validate_code(cls, value: str) -> str:
        return _require_identity_token(
            value, label="venue code", pattern=_VENUE_PATTERN, max_length=_MAX_VENUE_LENGTH
        )

    def canonical(self) -> str:
        """Return the deterministic canonical form of this venue."""
        return self.code


@final
class FuturesProduct(_CanonicalModel):
    """A futures product — a root listed on a venue — which is *not* tradable.

    ``CME:ES`` names the E-mini S&P 500 futures *product*. No order can be
    placed in it: an order names a delivery month. This type exists precisely
    so that the distinction is carried by the type system rather than by
    convention, which is why it is a separate class instead of two fields on
    :class:`FuturesContractId`.

    Nothing that accepts a product can be handed a contract by accident, and
    :func:`require_listed_contract` rejects a product outright. A product is the
    right key for "which contracts belong to the same roll chain"; it is never
    the right key for a position, a fill, or an order.
    """

    venue: Venue
    root: str

    @field_validator("root")
    @classmethod
    def _validate_root(cls, value: str) -> str:
        return _require_identity_token(
            value, label="product root", pattern=_ROOT_PATTERN, max_length=_MAX_ROOT_LENGTH
        )

    def canonical(self) -> str:
        """Return the deterministic canonical form, ``VENUE:ROOT``."""
        return f"{self.venue.canonical()}{CANONICAL_SEPARATOR}{self.root}"

    @classmethod
    def parse(cls, text: str) -> FuturesProduct:
        """Return the product described by a canonical ``VENUE:ROOT`` string.

        The exact inverse of :meth:`canonical`: it accepts every string
        :meth:`canonical` can emit and nothing else. Widening it — tolerating
        lower case, or a vendor's separator — would make the parser a second,
        looser definition of identity, and two definitions of identity is one
        too many.

        Raises:
            ValueError: if ``text`` is not exactly one canonical product form.
        """
        venue_code, root = _split_canonical(text, expected_fields=2, label="futures product")
        return cls(venue=Venue(code=venue_code), root=root)


@final
class FuturesContractId(_CanonicalModel):
    """The canonical identity of one specific listed futures contract.

    Identity is exactly three things — the product (which carries the venue),
    the delivery month, and the **full** delivery year::

        FuturesContractId(
            product=FuturesProduct(venue=Venue(code="CME"), root="ES"),
            contract_month=ContractMonth.JUNE,
            contract_year=2026,
        )                                            # canonical: CME:ES:M2026

    Equality and hashing cover those three components and nothing else, so an
    identity is stable for the life of the contract: no specification change, no
    vendor re-mapping, and no provenance detail can make a contract stop being
    equal to itself.

    Why the year is always full
    ---------------------------
    ``ESM6`` is an abbreviation, and no rule can expand it without a decade.
    Every rule that appears to work — "the current decade", "the nearest
    expiry", "the decade of the data" — either reads the wall clock, which makes
    an identity depend on when it was computed, or guesses from data, which
    makes it depend on which rows happened to be loaded. Both produce an
    identity that is not reproducible, which is the one thing this platform
    exists to prevent. So the full year is a required component and the
    ambiguity is pushed out to an explicit, testable boundary; see
    :func:`resolve_abbreviated_contract_year`.

    Not ordered, on purpose
    -----------------------
    No comparison operators are defined. Ordering listed contracts is
    meaningful only *within a product* and only *chronologically by delivery
    period*, which is a fact about delivery, not about identity — and a
    lexicographic order over the canonical string is not that order. A caller
    needing a deterministic sequence sorts by :meth:`canonical`; a caller
    needing roll order builds ``(contract_year, contract_month.month_number)``
    for contracts sharing one product. Defining ``__lt__`` here would let the
    wrong one of those be reached for silently.
    """

    product: FuturesProduct
    contract_month: ContractMonth
    contract_year: int

    @field_validator("contract_year")
    @classmethod
    def _validate_contract_year(cls, value: int) -> int:
        # Strict mode already rejects bool, float, and numeric strings for an
        # int field; the range is what still needs stating.
        if not MIN_CONTRACT_YEAR <= value <= MAX_CONTRACT_YEAR:
            raise ValueError(
                f"contract year must be a full {CONTRACT_YEAR_DIGITS}-digit year between "
                f"{MIN_CONTRACT_YEAR} and {MAX_CONTRACT_YEAR}; got {value!r}. An "
                f"abbreviated year is not a contract year — resolve it explicitly with "
                f"resolve_abbreviated_contract_year"
            )
        return value

    def canonical(self) -> str:
        """Return the deterministic canonical form, ``VENUE:ROOT:<CODE><YYYY>``.

        The form is fixed-width in its delivery field, uses no locale-sensitive
        formatting, and reads no ambient state, so the same identity produces
        the same string in every process, on every machine, under every locale
        and hash seed. It is the representation to persist, to log, and to use
        as a manifest key.

        It also cannot be confused with a vendor symbol: no vendor writes
        ``CME:ES:M2026``, so a canonical identity is never mistaken for an alias
        and an alias never parses as a canonical identity.
        """
        return (
            f"{self.product.canonical()}{CANONICAL_SEPARATOR}"
            f"{self.contract_month.code}{self.contract_year:0{CONTRACT_YEAR_DIGITS}d}"
        )

    @classmethod
    def parse(cls, text: str) -> FuturesContractId:
        """Return the contract described by a canonical identity string.

        The exact inverse of :meth:`canonical`. It accepts no vendor spelling,
        no abbreviated year, no lower case, and no surrounding whitespace — a
        string that did not come out of :meth:`canonical` is rejected.

        Raises:
            ValueError: if ``text`` is not exactly one canonical contract form.
        """
        venue_code, root, delivery = _split_canonical(
            text, expected_fields=3, label="futures contract"
        )
        match = _DELIVERY_PATTERN.fullmatch(delivery)
        if match is None:
            raise ValueError(
                f"futures contract delivery field must be one month code followed by a "
                f"{CONTRACT_YEAR_DIGITS}-digit year, for example {ContractMonth.JUNE.code}2026; "
                f"got {delivery!r}"
            )
        return cls(
            product=FuturesProduct(venue=Venue(code=venue_code), root=root),
            contract_month=ContractMonth.from_code(match.group(1)),
            contract_year=int(match.group(2)),
        )


def _split_canonical(text: str, *, expected_fields: int, label: str) -> list[str]:
    """Split a canonical string into exactly ``expected_fields`` parts.

    Field contents are validated by the types that own them; this only enforces
    the shape, so there is one place that knows how many fields a canonical form
    has and one message when the count is wrong.
    """
    if not isinstance(text, str):
        raise ValueError(f"canonical {label} identity must be a string; got {text!r}")
    fields = text.split(CANONICAL_SEPARATOR)
    if len(fields) != expected_fields:
        raise ValueError(
            f"canonical {label} identity must have exactly {expected_fields} fields "
            f"separated by {CANONICAL_SEPARATOR!r}; got {text!r}"
        )
    return fields


def require_listed_contract(value: object) -> FuturesContractId:
    """Return ``value`` if it identifies an executable listed futures contract.

    This is the guard an execution engine, an order router, or a position
    keeper calls before acting on an instrument. It exists because the most
    expensive mistake this model can prevent is **filling an order in a series
    that never traded**: a continuous or back-adjusted series is a research
    construct assembled after the fact, and a backtest that executes in one
    reports fills at prices no participant could have received.

    The guard is deliberately a type check rather than a flag check. A flag can
    be set wrongly; a type cannot be forged. A future continuous-series identity
    must therefore be its **own type**, and it will fail here without this
    function needing to know it exists.

    The check is ``type(value) is`` rather than ``isinstance``, which is exact
    rather than pedantic: :class:`FuturesContractId` is ``@final``, so it has no
    legitimate subtype and Liskov substitution has nothing to preserve.
    ``@final`` is enforced by the type checker only, so ``isinstance`` would let
    a runtime subclass — the obvious shape for a hastily added continuous
    series — walk straight through the one guard that exists to stop it.
    Verified: before this was tightened, a subclass declared at runtime passed.

    Rejected today, and tested as such: a :class:`FuturesProduct` (``ES`` is not
    a contract), a :class:`Venue`, a bare vendor string such as ``"ESM6"`` or
    ``"ES1!"``, ``None``, a runtime subclass, and any object that merely looks
    like a contract.

    Raises:
        TypeError: if ``value`` is not exactly a :class:`FuturesContractId`.
    """
    if type(value) is not FuturesContractId:
        raise TypeError(
            f"an executable listed futures contract is required, but got "
            f"{type(value).__name__}: {value!r}. A product root, a synthetic or "
            f"continuous series, and a vendor symbol are not executable instruments"
        )
    return value


def resolve_abbreviated_contract_year(code: str, *, cycle_start: int) -> int:
    """Return the full contract year an abbreviated year ``code`` denotes.

    Vendor symbols abbreviate the delivery year: ``ESM6`` carries one digit and
    ``ESM26`` carries two. Neither is a year. ``6`` denotes 2006, 2016, 2026,
    and 2036 equally, and choosing between them requires information the symbol
    does not contain.

    This function makes that information a **required argument**. ``cycle_start``
    is the first year of the repeating window the code indexes into: the decade
    for a one-digit code, the century for a two-digit one. There is no default,
    so a caller cannot obtain a year without stating which window it came from,
    and no call site can quietly acquire a dependency on the current date. That
    is the whole point of the signature:

        >>> resolve_abbreviated_contract_year("6", cycle_start=2020)
        2026
        >>> resolve_abbreviated_contract_year("6", cycle_start=2010)
        2016

    Both are correct. The symbol does not decide between them; the caller's
    declared context does, and the declaration is recorded wherever the caller
    records its configuration.

    Args:
        code: One or two ASCII digits. Other widths are rejected rather than
            guessed: this project has evidence for one- and two-digit vendor
            conventions and none for any other, and inventing a rule for a
            three-digit code would be fabricating a convention.
        cycle_start: First year of the window ``code`` indexes. Must be an exact
            multiple of the window size (10 for one digit, 100 for two), because
            a window starting at 2015 would make ``"6"`` mean 2021 under one
            reading and 2016 under another.

    Returns:
        The full contract year, always within
        :data:`MIN_CONTRACT_YEAR`..:data:`MAX_CONTRACT_YEAR`.

    Raises:
        ValueError: if ``code`` is not one or two ASCII digits, if
            ``cycle_start`` is not an ``int`` (``bool`` is rejected explicitly),
            is not a multiple of the window size, or names a window that would
            extend outside the representable contract-year range.
    """
    if not isinstance(code, str):
        raise ValueError(f"abbreviated contract year code must be a string; got {code!r}")
    if _ABBREVIATED_YEAR_PATTERN.fullmatch(code) is None:
        # Not `str.isdigit`: that accepts non-ASCII decimal digits such as "٦",
        # which `int` then happily converts, so a look-alike symbol would
        # resolve to a real year.
        raise ValueError(
            f"abbreviated contract year code must be one or two ASCII digits; got {code!r}"
        )

    cycle = 10 ** len(code)
    if isinstance(cycle_start, bool) or not isinstance(cycle_start, int):
        raise ValueError(f"cycle_start must be an int; got {cycle_start!r}")
    if cycle_start % cycle != 0:
        raise ValueError(
            f"cycle_start must be a multiple of {cycle} for a {len(code)}-digit code, so that "
            f"the code indexes an unambiguous window; got {cycle_start!r}"
        )
    if cycle_start < MIN_CONTRACT_YEAR or cycle_start + cycle - 1 > MAX_CONTRACT_YEAR:
        raise ValueError(
            f"cycle_start {cycle_start!r} names a window "
            f"{cycle_start}..{cycle_start + cycle - 1} outside the representable contract-year "
            f"range {MIN_CONTRACT_YEAR}..{MAX_CONTRACT_YEAR}"
        )
    return cycle_start + int(code)
