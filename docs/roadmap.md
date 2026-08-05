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

## Later phases (gated)

Deterministic replay, strategy APIs, execution simulation, experiments, and
analytics begin only after the import foundation is accepted. Options, Rust
migrations, AI assistance, Monte Carlo, optimization, and the Prop Firm
Evaluation module remain explicitly out of scope until their prerequisites are
correct and tested.
