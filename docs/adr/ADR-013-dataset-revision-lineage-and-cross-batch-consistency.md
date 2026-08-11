# ADR-013: Dataset revision lineage and cross-batch consistency

- Status: **Accepted**
- Date: 2026-08-11
- Related: ADR-003 (trade identity), ADR-005 (bar identity and conflicts),
  ADR-009 (futures identity), ADR-012 (dataset artifact identity, schema v3)

## Problem

Phase 2.3 made datasets and their manifests immutable and content-addressed.
Corrections are nonetheless real: a vendor reissues a session, a gap is
backfilled, a bad export is replaced. So the platform needs to answer

> Manifest B is said to correct manifest A. Can that be represented without
> mutating A, without redirecting anyone who pinned A, and without pretending to
> know correction semantics the stored records cannot prove?

The last clause is the hard one. ADR-003 already established, empirically, that
the obvious approach destroys data.

## Decision

### Lineage is a separate immutable artifact, not a manifest field

`DatasetManifest` gains **no** `parent`, `supersedes`, or `revision` field, for
two independently fatal reasons:

- A lineage claim is routinely made *after* both manifests exist, and a manifest
  is immutable — so there is no moment at which such a field could be filled.
- The manifest hash covers every field, so adding one would change a dataset's
  **identity** because something was later said *about* it.

Instead, `SupersedesRelation` is its own content-addressed artifact, published as
`<catalog_root>/lineage/<relation_hash>.json`.

### Exactly what the relation means

> `successor` was **explicitly declared by an operator** to be a
> correction/replacement successor of every manifest in `predecessors`.

A historical claim, and nothing more. It does **not** mean the successor is
preferred, latest, better, or automatically selected; it does not invalidate a
predecessor; and it never redirects a lookup.

### One relation type, one provenance kind

Only `SUPERSEDES` exists. `DERIVED_FROM` is deliberately absent: Phase 2.3's
`TransformationProvenance` already records migration and transformation history,
so a second mechanism for the same fact would create two answers to one question.

`RelationProvenanceKind` has exactly one member, `OPERATOR_DECLARED`. A
`SOURCE_DECLARED` member would be a value in the hashed payload that nothing can
produce: no provider adapter exposes a verified correction declaration, and
ADR-003 records that neither vendor decoder is verified against real output. It
stays an enum rather than a bare constant because the payload needs a closed
vocabulary with a stable token, matching `DatasetOrigin` and `TransformationKind`.

### Relation fields and hash

```
format token · successor ManifestHash · sorted predecessor ManifestHash tuple · provenance
```

hashed as SHA-256 over the canonical JSON (sorted keys, no whitespace,
`ensure_ascii`, no non-finite numbers), under the token
`qrt-dataset-supersedes/1`. `SupersedesRelationHash` reuses Phase 2.3's
`HexDigest` base — 64 lowercase hex characters, never normalised, no `sha256:`
prefix — so there is one digest contract in the platform rather than two.

Excluded by construction: any path, the catalog root, the wall clock, any random
or process-derived value, and any record of when or in what order relations were
published.

### Predecessors are a set

Non-empty, exact `ManifestHash` values, duplicates rejected, and serialized
**sorted by digest** so the caller's argument order cannot reach the hash —
`[A, B]` and `[B, A]` produce the identical relation. The model additionally
*refuses* a non-canonical order, so the builder's sort is not the only thing
standing between a caller and two spellings of one claim.

### Invariants, and where each is enforced

Structural, in the domain model, so a bad claim is unconstructible: format token;
non-empty, unique, sorted predecessors; no self-reference; exact digest types;
self-consistent hash.

Cross-manifest, at publication, because hashes alone cannot decide them: every
referenced manifest must be published; successor and predecessors must share a
**record type** and a **canonical `FuturesContractId`**; and the successor's
`SemanticDatasetHash` must **differ** from every predecessor's.

That last one carries real weight. **Same semantic hash + different manifest hash
is not a correction** — it is provider or physical-encoding variation, which
ADR-012 explicitly models as legitimate one-to-many. There is no corrected data
for the claim to be about, so the claim is refused. The converse is equally
important and is enforced by *absence*: **different semantic hashes prove
nothing**. Two datasets may differ for a hundred reasons. No relation exists
until a caller publishes one, and neither comparison nor registration ever
creates one.

### DAG: branching, multi-parent, no cycles

