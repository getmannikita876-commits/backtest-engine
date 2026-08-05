# ADR-002: Bar timestamps carry information-availability time

- Status: **Accepted** — import layer (Phase 1.4) and domain/storage contract (Phase 1.5)
- Date: 2026-08-05
- Revised: 2026-08-05 (Phase 1.5 — the proposed contract change was accepted, with one revision)
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

Negative — these were the reason the domain change was proposed, and Phase 1.5
resolved the first two:

- ~~**Interval start is no longer directly readable from a `Bar`.**~~ Resolved:
  `Bar.interval_start` is now a stored field.
- ~~**A `Bar` does not record its own duration.**~~ Resolved: `Bar.interval` is
  now a stored field.
- **The stored timestamp differs from the vendor's**, which will surprise
  anyone reconciling imported data against a raw export. Still true, but now
  self-explaining: the bar carries the interval, so the vendor's original
  interval-start value is readable directly rather than needing to be derived.

## Phase 1.5 revision — domain contract accepted

The proposed contract change is **accepted**, with one revision that
strengthens it.

### What was proposed

```
Bar:
    interval_start: datetime
    interval: timedelta
    timestamp: datetime        # availability; == interval_start + interval
```

### What was adopted, and why it differs

Storing `timestamp` as a *field* would have left the invariant
`timestamp == interval_start + interval` enforceable only by validation — and a
validated invariant is one a future refactor can weaken. The requirement was
that look-ahead be impossible *by construction*.

`availability_time` is therefore a **derived property**, not a stored field:

```
Bar:
    interval_start: datetime    # stored, UTC-validated
    interval: timedelta         # stored, strictly positive
    interval_end     -> property: interval_start + interval
    availability_time -> property: interval_end
    timestamp         -> property: availability_time   (alias)
```

There is now no field in which a wrong availability could be placed. A bar that
becomes visible before its interval closes is not invalid — it is
unconstructible. `_BaseDomainModel` sets `extra="forbid"`, so a caller still
passing `timestamp=` raises rather than having the argument silently ignored.

`timestamp` remains as an alias so every market event — trade, quote, bar —
answers one name with "when this became knowable", and replay ordering reads it
uniformly.

### Storage

`SCHEMA_VERSION` moves from 1 to 2. The bar schema gains
`interval_microseconds` (`uint64`); `timestamp` continues to hold availability
time. Interval start is **not** stored, because it is exactly
`timestamp - interval` and persisting both would let the two disagree.

Intervals are encoded as whole microseconds. `timedelta` is already
microsecond-resolution, so the encoding is exact and needs no rounding rule —
the same "no silent precision loss" principle the fixed-point price rules
follow.

### Migration impact

Version-1 data is **rejected, not migrated**. `validate_storage_schema` accepts
only an exact version match, so a version-1 file fails loudly.

This is deliberate. A version-1 bar records no interval, so its single
timestamp cannot be resolved into interval start and availability without
knowing the bar's duration — and that duration is not recoverable from the row.
Any automatic migration would have to guess it, which would silently
reintroduce the exact bias this ADR exists to prevent.

No production data exists at version 1; the storage layer has never been used
outside tests. Should version-1 data need recovery, the operator must supply
the interval explicitly, which is a deliberate act with a recorded value rather
than an inference.

### Consequences of the revision

The negative consequences listed above are resolved: interval start and
duration are both first-class, and bars of different intervals are
distinguishable. The remaining limitation is unchanged and unresolved: the
nominal session close is still calendar-unaware.
