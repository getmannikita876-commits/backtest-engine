"""Typed failures for dataset registration, verification, migration, lineage,
and cross-batch comparison.

Every *concrete* class here has a real trigger and a regression test. None is a
placeholder for a hypothetical future case: an error taxonomy invented ahead of
its callers is a set of promises nothing keeps. :class:`CatalogError` and
:class:`LineageError` are abstract bases and are never raised directly — they
exist so a caller can name a whole family in one ``except``.

All of them descend from :class:`CatalogError`, including the lineage family, so
a caller can catch the whole catalog surface with one name while still being able
to distinguish "this correction claim is invalid" from "this artifact is not what
its manifest says".
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


class LineageError(CatalogError):
    """Base class for every dataset-lineage error.

    A :class:`CatalogError` as well, so one ``except`` can cover the whole
    catalog surface while lineage failures stay separately catchable.
    """


class InvalidSupersedesRelationError(LineageError):
    """A supersedes claim violates an invariant that needs both manifests.

    Structural problems — no predecessors, duplicates, self-reference, a wrong
    hash — make the relation unconstructible in the domain model. This is the
    rest: the two manifests describe different contracts, different record
    types, or the very same dataset semantics, none of which a correction can
    be.
    """


class LineageCycleError(LineageError):
    """Publishing this relation would make the supersedes graph cyclic.

    A correction graph is a history, and a history cannot lead back to itself:
    a cycle would assert that some dataset transitively corrects one of its own
    predecessors.
    """


class MissingRelationError(LineageError):
    """No relation is published under the requested identity.

    Distinct from :class:`RelationHashMismatchError`, and the distinction is the
    difference between a normal negative lookup and lineage corruption. Merging
    them would leave a caller unable to tell "no such claim was ever made" from
    "the claim on disk is not what it says it is".
    """


class RelationHashMismatchError(LineageError):
    """A relation's stored hash does not match its own canonical content."""


class RelationConflictError(LineageError):
    """A different relation is already published under the same identity.

    Since the relation hash is a function of the relation's canonical bytes,
    this means either a SHA-256 collision or — far more likely — a serializer
    that is not deterministic.
    """


class LineageIndexError(LineageError):
    """The lineage index is present but unreadable.

    Distinct from an *absent* index, which is normal. The index is a discovery
    cache that no navigation answer depends on, so this is recoverable by
    rebuilding it — but it is reported rather than swallowed.
    """


class DatasetNotComparableError(CatalogError):
    """Two datasets are not directly cross-batch comparable.

    Raised when the datasets describe different listed contracts or different
    record types. Calling such a pair "conflicting" would be a false statement
    about unrelated data.
    """


class AmbiguousBarKeyError(CatalogError):
    """A bar dataset holds more than one row for one bar key.

    Key-based comparison is undefined over a multiset-keyed relation, and the
    two available shortcuts — keeping the first row or the last — are both
    guesses that would silently decide which claim about a period counts. The
    storage layer preserves duplicate rows by design and version-3 writes do not
    pass through the import validator that rejects them (ADR-005), so this is a
    reachable state, not a theoretical one.
    """
