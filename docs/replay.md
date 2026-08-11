# Deterministic replay

Replay answers one question:

> What information becomes available next?

It does not answer what a strategy should do, what market state should be, what
order should be sent, what fill happens, or what position exists. See ADR-014 for
the reasoning behind everything below.

## The contract

    same exact ManifestHash input set
  + same ReplayRange
  = same ReplayFrame stream

across repeated calls, fresh preparations, separate processes, `PYTHONHASHSEED`
values, storage layouts, and later lineage claims.

The strongest of these is a child process in which `datetime.now`, `utcnow`,
`today`, `fromtimestamp`, `time.time`, `time_ns`, `monotonic`, `perf_counter`,
`localtime`, `gmtime`, and `sleep` all raise: replay produces the identical
stream there, so it demonstrably reads no clock while working. A `TZ`
parametrisation is also run, but it is a POSIX-only smoke check and is
documented as such — `TZ` does not affect local time on Windows, and
`time.tzset` does not exist there, so on this project's primary platform it
varies an environment variable and nothing else.

## Shape

```
ReplayRange     half-open [start, end) window of availability time
ReplayConfig    canonical set of exact ManifestHash inputs, plus a range
      |  prepare_replay(catalog_root=, storage_root=, config=)
      v
PreparedReplay  immutable verified snapshot; .frames() is repeatable
      |
      v
ReplayFrame     every observation available at ONE instant
      +-- ReplayEvent  one observation plus the provenance that places it
              +-- payload: Trade | Quote | Bar
```

Pure value types live in `domain/replay.py` and import nothing above the domain.
Resolution, verification, and the frame timeline live in `replay/`.

## Availability time

| Record | Becomes available at | Basis |
| --- | --- | --- |
| `Trade` | `Trade.timestamp` | the only timestamp persisted |
| `Quote` | `Quote.timestamp` | the only timestamp persisted |
| `Bar` | `Bar.timestamp` — a derived property equal to `interval_start + interval` | ADR-002 |

A one-minute bar covering 10:00:00–10:01:00 becomes observable **exactly** at
10:01:00.000000. Not at 10:00:00, and not at 10:00:59.999999. This is structural
rather than validated: `Bar.availability_time` is a property, so there is no
field in which a wrong availability could be placed.

For trades and quotes, `availability_time == timestamp` is a property of the
**current data model**, not a claim about markets. The platform persists one
timestamp per trade and one per quote, so there is no second coordinate from
which a feed or dissemination delay could be read. This is *not* an assertion
that real feed latency is zero, nor that exchange occurrence time equals
researcher availability time. **No latency model exists in Phase 3 and none is
approximated.**

All availability times are timezone-aware UTC at microsecond precision. Naive and
non-UTC values are rejected; no local-time arithmetic occurs anywhere in replay
ordering.

## The frame is the unit — and why

Every observation sharing an availability time is delivered in **one** frame. A
consumer processes all of a frame or none of it, and there is no supported point
inside a frame at which a decision may be taken.

This is the phase's central anti-look-ahead invariant. If a trade and a bar both
become knowable at 10:01:00, delivering them one at a time would let a consumer
decide in between — on a sequencing artifact, because nothing persisted proves
the trade was knowable first. That decision would be look-ahead bias that
reproduces perfectly and looks entirely plausible.

An instant is never split across two frames, however many observations it holds.

## Frame event order is technical, not causal

`ReplayFrame.events` is sorted by

```
(source_manifest.canonical(), row_ordinal)
```

That order is deterministic and exists for serialization, debugging, and test
equality. **It carries no claim about exchange sequence.** The key is built from
provenance — a content hash of a dataset's claims — precisely so that nobody
mistakes it for market meaning.

There is no trade-before-quote rule, no bar priority, no contract priority, no
provider priority, and no path tie-break.

