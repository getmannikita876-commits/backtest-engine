# Data Contracts

This document describes the storage schema contracts for trades, quotes, and bars.

## Domain versus storage contracts

Domain models in `src/quant_research_terminal/domain` are the source of truth for business semantics.
Storage contracts in `src/quant_research_terminal/data` define how those domain models are encoded for persistence.
Storage schemas are explicit, stable, and independent of the package version.

## Timestamp semantics

- Persisted timestamps are stored as timezone-aware UTC values.
- The Arrow schema uses `timestamp[us, tz=UTC]`.
- The Polars schema uses `Datetime(time_unit="us", time_zone="UTC")`.
- Naive datetimes are rejected.
- Non-UTC timezones are rejected explicitly rather than silently normalized.
- Microsecond precision is preserved.

## The numeric envelope

One definition governs every layer — domain models, import validation, and
storage conversion. It lives in `quant_research_terminal.domain.numeric`,
because the domain may not depend on storage while every layer above may depend
on the domain. `data/contracts.py` re-exports the constants rather than
declaring them. See `docs/adr/ADR-004-numeric-domain-envelope.md`.

The invariant it exists to guarantee:

> Every constructible domain object is storage-encodable: its numeric fields
> encode exactly into the storage schema — as fixed-point integers and unsigned
> integers — without rounding, truncation, overflow, or coercion through float.

Prices and quantities have **separate envelopes**, because a price is a
fractional decimal on a fixed scale and a quantity is a count:

| | Price | Quantity (size, volume) |
| --- | --- | --- |
| Fields | `price`, `bid`, `ask`, `open`, `high`, `low`, `close` | `size`, `bid_size`, `ask_size`, `volume` |
| Minimum | `0.000001` | `1` |
| Maximum | `999999999999.999999` | `18446744073709551615` |
| Scale | maximum exact fractional precision: 6 decimal places | whole numbers only |
| Finiteness | required | required |
| Positivity | strictly positive | strictly positive |
| Storage | `int64`, `value * 10**6` | `uint64` |

Rejecting fractional quantities is a futures-first decision: contracts are
whole. Fractional units would need a schema change, not a looser validator.

### Maximum exact fractional precision: 6 decimal places

The rule is about information, not raw digit count. A value wider than six
decimal places is accepted when every digit beyond the sixth is a trailing zero,
because removing those loses nothing:

- `5000.250000000` — **accepted**: nine decimal places, but the digits past the
  sixth are all zero, so it represents exactly as `5000.250000`.
- `5000.2500001` — **rejected**: representing it at six decimal places would
  require removing a non-zero fractional digit.

Implemented by trapping `Inexact` and **not** `Rounded`. `Rounded` fires
whenever any digit is dropped, including trailing zeros, so trapping it rejected
exactly representable values — which made every Databento-decoded price, which
arrives at scale nine, unstorable.

### No rounding

A value outside the envelope is **rejected, never adjusted**. Nothing quantizes,
truncates, or clamps. Magnitude is checked before precision, so an enormous
value is reported as out of range rather than as having too many decimal places.

## Price encoding

Persisted prices use a documented fixed-point decimal representation:

- `PRICE_ENCODING = fixed_scale_decimal`
- `PRICE_PRECISION = 18`
- `PRICE_SCALE = 6`
- Storage representation is signed 64-bit integer (`int64`) containing `price * 10^6`.

This encoding preserves exact decimal values for current domain prices.
Conversions reject precision loss, overflow, NaN, and infinity.

## Volume and size encoding

- Trade size, quote sizes, and bar volume use unsigned 64-bit integer storage (`uint64`).
- Negative values are rejected.
- The contract only stores whole numbers for these fields, because size and volume are semantically counts.

## Bar temporal columns

A bar has two temporal coordinates: the period it describes and the instant its
values became knowable. Storage persists:

- `timestamp` — the bar's **availability time**, that is its interval close;
- `interval_microseconds` — the bar's duration, as an unsigned whole number of
  microseconds.

Interval start is deliberately **not** stored: it is exactly
`timestamp - interval`, and persisting both would allow the two to disagree.

`timedelta` is already microsecond-resolution, so the interval encoding is
exact and needs no rounding rule, following the same no-silent-precision-loss
principle as the fixed-point price rules.

See `docs/adr/ADR-002-bar-availability-time.md`.

## Trade side

The `side` column stores a `TradeSide` value: `buy`, `sell`, or `unknown`. A
stored value outside that vocabulary is rejected on read rather than coerced,
so an unrecognised code can never silently become `unknown`.

