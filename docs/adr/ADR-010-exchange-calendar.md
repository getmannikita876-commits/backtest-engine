# ADR-010: Exchange calendar — evidence-pinned definitions, deterministic materialization, UTC-only resolution

- Status: **Accepted**
- Date: 2026-08-10
- Related: ADR-002 (bar availability time), ADR-008 (environment
  reproducibility), ADR-009 (futures contract identity),
  `docs/calendar-evidence.md`, `docs/architecture.md`

> Numbering note: ADR-006 remains reserved by ADR-008's numbering note for the
> storage error contract; this decision takes 010 after ADR-009.

## Problem

Deterministic ES/NQ research needs to answer, for any UTC instant: was the
exchange trading, halted, or closed; which **trading date** did the instant
belong to; where are the surrounding window boundaries; and under which pinned
calendar was the answer produced. The answer must be byte-identical across
runs, processes, hash seeds, operating systems, wall-clock times, and future
timezone-database updates — and must be honest about what it does not know.

Two failure modes dominate naive designs. First, deriving the trading date
from a timestamp: CME's own holiday tables assign a Sunday-evening trade to
the following **Tuesday** when Monday is a holiday, so `timestamp.date()`,
exchange-local civil dates, and weekday arithmetic all produce silently wrong
labels. Second, encoding "the usual CME hours" from memory: a calendar that
guesses is a look-ahead-adjacent defect generator, because every wrong window
boundary reclassifies real market data.

## Decision

### Facts are data; the engine is generic

Schedule facts live in a versioned, declarative **TOML definition file**
(`src/quant_research_terminal/calendars/definitions/`), validated through
strict Pydantic models. Python contains generic interpretation only; no
schedule fact — no holiday, no session time, no trade-date rule for a real
exchange — is hardcoded in engine code or in resolver control flow. TOML was
chosen over YAML because Python 3.12 parses it in the standard library
(`tomllib`) with native date/time values and zero new dependencies; the
project pins no YAML parser.

Every rule and dated exception in a definition carries a verification status
and citations into an evidence table, whose durable narrative register is
`docs/calendar-evidence.md`. Evidence policy: only official CME publications
establish a fact; an unretrievable fact is an **unsupported range**, never an
"unverified" approximation; contradictory primary sources block the affected
range rather than being averaged.

### Pipeline

```
CalendarDefinition           authored facts (TOML → strict models)
      │ materialize()        deterministic; the only local-time arithmetic
      ▼
MaterializedCalendar         explicit half-open UTC windows, content-hashed
      │
      ▼
CalendarResolver             pure bisect lookups; no tz arithmetic at query time
```

Materialization is a required semantic boundary: replay-time resolution
consumes explicit UTC windows, so no query depends on the timezone database
present at replay time. tzdb sensitivity is concentrated at generation and
made loud there (probes, below). Phase 2.1 does **not** commit generated
materializations to Git; the materializer is deterministic and cheap, and
artifact lifecycle belongs to the future dataset-catalog work.

### Identity, version, and content hash

Three separated concepts:

- **`CalendarId`** — logical identity of a *schedule class* (e.g.
  `CME_EQUITY_INDEX`). Deliberately not a `Venue`: one venue operates many
  schedules, so `Venue("CME")` can never stand in for a calendar. Token rules
  mirror ADR-009: `[A-Z0-9_]`, at least one letter, never normalized.
- **`CalendarVersion`** — a positive integer naming one released set of
  authored facts. Released versions are immutable; corrections create a new
  version. Old pins keep reproducing old answers, tested as such.
- **`MaterializedCalendarHash`** — SHA-256 over a canonical serialization of
  the materialized semantics: format token, calendar id, version, declared
  trading-date range, coverage bounds, and every window's microsecond UTC
  bounds, state, and trading date. This is the strongest reproducibility pin
  for the actual UTC schedule.

The tzdb release is **generation provenance, not identity**: two tzdb versions
that yield byte-identical windows have produced the same calendar, and pinning
environment noise into the hash would make identical semantics look
different. Conversely everything the resolver's behavior depends on is inside
the hash — the falsification pass found (defect D1) that the declared
trading-date range gates the inverse API while being outside the hash, and
the fix moved it inside.

### Exchange state model — and what it is not

`ExchangeTradingState`: **TRADING**, **HALT**, **MAINTENANCE**, **CLOSED**.
The vocabulary follows CME's published legend where one exists (OPEN /
PREOPEN-HALT / CLOSED); MAINTENANCE is first-class for schedules that publish
an explicit maintenance interval, and a maintenance instant resolves as
MAINTENANCE, never CLOSED. The shipped CME equity definition uses TRADING,
HALT, and CLOSED only, because CME's own tables label the daily platform
breaks CLOSED and PREOPEN(HALT); inventing a "maintenance" label those
sources do not use would be fake precision. Windows are materialized for
TRADING/HALT/MAINTENANCE; **CLOSED is the complement** within coverage —
thousands of curated closed windows would be a second source of truth.

