# Quant Research Terminal

A local Windows desktop application supporting professional, reproducible
quantitative research on futures.

The build is a **data foundation plus a deterministic replay engine**, not a
backtester. Replay turns verified datasets into an availability timeline and
stops there: there is no backtest engine, market state, execution simulator,
strategy API, portfolio, options support, optimization, or AI feature, and none
is planned before the foundation is correct.

## What is implemented

| Area | State |
| --- | --- |
| Immutable domain models (`Trade`, `Quote`, `Bar`, `TradeSide`) | implemented |
| Storage contracts, fixed-point encoding, schema v2 and v3 | implemented |
| **Parquet read/write round-trip** | implemented |
| Import validation, normalization, ordering | implemented |
| End-to-end import use case (provider → validated Parquet, verified by read-back) | implemented |
| CSV provider | implemented |
| Databento archived delimited export decoding | implemented |
| ThetaData archived export decoding | **experimental, unverified** |
| Futures contract identity in the **domain** (`FuturesContractId`) | implemented |
| Futures contract identity in **storage** (schema v3) | implemented |
| Dataset manifests, provenance, and artifact catalog | implemented |
| Explicit v2→v3 migration (never inferred) | implemented |
| Importer writing schema v3 | **not implemented** — needs a declared contract per dataset |
| Exchange-calendar engine (definitions → materialized UTC windows → resolver) | implemented |
| CME equity-index (ES/NQ) calendar facts | **verified for trading dates 2023-05-22 … 2023-12-29 only** |
| Futures rollover mapping (instant → listed contract) | implemented |
| Continuous-series identity (non-executable) | implemented |
| Continuous **price** series (back-adjustment) | **not implemented**, deliberately |
| Dataset revision lineage (supersedes claims, cross-batch comparison) | implemented |
| **Deterministic replay** (verified datasets → availability timeline) | implemented |
| Market state, strategies, execution, portfolio | **not implemented** |

## Futures instrument identity

A specific listed contract is `FuturesContractId` — a venue, a product root, a
delivery month, and a **full four-digit year**, canonically `CME:ES:M2026`. See
`docs/adr/ADR-009-futures-contract-identity.md`.

- `ES` is a product and is **not executable**; only a `FuturesContractId`
  passes `require_listed_contract`. A continuous or back-adjusted series must be
  its own type and fails that guard.
- **`ESM6` is never expanded to 2026 by inference.** The one function that
  expands an abbreviated year requires an explicit decade and has no default, so
  no identity can depend on the current date. This holds for the committed
  `ESM6` fixture too, whose rows are dated March 2024 and which establishes
  nothing about the delivery year.
- Values are **rejected, never normalized**: `"es"` and `" ES"` are errors, so
  two spellings cannot silently become one instrument.
- Identity excludes specification (tick size, multiplier, currency, fees,
  expiry dates) and provenance. Neither is modelled.

**Schema v3 persists canonical identity; schema v2 never did.** A v2 file has one
`utf8` `instrument_symbol` column carrying no venue and no full year, so
canonical identity cannot be reconstructed from it without guessing. Schema v3
replaces that column with `canonical_identity` plus `vendor_symbol` and records
identity in the schema metadata too. `SCHEMA_VERSION` is 3; every v2 file is
still written and read exactly as before, and moving one to v3 requires an
operator-supplied alias→contract mapping. See "Dataset identity" below.

## Exchange calendar

The calendar answers, for a UTC instant: exchange state (trading / halt /
maintenance / closed), the **calendar-assigned** `TradingDate`, the current
window's bounds, the next transition, and the pinned calendar identity
(`CalendarId`, `CalendarVersion`, materialized content hash). See
`docs/adr/ADR-010-exchange-calendar.md`.

The **mechanics** and the **verified facts** are different things:

- Mechanics (implemented, generic): declarative TOML definitions validated by
  strict models, a deterministic materializer producing explicit half-open
  `[start, end)` UTC windows, typed rejection of DST-nonexistent and
  DST-ambiguous rule boundaries, tzdb probes that fail materialization loudly
  on a divergent zone database, a content hash pinning the materialized
  schedule, and a resolver that performs no timezone arithmetic at query time.
