# ADR-011: Futures rollover and continuous mapping

- Status: **Accepted**
- Date: 2026-08-10
- Related: ADR-009 (futures contract identity), ADR-010 (exchange calendar),
  `docs/architecture.md`, `docs/roadmap.md`

## Problem

Research on ES/NQ is conducted over a *continuous series*, but no order was
ever filled in one. Every backtest fill must name a real listed contract, so
the platform needs a deterministic answer to:

> given a research series and a historical UTC instant, **which listed contract
> was active?**

Phase 2.0 gave the platform `FuturesContractId` and a guard,
`require_listed_contract`, that only an exact `FuturesContractId` passes.
ADR-009 stated the shape of the missing piece explicitly: "A future
continuous-series identity must be its **own type** and will fail here without
this function needing to know it exists." Phase 2.1 gave the platform a
calendar that assigns `TradingDate` and materializes half-open UTC windows.
This decision joins them, and does nothing else.

The failure this exists to prevent is specific and silent: a research series
that reports a contract nobody could have traded, or that silently picks one
side of a roll that happened mid-session. Both produce results that look
entirely plausible.

## Decision

### Scope: mapping, not price synthesis

Phase 2.2 implements **rollover mapping only**. It does not implement
additive or ratio back-adjustment, Panama, forward adjustment,
return-preserving adjustment, synthetic OHLC, or roll-gap smoothing — and
deliberately ships **no placeholder API** for them, not even one raising
`NotImplementedError`. A placeholder advertises a capability the platform does
not have and invites callers to write against it. Price adjustment changes what
the returned numbers *mean*, so it is its own decision in a later phase. A test
asserts the continuous-series module exposes no attribute whose name suggests
adjustment.

### Ontology: listed versus synthetic

```
FuturesProduct            CME:ES                     not executable
  └── FuturesContractId   CME:ES:M2026               executable
ContinuousSeriesId        CME:ES:CONTINUOUS:ACTIVE   NOT executable, not a contract
```

`ContinuousSeriesId` is **not** a `FuturesContractId` and **not** a subclass of
one. It fails `require_listed_contract` automatically, because that guard is an
exact type check — ADR-009's design paying off exactly as intended.

The canonical form carries a literal `CONTINUOUS` marker in a **fourth** field.
A listed identity has exactly three fields, so `FuturesContractId.parse` rejects
a series form on field count alone and `ContinuousSeriesId.parse` rejects a
contract form the same way. The discriminator is **positional, not textual**,
so even a product whose root is literally spelled `CONTINUOUS` does not collide:
`CME:CONTINUOUS:M2026` (three fields) versus
`CME:CONTINUOUS:CONTINUOUS:ACTIVE` (four). Tested.

`token` is an opaque operator label, in the same spirit as `Venue.code`.
`"ACTIVE"` is a name, not a claim: it asserts no front-month or volume-based
selection. What the series maps to is whatever its schedule says.

Vendor aliases (`ES1!`, `NQ1!`) are **not** canonical continuous identities and
no parser accepts them.

### Two roll mechanisms, both ex-ante

1. **`ExplicitRollDefinition`** — the operator states the rolls. Nothing is
   inferred: not the initial contract (it is the first declared lifecycle), not
   the supported range, not the instants. This is the foundational oracle.
2. **`FixedCalendarRollDefinition`** — roll *N trading dates* before each
   contract's last trade date, at an explicitly named boundary.

Volume-crossover and open-interest rules, `RollObservation`, `TiePolicy`, and
`OscillationPolicy` are **not** implemented and have no skeleton. There is no
roll data pipeline yet, and an abstraction invented before its first consumer
would freeze the wrong shape around future volume/OI semantics.

### Decision time versus effective time

`RollEvent` keeps both coordinates and enforces `decision_time <=
effective_time`; a roll can never take effect before the decision that produced
it exists. Equality is permitted and is what the fixed-calendar rule emits.