Edges run **predecessor → successor**. `successors_of(A)` answers "what has been
declared to correct A?"; `predecessors_of(B)` answers "what does B claim to
correct?".

Branching (`A → B` and `A → C`) is legitimate and **neither branch is chosen**.
Multi-parent (`A, B → C`) is legitimate: one correction may replace several
datasets, and no linear chain is forced.

Cycles are refused. Detection runs over **all** published relations plus the
proposed one — not just the new edge — because a relation with several
predecessors can close a loop through any one of them. It is an iterative
depth-first search with explicit colouring, so a deep chain cannot exhaust the
interpreter stack, and it visits nodes and neighbours in canonical digest order,
which makes the reported cycle a function of the graph's *content* rather than of
publication order.

### No revision number, no family id, no "latest"

A branching DAG has no sequence to number, so `revision=1` would be a fiction the
moment anyone corrected one dataset twice. A `DatasetFamilyId` would add a
grouping policy with no current need — the relation graph already groups
manifests, and it does so from evidence rather than from an assignment.

And there is **no `latest`, `current`, `active`, or `preferred`**. A DAG with two
independent corrections of one predecessor has no natural answer, and a linear
chain must not be allowed to imply one. A future caller pins an exact
`ManifestHash` or applies an explicit policy of its own. An architecture test
fails the build if such a function name appears anywhere in the package.

### Old-run stability

After `B supersedes A`:

```
read_manifest(catalog, A)        -> A            (unchanged, byte-identical file)
A.semantic_hash / physical_hash  -> unchanged
verify_registration(... A ...)   -> OK
successors_of(A)                 -> (B,)         and this is the ONLY place B appears
```

No API substitutes the successor. This is the entire reason lineage lives beside
the data rather than inside it.

### Publication commit point

1. build the relation (structural invariants);
2. resolve the manifests and check the cross-manifest invariants;
3. check the resulting graph stays acyclic;
4. **publish the relation file** — the lineage commit point;
5. update the rebuildable index, strictly afterwards.

Crash before 4: no relation exists. Crash between 4 and 5: the relation exists
and is authoritative, the index is stale, and **no navigation answer changes**.

Publishing the same claim twice is idempotent — the hash is a function of the
content — and a published relation file is never overwritten; differing bytes
under one relation hash is a typed `RelationConflictError`, meaning either a
SHA-256 collision or a non-deterministic serializer.

A relation does **not** require the artifacts to be online. It is a claim between
two manifest *identities*, so archived data stays claimable. Comparison, which
reads rows, has its own artifact requirements.

### The lineage index is a cache, and navigation does not read it

The index maps `ManifestHash → relation hashes`, is rebuilt by
`rebuild_lineage_index`, and `verify_lineage_index` reports staleness.

**Navigation reads the published relation files, never the index.** That is the
ADR-012 lesson applied rather than restated. There, the catalog index held the
only manifest→location binding, so losing it lost something real. Here the
relation files *are* the graph — each is named by its content hash and names its
own successor and predecessors — so an index can only restate them, and the
restatement is the half that can go stale. Code that trusted it would give a
silently wrong answer inside the crash window above. So "the index is never
authority" is a fact here, not an aspiration: it is provable by deleting the file
and observing that every answer is unchanged.

A committed relation that fails to parse is **not** skipped. Silently omitting a
relation would silently omit a correction, so corruption is loud. A `.partial`
file is excluded by the `*.json` glob itself and is never a published claim.

### Comparison is explicit, and ephemeral

`compare_datasets(catalog_root, storage_root, left, right)` is a call someone
makes. Nothing in import or registration consults it, so **an import produces the
same result regardless of what else the local catalog contains**. An architecture
test asserts the application layer does not import the catalog at all.

The result is a plain value object — not published, not hashed, not indexed. A
comparison is a measurement of two artifacts at a moment, not a new immutable
fact, and persisting it would create a second artifact subsystem to keep
consistent with the first.

Before any row is read: both manifests are resolved exactly, comparability is
checked, the artifacts are located, and each is verified against its manifest.
Comparing a replaced or corrupted file would produce confident evidence about
data nobody stored.

**Comparability** requires the same `FuturesContractId` and the same
`RecordType`; anything else is `DatasetNotComparableError`. Calling unrelated
datasets "conflicting" would be a false statement about both.

**Semantic-hash shortcut.** Identical `SemanticDatasetHash` means the canonical
ordered logical datasets are equal by the Phase 2.3 contract, so the comparison
short-circuits to semantically-identical. Different physical encoding or
provenance does not make two datasets different data — and this is emphatically
not a revision.

