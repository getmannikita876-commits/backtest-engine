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
- **Storage** (`data/`) — storage contracts only. Converts domain models into
  deterministic Arrow/Polars-compatible rows with explicit schema metadata and
  fixed-point decimal encoding. See `docs/data-contracts.md`.
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

## Research invariants

Market timestamps, exchange calendars, dataset provenance, configuration
snapshots, and random seeds must remain explicit before replay or backtesting is
implemented.

The UI must never become the source of research truth: experiments will
eventually be represented by serializable configurations and immutable result
metadata so runs can be repeated without the desktop interface.
