"""Dataset manifests, provenance, and the artifact catalog.

Answers one question: *exactly which immutable canonical market-data dataset
artifact is this?* — and keeps three separate answers apart while doing it.

The layering below this package is strict. Identity value objects and the
semantic encoding live in ``domain``; physical byte hashing and Parquet I/O
live in ``data``; this package composes them into manifests, registration, and
a rebuildable location index. Nothing here is imported by either layer beneath
it, and nothing here reaches for replay, execution, strategy, or UI — none of
which exist.
"""

from quant_research_terminal.catalog.errors import (
    ArtifactOutsideStorageRootError,
    CatalogError,
    CatalogIndexError,
    CatalogLocationConflictError,
    ManifestConflictError,
    ManifestHashMismatchError,
    MissingArtifactError,
    MissingManifestError,
    PhysicalArtifactHashMismatchError,
    SchemaMismatchError,
    SemanticHashMismatchError,
    UnsupportedMigrationError,
)
from quant_research_terminal.catalog.index import (
    INDEX_FORMAT,
    CatalogEntry,
    CatalogIndex,
    empty_index,
    index_from_json_bytes,
)
from quant_research_terminal.catalog.manifest import (
    MANIFEST_FORMAT,
    DatasetManifest,
    build_manifest,
    compute_manifest_hash,
    manifest_from_json_bytes,
    manifest_to_json_bytes,
)
from quant_research_terminal.catalog.migration import (
    migrate_v2_to_v3,
    normalize_symbol_mapping,
)
from quant_research_terminal.catalog.provenance import (
    DatasetOrigin,
    SourceProvenance,
    TransformationKind,
    TransformationProvenance,
    untransformed,
)
from quant_research_terminal.catalog.store import (
    RegistrationOutcome,
    VerificationOutcome,
    find_by_semantic_hash,
    load_index,
    locate_manifest,
    manifest_path,
    published_manifests,
    read_manifest,
    rebuild_index,
    register_dataset,
    resolve_location,
    verify_artifact_against_manifest,
    verify_manifest,
    verify_registration,
    write_index,
)

__all__ = [
    "INDEX_FORMAT",
    "MANIFEST_FORMAT",
    "ArtifactOutsideStorageRootError",
    "CatalogEntry",
    "CatalogError",
    "CatalogIndex",
    "CatalogIndexError",
    "CatalogLocationConflictError",
    "DatasetManifest",
    "DatasetOrigin",
    "ManifestConflictError",
    "ManifestHashMismatchError",
    "MissingArtifactError",
    "MissingManifestError",
    "PhysicalArtifactHashMismatchError",
    "RegistrationOutcome",
    "SchemaMismatchError",
    "SemanticHashMismatchError",
    "SourceProvenance",
    "TransformationKind",
    "TransformationProvenance",
    "UnsupportedMigrationError",
    "VerificationOutcome",
    "build_manifest",
    "compute_manifest_hash",
    "empty_index",
    "find_by_semantic_hash",
    "index_from_json_bytes",
    "load_index",
    "locate_manifest",
    "manifest_from_json_bytes",
    "manifest_path",
    "manifest_to_json_bytes",
    "migrate_v2_to_v3",
    "normalize_symbol_mapping",
    "published_manifests",
    "read_manifest",
    "rebuild_index",
    "register_dataset",
    "resolve_location",
    "untransformed",
    "verify_artifact_against_manifest",
    "verify_manifest",
    "verify_registration",
    "write_index",
]
