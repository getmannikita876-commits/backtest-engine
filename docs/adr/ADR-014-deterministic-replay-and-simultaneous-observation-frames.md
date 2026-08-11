# ADR-014: Deterministic replay and simultaneous observation frames

- Status: **Accepted** — Phase 3
- Date: 2026-08-11
- Supersedes: nothing
- Amends: `docs/replay_rules.md` (three clauses; see "Correcting replay_rules.md")
- Related: ADR-002 (bar availability time), ADR-003 (trade identity), ADR-009
  (futures contract identity), ADR-012 (dataset artifact identity, schema v3),
  ADR-013 (revision lineage), `docs/replay.md`

## Context

Phases 1 and 2 produced immutable, content-addressed market-data artifacts and a
catalog that can say exactly which dataset a set of bytes is. Nothing yet turns
those artifacts back into a *timeline*.

Replay is the component that answers one question:

> What information becomes available next?

It does not answer what a strategy should do, what market state should be, what
order should be sent, what fill happens, or what position exists. Those are later
phases and every one of them is excluded here by construction.

The question sounds simple and contains the phase's entire difficulty. Turning a
set of stored artifacts into a stream forces four decisions that are easy to get
subtly, invisibly wrong:

1. **When does an observation become knowable?** Not the same question as "what
   timestamp does it carry" — for bars, demonstrably not (ADR-002).
2. **What happens when several observations become knowable at the same
   instant?** This is the dangerous one, and it is the reason this ADR exists.
3. **What may a replay layer do to its inputs?** Sorting, deduplicating,
   merging, and selecting all look like tidying and are all destructive.
4. **What exactly is a replay's input?** Anything resolved at run time — "the
   latest revision", a symbol, a filename — makes the same configuration mean
   different things as the catalog grows.

### The failure this phase is organised around

Nondeterminism is not the worst thing a replay engine can do. **Deterministic
look-ahead created by a fabricated ordering is**, because it reproduces
perfectly, every timestamp in the resulting log looks correct, and every result
looks plausible.

Concretely. Suppose a trade, a quote, and a completed one-minute bar all become
available at `10:01:00.000000`. A conventional event-loop replay hands a consumer
one event at a time:

```
    -> trade @ 10:01:00     consumer decides
    -> quote @ 10:01:00     consumer decides
    -> bar   @ 10:01:00     consumer decides
```

Nothing in the persisted data proves the trade was knowable before the quote. The
platform stores one timestamp per trade and one per quote and no sequence number
(ADR-003 records that trades and quotes have no logical event identity). So the
first arrow is an invention, and a decision taken between arrow one and arrow two
is a decision taken on a sequencing artifact. That is look-ahead bias of the most
damaging kind: invisible, reproducible, and indistinguishable from skill.

## Decision

### Replay ontology

```
ReplayRange     a half-open [start, end) window of availability time
ReplayConfig    a canonical set of exact ManifestHash inputs, plus a range
      |  prepare_replay()  — resolve, verify, snapshot; all failures happen here
      v
PreparedReplay  an immutable, verified snapshot; frames() is repeatable
      |
      v
ReplayFrame     every observation available at ONE instant
      |
      +-- ReplayEvent   one observation, with the provenance that places it
              +-- payload: Trade | Quote | Bar
```

### 1. The ReplayFrame is the fundamental unit

Every observation sharing an availability time is delivered in exactly one frame.
A consumer receives all of a frame or none of it, and **there is no supported
point inside a frame at which a decision may be taken**.

This is the direct answer to the failure above. Simultaneity that the data
asserts is simultaneity the replay layer preserves.

### 2. Intra-frame order is technical and explicitly non-causal

`ReplayFrame.events` is stored sorted by

```
(source_manifest.canonical(), row_ordinal)
```

so a frame has one serialization, one repr, and one comparable form in a test.

**The order carries no claim about exchange sequence.** The key is deliberately
built from *provenance* rather than from market meaning, and that choice is
load-bearing: a manifest digest is a SHA-256 of a dataset's claims, and reading
market causality out of a SHA-256 is obviously absurd in a way that reading it
out of "trades before quotes" is not. A convention that looks meaningful is more
dangerous than one that plainly is not.

Deliberately absent from the key: record type, contract, provider, vendor symbol,
physical hash, and file path.

