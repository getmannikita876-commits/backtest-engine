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
- `SCHEMA_VERSION = 2`

### Version history

| Version | Change |
| --- | --- |
| 1 | Initial trade, quote, and bar contracts |
| 2 | Bars gained `interval_microseconds`; the trade `side` column was constrained to the `TradeSide` vocabulary |

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

Schema metadata fields include:

- `schema_name`
- `schema_version`
- `timestamp_timezone`
- `price_encoding`
- `price_precision`
- `price_scale`

## Parquet persistence (implemented)

`quant_research_terminal.data.parquet_store` performs the real round trip:

```
domain records -> Arrow table -> Parquet file -> Arrow table -> domain records
```

### API

| Function | Purpose |
| --- | --- |
| `write_trades` / `read_trades` | Trade files |
| `write_quotes` / `read_quotes` | Quote files |
| `write_bars` / `read_bars` | Bar files |
| `read_schema_metadata` | Inspect a file's contract without reconstructing records |

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

Rather than take a dependency to convert a value already known to be UTC, the
store reads the underlying microsecond count and rebuilds the `datetime`
against `datetime.UTC` directly. That is exact, needs no zone database, and
cannot silently pick up a different zone. The `timestamp_timezone` metadata is
still validated on read, so the file's own claim is checked.

The same limitation applies to Polars: it reads the file's types and values
correctly, but converting a zone-aware timestamp to a Python object needs
`tzdata` too.

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
  fixed-point price, a non-positive bar interval.

Genuine filesystem failures are **not** wrapped. A missing file raises
`FileNotFoundError`, whose own message says more than a wrapper would.

Reconstruction goes through the ordinary domain constructors, so every stored
value is revalidated. Nothing uses `model_construct` or any other route that
would bypass validation: a file is untrusted input.

### Not implemented

No DuckDB, no dataset catalogue, no partitioning strategy, no caching, no query
layer, and no performance claim of any kind.

## Compatibility policy

- Only schema versions that exactly match the current contract are accepted.
- Unknown or incompatible schema metadata is rejected.
- Storage conversion is explicit and does not rely on generic model serialization.
- Domain models remain authoritative; storage contracts preserve the exact domain types, semantics, and validation rules.
