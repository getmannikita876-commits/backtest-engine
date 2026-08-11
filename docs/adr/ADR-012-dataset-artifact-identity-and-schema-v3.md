# ADR-012: Dataset artifact identity, semantic hashing, and schema v3

- Status: **Accepted**
- Date: 2026-08-11
- Related: ADR-002 (bar availability), ADR-003 (trade duplicates), ADR-007
  (application layer), ADR-009 (futures identity), ADR-010 (calendar),
  ADR-011 (rollover), `docs/data-contracts.md`

> Numbering note: ADR-006 remains reserved by ADR-008's numbering note for the
> storage error contract; this decision takes 012 after ADR-011.

## Problem

Phase 2.0 gave the platform a canonical identity for a listed futures contract.
None of it survived persistence. Schema v2's only instrument column is a single
`utf8 instrument_symbol` holding a vendor alias such as `"ESM6"` — a string that
carries no venue and no full delivery year. ADR-009 stated the consequence and
deferred the fix here; `ImportDatasetResult` documents itself as *"not a dataset
manifest"*.

So a researcher could not answer the question this phase exists for:

> **Exactly which immutable canonical market-data dataset artifact is this?**

There was no dataset identity, no provenance record, no catalog, and no way for
a future backtest to prove which bytes and which semantics it consumed.
Filenames and paths were the only handles, and both are mutable.

## Decision

### Three identities, never collapsed

| Identity | Answers | Changes when |
| --- | --- | --- |
| `SemanticDatasetHash` | *what does this data mean?* | a record, or the row order, changes |
| `PhysicalArtifactHash` | *which exact bytes?* | the file is re-encoded, corrupted, or replaced |
| `ManifestHash` | *which claims and provenance?* | any claim or any provenance field changes |

Keeping them apart is what lets verification distinguish a harmless re-encode
(`PHYSICAL_MISMATCH`) from an edited dataset (`SEMANTIC_MISMATCH`). Collapsing
any pair would destroy that distinction, and it is the most operationally
valuable thing the phase produces.

The clearest demonstration ships as a test: migrating one file twice with
mappings that differ only in an *unused* entry yields the **same** semantic
hash, the **same** physical hash, and a **different** manifest hash.

### Schema v3

`instrument_symbol` is replaced by two columns answering two different
questions:

```
timestamp | canonical_identity | vendor_symbol | <payload columns…>
```

`canonical_identity` is exactly `FuturesContractId.canonical()`.
`vendor_symbol` preserves what the source called it — provenance, never truth.

v3 metadata is the six legacy keys with `schema_version=3`, plus `record_type`
and `canonical_identity`, and the key set must match **exactly**. (v2's
tolerance of unknown extra keys is grandfathered: files already exist under it.)

**Identity is in the metadata as well as the column, deliberately.** Empty
artifacts are supported — `write_trades(path, [])` round-trips today — and an
artifact with no rows has nowhere else to record which contract it holds. The
reader cross-checks the two, so they cannot disagree silently.

The honest limit, stated rather than implied: for an **empty** artifact the
"every row matches the metadata" check is vacuously true, so the metadata is the
sole, uncorroborated identity evidence. What detects tampering there is the
manifest, not the reader.

Read validation order, cheap checks first so a wrong-contract file costs one
footer read rather than a full scan: metadata present → exact key set → version
→ columns match the record type → metadata `record_type` agrees with the columns
→ identity **round-trips** `parse(s).canonical() == s` → every row equals it →
rows decode through the ordinary domain constructors.

The round-trip requirement is not pedantry: a metadata value of
`"cme:es:m2026"` whose every row matched would pass naive equality while being a
string the platform's own parser rejects — a catalogue key written today that
cannot be read back tomorrow, the defect ADR-009's re-validating `model_copy`
exists to prevent.

### v3 is additive; v2 is untouched

