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
because the domain may not depend on storage and every other layer may depend
on the domain. See `docs/adr/ADR-004-numeric-domain-envelope.md`.

The invariant it exists to guarantee:

> Every domain object accepted by the numeric envelope has numeric fields that
> can be encoded exactly by storage schema v2 — as fixed-point integers and
> unsigned integers — without rounding, truncation, overflow, or coercion
> through float.

This is a claim about numeric encoding, not about end-to-end file persistence:
no Arrow or Parquet IO exists yet, so whether a full `Trade`, `Quote`, or `Bar`
object — or a file of them — can be written and read back is unverified and out
of scope here. See `docs/adr/ADR-004-numeric-domain-envelope.md`.

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

A value is accepted at scale 6 when nothing but trailing zeros would be
discarded to represent it there:

- `5000.250000000` — **accepted**: nine decimal places, but every digit past
  the sixth is a trailing zero that can be removed without information loss.
- `5000.2500001` — **rejected**: representing it at 6 decimal places would
  require removing a non-zero fractional digit.

Judging by raw digit count instead would reject exactly representable values.
Vendor decoders that emit fixed-point values commonly produce them — every
Databento price arrives at scale 9.

### No rounding

A value outside the envelope is **rejected, never adjusted**. Nothing
quantizes, truncates, or clamps. Magnitude is checked before precision, so an
enormous value is reported as out of range rather than as having too many
decimal places.

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

**Version 2 clarification (ADR-004), no bump.** The shared numeric envelope
narrows what the *domain* accepts; it does not change what storage writes or
how a written file is read. Column names, types, fixed-point encoding, and
metadata are unchanged, and every file previously written under version 2
remains valid — storage never wrote a value outside the envelope, it rejected
them. Bumping would signal an incompatibility that does not exist.

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
