# ADR-003: Trade identity and duplicate semantics

- Status: **Proposed** — the interim policy below is implemented; the contract change is not
- Date: 2026-08-05
- Related: `docs/data-import.md`, `docs/data-contracts.md`, ADR-002, ADR-005

## Context

Duplicate detection identified a record by the tuple of its record type plus
every required field. For trades that meant
`(TRADE, timestamp, instrument_symbol, price, size, side)`.

An independent audit demonstrated that this destroys data. Two genuinely
distinct executions can agree on all six attributes — one-lot fills at the same
price inside the same microsecond are ordinary in real tick data, not an edge
case. The default `REJECT` policy discarded the second, and because a duplicate
is reported at `WARNING` severity, the batch still returned `success=True`:

```
rows submitted : 2  (two real executions)
rows accepted  : 1
total volume   : 1 (should be 2)
success flag   : True
```

The consequence is systematically understated volume, biasing every
volume-derived statistic — VWAP, volume profile, participation rate, order-flow
imbalance — invisibly and in a data-dependent way.

The root cause is not the policy. It is that **`Trade` has no identity**. The
domain carries no venue, no exchange sequence number, and no vendor trade
identifier, so there is no field that could distinguish co-incident executions.
Attribute equality was standing in for an identity that does not exist.

Trades are not the only record type, and the same reasoning does not apply to
the others:

- A **quote** is a *state observation*. Two identical top-of-book snapshots for
  one instant assert the same fact; collapsing them loses no information.
- A **bar** is a *summary keyed by its period*. Two identical bars covering one
  interval are the same bar, and retaining one preserves its volume exactly.

## Decision — interim policy (implemented)

**Exact attribute-based duplicate removal no longer applies to trades.**

`record_identity` returns `None` for `ImportRecordType.TRADE`, which means the
duplicate validator reports nothing for trades and no `DuplicatePolicy` — not
`REJECT`, `KEEP_FIRST`, or `KEEP_LAST` — can discard one. The record types
without attribute-based identity are declared as data in
`UNIDENTIFIABLE_RECORD_TYPES` rather than special-cased at each call site.

Quote and bar duplicate handling is **unchanged**, on the grounds above.

Consequences accepted for now:

- A genuinely repeated trade — the same execution delivered twice by a vendor —
  is imported twice. **Over-counting is preferred to silent deletion**: a
  duplicated execution is visible in the data and correctable downstream, while
  a deleted one is unrecoverable and undetectable.
- Trade de-duplication becomes the operator's responsibility until an identity
  contract exists.

This is deliberately the conservative direction. It trades a detectable error
for an undetectable one.

## Proposed contract change (not implemented)

Give `Trade` an explicit identity so duplicate detection can be restored on a
sound basis. The smallest sufficient change:

```
Trade:
    venue: str                    # the reporting venue or exchange
    vendor_trade_id: str | None   # the vendor's identifier, when published
    vendor_sequence: int | None   # the venue's sequence number, when published
```

Duplicate identity would then be `(venue, vendor_trade_id)` when an identifier
exists, or `(venue, vendor_sequence)` when a sequence number does, and would
remain **disabled** when neither is available — because in that case the vendor
genuinely has not told us whether two executions are the same one.

This requires:

- a domain-model change in `quant_research_terminal.domain.trade`;
- a storage schema revision and a `SCHEMA_VERSION` bump for the new columns;
- import-contract changes so providers can carry the fields;
- a decision per vendor about which identifier is actually published — neither
  the Databento nor the ThetaData decoder is verified against real output, so
  what those vendors supply is currently an assumption rather than a fact.

Because it touches the audited domain model and the storage schema, it must be
accepted as its own decision. This ADR records the proposal.

## Alternatives considered

**Keep attribute identity, downgrade the policy default to keep-all.** Rejected:
the hazard would remain one configuration change away, and `REJECT` would still
read as a safe-sounding default that deletes real data.

**Report duplicates as errors so the batch fails.** Rejected: it converts a
routine, legitimate data pattern into a hard failure, and would make normal tick
data unimportable.

**Synthesise a surrogate identity from row position.** Rejected: a source
position is not an identity. It would make every record trivially unique,
disabling duplicate detection while appearing to perform it — worse than
disabling it honestly.

**Infer identity from a vendor sequence number where one exists.** Deferred
rather than rejected: this is the proposed direction above, but it cannot be
implemented against vendor fields that have not been verified to exist.

## Consequences

Positive:

- Genuine executions can no longer be deleted by any configuration.
- The absence of trade identity is now explicit in the code and documented,
  rather than being papered over by a heuristic.
- Quotes and bars retain useful de-duplication on stated, type-specific grounds.

Negative:

- Repeated trades from a defective source are imported twice, undetected.
- The import layer's duplicate handling is now asymmetric across record types,
  which is more to explain — the asymmetry is real, but it is a cost.

## Addendum (2026-08-06)

The bar reasoning above — "identity is the record type plus every required
field" — was subsequently found to be defective for bars: folding OHLCV into
the identity made two *conflicting* records for one period look distinct, so a
self-contradictory source passed silently. ADR-005 narrows bar identity to the
period `(instrument_symbol, interval_start, interval)` and separates exact
duplicates from conflicts. The statement that "two identical bars covering one
interval are the same bar" remains true and its handling unchanged.

Everything this ADR decides about **trades** stands unchanged: trades have no
identity, are never deduplicated, and the proposed identity contract remains
unimplemented. Quote identity also remains as described here.
