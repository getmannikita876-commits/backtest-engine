"""Resolving, verifying, and snapshotting the sources a replay will consume.

Everything that can fail because of a *static* problem with the inputs fails
here, before a single frame exists. That is the property this module is
organised around, and it is not merely tidy: a stream that emitted the first
dataset's observations and then discovered the second dataset was corrupt would
have handed a consumer a prefix of a run that can never complete. A partial
replay is worse than a failed one, because it looks like a replay.

So preparation is eager and total. Every manifest is resolved, every artifact
verified, every source's ordering checked, and every cross-source ambiguity
settled before :meth:`PreparedReplay.frames` can yield anything.

What "verified" means here
--------------------------
Each source is checked twice, against two different failure modes:

1. :func:`~...catalog.store.verify_artifact_against_manifest` recomputes every
   claim the manifest makes — physical hash, schema version, record type,
   contract, row count, time bounds, vendor aliases, semantic hash — from the
   file.
2. The rows this run will actually replay are then read, and their semantic hash
   is recomputed and compared to the manifest's.

The second is not a slower restatement of the first. Verification reads the file
and the load reads it again, and a check-then-use pair over a mutable filesystem
proves nothing about the second read unless the second read is itself checked.
Without step 2 the honest claim would be "the bytes were correct a moment ago";
with it, the claim is "the records held in memory are exactly the records this
manifest describes", which is what the snapshot semantics below actually need.

The cost, stated as a factor rather than as "more than once", because the
difference matters to anyone sizing a run: preparing one source touches its file
**five times** — one full byte pass for the physical hash, then a footer read
plus a full read for verification's ``read_records_v3``, then a footer read plus
a full read for the load. The records are also hashed twice. That is the same
trade Phase 2.4's comparison documents, and it is a straightforward profiling
target; none of the guarantees above depend on the pass count.

Snapshot semantics, stated exactly
----------------------------------
Preparation captures a verified **in-memory snapshot** of each source's
canonical rows. Once :func:`prepare_replay` returns, moving the artifact,
re-encoding it, editing it, or deleting it changes nothing about the stream the
returned :class:`PreparedReplay` produces.

That is a real property and it is deliberately not overstated. No file is
locked, no handle is held, and nothing here prevents another process from
changing the artifact — the guarantee is that this run no longer depends on it,
not that the file is safe. Eager verification cannot prove a file will never
change; loading the rows makes the question stop mattering.

Memory
------
Each selected source is fully materialised. Memory use therefore scales with the
size of the selected input artifacts, and a dataset larger than memory is not
supported today. This is the correctness-first reference implementation: the
public contracts here — frame grouping, ordering, provenance — say nothing about
how rows are held, so a future streaming or memory-mapped implementation can
replace the internals without changing what a frame means.
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from pathlib import Path
from typing import final

from quant_research_terminal.catalog.errors import (
    CatalogError,
    MissingArtifactError,
    PhysicalArtifactHashMismatchError,
    SchemaMismatchError,
)
from quant_research_terminal.catalog.manifest import DatasetManifest
from quant_research_terminal.catalog.store import (
    locate_manifest,
    read_manifest,
    verify_artifact_against_manifest,
)
from quant_research_terminal.data.contracts import SCHEMA_VERSION
from quant_research_terminal.data.parquet_store import StorageError, read_records_v3
from quant_research_terminal.domain.dataset_identity import (
    ManifestHash,
    SemanticEncodingError,
    semantic_dataset_hash,
)
from quant_research_terminal.domain.futures_contract import require_listed_contract
from quant_research_terminal.domain.replay import ReplayConfig, ReplayFrame, ReplayPayload
from quant_research_terminal.replay.errors import (
    AmbiguousReplayOverlapError,
    DuplicateReplaySemanticDatasetError,
    ReplayArtifactVerificationError,
    UnsupportedReplaySchemaError,
)
from quant_research_terminal.replay.stream import (
    PreparedSource,
    build_prepared_source,
    frame_timeline,
)


@final
class PreparedReplay:
    """A verified, immutable set of sources that can be replayed repeatedly.

    Build one with :func:`prepare_replay`; the constructor is for that function's
    use and assumes its arguments are already verified.

    :meth:`frames` returns a **fresh** iterator every call, over the same
    snapshot, so two calls produce equal streams. There is no cursor on this
    object, which is why there is no ``reset``: the thing that would need
    resetting does not exist.

    There is deliberately no replay clock, no ``seek``, and no checkpointing.
    Replay time *is*
    :attr:`~...domain.replay.ReplayFrame.availability_time`, so a separate
    mutable clock would be a second, weaker copy of it. And a replay range filters
    the raw information stream; it does not reconstruct whatever state a consumer
    would have accumulated by that point, so calling it a seek would promise
    something no component of this phase can deliver.
    """

    __slots__ = ("_config", "_sources")

    def __init__(self, *, config: ReplayConfig, sources: Sequence[PreparedSource]) -> None:
        self._config = config
        self._sources = tuple(sources)

    @property
    def config(self) -> ReplayConfig:
        """Return the configuration these sources were prepared from."""
        return self._config

    def frames(self) -> Iterator[ReplayFrame]:
        """Return a fresh iterator over this run's frame timeline."""
        return frame_timeline(self._sources)


