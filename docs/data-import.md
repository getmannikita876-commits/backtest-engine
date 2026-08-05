# Data Import Contracts

This document records the approved contracts for the data-import layer. It
covers the provider abstraction, the validation stages, and the rules that were
decided explicitly during the Phase 1.3 hardening pass.

Storage encoding is a separate concern and is documented in
`docs/data-contracts.md`.

## Status

Phase 1.3 delivered the provider and validation foundation. Phase 1.4 adds the
first real vendor integration.

- The CSV provider decodes generic delimited files.
- The **Databento provider** decodes archived Databento exports. It makes no
  network calls and handles no credentials — see "Databento provider" below.
- The ThetaData module remains an **interface-only stub**. It carries no
  credentials, makes no network calls, and depends on no vendor SDK. Calling
  `fetch` raises `ProviderNotConfiguredError`.
- Options records are not modelled. ThetaData's primary value is options data,
  so that integration additionally depends on domain contracts that do not yet
  exist.

## Stage direction

```
Provider -> Raw Records -> Validation -> Normalization -> Domain Objects
                                                               |
                                                               v
                                                      Storage Contracts
```

The direction is one-way. `RawRecord` is defined outside the `providers`
package so validation can consume provider output without importing vendor
code, and `tests/test_architecture_boundaries.py` enforces every edge.

Each stage owns one concern:

| Stage | Owns | Never does |
| --- | --- | --- |
| Provider | Decoding a source encoding into Python values | Validate, repair, or reject on semantic grounds |
| Validation | Judging records against the rules below | Mutate records, raise on bad data, or decide policy |
| Normalization | Building immutable domain objects | Repair data |
| Orchestration (`pipeline.py`) | Sequencing, row survival, duplicate policy, ordering | Own any rule |

A provider carries a defect through unchanged — a naive timestamp stays naive,
an unparseable price stays a string — so that validation can report it against a
specific row. This is what keeps "reject invalid data instead of silently
fixing it" enforceable at one testable boundary.

## Stream ownership (approved)

`MarketDataProvider.fetch` returns a `RecordStream`: an iterator over
`RawRecord` with an explicit `close()`, usable as a context manager or with
`contextlib.closing`.

A bare `Iterator` return type was rejected because it leaves no way to release
a file handle or socket through the interface, forcing callers to rely on
garbage collection. **No handle may depend only on garbage collection.**

- The stream is lazy: no resource is acquired until it is iterated, and a
  request rejected on capability grounds acquires none at all.
- The stream closes itself on exhaustion and on any exception raised while
  producing a record.
- A caller that stops early must close the stream.
- Closing is idempotent and terminal: a closed stream yields nothing further
  rather than raising.
- Each `fetch` returns an independent stream with its own resource.

```python
with provider.fetch(request) as records:
    for record in records:
        ...
```

## Schema version is batch-fatal (approved)

A batch whose `schema_version` does not match the storage contract produces
**exactly one** `FATAL` `unsupported_schema_version` issue, and nothing else
runs: no validator is invoked, no record is normalized, and no domain object is
returned.

The rows were written against a layout this build does not understand, so every
field meaning is in doubt. Validating them would produce findings derived from
an unknown layout, and normalizing them could manufacture domain objects from
misinterpreted values.

## Timestamp rules (approved)

A timestamp is accepted only when it is a `datetime` carrying a **fixed zero UTC
offset**. Named zones are rejected even when their current offset is zero (for
example `Europe/London` in winter), because their offset is not constant and
would reintroduce daylight-saving ambiguity into event ordering.

Timestamps are never converted, normalized, or repaired — only classified.

The three failure modes carry **distinct** machine-readable codes, because each
calls for different corrective action:

| Code | Meaning | Typical cause |
| --- | --- | --- |
| `non_datetime_timestamp` | Not a `datetime` at all | The source was never parsed |
| `naive_datetime` | A `datetime` with no offset | The source omitted its offset |
| `non_utc_timestamp` | Offset present but not a fixed zero | The source used a real but wrong zone |

## Quantity rules (approved)

Every quantity — `Trade.size`, `Quote.bid_size`, `Quote.ask_size`, and
`Bar.volume` — must be **strictly positive**.