### 3. Availability time

| Record | Availability time | Basis |
| --- | --- | --- |
| `Trade` | `Trade.timestamp` | the only timestamp persisted |
| `Quote` | `Quote.timestamp` | the only timestamp persisted |
| `Bar` | `Bar.timestamp`, a derived property equal to `interval_start + interval` | ADR-002 |

Bars are the interesting case and they are already safe: `Bar.availability_time`
is a *property*, not a field, so a bar visible before its interval closes is not
merely invalid but unconstructible. Replay reads `Bar.timestamp` and never
recomputes it, so there is one arithmetic and no opportunity to drift.

**Trades and quotes are stated honestly.** `availability_time == timestamp` is a
property of the *current data model*, not a claim about market microstructure. It
does **not** assert that feed latency is zero, nor that exchange occurrence time
equals researcher availability time. The platform persists one timestamp per
trade and one per quote, so there is no second coordinate from which a
dissemination delay could be read, and inventing one would be fabricating data. A
richer contract carrying both coordinates would change this mapping and nothing
else.

No latency model is inferred, assumed, or approximated in Phase 3.

### 4. UTC, microsecond, exact

Every availability time is a timezone-aware UTC `datetime` validated by
`domain.time.validate_utc_datetime`. Naive and non-UTC values are rejected. There
is no local-time arithmetic anywhere in replay ordering, no `utc.date()`, and no
`TradingDate` inference.

### 5. Row ordinals are provenance

`row_ordinal` is the observation's zero-based position in the source artifact's
original persisted logical row sequence. It **survives range filtering
unchanged**: selecting rows 100–105 yields ordinals 100–105.

It is not `source_index` (never persisted), not a vendor sequence, not a market
event id, not a trade id, and not a correction id. No such identity exists in the
data (ADR-003), and presenting a positional index as one would invent an
identity the data does not carry.

### 6. Persisted row order is preserved, and never repaired

Replay never sorts a source. A source whose availability times **decrease** in
persisted row order is rejected with `NonMonotonicReplaySourceError`.

Sorting would be the dangerous repair. ADR-012 makes the stored row order part of
a dataset's semantic identity — the hash is a sequence hash and the hasher never
sorts — so a replay layer that quietly reordered rows would emit a timeline whose
semantics *no published `SemanticDatasetHash` describes*, while hiding that the
source was out of order at all.

Monotonicity is checked over the **whole** artifact, before any range filtering,
so whether a dataset is a valid replay source never depends on which window a
particular run happens to ask for.

Equal consecutive availability times are legal and common; that simultaneity is
exactly what a frame preserves.

### 7. Inputs are exact manifest hashes, canonicalized as a set

`ReplayConfig.manifest_hashes` holds exact `ManifestHash` values, stored sorted by
digest. Caller order therefore cannot become hidden configuration semantics:
`ReplayConfig(A, B, C)` and `ReplayConfig(C, A, B)` are the *same* configuration
and produce the same frames. Sorting a set of content hashes is not an economic
judgement about the data; it is the absence of one.

Refused as inputs: a filename, a vendor symbol, a `FuturesContractId`, a
`SemanticDatasetHash`, a `PhysicalArtifactHash`, a `ContinuousSeriesId`, a dataset
family, and any query for "the latest".

### 8. Structural refusals are unconstructible, not raised

Two configuration mistakes are made **unconstructible** rather than reported at
prepare time:

* **zero sources** — a configuration error, deliberately distinct from a valid
  configuration whose range selects nothing;
* **a repeated manifest hash** — refused, never deduplicated. A caller supplying
  one twice expected two sources; collapsing them answers a question that was not
  asked, and honouring them would double-count one dataset entirely.

This follows ADR-013 exactly, which made a `SupersedesRelation` with duplicate
predecessors unconstructible in the domain model rather than rejected later. It
is strictly stronger than a typed runtime error: there is no window in which an
invalid configuration exists as a value that could be logged, passed on, or
recorded in a future run manifest. Consequently **no `DuplicateReplayManifestError`
class exists** — `replay/errors.py` says so and says why, honouring
`catalog/errors.py`'s rule that an error taxonomy invented ahead of its callers
is a set of promises nothing keeps.

### 9. Two manifests naming one dataset are refused

