"""Resolving and verifying replay sources, and refusing ambiguous input sets.

The properties under attack:

* nothing is replayed that has not been verified, and nothing is emitted at all
  if any source fails — there is no partial replay;
* a source is resolved through *any* byte-identical copy the catalog knows of,
  not through the first one it happens to list;
* the persisted row order is never repaired, and an out-of-order source is
  refused over its whole extent rather than over the requested window;
* two manifests naming one dataset, and two histories of one stream, are both
  refused rather than silently merged;
* canonical identity comes from the version-3 artifact, never from a vendor
  alias, and a version-2 artifact is refused rather than reinterpreted.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from dataset_fixtures import ES_M2026, ES_U2026, NQ_M2026
from replay_fixtures import (
    at,
    bar_starting,
    publish_dataset,
    publish_legacy_v2_manifest,
    quote_at,
    trade_at,
    write_artifact,
)

from quant_research_terminal.catalog.errors import MissingManifestError
from quant_research_terminal.catalog.store import rebuild_index
from quant_research_terminal.domain.dataset_identity import ManifestHash, RecordType
from quant_research_terminal.domain.replay import ReplayConfig, ReplayFrame, ReplayRange
from quant_research_terminal.replay import (
    AmbiguousReplayOverlapError,
    DuplicateReplaySemanticDatasetError,
    NonMonotonicReplaySourceError,
    PreparedReplay,
    ReplayArtifactVerificationError,
    UnsupportedReplaySchemaError,
    prepare_replay,
)


@pytest.fixture
def roots(tmp_path: Path) -> tuple[Path, Path]:
    storage = tmp_path / "storage"
    catalog = tmp_path / "catalog"
    storage.mkdir()
    return catalog, storage


def prepared(
    roots: tuple[Path, Path], *hashes: ManifestHash, replay_range: ReplayRange | None = None
) -> PreparedReplay:
    catalog, storage = roots
    return prepare_replay(
        catalog_root=catalog,
        storage_root=storage,
        config=ReplayConfig(manifest_hashes=hashes, replay_range=replay_range),
    )


def frames_of(
    roots: tuple[Path, Path], *hashes: ManifestHash, replay_range: ReplayRange | None = None
) -> list[ReplayFrame]:
    catalog, storage = roots
    replay = prepare_replay(
        catalog_root=catalog,
        storage_root=storage,
        config=ReplayConfig(manifest_hashes=hashes, replay_range=replay_range),
    )
    return list(replay.frames())


# --------------------------------------------------------------------------
# One valid source of each record type
# --------------------------------------------------------------------------


def test_a_trade_source_prepares_and_replays(roots: tuple[Path, Path]) -> None:
    catalog, storage = roots
    manifest = publish_dataset(
        catalog_root=catalog,
        storage_root=storage,
        name="trades.parquet",
        records=[trade_at(at(seconds=0)), trade_at(at(seconds=1))],
        record_type=RecordType.TRADE,
        contract=ES_M2026,
    )

    frames = frames_of(roots, manifest.manifest_hash)

    assert [frame.availability_time for frame in frames] == [at(seconds=0), at(seconds=1)]
    assert all(frame.events[0].record_type is RecordType.TRADE for frame in frames)


def test_a_quote_source_prepares_and_replays(roots: tuple[Path, Path]) -> None:
    catalog, storage = roots
    manifest = publish_dataset(
        catalog_root=catalog,
        storage_root=storage,
        name="quotes.parquet",
        records=[quote_at(at(seconds=0))],
        record_type=RecordType.QUOTE,
        contract=ES_M2026,
    )

    frames = frames_of(roots, manifest.manifest_hash)

    assert len(frames) == 1
    assert frames[0].events[0].record_type is RecordType.QUOTE


def test_a_bar_source_prepares_and_replays_at_interval_close(roots: tuple[Path, Path]) -> None:
    catalog, storage = roots
    manifest = publish_dataset(
        catalog_root=catalog,
        storage_root=storage,
        name="bars.parquet",
        records=[bar_starting(at(seconds=0))],
        record_type=RecordType.BAR,
        contract=ES_M2026,
    )

    frames = frames_of(roots, manifest.manifest_hash)

    assert [frame.availability_time for frame in frames] == [at(minutes=1)]


def test_an_empty_source_is_valid_and_contributes_nothing(roots: tuple[Path, Path]) -> None:
    catalog, storage = roots
    manifest = publish_dataset(
        catalog_root=catalog,
        storage_root=storage,
        name="empty.parquet",
        records=[],
        record_type=RecordType.TRADE,
        contract=ES_M2026,
    )

    assert frames_of(roots, manifest.manifest_hash) == []


def test_the_contract_comes_from_the_artifact_not_the_vendor_alias(
    roots: tuple[Path, Path],
) -> None:
    """The rows spell the instrument ``NQZ9``; the artifact says ``CME:ES:M2026``."""
    catalog, storage = roots
    manifest = publish_dataset(
        catalog_root=catalog,
        storage_root=storage,
        name="aliased.parquet",
        records=[trade_at(at(seconds=0), symbol="NQZ9")],
        record_type=RecordType.TRADE,
        contract=ES_M2026,
    )

    event = frames_of(roots, manifest.manifest_hash)[0].events[0]

    assert event.contract == ES_M2026
    assert event.payload.instrument_symbol == "NQZ9"


# --------------------------------------------------------------------------
# Failures resolved before any frame exists
# --------------------------------------------------------------------------


def test_an_unpublished_manifest_is_a_missing_manifest(roots: tuple[Path, Path]) -> None:
    with pytest.raises(MissingManifestError):
        prepared(roots, ManifestHash(value="a" * 64))


def test_an_artifact_the_catalog_cannot_locate_is_refused(roots: tuple[Path, Path]) -> None:
    catalog, storage = roots
    manifest = publish_dataset(
        catalog_root=catalog,
        storage_root=storage,
        name="gone.parquet",
        records=[trade_at(at(seconds=0))],
        record_type=RecordType.TRADE,
        contract=ES_M2026,
    )
    (storage / "gone.parquet").unlink()

    with pytest.raises(ReplayArtifactVerificationError):
        prepared(roots, manifest.manifest_hash)


def test_a_corrupt_artifact_is_refused(roots: tuple[Path, Path]) -> None:
    catalog, storage = roots
    manifest = publish_dataset(
        catalog_root=catalog,
        storage_root=storage,
        name="corrupt.parquet",
        records=[trade_at(at(seconds=0))],
        record_type=RecordType.TRADE,
        contract=ES_M2026,
    )
    (storage / "corrupt.parquet").write_bytes(b"not a parquet file")

    with pytest.raises(ReplayArtifactVerificationError):
        prepared(roots, manifest.manifest_hash)


def test_a_replaced_artifact_is_refused_even_though_it_is_a_valid_dataset(
    roots: tuple[Path, Path],
) -> None:
    """Readable, well-formed, and not the dataset the manifest describes."""
    catalog, storage = roots
    manifest = publish_dataset(
        catalog_root=catalog,
        storage_root=storage,
        name="swapped.parquet",
        records=[trade_at(at(seconds=0))],
        record_type=RecordType.TRADE,
        contract=ES_M2026,
    )
    write_artifact(
        storage / "swapped.parquet",
        [trade_at(at(seconds=0), price="9999.00")],
        record_type=RecordType.TRADE,
        contract=ES_M2026,
    )

    with pytest.raises(ReplayArtifactVerificationError):
        prepared(roots, manifest.manifest_hash)


def test_no_frame_is_emitted_before_a_later_source_is_found_broken(
    roots: tuple[Path, Path],
) -> None:
    """The whole no-partial-replay property, in one test.

    ``prepare_replay`` must fail rather than return an iterator that yields the
    good dataset's observations and then raises. A consumer that received those
    frames would have consumed a prefix of a run that can never complete.
    """
    catalog, storage = roots
    good = publish_dataset(
        catalog_root=catalog,
        storage_root=storage,
        name="good.parquet",
        records=[trade_at(at(seconds=0))],
        record_type=RecordType.TRADE,
        contract=ES_M2026,
    )
    broken = publish_dataset(
        catalog_root=catalog,
        storage_root=storage,
        name="broken.parquet",
        records=[quote_at(at(seconds=5))],
        record_type=RecordType.QUOTE,
        contract=ES_M2026,
    )
    (storage / "broken.parquet").write_bytes(b"wrecked")

    with pytest.raises(ReplayArtifactVerificationError):
        prepared(roots, good.manifest_hash, broken.manifest_hash)


def test_a_schema_version_two_source_is_refused_rather_than_reinterpreted(
    roots: tuple[Path, Path],
) -> None:
    catalog, storage = roots
    records = [trade_at(at(seconds=0))]
    artifact = write_artifact(
        storage / "legacy.parquet", records, record_type=RecordType.TRADE, contract=ES_M2026
    )
    manifest = publish_legacy_v2_manifest(
        catalog_root=catalog, artifact=artifact, records=records, contract=ES_M2026
    )

    with pytest.raises(UnsupportedReplaySchemaError, match="version 2"):
        prepared(roots, manifest.manifest_hash)


# --------------------------------------------------------------------------
# Multiple artifact locations
# --------------------------------------------------------------------------


def test_a_stale_first_location_does_not_hide_a_valid_second_copy(
    roots: tuple[Path, Path],
) -> None:
    """Phase 2.4's defect, reproduced against replay before it could recur.

    The index is the one part of the catalog explicitly allowed to be wrong. A
    single stale entry beside a perfectly good byte-identical copy must not turn
    an answerable replay into a hard failure.
    """
    catalog, storage = roots
    manifest = publish_dataset(
        catalog_root=catalog,
        storage_root=storage,
        name="stale.parquet",
        records=[trade_at(at(seconds=0)), trade_at(at(seconds=1))],
        record_type=RecordType.TRADE,
        contract=ES_M2026,
    )
    shutil.copy2(storage / "stale.parquet", storage / "valid.parquet")
    index = rebuild_index(catalog_root=catalog, storage_root=storage)
    assert index.find_locations(manifest.physical_hash) == ("stale.parquet", "valid.parquet")

    # Break the first-listed copy without refreshing the index.
    write_artifact(
        storage / "stale.parquet",
        [trade_at(at(seconds=99), price="1.00")],
        record_type=RecordType.TRADE,
        contract=ES_M2026,
    )

    frames = frames_of(roots, manifest.manifest_hash)

    assert [frame.availability_time for frame in frames] == [at(seconds=0), at(seconds=1)]


def test_relocating_an_artifact_does_not_change_the_replay(roots: tuple[Path, Path]) -> None:
    catalog, storage = roots
    manifest = publish_dataset(
        catalog_root=catalog,
        storage_root=storage,
        name="here.parquet",
        records=[trade_at(at(seconds=0)), trade_at(at(seconds=2))],
        record_type=RecordType.TRADE,
        contract=ES_M2026,
    )
    before = [frame.availability_time for frame in frames_of(roots, manifest.manifest_hash)]

    (storage / "moved").mkdir()
    shutil.move(storage / "here.parquet", storage / "moved" / "there.parquet")
    rebuild_index(catalog_root=catalog, storage_root=storage)

    after = [frame.availability_time for frame in frames_of(roots, manifest.manifest_hash)]

    assert before == after


# --------------------------------------------------------------------------
# Source monotonicity
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("record_type", "records"),
    [
        (
            RecordType.TRADE,
            [trade_at(at(seconds=0)), trade_at(at(seconds=2)), trade_at(at(seconds=1))],
        ),
        (
            RecordType.QUOTE,
            [quote_at(at(seconds=0)), quote_at(at(seconds=2)), quote_at(at(seconds=1))],
        ),
        (
            RecordType.BAR,
            [bar_starting(at(seconds=0)), bar_starting(at(minutes=2)), bar_starting(at(minutes=1))],
        ),
    ],
)
def test_a_source_whose_availability_decreases_is_refused(
    roots: tuple[Path, Path], record_type: RecordType, records: list[object]
) -> None:
    catalog, storage = roots
    manifest = publish_dataset(
        catalog_root=catalog,
        storage_root=storage,
        name="unsorted.parquet",
        records=records,  # type: ignore[arg-type]
        record_type=record_type,
        contract=ES_M2026,
    )

    with pytest.raises(NonMonotonicReplaySourceError, match="never sorted"):
        prepared(roots, manifest.manifest_hash)


def test_equal_consecutive_availability_times_are_accepted(roots: tuple[Path, Path]) -> None:
    catalog, storage = roots
    manifest = publish_dataset(
        catalog_root=catalog,
        storage_root=storage,
        name="simultaneous.parquet",
        records=[trade_at(at(seconds=0)), trade_at(at(seconds=0), price="4500.50")],
        record_type=RecordType.TRADE,
        contract=ES_M2026,
    )

    frames = frames_of(roots, manifest.manifest_hash)

    assert len(frames) == 1
    assert len(frames[0].events) == 2


def test_a_source_is_validated_outside_the_requested_range(roots: tuple[Path, Path]) -> None:
    """Whether a dataset is a valid replay source cannot depend on who is asking.

    The disorder sits entirely after the requested window. The source is still
    refused, so the same dataset is never acceptable to one run and unacceptable
    to another.
    """
    catalog, storage = roots
    manifest = publish_dataset(
        catalog_root=catalog,
        storage_root=storage,
        name="late-disorder.parquet",
        records=[
            trade_at(at(seconds=0)),
            trade_at(at(seconds=1)),
            trade_at(at(seconds=50)),
            trade_at(at(seconds=40)),
        ],
        record_type=RecordType.TRADE,
        contract=ES_M2026,
    )

    with pytest.raises(NonMonotonicReplaySourceError):
        prepared(
            roots,
            manifest.manifest_hash,
            replay_range=ReplayRange(start=at(seconds=0), end=at(seconds=2)),
        )


# --------------------------------------------------------------------------
# Duplicate semantic datasets
# --------------------------------------------------------------------------


def test_two_manifests_pinning_one_dataset_are_refused(roots: tuple[Path, Path]) -> None:
    catalog, storage = roots
    records = [trade_at(at(seconds=0)), trade_at(at(seconds=1))]
    first = publish_dataset(
        catalog_root=catalog,
        storage_root=storage,
        name="databento.parquet",
        records=records,
        record_type=RecordType.TRADE,
        contract=ES_M2026,
        provider_token="DATABENTO",
    )
    second = publish_dataset(
        catalog_root=catalog,
        storage_root=storage,
        name="thetadata.parquet",
        records=records,
        record_type=RecordType.TRADE,
        contract=ES_M2026,
        provider_token="THETADATA",
    )
    assert first.manifest_hash != second.manifest_hash
    assert first.semantic_hash == second.semantic_hash

    with pytest.raises(DuplicateReplaySemanticDatasetError, match="twice"):
        prepared(roots, first.manifest_hash, second.manifest_hash)


def test_either_of_two_provenance_variants_replays_identically_on_its_own(
    roots: tuple[Path, Path],
) -> None:
    """Provenance changes the envelope, never the market observations."""
    catalog, storage = roots
    records = [trade_at(at(seconds=0)), trade_at(at(seconds=1))]
    first = publish_dataset(
        catalog_root=catalog,
        storage_root=storage,
        name="a.parquet",
        records=records,
        record_type=RecordType.TRADE,
        contract=ES_M2026,
        provider_token="DATABENTO",
    )
    second = publish_dataset(
        catalog_root=catalog,
        storage_root=storage,
        name="b.parquet",
        records=records,
        record_type=RecordType.TRADE,
        contract=ES_M2026,
        provider_token="THETADATA",
    )

    one = frames_of(roots, first.manifest_hash)
    two = frames_of(roots, second.manifest_hash)

    assert [frame.availability_time for frame in one] == [frame.availability_time for frame in two]
    assert [event.payload for frame in one for event in frame.events] == [
        event.payload for frame in two for event in frame.events
    ]
    # The provenance-bearing envelope differs, and is meant to.
    assert one[0].events[0].source_manifest != two[0].events[0].source_manifest


def test_duplicate_semantics_are_refused_regardless_of_the_replay_range(
    roots: tuple[Path, Path],
) -> None:
    catalog, storage = roots
    records = [trade_at(at(seconds=0))]
    first = publish_dataset(
        catalog_root=catalog,
        storage_root=storage,
        name="a.parquet",
        records=records,
        record_type=RecordType.TRADE,
        contract=ES_M2026,
        provider_token="DATABENTO",
    )
    second = publish_dataset(
        catalog_root=catalog,
        storage_root=storage,
        name="b.parquet",
        records=records,
        record_type=RecordType.TRADE,
        contract=ES_M2026,
        provider_token="THETADATA",
    )

    with pytest.raises(DuplicateReplaySemanticDatasetError):
        prepared(
            roots,
            first.manifest_hash,
            second.manifest_hash,
            replay_range=ReplayRange(start=at(hours=5), end=at(hours=6)),
        )


# --------------------------------------------------------------------------
# Overlapping histories of one stream
# --------------------------------------------------------------------------


def publish_pair(
    roots: tuple[Path, Path],
    *,
    first_times: list[int],
    second_times: list[int],
    second_contract: object = None,
    second_type: RecordType = RecordType.TRADE,
) -> tuple[ManifestHash, ManifestHash]:
    catalog, storage = roots
    first = publish_dataset(
        catalog_root=catalog,
        storage_root=storage,
        name="first.parquet",
        records=[trade_at(at(seconds=offset)) for offset in first_times],
        record_type=RecordType.TRADE,
        contract=ES_M2026,
    )
    records: list[object] = (
        [quote_at(at(seconds=offset)) for offset in second_times]
        if second_type is RecordType.QUOTE
        else [trade_at(at(seconds=offset), price="4600.00") for offset in second_times]
    )
    second = publish_dataset(
        catalog_root=catalog,
        storage_root=storage,
        name="second.parquet",
        records=records,  # type: ignore[arg-type]
        record_type=second_type,
        contract=ES_M2026 if second_contract is None else second_contract,  # type: ignore[arg-type]
    )
    return first.manifest_hash, second.manifest_hash


def test_disjoint_shards_of_one_stream_are_allowed(roots: tuple[Path, Path]) -> None:
    """Concatenating histories that do not overlap invents nothing."""
    first, second = publish_pair(roots, first_times=[0, 1], second_times=[2, 3])

    frames = frames_of(roots, first, second)

    assert [frame.availability_time for frame in frames] == [
        at(seconds=0),
        at(seconds=1),
        at(seconds=2),
        at(seconds=3),
    ]


def test_two_histories_touching_at_one_instant_overlap(roots: tuple[Path, Path]) -> None:
    """``max(A) == min(B)`` is an overlap: both observe the same instant."""
    first, second = publish_pair(roots, first_times=[0, 2], second_times=[2, 4])

    with pytest.raises(AmbiguousReplayOverlapError):
        prepared(roots, first, second)


def test_two_interleaved_histories_overlap_even_without_a_shared_timestamp(
    roots: tuple[Path, Path],
) -> None:
    first, second = publish_pair(roots, first_times=[0, 10], second_times=[3, 7])

    with pytest.raises(AmbiguousReplayOverlapError, match="not a claim that they disagree"):
        prepared(roots, first, second)


def test_the_same_contract_with_different_record_types_may_overlap(
    roots: tuple[Path, Path],
) -> None:
    first, second = publish_pair(
        roots, first_times=[0, 2], second_times=[1, 3], second_type=RecordType.QUOTE
    )

    frames = frames_of(roots, first, second)

    assert len(frames) == 4


def test_different_contracts_of_one_record_type_may_overlap(roots: tuple[Path, Path]) -> None:
    first, second = publish_pair(
        roots, first_times=[0, 2], second_times=[1, 3], second_contract=NQ_M2026
    )

    frames = frames_of(roots, first, second)

    assert len(frames) == 4


def test_different_delivery_months_of_one_product_may_overlap(roots: tuple[Path, Path]) -> None:
    """``ES:M2026`` and ``ES:U2026`` are different listed contracts."""
    first, second = publish_pair(
        roots, first_times=[0, 2], second_times=[1, 3], second_contract=ES_U2026
    )

    assert len(frames_of(roots, first, second)) == 4


def test_a_range_that_makes_overlapping_sources_disjoint_is_allowed(
    roots: tuple[Path, Path],
) -> None:
    """Overlap is judged on what a run actually selected."""
    first, second = publish_pair(roots, first_times=[0, 1, 20], second_times=[10, 21, 22])

    frames = frames_of(
        roots, first, second, replay_range=ReplayRange(start=at(seconds=0), end=at(seconds=5))
    )

    assert [frame.availability_time for frame in frames] == [at(seconds=0), at(seconds=1)]


def test_an_empty_selection_overlaps_nothing(roots: tuple[Path, Path]) -> None:
    catalog, storage = roots
    first = publish_dataset(
        catalog_root=catalog,
        storage_root=storage,
        name="rows.parquet",
        records=[trade_at(at(seconds=0))],
        record_type=RecordType.TRADE,
        contract=ES_M2026,
    )
    empty = publish_dataset(
        catalog_root=catalog,
        storage_root=storage,
        name="none.parquet",
        records=[],
        record_type=RecordType.TRADE,
        contract=ES_M2026,
    )

    frames = frames_of(roots, first.manifest_hash, empty.manifest_hash)

    assert len(frames) == 1


def test_overlap_is_reported_the_same_way_whichever_order_the_caller_used(
    roots: tuple[Path, Path],
) -> None:
    first, second = publish_pair(roots, first_times=[0, 10], second_times=[3, 7])

    messages = []
    for ordering in ((first, second), (second, first)):
        with pytest.raises(AmbiguousReplayOverlapError) as failure:
            prepared(roots, *ordering)
        messages.append(str(failure.value))

    assert messages[0] == messages[1]
