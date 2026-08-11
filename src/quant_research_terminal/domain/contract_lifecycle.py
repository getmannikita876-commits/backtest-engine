"""A listed contract's lifecycle facts, supplied rather than inferred.

A futures contract stops trading on a date the exchange publishes. Nothing in
this repository can compute that date, and every rule that looks like it could
— "the third Friday", "the business day before the 25th", "eight days before
delivery" — is a product-specific convention that has changed over time and
differs between products. Guessing one would silently shift every roll built
on it, so this module contains **no expiry formula, no contract-month
arithmetic, and no wall clock**. The fact is a required argument.

What this module deliberately is *not*
--------------------------------------
**Not an instrument specification.** Tick size, multiplier, contract size,
currency, settlement method, first notice date, and delivery mechanics are not
here. Phase 2.2 needs exactly one lifecycle fact — the last trade date — and a
speculative specification subsystem would be fake completeness (ADR-009 made
the same call about identity).

**Not proof of a final tradable instant.** :attr:`ContractLifecycle.last_trade_date`
is a :class:`~quant_research_terminal.domain.exchange_calendar.TradingDate`, not
an instant. A contract's termination on its last trading date may follow
product-specific rules — a morning fixing, a special settlement procedure, an
early halt — that the exchange calendar's ordinary window end does not express.
So this module never derives a "last trade instant" from the calendar, and no
consumer may treat the last trade date as evidence that the contract traded
through that date's final calendar window. Establishing an exact termination
instant requires a separately source-backed lifecycle boundary, which Phase 2.2
does not model.

The one thing the lifecycle fact *is* used for is as the explicit anchor a
fixed-calendar roll counts backwards from.
"""

from __future__ import annotations

from typing import final

from pydantic import field_validator

from quant_research_terminal.domain.exchange_calendar import TradingDate
from quant_research_terminal.domain.futures_contract import (
    FuturesContractId,
    _CanonicalModel,
    require_listed_contract,
)


def _require_exact_trading_date(value: object) -> TradingDate:
    """Return ``value`` if it is exactly a :class:`TradingDate`.

    Pydantic's strict mode admits *subclass instances* for a field annotated
    with a model type, and ``@final`` is enforced by the type checker only. A
    runtime subclass would therefore be accepted here and only fail much later,
    inside ``CalendarResolver.windows_for``, whose own exact-type check would
    then raise from deep inside materialization. Rejecting at construction is
    the same defence ADR-009's defect D4 added to the executable guard.
    """
    if type(value) is not TradingDate:
        raise ValueError(
            f"a TradingDate is required, exactly; got {type(value).__name__}: {value!r}"
        )
    return value


@final
class LifecycleProvenance(_CanonicalModel):
    """Where a lifecycle fact came from.

    Deliberately its own type, and deliberately minimal. It is **not**
    :class:`~quant_research_terminal.domain.exchange_calendar.RuleProvenance`:
    a contract's last trade date is an *instrument lifecycle fact*, not an
    exchange-calendar rule, and coupling the two because their fields happen to
    fit would make one type answer two questions. It is also not the
    roll-derivation provenance in
    :mod:`quant_research_terminal.domain.rollover`, which describes how a roll
    *decision* was reached rather than where a *fact* was published.

    Only the field Phase 2.2 actually requires is present: a reference to the
    evidence establishing the fact. No evidence-strength grading exists here
    yet, because nothing in this phase consumes one and the repository ships no
    production lifecycle data — every fact is caller-supplied. Adding a
    strength field later is additive.
    """

    evidence_ids: tuple[str, ...]

    @field_validator("evidence_ids")
    @classmethod
    def _validate_evidence_ids(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if not value:
            raise ValueError("lifecycle provenance must reference at least one evidence record")
        for item in value:
            if not isinstance(item, str) or not item:
                raise ValueError(f"evidence id must be a non-empty string; got {item!r}")
        return value


@final
class ContractLifecycle(_CanonicalModel):
    """The lifecycle facts of one listed contract, as supplied by the operator.

    ``contract`` carries the product, the delivery month, and the full delivery
    year already (ADR-009), so none of those are repeated here. The only fact
    added is the one a fixed-calendar roll needs::

        ContractLifecycle(
            contract=FuturesContractId(...),      # CME:ES:H2026
            last_trade_date=TradingDate(...),     # supplied, never computed
            provenance=LifecycleProvenance(evidence_ids=("...",)),
        )

    ``last_trade_date`` is a :class:`TradingDate`, which means it must have been
    assigned by a calendar rather than derived from a timestamp — the type
    rejects ``datetime`` outright.
    """

    contract: FuturesContractId
    last_trade_date: TradingDate
    provenance: LifecycleProvenance

    @field_validator("contract")
    @classmethod
    def _validate_contract(cls, value: FuturesContractId) -> FuturesContractId:
        # Exact-type check, for the ADR-009 D4 reason: a runtime subclass would
        # otherwise ride through materialization and only fail at the
        # executable guard, possibly after research had been produced.
        #
        # The guard raises TypeError, which Pydantic does not convert into a
        # ValidationError, so it is re-raised as ValueError. Otherwise one
        # invalid field would surface as TypeError here and ValidationError
        # everywhere else, and a caller could not catch both with one except.
        try:
            return require_listed_contract(value)
        except TypeError as error:
            raise ValueError(f"a lifecycle names a listed contract: {error}") from error

    @field_validator("last_trade_date")
    @classmethod
    def _validate_last_trade_date(cls, value: TradingDate) -> TradingDate:
        return _require_exact_trading_date(value)

    def canonical(self) -> str:
        """Return a deterministic canonical form, ``<contract>@<date>``.

        Used for diagnostics and for the deterministic ordering of lifecycle
        collections; it is not an identity that anything persists.
        """
        return f"{self.contract.canonical()}@{self.last_trade_date.canonical()}"