This mirrors the Phase 1.1 domain contract exactly: all four fields are declared
`PositiveDecimal`, that is `Field(gt=0)`. Validating the rule at import time
means a violating row is rejected with a diagnosable issue instead of raising a
raw model error during normalization.

**Bar volume — explicit decision.** Zero volume is *not* allowed. An empty
period must be represented by the **absence of a bar**, not by a bar with zero
volume. Allowing zero would require changing the approved domain contract, which
is a domain-layer decision rather than an import-layer one. If empty periods
must become representable, revisit `Bar.volume` in the domain package first; the
import rule follows from it.

Values are exact: `Decimal` and `int` are accepted, `bool` and `float` are not.
Binary floating point cannot represent decimal tick values exactly, so admitting
a float would corrupt the fixed-point storage encoding downstream.

## Duplicate handling (approved)

Identity is the record type plus every required field, so two records are
duplicates only when indistinguishable in every field the domain models care
about. `record_identity` is the single definition, used by both detection and
policy.

Detection and policy are separate. The validator only **reports** a repeat as a
`WARNING`; `ImportBatch.duplicate_policy` decides which copy survives.

The validated field is always a `DuplicatePolicy`. The enum's string values are
accepted at the external boundary and coerced, so serialized configuration needs
no special handling; an unrecognised string is rejected rather than defaulted.

| Policy | Survivor |
| --- | --- |
| `reject` (default) | The earliest occurrence |
| `keep_first` | The earliest occurrence |
| `keep_last` | The latest occurrence |

## Deterministic event ordering (approved)

Accepted records are returned in the order given by this **complete** key, in
priority order:

1. **Timestamp** (UTC)
2. **Event-type precedence** — `QUOTE` (0), then `TRADE` (1), then `BAR` (2)
3. **Source index** — the record's position in its source, assigned before any
   filtering

This implements the sort priority fixed by `docs/replay_rules.md` (timestamp,
event type priority, sequence number).

Event-type precedence is not arbitrary. A quote publishes book state and a trade
executes against that state, so processing the trade first would match it
against a stale book. A bar summarises a period that has already closed, and
`docs/replay_rules.md` requires that bars become visible only after they close,
so a bar must never be seen before the ticks composing it.

The key is total: two records can compare equal only if they share a source
position, which is impossible by construction.

## Row rejection

A row is rejected when any issue attributed to it has severity `ERROR` or
`FATAL`. Warnings do not reject a row and do not fail a batch: a duplicate
resolved by policy is a handled condition, not a defect.

## Databento provider (approved)

### Exact implemented capability

`DatabentoMarketDataProvider` implements one narrow capability. The class name
should not be read as a general Databento integration:

| Capability | Status |
| --- | --- |
| Archived delimited (CSV) export decoding | **implemented** |
| Binary DBN decoding | not implemented |
| Historical API acquisition | not implemented |
| Live API acquisition | not implemented |
| Credential management | not implemented |

`provider.input_format` returns `archived-delimited-export`, so the limitation
is introspectable and not merely stated in prose.

The delimited export's columns are the DBN schema field names, so the field
mapping is the one a binary reader would also need; only the container differs.

### Why acquisition stays separate

Reading an archived file and fetching from a vendor API have incompatible
properties, and mixing them would compromise the one that matters most:

- Research must be reproducible. Decoding a fixed archived file returns the
  same records forever; a live API returns whatever the vendor currently holds,
  so a replay built on it could never be deterministic.
- The charter forbids secrets in source control, and this provider needs none
  because it never authenticates.

Acquisition — subscribing, requesting a range, downloading — is a legitimate
future component. It belongs outside deterministic decoding so the archived
file remains the reproducible unit of research input.
`requires_credentials` is therefore `false`: obtaining the file needs
credentials, reading it does not.

### Vendor SDK boundary

An earlier revision of this document claimed a vendor SDK would violate
provider independence. **That was wrong**, and the correction matters for
planning.

`PROJECT_RULES.md` requires that code not be *tightly coupled* to a vendor and
that the engine program against interfaces. A vendor SDK confined inside the
Databento provider package satisfies both: the engine depends only on
`MarketDataProvider`, and no other layer can see the SDK. Declaring it an
**optional dependency** (an install extra) means the package remains usable —
and CI remains runnable — without it.

