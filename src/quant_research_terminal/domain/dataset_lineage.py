"""The immutable claim that one dataset corrects another.

Phase 2.3 made datasets and their manifests immutable. Corrections are real, so
something has to represent "manifest B was declared a corrected successor of
manifest A" — and it must do so **without mutating A**, without redirecting
anyone who pinned A, and without inventing knowledge the stored records cannot
support.

A separate artifact, not a manifest field
-----------------------------------------
:class:`~quant_research_terminal.catalog.manifest.DatasetManifest` gains no
``parent`` or ``supersedes`` field, for two reasons that are both fatal on their
own. A lineage claim is routinely made *after* both manifests already exist, and
a manifest is immutable — so there is no moment at which the field could be
filled. And a manifest hash covers every field, so adding one would change the
identity of a dataset because something was later said *about* it.

What the relation means, exactly
--------------------------------
    ``successor`` was **explicitly declared** by an operator to be a
    correction/replacement successor of every manifest in ``predecessors``.

That is a historical lineage claim and nothing more. It does **not** mean the
successor is preferred, latest, better, or automatically selected; it does not
invalidate a predecessor; and it never redirects a lookup. A caller that pinned
a predecessor's :class:`ManifestHash` still gets exactly that manifest,
forever — which is the entire point of recording lineage beside the data rather
than inside it.

Deliberately absent from this module
------------------------------------
No revision number, because a branching DAG has no sequence to number. No
"latest", because a DAG with two independent corrections of one predecessor has
no natural answer and a linear chain must not be allowed to imply one. No
publication timestamp: a relation records that the claim exists *now*, and
using the moment a JSON file was written as a proxy for when a correction became
historically available would manufacture point-in-time semantics the repository
cannot support.
"""

from __future__ import annotations

import hashlib
import json
from enum import StrEnum, unique
from typing import Any, Final, final

from pydantic import model_validator

from quant_research_terminal.domain.common import CanonicalModel
from quant_research_terminal.domain.dataset_identity import (
    HexDigest,
    ManifestHash,
    require_digest,
)

#: Bumping this re-keys every relation, which is exactly what a change to the
#: canonical serialization must do.
SUPERSEDES_FORMAT: Final[str] = "qrt-dataset-supersedes/1"


@unique
class RelationProvenanceKind(StrEnum):
    """How a supersedes claim came to be made.

    One member, deliberately. An operator declaring the relation is the only
    thing this repository can currently support: no provider adapter exposes a
    verified correction declaration, and both vendor decoders are themselves
    unverified against real output (ADR-003). Adding a ``SOURCE_DECLARED``
    member now would put a value in the hashed payload that nothing can ever
    produce — a promise with no mechanism behind it.

    It is an enum rather than a bare constant because the payload needs a closed
    vocabulary with a stable serialized token, matching how ``DatasetOrigin``
    and ``TransformationKind`` are modelled one layer up.
    """

    OPERATOR_DECLARED = "operator-declared"


def require_relation_provenance_kind(value: object) -> RelationProvenanceKind:
    """Return ``value`` if it is exactly a :class:`RelationProvenanceKind`.

    An exact type check for the ``StrEnum`` reason established in
    :func:`~quant_research_terminal.domain.dataset_identity.require_record_type`:
    members of different ``StrEnum`` classes with equal values compare *and hash*
    equal, so an equal-valued token from elsewhere must not be able to select a
    provenance.
    """
    if type(value) is not RelationProvenanceKind:
        raise TypeError(
            f"a RelationProvenanceKind is required, exactly; got {type(value).__name__}: {value!r}"
        )
    return value


@final
class SupersedesRelationHash(HexDigest):
    """Identity of one supersedes claim's canonical content."""


@final
class RelationProvenance(CanonicalModel):
    """How a supersedes claim was made.

    Structured rather than free text. A prose note would raise exactly one
    question about an immutable, content-addressed artifact — does editing it
    change the identity? — and the cheapest correct answer is for there to be no
    prose to edit.
    """

    kind: RelationProvenanceKind

    @model_validator(mode="after")
    def _validate_provenance(self) -> RelationProvenance:
        require_relation_provenance_kind(self.kind)
        return self

    def canonical_payload(self) -> dict[str, Any]:
        """Return the deterministic payload this provenance contributes."""
        return {"kind": self.kind.value}


def operator_declared() -> RelationProvenance:
    """Return the provenance for a relation an operator declared."""
    return RelationProvenance(kind=RelationProvenanceKind.OPERATOR_DECLARED)


def supersedes_canonical_payload(
    *,
    successor: ManifestHash,
    predecessors: tuple[ManifestHash, ...],
    provenance: RelationProvenance,
) -> dict[str, Any]:
    """Return the deterministic payload a relation hash is computed over.

    Predecessors are serialized **sorted by digest**, so the caller's argument
    order cannot reach the hash. They are semantically a set; a hash that
    depended on iteration order would give one claim two identities.

    Excluded, by construction rather than by filtering: any path, the catalog
    root, the wall clock, any random or process-derived value, and any record of
    when or in what order relations were published. A relation is a claim about
    two identities and must hash to the same value on every machine, forever.
    """
    return {
        "format": SUPERSEDES_FORMAT,
        "successor_manifest_hash": successor.canonical(),
        "predecessor_manifest_hashes": sorted(item.canonical() for item in predecessors),
        "provenance": provenance.canonical_payload(),
    }


