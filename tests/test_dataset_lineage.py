"""The supersedes relation, its graph, and its persistence.

The properties under attack:

* a correction claim never mutates, invalidates, or redirects what it corrects;
* the claim's identity is a function of its content and nothing else — not the
  caller's argument order, not a path, not a clock, not a publication sequence;
* the graph branches and joins, and cycles are impossible;
* two manifests over the same canonical data cannot be a correction of one
  another, whatever their provenance says;
* the index is a cache, and losing it changes no answer.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest
from dataset_fixtures import ES_M2026, ES_U2026, trade
from lineage_fixtures import register
from pydantic import ValidationError

from quant_research_terminal.catalog import (
    InvalidSupersedesRelationError,
    LineageCycleError,
    LineageIndexError,
    MissingManifestError,
    MissingRelationError,
    RelationConflictError,
    RelationHashMismatchError,
    VerificationOutcome,
    empty_lineage_index,
    find_cycle,
    lineage_index_path,
    load_index,
    load_lineage_graph,
    load_lineage_index,
    publish_supersedes_relation,
    published_relations,
    read_manifest,
    read_relation,
    rebuild_lineage_index,
    relation_path,
    unpublished_relation_partials,
    verify_lineage_acyclic,
    verify_lineage_index,
    verify_registration,
)
from quant_research_terminal.domain.dataset_identity import ManifestHash, RecordType
from quant_research_terminal.domain.dataset_lineage import (
    SUPERSEDES_FORMAT,
    RelationProvenance,
    RelationProvenanceKind,
    SupersedesRelation,
    SupersedesRelationHash,
    build_supersedes_relation,
    operator_declared,
    relation_to_json_bytes,
)
from quant_research_terminal.domain.models import Trade


@pytest.fixture
def roots(tmp_path: Path) -> tuple[Path, Path]:
    storage = tmp_path / "storage"
    catalog = tmp_path / "catalog"
    storage.mkdir()
    return catalog, storage


def rows(offset: int) -> list[Trade]:
    """Distinct records, so each dataset has a distinct semantic hash."""
    return [trade(offset=offset, price=f"45{offset:02d}.25")]


def hash_of(value: str) -> ManifestHash:
    return ManifestHash(value=value)


# --------------------------------------------------------------------------
# The relation value object
# --------------------------------------------------------------------------


def test_a_relation_hash_describes_its_own_content() -> None:
    relation = build_supersedes_relation(
        successor=hash_of("b" * 64), predecessors=[hash_of("a" * 64)]
    )
    assert relation.relation_format == SUPERSEDES_FORMAT
    assert relation.provenance == operator_declared()
    assert relation.relation_hash.canonical() == relation.relation_hash.value


def test_predecessor_argument_order_does_not_change_the_hash() -> None:
    """A predecessor set has no order, so one claim must not have two identities."""
    first = build_supersedes_relation(
        successor=hash_of("c" * 64), predecessors=[hash_of("a" * 64), hash_of("b" * 64)]
    )
    second = build_supersedes_relation(
        successor=hash_of("c" * 64), predecessors=[hash_of("b" * 64), hash_of("a" * 64)]
    )
    assert first.relation_hash == second.relation_hash
    assert first == second
    assert relation_to_json_bytes(first) == relation_to_json_bytes(second)


def test_predecessors_are_stored_in_canonical_order() -> None:
    relation = build_supersedes_relation(
        successor=hash_of("c" * 64), predecessors=[hash_of("b" * 64), hash_of("a" * 64)]
    )
    assert [item.canonical() for item in relation.predecessors] == ["a" * 64, "b" * 64]


def test_an_empty_predecessor_set_is_refused() -> None:
    with pytest.raises(ValidationError, match="at least one predecessor"):
        build_supersedes_relation(successor=hash_of("b" * 64), predecessors=[])


def test_a_duplicate_predecessor_is_refused() -> None:
    with pytest.raises(ValidationError, match="unique"):
        build_supersedes_relation(
            successor=hash_of("c" * 64), predecessors=[hash_of("a" * 64), hash_of("a" * 64)]
        )


def test_self_supersedes_is_refused() -> None:
    """A cycle of length one, and it describes no correction."""
    with pytest.raises(ValidationError, match="cannot supersede itself"):
        build_supersedes_relation(successor=hash_of("a" * 64), predecessors=[hash_of("a" * 64)])


def test_unsorted_predecessors_are_unconstructible() -> None:
    """The model refuses non-canonical order, so the builder's sort is not the
    only thing standing between a caller and two spellings of one claim."""
    good = build_supersedes_relation(
        successor=hash_of("c" * 64), predecessors=[hash_of("a" * 64), hash_of("b" * 64)]
    )
    with pytest.raises(ValidationError, match="canonical sorted order"):
        SupersedesRelation(**{**dict(good), "predecessors": (hash_of("b" * 64), hash_of("a" * 64))})


def test_a_relation_carrying_a_wrong_hash_is_unconstructible() -> None:
    good = build_supersedes_relation(successor=hash_of("b" * 64), predecessors=[hash_of("a" * 64)])
    with pytest.raises(ValidationError, match="does not describe"):
        SupersedesRelation(
            **{**dict(good), "relation_hash": SupersedesRelationHash(value="0" * 64)}
        )


def test_relation_model_copy_revalidates() -> None:
    relation = build_supersedes_relation(
        successor=hash_of("b" * 64), predecessors=[hash_of("a" * 64)]
    )
    with pytest.raises(ValidationError):
        relation.model_copy(update={"successor": hash_of("c" * 64)})


def test_a_relation_refuses_a_digest_subclass() -> None:
    """``@final`` binds a type checker, not the interpreter."""
    good = build_supersedes_relation(successor=hash_of("b" * 64), predecessors=[hash_of("a" * 64)])

    class Forged(ManifestHash):  # type: ignore[misc]
        def canonical(self) -> str:
            return "0" * 64

    with pytest.raises(TypeError, match="exactly"):
        SupersedesRelation(**{**dict(good), "successor": Forged(value="b" * 64)})


def test_a_relation_has_no_path_clock_or_identifier_field() -> None:
    relation = build_supersedes_relation(
        successor=hash_of("b" * 64), predecessors=[hash_of("a" * 64)]
    )
    fields = {name.lower() for name in type(relation).model_fields}
    forbidden = (
        "path",
        "location",
        "uuid",
        "created",
        "published",
        "timestamp",
        "sequence",
        "revision",
        "notes",
        "comment",
    )
    assert not any(token in name for name in fields for token in forbidden)
    assert "revision" not in relation.canonical_payload()


def test_only_operator_declared_provenance_exists() -> None:
    """No placeholder kind: nothing here can produce a source-declared claim."""
    assert [member.value for member in RelationProvenanceKind] == ["operator-declared"]


def test_provenance_refuses_a_foreign_equal_valued_enum_member() -> None:
    with pytest.raises((TypeError, ValidationError)):
        RelationProvenance(kind="operator-declared")  # type: ignore[arg-type]


# --------------------------------------------------------------------------
# Publication invariants that need the manifests
# --------------------------------------------------------------------------


def test_publishing_a_simple_correction(roots: tuple[Path, Path]) -> None:
    catalog, storage = roots
    a = register(catalog, storage, "a.parquet", rows(1))
    b = register(catalog, storage, "b.parquet", rows(2))

    relation = publish_supersedes_relation(
        catalog_root=catalog, successor=b.manifest_hash, predecessors=[a.manifest_hash]
    )
    assert relation_path(catalog, relation.relation_hash).exists()
    assert read_relation(catalog, relation.relation_hash) == relation

    graph = load_lineage_graph(catalog)
    assert graph.successors_of(a.manifest_hash) == (b.manifest_hash,)
    assert graph.predecessors_of(b.manifest_hash) == (a.manifest_hash,)
    assert graph.successors_of(b.manifest_hash) == ()


def test_publishing_the_same_claim_twice_is_idempotent(roots: tuple[Path, Path]) -> None:
    catalog, storage = roots
    a = register(catalog, storage, "a.parquet", rows(1))
    b = register(catalog, storage, "b.parquet", rows(2))

    first = publish_supersedes_relation(
        catalog_root=catalog, successor=b.manifest_hash, predecessors=[a.manifest_hash]
    )
    second = publish_supersedes_relation(
        catalog_root=catalog, successor=b.manifest_hash, predecessors=[a.manifest_hash]
    )
    assert first.relation_hash == second.relation_hash
    assert len(published_relations(catalog)) == 1
    assert len(load_lineage_graph(catalog).relations) == 1


def test_same_semantic_hash_cannot_be_a_correction(roots: tuple[Path, Path]) -> None:
    """Provider variation is not revision.

    Two manifests over identical canonical records differ only in provenance or
    physical encoding, so there is no corrected data for the claim to be about.
    """
    catalog, storage = roots
    a = register(catalog, storage, "a.parquet", provider_token="DATABENTO")
    b = register(catalog, storage, "b.parquet", provider_token="THETADATA")
    assert a.semantic_hash == b.semantic_hash
    assert a.manifest_hash != b.manifest_hash

    with pytest.raises(InvalidSupersedesRelationError, match="same semantic dataset hash"):
        publish_supersedes_relation(
            catalog_root=catalog, successor=b.manifest_hash, predecessors=[a.manifest_hash]
        )
    assert published_relations(catalog) == ()


def test_a_different_contract_cannot_be_a_correction(roots: tuple[Path, Path]) -> None:
    catalog, storage = roots
    a = register(catalog, storage, "a.parquet", rows(1), contract=ES_M2026)
    b = register(catalog, storage, "b.parquet", rows(2), contract=ES_U2026)
    with pytest.raises(InvalidSupersedesRelationError, match="different listed"):
        publish_supersedes_relation(
            catalog_root=catalog, successor=b.manifest_hash, predecessors=[a.manifest_hash]
        )


def test_a_different_record_type_cannot_be_a_correction(roots: tuple[Path, Path]) -> None:
    from dataset_fixtures import quote

    catalog, storage = roots
    a = register(catalog, storage, "a.parquet", rows(1))
    b = register(catalog, storage, "b.parquet", [quote(offset=1)], record_type=RecordType.QUOTE)
    with pytest.raises(InvalidSupersedesRelationError, match="same kind"):
        publish_supersedes_relation(
            catalog_root=catalog, successor=b.manifest_hash, predecessors=[a.manifest_hash]
        )


def test_an_unpublished_manifest_cannot_take_part(roots: tuple[Path, Path]) -> None:
    catalog, storage = roots
    a = register(catalog, storage, "a.parquet", rows(1))
    with pytest.raises(MissingManifestError):
        publish_supersedes_relation(
            catalog_root=catalog, successor=hash_of("f" * 64), predecessors=[a.manifest_hash]
        )


def test_a_relation_does_not_need_the_artifacts_online(roots: tuple[Path, Path]) -> None:
    """A relation is between identities, so archived data stays claimable."""
    catalog, storage = roots
    a = register(catalog, storage, "a.parquet", rows(1))
    b = register(catalog, storage, "b.parquet", rows(2))
    (storage / "a.parquet").unlink()
    (storage / "b.parquet").unlink()

    relation = publish_supersedes_relation(
        catalog_root=catalog, successor=b.manifest_hash, predecessors=[a.manifest_hash]
    )
    assert load_lineage_graph(catalog).successors_of(a.manifest_hash) == (b.manifest_hash,)
    assert relation.relation_hash is not None


# --------------------------------------------------------------------------
# Graph shape: branching, multi-parent, chains
# --------------------------------------------------------------------------


def test_a_chain(roots: tuple[Path, Path]) -> None:
    catalog, storage = roots
    a = register(catalog, storage, "a.parquet", rows(1))
    b = register(catalog, storage, "b.parquet", rows(2))
    c = register(catalog, storage, "c.parquet", rows(3))
    publish_supersedes_relation(
        catalog_root=catalog, successor=b.manifest_hash, predecessors=[a.manifest_hash]
    )
    publish_supersedes_relation(
        catalog_root=catalog, successor=c.manifest_hash, predecessors=[b.manifest_hash]
    )

    graph = load_lineage_graph(catalog)
    assert graph.successors_of(a.manifest_hash) == (b.manifest_hash,)
    assert graph.descendants_of(a.manifest_hash) == tuple(
        sorted((b.manifest_hash, c.manifest_hash), key=lambda item: item.canonical())
    )
    assert graph.ancestors_of(c.manifest_hash) == tuple(
        sorted((a.manifest_hash, b.manifest_hash), key=lambda item: item.canonical())
    )


def test_branching_is_allowed_and_neither_branch_is_chosen(
    roots: tuple[Path, Path],
) -> None:
    """A -> B and A -> C are both legitimate independent corrections."""
    catalog, storage = roots
    a = register(catalog, storage, "a.parquet", rows(1))
    b = register(catalog, storage, "b.parquet", rows(2))
    c = register(catalog, storage, "c.parquet", rows(3))
    publish_supersedes_relation(
        catalog_root=catalog, successor=b.manifest_hash, predecessors=[a.manifest_hash]
    )
    publish_supersedes_relation(
        catalog_root=catalog, successor=c.manifest_hash, predecessors=[a.manifest_hash]
    )

    graph = load_lineage_graph(catalog)
    assert graph.successors_of(a.manifest_hash) == tuple(
        sorted((b.manifest_hash, c.manifest_hash), key=lambda item: item.canonical())
    )


def test_multiple_predecessors_join_into_one_successor(roots: tuple[Path, Path]) -> None:
    """A, B -> C: one correction replacing two datasets, with no chain forced."""
    catalog, storage = roots
    a = register(catalog, storage, "a.parquet", rows(1))
    b = register(catalog, storage, "b.parquet", rows(2))
    c = register(catalog, storage, "c.parquet", rows(3))
    publish_supersedes_relation(
        catalog_root=catalog,
        successor=c.manifest_hash,
        predecessors=[a.manifest_hash, b.manifest_hash],
    )

    graph = load_lineage_graph(catalog)
    assert graph.predecessors_of(c.manifest_hash) == tuple(
        sorted((a.manifest_hash, b.manifest_hash), key=lambda item: item.canonical())
    )
    assert graph.successors_of(a.manifest_hash) == (c.manifest_hash,)
    assert graph.successors_of(b.manifest_hash) == (c.manifest_hash,)


def test_navigation_is_deterministically_ordered(roots: tuple[Path, Path]) -> None:
    catalog, storage = roots
    a = register(catalog, storage, "a.parquet", rows(1))
    successors = [register(catalog, storage, f"s{i}.parquet", rows(10 + i)) for i in range(4)]
    for successor in successors:
        publish_supersedes_relation(
            catalog_root=catalog,
            successor=successor.manifest_hash,
            predecessors=[a.manifest_hash],
        )
    found = load_lineage_graph(catalog).successors_of(a.manifest_hash)
    assert [item.canonical() for item in found] == sorted(item.canonical() for item in found)


# --------------------------------------------------------------------------
# Cycles
# --------------------------------------------------------------------------


def test_a_two_node_cycle_is_refused(roots: tuple[Path, Path]) -> None:
    catalog, storage = roots
    a = register(catalog, storage, "a.parquet", rows(1))
    b = register(catalog, storage, "b.parquet", rows(2))
    publish_supersedes_relation(
        catalog_root=catalog, successor=b.manifest_hash, predecessors=[a.manifest_hash]
    )
    with pytest.raises(LineageCycleError, match="cyclic"):
        publish_supersedes_relation(
            catalog_root=catalog, successor=a.manifest_hash, predecessors=[b.manifest_hash]
        )
    assert len(published_relations(catalog)) == 1


def test_a_longer_cycle_is_refused(roots: tuple[Path, Path]) -> None:
    catalog, storage = roots
    a = register(catalog, storage, "a.parquet", rows(1))
    b = register(catalog, storage, "b.parquet", rows(2))
    c = register(catalog, storage, "c.parquet", rows(3))
    publish_supersedes_relation(
        catalog_root=catalog, successor=b.manifest_hash, predecessors=[a.manifest_hash]
    )
    publish_supersedes_relation(
        catalog_root=catalog, successor=c.manifest_hash, predecessors=[b.manifest_hash]
    )
    with pytest.raises(LineageCycleError):
        publish_supersedes_relation(
            catalog_root=catalog, successor=a.manifest_hash, predecessors=[c.manifest_hash]
        )


def test_a_cycle_through_one_of_several_predecessors_is_refused(
    roots: tuple[Path, Path],
) -> None:
    """The check considers every edge the relation adds, not just the first."""
    catalog, storage = roots
    a = register(catalog, storage, "a.parquet", rows(1))
    b = register(catalog, storage, "b.parquet", rows(2))
    c = register(catalog, storage, "c.parquet", rows(3))
    publish_supersedes_relation(
        catalog_root=catalog, successor=c.manifest_hash, predecessors=[a.manifest_hash]
    )
    # Now C -> {B, A} would close A -> C -> A.
    with pytest.raises(LineageCycleError):
        publish_supersedes_relation(
            catalog_root=catalog,
            successor=a.manifest_hash,
            predecessors=[b.manifest_hash, c.manifest_hash],
        )


def test_cycle_detection_is_independent_of_publication_order(
    roots: tuple[Path, Path], tmp_path: Path
) -> None:
    """Same graph content, opposite build order, same reported cycle."""
    catalog, storage = roots
    a = register(catalog, storage, "a.parquet", rows(1))
    b = register(catalog, storage, "b.parquet", rows(2))
    c = register(catalog, storage, "c.parquet", rows(3))

    forwards = build_supersedes_relation(successor=b.manifest_hash, predecessors=[a.manifest_hash])
    backwards = build_supersedes_relation(successor=c.manifest_hash, predecessors=[b.manifest_hash])
    closing = build_supersedes_relation(successor=a.manifest_hash, predecessors=[c.manifest_hash])

    from quant_research_terminal.catalog.lineage import LineageGraph

    one = find_cycle(LineageGraph([forwards, backwards, closing]))
    two = find_cycle(LineageGraph([closing, backwards, forwards]))
    assert one is not None
    assert [item.canonical() for item in one] == [item.canonical() for item in two or ()]


def test_an_acyclic_graph_reports_no_cycle(roots: tuple[Path, Path]) -> None:
    catalog, storage = roots
    a = register(catalog, storage, "a.parquet", rows(1))
    b = register(catalog, storage, "b.parquet", rows(2))
    publish_supersedes_relation(
        catalog_root=catalog, successor=b.manifest_hash, predecessors=[a.manifest_hash]
    )
    assert find_cycle(load_lineage_graph(catalog)) is None


# --------------------------------------------------------------------------
# Old-run stability — the non-negotiable
# --------------------------------------------------------------------------


def test_an_exact_manifest_pin_never_redirects_to_a_successor(
    roots: tuple[Path, Path],
) -> None:
    """The whole reason lineage lives beside the data rather than inside it.

    A research run pinned A. B is later declared to correct A. A must still be
    exactly A — same manifest, same three hashes, same verification — and no API
    may hand back B instead.
    """
    catalog, storage = roots
    a = register(catalog, storage, "a.parquet", rows(1))
    b = register(catalog, storage, "b.parquet", rows(2))
    pinned = (a.manifest_hash, a.semantic_hash, a.physical_hash)

    publish_supersedes_relation(
        catalog_root=catalog, successor=b.manifest_hash, predecessors=[a.manifest_hash]
    )

    recovered = read_manifest(catalog, a.manifest_hash)
    assert recovered == a
    assert (recovered.manifest_hash, recovered.semantic_hash, recovered.physical_hash) == pinned
    assert recovered.manifest_hash != b.manifest_hash

    location = next(
        entry.location
        for entry in load_index(catalog).entries
        if entry.physical_hash == a.physical_hash
    )
    assert (
        verify_registration(
            catalog_root=catalog,
            storage_root=storage,
            manifest_hash=a.manifest_hash,
            location=location,
        )
        is VerificationOutcome.OK
    )
    # The successor is reported by the graph, and only by the graph.
    assert load_lineage_graph(catalog).successors_of(a.manifest_hash) == (b.manifest_hash,)


def test_publishing_lineage_does_not_touch_either_manifest_file(
    roots: tuple[Path, Path],
) -> None:
    from quant_research_terminal.catalog import manifest_path

    catalog, storage = roots
    a = register(catalog, storage, "a.parquet", rows(1))
    b = register(catalog, storage, "b.parquet", rows(2))
    before = {
        path: manifest_path(catalog, path).read_bytes()
        for path in (a.manifest_hash, b.manifest_hash)
    }
    publish_supersedes_relation(
        catalog_root=catalog, successor=b.manifest_hash, predecessors=[a.manifest_hash]
    )
    for manifest_hash, payload in before.items():
        assert manifest_path(catalog, manifest_hash).read_bytes() == payload


def test_registration_alone_creates_no_lineage(roots: tuple[Path, Path]) -> None:
    """Different semantic hashes prove nothing until someone declares a claim."""
    catalog, storage = roots
    a = register(catalog, storage, "a.parquet", rows(1))
    b = register(catalog, storage, "b.parquet", rows(2))
    assert a.semantic_hash != b.semantic_hash
    assert published_relations(catalog) == ()
    graph = load_lineage_graph(catalog)
    assert graph.successors_of(a.manifest_hash) == ()
    assert graph.predecessors_of(b.manifest_hash) == ()


# --------------------------------------------------------------------------
# Persistence, index, and rebuild
# --------------------------------------------------------------------------


def test_a_published_relation_file_is_never_overwritten(roots: tuple[Path, Path]) -> None:
    """Differing bytes under one relation hash is a typed refusal.

    Exercised through the publication primitive directly, because reaching it
    through :func:`publish_supersedes_relation` is impossible by construction:
    that function loads the graph first, so a tampered file is already reported
    as corruption — a stronger outcome, covered by
    :func:`test_a_malformed_relation_is_loud_not_skipped`.
    """
    from quant_research_terminal.catalog.store import publish_bytes_atomically

    catalog, storage = roots
    a = register(catalog, storage, "a.parquet", rows(1))
    b = register(catalog, storage, "b.parquet", rows(2))
    relation = publish_supersedes_relation(
        catalog_root=catalog, successor=b.manifest_hash, predecessors=[a.manifest_hash]
    )
    path = relation_path(catalog, relation.relation_hash)
    payload = relation_to_json_bytes(relation)

    # Republishing identical bytes is idempotent and rewrites nothing.
    assert publish_bytes_atomically(path, payload, conflict_error=RelationConflictError) is False
    with pytest.raises(RelationConflictError, match="never overwritten"):
        publish_bytes_atomically(path, payload + b" ", conflict_error=RelationConflictError)
    assert path.read_bytes() == payload


def test_a_malformed_relation_is_loud_not_skipped(roots: tuple[Path, Path]) -> None:
    """Silently omitting a relation would silently omit a correction."""
    catalog, storage = roots
    a = register(catalog, storage, "a.parquet", rows(1))
    b = register(catalog, storage, "b.parquet", rows(2))
    relation = publish_supersedes_relation(
        catalog_root=catalog, successor=b.manifest_hash, predecessors=[a.manifest_hash]
    )
    relation_path(catalog, relation.relation_hash).write_bytes(b"{not json")
    with pytest.raises(RelationHashMismatchError):
        published_relations(catalog)
    with pytest.raises(RelationHashMismatchError):
        load_lineage_graph(catalog)


def test_a_tampered_relation_is_refused(roots: tuple[Path, Path]) -> None:
    catalog, storage = roots
    a = register(catalog, storage, "a.parquet", rows(1))
    b = register(catalog, storage, "b.parquet", rows(2))
    c = register(catalog, storage, "c.parquet", rows(3))
    relation = publish_supersedes_relation(
        catalog_root=catalog, successor=b.manifest_hash, predecessors=[a.manifest_hash]
    )
    document = relation_to_json_bytes(relation).replace(
        a.manifest_hash.canonical().encode(), c.manifest_hash.canonical().encode()
    )
    relation_path(catalog, relation.relation_hash).write_bytes(document)
    with pytest.raises(RelationHashMismatchError):
        read_relation(catalog, relation.relation_hash)


def test_a_partial_relation_is_not_published_lineage(roots: tuple[Path, Path]) -> None:
    catalog, storage = roots
    a = register(catalog, storage, "a.parquet", rows(1))
    b = register(catalog, storage, "b.parquet", rows(2))
    relation = build_supersedes_relation(successor=b.manifest_hash, predecessors=[a.manifest_hash])
    directory = catalog / "lineage"
    directory.mkdir(parents=True)
    partial = directory / f"{relation.relation_hash.canonical()}.json.catalog-partial"
    partial.write_bytes(relation_to_json_bytes(relation))

    assert published_relations(catalog) == ()
    assert load_lineage_graph(catalog).relations == ()
    assert unpublished_relation_partials(catalog) == (partial,)


def test_losing_the_index_changes_no_navigation_answer(roots: tuple[Path, Path]) -> None:
    """The index is a cache, and this is what makes that claim true."""
    catalog, storage = roots
    a = register(catalog, storage, "a.parquet", rows(1))
    b = register(catalog, storage, "b.parquet", rows(2))
    c = register(catalog, storage, "c.parquet", rows(3))
    publish_supersedes_relation(
        catalog_root=catalog, successor=b.manifest_hash, predecessors=[a.manifest_hash]
    )
    publish_supersedes_relation(
        catalog_root=catalog, successor=c.manifest_hash, predecessors=[a.manifest_hash]
    )
    before = load_lineage_graph(catalog).successors_of(a.manifest_hash)

    lineage_index_path(catalog).unlink()
    assert load_lineage_index(catalog) == empty_lineage_index()
    assert load_lineage_graph(catalog).successors_of(a.manifest_hash) == before

    rebuilt = rebuild_lineage_index(catalog)
    assert rebuilt.entries != ()
    assert verify_lineage_index(catalog)


def test_a_rebuilt_index_is_byte_identical(roots: tuple[Path, Path]) -> None:
    catalog, storage = roots
    a = register(catalog, storage, "a.parquet", rows(1))
    b = register(catalog, storage, "b.parquet", rows(2))
    publish_supersedes_relation(
        catalog_root=catalog, successor=b.manifest_hash, predecessors=[a.manifest_hash]
    )
    original = load_lineage_index(catalog).to_json_bytes()
    lineage_index_path(catalog).unlink()
    assert rebuild_lineage_index(catalog).to_json_bytes() == original


def test_a_stale_index_is_reported_rather_than_believed(roots: tuple[Path, Path]) -> None:
    """The crash window between publication and the index write."""
    catalog, storage = roots
    a = register(catalog, storage, "a.parquet", rows(1))
    b = register(catalog, storage, "b.parquet", rows(2))
    publish_supersedes_relation(
        catalog_root=catalog, successor=b.manifest_hash, predecessors=[a.manifest_hash]
    )
    lineage_index_path(catalog).write_bytes(empty_lineage_index().to_json_bytes())

    assert not verify_lineage_index(catalog)
    # Navigation is unaffected, because it never reads the cache.
    assert load_lineage_graph(catalog).successors_of(a.manifest_hash) == (b.manifest_hash,)


def test_a_corrupt_index_is_typed(roots: tuple[Path, Path]) -> None:
    catalog, storage = roots
    register(catalog, storage, "a.parquet", rows(1))
    lineage_index_path(catalog).write_bytes(b"{not json")
    with pytest.raises(LineageIndexError, match="unreadable"):
        load_lineage_index(catalog)


def test_relation_identity_does_not_depend_on_the_catalog_root(
    tmp_path: Path,
) -> None:
    """Relocating a catalog cannot change what a claim is."""
    hashes = []
    for name in ("first", "second"):
        storage = tmp_path / name / "storage"
        catalog = tmp_path / name / "catalog"
        storage.mkdir(parents=True)
        a = register(catalog, storage, "a.parquet", rows(1))
        b = register(catalog, storage, "b.parquet", rows(2))
        hashes.append(
            publish_supersedes_relation(
                catalog_root=catalog, successor=b.manifest_hash, predecessors=[a.manifest_hash]
            ).relation_hash
        )
    assert hashes[0] == hashes[1]


def test_a_relation_is_recoverable_with_no_index_at_all(roots: tuple[Path, Path]) -> None:
    catalog, storage = roots
    a = register(catalog, storage, "a.parquet", rows(1))
    b = register(catalog, storage, "b.parquet", rows(2))
    relation = publish_supersedes_relation(
        catalog_root=catalog, successor=b.manifest_hash, predecessors=[a.manifest_hash]
    )
    lineage_index_path(catalog).unlink()
    assert read_relation(catalog, relation.relation_hash) == relation


# --------------------------------------------------------------------------
# Cross-process determinism
# --------------------------------------------------------------------------


_DETERMINISM_SCRIPT = """
from quant_research_terminal.domain.dataset_identity import ManifestHash
from quant_research_terminal.domain.dataset_lineage import build_supersedes_relation

