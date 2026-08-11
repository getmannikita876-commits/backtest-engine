# Architecture

The project uses a small `src`-layout Python package. `app.py` owns application
lifecycle; `ui/main_window.py` owns the desktop shell. Each visible section is
currently a placeholder page with no engine or data-vendor behavior.

## Layers

Dependencies point inward, and never the other way:

```
UI -> Application -> Data Import -> Storage -> Domain
                                   Calendars -> Domain
                          Catalog -> Storage -> Domain
```

`calendars/` is a data package beside the pipeline: it ships versioned
calendar-definition resources and their strict loader, depends only on the
domain, and nothing in the domain depends back on it.

- **Domain** (`domain/`) — immutable models with validation only: no IO, no GUI,
  no provider-specific code. It is the foundation and depends on nothing above it.
  Where an invariant can be made structural it is: a bar's availability time is
  a derived property rather than a stored field, so a bar that would be visible
  before its interval closes cannot be constructed at all. Closed vocabularies
  are enums (`TradeSide`, `ContractMonth`), not strings. The **canonical numeric
  envelope** lives here too (`domain/numeric.py`) and is enforced on
  construction, so a constructible domain object is a storage-encodable one.
  Storage and the import layer both consume it; neither defines a numeric rule
  of its own. **Canonical futures identity** (`domain/futures_contract.py`)
  lives here for the same reason — see below.
- **Storage** (`data/`) — storage contracts and Parquet persistence. Converts
  domain models into deterministic Arrow rows with explicit schema metadata and
  fixed-point decimal encoding, and reads and writes single-record-type Parquet
  files (`parquet_store.py`). Writes are atomic; reconstruction goes through the
  ordinary domain constructors, so a file is treated as untrusted input. There
  is no partitioning, query layer, or DuckDB. See `docs/data-contracts.md`.
- **Catalog** (`catalog/`) — dataset manifests, provenance, explicit v2→v3
  migration, registration, and a rebuildable location index (ADR-012). It sits
  *above* storage and the domain and is imported by neither. It composes the
  domain's identity types with the storage layer's files; it owns no market-data
  semantics of its own. See below and `docs/data-contracts.md`.
- **Data Import** (`data_import/`) — providers, validation, normalization, and
  the orchestration that sequences them. See `docs/data-import.md`.
- **Application** (`application/`) — use cases that orchestrate the layers
  below; owns no business rules (ADR-007). The first and currently only use
  case is `ImportDatasetUseCase`: provider → validation → normalization →
  deterministic ordering → Parquet write → read-back → structural
  verification, published atomically only when fully verified. It accepts any
  `MarketDataProvider` through the existing interface, never imports a
  concrete vendor module, and calls the concrete storage functions directly —
  a storage port is deliberately deferred until a second backend exists. Its
  boundary surface is declared in `application/ports.py`.
- **UI** (`ui/`) — displays data and starts operations; contains no business
  logic and does not reach into the import or storage layers. It will call
  the application layer; it does not yet.

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
timestamp semantics, quantity positivity, instrument-symbol usability
(ADR-009), duplicate and bar-conflict handling (ADR-005), and the complete
event ordering key — are recorded in `docs/data-import.md`.

Leaf rule modules (`time_semantics`, `numeric_semantics`,
`instrument_semantics`) each own one question and depend on no stage that
consumes them, so a validator and a normalizer can never disagree about the
same rule. They disagreed once: normalization coerced an instrument symbol with
`str()` while duplicate detection compared the raw value, which let two
conflicting bars for one period pass as distinct records (ADR-009).

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
Storage is at `SCHEMA_VERSION = 3`, with `LEGACY_SCHEMA_VERSION = 2` still
written and read unchanged. Version-1 data is rejected rather than migrated,
because a version-1 bar records no interval; version-2 data is migrated only on
an explicit, operator-supplied alias→contract mapping.

## Futures instrument identity

A specific listed futures contract is identified by
`FuturesContractId` in `domain/futures_contract.py`, not by a symbol string.
See `docs/adr/ADR-009-futures-contract-identity.md`.

```
Venue                  a market namespace token
  └── FuturesProduct   venue + product root            e.g. CME:ES
        └── FuturesContractId   product + delivery month + FULL year
                                canonical: CME:ES:M2026
```

The distinctions the model exists to keep apart:

| Concept | Type | Executable? |
| --- | --- | --- |
| Product / root (`ES`) | `FuturesProduct` | **no** |
| Listed contract (`ESM6`) | `FuturesContractId` | yes |
| Vendor alias (`ES1!`, a numeric id) | none — an alias is not an identity | — |
| Synthetic / continuous series | not implemented; must be its **own** type | **no** |

`require_listed_contract(value)` is the guard execution-side code calls; only a
`FuturesContractId` passes it.

Properties, all tested rather than asserted in prose:

- **The full contract year is always explicit.** `ESM6` is an abbreviation and
  is never expanded by inference. The one expansion function,
  `resolve_abbreviated_contract_year`, requires the caller to state the decade
  and has no default, so no code path can acquire a dependency on the current
  date. `ESM6` does **not** mean 2026 here — including in the committed fixture.