def canonical_supersedes_bytes(payload: dict[str, Any]) -> bytes:
    """Serialize a relation payload deterministically.

    The established idiom: sorted keys, no whitespace, ASCII-escaped, and no
    non-finite numbers.
    """
    return json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False
    ).encode("ascii")


def compute_supersedes_relation_hash(payload: dict[str, Any]) -> SupersedesRelationHash:
    """Return the SHA-256 of a relation's canonical serialization."""
    return SupersedesRelationHash(
        value=hashlib.sha256(canonical_supersedes_bytes(payload)).hexdigest()
    )


@final
class SupersedesRelation(CanonicalModel):
    """One immutable, operator-declared correction claim.

    Structural invariants are enforced here, so a structurally impossible claim
    is unconstructible rather than merely rejected somewhere later. The
    invariants that need *both manifests* — same contract, same record type,
    different semantic identity — cannot be checked from hashes alone and are
    enforced at publication, where the manifests are resolved.
    """

    relation_format: str
    successor: ManifestHash
    predecessors: tuple[ManifestHash, ...]
    provenance: RelationProvenance
    relation_hash: SupersedesRelationHash

    @model_validator(mode="after")
    def _validate_relation(self) -> SupersedesRelation:
        if self.relation_format != SUPERSEDES_FORMAT:
            raise ValueError(
                f"relation format must be {SUPERSEDES_FORMAT!r}; got {self.relation_format!r}"
            )

        # Exact digest types, for the ADR-009 D4 reason applied to identity:
        # ``@final`` binds a type checker and not the interpreter, and a subclass
        # overriding ``canonical()`` would serialize one digest while the field
        # held another.
        require_digest(self.successor, ManifestHash)
        require_digest(self.relation_hash, SupersedesRelationHash)
        for predecessor in self.predecessors:
            require_digest(predecessor, ManifestHash)

        if not self.predecessors:
            raise ValueError(
                "a supersedes relation must name at least one predecessor; a claim to "
                "correct nothing is not a claim"
            )

        digests = [item.canonical() for item in self.predecessors]
        if len(set(digests)) != len(digests):
            raise ValueError(
                f"predecessors are a set and must be unique; got {digests}. A repeated "
                "predecessor would let one claim have two spellings"
            )
        if digests != sorted(digests):
            raise ValueError(
                "predecessors must be in canonical sorted order, so that the caller's "
                "argument order cannot survive into the stored relation"
            )
        if self.successor.canonical() in digests:
            raise ValueError(
                f"{self.successor.canonical()} cannot supersede itself; a self-reference "
                "is a cycle of length one and describes no correction"
            )

        expected = compute_supersedes_relation_hash(
            supersedes_canonical_payload(
                successor=self.successor,
                predecessors=self.predecessors,
                provenance=self.provenance,
            )
        )
        if self.relation_hash != expected:
            raise ValueError(
                f"relation_hash does not describe this relation; expected "
                f"{expected.canonical()}, got {self.relation_hash.canonical()}"
            )
        return self

    def canonical_payload(self) -> dict[str, Any]:
        """Return the payload this relation's hash is computed over."""
        return supersedes_canonical_payload(
            successor=self.successor,
            predecessors=self.predecessors,
            provenance=self.provenance,
        )

    def manifest_hashes(self) -> tuple[ManifestHash, ...]:
        """Return every manifest this relation touches, deterministically ordered."""
        return (*self.predecessors, self.successor)


def build_supersedes_relation(
    *,
    successor: ManifestHash,
    predecessors: tuple[ManifestHash, ...] | list[ManifestHash],
    provenance: RelationProvenance | None = None,
) -> SupersedesRelation:
    """Build a relation, sorting predecessors and computing its own hash.

    This is where caller order is discarded: predecessors are sorted by digest
    before the model sees them, so ``[A, B]`` and ``[B, A]`` produce the *same*
    relation with the same hash.

    Args:
        successor: The manifest declared to correct the predecessors.
        predecessors: One or more manifests being superseded, in any order.
        provenance: Defaults to operator-declared, the only kind this
            repository can currently support.
    """
    ordered = tuple(sorted(predecessors, key=lambda item: item.canonical()))
    resolved = operator_declared() if provenance is None else provenance
    payload = supersedes_canonical_payload(
        successor=successor, predecessors=ordered, provenance=resolved
    )
    return SupersedesRelation(
        relation_format=SUPERSEDES_FORMAT,
        successor=successor,
        predecessors=ordered,
        provenance=resolved,
        relation_hash=compute_supersedes_relation_hash(payload),
    )


def relation_to_json_bytes(relation: SupersedesRelation) -> bytes:
    """Serialize a relation for publication, hash included.

    The stored document is the canonical payload plus the relation hash, so a
    reader can verify the file without reconstructing the model first.
    """
    document = dict(relation.canonical_payload())
    document["relation_hash"] = relation.relation_hash.canonical()
    return json.dumps(
        document, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False
    ).encode("ascii")