**Research segmentation is a different concept.** RTH/ETH, overnight, and
custom research sessions are labels a researcher lays *over* physical
exchange state; they must never determine tradability. Phase 2.1 implements
no segmentation; when one arrives it will be a separate layer consuming the
calendar, never a state in it.

### TradingDate

`TradingDate` is a dedicated frozen type whose validator rejects `datetime`
(which *is-a* `date`) and date subclasses outright: a session label assigned
by the exchange, not a civil date, and not derivable by arithmetic. Windows
carry their trading date as authored data straight from CME's own TRADE DATE
labels. One trading date may own several disjoint windows (holiday halts
split a session), which is why the inverse API returns a tuple.

### Timezone and DST policy

- Authored rule times are exchange-local wall times under one IANA
  `Region/City` zone (`America/Chicago` for the CME equity schedule — CME
  states "U.S. Central Time"; the IANA mapping is recorded as an explicit
  interpretation in the evidence register). Abbreviations (CST), bare words
  (UTC), and fixed offsets (UTC-6, `Etc/GMT+6`) are rejected structurally;
  nothing is trimmed or canonicalized.
- Public runtime APIs are UTC-only: naive datetimes and non-UTC zones are
  rejected by the same `validate_utc_datetime` contract the rest of the
  domain uses.
- **Spring forward:** a rule boundary naming a nonexistent local time raises
  `NonexistentLocalTimeError`; nothing is shifted.
- **Fall back:** a rule boundary naming an ambiguous local time raises
  `AmbiguousLocalTimeError`; there is no implicit `fold=0` anywhere, and no
  disambiguation parameter exists until a real schedule needs one — an
  ambiguous boundary is an authoring defect to fix in the data.
- **tzdb probes:** the definition declares expected UTC offsets at chosen
  instants (standard time, daylight time, both 2023 transitions, the range
  edges); materialization fails loudly if the effective tzdb disagrees, and
  `tests/test_tzdb_probe.py` asserts the same expectations in plain pytest on
  both CI platforms — closing the gap where CI only ever probed
  `ZoneInfo("UTC")`.

### Window semantics

Half-open `[start, end)`: an instant exactly at a boundary belongs to what
follows. Boundary tests assert at one **microsecond** on either side — the
platform's canonical persisted precision (schema v2, `timestamp[us, tz=UTC]`);
nanoseconds are not representable and not used. Windows are explicit UTC,
immutable, strictly ordered, non-overlapping, bounded by coverage, and each
carries a `rule_key` linking it to the definition rule or exception that
produced it, which is how effective verification metadata reaches a resolved
context without being copied onto every day.

This is compatible with, and distinct from, ADR-002: **bar availability**
(a bar becomes visible at its interval close) is an event-visibility rule;
**calendar membership** (`[start, end)`) is a state-classification rule. A
bar closing exactly at 16:00 CT becomes available at an instant the calendar
already classifies as CLOSED — both statements are correct simultaneously,
and neither redefines the other. ADR-002 is unchanged.

### Resolver API

`CalendarResolver.resolve(utc_instant) → CalendarContext` with: state,
trading date (`None` for CLOSED gaps — a gap belongs to no trade date),
current window bounds (for CLOSED, the gap bounds), `next_transition`
(the current window's end, or `None` when it runs to the coverage edge —
the calendar refuses to describe transitions it cannot see), calendar id,
version, content hash, and the effective verification metadata for the
window's originating rule. `supports(utc_instant) → bool` answers the range
question; `resolve` outside coverage raises `UnsupportedTimestampError`.
Nothing is extrapolated: a missing date is unsupported, not "normal".

The resolver exposes verification metadata and decides nothing with it.
Warn/block/allow policies belong to future application-layer integrity
checks; a `warn_on_unverified` flag inside the domain resolver is exactly the
policy-in-the-wrong-layer design this ADR forbids.

### Inverse API

`windows_for(trading_date) → tuple[MaterializedWindow, ...]` — the smallest
inverse contract that does not lie: multiple windows per trading date are
returned in order (holiday halts split sessions; assuming one contiguous
tradable interval per date is exactly the bug the Memorial Day 2023 evidence
falsifies). Outside the supported date range it raises `CalendarRangeError`;
an in-range date the exchange did not assign (weekend, removed holiday)
returns `()`, which is the factual answer. No flatten-at-close, VWAP, or
execution semantics — temporal facts only.

### Holiday / exception model

Dated exceptions are explicit and evidence-cited, in two closed kinds:
`removed` (the date is not a trading date; its weekly-template windows never
materialize) and `replaced` (the date's complete window list is restated —
no partial merging with the weekly template, which invites silent drift).
Exception windows are bounded to lie near their trading date (defect D2:
a window months away is a typo, not a schedule). US-federal-holiday formulas
are not used as truth anywhere; every exception is a dated fact from a CME
holiday schedule. If CME behavior changes across years, the change lands as
new dated data — never as a mutation of a released version.

### Schema v2 and future compatibility

