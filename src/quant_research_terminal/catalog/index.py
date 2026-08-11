"""Where bytes are currently found — derived, rebuildable, non-authoritative.

The index answers exactly one question:

    given a :class:`PhysicalArtifactHash`, which paths currently hold those
    bytes?

Nothing else. It is the one part of the catalog allowed to be wrong, because it
is the one part describing something mutable: a file can be moved, and moving it
changes nothing about what the file *is*.

Why it does not index manifests
-------------------------------
A manifest already carries its own ``physical_hash``, immutably and under its
own hash. So ``ManifestHash -> PhysicalArtifactHash`` is not index state at all —
it is a published fact. Storing it here would duplicate immutable content into
mutable state, and the duplicate is the half that can rot.

Resolution therefore runs in two hops, each answered by the layer that owns it:

    ManifestHash --(published manifest)--> PhysicalArtifactHash
    PhysicalArtifactHash --(this index)--> current locations

An earlier design bound ``ManifestHash -> ArtifactLocation`` directly, and that
binding is what made byte-identical artifacts under different provenance look
unrecoverable after an index loss: two manifests sharing one physical hash gave
rediscovery no way to tell which manifest "belonged" at which path. The question
was unanswerable because it was the wrong question. Location was defined as
mutable discovery state, never provenance, so there is no historical pairing to
recover — only the current, verifiable fact of which bytes sit where. Both
manifests resolve through the bytes, and both are recoverable from any single
surviving copy.

One physical identity, many locations
-------------------------------------
Byte-identical copies at several paths are one physical artifact in several
places, so lookup returns a **tuple** of locations. Conversely one location holds
one byte content at a time, which is a fact about files rather than a policy, so
locations are unique here. Rebinding a location to *different* bytes is refused:
the storage writer overwrites by design, and accepting it silently would let the
catalog forget the previous bytes ever existed there.
"""

from __future__ import annotations

import json
import re
from typing import Any, Final, final

from pydantic import field_validator, model_validator

from quant_research_terminal.catalog.errors import (
    CatalogIndexError,
    CatalogLocationConflictError,
)
from quant_research_terminal.domain.common import CanonicalModel
from quant_research_terminal.domain.dataset_identity import PhysicalArtifactHash

#: Bumping this re-keys the on-disk index format.
INDEX_FORMAT: Final[str] = "qrt-catalog-index/2"

#: A location is a relative POSIX path under the configured storage root.
#:
#: Absolute paths, drive letters, backslashes, and parent traversal are all
#: rejected, because an index that stored ``C:\research\a.parquet`` would be
#: unusable on the next machine and would smuggle a machine-specific string
#: into what is meant to be portable state.
_LOCATION_PATTERN: Final[re.Pattern[str]] = re.compile(r"[^/\\:][^\\:]*")


@final
class CatalogEntry(CanonicalModel):
    """One path, and the physical identity of the bytes currently at it.

    Deliberately not a manifest binding. Two fields, both about the file: what
    it holds and where it is.
    """

    physical_hash: PhysicalArtifactHash
    location: str

    @field_validator("location")
    @classmethod
    def _validate_location(cls, value: str) -> str:
        if not value:
            raise ValueError("artifact location must not be empty")
        if _LOCATION_PATTERN.fullmatch(value) is None:
            raise ValueError(
                f"artifact location must be a relative POSIX path with no drive letter and "
                f"no backslash; got {value!r}"
            )
        if value.startswith("/") or ".." in value.split("/"):
            raise ValueError(
                f"artifact location must stay under the storage root, with no leading "
                f"separator and no parent traversal; got {value!r}"
            )
        return value

    def sort_key(self) -> tuple[str, str]:
        """Return the deterministic ordering key for this entry."""
        return (self.location, self.physical_hash.canonical())


@final
class CatalogIndex(CanonicalModel):
    """The full set of known artifact locations, deterministically ordered."""

    index_format: str
    entries: tuple[CatalogEntry, ...]

    @model_validator(mode="after")
    def _validate_index(self) -> CatalogIndex:
        if self.index_format != INDEX_FORMAT:
            raise ValueError(f"index format must be {INDEX_FORMAT!r}; got {self.index_format!r}")
        keys = [entry.sort_key() for entry in self.entries]
        if keys != sorted(keys):
            raise ValueError("catalog entries must be in deterministic sorted order")
        locations = [entry.location for entry in self.entries]
        if len(set(locations)) != len(locations):
            raise ValueError("one location cannot hold two different byte contents")
        return self

    def find_locations(self, value: PhysicalArtifactHash) -> tuple[str, ...]:
        """Return every location currently holding these bytes, in order.

        A tuple, never one location: byte-identical copies are one physical
        artifact in several places, and any of them satisfies any manifest that
        pins this hash.
        """
        return tuple(entry.location for entry in self.entries if entry.physical_hash == value)

    def find_by_location(self, location: str) -> CatalogEntry | None:
        """Return the entry at a location, if any. Locations are unique."""
        for entry in self.entries:
            if entry.location == location:
                return entry
        return None

    def with_entry(self, entry: CatalogEntry) -> CatalogIndex:
        """Return a new index including ``entry``.

        Recording the identical entry is idempotent. A location already holding
        *different* bytes is refused rather than rebound: the storage writer
        overwrites by design, so accepting it would let the catalog quietly
        forget the previous bytes ever existed there.

        Note what is **not** a conflict: two manifests describing the same bytes
        at the same path. That is the ordinary one-to-many case — two providers'
        provenance for one artifact — and it produces one entry, because the
        index describes the file rather than the claims about it.
        """
        existing = self.find_by_location(entry.location)
        if existing is not None:
            if existing == entry:
                return self
            raise CatalogLocationConflictError(
                f"{entry.location!r} already holds bytes "
                f"{existing.physical_hash.canonical()}; refusing to rebind it to "
                f"{entry.physical_hash.canonical()}"
            )
        merged = sorted((*self.entries, entry), key=lambda item: item.sort_key())
        return CatalogIndex(index_format=INDEX_FORMAT, entries=tuple(merged))

    def without_location(self, location: str) -> CatalogIndex:
        """Return a new index with any entry at ``location`` removed."""
        return CatalogIndex(
            index_format=INDEX_FORMAT,
            entries=tuple(entry for entry in self.entries if entry.location != location),
        )

    def to_json_bytes(self) -> bytes:
        """Serialize deterministically, so a rebuild can be compared byte-wise."""
        document: dict[str, Any] = {
            "format": self.index_format,
            "entries": [
                {
                    "physical_hash": entry.physical_hash.canonical(),
                    "location": entry.location,
                }
                for entry in self.entries
            ],
        }
        return json.dumps(
            document, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False
        ).encode("ascii")


def empty_index() -> CatalogIndex:
    """Return an index with no entries."""
    return CatalogIndex(index_format=INDEX_FORMAT, entries=())


def index_from_json_bytes(payload: bytes) -> CatalogIndex:
    """Parse a serialized index.

    Raises:
        CatalogIndexError: if the document is unreadable. A typed catalog error
            rather than a bare ``ValueError``, because losing derived state must
            be something a caller can catch by name.
    """
    try:
        document = json.loads(payload.decode("ascii"))
        return CatalogIndex(
            index_format=str(document["format"]),
            entries=tuple(
                CatalogEntry(
                    physical_hash=PhysicalArtifactHash(value=item["physical_hash"]),
                    location=item["location"],
                )
                for item in document["entries"]
            ),
        )
    except (UnicodeDecodeError, json.JSONDecodeError, KeyError, TypeError, ValueError) as error:
        raise CatalogIndexError(f"catalog index is unreadable: {error}") from error
