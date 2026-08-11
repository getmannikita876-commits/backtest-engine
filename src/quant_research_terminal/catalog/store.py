"""Registering, verifying, and rediscovering dataset artifacts.

Registration verifies; it never trusts. Every claim a manifest makes about an
artifact — schema version, record type, contract, row count, time bounds,
semantic hash, physical hash, vendor aliases — is recomputed from the artifact
before the manifest is published. A caller-supplied manifest that disagrees is
rejected, and there is no partial registration.

The commit point
----------------
Publishing the final manifest is the moment a dataset becomes registered:

1. the artifact already exists, written by the storage layer;
2. its physical hash is computed from the bytes on disk;
3. its records are read back and its semantic hash recomputed;
4. every derived field is recomputed and compared;
5. the manifest is written to ``<hash>.json.partial`` and renamed into place.

Step 5 is the commit. Updating the location index happens strictly afterwards
and is recoverable, because the index is derived state.

Crash windows, stated honestly:

* before step 5 — the artifact may be orphaned; no dataset is registered, and
  nothing is corrupt;
* after step 5, before the index write — the dataset **is** registered and
  :func:`rebuild_index` recovers the location.

Durability is not claimed
-------------------------
``os.replace`` makes a rename atomic with respect to *visibility*: a reader
never sees a half-written file at the target path. It does not make the write
durable. Neither this module nor the storage layer calls ``fsync`` on the file
or on the containing directory, so a power loss can leave a renamed file whose
data blocks never reached the disk. Directory ``fsync`` — the POSIX mechanism
that would close this — has no Windows equivalent, so a portable durability
guarantee is not available and is therefore not offered. The recovery story is
detection, not prevention: that is what the physical hash is for.

Concurrency, stated at the same standard
----------------------------------------
Manifest publication is safe under concurrency: the catalog creates its own
temporary files with ``O_EXCL``, so two processes racing to publish produce a
loud ``FileExistsError`` rather than interleaving into one file. The storage
layer's deterministic ``.partial`` name carries no such guard, so a single
writer per artifact path is assumed there.

The **index is not** safe under concurrency, and pretending otherwise would be
worse than the limitation. :func:`register_dataset` reads the index, adds an
entry, and writes it back; two registrations interleaving between the read and
the write leave the second one's entry missing from the index. No lock is taken,
because a correct cross-platform file lock is a design decision this phase does
not need to make: the manifest is still published, so the dataset *is*
registered, and :func:`rebuild_index` recovers the location whenever the
artifact's physical hash identifies its manifest. A single registering process
per catalog root is therefore assumed, and that assumption is written down
rather than relied on quietly.
"""

from __future__ import annotations

import os
from collections.abc import Iterable
from enum import StrEnum, unique
from pathlib import Path
from typing import Final

from quant_research_terminal.catalog.errors import (
    ArtifactOutsideStorageRootError,
    ManifestConflictError,
    ManifestHashMismatchError,
    MissingArtifactError,
    MissingManifestError,
    PhysicalArtifactHashMismatchError,
    SchemaMismatchError,
    SemanticHashMismatchError,
)
from quant_research_terminal.catalog.index import (
    CatalogEntry,
    CatalogIndex,
    empty_index,
    index_from_json_bytes,
)
from quant_research_terminal.catalog.manifest import (
    DatasetManifest,
    manifest_from_json_bytes,
    manifest_to_json_bytes,
)
from quant_research_terminal.data.artifact_hash import physical_artifact_hash
from quant_research_terminal.data.contracts import SCHEMA_VERSION
from quant_research_terminal.data.parquet_store import (
    StorageError,
    read_records_v3,
)
from quant_research_terminal.domain.dataset_identity import (
    ManifestHash,
    SemanticDatasetHash,
    SemanticEncodingError,
    dataset_time_bounds,
    semantic_dataset_hash,
)

#: Directory under the catalog root holding published manifests.
MANIFEST_DIRECTORY: Final[str] = "manifests"

#: The rebuildable location index.
INDEX_FILENAME: Final[str] = "index.json"

#: Suffix for the catalog's own temporary files. Distinct from the storage
#: layer's ``.partial`` so the two can never be mistaken for one another.
CATALOG_PARTIAL_SUFFIX: Final[str] = ".catalog-partial"