## Schema versioning

The storage package exposes an explicit schema contract:

- `SCHEMA_NAME = quant_research_terminal.storage`
- `SCHEMA_VERSION = 3`
- `LEGACY_SCHEMA_VERSION = 2`
- `SUPPORTED_SCHEMA_VERSIONS = {2, 3}`

### Version history

| Version | Change |
| --- | --- |
| 1 | Initial trade, quote, and bar contracts |
| 2 | Bars gained `interval_microseconds`; the trade `side` column was constrained to the `TradeSide` vocabulary |
| 3 | `instrument_symbol` replaced by `canonical_identity` + `vendor_symbol`; `record_type` and `canonical_identity` added to schema metadata (ADR-012) |

**Version 2 (ADR-009): canonical futures identity is _not_ persisted.**
Phase 2.0 introduced `FuturesContractId` in the domain — a venue, a product
root, a delivery month, and a full four-digit year, canonically `CME:ES:M2026`.
**Version-2 storage does not carry it.** All three v2 schemas store a single
`instrument_symbol` column of type `utf8`, and that column cannot represent
canonical identity:

| Canonical component | Recoverable from a schema-v2 file? |
| --- | --- |
| Venue | **No** — no column and no metadata field records it |
| Product root | Only by guessing a vendor's symbol syntax |
| Delivery month | Only by guessing a vendor's symbol syntax |
| Full contract year | **No** — `"ESM6"` carries one digit and no decade |

The repository's own fixture is the concrete example: `"ESM6"` with rows dated
March 2024 is consistent with a June 2026 listing *and* with a re-used symbol
from June 2016. Nothing in the file decides between them.

An automatic upgrade would therefore have to invent a venue and guess a decade,
silently giving a file written yesterday a different meaning today — which the
backward-compatibility policy forbids outright. So the legacy
`instrument_symbol` string stays a **vendor alias**, not canonical identity, and
must not be read as one.

**Version 3 (ADR-012): canonical identity is persisted, and never inferred.**
Version 3 replaces `instrument_symbol` with two columns answering two different
questions:

```
timestamp | canonical_identity | vendor_symbol | <payload columns…>
```

`canonical_identity` is exactly `FuturesContractId.canonical()`. `vendor_symbol`
preserves what the source called the instrument — provenance, never truth — and
several aliases may legitimately map to one contract.

Version-3 metadata is the six legacy keys with `schema_version=3`, plus
`record_type` and `canonical_identity`, and the key set must match **exactly**.
Version 2's tolerance of unknown extra keys is grandfathered, because files
already exist under it.

Identity lives in the metadata **as well as** the column, deliberately: an empty
artifact is legal and has no rows to carry it. The reader cross-checks the two,
so they cannot disagree silently — and it checks **every** row, not the first,
because a single divergent row is exactly the corruption the column exposes. The
metadata identity must also round-trip `parse(s).canonical() == s`, so a value
like `"cme:es:m2026"` that every row matched is still refused rather than
becoming a catalog key that cannot be read back.

The honest limit: for an **empty** artifact the per-row check is vacuously true,
so the metadata is the sole uncorroborated identity evidence. What detects
tampering there is the dataset manifest, not the reader.

**Version 3 is additive; version 2 is untouched.** New `write_*_v3`/`read_*_v3`
take an explicit `FuturesContractId`. The v2 writers and readers are unchanged
and still emit and accept version 2, because a v3 artifact needs a contract the
import pipeline has no evidenced way to derive from an alias. `SCHEMA_VERSION`
is 3, but `validate_storage_schema` defaults to **legacy** rather than current —
defaulting to "whatever is newest" is how a v2 file would one day be validated
as v3 — and the import pipeline is pinned to `LEGACY_SCHEMA_VERSION` /
`SUPPORTED_SCHEMA_VERSIONS`, so import acceptance is provably unchanged.

**Version 2 note (ADR-010), no bump: exchange calendars do not touch storage.**
Phase 2.1 added the exchange-calendar subsystem entirely outside the storage
schema: no calendar column exists in any Parquet schema, no metadata field was
added, and no stored file changed meaning. A calendar is pinned by three
strings — `CalendarId`, `CalendarVersion`, and the materialized content hash —
which a future run manifest or dataset-catalog artifact can record without any
schema-v2 reinterpretation. Schema v3 fields are not designed here.

