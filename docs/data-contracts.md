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

## Compatibility policy

- Only schema versions that exactly match the current contract are accepted.
- Unknown or incompatible schema metadata is rejected.
- Storage conversion is explicit and does not rely on generic model serialization.
- Domain models remain authoritative; storage contracts preserve the exact domain types, semantics, and validation rules.