def prepare_replay(
    *, catalog_root: Path, storage_root: Path, config: ReplayConfig
) -> PreparedReplay:
    """Resolve and verify every configured source, or fail before any frame exists.

    Sources are processed in the configuration's canonical order — sorted by
    manifest digest — so an input set with more than one problem reports the same
    failure whichever order the caller happened to list it in. An error that
    depended on argument order would make a reproducibility platform's own
    diagnostics irreproducible.

    Raises:
        MissingManifestError: if a configured manifest was never published.
        ManifestHashMismatchError: if a published manifest document is corrupt or
            not in canonical form. Both of these keep their catalog names rather
            than being re-wrapped: "is this dataset registered, and is its
            manifest intact?" is the catalog's question and already has a precise
            answer there, so replacing it would tell a caller less.
        UnsupportedReplaySchemaError: if a source is not canonical schema v3.
        ReplayArtifactVerificationError: if no verifiable copy of a source
            artifact can be found — including when the catalog knows of no copy
            at all, and when the bytes change between verification and loading.
            Always chained from the underlying catalog or storage failure.
        NonMonotonicReplaySourceError: if a source's availability times decrease
            in persisted row order.
        DuplicateReplaySemanticDatasetError: if two manifests pin one dataset.
        AmbiguousReplayOverlapError: if two selected sources cover overlapping
            time for one ``(contract, record type)``.
        CatalogIndexError: if the location index is present but unreadable.
        OSError: if a catalog file cannot be read at all. Filesystem failures are
            not wrapped anywhere in this platform; their own exceptions say more.

    Every one of these is raised before a :class:`PreparedReplay` exists, so a
    caller that receives one has received no frames and never will for this
    configuration. Nothing in this list can surface later from
    :meth:`PreparedReplay.frames`.

    The list is the reachable set, not a taxonomy: a corrupt manifest document
    can also surface as a Pydantic ``ValidationError`` or a ``TypeError`` from the
    exact-type guards, because those are the domain models refusing to construct
    an invalid identity rather than replay reporting a condition it anticipated.
    Naming them here as replay errors would claim a handling this function does
    not perform.
    """
    sources = [
        _prepare_source(
            catalog_root=catalog_root,
            storage_root=storage_root,
            manifest_hash=manifest_hash,
            config=config,
        )
        for manifest_hash in config.manifest_hashes
    ]

    # Semantic duplication is settled before overlap, and independently of the
    # replay range: two manifests pinning one dataset are redundant whichever
    # window is asked for, whereas overlap is a property of what a particular run
    # actually selected.
    _reject_duplicate_semantics(sources)
    _reject_ambiguous_overlap(sources)

    return PreparedReplay(config=config, sources=sources)


def _prepare_source(
    *,
    catalog_root: Path,
    storage_root: Path,
    manifest_hash: ManifestHash,
    config: ReplayConfig,
) -> PreparedSource:
    """Resolve one manifest into a verified in-memory source snapshot."""
    manifest = read_manifest(catalog_root, manifest_hash)

    if manifest.schema_version != SCHEMA_VERSION:
        raise UnsupportedReplaySchemaError(
            f"dataset {manifest_hash.canonical()} is schema version "
            f"{manifest.schema_version}; replay consumes canonical version "
            f"{SCHEMA_VERSION} artifacts only. Version 2 persists a vendor alias rather "
            f"than a listed contract, and resolving one into the other would mean guessing "
            f"a venue and a decade. Migrate it explicitly first"
        )

    records = _load_verified_records(
        catalog_root=catalog_root, storage_root=storage_root, manifest=manifest
    )

    return build_prepared_source(
        manifest_hash=manifest.manifest_hash,
        semantic_hash=manifest.semantic_hash,
        contract=require_listed_contract(manifest.contract),
        record_type=manifest.record_type,
        records=records,
        replay_range=config.replay_range,
    )


