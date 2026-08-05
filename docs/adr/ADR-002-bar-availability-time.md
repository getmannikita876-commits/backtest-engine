# ADR-002: Bar timestamps carry information-availability time

- Status: Accepted for the import layer; **Proposed** for the domain contract change in "Future work"
- Date: 2026-08-05
- Supersedes: nothing
- Related: `docs/replay_rules.md`, `docs/data-import.md`, `docs/data-contracts.md`

## Context

A bar summarises a time interval. It has **two** distinct temporal coordinates:

1. **Interval start** — the period the bar describes.
2. **Availability time** — the instant the bar's open, high, low, close, and
   volume are all determined, which is the interval's **close**.

Databento's `ohlcv` records are stamped at interval start. Most vendors do the
same, so this is not a Databento quirk; it is a property of bar data generally.

The domain contract carries a single `Bar.timestamp` field, and the storage
schema persists a single timestamp column. There is nowhere to record both
coordinates.

`Bar.timestamp` is also the value
`data_import.event_order.event_ordering_key` uses to place the bar in the event
stream. Whatever goes in that field decides *when the bar becomes visible*.

This creates a direct conflict with two project rules:

- `CLAUDE.md`: look-ahead bias is a critical defect; future information must
  never leak into historical calculations.
- `docs/replay_rules.md`: "Bars become visible only after they close."

Stamping a bar with its interval start and replaying it at that instant
publishes the closing price before the period has elapsed. A strategy could
read a 09:30 bar's close at 09:30 and trade the following minute on knowledge
of it. That is look-ahead bias of the most damaging kind, because it is
invisible: every timestamp looks correct and every result looks plausible.

## Decision

### Import layer (accepted, implemented)

`Bar.timestamp` carries the **information-availability time** — the interval
close. The Databento provider computes it as `interval_start + interval` and
requires the interval to be supplied explicitly.

Three properties make this safe rather than a mutation of vendor data:

- **It is a decode, not a repair.** `Bar.timestamp` means "when this event
  enters the stream". For a bar, that is interval close. Mapping the vendor's
  interval-start encoding onto it is the same class of translation as mapping
  Databento's `B` onto `buy`.
- **It is exactly invertible.** The original vendor timestamp is always
  `timestamp - interval`. Nothing is destroyed, and the interval is available
  from `provider.bar_interval`.
- **It is never guessed.** The interval is a required constructor argument for
  the OHLCV schema. Omitting it raises at construction. There is no default,
  because a wrong default would silently reintroduce the bias this decision
  exists to prevent.

### Rejected alternatives

- **Emit interval start and let the replay engine correct it.** Rejected: it
  ships a look-ahead landmine and defers the fix to a component that does not
  exist. Anyone importing bars before then gets biased data with no warning.
- **Refuse to import bars until the contract is richer.** Rejected as
  disproportionate. Availability time is computable exactly whenever the
  interval is known, and it is the coordinate replay actually needs.
- **Infer the interval from the vendor's `rtype`.** Rejected for now: it relies
  on vendor constants not verified against real output, and an inference error
  would silently shift every bar.

## Semantics by case

**1-minute bars.** Availability is `interval_start + 60s`. A bar covering
09:30:00–09:31:00 becomes visible at 09:31:00.

**Arbitrary intervals.** Any strictly positive `timedelta` works; the rule is
uniform. Non-standard intervals (30s, 4h) need no special handling.

**Session-boundary bars.** For intervals that align to a trading session —
daily bars especially — `interval_start + interval` is a *nominal* close. The
true session close depends on the exchange calendar: half-days, holidays, and
early closes all shorten the session, so a daily bar's real availability can be
earlier than start + 24h. The import layer has no exchange calendar, and the
charter lists calendars as future work. The nominal close is therefore
**conservative in the right direction** — it is never earlier than the true
close, so it cannot create look-ahead. It may delay a bar's visibility past its
true availability, which costs realism but not correctness. Calendar-aware
refinement is future work.

**Missing interval metadata.** The provider refuses to construct. There is no
safe fallback: without the interval the availability time is unknown, and any
assumed value would either fabricate data or reintroduce the bias.

## Consequences

Positive:

- A completed bar cannot be observed at its interval start. This is enforced by
  construction and covered by regression tests.
- No contract change is required to get the safety property today.
- Replay inherits correct ordering for free, because the ordering key already
  reads `Bar.timestamp`.

Negative — and these are the reason the domain change below is proposed:

- **Interval start is no longer directly readable from a `Bar`.** It is
  recoverable only if the consumer knows the interval, which the `Bar` does not
  carry. Charting or reporting code that wants to label a bar by its opening
  time must obtain the interval separately.
- **A `Bar` does not record its own duration**, so two bars of different
  intervals are indistinguishable once imported.
- **The stored timestamp differs from the vendor's**, which will surprise
  anyone reconciling imported data against a raw export. This is documented in
  `docs/data-import.md`, but documentation is weaker than a contract.

## Future work (proposed, not accepted)

The clean fix is to make both coordinates explicit in the domain contract. The
smallest change that resolves every negative consequence above:

```
Bar:
    interval_start: datetime   # the period the bar describes
    interval: timedelta        # how long that period is
    timestamp:  datetime       # availability time; == interval_start + interval
```

This requires:

- a domain-model change in `quant_research_terminal.domain.bar`;
- a storage schema revision and a `SCHEMA_VERSION` bump, since two columns are
  added (`docs/data-contracts.md` accepts only exactly-matching versions);
- a migration decision for any data already persisted under version 1.

Because that touches an audited Phase 1.1 contract and the Phase 1.2 storage
schema, it must be accepted as its own decision rather than folded into a
provider change. This ADR records the proposal; adopting it is a separate ADR.

Until then, the import-layer decision above stands, and consumers must treat
`Bar.timestamp` as availability time.