New `write_*_v3`/`read_*_v3` take an explicit `FuturesContractId`. The v2
writers and readers are unchanged and still emit and accept version 2, because a
v3 artifact needs a contract the import pipeline has no evidenced way to derive
from an alias. `SCHEMA_VERSION` is now 3 and `LEGACY_SCHEMA_VERSION` is 2.

Two traps that bump would otherwise have sprung, both closed:

- `_arrow_schema_metadata()` is shared by all three v2 schemas, so reading the
  current constant there would have made **v2 writers emit version 3** — a
  silent re-keying of every existing file, with no line of the v2 write path
  changed. The factory now takes the version as a parameter, and the tests that
  compare metadata to the constant were re-pinned to the **literal** `"2"`,
  because a constant-following assertion would have ratified the regression.
- `SCHEMA_VERSION` also gated the **import pipeline** (`ImportBatch`'s default
  and the batch-fatal version check). Left alone, every batch would have
  declared 3 and any caller passing 2 would have become batch-fatal. The import
  layer is repointed at `LEGACY_SCHEMA_VERSION` / `SUPPORTED_SCHEMA_VERSIONS`,
  and a regression test pins that a batch declaring 2 is still accepted and 999
  still rejected.

`validate_storage_schema` takes the expected version keyword-only, defaulting to
**legacy** rather than current — defaulting to "whatever is newest" is precisely
how a v2 file gets validated as v3 on the next bump.

### The semantic hash

SHA-256 over a framed binary stream, fed incrementally so a large dataset never
becomes one byte string:

```
frame(format token) ‖ frame(record type) ‖ frame(schema token) ‖ frame(contract)
    ‖ row₀ ‖ row₁ ‖ … ‖ uint64(row count)      ← footer, not header
```

- **Every string is length-prefixed**, unconditionally. Relying on "these two
  vocabularies happen not to alias" would make injectivity depend on
  `_VENUE_PATTERN`, which is not a property anyone should have to maintain.
- **The record-type token in the header prevents cross-type aliasing**, with
  arithmetic rather than hand-waving behind the claim: fixed row widths are 25
  bytes for a trade, 40 for a quote, 56 for a bar, and `25·8 = 40·5 = 200`, so
  eight trades occupy exactly the row bytes of five valid quotes. The token
  alone is sufficient there, and a test asserts it with the row-count footer
  stripped from both streams.
- **The row-count footer earns its place differently**, and saying otherwise
  would be an overclaim — within one record type every row is fixed-width, so
  the count is already recoverable from the stream length. What it buys is that
  the count becomes an explicit claim the hasher can be *asked* to check, so a
  generator that stops early cannot produce a valid-looking digest. Putting it
  in the footer rather than the header is what makes the pass genuinely single:
  the hasher counts what it was actually fed instead of trusting a length taken
  before iteration.
- **Signedness is per field.** `MIN_TIMESTAMP_MICROSECONDS` is negative, so an
  unsigned timestamp encoding raises on legal historical data; `2**64−1`
  quantities do not fit a signed 8-byte integer. One uniform integer helper
  would have crashed on both.
- **A derived schema token** built from the field-order table goes in the
  header, so reordering or renaming a field moves every hash *even if the author
  forgets to bump the format token* — a structural guarantee where the token
  alone is a human promise.

It hashes **decoded domain records, not stored columns.** `parse_trade_side`
normalises case and whitespace, so a file holding `"BUY"` and one holding
`"buy"` decode identically; hashing columns would call those different datasets,
which is not what "semantic" can mean.

Excluded: vendor symbol, provider, any path, compression, row-group size, writer
version, wall clock, randomness, and **`schema_version`**. That last exclusion is
a decision, not an oversight: the encoding is already pinned by
`qrt-semantic-dataset/1`, so a future v4 re-encoding of the same records yields
the same semantic hash — which is exactly the right answer to "did the data
change?".

### Ordering, and what it does not certify

The hash is a **sequence** hash: same records in a different order give a
different result, and it never sorts. Duplicate rows survive exactly — ADR-003
makes repeated identical trades legitimate.

