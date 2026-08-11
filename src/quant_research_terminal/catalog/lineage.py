"""Publishing, navigating, and rebuilding the dataset supersedes graph.

A relation is published as an immutable JSON file named by its own content hash,
under ``<catalog_root>/lineage/``. Publishing that file is the lineage commit
point, exactly as publishing a manifest is the registration commit point.

Edge orientation, stated once and used everywhere
-------------------------------------------------
Edges run **predecessor → successor**, in the direction corrections were
declared. So :func:`successors_of` answers "what has been declared to correct
this?" and :func:`predecessors_of` answers "what does this claim to correct?".

The index is not authority, and here that is provable
-----------------------------------------------------
Navigation always reads the published relation files. The lineage index is a
discovery cache and **no navigation answer depends on it** — which is the only
way "the index is never authority" is a fact rather than an aspiration.

This is the ADR-012 lesson applied. There, the catalog index held the only
manifest→location binding, so losing it lost something real. Here the relation
files *are* the graph: each is named by its content hash and names its own
successor and predecessors, so an index can only ever restate them. An index
that restates immutable content is the half that can go stale, and code that
trusted it would give a silently wrong answer after the crash window §16
describes — a relation published, the index not yet updated.

So the index exists for discovery (which manifests participate in lineage at
all, without parsing every relation), it is rebuilt by
:func:`rebuild_lineage_index`, and :func:`verify_lineage_index` reports staleness
rather than letting it become truth.

Concurrency, stated at the same standard
----------------------------------------
Relation *files* are safe under concurrency: publication uses the same ``O_EXCL``
protocol as manifests, so two processes racing to publish cannot interleave.

The **acyclicity invariant is not** safe under concurrency, and pretending
otherwise would be worse than the limitation. Publication reads the graph, checks
for a cycle, then writes; two processes publishing opposite edges simultaneously
can each observe an acyclic graph and both commit, leaving a cycle on disk. No
lock is taken, because a correct cross-platform file lock is a design decision
this phase does not need to make. A single publishing process per catalog root is
therefore assumed — and unlike an assumption that is merely hoped for, it is
auditable: :func:`verify_lineage_acyclic` re-checks the entire published graph at
any time.

Durability is not claimed
-------------------------
The same terms as the rest of the catalog: ``os.replace`` gives visibility
atomicity, not durability. No ``fsync`` is issued on the file or its directory,
and directory ``fsync`` has no Windows equivalent, so the recovery story is
detection rather than prevention.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any, Final, final

from pydantic import model_validator

from quant_research_terminal.catalog.errors import (
    InvalidSupersedesRelationError,
    LineageCycleError,
    LineageIndexError,
    MissingRelationError,
    RelationConflictError,
    RelationHashMismatchError,
)
from quant_research_terminal.catalog.manifest import DatasetManifest
from quant_research_terminal.catalog.store import (
    CATALOG_PARTIAL_SUFFIX,
    publish_bytes_atomically,
    read_manifest,
    write_index_bytes,
)
from quant_research_terminal.domain.common import CanonicalModel
from quant_research_terminal.domain.dataset_identity import ManifestHash, require_digest
from quant_research_terminal.domain.dataset_lineage import (
    SUPERSEDES_FORMAT,
    RelationProvenance,
    RelationProvenanceKind,
    SupersedesRelation,
    SupersedesRelationHash,
    build_supersedes_relation,
    relation_to_json_bytes,
)

#: Directory under the catalog root holding published relations.
LINEAGE_DIRECTORY: Final[str] = "lineage"

#: The rebuildable lineage discovery cache.
#:
#: Deliberately a sibling of the lineage directory rather than a file inside it,
#: so that globbing ``lineage/*.json`` for relations cannot pick the index up.
LINEAGE_INDEX_FILENAME: Final[str] = "lineage-index.json"

#: Bumping this re-keys the on-disk lineage index format.
LINEAGE_INDEX_FORMAT: Final[str] = "qrt-lineage-index/1"


def lineage_directory(catalog_root: Path) -> Path:
    """Return the directory published relations live in."""
    return catalog_root / LINEAGE_DIRECTORY


def relation_path(catalog_root: Path, relation_hash: SupersedesRelationHash) -> Path:
    """Return where a relation is published.

    Named by content, so the filename is discovery convenience rather than
    identity, and relocating a catalog cannot change a relation's hash.
    """
    return lineage_directory(catalog_root) / f"{relation_hash.canonical()}.json"


def lineage_index_path(catalog_root: Path) -> Path:
    """Return the lineage index's path."""
    return catalog_root / LINEAGE_INDEX_FILENAME


