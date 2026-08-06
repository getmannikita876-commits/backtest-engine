# ADR-007: Application layer and the first vertical slice

- Status: **Accepted**
- Date: 2026-08-06
- Related: ADR-002 (bar availability time), ADR-003 (trade identity), ADR-005
  (bar conflicts), ADR-008 (environment reproducibility, which reserved this
  number), `docs/architecture.md`, `docs/data-import.md`

## Context

After Phases 1.3–1.8C the repository held individually verified components —
providers, validation, normalization, deterministic ordering, domain models,
Parquet storage — with **no source-code path connecting them**. The
post-storage foundation audit rated this the blocking gap: every guarantee the
platform claims is a guarantee about a *pipeline*, and no test crossed a
single seam. `write_trades` had no caller anywhere in `src/`.

The architecture documents also named an Application layer between UI and the
lower layers that did not exist as a package.

## Decision

### Why the Application layer exists

The layer exists to own precisely the decisions that live *between* layers and
that no single lower layer is positioned to make:

- the **sequencing** of provider → validation → normalization → ordering →
  storage;
- the **transaction boundary**: a dataset is either published fully verified
  or the target path is untouched;
- the **one-record-type-per-dataset policy**, enforced before validation;
- **read-back verification**: written records equal reconstructed records — a
  fact about the whole pipeline, not about any layer.

### Orchestration only — forbidden knowledge

The application layer owns **no business rules**. It must not validate a
price, define bar identity, decide a duplicate's fate, decode a vendor
timestamp, encode a fixed-point value, or know a Parquet schema. All of those
remain in their owning layers and reach the application only through their
public APIs. Architecture tests enforce the boundary from both sides.

It must also never import Qt or anything under `ui`; the dependency points
the other way — UI will later call the application layer, not the layers
below it.

### Dependencies

Allowed: `data_import` public APIs (contracts, the batch pipeline, the raw
record adapter), the vendor-neutral provider **interface** module
(`data_import.providers.provider`), the storage public API
(`data.parquet_store`), and domain types. Forbidden: concrete provider
implementations, `ui`, and being imported by anything below it. The boundary
surface is declared once in `application/ports.py`.

Providers are supplied by the caller as `MarketDataProvider` instances. The
provider *stream* is owned by the use case from fetch to close: it is consumed
under a context manager, so the underlying handle is released deterministically
on success and on every failure path.

### Storage boundary: a deliberate non-port

The use case calls the concrete Parquet functions directly, dispatched
explicitly by record type. No storage protocol is introduced: there is one
backend, the integration tests are required to run against the real one, and a
port with a single implementation and no substitution use is an unused
abstraction. If a second backend arrives (DuckDB is the likely candidate), the
port should be shaped then, by the real second implementation.

### Transaction and failure boundary

The use case stages the dataset beside the target
(`<name>.importing`), written through the storage layer's own atomic write,
reads it back through the real read path, verifies structural equality, and
only then promotes it with a single atomic `os.replace`. A `finally` unlink
removes the staging file on every failure path.

Consequences, all integration-tested:

- validation failure, conflicting bars, an empty source, a provider decode
  error: **no file is created**;
- storage failure: the storage layer's own atomicity holds; no staging
  artifact remains;
- read-back mismatch: `VerificationError`, staging discarded, target
  untouched;
- an existing target survives **every** failed run byte-for-byte and is only
  ever replaced atomically by a fully verified dataset.

The storage layer's atomic write guards a single file against partial writes;
this boundary composes that same primitive into "never published unverified".
It does not reimplement the storage layer's mechanism.

### Error contract

Three exceptions, no more: `ApplicationError` (base), `ImportDatasetError`
(validation failure — carrying the full `ValidationReport` — or a stream that
violates the record-type policy), and `VerificationError` (read-back
mismatch). Provider and storage errors propagate **unchanged**: they are the
typed public contracts of their layers, and wrapping them would add
indirection without information. Programmer errors are not caught at all.

### Why no DI framework, service locator, or provider registry

The use case has exactly one collaborator arriving from outside (the
provider) and it arrives as an explicit argument typed by an existing
protocol. A container, locator, or registry would add machinery with zero
current callers — the UI does not exist yet, and when it does, constructing a
provider and passing it is still simpler than registering one. Registries
also invite exactly the implicit, ambient wiring that makes replay
non-reproducible.

### Why one vertical slice

One use case proves the seams; a second use case before the first has a real
caller would be speculation. The slice is deliberately the smallest complete
path that exercises every existing layer for all three record types.

### Current materialization limitation

The stream is materialized into memory before validation because the batch
API — which owns policy application, ordering, and report accounting — is
eager. This is documented as an implementation detail; the public API
(provider in, result out) does not depend on it. No tick-scale performance is
claimed, and no streaming or Polars batching is added before measurement.

### Relationship to later layers

- **Dataset Catalog / Manifest (later phases):** `ImportDatasetResult` is
  *not* a manifest. It records only facts this use case established
  (accounting, path, verified count) and deliberately omits dataset IDs,
  hashes, environment capture, and calendar/rollover metadata.
- **Replay (later phases):** replay will consume datasets this slice
  produces; the slice's ordering is already the replay ordering contract
  (`docs/replay_rules.md`), applied by the existing import pipeline.
- **Instrument identity (Phase 2.0):** the fixture's `ESM6` is a plain
  string. The futures `InstrumentId` model does not exist yet, and this
  interim representation must not be mistaken for it.

### Provenance limitation (recorded for Phases 2.0/2.3)

`RawRecord` preserves `provider_name` and a pre-filter source row index in
stream form. Converting to the eager batch renumbers `source_index` to the
record's batch position, so when a request filters rows the original file row
coordinate is lost to diagnostics; and **no provenance of any kind survives
into domain objects or the Parquet schema** — the domain models and storage
columns have no fields for it. This phase does not smuggle provenance into
unrelated fields and does not modify frozen schemas to force it. End-to-end
provenance is therefore *not claimed* and is recorded as a dependency for the
instrument-identity and dataset-catalog phases.

## Alternatives considered

**Drive the streaming pipeline directly instead of the batch API.** Rejected:
policy application, rejected-row accounting, ordering, and report construction
already live in `validate_import_batch`; re-sequencing them in the application
would duplicate orchestration the import layer owns.

**Write directly to the target and delete it on verification failure.**
Rejected: a failed verification would have already destroyed a pre-existing
dataset, and deletion-on-failure leaves a window where the target is invalid.

**Wrap provider and storage errors in application exceptions.** Rejected:
`ProviderDecodeError` and `StorageContractError` are stable public contracts
whose names carry the diagnosis; a wrapper would subtract information.

## Consequences

Positive: the first executable proof that the layers compose; a transaction
boundary that cannot publish an unverified dataset; a UI integration point
that does not touch lower layers.

Negative: full materialization bounds dataset size by memory until a measured
streaming design exists; the result type will need superseding by a real
manifest; provenance remains import-diagnostic-only until Phase 2.0/2.3.