`SCHEMA_VERSION` stays 2. No calendar column is added to market-data Parquet,
no existing file is reinterpreted, and `instrument_symbol` keeps its ADR-009
meaning. The one forward-compatibility statement this ADR makes: a future run
manifest or dataset-catalog artifact can pin a calendar by
`(CalendarId, CalendarVersion, MaterializedCalendarHash)` — three strings —
without reinterpreting anything that exists today. Schema v3 fields are not
designed here.

## Alternatives considered

- **Depend on an existing calendar library** (`exchange_calendars`,
  `pandas_market_calendars`). Rejected: their session models collapse the
  states this project must keep apart, their facts are not evidence-pinned to
  primary CME sources, and their update cadence mutates history — the exact
  opposite of immutable versions.
- **Compute schedules at query time from local-time rules.** Rejected: every
  query would depend on the replay machine's tzdb, and DST edge cases would
  live on the hot path instead of at one audited boundary.
- **Commit materialized windows to Git instead of materializing at load.**
  Deferred, not rejected: the materializer is deterministic and probed, the
  content hash pins the output, and artifact lifecycle belongs to the
  dataset-catalog phase. A golden hash pin in tests gives the regression
  protection committing the artifact would give, without inventing an
  artifact store early.
- **Model ETH/RTH as exchange states.** Rejected outright: research
  segmentation is not tradability, and coupling them is the critical
  ontology error this phase exists to prevent.
- **Make the tzdb version part of calendar identity.** Rejected: semantic
  identity is the materialized content; environment provenance in the
  identity would split identical calendars.
- **A `TradingDate` as a plain `datetime.date` alias.** Rejected: nothing
  would stop `timestamp.date()` from masquerading as calendar resolution.
- **Encoding "probably normal" days outside the evidence window.** Rejected
  by the evidence policy: the supported range is exactly what the evidence
  covers.

## Consequences

Positive: ES/NQ research over May–December 2023 has a deterministic,
evidence-pinned temporal oracle; every later phase (rollover, replay,
execution) consumes explicit UTC windows and typed trading dates; tzdb
divergence and DST hazards fail loudly at generation; corrections cannot
silently rewrite history.

Negative, stated plainly: the supported range is eight months of 2023 —
extending it is evidence work, not code work; the Sunday pre-open time and
the Columbus/Veterans "normal day" treatment rest on corroborated rather than
directly-stated evidence (recorded as such in the metadata); the definition
format will need a versioned migration if its schema ever changes; and
nothing yet maps an instrument to its calendar — ES/NQ consumers select
`CME_EQUITY_INDEX` explicitly, and a general instrument→calendar mapping is
future work.

Out of scope, unchanged by this ADR: rollover and continuous futures,
schema v3, dataset catalog, replay, execution, research segmentation,
settlement semantics, options, and every other Phase 2.2+ subject.

## Falsification record

Two adversarial passes ran after implementation and green tests: a
self-review, then an independent hostile review of the full diff. Every
defect was reproduced or concretely characterized, fixed, and
regression-tested.

Pass 1: **D1** — the declared trading-date range influenced inverse-API
behavior but sat outside the content hash, so two semantically different
calendars could share a hash (fixed: range serialized). **D2** — exception
windows accepted civil dates arbitrarily far from their trading date,
letting a typo'd month materialize quietly (fixed: locality bound).

Pass 2: **D3** — the hash omitted rule keys, the provenance table, and the
timezone name while `CalendarContext.verification` is public resolver
output, so an edited evidence citation would not move the "immutable" pin
(fixed: the serialization now covers every observable artifact field).
**D4** — the loader passed a scalar TOML `evidence` string through
`tuple()`, exploding it character-by-character (fixed: arrays required).
**D5** — `timezone_probe` and `exception` sections defaulted to empty when
absent, so an accidentally deleted probe section shipped silently without
tzdb protection (fixed: both sections are required, `= []` states intent
explicitly). **D6** — the probe comparison converted the actual tzdb offset
to whole minutes before comparing, quantizing away sub-minute divergence
(fixed: exact `timedelta` comparison). **D7** — `CalendarContext` enforced
no cross-field coherence, so self-contradictory answers (a CLOSED context
with a trading date, a timestamp outside its own window) were constructible
(fixed: coherence validator). **D8** — a window labelled with a trading date
outside the declared range passed construction, making it unreachable
through the inverse API (fixed: label range check). **D9** — the calendar
architecture scans enumerated today's modules by name, so a future calendar
module would escape them (fixed: prefix/name sweep with a
known-modules canary). **D10** — the weekday 16:45 pre-open was labelled
`verified-primary` although its general application is inferred from
holiday-PDF instances, the same evidence shape the Sunday pre-open honestly
labels `corroborated` (fixed in the definition data; hash re-pinned).

Also hardened from pass-2 nits: exact docstring/regex agreement on the
IANA-zone contract (tzdb aliases are accepted as written, never
canonicalized), a meaningful no-invented-precision window assertion, and
rejection of path-traversal resource names in the packaged-definition
loader.