@unique
class RegistrationOutcome(StrEnum):
    """What a registration attempt did."""

    REGISTERED = "registered"
    ALREADY_REGISTERED = "already-registered"


@unique
class VerificationOutcome(StrEnum):
    """What verifying a registered artifact found.

    ``PHYSICAL_MISMATCH`` and ``SEMANTIC_MISMATCH`` are deliberately separate.
    The first means the bytes changed while the data did not — a re-encode, a
    different PyArrow build — and is usually benign. The second means **the
    data changed**. Reporting both as one outcome would waste the entire point
    of keeping the two identities apart.
    """

    OK = "ok"
    PHYSICAL_MISMATCH = "physical-mismatch"
    SEMANTIC_MISMATCH = "semantic-mismatch"
    MISSING_ARTIFACT = "missing-artifact"
    MISSING_MANIFEST = "missing-manifest"


def publish_bytes_atomically(path: Path, payload: bytes) -> bool:
    """Publish immutable bytes, refusing to change anything already there.

    Returns ``True`` if this call created the file and ``False`` if identical
    content was already published — which makes republication idempotent
    without rewriting a file another process may have open.

    Raises:
        ManifestConflictError: if different content already exists at ``path``.
        FileExistsError: if another process is mid-publication for the same
            target. The temporary file is created with ``O_EXCL`` precisely so
            that this is loud rather than a silent interleave.
    """
    if path.exists():
        if path.read_bytes() == payload:
            return False
        raise ManifestConflictError(
            f"{path} already holds different content; a published manifest is immutable "
            f"and is never overwritten. Identical identity with differing bytes means the "
            f"serialization is not deterministic"
        )

    path.parent.mkdir(parents=True, exist_ok=True)
    partial = path.with_name(path.name + CATALOG_PARTIAL_SUFFIX)
    descriptor = os.open(partial, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
        os.replace(partial, path)
    except BaseException:
        partial.unlink(missing_ok=True)
        raise
    return True


def manifest_path(catalog_root: Path, manifest_hash: ManifestHash) -> Path:
    """Return where a manifest is published.

    Named by content, so the filename is discovery convenience rather than
    identity: no timestamp, no random token, no human label decides it.
    """
    return catalog_root / MANIFEST_DIRECTORY / f"{manifest_hash.canonical()}.json"


def verify_artifact_against_manifest(artifact_path: Path, manifest: DatasetManifest) -> None:
    """Recompute every claim in ``manifest`` from the artifact, or raise.

    Nothing is taken on trust. The order runs cheap metadata checks before the
    full row scan, so a wrong-contract artifact fails without being read
    end to end.
    """
    if not artifact_path.exists():
        raise MissingArtifactError(f"no artifact at {artifact_path}")

    physical = physical_artifact_hash(artifact_path)
    if physical != manifest.physical_hash:
        raise PhysicalArtifactHashMismatchError(
            f"{artifact_path} has physical hash {physical.canonical()}, but the manifest "
            f"claims {manifest.physical_hash.canonical()}"
        )

    try:
        dataset = read_records_v3(artifact_path)
    except StorageError as error:
        raise SchemaMismatchError(f"{artifact_path} is not a readable v3 artifact: {error}") from (
            error
        )

    if dataset.record_type is not manifest.record_type:
        # Checked before the contract, and checked at all, because an *empty*
        # artifact has no rows to contradict a wrong record type: the declared
        # type is then the only evidence in the file, so ignoring it made a
        # manifest claiming "quote" verify against a trade artifact.
        raise SchemaMismatchError(
            f"{artifact_path} holds {dataset.record_type.value} records, but the manifest "
            f"claims {manifest.record_type.value}"
        )
    if dataset.contract != manifest.contract:
        raise SchemaMismatchError(
            f"{artifact_path} holds {dataset.contract.canonical()}, but the manifest claims "
            f"{manifest.contract.canonical()}"
        )
    if manifest.schema_version != SCHEMA_VERSION:
        raise SchemaMismatchError(
            f"the manifest claims schema version {manifest.schema_version}; registered "
            f"artifacts are version {SCHEMA_VERSION}"
        )
    if len(dataset.records) != manifest.row_count:
        raise SchemaMismatchError(
            f"{artifact_path} holds {len(dataset.records)} rows, but the manifest claims "
            f"{manifest.row_count}"
        )

    earliest, latest = dataset_time_bounds(dataset.records)
    if earliest != manifest.min_timestamp or latest != manifest.max_timestamp:
        raise SchemaMismatchError(
            f"{artifact_path} spans {earliest} to {latest}, but the manifest claims "
            f"{manifest.min_timestamp} to {manifest.max_timestamp}"
        )

    observed_aliases = tuple(sorted({record.instrument_symbol for record in dataset.records}))
    if observed_aliases != manifest.source.vendor_symbols:
        raise SchemaMismatchError(
            f"{artifact_path} carries vendor aliases {list(observed_aliases)}, but the "
            f"manifest claims {list(manifest.source.vendor_symbols)}"
        )

    semantic = semantic_dataset_hash(
        record_type=manifest.record_type,
        contract=dataset.contract,
        records=dataset.records,
        expected_row_count=manifest.row_count,
    )
    if semantic != manifest.semantic_hash:
        raise SemanticHashMismatchError(
            f"{artifact_path} has semantic hash {semantic.canonical()}, but the manifest "
            f"claims {manifest.semantic_hash.canonical()}. The data is not what the "
            f"manifest describes"
        )


def register_dataset(
    *,
    catalog_root: Path,
    storage_root: Path,
    artifact_path: Path,
    manifest: DatasetManifest,
) -> RegistrationOutcome:
    """Verify an artifact against its manifest, publish it, then index it.

    Everything that can fail is made to fail **before** the commit point. The
    index is read and the new index computed first, so a corrupt index or a
    location conflict raises while nothing has been published — rather than
    after, which would report a failure for a dataset that is in fact
    registered.
    """
    verify_artifact_against_manifest(artifact_path, manifest)

    location = _relative_location(storage_root, artifact_path)
    entry = CatalogEntry(physical_hash=manifest.physical_hash, location=location)
    updated = load_index(catalog_root).with_entry(entry)

    target = manifest_path(catalog_root, manifest.manifest_hash)
    created = publish_bytes_atomically(target, manifest_to_json_bytes(manifest))

    write_index(catalog_root, updated)
    return RegistrationOutcome.REGISTERED if created else RegistrationOutcome.ALREADY_REGISTERED


def _relative_location(storage_root: Path, artifact_path: Path) -> str:
    """Return the storage-root-relative POSIX location of an artifact.

    Absolute machine paths never become portable state; only the part below the
    configured root is recorded, and it is recorded with forward slashes so a
    Windows-written index is readable on Linux.
    """
    try:
        relative = artifact_path.resolve().relative_to(storage_root.resolve())
    except ValueError as error:
        raise ArtifactOutsideStorageRootError(
            f"{artifact_path} is not inside the configured storage root {storage_root}; "
            f"an absolute path must never become catalog state"
        ) from error
    return relative.as_posix()


def resolve_location(storage_root: Path, location: str) -> Path:
    """Return the absolute path a stored location currently denotes."""
    return storage_root / location


def index_path(catalog_root: Path) -> Path:
    """Return the location index's path."""
    return catalog_root / INDEX_FILENAME


def load_index(catalog_root: Path) -> CatalogIndex:
    """Return the stored index, or an empty one if none has been written."""
    path = index_path(catalog_root)
    if not path.exists():
        return empty_index()
    return index_from_json_bytes(path.read_bytes())


def write_index(catalog_root: Path, index: CatalogIndex) -> None:
    """Replace the location index.

    Unlike a manifest the index is mutable derived state, so it is written with
    a plain atomic replace rather than the immutable publication protocol.
    """
    catalog_root.mkdir(parents=True, exist_ok=True)
    path = index_path(catalog_root)
    partial = path.with_name(path.name + CATALOG_PARTIAL_SUFFIX)
    partial.write_bytes(index.to_json_bytes())
    os.replace(partial, path)


def published_manifests(catalog_root: Path) -> tuple[DatasetManifest, ...]:
    """Return every published manifest, in deterministic order.

    Walks only the configured manifest directory, never the whole filesystem.
    Unpublished ``<hash>.json.catalog-partial`` files are excluded by the
    ``*.json`` pattern itself — a partial is not a registered dataset — so no
    separate suffix test is written here to imply a check that would never run.
    A committed manifest that fails to parse is **not** skipped: identity
    corruption must be loud.
    """
    directory = catalog_root / MANIFEST_DIRECTORY
    if not directory.exists():
        return ()
    return tuple(
        manifest_from_json_bytes(path.read_bytes()) for path in sorted(directory.glob("*.json"))
    )


def read_manifest(catalog_root: Path, manifest_hash: ManifestHash) -> DatasetManifest:
    """Return one published manifest.

    Verifiable without any index: identity lives in the manifest and the
    artifact, never in the location map.
    """
    path = manifest_path(catalog_root, manifest_hash)
    if not path.exists():
        raise MissingManifestError(f"no manifest published under {manifest_hash.canonical()}")
    manifest = manifest_from_json_bytes(path.read_bytes())
    if manifest.manifest_hash != manifest_hash:
        raise ManifestHashMismatchError(
            f"{path} is published under {manifest_hash.canonical()} but describes "
            f"{manifest.manifest_hash.canonical()}"
        )
    return manifest


def find_by_semantic_hash(
    catalog_root: Path, semantic_hash: SemanticDatasetHash
) -> tuple[DatasetManifest, ...]:
    """Return every published manifest describing one semantic dataset identity.

    Ordered by manifest hash, and read from the **published manifests** rather
    than from the index. One dataset legitimately has many artifacts, so this
    can never be a single answer — and sourcing it from immutable state means it
    keeps working with the index deleted, which is what "the index is not
    authority" has to mean if it means anything.
    """
    return tuple(
        sorted(
            (
                manifest
                for manifest in published_manifests(catalog_root)
                if manifest.semantic_hash == semantic_hash
            ),
            key=lambda manifest: manifest.manifest_hash.canonical(),
        )
    )


def locate_manifest(
    *, catalog_root: Path, storage_root: Path, manifest: DatasetManifest
) -> tuple[Path, ...]:
    """Return every current path whose bytes satisfy ``manifest``.

    Resolution runs in two hops, each answered by the layer that owns it: the
    manifest supplies its own ``physical_hash`` immutably, and the index supplies
    the paths currently holding those bytes. A manifest is therefore resolvable
    through *any* surviving copy, and several manifests pinning one physical
    hash are all resolvable through the same copy.
    """
    locations = load_index(catalog_root).find_locations(manifest.physical_hash)
    return tuple(resolve_location(storage_root, location) for location in locations)


def rebuild_index(*, catalog_root: Path, storage_root: Path) -> CatalogIndex:
    """Rediscover where registered bytes currently live.

    Every candidate artifact under the storage root is hashed and recorded if
    some published manifest declares that physical hash. That is the whole
    algorithm, and it is total: there is no case it must refuse.

    Why there is nothing ambiguous to resolve
    -----------------------------------------
    An earlier version of this function tried to recover a
    ``ManifestHash -> ArtifactLocation`` binding, and had to raise when two
    manifests shared one physical hash because hashing could not say which
    manifest "belonged" at which path. That binding was never needed. A manifest
    already carries its own ``physical_hash``, so the only thing the index has to
    supply is where those bytes are — and both manifests resolve through the same
    bytes, from any single surviving copy.

    Nothing historical is reconstructed and nothing is invented: an entry states
    only the verified, present-tense fact that these bytes are at this path.
    Location was defined as mutable discovery state rather than provenance, so
    there is no past pairing to recover in the first place.

    Two identical copies of one artifact produce two entries, which is correct:
    one physical artifact in two places, and either satisfies any manifest that
    pins it.
    """
    declared = {manifest.physical_hash for manifest in published_manifests(catalog_root)}
    entries: list[CatalogEntry] = []
    for path in _candidate_artifacts(storage_root):
        digest = physical_artifact_hash(path)
        if digest not in declared:
            continue
        entries.append(
            CatalogEntry(
                physical_hash=digest,
                location=_relative_location(storage_root, path),
            )
        )
    rebuilt = CatalogIndex(
        index_format=empty_index().index_format,
        entries=tuple(sorted(entries, key=lambda entry: entry.sort_key())),
    )
    write_index(catalog_root, rebuilt)
    return rebuilt


def _candidate_artifacts(storage_root: Path) -> Iterable[Path]:
    """Yield artifacts under the storage root, deterministically ordered.

    The glob is the filter. The storage layer's temporaries are named
    ``<name>.parquet.partial`` and the application layer's are
    ``<name>.parquet.importing``, so ``*.parquet`` already excludes both — an
    additional suffix test here would be unreachable code standing in for a
    guarantee the pattern already gives. Neither is a published artifact, and
    neither is deleted: removing one could race a live writer.
    """
    yield from sorted(storage_root.rglob("*.parquet"))


def verify_registration(
    *, catalog_root: Path, storage_root: Path, manifest_hash: ManifestHash, location: str
) -> VerificationOutcome:
    """Re-verify one published manifest against the file at one location.

    Both are named by the caller, because they answer different questions and
    the catalog no longer pretends a manifest is bound to a path: ``location``
    says *which bytes to look at* and ``manifest_hash`` says *what to check them
    against*.

    Distinguishes a re-encode from an edit, which is the whole reason the
    physical and semantic identities are kept apart. Reporting the difference
    needs a specific file to inspect, which is why this takes a location while
    :func:`verify_manifest` does not.
    """
    try:
        manifest = read_manifest(catalog_root, manifest_hash)
    except MissingManifestError:
        return VerificationOutcome.MISSING_MANIFEST

    artifact = resolve_location(storage_root, location)
    if not artifact.exists():
        return VerificationOutcome.MISSING_ARTIFACT

    if physical_artifact_hash(artifact) != manifest.physical_hash:
        # The bytes moved. Ask the stronger question before reporting: a
        # re-encode is benign, a changed record is not.
        try:
            dataset = read_records_v3(artifact)
            if dataset.record_type is not manifest.record_type:
                # A different record type is a changed dataset, and asking the
                # hasher to encode it under the manifest's type would raise
                # rather than answer.
                return VerificationOutcome.SEMANTIC_MISMATCH
            semantic = semantic_dataset_hash(
                record_type=manifest.record_type,
                contract=dataset.contract,
                records=dataset.records,
            )
        except (StorageError, ValueError, SemanticEncodingError):
            # ``SemanticEncodingError`` is named explicitly: it descends from
            # ``Exception``, not ``ValueError``, and a function whose whole
            # contract is to return a ``VerificationOutcome`` must not raise.
            return VerificationOutcome.SEMANTIC_MISMATCH
        if semantic != manifest.semantic_hash:
            return VerificationOutcome.SEMANTIC_MISMATCH
        return VerificationOutcome.PHYSICAL_MISMATCH

    return VerificationOutcome.OK


def verify_manifest(
    *, catalog_root: Path, storage_root: Path, manifest_hash: ManifestHash
) -> VerificationOutcome:
    """Verify a published manifest against whatever copy the catalog can find.

    ``OK`` as soon as **any** currently indexed location satisfies it: a manifest
    describes bytes, and byte-identical copies satisfy it equally well, so
    several manifests pinning one physical hash are all verifiable through the
    same surviving copy.

    When no location satisfies it, the strongest outcome among the candidates is
    reported — an edit is worse news than a re-encode — and ``MISSING_ARTIFACT``
    when the catalog knows of no copy at all.
    """
    try:
        manifest = read_manifest(catalog_root, manifest_hash)
    except MissingManifestError:
        return VerificationOutcome.MISSING_MANIFEST

    locations = load_index(catalog_root).find_locations(manifest.physical_hash)
    outcomes = [
        verify_registration(
            catalog_root=catalog_root,
            storage_root=storage_root,
            manifest_hash=manifest_hash,
            location=location,
        )
        for location in locations
    ]
    if VerificationOutcome.OK in outcomes:
        return VerificationOutcome.OK
    for severe in (
        VerificationOutcome.SEMANTIC_MISMATCH,
        VerificationOutcome.PHYSICAL_MISMATCH,
    ):
        if severe in outcomes:
            return severe
    return VerificationOutcome.MISSING_ARTIFACT