Two architecture tests guard this, and it is worth being exact about which does
the work. The **API allowlist** pins the exact public surface of every replay
module, so any new public name — a `RECORD_PRIORITY` table, a `ReplayClock`
alias — fails the build until somebody adds it deliberately. A second test
refuses a `RecordType`-keyed mapping *literal* in a replay module; that one is
supplementary and catches only literals, not a mapping built by comprehension.

## What replay does to its inputs: nothing

- **Never sorts.** A source whose availability times decrease in persisted row
  order is rejected (`NonMonotonicReplaySourceError`), never repaired. The stored
  row order is part of a dataset's semantic identity (ADR-012), so reordering
  would emit semantics no published hash describes — and would hide the defect.
  Checked over the whole artifact, before any range filtering, so a source's
  validity never depends on which window is asked for.
- **Never deduplicates.** Repeated identical rows are real data (ADR-003) and are
  all emitted, under their own ordinals.
- **Never renumbers.** `row_ordinal` is the row's position in the original
  artifact and survives range filtering: rows 100–105 keep ordinals 100–105.
- **Never reconciles.** See below.
- **Never redirects.** Lineage is not consulted.

Equal consecutive availability times are legal and common; that simultaneity is
exactly what a frame preserves.

## Inputs

Exact `ManifestHash` values only. Refused: a filename, a vendor symbol, a
`FuturesContractId`, a `SemanticDatasetHash`, a `PhysicalArtifactHash`, a
`ContinuousSeriesId`, a dataset family, and any query for "the latest".

The inputs are semantically a **set** and are stored sorted by digest, so caller
order cannot become hidden configuration semantics.

