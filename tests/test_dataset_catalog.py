"""Manifests, registration, the catalog index, and v2→v3 migration.

The properties under attack:

* registration verifies rather than trusts;
* location is never identity — moving a file changes no hash;
* the index is derived state and can be thrown away;
* a replaced artifact is detected, and an *edited* one is distinguished from a
  merely re-encoded one;
* migration never infers a canonical identity from a vendor alias.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pyarrow.parquet as pq  # type: ignore[import-untyped]
import pytest
from dataset_fixtures import (
    ES_M2026,
    ES_U2026,
    manifest_for,
    quote,
    three_trades,
    trade,
)
from pydantic import ValidationError

from quant_research_terminal.catalog import (
    ArtifactOutsideStorageRootError,
    CatalogEntry,
    CatalogIndexError,
    CatalogLocationConflictError,
    DatasetManifest,
    DatasetOrigin,
    ManifestConflictError,
    ManifestHashMismatchError,
    MissingManifestError,
    PhysicalArtifactHashMismatchError,
    RegistrationOutcome,
    SchemaMismatchError,
    SemanticHashMismatchError,
    SourceProvenance,
    TransformationKind,
    TransformationProvenance,
    UnsupportedMigrationError,
    VerificationOutcome,
    build_manifest,
    empty_index,
    find_by_semantic_hash,
    load_index,
    locate_manifest,
    manifest_from_json_bytes,
    manifest_path,
    manifest_to_json_bytes,
    migrate_v2_to_v3,
    normalize_symbol_mapping,
    read_manifest,
    rebuild_index,
    register_dataset,
    verify_artifact_against_manifest,
    verify_manifest,
    verify_registration,
)
from quant_research_terminal.data.artifact_hash import physical_artifact_hash
from quant_research_terminal.data.parquet_store import (
    read_trades,
    write_quotes_v3,
    write_trades,
    write_trades_v3,
)
from quant_research_terminal.domain.dataset_identity import (
    ManifestHash,
    PhysicalArtifactHash,
    RecordType,
    semantic_dataset_hash,
)
from quant_research_terminal.domain.futures_contract import FuturesContractId
from quant_research_terminal.domain.models import Trade


@pytest.fixture
def roots(tmp_path: Path) -> tuple[Path, Path]:
    storage = tmp_path / "storage"
    catalog = tmp_path / "catalog"
    storage.mkdir()
    return catalog, storage


def write_artifact(
    storage: Path,
    name: str,
    records: list[Trade] | None = None,
    contract: FuturesContractId = ES_M2026,
) -> Path:
    path = storage / name
    path.parent.mkdir(parents=True, exist_ok=True)
    write_trades_v3(path, three_trades() if records is None else records, contract=contract)
    return path


# --------------------------------------------------------------------------
# Manifest identity
# --------------------------------------------------------------------------


def test_a_manifest_hash_describes_its_own_content(roots: tuple[Path, Path]) -> None:
    _, storage = roots
    artifact = write_artifact(storage, "a.parquet")
    manifest = manifest_for(artifact, three_trades())
    assert manifest_from_json_bytes(manifest_to_json_bytes(manifest)) == manifest


def test_a_manifest_carrying_a_wrong_hash_is_unconstructible(roots: tuple[Path, Path]) -> None:
    _, storage = roots
    artifact = write_artifact(storage, "a.parquet")
    good = manifest_for(artifact, three_trades())
    from quant_research_terminal.catalog.manifest import DatasetManifest
    from quant_research_terminal.domain.dataset_identity import ManifestHash

    with pytest.raises(ValidationError, match="does not describe"):
        DatasetManifest(**{**dict(good), "manifest_hash": ManifestHash(value="0" * 64)})


def test_manifest_model_copy_revalidates(roots: tuple[Path, Path]) -> None:
    _, storage = roots
    manifest = manifest_for(write_artifact(storage, "a.parquet"), three_trades())
    with pytest.raises(ValidationError):
        manifest.model_copy(update={"row_count": 99})


def test_a_manifest_has_no_location_field(roots: tuple[Path, Path]) -> None:
    """Structural, not merely tested: relocation cannot change identity."""
    _, storage = roots
    manifest = manifest_for(write_artifact(storage, "a.parquet"), three_trades())
    fields = set(type(manifest).model_fields)
    assert not fields & {"path", "location", "artifact_path", "directory", "filename"}


def test_a_manifest_has_no_identifier_timestamp_or_free_text_field(
    roots: tuple[Path, Path],
) -> None:
    """No clock, no random id, and no free-form prose.

    The prose half is asserted explicitly rather than left to intent: a text
    field on an immutable model invites exactly one question — does editing it
    change identity? — and the cheapest correct answer is for it not to exist.
    """
    _, storage = roots
    manifest = manifest_for(write_artifact(storage, "a.parquet"), three_trades())
    fields = {name.lower() for name in type(manifest).model_fields}
    forbidden = (
        "uuid",
        "created",
        "registered",
        "notes",
        "note",
        "comment",
        "description",
        "label",
        "title",
        "author",
    )
    assert not any(token in name for name in fields for token in forbidden)


def test_provenance_changes_the_manifest_hash_but_not_the_semantic_hash(
    roots: tuple[Path, Path],
) -> None:
    _, storage = roots
    artifact = write_artifact(storage, "a.parquet")
    first = manifest_for(artifact, three_trades(), provider_token="DATABENTO")
    second = manifest_for(artifact, three_trades(), provider_token="THETADATA")
    assert first.semantic_hash == second.semantic_hash
    assert first.physical_hash == second.physical_hash
    assert first.manifest_hash != second.manifest_hash


def test_an_empty_dataset_manifest_has_no_timestamps(roots: tuple[Path, Path]) -> None:
    _, storage = roots
    artifact = write_artifact(storage, "empty.parquet", records=[])
    manifest = manifest_for(artifact, [])
    assert manifest.row_count == 0
    assert manifest.min_timestamp is None and manifest.max_timestamp is None


def test_a_zero_row_manifest_claiming_timestamps_is_unconstructible(
    roots: tuple[Path, Path],
) -> None:
    _, storage = roots
    artifact = write_artifact(storage, "a.parquet")
    populated = manifest_for(artifact, three_trades())
    with pytest.raises(ValidationError, match="empty dataset"):
        build_manifest(
            semantic_hash=populated.semantic_hash,
            physical_hash=populated.physical_hash,
            schema_version=populated.schema_version,
            record_type=populated.record_type,
            contract=populated.contract,
            row_count=0,
            min_timestamp=populated.min_timestamp,
            max_timestamp=populated.max_timestamp,
            source=populated.source,
            transformation=populated.transformation,
        )


def test_a_manifest_refuses_a_digest_subclass(roots: tuple[Path, Path]) -> None:
    """``@final`` binds a type checker, not the interpreter.

    A subclass overriding ``canonical()`` would serialize one digest while the
    in-memory field held another — the ADR-009 D4 failure aimed at identity.
    """
    _, storage = roots
    honest = manifest_for(write_artifact(storage, "a.parquet"), three_trades())

    # mypy refuses the subclass, which is exactly the point: ``@final`` stops a
    # type checker and nothing else. The runtime needs its own guard, so the
    # attack has to be written past the checker to test it.
    class Forged(ManifestHash):  # type: ignore[misc]
        def canonical(self) -> str:
            return "0" * 64

    with pytest.raises(TypeError, match="exactly"):
        DatasetManifest(
            **{**dict(honest), "manifest_hash": Forged(value=honest.manifest_hash.value)}
        )


def test_a_manifest_refuses_a_schema_version_the_platform_never_defined(
    roots: tuple[Path, Path],
) -> None:
    """A manifest is constructible and hashable without ever being registered.

    A version checked only at the registration boundary is a claim that can be
    built, hashed, and published unchallenged.
    """
    _, storage = roots
    honest = manifest_for(write_artifact(storage, "a.parquet"), three_trades())
    with pytest.raises(ValidationError, match="schema version must be one of"):
        build_manifest(
            semantic_hash=honest.semantic_hash,
            physical_hash=honest.physical_hash,
            schema_version=-99,
            record_type=honest.record_type,
            contract=honest.contract,
            row_count=honest.row_count,
            min_timestamp=honest.min_timestamp,
            max_timestamp=honest.max_timestamp,
            source=honest.source,
            transformation=honest.transformation,
        )


def test_an_out_of_range_instant_in_a_manifest_document_is_typed(
    roots: tuple[Path, Path],
) -> None:
    """``OverflowError`` is an ``ArithmeticError``, not a ``ValueError``.

    Without naming it, an absurd microsecond count escaped the catalog's error
    taxonomy entirely — the same trap the storage layer documents.
    """
    _, storage = roots
    manifest = manifest_for(write_artifact(storage, "a.parquet"), three_trades())
    document = manifest_to_json_bytes(manifest)
    stored = str(int(manifest.min_timestamp.timestamp() * 1_000_000))  # type: ignore[union-attr]
    tampered = document.replace(
        f'"min_timestamp":{stored}'.encode(), b'"min_timestamp":' + b"1" * 40
    )
    assert tampered != document
    with pytest.raises(ManifestHashMismatchError):
        manifest_from_json_bytes(tampered)


def test_vendor_symbols_must_be_sorted_and_unique() -> None:
    with pytest.raises(ValidationError, match="sorted"):
        SourceProvenance(
            origin=DatasetOrigin.IMPORTED,
            provider_token=None,
            source_artifact_hash=None,
            vendor_symbols=("ESM6", "ES M6"),
        )


def test_a_provider_token_is_not_a_path() -> None:
    for hostile in ("/tmp/x", "C:\\data", "module.Class", "lower"):
        with pytest.raises(ValidationError):
            SourceProvenance(
                origin=DatasetOrigin.IMPORTED,
                provider_token=hostile,
                source_artifact_hash=None,
                vendor_symbols=(),
            )


# --------------------------------------------------------------------------
# Registration verifies rather than trusts
# --------------------------------------------------------------------------


def test_registration_succeeds_and_is_idempotent(roots: tuple[Path, Path]) -> None:
    catalog, storage = roots
    artifact = write_artifact(storage, "es/a.parquet")
    manifest = manifest_for(artifact, three_trades())

    def attempt() -> RegistrationOutcome:
        return register_dataset(
            catalog_root=catalog,
            storage_root=storage,
            artifact_path=artifact,
            manifest=manifest,
        )

    assert attempt() is RegistrationOutcome.REGISTERED
    assert attempt() is RegistrationOutcome.ALREADY_REGISTERED


def test_a_false_row_count_is_detected(roots: tuple[Path, Path]) -> None:
    catalog, storage = roots
    artifact = write_artifact(storage, "a.parquet")
    truthful = manifest_for(artifact, three_trades())
    liar = build_manifest(
        semantic_hash=truthful.semantic_hash,
        physical_hash=truthful.physical_hash,
        schema_version=truthful.schema_version,
        record_type=truthful.record_type,
        contract=truthful.contract,
        row_count=99,
        min_timestamp=truthful.min_timestamp,
        max_timestamp=truthful.max_timestamp,
        source=truthful.source,
        transformation=truthful.transformation,
    )
    with pytest.raises(SchemaMismatchError, match="rows"):
        verify_artifact_against_manifest(artifact, liar)


def test_a_false_semantic_hash_is_detected(roots: tuple[Path, Path]) -> None:
    catalog, storage = roots
    artifact = write_artifact(storage, "a.parquet")
    truthful = manifest_for(artifact, three_trades())
    other = manifest_for(artifact, [trade(offset=0, price="1.5")])
    liar = build_manifest(
        semantic_hash=other.semantic_hash,
        physical_hash=truthful.physical_hash,
        schema_version=truthful.schema_version,
        record_type=truthful.record_type,
        contract=truthful.contract,
        row_count=truthful.row_count,
        min_timestamp=truthful.min_timestamp,
        max_timestamp=truthful.max_timestamp,
        source=truthful.source,
        transformation=truthful.transformation,
    )
    with pytest.raises(SemanticHashMismatchError):
        verify_artifact_against_manifest(artifact, liar)


def test_a_false_physical_hash_is_detected(roots: tuple[Path, Path]) -> None:
    catalog, storage = roots
    artifact = write_artifact(storage, "a.parquet")
    other = write_artifact(storage, "b.parquet", records=[trade(price="7.5")])
    truthful = manifest_for(artifact, three_trades())
    liar = build_manifest(
        semantic_hash=truthful.semantic_hash,
        physical_hash=physical_artifact_hash(other),
        schema_version=truthful.schema_version,
        record_type=truthful.record_type,
        contract=truthful.contract,
        row_count=truthful.row_count,
        min_timestamp=truthful.min_timestamp,
        max_timestamp=truthful.max_timestamp,
        source=truthful.source,
        transformation=truthful.transformation,
    )
    with pytest.raises(PhysicalArtifactHashMismatchError):
        verify_artifact_against_manifest(artifact, liar)


def test_a_false_vendor_alias_summary_is_detected(roots: tuple[Path, Path]) -> None:
    catalog, storage = roots
    artifact = write_artifact(storage, "a.parquet")
    liar = manifest_for(artifact, three_trades(), vendor_symbols=("NOT_PRESENT",))
    with pytest.raises(SchemaMismatchError, match="vendor aliases"):
        verify_artifact_against_manifest(artifact, liar)


def test_an_artifact_for_another_contract_is_detected(roots: tuple[Path, Path]) -> None:
    catalog, storage = roots
    artifact = write_artifact(storage, "a.parquet", contract=ES_U2026)
    liar = manifest_for(artifact, three_trades(), contract=ES_M2026)
    with pytest.raises(SchemaMismatchError, match="holds"):
        verify_artifact_against_manifest(artifact, liar)


# --------------------------------------------------------------------------
# One semantic hash, many manifests
# --------------------------------------------------------------------------


def test_one_semantic_hash_may_have_several_manifests(roots: tuple[Path, Path]) -> None:
    """Same records, different provenance: not a collision, and both discoverable."""
    catalog, storage = roots
    first = write_artifact(storage, "a.parquet")
    second = write_artifact(storage, "b.parquet")
    manifest_a = manifest_for(first, three_trades(), provider_token="DATABENTO")
    manifest_b = manifest_for(second, three_trades(), provider_token="THETADATA")
    assert manifest_a.semantic_hash == manifest_b.semantic_hash

    register_dataset(
        catalog_root=catalog, storage_root=storage, artifact_path=first, manifest=manifest_a
    )
    register_dataset(
        catalog_root=catalog, storage_root=storage, artifact_path=second, manifest=manifest_b
    )

    found = find_by_semantic_hash(catalog, manifest_a.semantic_hash)
    assert len(found) == 2
    assert {manifest.manifest_hash for manifest in found} == {
        manifest_a.manifest_hash,
        manifest_b.manifest_hash,
    }


def test_lookup_by_semantic_hash_is_deterministically_ordered(roots: tuple[Path, Path]) -> None:
    catalog, storage = roots
    for name, provider in (("z.parquet", "A"), ("a.parquet", "B"), ("m.parquet", "C")):
        artifact = write_artifact(storage, name)
        register_dataset(
            catalog_root=catalog,
            storage_root=storage,
            artifact_path=artifact,
            manifest=manifest_for(artifact, three_trades(), provider_token=provider),
        )
    semantic = manifest_for(storage / "a.parquet", three_trades()).semantic_hash
    found = find_by_semantic_hash(catalog, semantic)
    hashes = [manifest.manifest_hash.canonical() for manifest in found]
    assert hashes == sorted(hashes)


def test_lookup_by_semantic_hash_survives_losing_the_index(roots: tuple[Path, Path]) -> None:
    """It reads published manifests, so the index is genuinely not authority."""
    catalog, storage = roots
    artifact = write_artifact(storage, "a.parquet")
    manifest = manifest_for(artifact, three_trades())
    register_dataset(
        catalog_root=catalog, storage_root=storage, artifact_path=artifact, manifest=manifest
    )
    (catalog / "index.json").unlink()
    assert find_by_semantic_hash(catalog, manifest.semantic_hash) == (manifest,)


def test_two_manifests_may_describe_one_artifact_at_one_location(
    roots: tuple[Path, Path],
) -> None:
    """Two providers' provenance for identical bytes is not a conflict.

    The index records what the file holds, not the claims made about it, so one
    location and two manifests coexist. Refusing this was an artifact of binding
    a manifest to a path, which nothing needs.
    """
    catalog, storage = roots
    artifact = write_artifact(storage, "a.parquet")
    first = manifest_for(artifact, three_trades(), provider_token="DATABENTO")
    second = manifest_for(artifact, three_trades(), provider_token="THETADATA")
    assert first.manifest_hash != second.manifest_hash
    assert first.physical_hash == second.physical_hash

    for manifest in (first, second):
        register_dataset(
            catalog_root=catalog,
            storage_root=storage,
            artifact_path=artifact,
            manifest=manifest,
        )

    assert len(load_index(catalog).entries) == 1
    for manifest in (first, second):
        assert (
            verify_manifest(
                catalog_root=catalog, storage_root=storage, manifest_hash=manifest.manifest_hash
            )
            is VerificationOutcome.OK
        )


def test_rebinding_a_location_to_different_bytes_is_refused(roots: tuple[Path, Path]) -> None:
    """The real protection: the storage writer overwrites by design.

    Accepting this would let the catalog quietly forget the previous bytes ever
    existed at that path.
    """
    catalog, storage = roots
    artifact = write_artifact(storage, "a.parquet")
    register_dataset(
        catalog_root=catalog,
        storage_root=storage,
        artifact_path=artifact,
        manifest=manifest_for(artifact, three_trades()),
    )

    replacement = [trade(offset=4, price="1111.00")]
    write_trades_v3(artifact, replacement, contract=ES_M2026)
    with pytest.raises(CatalogLocationConflictError, match="already holds bytes"):
        register_dataset(
            catalog_root=catalog,
            storage_root=storage,
            artifact_path=artifact,
            manifest=manifest_for(artifact, replacement),
        )


def test_publishing_different_bytes_under_one_manifest_hash_is_refused(
    roots: tuple[Path, Path],
) -> None:
    catalog, storage = roots
    artifact = write_artifact(storage, "a.parquet")
    manifest = manifest_for(artifact, three_trades())
    register_dataset(
        catalog_root=catalog, storage_root=storage, artifact_path=artifact, manifest=manifest
    )
    published = manifest_path(catalog, manifest.manifest_hash)
    published.write_bytes(manifest_to_json_bytes(manifest) + b" ")
    with pytest.raises(ManifestConflictError):
        register_dataset(
            catalog_root=catalog, storage_root=storage, artifact_path=artifact, manifest=manifest
        )


# --------------------------------------------------------------------------
# Location, relocation, rebuild
# --------------------------------------------------------------------------


def test_relocation_changes_no_hash(roots: tuple[Path, Path]) -> None:
    catalog, storage = roots
    artifact = write_artifact(storage, "es/a.parquet")
    manifest = manifest_for(artifact, three_trades())
    register_dataset(
        catalog_root=catalog, storage_root=storage, artifact_path=artifact, manifest=manifest
    )

    moved = storage / "archive" / "a.parquet"
    moved.parent.mkdir(parents=True)
    shutil.move(str(artifact), str(moved))

    assert physical_artifact_hash(moved) == manifest.physical_hash
    rebuilt = rebuild_index(catalog_root=catalog, storage_root=storage)
    assert [entry.location for entry in rebuilt.entries] == ["archive/a.parquet"]
    assert rebuilt.entries[0].physical_hash == manifest.physical_hash
    assert locate_manifest(catalog_root=catalog, storage_root=storage, manifest=manifest) == (
        moved,
    )


@pytest.mark.parametrize(
    "location", ["/abs.parquet", "C:/x.parquet", "a\\b.parquet", "../escape.parquet", ""]
)
def test_a_location_must_be_relative_and_contained(location: str) -> None:
    with pytest.raises(ValidationError):
        CatalogEntry(physical_hash=PhysicalArtifactHash(value="a" * 64), location=location)


def test_a_catalog_entry_records_bytes_and_a_path_and_nothing_else() -> None:
    """The index must not duplicate immutable manifest content.

    ``ManifestHash -> PhysicalArtifactHash`` is already a published fact inside
    the manifest; copying it into mutable state creates a duplicate that can rot,
    and it was what made a manifest look bound to one path.
    """
    assert set(CatalogEntry.model_fields) == {"physical_hash", "location"}


def test_the_index_can_be_discarded_and_rebuilt(roots: tuple[Path, Path]) -> None:
    catalog, storage = roots
    artifact = write_artifact(storage, "es/a.parquet")
    manifest = manifest_for(artifact, three_trades())
    register_dataset(
        catalog_root=catalog, storage_root=storage, artifact_path=artifact, manifest=manifest
    )
    original = load_index(catalog).to_json_bytes()

    (catalog / "index.json").unlink()
    assert load_index(catalog) == empty_index()

    rebuild_index(catalog_root=catalog, storage_root=storage)
    assert load_index(catalog).to_json_bytes() == original


def test_a_manifest_and_artifact_verify_with_no_index_at_all(roots: tuple[Path, Path]) -> None:
    """The index is discovery state; identity lives in the manifest."""
    catalog, storage = roots
    artifact = write_artifact(storage, "a.parquet")
    manifest = manifest_for(artifact, three_trades())
    register_dataset(
        catalog_root=catalog, storage_root=storage, artifact_path=artifact, manifest=manifest
    )
    (catalog / "index.json").unlink()

    recovered = read_manifest(catalog, manifest.manifest_hash)
    assert recovered == manifest
    verify_artifact_against_manifest(artifact, recovered)


def test_an_orphan_artifact_is_not_registered(roots: tuple[Path, Path]) -> None:
    catalog, storage = roots
    write_artifact(storage, "orphan.parquet")
    rebuilt = rebuild_index(catalog_root=catalog, storage_root=storage)
    assert rebuilt.entries == ()


def _register_two_byte_identical_copies(
    catalog: Path, storage: Path
) -> tuple[Path, Path, DatasetManifest, DatasetManifest]:
    """Register byte-identical artifacts under two different providers.

    One semantic hash, one physical hash, two manifests — the legitimate
    one-to-many case, and the one an earlier design could not rebuild.
    """
    first = write_artifact(storage, "a.parquet")
    second = storage / "b.parquet"
    shutil.copy2(first, second)
    assert physical_artifact_hash(first) == physical_artifact_hash(second)

    manifests = []
    for path, provider in ((first, "DATABENTO"), (second, "THETADATA")):
        manifest = manifest_for(path, three_trades(), provider_token=provider)
        register_dataset(
            catalog_root=catalog,
            storage_root=storage,
            artifact_path=path,
            manifest=manifest,
        )
        manifests.append(manifest)
    return first, second, manifests[0], manifests[1]


def test_a_rebuild_of_byte_identical_copies_is_idempotent(roots: tuple[Path, Path]) -> None:
    catalog, storage = roots
    _register_two_byte_identical_copies(catalog, storage)
    before = load_index(catalog).to_json_bytes()

    assert rebuild_index(catalog_root=catalog, storage_root=storage).to_json_bytes() == before
    assert load_index(catalog).to_json_bytes() == before


def test_both_manifests_recover_from_one_surviving_copy(roots: tuple[Path, Path]) -> None:
    """The case an earlier design called unrecoverable. It is not.

    Two manifests, one physical hash, the index deleted, and a **single**
    remaining artifact copy. Because a manifest already carries its own
    ``physical_hash``, the index only has to say where those bytes are — so both
    manifests resolve through the one surviving copy. No historical
    manifest→location pairing is reconstructed, because location was never
    provenance and there is nothing historical to recover.
    """
    catalog, storage = roots
    first, second, manifest_a, manifest_b = _register_two_byte_identical_copies(catalog, storage)
    assert manifest_a.manifest_hash != manifest_b.manifest_hash
    assert manifest_a.physical_hash == manifest_b.physical_hash
    before = (manifest_a.semantic_hash, manifest_a.physical_hash, manifest_a.manifest_hash)

    first.unlink()  # one copy survives, at b.parquet
    (catalog / "index.json").unlink()
    assert load_index(catalog) == empty_index()

    rebuilt = rebuild_index(catalog_root=catalog, storage_root=storage)
    assert [entry.location for entry in rebuilt.entries] == ["b.parquet"]

    for manifest in (manifest_a, manifest_b):
        assert find_by_semantic_hash(catalog, manifest.semantic_hash) == (
            *sorted((manifest_a, manifest_b), key=lambda m: m.manifest_hash.canonical()),
        )
        assert locate_manifest(catalog_root=catalog, storage_root=storage, manifest=manifest) == (
            second,
        )
        assert (
            verify_manifest(
                catalog_root=catalog, storage_root=storage, manifest_hash=manifest.manifest_hash
            )
            is VerificationOutcome.OK
        )
        verify_artifact_against_manifest(second, read_manifest(catalog, manifest.manifest_hash))

    # No immutable hash moved, and no provenance was invented.
    recovered = read_manifest(catalog, manifest_a.manifest_hash)
    assert (recovered.semantic_hash, recovered.physical_hash, recovered.manifest_hash) == before
    assert recovered == manifest_a
    assert read_manifest(catalog, manifest_b.manifest_hash) == manifest_b


def test_a_rebuild_drops_a_location_whose_bytes_no_longer_match(
    roots: tuple[Path, Path],
) -> None:
    """An entry states a present-tense fact, so a changed file is re-recorded."""
    catalog, storage = roots
    first = write_artifact(storage, "a.parquet")
    original = manifest_for(first, three_trades())
    register_dataset(
        catalog_root=catalog, storage_root=storage, artifact_path=first, manifest=original
    )
    replacement = [trade(offset=7, price="1234.50")]
    write_trades_v3(first, replacement, contract=ES_M2026)

    rebuilt = rebuild_index(catalog_root=catalog, storage_root=storage)
    assert rebuilt.entries == ()
    assert locate_manifest(catalog_root=catalog, storage_root=storage, manifest=original) == ()
    assert (
        verify_manifest(
            catalog_root=catalog, storage_root=storage, manifest_hash=original.manifest_hash
        )
        is VerificationOutcome.MISSING_ARTIFACT
    )


def test_a_corrupt_index_fails_before_anything_is_published(roots: tuple[Path, Path]) -> None:
    """A corrupt index must not report failure for a registered dataset.

    The index is read before the commit point, so a caller never sees a raised
    error for a manifest that was in fact published.
    """
    catalog, storage = roots
    artifact = write_artifact(storage, "a.parquet")
    manifest = manifest_for(artifact, three_trades())
    catalog.mkdir(parents=True)
    (catalog / "index.json").write_bytes(b"{not json")

    with pytest.raises(CatalogIndexError, match="unreadable"):
        register_dataset(
            catalog_root=catalog, storage_root=storage, artifact_path=artifact, manifest=manifest
        )
    assert not manifest_path(catalog, manifest.manifest_hash).exists()


def test_an_artifact_outside_the_storage_root_is_refused(
    roots: tuple[Path, Path], tmp_path: Path
) -> None:
    catalog, storage = roots
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    artifact = outside / "a.parquet"
    write_trades_v3(artifact, three_trades(), contract=ES_M2026)
    with pytest.raises(ArtifactOutsideStorageRootError, match="storage root"):
        register_dataset(
            catalog_root=catalog,
            storage_root=storage,
            artifact_path=artifact,
            manifest=manifest_for(artifact, three_trades()),
        )


def test_an_unpublished_partial_manifest_is_not_registered(roots: tuple[Path, Path]) -> None:
    catalog, storage = roots
    artifact = write_artifact(storage, "a.parquet")
    manifest = manifest_for(artifact, three_trades())
    directory = catalog / "manifests"
    directory.mkdir(parents=True)
    (directory / f"{manifest.manifest_hash.canonical()}.json.catalog-partial").write_bytes(
        manifest_to_json_bytes(manifest)
    )
    rebuilt = rebuild_index(catalog_root=catalog, storage_root=storage)
    assert rebuilt.entries == ()
    with pytest.raises(MissingManifestError):
        read_manifest(catalog, manifest.manifest_hash)


# --------------------------------------------------------------------------
# Verification after registration
# --------------------------------------------------------------------------


def test_a_reencode_reports_physical_mismatch_not_semantic(roots: tuple[Path, Path]) -> None:
    """The distinguishing half of the phase's headline claim.

    Re-encoding really re-encodes here — different codec, different row-group
    size — rather than writing different data and calling it a re-encode.
    """
    catalog, storage = roots
    artifact = write_artifact(storage, "a.parquet")
    manifest = manifest_for(artifact, three_trades())
    register_dataset(
        catalog_root=catalog, storage_root=storage, artifact_path=artifact, manifest=manifest
    )
    location = load_index(catalog).entries[0].location

    table = pq.read_table(artifact)
    pq.write_table(table, artifact, compression="gzip", row_group_size=1)
    assert physical_artifact_hash(artifact) != manifest.physical_hash

    assert (
        verify_registration(
            catalog_root=catalog,
            storage_root=storage,
            manifest_hash=manifest.manifest_hash,
            location=location,
        )
        is VerificationOutcome.PHYSICAL_MISMATCH
    )


def test_verification_reports_an_edit_and_a_missing_artifact(roots: tuple[Path, Path]) -> None:
    catalog, storage = roots
    artifact = write_artifact(storage, "a.parquet")
    manifest = manifest_for(artifact, three_trades())
    register_dataset(
        catalog_root=catalog, storage_root=storage, artifact_path=artifact, manifest=manifest
    )
    location = load_index(catalog).entries[0].location
    assert (
        verify_registration(
            catalog_root=catalog,
            storage_root=storage,
            manifest_hash=manifest.manifest_hash,
            location=location,
        )
        is VerificationOutcome.OK
    )

    write_trades_v3(artifact, [trade(price="9999.00")], contract=ES_M2026)
    assert (
        verify_registration(
            catalog_root=catalog,
            storage_root=storage,
            manifest_hash=manifest.manifest_hash,
            location=location,
        )
        is VerificationOutcome.SEMANTIC_MISMATCH
    )

    artifact.unlink()
    assert (
        verify_registration(
            catalog_root=catalog,
            storage_root=storage,
            manifest_hash=manifest.manifest_hash,
            location=location,
        )
        is VerificationOutcome.MISSING_ARTIFACT
    )


def test_a_deleted_manifest_reports_missing_manifest(roots: tuple[Path, Path]) -> None:
    catalog, storage = roots
    artifact = write_artifact(storage, "a.parquet")
    manifest = manifest_for(artifact, three_trades())
    register_dataset(
        catalog_root=catalog, storage_root=storage, artifact_path=artifact, manifest=manifest
    )
    location = load_index(catalog).entries[0].location
    manifest_path(catalog, manifest.manifest_hash).unlink()
    assert (
        verify_registration(
            catalog_root=catalog,
            storage_root=storage,
            manifest_hash=manifest.manifest_hash,
            location=location,
        )
        is VerificationOutcome.MISSING_MANIFEST
    )


def test_a_record_type_swap_is_reported_and_never_raises(roots: tuple[Path, Path]) -> None:
    """``verify_registration`` returns an outcome; it does not raise.

    Overwriting a trade artifact with a quote artifact for the same contract
    used to escape the function entirely as a ``SemanticEncodingError``, because
    that class descends from ``Exception`` rather than ``ValueError``.
    """
    catalog, storage = roots
    artifact = write_artifact(storage, "a.parquet")
    manifest = manifest_for(artifact, three_trades())
    register_dataset(
        catalog_root=catalog, storage_root=storage, artifact_path=artifact, manifest=manifest
    )
    location = load_index(catalog).entries[0].location

    write_quotes_v3(artifact, [quote(offset=0)], contract=ES_M2026)
    assert (
        verify_registration(
            catalog_root=catalog,
            storage_root=storage,
            manifest_hash=manifest.manifest_hash,
            location=location,
        )
        is VerificationOutcome.SEMANTIC_MISMATCH
    )


def test_a_manifest_claiming_the_wrong_record_type_is_refused(roots: tuple[Path, Path]) -> None:
    catalog, storage = roots
    artifact = write_artifact(storage, "a.parquet")
    honest = manifest_for(artifact, three_trades())
    lying = build_manifest(
        semantic_hash=honest.semantic_hash,
        physical_hash=honest.physical_hash,
        schema_version=honest.schema_version,
        record_type=RecordType.QUOTE,
        contract=honest.contract,
        row_count=honest.row_count,
        min_timestamp=honest.min_timestamp,
        max_timestamp=honest.max_timestamp,
        source=honest.source,
        transformation=honest.transformation,
    )
    with pytest.raises(SchemaMismatchError, match="trade records"):
        verify_artifact_against_manifest(artifact, lying)


def test_an_empty_artifact_still_refutes_a_wrong_record_type(roots: tuple[Path, Path]) -> None:
    """The case the schema metadata exists for, and the one that was missing.

    With no rows there is nothing to contradict a false claim, so the declared
    record type in the metadata is the only evidence in the file. Ignoring it
    let a manifest claiming "quote" verify — and register — against an empty
    trade artifact.
    """
    catalog, storage = roots
    artifact = write_artifact(storage, "empty.parquet", records=[])
    honest = manifest_for(artifact, [])
    lying = build_manifest(
        semantic_hash=semantic_dataset_hash(
            record_type=RecordType.QUOTE, contract=ES_M2026, records=[]
        ),
        physical_hash=honest.physical_hash,
        schema_version=honest.schema_version,
        record_type=RecordType.QUOTE,
        contract=honest.contract,
        row_count=0,
        min_timestamp=None,
        max_timestamp=None,
        source=honest.source,
        transformation=honest.transformation,
    )
    with pytest.raises(SchemaMismatchError, match="claims quote"):
        verify_artifact_against_manifest(artifact, lying)
    with pytest.raises(SchemaMismatchError):
        register_dataset(
            catalog_root=catalog, storage_root=storage, artifact_path=artifact, manifest=lying
        )
    assert load_index(catalog) == empty_index()


def test_a_corrupted_artifact_is_detected(roots: tuple[Path, Path]) -> None:
    catalog, storage = roots
    artifact = write_artifact(storage, "a.parquet")
    manifest = manifest_for(artifact, three_trades())
    artifact.write_bytes(b"not parquet at all")
    with pytest.raises(PhysicalArtifactHashMismatchError):
        verify_artifact_against_manifest(artifact, manifest)


# --------------------------------------------------------------------------
# Migration
# --------------------------------------------------------------------------


def test_several_aliases_may_map_to_one_contract(roots: tuple[Path, Path]) -> None:
    catalog, storage = roots
    source = storage / "legacy.parquet"
    rows = [trade(offset=0, symbol="ESM6"), trade(offset=1, symbol="ES M6")]
    write_trades(source, rows)
    before = physical_artifact_hash(source)

    destination = storage / "migrated.parquet"
    manifest = migrate_v2_to_v3(
        source=source,
        destination=destination,
        record_type=RecordType.TRADE,
        contract=ES_M2026,
        mapping={"ESM6": ES_M2026, "ES M6": ES_M2026},
    )
    assert manifest.row_count == 2
    assert manifest.source.vendor_symbols == ("ES M6", "ESM6")
    assert manifest.source.origin is DatasetOrigin.MIGRATED_FROM_V2
    assert manifest.source.source_artifact_hash == before
    assert physical_artifact_hash(source) == before  # input untouched
    verify_artifact_against_manifest(destination, manifest)


def test_migrating_onto_the_source_is_refused(roots: tuple[Path, Path]) -> None:
    """ "Opened read-only; never modified" has to be true of every call.

    Writing the output over the input destroyed the version-2 artifact and left
    the recorded source hash describing the migration's own result.
    """
    catalog, storage = roots
    source = storage / "legacy.parquet"
    write_trades(source, [trade(symbol="ESM6")])
    before = physical_artifact_hash(source)

    with pytest.raises(UnsupportedMigrationError, match="never overwrites its input"):
        migrate_v2_to_v3(
            source=source,
            destination=source,
            record_type=RecordType.TRADE,
            contract=ES_M2026,
            mapping={"ESM6": ES_M2026},
        )
    assert physical_artifact_hash(source) == before
    assert read_trades(source) == (trade(symbol="ESM6"),)


def test_migrating_onto_the_source_by_an_equivalent_path_is_refused(
    roots: tuple[Path, Path],
) -> None:
    """A different spelling of one file is still that file."""
    catalog, storage = roots
    source = storage / "legacy.parquet"
    write_trades(source, [trade(symbol="ESM6")])
    with pytest.raises(UnsupportedMigrationError, match="never overwrites its input"):
        migrate_v2_to_v3(
            source=source,
            destination=storage / "." / "legacy.parquet",
            record_type=RecordType.TRADE,
            contract=ES_M2026,
            mapping={"ESM6": ES_M2026},
        )


def test_an_unmapped_alias_is_never_inferred(roots: tuple[Path, Path]) -> None:
    catalog, storage = roots
    source = storage / "legacy.parquet"
    write_trades(source, [trade(symbol="ESM6")])
    with pytest.raises(UnsupportedMigrationError, match="no explicit mapping"):
        migrate_v2_to_v3(
            source=source,
            destination=storage / "out.parquet",
            record_type=RecordType.TRADE,
            contract=ES_M2026,
            mapping={},
        )


def test_a_source_spanning_two_contracts_is_refused(roots: tuple[Path, Path]) -> None:
    catalog, storage = roots
    source = storage / "legacy.parquet"
    write_trades(source, [trade(offset=0, symbol="ESM6"), trade(offset=1, symbol="ESU6")])
    with pytest.raises(UnsupportedMigrationError, match="more than one contract"):
        migrate_v2_to_v3(
            source=source,
            destination=storage / "out.parquet",
            record_type=RecordType.TRADE,
            contract=ES_M2026,
            mapping={"ESM6": ES_M2026, "ESU6": ES_U2026},
        )


def test_unused_mapping_entries_are_refused_by_default(roots: tuple[Path, Path]) -> None:
    catalog, storage = roots
    source = storage / "legacy.parquet"
    write_trades(source, [trade(symbol="ESM6")])
    with pytest.raises(UnsupportedMigrationError, match="not present"):
        migrate_v2_to_v3(
            source=source,
            destination=storage / "out.parquet",
            record_type=RecordType.TRADE,
            contract=ES_M2026,
            mapping={"ESM6": ES_M2026, "NEVER": ES_M2026},
        )


def test_an_unused_mapping_entry_changes_only_the_manifest_hash(roots: tuple[Path, Path]) -> None:
    """The cleanest demonstration that the three identities are not one."""
    catalog, storage = roots
    source = storage / "legacy.parquet"
    write_trades(source, [trade(symbol="ESM6")])

    lean = migrate_v2_to_v3(
        source=source,
        destination=storage / "lean.parquet",
        record_type=RecordType.TRADE,
        contract=ES_M2026,
        mapping={"ESM6": ES_M2026},
    )
    rich = migrate_v2_to_v3(
        source=source,
        destination=storage / "rich.parquet",
        record_type=RecordType.TRADE,
        contract=ES_M2026,
        mapping={"ESM6": ES_M2026, "NEVER_SEEN": ES_U2026},
        allow_unused=True,
    )
    assert lean.semantic_hash == rich.semantic_hash
    assert lean.physical_hash == rich.physical_hash
    assert lean.manifest_hash != rich.manifest_hash


def test_a_mapping_hash_ignores_insertion_order() -> None:
    forward = normalize_symbol_mapping({"ESM6": ES_M2026, "ES M6": ES_M2026})
    backward = normalize_symbol_mapping({"ES M6": ES_M2026, "ESM6": ES_M2026})
    assert forward == backward
    assert (
        TransformationProvenance(
            kind=TransformationKind.SYMBOL_MAPPING,
            symbol_mapping=forward,
            source_schema_version=2,
        ).canonical_payload()
        == TransformationProvenance(
            kind=TransformationKind.SYMBOL_MAPPING,
            symbol_mapping=backward,
            source_schema_version=2,
        ).canonical_payload()
    )


def test_a_mapping_target_must_be_a_listed_contract() -> None:
    from quant_research_terminal.domain.continuous_series import ContinuousSeriesId
    from quant_research_terminal.domain.futures_contract import FuturesProduct, Venue

    series = ContinuousSeriesId(
        product=FuturesProduct(venue=Venue(code="CME"), root="ES"), token="ACTIVE"
    )
    with pytest.raises(UnsupportedMigrationError, match="not a listed contract"):
        normalize_symbol_mapping({"ES1!": series})  # type: ignore[dict-item]


def test_migrating_an_empty_v2_artifact(roots: tuple[Path, Path]) -> None:
    catalog, storage = roots
    source = storage / "legacy.parquet"
    write_trades(source, [])
    manifest = migrate_v2_to_v3(
        source=source,
        destination=storage / "out.parquet",
        record_type=RecordType.TRADE,
        contract=ES_M2026,
        mapping={},
    )
    assert manifest.row_count == 0
    assert manifest.source.vendor_symbols == ()


def test_no_calendar_or_rollover_pin_is_attached(roots: tuple[Path, Path]) -> None:
    """Raw listed-contract data depends on neither, so neither is recorded."""
    _, storage = roots
    manifest = manifest_for(write_artifact(storage, "a.parquet"), three_trades())
    fields = {name.lower() for name in type(manifest).model_fields}
    assert not any(
        token in name
        for name in fields
        for token in ("calendar", "roll", "continuous", "series", "session")
    )