# --------------------------------------------------------------------------
# Reading published relations
# --------------------------------------------------------------------------


def relation_from_json_bytes(payload: bytes) -> SupersedesRelation:
    """Parse and fully re-verify a published relation document.

    The stored hash is checked against a hash recomputed from the stored
    content, and the document is re-serialized and compared byte for byte, so a
    hand-edited relation is rejected rather than believed.
    """
    try:
        document = json.loads(payload.decode("ascii"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise RelationHashMismatchError(f"relation document is unreadable: {error}") from error
    if not isinstance(document, dict):
        raise RelationHashMismatchError("relation document must be a JSON object")

    try:
        provenance = RelationProvenance(kind=RelationProvenanceKind(document["provenance"]["kind"]))
        predecessors = tuple(
            ManifestHash(value=item) for item in document["predecessor_manifest_hashes"]
        )
        relation = SupersedesRelation(
            relation_format=str(document["format"]),
            successor=ManifestHash(value=document["successor_manifest_hash"]),
            predecessors=predecessors,
            provenance=provenance,
            relation_hash=SupersedesRelationHash(value=str(document["relation_hash"])),
        )
    except RelationHashMismatchError:
        raise
    except (KeyError, TypeError, ValueError, OverflowError) as error:
        # ``OverflowError`` is an ``ArithmeticError`` rather than a
        # ``ValueError``; the manifest reader names it for the same reason.
        raise RelationHashMismatchError(f"relation document is invalid: {error}") from error

    if relation_to_json_bytes(relation) != payload:
        raise RelationHashMismatchError(
            "relation document is not in canonical form; re-serializing it produces "
            "different bytes, so its stored hash cannot be trusted"
        )
    return relation


def read_relation(catalog_root: Path, relation_hash: SupersedesRelationHash) -> SupersedesRelation:
    """Return one published relation.

    Raises:
        MissingRelationError: if nothing is published under this hash.
        RelationHashMismatchError: if the published document does not describe
            the requested relation.
    """
    path = relation_path(catalog_root, relation_hash)
    if not path.exists():
        raise MissingRelationError(f"no relation published under {relation_hash.canonical()}")
    relation = relation_from_json_bytes(path.read_bytes())
    if relation.relation_hash != relation_hash:
        raise RelationHashMismatchError(
            f"{path} is published under {relation_hash.canonical()} but describes "
            f"{relation.relation_hash.canonical()}"
        )
    return relation


def published_relations(catalog_root: Path) -> tuple[SupersedesRelation, ...]:
    """Return every published relation, ordered by relation hash.

    Walks only the configured lineage directory. Unpublished
    ``<hash>.json.catalog-partial`` files are excluded by the ``*.json`` pattern
    itself — a partial is not a published claim — so no separate suffix test is
    written here to imply a check that would never run. A committed relation that
    fails to parse is **not** skipped: lineage corruption must be loud, because
    silently omitting a relation would silently omit a correction.
    """
    directory = lineage_directory(catalog_root)
    if not directory.exists():
        return ()
    relations = [
        relation_from_json_bytes(path.read_bytes()) for path in sorted(directory.glob("*.json"))
    ]
    return tuple(sorted(relations, key=lambda item: item.relation_hash.canonical()))


# --------------------------------------------------------------------------
# The graph
# --------------------------------------------------------------------------


@final
class LineageGraph:
    """The supersedes graph, built from published relations.

    Deterministic throughout: every returned collection is a tuple ordered by
    canonical digest, so no answer can depend on set or dict iteration order.

    Deliberately absent: ``latest``, ``current``, ``active``, ``preferred``, and
    any other name that would pick a winner. A graph with two independent
    corrections of one predecessor has no natural answer, and a linear chain
    must not be allowed to imply one — a future caller pins an exact
    :class:`ManifestHash` or applies an explicit policy of its own.
    """

    __slots__ = ("_by_manifest", "_relations")

    def __init__(self, relations: Iterable[SupersedesRelation]) -> None:
        ordered = tuple(sorted(relations, key=lambda item: item.relation_hash.canonical()))
        self._relations = ordered
        by_manifest: dict[str, list[SupersedesRelation]] = {}
        for relation in ordered:
            for manifest_hash in relation.manifest_hashes():
                by_manifest.setdefault(manifest_hash.canonical(), []).append(relation)
        self._by_manifest = by_manifest

    @property
    def relations(self) -> tuple[SupersedesRelation, ...]:
        """Every relation in the graph, ordered by relation hash."""
        return self._relations

    def relations_for(self, manifest_hash: ManifestHash) -> tuple[SupersedesRelation, ...]:
        """Return every relation naming this manifest, in either position."""
        return tuple(self._by_manifest.get(manifest_hash.canonical(), ()))

    def successors_of(self, manifest_hash: ManifestHash) -> tuple[ManifestHash, ...]:
        """Return manifests declared to correct this one, directly.

        One hop, not the transitive closure: reporting descendants as though
        they were direct corrections would flatten the very structure the graph
        exists to record. Use :meth:`descendants_of` when the closure is wanted.
        """
        found = {
            relation.successor.canonical(): relation.successor
            for relation in self.relations_for(manifest_hash)
            if manifest_hash in relation.predecessors
        }
        return tuple(found[key] for key in sorted(found))

    def predecessors_of(self, manifest_hash: ManifestHash) -> tuple[ManifestHash, ...]:
        """Return manifests this one claims to correct, directly."""
        found = {
            predecessor.canonical(): predecessor
            for relation in self.relations_for(manifest_hash)
            if relation.successor == manifest_hash
            for predecessor in relation.predecessors
        }
        return tuple(found[key] for key in sorted(found))

    def descendants_of(self, manifest_hash: ManifestHash) -> tuple[ManifestHash, ...]:
        """Return every manifest transitively declared to correct this one.

        Informational and audit semantics only. Being a descendant says nothing
        about preference: a branch may have several, and none of them is "the"
        answer.
        """
        return self._reachable(manifest_hash, self.successors_of)

    def ancestors_of(self, manifest_hash: ManifestHash) -> tuple[ManifestHash, ...]:
        """Return every manifest this one transitively claims to correct."""
        return self._reachable(manifest_hash, self.predecessors_of)

    def _reachable(
        self,
        start: ManifestHash,
        step: Any,
    ) -> tuple[ManifestHash, ...]:
        seen: dict[str, ManifestHash] = {}
        frontier = [start]
        while frontier:
            current = frontier.pop()
            for nxt in step(current):
                key = nxt.canonical()
                if key not in seen:
                    seen[key] = nxt
                    frontier.append(nxt)
        return tuple(seen[key] for key in sorted(seen))

    def manifests(self) -> tuple[ManifestHash, ...]:
        """Return every manifest appearing anywhere in the graph."""
        return tuple(ManifestHash(value=key) for key in sorted(self._by_manifest))

    def with_relation(self, relation: SupersedesRelation) -> LineageGraph:
        """Return a graph including ``relation``, without mutating this one."""
        if any(item.relation_hash == relation.relation_hash for item in self._relations):
            return self
        return LineageGraph((*self._relations, relation))


def load_lineage_graph(catalog_root: Path) -> LineageGraph:
    """Return the supersedes graph as the published relations define it.

    Authoritative. Reads the relation files, never the index, so the answer is
    the same whether or not an index exists and whether or not it is current.
    """
    return LineageGraph(published_relations(catalog_root))


# --------------------------------------------------------------------------
# Cycle detection
# --------------------------------------------------------------------------


def find_cycle(graph: LineageGraph) -> tuple[ManifestHash, ...] | None:
    """Return one cycle in the supersedes graph, or ``None`` if it is acyclic.

    Iterative depth-first search with an explicit colouring, so a deep chain
    cannot exhaust the interpreter stack. Nodes and neighbours are both visited
    in canonical digest order, which makes the reported cycle a deterministic
    function of the graph's *content* — independent of the order relations were
    published or read.
    """
    white, grey, black = 0, 1, 2
    colour: dict[str, int] = {}
    parent: dict[str, ManifestHash] = {}

    for root in graph.manifests():
        if colour.get(root.canonical(), white) != white:
            continue
        stack: list[tuple[ManifestHash, bool]] = [(root, False)]
        while stack:
            node, expanded = stack.pop()
            key = node.canonical()
            if expanded:
                colour[key] = black
                continue
            if colour.get(key, white) != white:
                continue
            colour[key] = grey
            stack.append((node, True))
            for successor in graph.successors_of(node):
                successor_key = successor.canonical()
                state = colour.get(successor_key, white)
                if state == grey:
                    return _cycle_path(parent, node, successor)
                if state == white:
                    parent[successor_key] = node
                    stack.append((successor, False))
    return None


def _cycle_path(
    parent: Mapping[str, ManifestHash], node: ManifestHash, target: ManifestHash
) -> tuple[ManifestHash, ...]:
    """Rebuild the cycle closed by the edge ``node -> target``."""
    path = [target, node]
    cursor = node
    while cursor.canonical() != target.canonical():
        step = parent.get(cursor.canonical())
        if step is None:
            break
        path.append(step)
        cursor = step
    return tuple(reversed(path))


# --------------------------------------------------------------------------
# Publication
# --------------------------------------------------------------------------


def validate_supersedes_relation(
    *, catalog_root: Path, relation: SupersedesRelation
) -> tuple[DatasetManifest, tuple[DatasetManifest, ...]]:
    """Resolve and check every invariant that needs the manifests themselves.

    The structural invariants — non-empty, unique, sorted predecessors, no
    self-reference, a self-consistent hash — are already guaranteed by
    :class:`SupersedesRelation`. These are the ones hashes alone cannot decide.

    Returns:
        The successor manifest and the predecessor manifests, ordered as the
        relation orders them.

    Raises:
        MissingManifestError: if any referenced manifest is not published. The
            relation is between identities, so they must be identities the
            catalog actually knows.
        InvalidSupersedesRelationError: if the manifests describe different
            contracts, different record types, or the same dataset semantics.
    """
    successor = read_manifest(catalog_root, relation.successor)
    predecessors = tuple(
        read_manifest(catalog_root, predecessor) for predecessor in relation.predecessors
    )

    for predecessor in predecessors:
        if predecessor.record_type is not successor.record_type:
            raise InvalidSupersedesRelationError(
                f"{relation.successor.canonical()} holds {successor.record_type.value} "
                f"records but {predecessor.manifest_hash.canonical()} holds "
                f"{predecessor.record_type.value}; a correction replaces data of the same "
                f"kind"
            )
        if predecessor.contract != successor.contract:
            raise InvalidSupersedesRelationError(
                f"{relation.successor.canonical()} describes "
                f"{successor.contract.canonical()} but "
                f"{predecessor.manifest_hash.canonical()} describes "
                f"{predecessor.contract.canonical()}; datasets for different listed "
                f"contracts are unrelated, not corrections of one another"
            )
        if predecessor.semantic_hash == successor.semantic_hash:
            raise InvalidSupersedesRelationError(
                f"{relation.successor.canonical()} and "
                f"{predecessor.manifest_hash.canonical()} have the same semantic dataset "
                f"hash {successor.semantic_hash.canonical()}, so there is no corrected "
                f"data to declare. Two manifests over identical canonical records differ "
                f"in provenance or physical encoding — that is variation, not correction"
            )
    return successor, predecessors


def publish_supersedes_relation(
    *,
    catalog_root: Path,
    successor: ManifestHash,
    predecessors: tuple[ManifestHash, ...] | list[ManifestHash],
    provenance: RelationProvenance | None = None,
) -> SupersedesRelation:
    """Validate, publish, and index one operator-declared correction claim.

    The order is the commit order, and everything this process can decide is
    decided before the commit point:

    1. build the relation, which enforces every structural invariant;
    2. resolve the manifests and check the invariants that need them;
    3. check that the resulting graph stays acyclic;
    4. **publish the relation file** — the lineage commit point;
    5. update the rebuildable index, strictly afterwards.

    A crash before step 4 leaves no relation. A crash between 4 and 5 leaves the
    relation published and the index stale, which changes no navigation answer
    because navigation reads the relations; :func:`rebuild_lineage_index`
    refreshes the cache. Step 5 failing for any reason is likewise **not** a
    failure of the publication: the relation is committed, so a cache error is
    swallowed rather than reported as though nothing had happened.

    The acyclicity check in step 3 is a check against the graph *this process*
    can see. A concurrent publisher can defeat it — see the module docstring — so
    a single publishing process per catalog root is assumed, and
    :func:`verify_lineage_acyclic` exists to audit that assumption.

    Publishing the same claim twice is idempotent: the relation hash is a
    function of its content, so the second call finds identical bytes already
    published and adds no duplicate edge.

    Note that this does **not** require the artifacts to be online. A relation is
    a claim between two manifest identities, so it stays publishable when the
    Parquet files are archived or offline. Comparison, which reads rows, has its
    own artifact requirements.
    """
    relation = build_supersedes_relation(
        successor=successor, predecessors=predecessors, provenance=provenance
    )
    validate_supersedes_relation(catalog_root=catalog_root, relation=relation)

    published = load_lineage_graph(catalog_root)
    # Distinguish "this claim closes a loop" from "the graph was already cyclic".
    # Blaming the incoming relation for a pre-existing cycle would send an
    # operator to inspect a claim that has nothing to do with the problem.
    existing_cycle = find_cycle(published)
    if existing_cycle is not None:
        raise LineageCycleError(
            f"the published supersedes graph is already cyclic and must be repaired "
            f"before anything further is published: "
            f"{' -> '.join(item.canonical() for item in existing_cycle)}. This relation "
            f"is not the cause"
        )

    graph = published.with_relation(relation)
    cycle = find_cycle(graph)
    if cycle is not None:
        raise LineageCycleError(
            f"publishing this relation would make the supersedes graph cyclic: "
            f"{' -> '.join(item.canonical() for item in cycle)}. A correction history "
            f"cannot lead back to itself"
        )

    publish_bytes_atomically(
        relation_path(catalog_root, relation.relation_hash),
        relation_to_json_bytes(relation),
        conflict_error=RelationConflictError,
    )

    # Past the commit point. The relation is published and authoritative, so a
    # failure to refresh the *cache* must not surface as a failed publication:
    # the caller would be told nothing happened when the claim in fact exists.
    # The cache is rebuildable and navigation never reads it.
    try:
        write_lineage_index(catalog_root, LineageIndex.from_graph(graph))
    except OSError:
        pass
    return relation


# --------------------------------------------------------------------------
# The discovery cache
# --------------------------------------------------------------------------


@final
class LineageIndexEntry(CanonicalModel):
    """Which relations name one manifest, in either position."""

    manifest_hash: ManifestHash
    relation_hashes: tuple[SupersedesRelationHash, ...]

    @model_validator(mode="after")
    def _validate_entry(self) -> LineageIndexEntry:
        # This class is what gets serialized to disk, so it needs the same
        # exact-type guard as the relation: a digest subclass overriding
        # ``canonical()`` would write one value while the field held another.
        require_digest(self.manifest_hash, ManifestHash)
        for relation_hash in self.relation_hashes:
            require_digest(relation_hash, SupersedesRelationHash)
        digests = [item.canonical() for item in self.relation_hashes]
        if not digests:
            raise ValueError("an index entry with no relations records nothing")
        if len(set(digests)) != len(digests):
            raise ValueError(f"relation hashes must be unique; got {digests}")
        if digests != sorted(digests):
            raise ValueError("relation hashes must be in canonical sorted order")
        return self


@final
class LineageIndex(CanonicalModel):
    """A rebuildable map from manifest to the relations naming it.

    Discovery only. Nothing in :class:`LineageGraph` consults it, so a stale or
    missing index cannot change an answer — see this module's docstring for why
    that is the deliberate design rather than an oversight.
    """

    index_format: str
    entries: tuple[LineageIndexEntry, ...]

    @model_validator(mode="after")
    def _validate_index(self) -> LineageIndex:
        if self.index_format != LINEAGE_INDEX_FORMAT:
            raise ValueError(
                f"index format must be {LINEAGE_INDEX_FORMAT!r}; got {self.index_format!r}"
            )
        keys = [entry.manifest_hash.canonical() for entry in self.entries]
        if keys != sorted(keys):
            raise ValueError("index entries must be in canonical sorted order")
        if len(set(keys)) != len(keys):
            raise ValueError("one manifest cannot have two index entries")
        return self

    @classmethod
    def from_graph(cls, graph: LineageGraph) -> LineageIndex:
        """Derive the cache from a graph."""
        entries = []
        for manifest_hash in graph.manifests():
            relations = graph.relations_for(manifest_hash)
            entries.append(
                LineageIndexEntry(
                    manifest_hash=manifest_hash,
                    relation_hashes=tuple(
                        sorted(
                            (item.relation_hash for item in relations),
                            key=lambda item: item.canonical(),
                        )
                    ),
                )
            )
        return cls(index_format=LINEAGE_INDEX_FORMAT, entries=tuple(entries))

    def to_json_bytes(self) -> bytes:
        """Serialize deterministically, so a rebuild can be compared byte-wise."""
        document: dict[str, Any] = {
            "format": self.index_format,
            "entries": [
                {
                    "manifest_hash": entry.manifest_hash.canonical(),
                    "relation_hashes": [item.canonical() for item in entry.relation_hashes],
                }
                for entry in self.entries
            ],
        }
        return json.dumps(
            document, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False
        ).encode("ascii")


def empty_lineage_index() -> LineageIndex:
    """Return an index with no entries."""
    return LineageIndex(index_format=LINEAGE_INDEX_FORMAT, entries=())


def lineage_index_from_json_bytes(payload: bytes) -> LineageIndex:
    """Parse a serialized lineage index.

    Raises:
        LineageIndexError: if the document is unreadable.
    """
    try:
        document = json.loads(payload.decode("ascii"))
        return LineageIndex(
            index_format=str(document["format"]),
            entries=tuple(
                LineageIndexEntry(
                    manifest_hash=ManifestHash(value=item["manifest_hash"]),
                    relation_hashes=tuple(
                        SupersedesRelationHash(value=value) for value in item["relation_hashes"]
                    ),
                )
                for item in document["entries"]
            ),
        )
    except (UnicodeDecodeError, json.JSONDecodeError, KeyError, TypeError, ValueError) as error:
        raise LineageIndexError(f"lineage index is unreadable: {error}") from error


def load_lineage_index(catalog_root: Path) -> LineageIndex:
    """Return the stored cache, or an empty one if none has been written."""
    path = lineage_index_path(catalog_root)
    if not path.exists():
        return empty_lineage_index()
    return lineage_index_from_json_bytes(path.read_bytes())


def write_lineage_index(catalog_root: Path, index: LineageIndex) -> None:
    """Replace the lineage cache with an atomic rename."""
    write_index_bytes(lineage_index_path(catalog_root), index.to_json_bytes())


def rebuild_lineage_index(catalog_root: Path) -> LineageIndex:
    """Rediscover the cache by reading the published relations.

    Total with respect to *ambiguity*: relation files are self-describing and
    named by content, so unlike a location rebuild there is never a binding to
    guess between and no well-formed case this refuses.

    It is **not** total with respect to corruption. A committed relation that
    does not parse raises, because rebuilding around it would quietly drop a
    published correction from the graph — and a rebuild that silently returns a
    smaller graph than the one on disk is worse than one that stops. Repairing
    the corrupt file is an operator decision, not this function's.
    """
    rebuilt = LineageIndex.from_graph(load_lineage_graph(catalog_root))
    write_lineage_index(catalog_root, rebuilt)
    return rebuilt


def verify_lineage_index(catalog_root: Path) -> bool:
    """Return whether the stored cache matches the published relations.

    Staleness is reported, never repaired silently and never believed. It is an
    expected state after the crash window between publication and the index
    write, and it is harmless precisely because navigation does not read here.

    An **unreadable** index answers ``False`` rather than raising. A function
    whose entire contract is to return a bool must not throw — the storage layer
    names this same trap for ``verify_registration`` — and "the cache does not
    match the relations" is the truthful answer for a corrupt cache as much as
    for a stale one. A corrupt *relation* still raises, because that is lineage
    corruption rather than cache staleness.
    """
    expected = LineageIndex.from_graph(load_lineage_graph(catalog_root))
    try:
        stored = load_lineage_index(catalog_root)
    except LineageIndexError:
        return False
    return stored.to_json_bytes() == expected.to_json_bytes()


def verify_lineage_acyclic(catalog_root: Path) -> tuple[ManifestHash, ...] | None:
    """Audit the whole published graph, returning a cycle if one exists.

    Publication checks acyclicity against the graph the publishing process can
    see, which a concurrent publisher can defeat. This re-checks everything on
    disk, so the single-writer assumption is verifiable rather than merely
    stated: an operator can run it after a crash, after a migration, or on a
    schedule, and find out.

    Returns:
        The cycle, deterministically ordered, or ``None`` if the graph is a DAG.
    """
    return find_cycle(load_lineage_graph(catalog_root))


def unpublished_relation_partials(catalog_root: Path) -> tuple[Path, ...]:
    """Return partial relation files, which are not published lineage.

    Exposed so an operator can see them; they are never read as relations, never
    deleted here (removing one could race a live writer), and never counted.
    """
    directory = lineage_directory(catalog_root)
    if not directory.exists():
        return ()
    return tuple(sorted(directory.glob(f"*{CATALOG_PARTIAL_SUFFIX}")))


__all__ = [
    "LINEAGE_DIRECTORY",
    "LINEAGE_INDEX_FILENAME",
    "LINEAGE_INDEX_FORMAT",
    "SUPERSEDES_FORMAT",
    "LineageGraph",
    "LineageIndex",
    "LineageIndexEntry",
    "empty_lineage_index",
    "find_cycle",
    "lineage_directory",
    "lineage_index_from_json_bytes",
    "lineage_index_path",
    "load_lineage_graph",
    "load_lineage_index",
    "publish_supersedes_relation",
    "published_relations",
    "read_relation",
    "rebuild_lineage_index",
    "relation_from_json_bytes",
    "relation_path",
    "unpublished_relation_partials",
    "validate_supersedes_relation",
    "verify_lineage_acyclic",
    "verify_lineage_index",
    "write_lineage_index",
]