### What comparison can and cannot prove, per record type

**Trade — no logical event identity.** ADR-003 is explicit and the code matches:
`record_identity()` returns `None` for trades. Identifying a trade by
`(timestamp, instrument, price, size, side)` was found to *destroy data*, because
one-lot fills at the same price inside the same microsecond are ordinary tick
data. ADR-003's root cause is the sentence this phase honours: *"Attribute
equality was standing in for an identity that does not exist."* The proposed
`venue`/`vendor_trade_id`/`vendor_sequence` contract remains unimplemented.

So trade comparison determines: the same canonical ordered sequence; the same
exact canonical row multiset; exact-row overlap multiplicities; left-only and
right-only exact rows; and temporal overlap. It **cannot** determine that one row
corrects another. A shared timestamp is **not** a logical event identity.

**Quote — the same position.** Quote identity is every required field, which is
the same attribute equality, i.e. the same non-identity; ADR-005 explicitly left
a narrower `(instrument, timestamp)` quote key out of scope. Same conservatism,
same refusal to infer.

**Bar — a proven natural key.** ADR-005 established that a bar is a summary
*keyed by its period*, with OHLCV as claims about that period. Its key is
`(instrument_symbol, interval_start, interval)`; this phase substitutes the
**canonical contract** for the vendor alias, which schema v3 makes possible and
ADR-012 requires — `vendor_symbol` is provenance and must never decide whether
two rows describe the same thing. The contract is a dataset-level fact
established once for both datasets, so the per-row key is
`(interval_start, interval)`.

Same key + same canonical payload is identical overlap; same key + different
payload is a **proven conflict** — with **no direction implied**. ADR-005 refused
to guess a winner inside one batch, and nothing across batches makes the guess
safer.

**Bar duplicate keys are refused, not resolved.** ADR-005's uniqueness guarantee
is an *import-validation* property. Storage enforces nothing of the kind: Phase
2.3's own test proves duplicate rows survive a round trip, and v3 writes
deliberately bypass the importer. So a v3 bar artifact can hold two rows for one
period, and a `dict[key, Bar]` would silently pick one. `AmbiguousBarKeyError` is
raised instead, because first-wins and last-wins are both guesses about which
claim counts.

### Exact-row semantics

Rows are compared by their **canonical semantic encoding** — the same bytes the
Phase 2.3 dataset hash is built from, exposed as `canonical_record_bytes`. Using
one definition means "same row" and "same dataset" cannot drift apart, and it is
vendor-neutral by construction rather than by a filter someone must remember:
`instrument_symbol` is not among the encoded fields. Parquet bytes and writer
metadata are never compared.

**Duplicate multiplicity is preserved.** `[X, X, Y]` and `[X, Y]` are not the
same multiset. Nothing is de-duplicated, because ADR-003 established that
repeated identical trades are real.

**"No exact overlap" is reported as exactly that** — `exact_row_overlap_count ==
0` — and never as "the underlying market events are disjoint." Without a stable
event identity, two different rows may still describe one event with one of them
corrected. The API name `has_exact_row_overlap` preserves the distinction.

**Reordered-only.** Because Phase 2.3 semantic identity includes row order, the
same multiset in a different sequence yields a different `SemanticDatasetHash`.
That is reported as `reordered_only=True`. It is not a correction, not a
supersedes, and it publishes nothing.

**Temporal ranges** are a prefilter used only in the safe direction: disjoint
ranges prove no shared instant; overlapping ranges prove nothing about the rows
inside them. Coverage summaries never substitute for exact row comparison.

### Late arrival, providers, merging

A newly appearing historical row is **not** labelled a correction. It may be a
backfill, a late observation, a missing-data repair, an additional event, or a
provider difference. Comparison reports evidence; direction stays an explicit
operator claim.

Two providers disagreeing produces evidence, not a winner. There is no
cross-provider preference anywhere. Two providers *agreeing* produces the same
`SemanticDatasetHash`, which is not a revision and needs no relation.

Comparison never mutates, merges, deduplicates, or produces a reconciled
artifact. `merge_datasets`, `reconcile_datasets`, and `apply_revision` do not
exist, and an architecture test fails the build if they appear.

## The limitation that matters most

**Phase 2.4 does not establish historical knowledge state.**