- Facts (`CME_EQUITY_INDEX` v1): the CME Globex EQUITIES schedule —
  Sun–Fri 17:00–16:00 CT with the 15:15–15:30 CT daily halt and all six 2023
  holiday exceptions — **verified against official CME publications for
  trading dates 2023-05-22 through 2023-12-29 and no further**. Outside that
  range the calendar refuses to answer rather than extrapolating; extending it
  is evidence work, recorded in `docs/calendar-evidence.md`.

A trading date is assigned by the calendar, never derived from a timestamp:
the Sunday-evening session before Memorial Day 2023 belongs to trading date
**Tuesday** 2023-05-30, per CME's own schedule.

## Futures rollover and continuous mapping

A continuous series is a research construct; no order was ever filled in one.
The rollover layer answers one question — *at this UTC instant, which listed
contract was active?* — and nothing else. See
`docs/adr/ADR-011-futures-rollover-and-continuous-mapping.md`.

```
ExplicitRollDefinition / FixedCalendarRollDefinition   authored facts
      │  deterministic materialization
      ▼
RollSchedule           explicit UTC roll events, content-hashed
      │
      ▼
RollResolver.active_contract_at(utc) -> FuturesContractId
```

- **A continuous identity can never be executed.** `ContinuousSeriesId`
  (`CME:ES:CONTINUOUS:ACTIVE`) is not a `FuturesContractId` and fails
  `require_listed_contract` automatically — the exact-type guard ADR-009 built
  for precisely this. Its canonical form has four fields against a listed
  contract's three, so neither parses as the other.
- **Exactly one contract at every supported instant.** Roll boundaries are
  half-open `[start, end)` at microsecond precision; outside the declared range
  the schedule raises rather than extrapolating.
- **A trading date is never collapsed.** A roll can land mid-session, so
  `segments_for_trading_date` returns ordered segments carrying the exchange
  state, not one arbitrarily chosen contract.
- **Trading-date arithmetic is the calendar's, not Python's.** The
  fixed-calendar rule counts back *trading dates*; weekends and holidays are
  skipped because the calendar says they are not trading dates.
- **No lifecycle fact is ever computed.** A contract's last trade date is
  supplied with evidence; there is no expiry formula anywhere. A missing fact
  is unsupported.
- **No prices are synthesized.** No back-adjustment, ratio, Panama, or
  roll-gap smoothing — and no placeholder API for them.

**No roll data ships.** Every schedule and lifecycle fact is caller-supplied.

## Dataset identity, provenance, and the catalog

Answers *exactly which immutable canonical market-data dataset artifact is
this?* — without a filename, a path, a provider's state, a clock, or any
inference from a vendor alias. See
`docs/adr/ADR-012-dataset-artifact-identity-and-schema-v3.md`.

| Identity | Answers | Changes when |
| --- | --- | --- |
| `SemanticDatasetHash` | *what does this data mean?* | a record, or the row order, changes |
| `PhysicalArtifactHash` | *which exact bytes?* | the file is re-encoded, corrupted, or replaced |
| `ManifestHash` | *which claims and provenance?* | any claim or provenance field changes |

- **The three are never collapsed.** That is what lets verification report a
  re-encode as `PHYSICAL_MISMATCH` and an *edited* dataset as
  `SEMANTIC_MISMATCH`. The clearest demonstration ships as a test: migrating one
  file twice with mappings differing only in an **unused** entry yields the same
  semantic hash, the same physical hash, and a different manifest hash.
- **The semantic hash is computed from decoded domain records**, not stored
  columns — `parse_trade_side` normalises, so a file holding `"BUY"` and one
  holding `"buy"` are the same dataset. It excludes vendor alias, provider, path,
  codec, row-group layout, wall clock, randomness, and `schema_version` itself.
- **Order is semantic; sorting is forbidden.** Duplicates survive exactly
  (ADR-003). The order pinned is the artifact's *stored* order, which is **not**
  the future replay total order.
- **Location is never identity.** `DatasetManifest` has no path field at all, so
  relocation cannot change a hash by construction rather than by policy. The
  index maps bytes to paths — never manifests to paths — so a manifest resolves
  through *any* surviving byte-identical copy, and the index can be deleted and
  rebuilt without losing a registration.
- **Nothing is trusted.** Registration recomputes every claim — schema version,
  record type, contract, row count, time bounds, vendor aliases, both hashes —
  from the artifact. There is no partial registration.
- **One dataset may have many artifacts.** Identical records from two vendors
  share a semantic hash while differing physically and in provenance, so lookup
  returns a deterministic tuple and never silently drops an alternative.