**What that equality claims, precisely.** For a calendar-derived roll,
`decision_time` is the **logical policy-decision instant**: the roll became
actionable no later than the instant it took effect. It is **not** the
publication timestamp of the lifecycle fact, **not** when the exchange first
announced the last trade date, and **not** an information-availability
timestamp for any market observation. The equality is valid *precisely because*
this rule consumes no market observation — the calendar and the last trade date
are ex-ante facts — so it cannot claim knowledge earlier than it existed. It is
conservative, not sharp: the roll was in fact knowable much earlier, and
nothing should read `decision_time` as "earliest knowable".

**The contract a future data-driven mechanism inherits**, recorded now and
deliberately not built now:

> every observation's `availability_time <= decision_time <= effective_time`

The type system can enforce only the second half. A future volume/OI rule that
copy-pastes `decision_time = effective_time` would acquire look-ahead with a
green validator, so the derivation is encoded in each event's `rule_key`
(`fixed-calendar.offset-2.first-trading-window-start.CME:ES:H2026` versus
`explicit.3`), which is hashed and public, making an audit possible.

### RollSchedule: total coverage is structural

```
RollSchedule(product, calendar pin, supported range, trading-date range,
             initial_contract, events, provenance, content_hash)
```

Within `[supported_start, supported_end)` **exactly one** contract is active at
every instant. This is not a validated property but a consequence of
`bisect_right` over strictly increasing effective times, which is total and
single-valued on any list. `initial_contract` is *defined* as the contract
active at `supported_start`.

The validators exist to prevent **silent loss**, not gaps:

- **Strictly increasing effective times.** A duplicate would make `bisect_right`
  skip an authored event entirely — the event would vanish while the schedule
  still validated. Rejected, never broken by a tiebreak.
- **Chain continuity.** `events[0].from_contract == initial_contract` and
  `events[i].from_contract == events[i-1].to_contract`. Without it
  `from_contract` — a public field — could name a contract that was never
  active. `initial=A, A→B, C→D` is two locally valid events and one broken
  chain; it is rejected.
- **Events strictly inside `(supported_start, supported_end)`.** Every hashed
  field must be observable, or the hash distinguishes artifacts that behave
  identically. An event at `supported_start` would make `initial_contract`
  active over an empty interval — a hashed, public field no query could return.
  An event at `supported_end` would do the same to its own `to_contract`.
- **Contract distinctness.** A chain that revisits a contract makes "the
  interval during which X was active" ill-defined for every consumer.

A zero-event schedule is **valid** and describes a constant mapping. (This
differs from `MaterializedCalendar`, which requires at least one window: a
calendar with no windows describes nothing, whereas a constant roll mapping is
a real and useful thing.)

**Delivery-order monotonicity is deliberately NOT enforced.** Requiring a chain
to move forward in delivery period would encode an economic assumption about
which way a research series rolls. Distinctness is structural; monotonicity is
a market opinion, and this layer does not hold opinions.

### Structural versus factual invariants — stated, not implied

`RollSchedule` guarantees **structural** well-formedness and is unconstructible
if violated. It does **not** guarantee **factual** consistency with a calendar
or with lifecycle facts, because those need inputs a frozen model does not
hold; those checks live in the materializers. A caller constructing a
`RollSchedule` directly gets the structural guarantees only.
`MaterializedCalendar` has the same shape and the same limitation, and neither
pretends otherwise. `model_construct` and `object.__setattr__` remain
documented, announced bypasses (ADR-009).

### The calendar pin

`RollSchedule` carries `calendar_id`, `calendar_version`, and
`calendar_content_hash`, all hashed, and `segments_for_trading_date` verifies
them against the resolver it is handed before computing anything.

Without this, segmentation would accept *any* calendar and answer against a
different trade-date labelling — silently wrong rather than obviously wrong.
This is ADR-010's defect **D1** in a new place: a fact that gates the inverse
API sitting outside the pin. The three strings are exactly the shape ADR-010
blessed for pinning a calendar in a manifest.

`first_trading_date` / `last_trading_date` are carried for the same class of
reason: without them, a trading date entirely outside the schedule's coverage
would return `()` — indistinguishable from "the exchange did not trade that
day".

### Half-open boundaries