The order it pins is the artifact's **stored order**. That is *not* the future
replay total order. `source_index` is not persisted (the pipeline discards it
after sorting), so nothing in a stored artifact proves its order came from
`event_ordering_key`. The hash certifies *which* order the artifact has, never
*how* that order was derived. Phase 3 owns replay ordering, and nothing here
anticipates it.

Making the hash order-*independent* would need a canonical sort, and the only
total order available is over the record fields themselves — which ADR-003 makes
ambiguous, since identical trades must stay distinct. Order-sensitive is the
correct call and needs no `source_index`.

### Migration is explicit or it does not happen

The caller supplies alias → exact contract. Every alias in the file must
resolve; an unmapped one is `UnsupportedMigrationError`. There is **no** fallback
that parses a symbol, no current-year default, and no month-cycle arithmetic
anywhere in the module.

- Several aliases mapping to **one** contract is the ordinary case and is fine;
  `vendor_symbol` varies per row and the semantic hash is unaffected.
- A source spanning **two** contracts cannot become one single-instrument
  artifact and is refused rather than silently split — splitting would need an
  output-naming policy, reintroducing paths into a layer just purged of them.
- Unused mapping entries are refused by default, because an unused entry is
  usually a typo whose real symbol then trips the unmapped check and produces a
  confusing second-order error.
- The whole declared mapping is hashed, sorted by symbol with plain code-point
  comparison, serialized as a **list of pairs** so ordering is explicit in the
  structure. `ensure_ascii` stays on: vendor symbols are not constrained to
  ASCII, and the established idiom encodes to ASCII.

Migration creates a **new** artifact; the v2 input is never modified. Bytes are
not preserved and cannot be (reading normalises), so the honest link back is the
source artifact's physical hash recorded in provenance. This is lineage, not a
correction graph — Phase 2.4 owns revisions.

### The manifest, and why it has no path

`DatasetManifest` records what an artifact *is*: both identities, schema version,
record type, contract, row count, time bounds, and structured provenance. It
records nothing about where the artifact is stored — **there is no location
field at all**, so relocation cannot change identity by construction rather than
by policy.

The manifest hash covers every field except itself, is re-derived on
construction, and a manifest carrying a hash that does not describe its contents
is unconstructible (the ADR-010/011 pattern). `row_count == 0` if and only if
both time bounds are absent. Time bounds are computed by a **full scan**, never
first-and-last: storage never sorts, so an unsorted artifact is legal and
`records[0]`/`records[-1]` would produce a manifest that is self-consistent and
wrong about its own artifact.

No free-form notes field exists. A text field on an immutable model invites
exactly one question — does editing it change identity? — and the cheapest
correct answer is for it not to exist.

`SourceProvenance.vendor_symbols` is a **sorted unique tuple**, not a scalar.
Several vendor spellings legitimately map to one contract, so a single-alias
field would either be a lie or force an artificial one-alias rule. It is
recomputed from the artifact during verification, never taken from the first row.
`provider_token` is an operator-declared opaque label in the spirit of
`Venue.code` — explicitly not a filesystem path and not a Python class name,
which change under refactoring.

### The catalog

`<root>/manifests/<manifest_hash>.json` holds immutable published manifests;
`<root>/index.json` holds the rebuildable location index. Manifest filenames are
content-derived, so a filename is discovery convenience and never identity.

**One semantic hash may have many manifests.** Identical records from different
vendors, or under different Parquet encodings, share semantic identity while
differing physically and in provenance. So lookup by semantic hash returns a
**deterministic tuple**, and registering a second manifest for the same records
is *not* a collision. Only the same `ManifestHash` with differing bytes is fatal
— which means either a SHA-256 collision or a non-deterministic serializer.

Byte-identical copies at several locations are legal, and so are several
manifests describing the bytes at **one** location — two providers' provenance
for one artifact is the ordinary one-to-many case, and the index records what the
file holds rather than the claims made about it. What is **refused** is rebinding
a location to *different* bytes: the storage writer overwrites by design, so
accepting that would let the catalog quietly forget the previous bytes ever
existed there.

