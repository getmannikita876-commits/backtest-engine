# Architecture

The project uses a small `src`-layout Python package. `app.py` owns application
lifecycle; `ui/main_window.py` owns the desktop shell. Each visible section is
currently a placeholder page with no engine or data-vendor behavior.

## Layers

Dependencies point inward, and never the other way:

```
UI -> Application -> Data Import -> Storage -> Domain
```

- **Domain** (`domain/`) — immutable models with validation only: no IO, no GUI,
  no provider-specific code. It is the foundation and depends on nothing above it.
  Where an invariant can be made structural it is: a bar's availability time is
  a derived property rather than a stored field, so a bar that would be visible
  before its interval closes cannot be constructed at all. Closed vocabularies
  are enums (`TradeSide`), not strings. The **canonical numeric envelope** lives
  here too (`domain/numeric.py`) and is enforced on construction, so a
  constructible domain object is a storage-encodable one. Storage and the import
  layer both consume it; neither defines a numeric rule of its own.
- **Storage** (`data/`) — storage contracts and Parquet persistence. Converts
  domain models into deterministic Arrow rows with explicit schema metadata and
  fixed-point decimal encoding, and reads and writes single-record-type Parquet
  files (`parquet_store.py`). Writes are atomic; reconstruction goes through the
  ordinary domain constructors, so a file is treated as untrusted input. There
  is no catalogue, partitioning, query layer, or DuckDB. See
  `docs/data-contracts.md`.
- **Data Import** (`data_import/`) — providers, validation, normalization, and
  the orchestration that sequences them. See `docs/data-import.md`.
- **UI** (`ui/`) — displays data and starts operations; contains no business
  logic and does not reach into the import or storage layers.

`tests/test_architecture_boundaries.py` enforces these edges by parsing the
import graph, so a forbidden dependency fails CI rather than eroding quietly.

## Data import

The import layer composes in one direction:

```
Provider -> Raw Records -> Validation -> Normalization -> Domain Objects -> Storage Contracts
```

Providers decode a source encoding and nothing more; validators judge records
and never mutate or repair them; normalizers build domain objects; and
`pipeline.py` orchestrates without owning any rule of its own. `RawRecord` is
deliberately defined outside the `providers` package so validation can consume
provider output without importing vendor code.

The engine never depends on a specific vendor: every provider satisfies the same
`MarketDataProvider` interface, and `fetch` returns a closeable `RecordStream`
so resources are released deterministically rather than by garbage collection.

The approved import rules — stream ownership, batch-fatal schema versions,
timestamp semantics, quantity positivity, duplicate handling, and the complete
event ordering key — are recorded in `docs/data-import.md`.

Vendor coverage is deliberately narrow. The Databento and ThetaData providers
decode **archived delimited exports only**: no binary DBN, no API acquisition,
no credentials, no options. Keeping acquisition out of the import layer is what
allows an archived file to be the reproducible unit of research input. A future
vendor SDK may be an optional dependency confined to its own provider package;
the engine still depends only on `MarketDataProvider`.

Vendor-specific decoding lives in a pure module per vendor
(`databento_decoding.py`, `thetadata_decoding.py`) that performs no IO, so every
encoding rule is testable without a file. Shared file plumbing — delimited
reading, request filtering, symbol reconciliation — lives in `file_source.py`
so the vendors cannot drift apart on it.

Where a vendor's semantics are not documented to a standard the project is
willing to assume, the provider requires the operator to declare them rather
than defaulting: ThetaData's session timezone and OHLC timestamp meaning are
both required arguments, because a wrong default would shift every record
invisibly.

A provider whose vendor assumptions have not been checked against a real export
carries that status as data. The ThetaData provider reports
`verification_status == "experimental-unverified"` and is not
production-compatible; passing tests do not change that, since the tests were
written from the implementation rather than from the vendor. A read-only
inspector (`thetadata_inspection`) exists to settle the assumptions against a
user-supplied file without guessing or repairing anything.

Bars carry both temporal coordinates — `interval_start` and `interval` — and
derive availability time from them, so a completed bar cannot be observed
before its interval closes. See `docs/adr/ADR-002-bar-availability-time.md`.
Storage is at `SCHEMA_VERSION = 2`; version-1 data is rejected rather than
migrated, because a version-1 bar records no interval.

## Research invariants

Market timestamps, exchange calendars, dataset provenance, configuration
snapshots, and random seeds must remain explicit before replay or backtesting is
implemented.

The UI must never become the source of research truth: experiments will
eventually be represented by serializable configurations and immutable result
metadata so runs can be repeated without the desktop interface.