For a roll effective at `R`, the old contract is active on `[…, R)` and the new
one from `R` inclusive. Tested at `R − 1µs`, `R`, and `R + 1µs`; microseconds
are the platform's persisted precision, and nanoseconds appear nowhere.

### TradingDate is never collapsed

A roll may land mid-session, so the inverse API is:

```python
segments_for_trading_date(roll_resolver, calendar_resolver, trading_date)
    -> tuple[ContractSegment, ...]
```

Each segment carries `(start, end, contract, state, trading_date,
window_rule_key, roll_rule_key)`. Segments are produced by intersecting the
trading date's calendar windows with the roll partition, so one never spans a
gap between windows and none is ever zero-length. `state` is carried so a
consumer cannot mistake a pre-open or halt interval for tradable time.

Both resolvers are explicit arguments: the schedule does not own a calendar,
and this phase introduces **no instrument-to-calendar registry**.

Two behaviours worth knowing, both tested:

- A trading date's segments are **not necessarily contiguous**. If a roll falls
  in a closed break between two of the date's windows, the roll instant appears
  in **no** segment — the window before is wholly the old contract, the window
  after wholly the new.
- A window leaving the supported range makes the whole date **refused**
  (`RollScheduleRangeError`), never silently truncated: a truncated answer is
  indistinguishable from a genuinely short session.

### The count-back, and why it is not business-day arithmetic

`CalendarResolver` exposes no "list all trading dates" method, so
`roll_target_trading_date` walks a **civil-day cursor** while the **calendar
decides**: a candidate counts only when `windows_for` returns a non-empty
tuple. No weekday rule and no holiday list appears anywhere — and neither could
produce CME's trade-date labelling anyway, where a Sunday-evening session
belongs to Monday, or to Tuesday across a holiday. Termination is bounded by
the calendar's first trading date, not by a magic constant such as
`range(offset * 5)`, which would fail across a long holiday run.

Typed failures, each distinct and none of them a nudge: a last trade date the
calendar calls a non-trading date is a **contradiction between two authored
facts** (`LifecycleCalendarMismatchError`), not a date to shift; running off the
calendar raises `InsufficientCalendarCoverageError` rather than clamping.

### Effective boundary: explicit, no default

`EffectiveBoundary` has two members and **no default value**:

- `FIRST_WINDOW_START` — the target trading date's earliest window (for an
  overnight session, the pre-open on the *previous civil day*).
- `FIRST_TRADING_WINDOW_START` — its first `TRADING` window, i.e. the open.

They differ by the length of the pre-open on every date, so defaulting to
either would be a hidden policy. A trading date with windows but no `TRADING`
window raises `NoTradingWindowError`; there is deliberately no fallback to
`windows[0]`, because those are different instants.

### Content hash