**Version 2 note (ADR-011), no bump: rollover does not touch storage.**
Phase 2.2 added futures rollover and continuous-series mapping entirely outside
the storage schema: no roll, series, or lifecycle column exists in any Parquet
schema, no metadata field was added, and no stored file changed meaning.
`instrument_symbol` keeps its ADR-009 status as a legacy vendor alias — nothing
reads it as canonical identity, and no continuous identity is written into it.
A roll schedule is pinned by its content hash, which a future run manifest can
record alongside the calendar's three-string pin without reinterpreting
anything. Roll schedules have **no persisted artifact format** in this phase;
persistence belongs to the dataset-catalog work.

**Version 3 note (ADR-012): a raw dataset pins no calendar and no roll
schedule.** A `DatasetManifest` has no field that could carry either, and a test
asserts it. Trades and quotes do not depend on calendar materialization, and
bars keep ADR-002's interval and availability semantics untouched. A pin would
only be justified if a dataset *transformation* had consumed one, and none does.

**Version 2 clarification (ADR-004), no bump.** The canonical numeric envelope
narrows what the *domain* accepts; it does not change what storage writes or how
a written file is read. Column names, types, fixed-point encoding, and metadata
are unchanged, and every file previously written under version 2 remains valid —
storage never wrote a value outside the envelope, it rejected them. Bumping
would signal an incompatibility that does not exist.

### Migration impact for version 2

Version-1 data is **rejected, not migrated**. `validate_storage_schema` accepts
only an exact version match, so a version-1 file fails loudly on read.

This is deliberate rather than an omission. A version-1 bar records no
interval, so its single timestamp cannot be resolved into interval start and
availability time without knowing the bar's duration — and that duration is not
recoverable from the row. An automatic migration would have to assume one,
silently reintroducing the look-ahead bias that ADR-002 exists to prevent.

Recovering version-1 bars therefore requires the operator to supply the
interval explicitly: a deliberate act with a recorded value, not an inference.
Version-1 trades and quotes are unaffected in shape, but are rejected by the
version check along with everything else, because the schema version applies to
the file rather than to individual record types.
- Schema metadata is stored with every Arrow schema.
- Versioning is independent of the package version.

### Migration impact for version 3

Version-2 data is **migrated only on explicit instruction**, never automatically.
`catalog.migrate_v2_to_v3` requires the caller to supply a mapping from vendor
alias to exact `FuturesContractId`; every alias in the file must resolve, and an
unmapped one is `UnsupportedMigrationError`. There is no fallback that parses a
symbol, no current-year default, and no month-cycle arithmetic anywhere in the
module — that inference is the thing schema v3 exists to make impossible.

- Several aliases mapping to **one** contract is the ordinary case and is fine.
- A source spanning **two** contracts cannot become one single-instrument
  artifact and is refused rather than silently split.
- Unused mapping entries are refused by default (`allow_unused=True` accepts them
  deliberately), because an unused entry is usually a typo whose real symbol then
  trips the unmapped check and produces a confusing second-order error.
- Migration writes a **new** artifact; the v2 input is opened read-only, and a
  destination resolving to the source is refused. Bytes are not preserved and
  cannot be — reading normalises where storage was lenient, so a source storing
  `"BUY"` produces a v3 file holding `"buy"` — so the honest link back is the
  source artifact's physical hash recorded in provenance.

Schema metadata fields include:

- `schema_name`
- `schema_version`
- `timestamp_timezone`
- `price_encoding`
- `price_precision`
- `price_scale`

Version 3 adds, and requires exactly:

- `record_type` — `trade`, `quote`, or `bar`
- `canonical_identity` — e.g. `CME:ES:M2026`

## Parquet persistence (implemented)

`quant_research_terminal.data.parquet_store` performs the real round trip:

```
domain records -> Arrow table -> Parquet file -> Arrow table -> domain records
```

### API

| Function | Purpose |
| --- | --- |
| `write_trades` / `read_trades` | Version-2 trade files |
| `write_quotes` / `read_quotes` | Version-2 quote files |
| `write_bars` / `read_bars` | Version-2 bar files |
| `write_trades_v3` / `read_trades_v3` | Version-3 trade files, naming an explicit contract |
| `write_quotes_v3` / `read_quotes_v3` | Version-3 quote files |
| `write_bars_v3` / `read_bars_v3` | Version-3 bar files |
| `read_dataset_descriptor` | A version-3 file's record type and contract, without reading rows |
| `read_records_v3` | A version-3 file of whichever record type it declares |
| `read_schema_metadata` | Inspect a file's contract without reconstructing records |