for predecessors in (
    ["a" * 64],
    ["b" * 64, "a" * 64],
    ["a" * 64, "b" * 64],
    ["f" * 64, "0" * 64, "9" * 64],
):
    relation = build_supersedes_relation(
        successor=ManifestHash(value="c" * 64),
        predecessors=[ManifestHash(value=item) for item in predecessors],
    )
    print(relation.relation_hash.canonical())
"""


def test_relation_hashes_are_identical_across_hash_seeds_and_timezones() -> None:
    """A relation must hash the same on every machine, forever.

    The predecessor sets include the *same* set spelled in two orders, so a
    per-process iteration order leaking into the digest would show up as a
    difference both within one run's output and between runs.
    """
    outputs = set()
    for seed, zone in (
        ("0", "UTC"),
        ("1", "America/New_York"),
        ("42", "Asia/Tokyo"),
        ("random", "Australia/Eucla"),
    ):
        completed = subprocess.run(  # noqa: S603
            [sys.executable, "-c", _DETERMINISM_SCRIPT],
            capture_output=True,
            text=True,
            check=True,
            env={**os.environ, "PYTHONHASHSEED": seed, "TZ": zone},
        )
        outputs.add(completed.stdout)
    assert len(outputs) == 1

    lines = next(iter(outputs)).split()
    assert lines[1] == lines[2], "the same predecessor set in two orders must hash alike"
    assert len({*lines}) == 3, "distinct predecessor sets must hash apart"


# --------------------------------------------------------------------------
# Falsification pass 2 regressions
# --------------------------------------------------------------------------


def test_a_corrupt_index_makes_verify_return_false_not_raise(roots: tuple[Path, Path]) -> None:
    """A function whose whole contract is to return a bool must not throw.

    "The cache does not match the relations" is the truthful answer for a
    corrupt cache as much as for a stale one.
    """
    catalog, storage = roots
    a = register(catalog, storage, "a.parquet", rows(1))
    b = register(catalog, storage, "b.parquet", rows(2))
    publish_supersedes_relation(
        catalog_root=catalog, successor=b.manifest_hash, predecessors=[a.manifest_hash]
    )
    for corruption in (b"{not json", b'{"format":"qrt-lineage-index/1","entr'):
        lineage_index_path(catalog).write_bytes(corruption)
        assert verify_lineage_index(catalog) is False
    assert load_lineage_graph(catalog).successors_of(a.manifest_hash) == (b.manifest_hash,)


def test_an_unpublished_relation_is_missing_not_mismatched(roots: tuple[Path, Path]) -> None:
    """ "No such claim" and "the stored claim is corrupt" are different facts."""
    catalog, _ = roots
    with pytest.raises(MissingRelationError, match="no relation published"):
        read_relation(catalog, SupersedesRelationHash(value="0" * 64))


def test_an_index_entry_refuses_a_digest_subclass() -> None:
    """The class that gets serialized needs the guard the relation has."""
    from quant_research_terminal.catalog import LineageIndexEntry

    class Forged(SupersedesRelationHash):  # type: ignore[misc]
        def canonical(self) -> str:
            return "0" * 64

    with pytest.raises(TypeError, match="exactly"):
        LineageIndexEntry(
            manifest_hash=hash_of("a" * 64), relation_hashes=(Forged(value="b" * 64),)
        )


def test_a_preexisting_cycle_does_not_blame_the_next_relation(
    roots: tuple[Path, Path],
) -> None:
    """A corrupt graph must not send an operator to inspect an innocent claim.

    A cyclic graph is only reachable through concurrent publication, which is
    outside the assumed single-writer model — so it is written directly here to
    prove the diagnosis is right when it happens.
    """
    catalog, storage = roots
    a = register(catalog, storage, "a.parquet", rows(1))
    b = register(catalog, storage, "b.parquet", rows(2))
    c = register(catalog, storage, "c.parquet", rows(3))
    d = register(catalog, storage, "d.parquet", rows(4))

    directory = catalog / "lineage"
    directory.mkdir(parents=True, exist_ok=True)
    for successor, predecessor in ((b, a), (a, b)):
        relation = build_supersedes_relation(
            successor=successor.manifest_hash, predecessors=[predecessor.manifest_hash]
        )
        (directory / f"{relation.relation_hash.canonical()}.json").write_bytes(
            relation_to_json_bytes(relation)
        )

    reported = verify_lineage_acyclic(catalog)
    assert reported is not None

    with pytest.raises(LineageCycleError, match="already cyclic"):
        publish_supersedes_relation(
            catalog_root=catalog, successor=d.manifest_hash, predecessors=[c.manifest_hash]
        )


def test_an_acyclic_catalog_audits_clean(roots: tuple[Path, Path]) -> None:
    catalog, storage = roots
    a = register(catalog, storage, "a.parquet", rows(1))
    b = register(catalog, storage, "b.parquet", rows(2))
    publish_supersedes_relation(
        catalog_root=catalog, successor=b.manifest_hash, predecessors=[a.manifest_hash]
    )
    assert verify_lineage_acyclic(catalog) is None


def test_a_semantic_revert_is_permitted(roots: tuple[Path, Path]) -> None:
    """A -> B -> C where C restores A's data is a real operator action.

    The same-semantic refusal is pairwise and adjacent by design: it stops a
    claim that corrects *nothing*, not a chain that happens to return to an
    earlier state. Asserted so the behaviour is a decision rather than an
    accident of where the check sits.
    """
    catalog, storage = roots
    payload = rows(1)
    a = register(catalog, storage, "a.parquet", payload)
    b = register(catalog, storage, "b.parquet", rows(2))
    c = register(catalog, storage, "c.parquet", payload, provider_token="THETADATA")
    assert a.semantic_hash == c.semantic_hash

    publish_supersedes_relation(
        catalog_root=catalog, successor=b.manifest_hash, predecessors=[a.manifest_hash]
    )
    publish_supersedes_relation(
        catalog_root=catalog, successor=c.manifest_hash, predecessors=[b.manifest_hash]
    )
    assert verify_lineage_acyclic(catalog) is None
    assert load_lineage_graph(catalog).descendants_of(a.manifest_hash) == tuple(
        sorted((b.manifest_hash, c.manifest_hash), key=lambda item: item.canonical())
    )
