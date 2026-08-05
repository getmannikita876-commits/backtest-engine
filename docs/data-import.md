# Data Import Contracts

This document records the approved contracts for the data-import layer. It
covers the provider abstraction, the validation stages, and the rules that were
decided explicitly during the Phase 1.3 hardening pass.

Storage encoding is a separate concern and is documented in
`docs/data-contracts.md`.

## Status

Phase 1.3 delivers the **provider and validation foundation only**. No market
data vendor is integrated.

- The CSV provider is the only provider with behaviour, and it decodes only.
- The Databento and ThetaData modules are **interface-only stubs**. They carry
  no credentials, make no network calls, and depend on no vendor SDK. Calling
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
