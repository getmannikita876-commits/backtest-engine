# ADR-004: One numeric envelope shared by domain, import, and storage

- Status: **Accepted**
- Date: 2026-08-06
- Related: `docs/data-contracts.md`, `docs/data-import.md`, ADR-002, ADR-003

## Context

An independent audit found that the domain accepted values storage could not
encode. `Trade(price=Decimal("1E+400"))` and `Trade(price=Decimal("0.0000001"))`
both constructed successfully and failed only when storage's encoding step was
reached.

Auditing the repository directly surfaced **six** mismatches, not two:

| Value | Domain | Storage | Nature |
| --- | --- | --- | --- |
| `Decimal("1E+400")` | accept | reject | magnitude, **misreported as "at most 6 fractional digits"** |
| `Decimal("0.0000001")` | accept | reject | precision |
| `Decimal("5000.250000000")` | accept | reject | **false rejection** |
| `Decimal("1000000000000")` | accept | reject | fixed-point overflow, raised `OverflowError` not `ValueError` |
| `size=Decimal("0.5")` | accept | reject | fractional quantity |
| `size=Decimal(2**64)` | accept | reject | uint64 overflow |

The third was the most damaging and was not in the audit. The precision check
trapped `Rounded`, which the decimal module signals whenever *any* digit is
discarded — including trailing zeros that carry no information. Every price the
Databento decoder produces arrives at scale nine (`Decimal(raw).scaleb(-9)`), so
**every Databento-decoded price was unstorable**:

```
5000.250000000   Inexact=no      Rounded=TRAPS   <- rejected, but loses nothing
5000.2500001     Inexact=TRAPS   Rounded=TRAPS   <- correctly rejected
```

The misleading message had a related cause: `quantize` raises
`InvalidOperation` when the result needs more digits than the context allows, so
a huge value tripped the same handler as a too-precise one.

**Underlying cause: three modules each defined "a usable number", and they
disagreed.**

| Module | Rule |
| --- | --- |
| `domain/common.py` | positive only; any scale, any magnitude; accepts `str` |
| `data_import/numeric_semantics.py` | finite; no scale, no magnitude; rejects `str`; docstring claimed to be *"the authoritative definition"* |
| `data/conversion.py` | finite, **non-negative (allowed zero)**, ≤6 digits by `Rounded`, magnitude-bounded, quantities whole |

Nothing forced them to agree, and the constants they needed
(`PRICE_SCALE`, `PRICE_PRECISION`, `UINT64_MAX`, …) lived in `data/contracts.py`,
which the domain **may not import** — the architecture tests forbid
`domain → data`.

## Decision

There is **one envelope**, defined in `quant_research_terminal.domain.numeric`.
Domain models enforce it on construction, import validation consults it when
judging a raw record, and storage conversion applies it defensively. No other
module defines a numeric rule or restates a bound.

It lives in the domain package because the domain is the foundation and may not
depend on storage, while every layer above may depend on the domain. It imports
nothing from this project, so placing it there inverts no dependency.
`data/contracts.py` now re-exports the constants rather than declaring them.

### Two envelopes, not one

| | Price | Quantity (size, volume) |
| --- | --- | --- |
| Kind | Fractional decimal | Count |
| Minimum | `0.000001` | `1` |
| Maximum | `999999999999.999999` | `18446744073709551615` |
| Scale | maximum exact fractional precision: 6 decimal places | whole numbers only |
| Finiteness | required | required |
| Positivity | strictly positive | strictly positive |
| Storage | signed `int64`, `value * 10**6` | `uint64` |

Collapsing them would either strip prices of their fractional scale or admit
fractional contract counts that cannot be persisted. Rejecting fractional
quantities is a futures-first decision: contracts are whole. Fractional units
would need a storage schema change, not a looser validator.

### Maximum exact fractional precision: 6 decimal places

The rule is about information, not raw digit count. A value wider than six
decimal places is accepted when every digit beyond the sixth is a trailing
zero, because removing those loses nothing:

- `5000.250000000` — **accepted**; represents exactly as `5000.250000`.
- `5000.2500001` — **rejected**; would require discarding a non-zero digit.

Implemented by trapping `Inexact` and **not** `Rounded`. That is the whole fix
for the Databento regression.

### Check order

Magnitude is tested **before** precision, so an enormous value is reported as
out of range rather than as having too many decimal places.

### No rounding, ever

A value outside the envelope is **rejected**, never adjusted. Nothing
quantizes, truncates, clamps, or coerces. Silently altering a price is a
data-integrity failure that would propagate into every downstream result.

### Validation lives in the domain models

Enforcing only at the storage boundary would leave the original defect intact —
research could run to completion and discover the problem at save time.
Construction is therefore the point of enforcement, which makes the invariant
structural:

> Every constructible domain object is storage-encodable: its numeric fields
> encode exactly into the storage schema — as fixed-point integers and unsigned
> integers — without rounding, truncation, overflow, or coercion through float.

This is a claim about **numeric encoding**, proven by the round-trip tests in
`tests/test_numeric_envelope.py` and exercised through real files by
`tests/test_parquet_store.py`.

Storage keeps its own checks as defence in depth. It does not define a different
envelope; it calls the same one, so a violation reaching it means a layer was
bypassed.

## Schema version: **no bump**

Schema version stays at **2**.

The serialized representation is unchanged: same Arrow and Polars column names
and types, same fixed-point encoding, same metadata values. The envelope
narrows what the *domain* accepts; it does not change what storage writes or how
a written file is read.

Every file previously written under version 2 remains valid and readable,
because storage never wrote a value outside the envelope — it rejected them.
Bumping would signal an incompatibility that does not exist. Recorded as a
**clarification of version 2** in `docs/data-contracts.md`.

## Compatibility impact

This narrows the domain, so code that previously constructed now-invalid values
starts failing — correctly, and at construction rather than at save time:

- **Fractional quantities are refused.** `size=Decimal("0.5")` and a sub-integer
  `volume` no longer construct.
- **Sub-tick prices are refused.** A value requiring removal of a non-zero
  fractional digit beyond the sixth decimal place is a construction error.
- **Enormous values are refused**, with a magnitude message rather than a
  precision one.
- **Bar volume reports quantity issue codes** (`negative_value`,
  `non_integer_quantity`) rather than `non_decimal_price`, which misdescribed a
  count as a price.
- **Storage raises `NumericEnvelopeError`** — a `ValueError` carrying a
  `violation` attribute — rather than a bare `ValueError`/`OverflowError` pair.
  Callers matching on `OverflowError` must change; the replacement is
  machine-readable, which the previous split was not.

In the other direction it **widens** one case that was wrongly rejected: prices
carrying only trailing zeros beyond the sixth decimal place now encode
successfully, which restores the Databento provider to working order.

## Alternatives considered

**Widen storage to match the domain.** Rejected: the domain accepted unbounded
magnitude and arbitrary scale, which no fixed-point encoding can hold.

**Enforce only at the storage boundary.** Rejected: it leaves the original
defect — research runs to completion, then cannot be saved.

**One envelope for both prices and quantities.** Rejected: it would either force
prices to be whole or let quantities be fractional. Both are wrong.

**Round values to fit.** Rejected outright. Silent alteration of market data is
the failure mode this project exists to prevent.

## Consequences

Positive:

- Domain-valid and storage-encodable are the same set, enforced structurally.
- Databento prices encode exactly again.
- Diagnostics distinguish magnitude, precision, wholeness, finiteness, and
  positivity, each with a stable machine-readable reason.
- Three numeric definitions became one, and a test asserts no module redefines
  the constants.

Negative:

- The domain is stricter, so previously-constructible values now fail. Intended,
  but a visible behaviour change.
- Fractional quantities are unrepresentable, which will need revisiting for any
  asset class with fractional units — a storage schema change with its own
  decision.
- Signed values — PnL, deltas — have no envelope yet. Out of scope here; they
  will need one before a portfolio layer exists.
