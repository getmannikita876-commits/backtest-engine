# Roadmap

## Phase 0 — Project Foundation (complete)

- Runnable package and Windows setup instructions
- PySide6 shell with six placeholder pages
- Quality-tool configuration and smoke tests
- Architecture and decision records

## Phase 1.1 — Domain contracts (complete)

- Immutable domain models with UTC-only timestamps
- Decimal-based prices and strictly positive quantities

## Phase 1.2 — Storage contracts (complete, audited)

- Deterministic storage contracts for Arrow and Polars with schema metadata
- Fixed-point decimal price encoding with explicit precision and scale
- Regression tests covering boundary conditions and round-trip semantics
- Packaging and tooling alignment for editable installs, Ruff, MyPy, and Pytest

## Phase 1.3 — Data import interfaces and validation pipeline (in progress)

Delivered so far — the **provider and validation foundation only**:

- Vendor-neutral provider abstraction; `fetch` returns a closeable `RecordStream`
- Minimal CSV provider (decoding only, no validation or normalization)
- Validation rules with single authoritative owners: schema, timestamp, value,
  duplicate, and ordering
- Normalization to domain objects, and orchestration that owns no rules
- Enforced layering via import-graph tests
- Hardened contracts: batch-fatal schema versions, distinct timestamp issue
  codes, strictly positive quantities, typed duplicate policy, and the complete
  event ordering key

Explicitly **not** implemented in this phase:

- Real vendor integrations. Both vendor modules were interface-only stubs with
  no credentials, no network calls, and no vendor SDK dependency.
- Options records, which require new domain contracts first.

The approved import rules are documented in `docs/data-import.md`.

Remaining before the import foundation can be considered complete:

- A provider registry and configuration-driven selection, with credentials
  sourced from the environment and never from source control
- A richer instrument identity to support futures contract rolls
- A validation report for the streaming path

## Phase 1.4 — Databento archived delimited export decoding (in progress)

This phase implements **one narrow capability**, not a general Databento
integration:

| Capability | Status |
| --- | --- |
| Archived delimited (CSV) export decoding | **implemented** |
| Binary DBN decoding | not implemented |
| Historical API acquisition | not implemented |
| Live API acquisition | not implemented |
| Credential management | not implemented |

Delivered:

- Decoding of archived Databento delimited exports into raw records
- Vendor field semantics isolated in a pure, independently testable module
- Approved rules: `ts_recv` default to avoid look-ahead bias, bar timestamps
  carrying interval-close availability time (ADR-002), explicit
  sub-microsecond precision policy, strict symbology with conflict detection,
  sentinel handling, and no guessing of an unattributed trade side
- Shared file-reading plumbing extracted so providers do not duplicate it

Not implemented, with reasons:

- **Acquisition of any kind.** A live API cannot be deterministic, so it would
  break replay reproducibility. Acquisition is a legitimate future component
  that belongs outside deterministic decoding.
- **Binary DBN.** It would need the vendor SDK, which is absent here, so any
  decoder written against it would ship unverified. This is a practical
  constraint, not an architectural one — a future DBN backend may use the SDK
  as an **optional dependency isolated inside the Databento provider package**
  without compromising provider independence.
- **ThetaData**, which remains an interface-only stub.

Open contract question, resolved in Phase 1.5: ADR-002's proposed domain change
was accepted.

## Phase 1.5 — Domain time hardening (in progress)

Hardens the audited domain and storage contracts before another provider is
built on them.

- **Bar temporal model.** `Bar` stores `interval_start` and `interval`;
  `availability_time` is a derived property, with `timestamp` as its alias.
  Look-ahead is impossible by construction rather than by validation — there is
  no field in which a wrong availability could be placed.
- **`TradeSide` enum** (`BUY`, `SELL`, `UNKNOWN`) replaces the unconstrained
  `side` string. `UNKNOWN` is a recorded fact and is never inferred; an
  unrecognised vendor code is rejected rather than folded into it.
- **Storage `SCHEMA_VERSION` 1 → 2.** Bars gained `interval_microseconds`; the
  `side` column is constrained to the `TradeSide` vocabulary. Version-1 data is
  rejected, not migrated — a version-1 bar records no interval, so migrating it
  would mean guessing one.
