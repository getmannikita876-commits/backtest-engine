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

- Real Databento and ThetaData integrations. Both modules are interface-only
  stubs with no credentials, no network calls, and no vendor SDK dependency.
- Options records, which require new domain contracts first.

The approved import rules are documented in `docs/data-import.md`.

Remaining before Phase 1.3 can be considered complete:

- A provider registry and configuration-driven selection, with credentials
  sourced from the environment and never from source control
- A richer instrument identity to support futures contract rolls
- A validation report for the streaming path

## Later phases (gated)

Deterministic replay, strategy APIs, execution simulation, experiments, and
analytics begin only after the import foundation is accepted. Options, Rust
migrations, AI assistance, Monte Carlo, optimization, and the Prop Firm
Evaluation module remain explicitly out of scope until their prerequisites are
correct and tested.