#: Failures that mean "this copy is unusable", rather than "this run is broken".
#:
#: Each is a statement about one file, so encountering one is a reason to try the
#: next byte-identical copy rather than to abandon the source. ``OSError`` is
#: included because an unreadable or vanished file is precisely such a statement;
#: it is deliberately not swallowed, only attributed to the location it came from
#: and re-raised as the chained cause if no location works.
#:
#: ``ValueError`` is deliberately **excluded**, and the reason is worth writing
#: down because the obvious argument runs the other way. ``pyarrow``'s
#: ``ArrowInvalid`` *is* a ``ValueError``, so catching it here would look like
#: defence in depth — but so is Pydantic's ``ValidationError``, and swallowing
#: that would turn a domain-contract violation into "try the next copy", which is
#: exactly the silent-repair behaviour this package refuses everywhere else. The
#: correct place for that guarantee is the storage layer's own contract — *no
#: public function in ``parquet_store`` leaks a raw Arrow or Parquet error* — and
#: that contract is now enforced rather than assumed: its one unwrapped read was
#: removed and pinned by regression tests in ``tests/test_schema_v3.py``.
_UNUSABLE_COPY: tuple[type[Exception], ...] = (
    CatalogError,
    StorageError,
    SemanticEncodingError,
    ReplayArtifactVerificationError,
    OSError,
)

#: How bad each per-copy failure is, most severe first.
#:
#: Mirrors :func:`~...catalog.store.verify_manifest`, which reports "the
#: strongest outcome among the candidates — an edit is worse news than a
#: re-encode". Keeping the *first* failure instead would discard exactly the
#: distinction ADR-012 keeps the semantic and physical identities apart to
#: preserve: told that one copy is missing, an operator would never learn that a
#: surviving copy had been altered.
#:
#: Only failures actually reachable through :func:`_verify_and_load` are listed.
#: ``SemanticHashMismatchError`` is deliberately absent, though it is the worst
#: news the catalog can report: ``verify_artifact_against_manifest`` checks the
#: physical hash first, so reaching a semantic mismatch under a matching physical
#: hash would require a SHA-256 collision. Listing it would be a rank standing in
#: for a case that cannot occur.
_FAILURE_SEVERITY: tuple[type[Exception], ...] = (
    ReplayArtifactVerificationError,
    PhysicalArtifactHashMismatchError,
    SchemaMismatchError,
    SemanticEncodingError,
    StorageError,
    MissingArtifactError,
)


def _severity(error: Exception) -> int:
    """Return an ordering rank for a per-copy failure; lower is worse.

    An **unrecognised** failure ranks worst rather than best, which is the safe
    direction: a failure mode nobody anticipated should surface rather than hide
    behind a merely missing file.
    """
    for rank, kind in enumerate(_FAILURE_SEVERITY):
        if isinstance(error, kind):
            return rank
    return -1


def _load_verified_records(
    *, catalog_root: Path, storage_root: Path, manifest: DatasetManifest
) -> Sequence[ReplayPayload]:
    """Return one manifest's records, read from a copy that satisfies it.

    **Every** indexed location is tried, in the index's deterministic order, and
    the first that verifies wins. Taking only the first candidate was a real
    Phase 2.4 defect: the index is the one part of the catalog explicitly allowed
    to be wrong, so a single stale entry beside a perfectly good byte-identical
    copy turned an answerable question into a hard failure. Replay resolves the
    same way ``verify_manifest`` does.
    """
    identity = manifest.manifest_hash.canonical()
    candidates = locate_manifest(
        catalog_root=catalog_root, storage_root=storage_root, manifest=manifest
    )
    if not candidates:
        raise ReplayArtifactVerificationError(
            f"the catalog knows of no stored copy of dataset {identity}; replay verifies "
            f"every source before emitting any frame, so there is no partial run to fall "
            f"back to"
        )

    failures: list[tuple[Path, Exception]] = []
    for path in candidates:
        try:
            return _verify_and_load(path, manifest)
        except _UNUSABLE_COPY as error:
            failures.append((path, error))

    # Report the worst news, not the first. A missing copy beside an *edited* one
    # is a very different situation from two missing copies, and an operator told
    # only about the missing one would never look at the altered data.
    failures.sort(key=lambda failure: _severity(failure[1]))
    worst_path, worst_error = failures[0]
    raise ReplayArtifactVerificationError(
        f"no stored copy of dataset {identity} could be verified; "
        f"{len(candidates)} location(s) were tried. The most serious failure was at "
        f"{worst_path}: {type(worst_error).__name__}. All of them: "
        + "; ".join(f"{path.name}: {type(error).__name__}" for path, error in failures)
    ) from worst_error