- **Migration is explicit or it does not happen.** Alias → exact contract,
  supplied by the caller; no symbol parsing, no current-year default, no
  month-cycle arithmetic. The v2 input is never modified.

**Durability is not claimed.** `os.replace` gives visibility atomicity only; no
`fsync` is called, and directory `fsync` has no Windows equivalent — so the
recovery story is detection, which is what the physical hash is for.

## Correction lineage and cross-batch comparison

Datasets are immutable, but corrections are real. A correction is recorded as a
**separate immutable claim** beside the data, never as a change to it. See
`docs/adr/ADR-013-dataset-revision-lineage-and-cross-batch-consistency.md`.

- **A pinned dataset stays pinned, forever.** After `B supersedes A`, asking for
  A returns exactly A — same manifest, same three hashes, same verification. The
  successor appears only in the lineage graph. Nothing anywhere redirects.
- **`DatasetManifest` is unchanged.** It gains no `parent` or `supersedes` field:
  a claim is usually made *after* both manifests exist and a manifest is
  immutable, and the manifest hash covers every field — so such a field would
  change a dataset's identity because something was later said *about* it.
- **Corrections are declared, never inferred.** Different semantic hashes prove
  nothing on their own; identical ones cannot be a correction at all, because
  that is provider or encoding variation with no corrected data in it.
- **The graph branches and joins.** `A → B` and `A → C` are both valid, and
  **neither is chosen** — there is no `latest`, `current`, or `preferred`, and a
  build-failing test keeps it that way. Cycles are refused.
- **Comparison is an explicit call** that reports evidence and returns an
  ephemeral value object. It is never consulted during import, so an import's
  result never depends on what else is in the local catalog.
- **Bars have a real key** — contract plus period (ADR-005) — so disagreement
  about one period is a *proven conflict*, with no direction implied. Duplicate
  periods are refused rather than resolved by a first-wins guess.
- **Trades and quotes do not.** ADR-003 established that they carry no logical
  event identity, so comparison reports exact-row differences and multiplicities
  and never claims one row corrects another. A shared timestamp is not an
  identity.

**Lineage is not a point-in-time capability.** It records corrections known to
the platform *now*. It cannot say when a correction became historically
available — no manifest carries a trustworthy source-publication time, and
catalog write time, file mtime, and registration order all measure when this
machine learned something rather than when the data was revised. Answering
"would this have been available on date T?" needs source-backed availability
semantics that no provider here supplies, so the capability is absent rather
than approximated.

## Deterministic replay

Replay answers one question — *what information becomes available next?* — and
refuses to answer any other. See `docs/replay.md` and
`docs/adr/ADR-014-deterministic-replay-and-simultaneous-observation-frames.md`.

    same exact ManifestHash input set + same ReplayRange = same ReplayFrame stream

verified across repeated calls, fresh preparations, separate processes,
`PYTHONHASHSEED` values, storage layouts, and later lineage claims — and, more
strongly than any of those, in a child process where **every clock read raises**.

- **The frame is the unit, and that is the anti-look-ahead decision.** Every
  observation that became available at one instant is delivered in **one**
  `ReplayFrame`, processed atomically. Delivering them one at a time would let a
  consumer decide in between — on a sequencing artifact, because nothing
  persisted proves a trade was knowable before a quote at the same microsecond.
  That decision would be look-ahead bias that reproduces perfectly and looks
  entirely plausible.
- **Order inside a frame is technical, never causal.** Events are stored by
  `(source manifest digest, original row ordinal)`, built from provenance
  precisely so nobody mistakes it for market meaning. There is no
  trade-before-quote rule, no bar priority, no contract, provider, path, or
  physical-encoding tie-break — and a build-failing test refuses a
  `RecordType`-keyed mapping *literal* in a replay module, backed by an allowlist
  that refuses any new public name at all.
- **A bar becomes observable exactly at its interval close** (ADR-002), never at
  its interval start and never a microsecond early. Structural, not validated:
  availability is a derived property with no field to put a wrong value in.
- **Trade and quote availability is stated honestly.** It equals the persisted
  timestamp because that is the only timestamp there is. This is *not* a claim
  that feed latency is zero. No latency model exists and none is approximated.
- **Inputs are exact `ManifestHash` values.** Not a filename, a vendor symbol, a
  contract, a continuous series, or "the latest revision". They form a *set*,
  stored sorted, so caller order cannot become hidden semantics.
