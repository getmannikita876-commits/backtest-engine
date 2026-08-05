# ADR-004: One numeric envelope shared by domain, import, and storage

- Status: **Accepted**
- Date: 2026-08-05
- Related: `docs/data-contracts.md`, `docs/data-import.md`, ADR-002, ADR-003

## Context

An independent audit found that the domain accepted values storage could not
encode. `Trade(price=Decimal("1E+400"))` and `Trade(price=Decimal("0.0000001"))`
both constructed successfully and both failed only when storage's numeric
encoding step was reached.

Investigating it surfaced four distinct mismatches, not one:

| # | Domain accepted | Storage rejected |
| --- | --- | --- |
| 1 | Any magnitude | Fixed-point overflow past `10**18 - 1` |
| 2 | Any scale | More than 6 fractional digits |
| 3 | Fractional quantities (`size=0.5`) | Quantities must be whole |
| 4 | `Decimal("5000.250000000")` | **Rejected — trailing zeros** |

The fourth was not in the audit and is the most damaging. The precision check
trapped `Rounded`, which the decimal module signals whenever *any* digit is
discarded — including trailing zeros that carry no information. Every price the
Databento decoder produces arrives at scale 9 (`Decimal(raw).scaleb(-9)`), so
**every Databento-decoded price failed the storage encoding step**:

```
decoded: Decimal('5000.250000000')
storage REJECT: price must have at most 6 fractional digits
```

The error message was also misleading in the opposite direction: a magnitude
overflow reported "must have at most 6 fractional digits" for a value with none,
because `quantize` raises `InvalidOperation` on a huge value and that was caught
as a precision failure.

Underlying cause: **there were three numeric definitions.** `domain/common.py`
said "positive". `data_import/numeric_semantics.py` said "finite and positive".
`data/conversion.py` said "positive, scale ≤ 6, magnitude bounded, quantities
whole". Nothing forced them to agree, and they did not.

## Decision

There is **one envelope**, defined in `quant_research_terminal.domain.numeric`.
Domain models enforce it on construction, import validation consults it when
judging a raw record, and storage conversion applies it defensively. No layer
defines a second one.

The module lives in the domain package because the domain is the foundation and
may not depend on storage. It imports nothing from this project, so every layer
above can use it without inverting the dependency direction.

### Two envelopes, not one

Prices and quantities are different kinds of number:

| | Price | Quantity (size, volume) |
| --- | --- | --- |
| Kind | Fractional decimal | Count |
| Scale | 6 decimal places | Whole numbers only |
| Minimum | `0.000001` | `1` |
| Maximum | `999999999999.999999` | `18446744073709551615` |
| Storage | signed fixed-point `int64`, `value * 10**6` | `uint64` |
| Positivity | strictly positive | strictly positive |
| Finiteness | required | required |

Collapsing them into one envelope would either strip prices of their fractional
scale or admit fractional contract counts that cannot be persisted. The
asymmetry is real, so it is modelled rather than hidden.

Rejecting fractional quantities is a **futures-first decision**: contracts are
whole. Admitting fractional shares would require a storage schema change, not
merely a looser validator.

### Maximum exact fractional precision: 6 decimal places

The rule is about information, not raw digit count. A value is accepted at
scale 6 when nothing but trailing zeros would be discarded to represent it
there:

- `5000.250000000` — **accepted**. Nine decimal places, but every digit past
  the sixth is a trailing zero; removing them loses nothing.
- `5000.2500001` — **rejected**. Representing it at 6 decimal places would
  require discarding a non-zero fractional digit.

Implemented by trapping `Inexact` only. Trapping `Rounded` — the previous
behaviour — rejects values whose only fractional digits beyond the sixth place
are zeros, which is what broke every Databento price.

### Check order

Magnitude is tested **before** precision. An enormous value is reported as out
of range rather than as having too many decimal places, which is how a
quantize-based check would misdescribe it.

### No rounding, ever