If two distinct `ManifestHash` values pin the same `SemanticDatasetHash`,
preparation fails with `DuplicateReplaySemanticDatasetError`.

They decode to the same records in the same order — that is what semantic
identity means (ADR-012) — so replaying both would deliver every observation
twice. Each is perfectly valid *individually*; what is refused is the pair.
Neither is deduplicated and neither is chosen: choosing would make replay a
provenance-preference policy, which it is not. Two providers agreeing is
corroboration, never two market streams.

Checked before overlap, and independently of the replay range: redundancy is a
property of the input set, not of the window.

### 10. Overlapping histories of one stream are refused; replay never reconciles

Two selected sources for the same `(FuturesContractId, RecordType)` may coexist
only if their **selected** availability spans are strictly disjoint —
`max(A) < min(B)` or `max(B) < min(A)`. Touching at a single instant counts as
overlap. Otherwise: `AmbiguousReplayOverlapError`.

The wording matters and is deliberately narrow. This is **not** a claim that the
data conflicts; the rows may agree perfectly, and ADR-013's comparison could even
prove it. The statement is about this layer's remit: *replay is not a
reconciliation layer*, so it will not manufacture a union of two unreconciled
histories and present it as one observed sequence. A reconciled dataset is a real
artifact somebody materialises upstream, with its own identity and provenance.

Disjoint shards **are** allowed, and that is not an inconsistency: concatenating
histories that do not overlap invents nothing, whereas interleaving overlapping
ones invents a sequence.

Overlap is judged on what a run actually **selected**, so a range that makes two
globally-overlapping sources disjoint is accepted. Artifact verification and
monotonicity, by contrast, apply to the whole source (§6) — a source's validity
must not depend on the asker, but a run's ambiguity is genuinely a property of
the run.

Different record types for one contract may overlap. Different contracts of one
record type may overlap. Neither is ambiguous: they are different streams.

### 11. Schema v3 only

A source that is not canonical schema v3 raises `UnsupportedReplaySchemaError`.
Schema v2 persists a vendor alias rather than a listed contract, and resolving
`"ESM6"` into `CME:ES:M2026` would mean guessing a venue and a decade — exactly
the inference ADR-012 exists to make impossible. Migration is never invoked
automatically; it is an explicit operator-supplied mapping producing a new
artifact with its own identity.

### 12. Full preflight, then a snapshot

`prepare_replay` resolves every manifest, verifies every artifact, validates every
source's ordering, and settles every cross-source ambiguity **before**
`PreparedReplay.frames()` can yield anything. A stream that emitted the first
dataset's observations and then discovered the second was corrupt would have
handed a consumer a prefix of a run that can never complete; a partial replay is
worse than a failed one, because it looks like a replay.

Each source is checked twice, against two different failure modes:

1. `verify_artifact_against_manifest` recomputes every claim — physical hash,
   schema version, record type, contract, row count, time bounds, vendor aliases,
   semantic hash — from the file.
2. The rows actually loaded are then re-hashed and compared to the manifest's
   semantic hash.

The second is not a slower restatement of the first. Verification reads the file
and the load reads it again; a check-then-use pair over a mutable filesystem
proves nothing about the second read unless the second read is itself checked.
Without step 2 the honest claim would be "the bytes were correct a moment ago".

**Every** indexed location is tried, in the index's deterministic order, and the
first that verifies wins. Taking only the first candidate was a real Phase 2.4
defect in `catalog/comparison.py`: the index is the one part of the catalog
explicitly allowed to be wrong, so one stale entry beside a good byte-identical
copy turned an answerable question into a hard failure. Replay resolves the way
`verify_manifest` does, and there is a regression test.

**TOCTOU, stated exactly.** Eager verification cannot prove a file will never
change. Loading the rows makes the question stop mattering: preparation captures
a verified **in-memory snapshot**, so once `prepare_replay` returns, moving,
re-encoding, editing, or deleting the artifact changes nothing about the stream.
No file is locked and no handle is held — the guarantee is that this run no
longer depends on the file, not that the file is safe.

### 13. Repeatable, cursor-free iteration

`PreparedReplay.frames()` returns a **fresh** iterator every call over the same
snapshot, so two calls produce equal streams and two iterators do not interfere.
There is no `reset()`, because there is no cursor to reset.