`roll_schedule_content_hash` covers: format token `qrt-roll-schedule/1`,
product, the calendar pin, supported range (epoch µs), trading-date range,
initial contract, every event (`from`, `to`, decision µs, effective µs,
`rule_key`), and the sorted provenance table. Verification metadata is public
output, so an edited evidence citation moves the pin (ADR-010's **D3**).

Deliberately **excluded**: the effective-boundary policy and the roll offset —
they determine the effective instants, which are already covered, and two
policies yielding identical instants yield identical mappings and must hash
identically. They survive inside `rule_key`, which *is* hashed. Also excluded,
as always: generation provenance and **any free-form prose**, so documentation
edits cannot destabilize semantic identity.

`continuous_series_content_hash` covers the series id, the product, its own
supported range, and the schedule's `content_hash` — a **Merkle link**, so any
change to the referenced schedule's semantics changes the series hash. The
schedule is not re-serialized inline, because two definitions of one
serialization is one too many.

No `decision_hash` is invented. The schedule carries **no logical version
number**: the content hash is the pin, and dataset-catalog versioning is a
later phase.

### Provenance

`RollProvenance(derivation_kind, evidence_ids)` with
`RollDerivationKind ∈ {OPERATOR_DECLARED, CALENDAR_DERIVED}` — a *mechanism*,
not an evidence grade. It is deliberately **not** `VerificationStatus`: that
enum grades how strongly a published exchange fact is evidenced, and grading a
research *decision* as "verified-primary" would be the same overclaim ADR-010's
defect D10 removed from the calendar. Mixing them would also mean a member
added for rollover could be used to author a calendar rule, weakening ADR-010's
evidence policy.

For the same reason `ContractLifecycle` carries its own minimal
`LifecycleProvenance` (evidence references only) rather than the calendar's
`RuleProvenance`. Three provenance concepts stay semantically separate:
calendar-rule, lifecycle-fact, roll-derivation. Phase 2.1 is untouched.

Provenance integrity mirrors the calendar and goes one step further: keys
unique, every `rule_key` resolves, and **orphan entries rejected** — an
unreferenced entry would move the hash without changing any answer, the inverse
of D1. Accumulation raises on a conflicting key rather than `setdefault`
silently dropping the second entry's evidence.

### Storage

`SCHEMA_VERSION` stays **2**. No roll or continuous column enters any Parquet
schema, no file changes meaning, and `instrument_symbol` keeps its ADR-009
status as a legacy vendor alias — nothing in this phase reads it as canonical
identity in either direction. Phase 2.2 adds **no persisted artifact format**;
the deterministic serialization and content hash are sufficient for a domain
artifact, and catalog/persistence integration belongs to a later phase.

## Alternatives considered

**A generic `RollRule` hierarchy with a `VolumeCrossoverRollRule` skeleton.**
Rejected. There is no roll data pipeline to shape it, and the abstraction would
freeze around guessed volume/OI semantics.

**Deriving expiry from the contract month.** Rejected outright: every rule that
looks plausible ("third Friday", "business day before the 25th") is a
product-specific convention that has changed over time. A missing lifecycle
fact is unsupported, never computed.

**Bounding `supported_end` by the final contract's last trade date.** Rejected
after review. `last_trade_date` is a *date*; deriving a final tradable *instant*
from the calendar's last window would assert something the fact does not
establish, since termination on the last trading date may follow
product-specific rules. See Limitations.

**Reusing `VerificationStatus` for roll provenance**, or adding a member to it.
Rejected: see Provenance.

**Collapsing `decision_time` into `effective_time`.** Rejected: it would
destroy the coordinate a future data-driven rule needs to prove it consumed no
future information.

**Sorting authored events into order.** Rejected: authored order is a fact, not
an implementation artifact. Out-of-order events are an authoring error and are
reported. (The calendar materializer sorts *generated* windows, which is a
different thing.)

**A single effective-boundary policy.** Rejected: it would make the pre-open
versus open choice a hidden default.

**`active_contract_for_trading_date(td) -> FuturesContractId`.** Rejected: it
cannot be correct when a roll lands inside the trading date, and its failure
mode is silent.

## Consequences

Positive: a research series can be resolved to a real listed contract at any
supported instant, deterministically and reproducibly; a continuous identity
cannot be executed; every schedule is pinned by a content hash that covers
everything observable; and the calendar it was built against travels with it.

Negative, stated plainly:

- **Contract lifetimes are bounded at the end only, and not at all in the
  schedule.** No listing or first-trade fact exists in this repository, and by
  explicit decision no last-trade *instant* is derived from the calendar. A
  schedule can therefore map a contract after its last trade date; nothing
  detects it. Establishing an exact termination boundary requires a separately
  source-backed lifecycle fact, which is future work.
- **LTD-anchored rolls cannot express first-notice-date-anchored rolls**, since
  no FND fact exists here. For a *physically delivered* product an LTD-anchored
  roll holds past first notice, i.e. into the delivery process. ES and NQ are
  cash-settled so this does not bite them, but it would bite others.
- **No roll data ships.** Every schedule and every lifecycle fact is
  caller-supplied, and any schedule materialized against the shipped
  `CME_EQUITY_INDEX` v1 calendar inherits its supported range
  (2023-05-22 … 2023-12-29).
- **Vocabulary tension:** `ContinuousSeriesDefinition` carries a content hash,
  whereas elsewhere `*Definition` means an authored input and `Materialized*`
  means the hashed artifact. The name is retained because it is the owner's
  chosen public name; the inconsistency is recorded rather than hidden.
- `windows_for` is a linear scan, so the count-back is O(days × |windows|).
  Fine at present scale; an index is the fix if it ever matters.

## Falsification record

Two adversarial passes ran after implementation and green tests.

**Pass 1** attacked D1–D10 from the phase brief directly: a broken first link, a
broken later link, duplicate effective instants, a gap inside the supported
range, a hash ignoring the initial contract or the range, a trading-date lookup
collapsing an intraday roll, Python date arithmetic replacing calendar
semantics, a lifecycle boundary inferred from the contract month, a
`ContinuousSeriesId` passing the executable guard, and `instrument_symbol` read
as canonical identity. **All ten held.** One genuine gap was found in the
*tests* rather than the code: the documented "roll inside a closed gap" case
was unasserted, and is now a named regression test.

**Pass 2** was an independent hostile review of the whole diff, and it was the
productive one. Ten findings were accepted and fixed, each with a regression
test:

- **P1 — provenance order was observable but unhashable.** The serialization
  emits provenance as a sorted JSON object, so the authored tuple order never
  reached the hash: two schedules differing only in `provenance[0]` validated,
  compared unequal, and shared a pin. Fixed by requiring provenance to be in
  sorted key order, making the observable order a function of the hashed keys.
- **P2 — `RollSchedule.events` and `.provenance` admitted subclasses.** The
  ADR-009-D4 guard was applied one level down but not to the schedule's own
  collections, so a `RollEvent` subclass carrying an extra public field was
  admitted and hashed identically to the plain event. Exact-type guards added.
- **P3 — only one of the three pinned calendar fields was verified**, while
  this ADR claimed all three were. A schedule whose pin named a different
  calendar but carried the right content hash was accepted. Fixed with a shared
  `require_matching_calendar_pin` used by both segmentation and materialization.
- **P4 — `roll_effective_instant` had no `else: raise`.** `EffectiveBoundary`
  is a `StrEnum`, so the equal-valued bare string `"first-window-start"` failed
  the `is` test and silently selected the *other* policy — 15 minutes late, with
  a confident answer. Exact enum-type check plus an explicit final raise, so a
  member added later fails loudly instead of inheriting a branch.
- **P5 — the explicit path had no test coverage at all**, despite this ADR
  calling it the oracle every other mechanism is checked against. A full
  `tests/test_explicit_rollover.py` was added. (Independently found and written
  before the review returned.)
- **P6 — `RollScheduleError` was defined, exported, and never raised.** Deleted;
  a documented error class that can never be caught is misleading vocabulary.
- **P7 — `roll_effective_instant` leaked `CalendarRangeError`** and documented
  no raises, unlike its sibling. Now wraps, guards its arguments, and documents.
- **P8 — definitions never checked `first_trading_date <= last_trading_date`**,
  so an inverted range surfaced much later as a generic materialization error.
- **P9 — explicit definitions required evidence-cited lifecycles and then used
  one field of one of them.** Now every contract named by an authored event
  must have a declared lifecycle, and the docstring states plainly that
  `last_trade_date` is not consumed on this path.
- **P10 — `series_id` admitted a subclass** while the adjacent `schedule` field
  did not; since the hash is built from `series_id.canonical()`, an overriding
  subclass could diverge the hashed identity from the object's.

Also corrected: `domain/time.py` — which now owns `epoch_microseconds`, the
instant encoding *both* content hashes are built from — matched none of the
architecture sweep's subject tokens and was therefore the one module not
covered by the no-wall-clock and no-ambient-state guarantees. The token list
now includes it, with a canary test.

Two nits were accepted as documentation fixes rather than code changes: the
module docstring said the `CONTINUOUS` marker sits "in a fourth field" when it
sits third of four; and the stated reason for rejecting boundary-effective
events ("a hashed field no query could return") was too strong, since
`contracts()` does return such a contract. The honest claim — that the
*time-indexed mapping* would never reach it — now appears in both the code and
the test that pins it.

One finding was accepted as a docstring correction only: `_record`'s
conflict branch is unreachable from any current input, and its docstring now
says so while keeping the guard, because the alternative (`setdefault`) fails
by silently discarding evidence.