Registration verifies rather than trusts. Every claim — schema version, record
type, contract, row count, bounds, aliases, both hashes — is recomputed from the
artifact first. There is no partial registration.

The **record type** is checked against what the artifact declares, not only
against what its rows imply. For an empty artifact there are no rows to imply
anything, so the declared type in the metadata is the only evidence the file
contains; a reader that discards it makes a wrong record-type claim about an
empty dataset unfalsifiable. `read_records_v3` therefore returns the declared
type alongside the records.

**Publishing the manifest is the commit point**, and everything that can fail is
made to fail before it. The index is read and the updated index computed *first*,
so a corrupt index or a location conflict raises while nothing has been
published — rather than afterwards, which would report a failure for a dataset
that is in fact registered. Crash before publication: the artifact may be
orphaned, no dataset is registered, nothing is corrupt. Crash after publication
but before the index write: the dataset **is** registered and `rebuild_index`
recovers the location, subject to the limit below.

### Resolution is two hops, and the index owns only the second

The index maps `PhysicalArtifactHash → locations`. It does **not** map manifests
to paths, because a manifest already carries its own `physical_hash` immutably
and under its own hash. Resolution therefore runs:

```
ManifestHash --(published manifest)--> PhysicalArtifactHash --(index)--> locations
```

Each hop is answered by the layer that owns it, and the mutable half stores
nothing the immutable half already states. A `CatalogEntry` is two fields —
the bytes and the path — and asserts one present-tense, verifiable fact: these
bytes are at this path.

An earlier version of this design bound `ManifestHash → ArtifactLocation`
directly, storing `semantic_hash` and `manifest_hash` in the index alongside the
path. That duplicated immutable manifest content into mutable state, and the
duplicate was the half that could rot. It also made `rebuild_index` ask an
unanswerable question: with two manifests sharing one physical hash, hashing
cannot say which manifest "belonged" at which path, so rebuilding either
invented a pairing or refused. Refusing was correct *given the binding* — but
the binding itself was the defect. Location was defined as mutable discovery
state and never provenance, so there is no historical pairing to recover.

`rebuild_index` is consequently total: hash every candidate artifact under the
storage root, record it if some published manifest declares that hash, done.
There is no case it must refuse and no `AmbiguousArtifactBindingError`. Two
manifests pinning one physical hash both resolve through **any** surviving copy,
so a single remaining artifact recovers both — with no provenance invented and
no immutable hash touched. Two byte-identical copies produce two entries, which
is correct: one physical artifact in two places, either satisfying either
manifest.

`find_by_semantic_hash` reads the **published manifests**, not the index,
returning manifests ordered by manifest hash. That is what "the index is not
authority" has to mean if it means anything: semantic lookup keeps working with
`index.json` deleted.

Verification splits accordingly. `verify_manifest` asks whether *any* current
copy satisfies a manifest — the right question when copies are interchangeable.
`verify_registration` takes a manifest hash **and** a location explicitly,
because distinguishing `PHYSICAL_MISMATCH` from `SEMANTIC_MISMATCH` requires a
specific file to inspect; naming both is honest where an entry conflating them
was not.

### Durability is not claimed

`os.replace` gives **visibility** atomicity: no reader observes a half-written
file at the target path. It does **not** give durability. Neither the catalog
nor the storage layer calls `fsync` on the file or the containing directory, so
a power loss can leave a renamed file whose blocks never reached disk. Directory
`fsync` — the POSIX mechanism that would close this — has no Windows equivalent,
so a portable guarantee is unavailable and is therefore not offered. The recovery
story is **detection, not prevention**; that is what the physical hash is for.

Concurrency is held to the same standard. **Manifest publication is safe**: the
catalog creates its own temporaries with `O_EXCL`, so racing publishers get a
loud `FileExistsError` rather than an interleaved file. The storage layer's
deterministic `.partial` name has no such guard, so a single writer per artifact
path is assumed there.

