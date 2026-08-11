"""Typed failures for dataset registration, verification, and migration.

Every class here has a concrete trigger and a regression test. None is a
placeholder for a hypothetical future case: an error taxonomy invented ahead of
its callers is a set of promises nothing keeps.
"""

from __future__ import annotations


class CatalogError(Exception):
    """Base class for every dataset-catalog error."""


class SchemaMismatchError(CatalogError):
    """An artifact's schema is not the one the operation requires."""


class MissingArtifactError(CatalogError):
    """A registered manifest refers to an artifact that is not present."""


class MissingManifestError(CatalogError):
    """No published manifest exists for the requested identity."""


class SemanticHashMismatchError(CatalogError):
    """The artifact's recomputed semantic hash contradicts a claim about it.

    The serious one: the *data* is not what the manifest says it is.
    """


class PhysicalArtifactHashMismatchError(CatalogError):
    """The artifact's bytes contradict a claim about them.

    Benign on its own — a re-encode changes bytes without changing data — so it
    is deliberately distinguishable from a semantic mismatch.
    """


class ManifestHashMismatchError(CatalogError):
    """A manifest's stored hash does not match its own canonical content."""


class ManifestConflictError(CatalogError):
    """A different manifest is already published under the same identity.

    Since the manifest hash is a function of the manifest's canonical bytes,
    this means either a SHA-256 collision or — far more likely — a serializer
    that is not deterministic.
    """


class ArtifactOutsideStorageRootError(CatalogError):
    """An artifact does not lie under the configured storage root.

    Refused because the only alternative is recording an absolute machine path,
    and an absolute path must never become catalog state: it is meaningless on
    the next machine and would not survive the drive letter changing.
    """


class CatalogLocationConflictError(CatalogError):
    """A location is already recorded as holding different bytes.

    Refused rather than silently rebound: the storage writer overwrites by
    design, so accepting this would let the catalog quietly forget that the
    previous bytes ever existed at that path.

    Note what this is *not*: two manifests describing the same bytes at the same
    path is the ordinary one-to-many case and is accepted. The index records
    what the file holds, not the claims made about it.
    """


class CatalogIndexError(CatalogError):
    """The location index is present but unreadable.

    Distinct from an *absent* index, which is normal and yields an empty one.
    A corrupt index is a typed catalog failure rather than a bare ``ValueError``
    escaping from every entry point, because a caller that wants to survive
    losing derived state has to be able to name what it is surviving.
    """


class UnsupportedMigrationError(CatalogError):
    """A version-2 artifact cannot be migrated with the mapping supplied.

    Raised rather than guessing. There is no fallback that parses a vendor
    symbol into a canonical identity: inferring a venue, a decade, or a
    delivery year from an alias is exactly the inference this phase exists to
    make impossible.
    """