- **Nothing is normalized.** `"es"` and `" ES"` are rejected, never trimmed or
  upper-cased, so two source spellings cannot silently become one instrument.
- **Canonical serialization is deterministic** — byte-identical across processes
  under different `PYTHONHASHSEED` values, independent of locale and timezone —
  and `parse()` is its exact inverse, neither wider nor narrower.
- **Identity excludes specification** (tick size, multiplier, currency, fees,
  margin, settlement, hours, expiry dates) **and provenance** (provider, file,
  row, dataset). Neither is modelled yet; both are separate dimensions.
- **Venue is a deliberately shallow namespace token** — not a MIC, not an
  exchange group name, not a vendor venue code. The repository has no
  authoritative venue registry, and inventing a hierarchy would be fake
  precision. Using one token per market consistently is currently the
  operator's responsibility.

**Schema v2 does not persist canonical identity.** It stores one `utf8`
`instrument_symbol` column, and `"ESM6"` carries no venue and no full year, so
canonical identity cannot be reconstructed from a v2 file without guessing.
**Schema v3 does** (ADR-012): it replaces that column with `canonical_identity`
plus `vendor_symbol`, and records identity in the schema metadata too, because an
empty artifact has no rows to carry it. The upgrade path is an explicit
alias→contract mapping supplied by the operator, never an inference.

## Exchange calendar

The calendar subsystem (ADR-010) separates authored facts from generic
mechanics, and physical exchange state from research labels:

```
CalendarDefinition            TOML data + strict models (facts, evidence refs)
      │ materialize()         deterministic; the only local-time arithmetic
      ▼
MaterializedCalendar          explicit half-open [start, end) UTC windows,
      │                       content-hashed for reproducibility pinning
      ▼
CalendarResolver              pure bisect lookups; UTC-only inputs;
                              no tzdb dependence at query time
```

- **Facts are data.** Every schedule fact — weekly session windows, holiday
  exceptions, early closes — lives in a versioned TOML definition
  (`calendars/definitions/`) with per-rule verification status and evidence
  citations; the narrative register is `docs/calendar-evidence.md`. Engine
  code contains no exchange's schedule.
- **Exchange tradability ≠ research segmentation.** `ExchangeTradingState`
  (TRADING / HALT / MAINTENANCE / CLOSED) describes what the matching engine
  was doing. RTH/ETH-style research sessions are a future layer *over* the
  calendar and can never change tradability.
- **`TradingDate` is assigned, never derived.** The type rejects `datetime`
  outright, and windows carry CME's own trade-date labels — a Sunday-evening
  session can belong to Tuesday across a holiday, which no date arithmetic
  produces.
- **Unknown is unsupported.** Instants outside the materialized coverage and
  dates outside the supported range raise typed errors; nothing extrapolates
  a "normal day". The shipped `CME_EQUITY_INDEX` v1 supports trading dates
  2023-05-22 … 2023-12-29, the exact span its CME evidence covers.
- **DST is explicit.** Rule boundaries naming nonexistent or ambiguous local
  times fail materialization with typed errors; declarative tzdb probes make
  a divergent zoneinfo source fail loudly; the resolver itself never touches
  local time.

## Futures rollover and continuous mapping

The rollover subsystem (ADR-011) sits beside the calendar and consumes it:

```
Explicit / FixedCalendar RollDefinition    authored facts (+ calendar pin)
      │  deterministic materialization
      ▼
RollSchedule                               explicit UTC events, content-hashed
      │
      ▼
RollResolver                               pure bisect lookups
```

- **Mapping, not price synthesis.** The layer answers *instant → listed
  contract*. Back-adjustment, ratio/Panama methods, and synthetic OHLC are not
  implemented and have no placeholder API; they change what the numbers mean
  and belong to a later research layer.