- New validation codes `invalid_bar_interval` and `invalid_trade_side`, so both
  defects are diagnosable rejections rather than raw model errors during
  normalization.
- ADR-002 accepted, with the revision that availability is derived rather than
  stored.

Unchanged in this phase: no replay engine, execution engine, strategy SDK,
ThetaData, options, Monte Carlo, or prop-firm evaluation. The Databento
provider was updated only as far as the new contracts required.

Still open: the nominal session close remains calendar-unaware.

## Phase 1.6 — ThetaData archived provider (in progress)

Implements deterministic decoding of archived ThetaData exports through the
existing provider architecture. No frozen contract required changing.

| Capability | Status |
| --- | --- |
| Archived delimited (CSV) export decoding | **implemented** |
| Trade, quote, and OHLC schemas | **implemented** |
| Live / Theta Terminal API access | not implemented |
| Credential management | not implemented |
| Options, Greeks, implied volatility | not implemented |
| Order-book depth | not implemented |

Decisions worth noting:

- **Session timezone is a required argument.** ThetaData timestamps are
  exchange-local `date` + `ms_of_day`, so an instant cannot be derived without
  a zone. Ambiguous and nonexistent local readings are rejected rather than
  resolved arbitrarily.
- **OHLC timestamp meaning is a required argument.** The vendor's convention is
  not documented to a standard worth assuming, and misreading it would shift
  every bar by one interval.
- **Every trade is `TradeSide.UNKNOWN`** unless the operator supplies a code
  map, because the archived trade schema publishes no aggressor.
- Symbol reconciliation was extracted to `file_source.py` and is now shared
  with the Databento provider rather than duplicated.

No provider is an interface-only stub any more.

### Format verification gate

The decoder was written without inspecting a real ThetaData export, so it is
marked **experimental and not production-compatible**:
`provider.verification_status` returns `experimental-unverified`.

- Every vendor assumption is registered and classified in `docs/data-import.md`.
  **None** is verified by a primary source or a sample file; all are inferred,
  or explicitly unknown and converted into required operator declarations.
- The repository's ThetaData tests do not count as vendor verification — they
  were written from the implementation.
- A read-only inspector settles the assumptions against a real file:
  `python -m quant_research_terminal.data_import.providers.thetadata_inspection FILE`.
  It reports observations only, and never guesses or repairs.
- No vendor market data is committed.

**Real sample files are still required** before this provider can be relied on.

## Phase 1.7C — Real Arrow/Parquet storage round trip (in progress)

The storage layer now touches real files. Its guarantees were previously
theoretical; this phase makes them observable.

Delivered — `data/parquet_store.py`:

- `write_trades`/`read_trades`, `write_quotes`/`read_quotes`,
  `write_bars`/`read_bars`, plus `read_schema_metadata`
- **One record type per file.** The Arrow schemas carry no discriminator, so a
  mixed file could not be read back without inventing one
- **Atomic writes** via a derived `.partial` name plus `os.replace`; an existing
  target survives a failed write and no orphaned temporary is left behind
- **Explicit Parquet settings** — snappy, format 2.6, dictionary encoding off,
  fixed row-group size — documented in `docs/data-contracts.md`
- **Semantic determinism guaranteed**; byte identity verified for the pinned
  PyArrow version and not promised beyond it
- Contract violations raise `StorageContractError` rather than leaking raw
  Arrow/Parquet errors; genuine filesystem errors keep their own exceptions

Verified by real IO, not by mocks: uint64 maximum, fixed-point maximum,
microsecond timestamps, all three `TradeSide` values, bar interval and derived
availability time, row order, and repeated identical trades remaining repeated.

### Findings from doing real IO

- **`tzdata` is absent on this platform**, so PyArrow's and Polars' conversion
  of a zone-aware timestamp to a Python object raises `ZoneInfoNotFoundError`
  for the name `UTC` itself. The store reads the microsecond count and rebuilds
  the `datetime` against `datetime.UTC`, taking no dependency.
- **Polars compatibility is partial by design.** It reads the written types and
  values exactly; domain reconstruction from Polars is not implemented and is
  not claimed.

Not implemented: DuckDB, dataset catalogue, partitioning, caching, query layer,
performance work.

## Phase 1.7B — Numeric domain envelope (in progress)