The v3 readers return a `DatasetV3` carrying the **declared record type**, the
contract, and the records in file order. The record type is returned rather than
discarded because an empty artifact has no rows to contradict a false claim
about it, so the declared type is the only evidence such a file contains.

### One record type per file

A file holds trades, or quotes, or bars — never a mixture. The three Arrow
schemas have distinct column sets and carry no discriminator field, so a mixed
file could not be read back without inventing one, and inventing a schema-level
discriminator would be a contract change. The caller states the expected record
type by choosing the read function; the file's schema is checked against it.

### Parquet settings

Explicit rather than inherited, so a file can be reproduced when library
defaults change. Chosen for stability, not speed — performance tuning is out of
scope.

| Setting | Value | Reason |
| --- | --- | --- |
| Compression | `snappy` | Most widely supported codec; lossless, so it cannot affect a value. No compression level applies |
| Format version | `2.6` | Pinned so layout does not shift with the writing library |
| Dictionary encoding | disabled | Keeps every column's encoding uniform and the file predictable |
| Row group size | 65 536 rows | Fixed so grouping does not vary with input size |
| Statistics | library default | Does not affect values |

### Determinism

**Semantic determinism is guaranteed:** the same records written with the same
configuration read back as equal records, in the same order. Nothing reads the
wall clock, generates a random identifier, or depends on dictionary iteration
order or the Python hash seed.

**Byte identity is verified, but narrowly.** Two writes of the same records
produce byte-identical files under the pinned PyArrow version, and a test
asserts it. This is not promised across PyArrow versions: the Parquet footer
records the writing library's version string.

Row order is preserved exactly. Storage never sorts — ordering is a replay
concern with its own rules, and reordering here would hide the order the caller
established.

### Atomic writes

A write goes to `<target>.partial` in the target's own directory, is closed
completely, and is then moved onto the target with `os.replace`, which is
atomic on POSIX and for same-volume moves on Windows. The target is therefore
always either the previous file or the new one, never a truncated one — and a
truncated Parquet file is indistinguishable from a valid one until it is read.

The temporary name is derived from the target rather than randomised, so a
failure leaves a predictable artefact rather than a uniquely-named orphan. On
any failure the temporary file is removed and an existing target is left
untouched. This is not a transaction system: concurrent writes to the same path
are not supported.

### Timestamps without a zone database

Arrow stores a timestamp as a microsecond count plus a zone name. Converting one
back through PyArrow's `as_py()` resolves that name through `zoneinfo`, which
requires the `tzdata` package — absent on a stock Windows install, where it
raises `ZoneInfoNotFoundError` for the name `UTC` itself.

Rather than depend on zone-name resolution to convert a value already known to
be UTC, the store reads the underlying microsecond count and rebuilds the
`datetime` against `datetime.UTC` directly. That is exact, needs no zone
database, and cannot silently pick up a different zone. The
`timestamp_timezone` metadata is still validated on read, so the file's own
claim is checked.

Since Phase 1.8C, `tzdata` is additionally a **declared runtime dependency**
(ADR-008), because every path *other* than this one needs it: PyArrow's
`as_py()`, Polars scalar access, and Polars `to_dicts()` all resolve the
schema's zone name through `zoneinfo`, and without an IANA database they fail
— the Polars dictionary path with a Rust-level panic rather than a catchable
exception. The reconstruction above is retained as defence in depth: the
store's own read path works even where zone resolution is broken, and the
declared dependency makes the ordinary paths work everywhere else.

### Rejected on read

All raise `StorageContractError`, a domain-specific exception, rather than a
raw Arrow or Parquet error:

- missing schema metadata, or an unsupported schema version;
- wrong `timestamp_timezone`, `price_scale`, `price_encoding`, or
  `schema_name` metadata;
- missing, extra, or reordered columns — including reading a file as the wrong
  record type;
- a column whose Arrow type differs from the contract;
- a null in any column, since every column is required;
- a corrupted or truncated file;
- a stored value the domain rejects: an unrecognised `side`, a negative
  fixed-point price, a non-positive bar interval;
- a stored value no `datetime` or `timedelta` can represent — see below.

Genuine filesystem failures are **not** wrapped. A missing file raises
`FileNotFoundError`, whose own message says more than a wrapper would.

### Unrepresentable stored values

