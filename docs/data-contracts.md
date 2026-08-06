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
- `SCHEMA_VERSION = 2`

### Version history

| Version | Change |
| --- | --- |
| 1 | Initial trade, quote, and bar contracts |
| 2 | Bars gained `interval_microseconds`; the trade `side` column was constrained to the `TradeSide` vocabulary |

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

### Not implemented

No DuckDB, no dataset catalogue, no partitioning strategy, no caching, no query
layer, and no performance claim of any kind.

## Compatibility policy

- Only schema versions that exactly match the current contract are accepted.
- Unknown or incompatible schema metadata is rejected.
- Storage conversion is explicit and does not rely on generic model serialization.
- Domain models remain authoritative; storage contracts preserve the exact domain types, semantics, and validation rules.