## Explicitly not implemented

* **No `ReplayClock`.** Replay time *is* `ReplayFrame.availability_time`. A
  separate mutable clock would be a second, weaker copy of it, and a wall-clock
  pacing mode would make a research run's output depend on CPU speed. No
  `tick()`, `advance()`, `sleep()`, or `speed()`.
* **No true seek or checkpointing.** A `ReplayRange` filters the *raw information
  stream*. It does **not** reconstruct whatever state a consumer would have
  accumulated by that instant. Calling it a seek would promise something no
  component of this phase can deliver, so no `seek`, `checkpoint`, `restore`, or
  `jump_to` exists.
* **No `MarketState`, strategy interface, strategy callbacks, signals, features,
  or indicators.**
* **No orders, fills, execution, portfolio, PnL, slippage, commission, latency
  model, or queue model.**
* **No `RunManifest` or experiment registry.** A future one can pin its inputs
  with exactly `(manifest hashes, replay range)`.
* **No `ReplayStreamHash`.** Determinism is proven by exact frame-stream equality
  across processes, hash seeds, permutations, relocations, and a child process
  in which every clock read raises. A
  public stream hash would be a second definition of "same replay" with nothing
  currently needing it; if a concrete need appears, it is a decision to take
  deliberately rather than a convenience to add.
* **No serialized replay artifact and no replay cache.** Replay is a derived
  read-only stream.
* **No catalog writes.** Phase 3 publishes no manifest, no lineage, no artifact.
* **No calendar dependency.** No trading date is derived, and no session-open,
  session-close, `HALT`, or `MAINTENANCE` event is synthesised. Those are
  consumer concerns layered *over* an availability timeline, not properties of
  one.
* **No roll-schedule dependency and no continuous-series source selection.**
  Replay replays the exact listed-contract datasets it was given; it does not
  auto-switch contracts, emit rollover events, or resolve a `ContinuousSeriesId`.
* **No lineage redirect.** Replay imports no lineage navigation, so a
  configuration pinning A replays A after `B supersedes A` is published — not by
  policy but because the code cannot follow an edge it never imports. A run that
  pinned some bytes consumed those bytes.
* **No schema change.** `SCHEMA_VERSION` remains 3; replay required no new
  persisted field.
* **No changes to the import pipeline.** Replay consumes persisted canonical
  artifacts, never raw provider batches.

## Rejected alternatives

**Flat sequential same-time event callbacks.** The conventional design, and the
reason this ADR exists. It creates decision boundaries between simultaneous
observations that the data does not license — see "The failure this phase is
organised around". Rejected as a look-ahead mechanism, not as an ergonomics
trade-off.

**Trade / Quote / Bar priority (in any order).** A table mapping record type to a
rank would be a fabricated exchange sequence presented as a convention. The
repository holds no evidence for any such ordering. Two architecture tests guard
its absence, and the division of labour between them matters: the **API
allowlist** pins each replay module's exact public surface, so a `RECORD_PRIORITY`
constant fails the build simply by existing; a supplementary test refuses a
`RecordType`-keyed mapping *literal*, which catches the obvious shape but not one
built by comprehension. The allowlist is the guarantee; the literal check is a
convenience that fails earlier and more legibly.

**Sorting source datasets during replay.** Rejected in §6: it would emit
semantics no published dataset hash describes, and would hide the defect it
appears to fix.

**Caller-order tie-breaking.** Would make `ReplayConfig(A, B)` and
`ReplayConfig(B, A)` different runs. Rejected: the inputs are a set.

**Path, provider, or physical-hash tie-breaking.** Each would make market output
depend on storage layout, vendor choice, or Parquet encoding — the three things
ADR-012 separated identity from in the first place.

**Replaying two manifests of one semantic dataset.** Silent double counting.
Rejected in §9.

**Automatic reconciliation of overlapping histories.** Would make replay a merge
layer, quietly deciding which of two claims about an instant is true. Rejected in
§10; ADR-013 already established that the platform does not choose.

**Schema-v2 identity inference.** `ESM6 -> CME:ES:M2026` requires guessing a
venue and a decade. Rejected by ADR-012 and again here.

**A wall-clock `ReplayClock`.** Output that depends on CPU speed is not
reproducible research.