- **Nothing is repaired.** Sources are never sorted — an out-of-order source is
  refused, because stored row order is part of a dataset's semantic identity.
  Repeated rows are never deduplicated. Row ordinals are never renumbered, even
  after range filtering.
- **Replay never reconciles and never redirects.** Two manifests pinning one
  dataset are refused, as are two overlapping histories of one stream — not
  because the data conflicts, but because replay will not present an
  unreconciled union as one observed sequence. Lineage is never consulted: the
  package imports no successor navigation, so a run that pinned some bytes
  consumed those bytes.
- **Full preflight, then a snapshot.** Every artifact is verified before any
  frame exists, so there is no partial replay; every indexed location is tried,
  so one stale index entry cannot hide a valid copy. The verified rows are then
  held in memory, so later changes to the file cannot change the run.
- **No clock, no seek.** Replay time *is* the frame's availability time. A range
  filters the raw information stream; it does not reconstruct accumulated state,
  which is why it is not called a seek.

Memory scales with the selected input artifacts and is unprofiled; a dataset
larger than memory is not supported yet.

## What is not implemented

Market state, execution, strategies, portfolio, options, Monte Carlo, prop-firm
evaluation, DuckDB, SQL or graph databases, dataset partitioning, provider
registry, live or historical vendor APIs, credential handling, and experiment
tracking. Also deliberately absent: automatic merge or reconciliation of
disagreeing datasets, cross-provider preference, revision numbers, and any
point-in-time "what was known then" reconstruction.

Replay deliberately excludes a replay clock, wall-clock pacing, true seek or
checkpointing, market state, strategy callbacks, orders, fills, latency and queue
models, a run manifest, a replay stream hash, any persisted replay artifact, and
any calendar, roll-schedule, or continuous-series dependency.

Also not implemented: research session segmentation (RTH/ETH labels over the
calendar), instrument→calendar mapping, calendar facts outside the verified
2023 range, continuous **price** construction and back-adjustment,
volume/open-interest roll rules, instrument specifications, any vendor
symbol-to-identity alias registry, and persistence of roll schedules.

Storage writes and reads single-record-type Parquet files by path. The catalog
sits above it and registers those files by content; there is no partitioning and
no performance claim.

## Requirements

- Windows 10/11
- Python 3.12 (`requires-python = ">=3.12,<3.13"`; 3.11 will not install,
  3.13 is untested and deliberately excluded)

## Setup and run

```powershell
py -3.12 -m venv .venv
.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -e ".[dev]" -c constraints.txt
quant-research-terminal
```

`constraints.txt` pins the exact version of every package in the supported
environment, so two installs from the same commit resolve identically. See
`docs/adr/ADR-008-environment-reproducibility.md` for the policy and the
regeneration procedure.

Alternative launch:

```powershell
python -m quant_research_terminal
```

## Verification

```powershell
python -m pip install -e ".[dev]" -c constraints.txt
python -m pytest
python -m ruff check .
python -m mypy src tests
```

## Environment report

When reporting a defect or recording an experiment environment, capture:

```powershell
python -c "import sys, platform; print(sys.version); print(platform.platform())"
python -m pip freeze
python -c "import zoneinfo; print(zoneinfo.ZoneInfo('UTC'))"
git rev-parse HEAD
```

The third line verifies the IANA timezone database is available — required for
materializing the storage layer's UTC timestamps through PyArrow or Polars.
The Git command is optional context, not a runtime dependency; the application
never invokes Git.

See `PROJECT_CHARTER.md`, `docs/architecture.md`, `docs/roadmap.md`, and
`docs/adr/ADR-001-project-foundation.md` for scope and architectural decisions.

## Deliberate limitations

- **No vendor acquisition.** Providers decode *archived* files only. Nothing
  authenticates or opens a socket, because a live API cannot be deterministic
  and determinism is the point.
- **The ThetaData decoder is unverified.** It was written without inspecting a
  real export; every vendor assumption is registered in `docs/data-import.md`
  and none is confirmed. `provider.verification_status` reports this.
- **Storage is a file API, not a database.** One record type per file, no
  catalogue, no partitioning, no query layer.
- **Reproducibility machinery does not exist yet.** There is no experiment
  tracker, seed management, or dataset versioning beyond the storage schema
  version.