A future binary-DBN backend may therefore use `databento` / `databento-dbn` as
an optional dependency isolated inside this provider package. The reasons it is
absent today are practical, not architectural:

- it is not installed here, so any decoder written against it would ship
  unverified, which the code-quality rules forbid;
- there are no sample DBN files to validate decoding against.

The constraint that *does* survive: **network acquisition must remain separate
from deterministic archived-file decoding**, whether or not an SDK is present.
An SDK used to parse a local DBN file is decoding; an SDK used to fetch from
the API is acquisition, and belongs in a different component.

### Documented vendor encodings

These semantics are relied upon and have **not been verified against live
Databento output**. They are the first thing to re-check when real data arrives.

| Vendor field | Encoding |
| --- | --- |
| `ts_recv`, `ts_event` | int64 nanoseconds since epoch, UTC |
| prices | int64 fixed-point, 1e-9 scale (`5000.25` → `5000250000000`) |
| `UNDEF_PRICE` | `INT64_MAX` — means *no price*, not a large price |
| `instrument_id` | uint32 vendor-local id, **not** a symbol |
| `side` | `A` = ask (sell aggressor), `B` = bid (buy aggressor), `N` = none |
| `size`, `volume` | unsigned integer counts |

Schema mapping: `trades` → TRADE, `mbp-1` → QUOTE, `ohlcv` → BAR.

Prices convert exactly via `Decimal(raw).scaleb(-9)`; no float is involved.
Sentinels decode to a non-numeric marker so a numeric validator can never
mistake them for real values, and the row is rejected with a diagnosable issue.

### Timestamp selection and look-ahead bias

The record timestamp defaults to **`ts_recv`** — when the data became
observable — not `ts_event`. Stamping a record with `ts_event` and replaying it
at that instant would let a strategy act on information before it could have
arrived, which is look-ahead bias and a critical defect under this project's
rules. `ts_event` remains selectable for research that deliberately studies
exchange sequencing.

### Bar availability time (ADR-002)

A bar has two temporal coordinates: the interval it describes, and the instant
its values become known. Databento stamps `ohlcv` records at **interval
start**, but open/high/low/close/volume are not determined until interval
**close**. Publishing the completed bar at its start timestamp would expose the
closing price before the period elapsed — look-ahead bias, and invisible
because every timestamp still looks correct.

`Bar.timestamp` is what the replay ordering key reads, so it decides when a bar
becomes visible. **It therefore carries the information-availability time:
`interval_start + interval`.**

This is a decode, not a mutation:

- `Bar.timestamp` means "when this event enters the stream"; for a bar that is
  interval close, so mapping the vendor's start-stamped encoding onto it is the
  same kind of translation as mapping `B` onto `buy`;
- it is exactly invertible — the vendor's original value is always
  `timestamp - interval`, and the interval is available from
  `provider.bar_interval`;
- it is never guessed — `bar_interval` is a **required** argument for the OHLCV
  schema, and omitting it raises at construction.

| Case | Semantics |
| --- | --- |
| 1-minute bars | Available at `start + 60s`; a 09:30 bar becomes visible at 09:31 |
| Arbitrary intervals | Any strictly positive `timedelta`; the rule is uniform |
| Session-boundary bars | `start + interval` is a **nominal** close. Without an exchange calendar, half-days and holidays are not modelled. The nominal close is never *earlier* than the true close, so it cannot create look-ahead; it may delay visibility, costing realism but not correctness |
| Missing interval metadata | Refused at construction. No fallback is safe — an assumed interval would silently reintroduce the bias |

The full rationale, rejected alternatives, and the proposed domain-contract
change are in `docs/adr/ADR-002-bar-availability-time.md`.

**Known limitation:** because the domain `Bar` carries one timestamp, interval
start is no longer directly readable from an imported bar, and a bar does not
record its own duration. ADR-002 proposes adding `interval_start` and
`interval` to the domain contract to resolve this; that requires a storage
schema revision and is a separate decision.

### Sub-microsecond precision

Python's `datetime` and the storage schema both resolve to microseconds, so a
Databento timestamp carrying nanosecond detail has no exact representation
anywhere in this system. The caller must choose:

| Policy | Behaviour |
| --- | --- |
| `reject` (default) | The raw nanosecond value passes through undecoded; validation reports `non_datetime_timestamp` and the row is rejected with its original value intact |
| `truncate` | The sub-microsecond remainder is discarded, flooring toward the past |