**`ReplayStreamHash` in Phase 3.** No current need; see above.

**True seek and checkpoints.** Would promise reconstructed state this phase
cannot produce.

**Continuous-series hidden source selection.** A helper resolving a
`ContinuousSeriesId` into a set of manifests would put an economic decision — which
contract was active when — inside a data-transport layer, and would make a replay
input non-exact.

## Correcting `docs/replay_rules.md`

`docs/replay_rules.md` is a Phase 0 aspirational document written before any
implementation existed. Three of its clauses are contradicted by this decision
and are amended rather than left to rot:

| `replay_rules.md` said | Now |
| --- | --- |
| "Sorting priority: 1. Timestamp 2. **Event Type Priority** 3. Sequence Number" | Timestamp decides the frame; within a frame the order is technical and non-causal. There is no event-type priority, and no sequence number is persisted (ADR-003). |
| "Replay processes **one event at a time**." | Replay processes one *frame* at a time. Per-event delivery is the look-ahead mechanism this ADR rejects. |
| "**Replay owns the clock.**" | Replay time is `ReplayFrame.availability_time`. There is no clock object to own; the intent — that no consumer asks the OS for the time — is preserved and strengthened. |

Everything else in that document (determinism, UTC-only, no look-ahead, bars
visible only after they close, ticks preserving exchange ordering, no reordering,
seeded randomness only, regression tests for every replay bug) stands unchanged
and is implemented.

## Known limitations, stated rather than hidden

* **No latency model exists, and none is approximated.** For trades and quotes,
  availability time equals the persisted timestamp because there is no second
  coordinate. This is a limitation of the data contract, not a finding about
  markets.
* **Memory scales with the selected input artifacts.** Each selected source is
  fully materialised. A dataset larger than memory is not supported today. This
  is the correctness-first reference implementation; the public contracts say
  nothing about how rows are held, so a streaming or memory-mapped internal
  rewrite would change no frame semantics. Unprofiled, and not claimed to be
  fast.
* **One instant can produce a very large frame.** All simultaneous observations
  stay together by design, so a dense microsecond is a memory cost that cannot be
  chunked away without breaking the phase's central invariant.
* **Each source artifact is read more than once during preparation** — the same
  trade ADR-013's comparison documents. A straightforward profiling target; none
  of the guarantees depend on the pass count.
* **`PreparedSource` and `PreparedRow` are `NamedTuple`s and do not validate.**
  Hand-assembling one bypasses `build_prepared_source`. This is an explicit,
  visible bypass in the spirit of `model_construct` (see `CanonicalModel`), but
  its two consequences were silent rather than loud, so `frame_timeline`
  re-establishes both across every source **before returning an iterator at
  all**: rows that are not non-decreasing (replay time running backwards), and a
  cached `availability_time` that disagrees with its payload. The second is the
  more insidious — the cache steers every decision the interleave makes, so a
  lying row used to be caught only when its event was finally constructed, after
  earlier frames had already reached the consumer. `frame_timeline` is therefore
  deliberately *not* a generator function: a generator would defer validation to
  the first `next()` and still hand out the frames that preceded the bad row.
* **The overlap refusal is per-window, not per-dataset.** Overlap is judged on
  the rows a run selected, so narrowing a `ReplayRange` until two
  globally-overlapping datasets no longer overlap *within the window* makes the
  configuration acceptable, and replay composes their disjoint selected shards.
  That timeline is unambiguous, but replay does not warn that the datasets
  disagree outside it. The asymmetry with §6 is deliberate — a source's validity
  must not depend on who is asking, whereas a run's ambiguity genuinely is a
  property of what that run consumed — but it means
  `AmbiguousReplayOverlapError` asserts "this run would have had to interleave
  two histories", not "these datasets agree elsewhere". Detecting the latter is
  ADR-013's explicit `compare_datasets`.
* **Replay cannot answer "would this data have been available to a strategy on
  date T?"** ADR-013 established that the platform has no trustworthy
  source-publication time. Replay inherits that limitation exactly and does not
  approximate around it.
* **Concurrency and durability inherit ADR-012's terms**: a single writing process
  per catalog root is assumed, and `os.replace` gives visibility atomicity, not
  durability. Replay is read-only and adds no new assumption.