| Situation | Behaviour |
| --- | --- |
| Zero manifests | Unconstructible — a configuration error |
| One manifest supplied twice | Unconstructible — never deduplicated |
| Two manifests, one `SemanticDatasetHash` | `DuplicateReplaySemanticDatasetError` |
| Not schema v3 | `UnsupportedReplaySchemaError` |
| Manifest never published | `MissingManifestError` (the catalog's own) |
| No verifiable copy of the artifact | `ReplayArtifactVerificationError` |
| Source availability decreases | `NonMonotonicReplaySourceError` |
| Two overlapping histories of one stream | `AmbiguousReplayOverlapError` |
| Frame time fails to increase | `ReplayInvariantError` |

## Multiple sources for one stream

Two selected sources for the same `(contract, record type)` may coexist only if
their **selected** availability spans are strictly disjoint. Touching at one
instant counts as overlap.

Refusing overlap is **not** a claim that the data conflicts — the rows may agree
perfectly. It is a statement about this layer's remit: replay will not
manufacture a union of two unreconciled histories and present it as one observed
sequence. A reconciled dataset is a real artifact somebody materialises upstream.

| Combination | Allowed? |
| --- | --- |
| Same contract, same type, disjoint spans | yes — composition of disjoint shards |
| Same contract, same type, overlapping spans | no |
| Same contract, same type, touching at one instant | no |
| Same contract, different record types | yes |
| Different contracts, same record type | yes |
| An empty selection | yes — it overlaps nothing |

Overlap is judged on what a run actually *selected*, so a range that makes two
globally-overlapping sources disjoint is accepted. Artifact verification and
monotonicity, by contrast, apply to the whole source.

**The scope of this guarantee is narrower than it first reads, and the asymmetry
is deliberate.** A source's *validity* must not depend on who is asking, so
monotonicity is checked over the whole artifact. A run's *ambiguity* is genuinely
a property of what that run consumed, so overlap is checked over the selection.

The consequence is worth stating plainly rather than leaving to be discovered: a
caller can narrow a range until two globally-overlapping datasets no longer
overlap *in the window*, and replay will then compose them. The resulting
timeline is unambiguous — the selected shards are disjoint, and concatenating
disjoint shards invents nothing — but replay does **not** warn that the
underlying datasets disagree elsewhere. Detecting that is Phase 2.4's
`compare_datasets`, which is an explicit call a caller makes deliberately.

So `AmbiguousReplayOverlapError` means "this run would have had to interleave two
histories", not "these two datasets are mutually consistent everywhere else".

## Replay range

Half-open `[start, end)`. A frame exactly at `start` is included; a frame exactly
at `end` is excluded. Because every event in a frame shares one instant, a frame
is never partially clipped — it is entirely inside the window or entirely
outside.

A valid range that selects nothing yields an empty stream. That is an answer, not
an error.

A range **filters the raw information stream**. It does not reconstruct whatever
state a consumer would have accumulated by that point, which is why it is not
called a seek and why no `seek` or `checkpoint` exists.

## Verification and snapshot semantics

`prepare_replay` resolves every manifest, verifies every artifact, checks every
source's ordering, and settles every cross-source ambiguity **before** any frame
exists. There is no partial replay: a stream that emitted one dataset's
observations and then found the next corrupt would have handed a consumer a
prefix of a run that can never complete.

Every indexed location is tried, in deterministic order, and the first that
verifies wins — so one stale index entry beside a valid byte-identical copy does
not break a replay.

Each source is verified twice against two different failure modes: once against
the manifest's full set of claims, and once by re-hashing the rows actually
loaded. The second closes the gap a check-then-use pair leaves over a mutable
filesystem.

Preparation then holds a verified **in-memory snapshot**. Once `prepare_replay`
returns, moving, re-encoding, editing, or deleting the artifact changes nothing
about the stream. No file is locked and no handle is held — the guarantee is that
this run no longer depends on the file, not that the file is safe.

## Deliberately absent

No `ReplayClock`, wall-clock pacing, or `sleep`. No true seek, checkpoint, or
restore. No `MarketState`. No strategy interface, callbacks, signals, features, or
indicators. No orders, fills, execution, portfolio, PnL, slippage, commission,
latency model, or queue model. No `RunManifest`. No `ReplayStreamHash`. No
persisted replay artifact and no replay cache. No catalog writes. No calendar or
roll-schedule dependency. No continuous-series source selection. No lineage
redirect. No schema change — `SCHEMA_VERSION` remains 3.

## Limitations

- **Memory scales with the selected input artifacts.** Each selected source is
  fully materialised; a dataset larger than memory is not supported today. This
  is the correctness-first reference implementation, unprofiled, and no
  performance claim is made. The public contracts say nothing about how rows are
  held, so a streaming rewrite would change no frame semantics.
- **A dense instant produces a large frame**, and cannot be chunked without
  breaking the central invariant.
- **Each artifact is read more than once during preparation.**
- **`PreparedSource` and `PreparedRow` do not validate** — they are
  `NamedTuple`s, so hand-assembling one bypasses `build_prepared_source`.
  `frame_timeline` therefore re-checks both invariants across every source before
  returning an iterator: rows must be non-decreasing, and a row's cached
  availability time must equal its payload's. Either failure raises from the
  call, so no frame is emitted first.
- **Replay cannot say whether a dataset would have been available on a past
  date.** The platform records no trustworthy source-publication time (ADR-013),
  and Phase 3 does not approximate around that.

## Example

```python
from pathlib import Path

from quant_research_terminal.domain.dataset_identity import ManifestHash
from quant_research_terminal.domain.replay import ReplayConfig, ReplayRange
from quant_research_terminal.replay import prepare_replay

config = ReplayConfig(
    manifest_hashes=(
        ManifestHash(value="<trade dataset digest>"),
        ManifestHash(value="<bar dataset digest>"),
    ),
    replay_range=ReplayRange(start=session_open, end=session_close),
)

replay = prepare_replay(catalog_root=Path("catalog"), storage_root=Path("storage"), config=config)

for frame in replay.frames():
    # frame.availability_time is replay's notion of "now".
    # Process the whole frame as one information boundary; the order of
    # frame.events is technical and carries no causal meaning.
    for event in frame.events:
        ...
```