**The index is not safe**, and pretending otherwise would be worse than the
limitation. `register_dataset` reads the index, adds an entry, and writes it
back; two registrations interleaving between the read and the write leave the
second one's entry missing. No lock is taken, because a correct cross-platform
file lock is a design decision this phase does not need to make — the manifest
is still published, so the dataset *is* registered, and `rebuild_index` recovers
the location whenever the physical hash identifies its manifest. A single
registering process per catalog root is assumed, and that assumption is written
down rather than relied on quietly.

### No false dependencies

Raw listed-contract datasets pin **no calendar** and **no roll schedule**.
Trades and quotes do not depend on calendar materialization, and bars keep
ADR-002's interval/availability semantics untouched. A pin would only be
justified if a dataset *transformation* had actually consumed one, and none
does. The manifest has no field that could carry one, and a test asserts it.

## Alternatives considered

**A UUID or timestamp dataset id.** Rejected: the same artifact registered twice
would acquire unrelated identities, which defeats the purpose. Identity is
content-addressed throughout.

**Hashing the raw Parquet bytes as the dataset's semantic identity.** Rejected:
it would make compression settings, row-group size, and the writer's version
string part of what the data *means*, so a PyArrow upgrade would look like a data
change.

**Hashing a replay key `(timestamp, event_type, source_index)`.** Rejected
twice over: `source_index` is not persisted, so the hash would be uncomputable
from a stored artifact, and it would bake a Phase 3 ordering decision into a
Phase 2.3 artifact.

**Putting the artifact path in the manifest.** Rejected: moving a file would
change its identity, and a Windows-written manifest would be meaningless on
Linux.

**A DuckDB or SQL catalog.** Rejected for this phase: deterministic JSON plus a
rebuildable index meets every requirement, and a database would become a second
source of truth for identity.

**Indexing `ManifestHash → ArtifactLocation`.** Implemented first, then removed.
It copies `ManifestHash → PhysicalArtifactHash` — already an immutable published
fact — into mutable state, and it asserts a manifest↔path pairing that nothing
needs and that rediscovery cannot reconstruct when several manifests share one
physical hash. Indexing `PhysicalArtifactHash → locations` keeps every invariant,
stores strictly less, and makes rebuild total.

**Implicit migration by parsing vendor symbols.** Rejected outright — it is the
single inference this phase exists to make impossible.

**Reusing `ImportRecordType` for the storage vocabulary.** Not possible: the
layering forbids storage importing `data_import`. A domain `RecordType` was
added with **lower-case** values, and the casing matters — `StrEnum` members
compare *and hash* equal across enums, so equal values would let an import-layer
member silently key a domain hash. A canary asserts the value sets stay disjoint.

## Consequences

Positive: a dataset has a provider-neutral semantic identity, an exact physical
identity, and a provenance-bearing manifest; relocation is identity-free; the
index is disposable; a replaced artifact is detected and an edited one is
distinguished from a re-encoded one; and no identity anywhere depends on a
clock, a path, or a random source.

Negative, stated plainly:

- **The import pipeline still writes v2.** v3 requires a contract it cannot
  derive, so v3 artifacts come from a direct v3 write or from migration. Wiring
  the importer needs an operator-declared contract per dataset and is future work.
- **`canonical_identity` is stored per row with dictionary encoding disabled**
  (a deliberate reproducibility choice in the storage layer), so a tick dataset
  pays roughly 12–16 bytes per row before Snappy. Accepted rather than quietly
  enabling dictionary encoding for v3, which would break the uniform-encoding
  property.
- **An empty artifact's identity is uncorroborated by rows**, as described above.
- **SHA-256 here is integrity, not authenticity.** It proves an artifact is
  byte-identical to one previously seen; it is not a signature and says nothing
  about who produced it.
- The manifest and index formats are versioned but have no migration tooling;
  a format bump re-keys manifests and would need one.