`truncate` is lossy and irreversible, which is why it is opt-in. Most real
Databento data carries nanosecond detail, so an operator importing captured
data will normally need it — but they enable it knowing exactly what is lost.
Truncation always **floors toward the past**, so a record is never moved
forward in time; rounding to nearest could shift a record later and create a
look-ahead of up to 500ns.

Operator-visible behaviour under `reject`: the row is not dropped in silence.
The raw nanosecond value reaches validation, which reports
`non_datetime_timestamp` against that row's source index, and the run's report
counts it as rejected.

**Alternatives reviewed and not adopted.** Recording per-record precision-loss
metadata, or preserving the nanosecond remainder in a separate field, both
require a field that neither `RawRecord` (whose fields are strictly validated
against the required set) nor the domain and storage contracts provide.
Migrating storage to `timestamp[ns]` would not help on its own either, because
Python's `datetime` still resolves to microseconds, so the domain would need a
different temporal type entirely. None of these is justified by current
requirements; each would need its own ADR. The existing two-policy contract is
retained.

### Symbology

Databento identifies instruments by numeric `instrument_id`. That is not a
symbol, and nothing downstream validates a symbol's shape, so emitting the id
as one would corrupt instrument identity across the platform. **Every record
requires a resolved symbol; `instrument_id` is never used as one.**

Resolution is strict and never guesses. Both sources are consulted rather than
short-circuiting on the first, so a disagreement is reported instead of being
silently settled in favour of one:

| Situation | Outcome |
| --- | --- |
| Export carries a `symbol` value | Used |
| No `symbol`, `instrument_id` in the caller's mapping | Mapped symbol used |
| Both present and identical | Used |
| Both present and **different** | `ProviderDecodeError` — contradictory identity |
| No `symbol`, `instrument_id` unmapped | `ProviderDecodeError` |
| No `symbol` and no `instrument_id` | `ProviderDecodeError` |
| Mapping contains a blank or whitespace-only symbol | `ValueError` at construction |

The blank-symbol check matters because `"   "` satisfies the domain's
`min_length=1` constraint while carrying no identity at all. Rejecting it
eagerly fails at the call site rather than days later inside a result.

### Trade side

`A` decodes to `sell` and `B` to `buy`. `N` — the vendor could not attribute an
aggressor — is **passed through unchanged and never guessed**. Mapping it to a
direction would fabricate a fact the vendor did not supply, and would bias any
order-flow research built on it.

**Known limitation.** `Trade.side` is an unconstrained `str` in the current
domain contract, so an unattributed side reaches the domain as the literal
vendor code `"N"`. Nothing validates it, and a consumer testing
`side == "buy"` will treat it as though it were a sell. The contract cannot
express "unknown" *safely* — only incidentally.

Until that changes, `KNOWN_TRADE_SIDES` (`{"buy", "sell"}`) is the supported
way to detect an unattributed side; treat any value outside it as unknown.

**Smallest proposed contract change:** replace `Trade.side: str` with a
`TradeSide` StrEnum of `BUY`, `SELL`, `UNKNOWN`. This makes the unknown case
representable and type-checkable, and lets validation reject codes outside the
vocabulary. It touches the audited Phase 1.1 domain model and the storage
encoding of the `side` column, so it warrants its own ADR rather than being
folded into a provider change.

### Storage precision constraint

Databento prices carry up to 9 decimal places; the storage contract
(`PRICE_SCALE = 6`) holds 6 and rejects precision loss. A price finer than
1e-6 is decoded exactly by the provider but will be **rejected at storage
time**. This does not affect ES/NQ, whose tick is 0.25.

## Validation composition

Two compositions share the same validator instances:

- **Stream** (`default_validation_pipeline`) — schema, timestamp, value,
  duplicate, **ordering**. A provider stream is consumed in arrival order and
  nothing re-sorts it, so a record moving backwards in time is a genuine defect.
- **Batch** (`batch_validation_pipeline`) — schema, timestamp, value,
  duplicate. Ordering is omitted because the batch API's contract is to *sort*
  its output, not to reject unsorted input.

Validators are independent but stay silent where another owns the diagnosis: a
record missing its timestamp column is reported once by the schema validator,
not five times.