Arrow holds a timestamp as `int64` microseconds and a bar interval as `uint64`
microseconds. Both span far more than the values the store can rebuild: `int64`
microseconds reach some 292 000 years either side of the epoch, against a
`datetime` range of roughly year 1 to year 9999. A file that was not written
here, or one corrupted after it was, can therefore be perfectly valid Parquet
and still carry an instant with no Python counterpart.

The guarantee:

> No public function in the storage layer raises `OverflowError`. A stored
> value that cannot be rebuilt is a contract violation and is reported as
> `StorageContractError`.

`OverflowError` is an `ArithmeticError`, not a `ValueError`, so it is not
covered incidentally by handlers that catch malformed values; it is named
explicitly wherever stored values become `datetime` or `timedelta` objects.

Two guards exist because there are two kinds of failure:

- A **timestamp** has a fixed representable range, independent of every other
  column. It is range-checked *before* reconstruction, so the error names the
  column, the row, and the offending value. The bounds are exported as
  `MIN_TIMESTAMP_MICROSECONDS` and `MAX_TIMESTAMP_MICROSECONDS`, derived from
  `datetime.min`/`datetime.max` rather than written out, so they cannot drift
  from the type they describe. They are a property of the Python type, not of
  the storage schema — no schema version depends on them.
- A bar's **interval** has no fixed bound. Whether
  `availability_time - interval` is representable depends on both columns
  together, so no per-column check can decide it. That arithmetic is allowed to
  fail and its `OverflowError` is translated at the boundary. A `uint64`
  interval of roughly 584 000 years decodes to a valid `timedelta` and only
  fails when interval start is derived.

**Write-path symmetry.** Every value the write path can emit, the read path
accepts. A `datetime` has no value outside the bounds the reader enforces, and
the domain already refuses a bar whose interval pushes availability past the
representable range, so no constructible record can produce a file this store
would then refuse. The write path nonetheless converts `OverflowError` to
`StorageContractError` as well: making the API's guarantee depend on that
coincidence would hold only until the domain widened.

Reconstruction goes through the ordinary domain constructors, so every stored
value is revalidated. Nothing uses `model_construct` or any other route that
would bypass validation: a file is untrusted input.

### Dataset identity and the catalog

A stored artifact's identity is not a filename or a path. See ADR-012 and
`quant_research_terminal.catalog`:

| Identity | Answers | Changes when |
| --- | --- | --- |
| `SemanticDatasetHash` | *what does this data mean?* | a record, or the row order, changes |
| `PhysicalArtifactHash` | *which exact bytes?* | the file is re-encoded, corrupted, or replaced |
| `ManifestHash` | *which claims and provenance?* | any claim or provenance field changes |

The semantic hash is a framed, single-pass SHA-256 over **decoded domain
records** — not stored column values, because `parse_trade_side` normalises, so
a file holding `"BUY"` and one holding `"buy"` are the same dataset. It excludes
vendor alias, provider, path, compression, row-group layout, writer version,
wall clock, randomness, and `schema_version` itself: a future v4 re-encoding of
the same records yields the same semantic hash, which is the right answer to
"did the data change?".

It is a **sequence** hash. Same records in a different order give a different
result, the hasher never sorts, and duplicates survive exactly (ADR-003). The
order it pins is the artifact's **stored** order, which is *not* the future
replay total order — `source_index` is not persisted, so nothing in a stored
artifact proves its order came from `event_ordering_key`.

### Record identity, and what cross-batch comparison can prove

Comparison across two persisted datasets is bounded by what the *stored records*
identify. That boundary is not a style choice; it was established empirically.

| Record | Logical event identity | Cross-batch comparison can determine |
| --- | --- | --- |
| Trade | **none** (ADR-003) | exact canonical rows, with multiplicity; sequence and multiset equality; temporal overlap |
| Quote | **none** (ADR-003, ADR-005) | the same |
| Bar | the **period** (ADR-005) | all of the above, **plus** per-period agreement, conflict, and left/right-only |

**Trades and quotes carry no identity.** ADR-003 records that identifying a trade
by `(timestamp, instrument_symbol, price, size, side)` *destroyed data*, because
one-lot fills at the same price inside one microsecond are ordinary tick data —
"attribute equality was standing in for an identity that does not exist." So a
shared timestamp is **not** a logical event key, and no correction is ever
inferred between two rows. `exact_row_overlap_count == 0` means *no identical
rows were found*, and never that the underlying market events are disjoint.