Unifies the numeric contract. Recovers work that a previous session reported as
complete but which never reached Git — the repository was the authority, and the
envelope was rebuilt from it.

One envelope, defined in `domain/numeric.py`, consumed by domain models, import
validation, and storage conversion. See ADR-004 (**Accepted**).

- **Invariant established:** every constructible domain object is
  storage-encodable — its numeric fields encode exactly into schema v2 with no
  rounding, truncation, overflow, or float coercion.
- **Six mismatches closed**, where the domain accepted a value storage rejected:
  magnitude overflow, sub-tick precision, fixed-point overflow, trailing zeros,
  fractional quantities, and uint64 overflow.
- **Fixed a defect the audit did not find:** the precision rule trapped
  `Rounded`, which fires on trailing zeros carrying no information, so *every
  Databento-decoded price was unstorable*. The rule now traps `Inexact`.
- Separate price and quantity envelopes: maximum exact fractional precision for
  prices is 6 decimal places (trailing zeros beyond that accepted, a non-zero
  digit rejected); quantities are whole counts.
- Magnitude is checked before precision, so an enormous value is no longer
  reported as having too many decimal places.
- Constants moved from `data/contracts.py` down to the domain, which may not
  import storage; `data/contracts.py` re-exports them. A test asserts no module
  redefines them.
- **No schema bump.** The serialized representation is unchanged; version-2
  files remain valid. Recorded as a clarification of version 2.

Compatibility: the domain is stricter, so fractional quantities and sub-tick
prices now fail at construction rather than at save time. Bar volume reports
quantity issue codes rather than `non_decimal_price`. Storage raises
`NumericEnvelopeError` rather than a `ValueError`/`OverflowError` pair.

Still open: signed values (PnL, deltas) have no envelope; fractional quantities
would need a schema change.

## Phase 1.8B — Bar identity and conflict semantics (in progress)

Fixes the audit-confirmed defect that two bars for the same instrument and
period with *different* OHLCV values were silently accepted, while identical
copies were correctly flagged — the harmless case caught, the harmful one
missed. See ADR-005 (**Accepted**).

- **Bar identity is the period**: `(instrument_symbol, interval_start,
  interval)`. OHLCV values are claims about the period, not part of the
  identity.
- **Exact duplicate and conflict are separate concepts.** Identical claims
  remain a `duplicate_row` warning resolved by `DuplicatePolicy`; differing
  claims are a `conflicting_bar` **error** on every member of the group, so
  no copy survives and no policy can pick a winner.
- Neither copy of a conflict is retained: the layer cannot tell a vendor
  correction from a stale double-fetch, and guessing would convert a visible
  contradiction into an invisible bias. Resolution belongs to the operator.
- Trades remain non-deduplicated (ADR-003 unchanged); quote semantics are
  unchanged — a quote's identity spans every field, so quote conflicts are
  unconstructible.
- No schema change; identity and conflict are import-validation concepts.

## Phase 1.8C — Environment and reproducibility hardening (in progress)

Closes the audit-confirmed gap that the environment was the least reproducible
part of a reproducibility platform. See ADR-008 (**Accepted**).

- **`tzdata` declared as an unconditional runtime dependency.** Reproduced
  first: on a clean Windows environment `ZoneInfo("UTC")` raised, PyArrow
  `as_py()` failed, and Polars `to_dicts()` panicked at the Rust level while
  materializing the repository's own stored timestamps. The storage layer's
  microsecond-count reconstruction is retained as defence in depth.
- **Python 3.12 everywhere.** Ruff `target-version` corrected from `py311`;
  README and ADR-001 corrected from "3.11+"; a regression test now asserts
  the interpreter, `requires-python`, and every tool target agree.
- **`constraints.txt`**: exact version pins for the whole environment,
  consumed by developers and CI (`pip install -e ".[dev]" -c
  constraints.txt`). Version-exact, not hash-exact — stated honestly in
  ADR-008; hash locking is future work.
- **`.gitattributes`**: LF in repository and working tree (CRLF only for
  Windows shell scripts, binary formats protected). Introduced with zero
  rewritten blobs — the index already stored LF everywhere.
- **`py.typed`** marker added and verified present in the built wheel.
- CI installs through the constraints file and prints interpreter, platform,
  frozen package versions, and timezone-database resolution on both runners.

## Phase 1.9 — Application layer and first vertical slice (in progress)