Lineage records correction relationships known to QRT **now**. It does not record
when a correction became *available*. Manifests carry no trustworthy source
publication or correction-availability timestamp, and the available proxies are
all invalid: catalog publication time, filesystem mtime, the current clock, and
registration order each measure when this machine happened to learn something,
not when the market data was actually revised.

So this phase cannot answer *"would this corrected dataset have been available to
a strategy on date T?"* — and it must not appear to. That would require
source-backed availability semantics that no current provider adapter supplies.
Building a backtest on a fake as-of capability would produce look-ahead bias of
exactly the kind ADR-002 exists to prevent, so the capability is absent rather
than approximated.

## Alternatives considered

**A `parent`/`supersedes` field on `DatasetManifest`.** Rejected: unfillable
(claims are made after the fact, manifests are immutable) and identity-corrupting
(the hash covers every field).

**A mutable "latest" pointer per dataset family.** Rejected: it has no defined
value on a branch, it makes research results depend on when the run happened, and
it is precisely the redirection this phase exists to prevent.

**A revision sequence number.** Rejected: no total order exists over a branching
DAG. A provider's own revision number could become source-backed provenance
later; it is not universal identity.

**Schema v4 with a vendor sequence or event id**, to make trade/quote correction
matching possible. Rejected outright. It would put one vendor's semantics into
the canonical market-data schema, and ADR-003 records that what the vendors
actually publish is currently an assumption rather than a verified fact. The
limitation is accepted and documented instead.

**Timestamp-based trade/quote correction matching.** Rejected — this is the
ADR-003 defect in a new location. Co-incident executions are ordinary, so a
shared timestamp identifies nothing.

**Automatic lineage inferred from comparison.** Rejected: differing semantic
hashes are consistent with correction, backfill, a different provider, a
different time range, and simple unrelatedness. Inference would manufacture a
claim from an ambiguity.

**Automatic merge or reconciliation.** Deferred, not rejected in principle, but
it requires a resolution policy with evidence behind it — ADR-005 already refused
to guess a winner between two conflicting bars inside one batch, and nothing here
makes that guess safer across batches.

**A SQL or graph database for the lineage graph.** Rejected for this phase:
deterministic JSON artifacts plus a rebuildable cache meet every requirement, and
a database would become a second source of truth for immutable claims.

## Consequences

Positive: corrections are representable without mutation; a pinned
`ManifestHash` is stable forever; branching and multi-parent histories are
first-class; cycles are impossible; the graph survives losing its index
completely; comparison gives precise, provable evidence; and no clock, path,
random source, or registration order reaches any identity.

Negative, stated plainly:

- **Trade and quote corrections cannot be detected**, only exact-row differences
  reported. This is a real capability gap and it is accepted rather than faked.
- **Bar comparison refuses duplicate keys** rather than analysing them, so an
  artifact with repeated periods must be inspected directly.
- **Comparison is in-memory**: rows are decoded and counted with a `Counter`, and
  bars are keyed in a dict. Correct and deterministic at current scale, and
  explicitly not optimised for artifacts that do not fit in memory. No profiling
  has been done, so no optimisation has been attempted.
- **`find_by_semantic_hash` and graph navigation both scan published files**, so
  both are O(published artifacts). The index exists to accelerate discovery, and
  deliberately is not on the correctness path.
- **The lineage index write is not atomic with publication**, so a stale index is
  reachable; it is harmless by design and detectable by `verify_lineage_index`.
- Durability is not claimed, on the same terms as ADR-012: `os.replace` gives
  visibility atomicity, no `fsync` is issued, and directory `fsync` has no
  Windows equivalent.

## Future handoff

A future `RunManifest` pins exact dataset `ManifestHash` values. Publishing
lineage after a run **does not alter that pin** — the run consumed the bytes it
consumed. A future tool or UI may surface "this dataset has successors" as
information beside a result, without changing any execution input. That
orchestration is not implemented here.

## Phase 3 boundary

Untouched and unreferenced by this phase: replay and its total ordering,
execution, portfolio, strategies, continuous-price synthesis, options, and UI. An
architecture test asserts the lineage and comparison modules import none of them.

## Falsification record

**Pass 1** attacked D1–D32 from the phase brief: automatic correction from
differing semantics, same-semantic supersedes, insertion order as revision order,
mtime, successor redirection, wrongly rejected branches, cycles, manifest
mutation, path/UUID/clock in the relation hash, trade and quote timestamp
inference, missed bar conflicts, lost multiplicity, compression affecting
semantics, ambient import coupling, provider preference, pinned-hash redirection,
corrupt-artifact comparison, index loss, the publication crash window, duplicate
publication, predecessor ordering, self-supersedes, deferred `DERIVED_FROM`,
publication time as availability, schema v4, merge code, replay leakage,
byte-identical alternatives, duplicate bar keys, and semantic inequality as
evidence. **All 32 held.**