- **Listed ≠ synthetic.** `ContinuousSeriesId` is its own type, canonically
  `CME:ES:CONTINUOUS:ACTIVE` (four fields against a listed contract's three),
  and fails `require_listed_contract` automatically.
- **Total coverage is structural.** Exactly one contract is active at every
  instant of the supported range, as a consequence of `bisect_right` over
  strictly increasing effective times — the validators exist to prevent silent
  *loss* (a duplicate instant erasing an event, a `from_contract` naming a
  contract that was never active).
- **Decision time is kept apart from effective time**, so a future data-driven
  rule can prove it consumed no information published after its decision.
- **The calendar travels with the schedule.** A schedule pins the calendar's
  id, version, and content hash, and segmentation verifies them — roll instants
  are only meaningful against the trade-date labelling that produced them.
- **Trading dates are never collapsed**: `segments_for_trading_date` returns
  ordered segments carrying exchange state, because a roll can land
  mid-session.
- **Structural versus factual guarantees are stated, not implied.**
  `RollSchedule` is unconstructible if structurally invalid; consistency with a
  calendar and with lifecycle facts is checked in the materializers, so a
  directly constructed schedule carries the structural guarantees only.

## Dataset identity and the catalog

The catalog (ADR-012) answers *exactly which immutable canonical market-data
dataset artifact is this?* — without a filename, a path, a provider's state, a
clock, or any inference from a vendor alias.

```
domain/dataset_identity.py   RecordType + the three hash value objects + the
      ▲                      framed semantic encoding (pure; no IO)
data/  v3 schemas · v3 read/write · physical byte hashing
      ▲
catalog/  manifest · provenance · migration · registration · rebuildable index
```

- **Three identities, never collapsed.** `SemanticDatasetHash` (the data),
  `PhysicalArtifactHash` (the bytes), `ManifestHash` (the claims and
  provenance). Keeping them apart is what lets verification distinguish a
  harmless re-encode from an edited dataset; collapsing any pair would make the
  distinction unavailable.
- **Identity is content-addressed.** No UUID, no randomness, no wall clock
  anywhere in an immutable identity, enforced by the architecture sweep rather
  than by convention. Registering the same artifact twice yields the same
  identity.
- **Location is not identity.** `DatasetManifest` has **no path field at all**,
  so relocation cannot change identity by construction rather than by policy.
  Paths live only in the rebuildable index.
- **Nothing is trusted.** Registration recomputes schema version, record type,
  contract, row count, time bounds, vendor aliases, and both hashes from the
  artifact. There is no partial registration.
- **One semantic hash, many manifests.** Identical records from different
  vendors or under different Parquet encodings share semantic identity while
  differing physically and in provenance, so lookup returns a deterministic
  tuple and never silently drops an alternative.
- **The index is not authority, and stores only what the manifests do not.**
  Resolution is two hops: `ManifestHash → PhysicalArtifactHash` from the
  immutable manifest, then `PhysicalArtifactHash → locations` from the index. A
  manifest is never bound to a path, so `rebuild_index` is total — it hashes
  candidate artifacts and records where the bytes are, and several manifests
  pinning one physical hash all resolve through any surviving copy. Lookup by
  semantic hash reads the published manifests, so it works with no index at all.

## Correction lineage and cross-batch comparison

Datasets are immutable; corrections are real. The reconciliation is that a
correction is a **separate immutable claim beside the data**, never a change to
it (ADR-013).

```
domain/dataset_lineage.py    pure: SupersedesRelation + its hash + provenance
      ▲
catalog/lineage.py           publication · rebuildable index · graph navigation
catalog/comparison.py        explicit dataset-to-dataset comparison
```

- **A separate artifact, not a manifest field.** A lineage claim is usually made
  *after* both manifests exist and a manifest is immutable, so there is no moment
  at which such a field could be filled — and the manifest hash covers every
  field, so adding one would change a dataset's identity because something was
  later said *about* it.
- **Exact semantics.** `successor` was explicitly declared by an operator to
  correct every manifest in `predecessors`. Nothing more: not preferred, not
  latest, not automatically selected, and the predecessors stay valid.
- **Old pins never redirect.** After `B supersedes A`, `read_manifest(A)` is
  exactly A with its three hashes unchanged; B appears only via
  `successors_of(A)`. There is no `latest`, `current`, or `preferred` API, and an
  architecture test fails the build if such a name appears.
- **Edges run predecessor → successor.** Branching (`A → B`, `A → C`) and
  multi-parent (`A, B → C`) are both first-class; cycles are refused by a
  deterministic, iterative check over *all* published relations.
- **Predecessors are a set**, sorted by digest before hashing, so caller order
  cannot produce two identities for one claim.
- **The lineage index is a cache that navigation does not read.** The relation
  files *are* the graph — each named by its content hash, each naming its own
  endpoints — so an index can only restate them, and the restatement is the half
  that goes stale. Deleting it changes no answer; that is what makes "never
  authority" a fact rather than an aspiration.
- **Comparison is explicit and ephemeral.** It reports evidence — exact-row
  multiplicities, reordering, bar-period agreement and conflict — and returns a
  plain value object that is never published, hashed, or indexed. Nothing in
  import consults it, so import has no ambient dependency on catalog contents.
- **Only bars have a provable natural key** (contract + period, ADR-005). Trades
  and quotes have no logical event identity (ADR-003), so no correction is ever
  inferred for them and a shared timestamp is not treated as one.

## Research invariants

Market timestamps, exchange calendars, dataset provenance, configuration
snapshots, and random seeds must remain explicit before replay or backtesting is
implemented.

A future `RunManifest` can pin its inputs with each dataset's
`SemanticDatasetHash` (the strongest research-data pin) and `ManifestHash` (which
adds provenance), plus — separately, and only where relevant — a calendar content
hash (ADR-010) and a roll-schedule content hash (ADR-011). All four are exposed
today; none of that orchestration is implemented.

Publishing lineage **after** a run does not alter that pin — the run consumed the
bytes it consumed. A future tool may surface "this dataset has successors" beside
a result as information, without changing any execution input.

The UI must never become the source of research truth: experiments will
eventually be represented by serializable configurations and immutable result
metadata so runs can be repeated without the desktop interface.