The first source-code path connecting the individually verified layers. See
ADR-007 (**Accepted**).

- New `application/` package: `ImportDatasetUseCase` orchestrates provider →
  validation → normalization → deterministic ordering → Parquet write →
  read-back → structural equality verification → `ImportDatasetResult`.
  Orchestration only; every rule stays in its owning layer.
- **Transaction boundary:** the dataset is staged beside the target, verified
  by reading it back through the real read path, then promoted with one
  atomic replace. No failure mode creates or replaces the target; an existing
  dataset survives every failed run byte-for-byte.
- One record type per dataset, enforced before validation; mixed streams are
  refused with an application error.
- True integration tests over a committed `ESM6` CSV fixture (specific
  contract symbol — an interim string pending the Phase 2.0 instrument
  model): trades, quotes, bars, conflicting bars, invalid values, duplicate
  headers, empty sources, stream closure, existing-target preservation, and
  repeat-run determinism including byte identity in the pinned environment.
- **Honest limits:** records are materialized in memory before validation
  (documented, unmeasured, no tick-scale claim); provenance does not survive
  into domain objects or Parquet (recorded for Phases 2.0/2.3); no dataset
  catalog, no manifest, no rollover, no replay, no UI integration yet.

## Phase 2.0 — Futures instrument and contract identity (in progress)

Replaces the bare `instrument_symbol` string as the platform's notion of *which
futures contract this is*. See ADR-009 (**Accepted for the domain model;
persistence deliberately deferred**).

- **`FuturesContractId`** = `FuturesProduct` (a `Venue` plus a product root)
  plus a `ContractMonth` plus a **full four-digit year**. Canonical form
  `CME:ES:M2026`. Immutable, hashable, strictly validated, `@final`.
- **A product is not executable.** `ES` is a `FuturesProduct`; only a
  `FuturesContractId` passes `require_listed_contract`, the guard future
  execution code calls. A continuous series must be its own type and fails that
  guard without the guard knowing it exists.
- **`ESM6` never means 2026 by inference.** The one expansion function requires
  an explicit decade and has no default, so no code path can depend on the
  current date. The committed `ESM6` fixture is dated March 2024 and is
  consistent with several delivery years; its docstring previously claimed
  "June 2026" and no longer does.
- **Nothing is normalized.** `"es"` and `" ES"` are rejected rather than
  trimmed or upper-cased. `parse()` is the exact inverse of `canonical()`.
- **Identity excludes specification and provenance.** No specification type
  ships — the repository has no authoritative source for one.
- **Venue is a shallow namespace token**, explicitly not a MIC, exchange group
  name, or vendor venue code. The limitation is documented rather than papered
  over with a guessed hierarchy.

**Four defects reproduced and fixed**, each with regression tests:

1. Normalization coerced the instrument symbol with `str()` while duplicate
   detection compared the raw value, so records carrying `None` and `"None"`
   had different import identities but one domain instrument — **two
   conflicting bars for the same period were accepted with `success=True`**, a
   live hole in ADR-005 that silently double-counted volume.
2. Pydantic's `model_copy` skipped validation, so an identity could be forged
   whose `canonical()` emitted a string its own `parse()` rejects.
3. A `str` subclass with overridden equality split one instrument in two,
   reproducing defect 1 one level down.
4. A runtime subclass of `FuturesContractId` passed the executable guard.

**No schema change.** Schema v2 stores one `utf8` `instrument_symbol` column
and cannot represent venue, product, delivery month, or full year; `"ESM6"`
cannot be resolved into canonical identity without guessing a venue and a
decade. `SCHEMA_VERSION` stays at 2, the vertical slice still persists only the
legacy string, and two tests assert that limit. Full persistence needs a future
schema v3 with an explicit, operator-declared migration.

Not implemented in this phase: exchange calendars, sessions, rollover,
continuous futures, back-adjustment, dataset catalog, replay, execution,
instrument specifications, and any alias registry or vendor symbol parser.

## Later phases (gated)

Deterministic replay, strategy APIs, execution simulation, experiments, and
analytics begin only after the import foundation is accepted. Options, Rust
migrations, AI assistance, Monte Carlo, optimization, and the Prop Firm
Evaluation module remain explicitly out of scope until their prerequisites are
correct and tested.