Out of scope and untouched: revision/supersedes graphs, late-arriving
corrections, replay and its ordering, execution, portfolio, strategies,
continuous-price synthesis, options, remote catalogs, distributed transactions,
and UI.

## Future handoff

A future `RunManifest` can pin its inputs with, at minimum, each dataset's
`SemanticDatasetHash` (the strongest research-data pin) and `ManifestHash` (which
adds provenance), plus — separately and only where relevant — a calendar content
hash (ADR-010) and a roll-schedule content hash (ADR-011). Phase 2.3 exposes all
of these and implements none of that orchestration.

## Falsification record

**Pass 1** attacked D1–D25 from the phase brief directly — order sensitivity,
re-encode divergence, byte-copy stability, unmapped-alias refusal, alias
neutrality, falsified `row_count`/semantic/physical claims, post-registration
replacement, duplicate registration, v2-as-v3, false calendar and roll pins,
provider leakage, wall clock and randomness, path leakage, index loss, wrong file
at a location, `model_copy` bypass, `source_index` dependence, framing
ambiguity, multi-instrument migration, and empty-dataset identity. **All held.**

One defect was found by the phase's own tests: `TransformationProvenance`
rejected a `SYMBOL_MAPPING` with an empty mapping, which made *migrating an
empty v2 artifact* unrepresentable even though that is a real migration with
legitimately nothing to map. The invariant was relaxed in that direction only —
an untransformed dataset still may not claim a mapping — with a regression test.

**Pass 2** was an independent hostile review of the whole diff, and it broke
things pass 1 had not. Six defects, each now fixed with a regression test:

1. **A manifest's `record_type` was never verified.** For an empty artifact a
   manifest claiming the wrong type verified *and registered*. Fixed by carrying
   the declared type out of the reader and comparing it.
2. For a non-empty artifact the same lie escaped the `CatalogError` taxonomy as
   a `SemanticEncodingError` raised incidentally inside the hasher, rather than
   being caught as a `SchemaMismatchError`.
3. **`rebuild_index` keyed manifests by physical hash**, collapsing the
   one-to-many case: two byte-identical artifacts under two providers lost one
   registration and had the other's manifest invented in its place. The first
   fix made the rebuild refuse instead of guess; a subsequent architecture
   review found that refusal unnecessary and the manifest→location binding
   behind it wrong, and removed both — see "Resolution is two hops" above.
4. **Migration did not check `destination != source`.** Writing over the input
   destroyed the v2 artifact, and because the source was hashed *after* the
   write, the recorded provenance pointed at the migration's own output. Both
   halves fixed: the paths are compared after `resolve()`, and the source hash
   is taken before anything is written.
5. **`verify_registration` could raise.** `SemanticEncodingError` descends from
   `Exception`, not `ValueError`, so a function whose entire contract is to
   return a `VerificationOutcome` escaped through its own `except` clause.
6. **`OverflowError` is an `ArithmeticError`**, so a corrupt manifest carrying an
   absurd instant escaped `ManifestHashMismatchError` — the same trap the storage
   layer documents, repeated in the manifest reader.

Four weaknesses were also fixed: a corrupt index now raises a typed
`CatalogIndexError` and does so before the commit point; the three digest fields
are exact-type-checked like the contract and record type, since `@final` binds a
checker and not the interpreter; `schema_version` is validated on the model
rather than only at the registration boundary; and the non-atomic index
read-modify-write is documented as a single-writer assumption instead of being
left implicit.

Pass 2 also removed two overclaims, both worth recording because an overclaimed
guarantee is one nobody re-checks: two error classes were exported while being
raised nowhere, and two filter branches described protection that could never
run because the glob already excluded the files. The row-count footer's rationale
was corrected as described under "The semantic hash". Missing coverage was added
for `PHYSICAL_MISMATCH` and `MISSING_MANIFEST`, for all three v2 schemas rather
than one, and for the determinism sweep's own blind spot — `data/artifact_hash.py`
and `domain/common.py`, which hold half the phase's guarantees and matched no
subject token, were covered by no guard at all.