def _verify_and_load(path: Path, manifest: DatasetManifest) -> Sequence[ReplayPayload]:
    """Verify one copy against a manifest and return the records it holds."""
    identity = manifest.manifest_hash.canonical()
    verify_artifact_against_manifest(path, manifest)

    dataset = read_records_v3(path)
    if dataset.record_type is not manifest.record_type:
        raise ReplayArtifactVerificationError(
            f"{path} holds {dataset.record_type.value} records, but dataset {identity} "
            f"claims {manifest.record_type.value}"
        )
    if dataset.contract != manifest.contract:
        raise ReplayArtifactVerificationError(
            f"{path} holds {dataset.contract.canonical()}, but dataset {identity} claims "
            f"{manifest.contract.canonical()}"
        )

    # Re-hash what was actually loaded. Verification read the file and this read
    # it again; without checking the second read, the rows now in memory would be
    # trusted on the strength of a check performed against a possibly different
    # state of the same path. The row-count claim is checked by the same call, so
    # a truncated read cannot produce a valid-looking digest.
    loaded = semantic_dataset_hash(
        record_type=manifest.record_type,
        contract=dataset.contract,
        records=dataset.records,
        expected_row_count=manifest.row_count,
    )
    if loaded != manifest.semantic_hash:
        raise ReplayArtifactVerificationError(
            f"{path} changed between verification and loading: the records read hash to "
            f"{loaded.canonical()}, but dataset {identity} describes "
            f"{manifest.semantic_hash.canonical()}"
        )

    records: Sequence[ReplayPayload] = dataset.records
    return records


def _reject_duplicate_semantics(sources: Sequence[PreparedSource]) -> None:
    """Refuse an input set in which two manifests pin one dataset's semantics."""
    for index, source in enumerate(sources):
        for other in sources[index + 1 :]:
            if source.semantic_hash == other.semantic_hash:
                raise DuplicateReplaySemanticDatasetError(
                    f"datasets {source.manifest_hash.canonical()} and "
                    f"{other.manifest_hash.canonical()} both pin semantic identity "
                    f"{source.semantic_hash.canonical()}: they decode to the same records in "
                    f"the same order, so replaying both would deliver every observation "
                    f"twice. Neither is deduplicated and neither is chosen; supply one"
                )


def _reject_ambiguous_overlap(sources: Sequence[PreparedSource]) -> None:
    """Refuse overlapping selected histories for one logical market stream."""
    for index, source in enumerate(sources):
        span = source.selected_span()
        if span is None:
            # An empty selection covers no time, so it overlaps nothing. A source
            # whose rows all fall outside the requested range is exactly as
            # unambiguous as one that was empty to begin with.
            continue
        for other in sources[index + 1 :]:
            if other.record_type is not source.record_type or other.contract != source.contract:
                continue
            other_span = other.selected_span()
            if other_span is None:
                continue
            if span[1] < other_span[0] or other_span[1] < span[0]:
                continue
            raise AmbiguousReplayOverlapError(
                f"datasets {source.manifest_hash.canonical()} and "
                f"{other.manifest_hash.canonical()} both supply "
                f"{source.record_type.value} observations for "
                f"{source.contract.canonical()} over overlapping selected time "
                f"({span[0].isoformat()}..{span[1].isoformat()} and "
                f"{other_span[0].isoformat()}..{other_span[1].isoformat()}). This is not a "
                f"claim that they disagree: replay will not interleave two unreconciled "
                f"histories of one stream into a single observed sequence. Reconcile them "
                f"into one artifact upstream, or select disjoint ranges"
            )