Four initially reported as broken were **probe defects, not code defects**: the
greps matched prose in the modules' own docstrings *documenting the absence* of
the thing being probed ("No revision number, because…", "…any random or
process-derived value…", "cataloguing belongs to later phases"). The probes were
rewritten to parse the AST — stripping docstrings and inspecting real imports —
after which all 32 held with nothing changed in the source. Recorded because a
detector that cannot distinguish code from a comment denying that code is a
detector that will eventually hide a real defect.

**Pass 2** was an independent hostile review of the whole diff, and it broke
things pass 1 had not. Three defects, six weaknesses, five nits; all real ones
fixed with regression tests.

Defects:

1. **Concurrent publication could leave a cyclic graph on disk**, and — worse —
   the losing publisher got an untyped `PermissionError` *after* its relation was
   committed, so it was told the operation failed when the claim existed. Two
   causes, both fixed. `write_index_bytes` used a fixed temporary name with no
   `O_EXCL`, unlike the publication primitive directly above it, so two cache
   writers collided on Windows; the temporary now carries the process id. And a
   cache failure after the commit point is now swallowed rather than raised,
   because the relation is published and the cache is rebuildable. The remaining
   race — two publishers each seeing an acyclic graph — is **not** fixable
   without a lock, so it is now stated in the module docstring on the same terms
   as ADR-012's index assumption, and `verify_lineage_acyclic` was added so the
   single-writer assumption is *auditable* rather than merely asserted.
   Separately, a pre-existing cycle used to be blamed on whatever relation was
   published next; the two cases are now diagnosed apart.
2. **`verify_lineage_index` raised instead of returning `bool`** on a corrupt or
   truncated index — the exact trap `verify_registration` documents one module
   over. A corrupt cache now answers `False`, which is the truthful answer.
   `rebuild_lineage_index`'s "total" claim was also too strong and is now scoped:
   total with respect to *ambiguity*, not to corruption, since rebuilding around
   an unparseable relation would silently drop a published correction.
3. **`compare_datasets` tried only the first indexed location** and gave up,
   while `verify_manifest` deliberately tries all of them. One stale entry — in
   the part of the catalog explicitly allowed to be wrong — turned a comparison a
   good surviving copy could answer into a hard failure.

Weaknesses fixed: the semantic-hash shortcut made `bars` mean "not a bar
dataset" *or* "identical bars" depending on a flag, and skipped the duplicate-key
refusal entirely — so a guarantee held only for some comparison partners; the
shortcut is now a reported fact rather than an early return. `MissingRelationError`
was added, because "no such claim" and "the stored claim is corrupt" are
different facts. `LineageIndexEntry` and `DatasetComparison` got the exact-digest
guard the relation already had. `_ranges_disjoint` performed a type-narrowing
filter inside a four-tuple unpack, which would raise `ValueError` from a
prefilter documented to prove nothing; it is now explicit `is None` guards.

The no-selection and no-merge guards were the weakest thing in the diff: a
hand-written substring denylist that a reviewer showed would miss
`newest_successor`, `head_of`, `tip`, and `winning_revision`. They are now an
**allowlist** pinning the exact public surface of all three modules, so any new
public name fails until someone adds it deliberately — which is the moment to ask
whether it selects a winner.

Nits fixed: `_MICROSECOND_UNIT` was duplicated rather than shared;
`AmbiguousBarKeyError` was raised from inside the result's argument list,
discarding row evidence already computed; the relation value objects were not
re-exported from the package that hands them out; and `errors.py` claimed every
class had a concrete trigger while two are abstract bases.

One finding was accepted as correct behaviour and documented instead: a chain
`A → B → C` where C restores A's data is publishable, because the same-semantic
refusal is deliberately pairwise and adjacent — it blocks a claim that corrects
*nothing*, not a chain that returns to an earlier state. Reverts are real. A test
now pins that.

One was accepted as a known cost: comparison reads and re-hashes each artifact
twice, once to verify and once to compare. Correct but 2× the I/O on the API
meant for large work. Left as is under "no optimisation before profiling", and
recorded in the consequences above.