**Bars are keyed by their period.** ADR-005 defines that key as
`(instrument_symbol, interval_start, interval)`. Cross-batch comparison
substitutes the **canonical contract** for the vendor alias — schema v3 makes the
contract a dataset-level fact, and ADR-012 makes `vendor_symbol` provenance that
must never decide whether two rows describe the same thing. So the effective key
is `FuturesContractId + interval_start + interval`, and the per-row part reduces
to `(interval_start, interval)`.

**Duplicate bar keys are refused, not resolved.** ADR-005's uniqueness guarantee
is an *import-validation* property. Storage enforces nothing of the kind —
duplicate rows survive a round trip by design, and version-3 writes do not pass
through the import validator — so an artifact can hold two rows for one period.
Comparison raises rather than choosing, because first-wins and last-wins are both
guesses about which claim counts.

Rows are compared by the **same canonical encoding the semantic hash uses**
(`canonical_record_bytes`), so "same row" and "same dataset" cannot drift apart,
and `vendor_symbol` is excluded by construction rather than by a filter. Parquet
bytes and writer metadata are never compared. Multiplicity is preserved:
`[X, X, Y]` and `[X, Y]` are not the same multiset.

### Correction lineage

A correction is a separate immutable artifact — `SupersedesRelation`, published
under `<catalog_root>/lineage/<relation_hash>.json` — never a change to a
manifest. `DatasetManifest` has no `parent`, `supersedes`, or `revision` field.
See ADR-013.

The relation hash covers a format token, the successor `ManifestHash`, the
**sorted** predecessor `ManifestHash` tuple, and structured provenance. It
excludes every path, the catalog root, the wall clock, randomness, and
publication order, so caller argument order cannot give one claim two identities.

Publication requires the same contract and the same record type on both sides,
and a **different** `SemanticDatasetHash` — identical semantics is provider or
encoding variation, not correction. Different semantics, conversely, proves
nothing: no relation exists until one is explicitly published.

### What replay reads, and what it refuses

Deterministic replay (ADR-014) consumes these contracts read-only and adds no
persisted field: `SCHEMA_VERSION` remains 3.

The temporal contract it depends on, restated where a reader of this document
will look for it:

| Record | Availability time — when replay makes it observable |
| --- | --- |
| `Trade` | `Trade.timestamp` |
| `Quote` | `Quote.timestamp` |
| `Bar` | `Bar.timestamp`, the derived interval close (`interval_start + interval`) |

For bars this is the ADR-002 property and replay never recomputes it — it reads
`Bar.timestamp`, so there is one arithmetic and no opportunity to drift. For
trades and quotes the equality holds because the schema persists **one**
timestamp column and no second coordinate; it is a limitation of the contract and
is deliberately *not* documented as a claim that feed latency is zero.

Two storage properties are load-bearing for replay and are stated here as
requirements on this contract rather than as replay's own behaviour:

- **File order is the logical row order.** `read_records_v3` returns rows in file
  order, the writers preserve caller order, and neither sorts. Replay derives
  `row_ordinal` from that order and never renumbers it.
- **Stored order is part of semantic identity.** The dataset hash is a sequence
  hash and never sorts, so replay refuses an out-of-order source rather than
  repairing it — sorting would produce a timeline whose semantics no published
  `SemanticDatasetHash` describes.

Replay refuses a **schema-v2** source outright. Version 2 persists a vendor alias
rather than a listed contract, and resolving `"ESM6"` into `CME:ES:M2026` would
mean guessing a venue and a decade. Migration is explicit and is never invoked
automatically.

An **empty** version-3 artifact is a valid replay source: it contributes zero
events, is vacuously ordered, and still participates in the duplicate-dataset
check. Its identity lives in the schema metadata, which is why an empty artifact
is meaningful at all.

### Not implemented

No DuckDB, no SQL or graph database, no partitioning strategy, no caching, no
query layer, and no performance claim of any kind. Durability is **not** claimed:
`os.replace` gives visibility atomicity only, no `fsync` is called on the file or
its directory, and directory `fsync` has no Windows equivalent — so recovery is
detection, which is what the physical hash is for.

## Compatibility policy

- A file is validated against an **explicitly stated** expected version;
  `validate_storage_schema` defaults to legacy rather than current, so a v2 file
  is never silently validated as v3.
- Unknown or incompatible schema metadata is rejected. Version 3 requires an
  exact metadata key set; version 2's tolerance of extra keys is grandfathered.
- Storage conversion is explicit and does not rely on generic model serialization.
- Domain models remain authoritative; storage contracts preserve the exact domain types, semantics, and validation rules.