A value outside the envelope is **rejected**, never adjusted. Nothing quantizes,
truncates, clamps, or coerces. Silently altering a price is a data-integrity
failure that propagates into every downstream result, so the only safe response
to an unrepresentable value is to refuse it.

### Validation lives in the domain models

Enforcing at the storage boundary alone would leave the original defect intact —
research could still run to completion on values that fail storage's numeric
encoding and discover it only when a save was attempted. Constructing a domain
object is therefore the point of enforcement, which makes the invariant
structural:

> Every domain object accepted by the numeric envelope has numeric fields that
> can be encoded exactly by storage schema v2 — as fixed-point integers and
> unsigned integers — without rounding, truncation, overflow, or coercion
> through float.

This is a claim about **numeric encoding**, proven by the round-trip tests in
`tests/test_numeric_envelope.py`. It is deliberately not a claim that a `Trade`,
`Quote`, or `Bar` object — or a file of them — has been or can be persisted end
to end: no Arrow or Parquet IO exists in this phase, so full object and file
persistability remain unverified and out of scope for this ADR.

Storage keeps its own checks as defence in depth. It does not define a
different envelope; it calls the same one, so a violation reaching it means a
layer was bypassed.

## Schema version: **no bump**

Schema version stays at **2**.

The serialized representation is unchanged: same Arrow and Polars column names
and types, same fixed-point encoding, same metadata values. The envelope
narrows what the *domain* accepts; it does not change what storage writes or
how a written file is read.

Every file previously written under version 2 remains valid and readable,
because storage never wrote a value outside the envelope — it rejected them.
Bumping the version would signal an incompatibility that does not exist.

This is recorded as a **clarification of version 2**, documented in
`docs/data-contracts.md`.

## Compatibility impact

This narrows the domain, so code that previously constructed now-invalid values
will start failing — correctly, and at the point of construction rather than at
save time:

- **Fractional quantities are refused.** `size=Decimal("0.5")` and a
  sub-integer `volume` no longer construct.
- **Sub-tick prices are refused.** A value requiring removal of a non-zero
  fractional digit beyond the sixth decimal place is a construction error;
  trailing zeros beyond the sixth place are accepted.
- **Enormous values are refused**, with a magnitude message rather than a
  precision one.
- **Bar volume now reports quantity issue codes** (`negative_value`,
  `non_integer_quantity`) rather than `non_decimal_price`, which misdescribed a
  count as a price.
- **Storage raises `NumericEnvelopeError`** (a `ValueError` carrying a
  `violation` attribute) rather than a bare `ValueError`/`OverflowError` pair.
  Callers matching on `OverflowError` must change; the replacement is
  machine-readable, which the previous split was not.

In the other direction it **widens** one case that was wrongly rejected:
prices carrying only trailing zeros beyond the sixth decimal place now encode
successfully, which restores the Databento provider to working order.

## Alternatives considered

**Widen storage to match the domain.** Rejected: the domain accepted unbounded
magnitude and arbitrary scale, which no fixed-point encoding can hold. Matching
it would mean abandoning exact decimal storage.

**Enforce only at the storage boundary.** Rejected: it leaves the original
defect — research runs to completion, then cannot be saved or reproduced.

**One envelope for both prices and quantities.** Rejected: it would either
force prices to be whole or let quantities be fractional. Both are wrong.

**Round values to fit.** Rejected outright. Silent alteration of market data is
the failure mode this project exists to prevent.

## Consequences

Positive:

- Domain-valid and storage-encodable are now the same set, enforced structurally.
- Databento prices encode exactly again.
- Diagnostics distinguish magnitude, precision, wholeness, finiteness, and
  positivity, each with a stable machine-readable reason.
- Three numeric definitions became one.

Negative:

- The domain is stricter, so previously-constructible values now fail. This is
  the intended correction, but it is a visible behaviour change.
- Fractional quantities are unrepresentable, which will need revisiting for any
  asset class with fractional units. That requires a storage schema change and
  its own decision.
- Signed values — PnL, deltas — have no envelope yet. They are out of scope
  here and will need one before a portfolio layer exists.
