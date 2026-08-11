"""Explicit cross-batch comparison, and the conclusions it refuses to draw.

The properties under attack:

* comparison reports evidence and never a verdict;
* a shared timestamp is **not** a logical event identity for trades or quotes;
* duplicate rows keep their multiplicity;
* reordering is reported as reordering, not as correction;
* bars are compared by their proven natural key, and duplicate keys are refused
  rather than silently resolved;
* provider and physical encoding do not change what data *is*;
* nothing about comparison creates lineage.
"""

from __future__ import annotations

import shutil
from datetime import timedelta
from decimal import Decimal
from pathlib import Path

import pyarrow.parquet as pq  # type: ignore[import-untyped]
import pytest
from dataset_fixtures import BASE_TIME, ES_M2026, ES_U2026, manifest_for, quote, trade
from lineage_fixtures import register

from quant_research_terminal.catalog import (
    AmbiguousBarKeyError,
    DatasetNotComparableError,
    MissingArtifactError,
    PhysicalArtifactHashMismatchError,
    compare_datasets,
    load_index,
    published_relations,
    register_dataset,
)
from quant_research_terminal.data.parquet_store import write_trades_v3
from quant_research_terminal.domain.dataset_identity import RecordType
from quant_research_terminal.domain.models import Bar
from quant_research_terminal.domain.trade_side import TradeSide


@pytest.fixture
def roots(tmp_path: Path) -> tuple[Path, Path]:
    storage = tmp_path / "storage"
    catalog = tmp_path / "catalog"
    storage.mkdir()
    return catalog, storage


def compare(catalog: Path, storage: Path, left, right):  # type: ignore[no-untyped-def]
    return compare_datasets(
        catalog_root=catalog,
        storage_root=storage,
        left=left.manifest_hash,
        right=right.manifest_hash,
    )


def keyed_bar(*, minute: int, close: str = "4501.00") -> Bar:
    return Bar(
        instrument_symbol="ESM6",
        interval_start=BASE_TIME + timedelta(minutes=minute),
        interval=timedelta(minutes=1),
        open=Decimal("4499.50"),
        high=Decimal("4502.00"),
        low=Decimal("4498.75"),
        close=Decimal(close),
        volume=Decimal(1000),
    )


# --------------------------------------------------------------------------
# Comparability
# --------------------------------------------------------------------------


def test_different_contracts_are_not_comparable(roots: tuple[Path, Path]) -> None:
    """Unrelated datasets must not be called conflicting."""
    catalog, storage = roots
    a = register(catalog, storage, "a.parquet", [trade(offset=1)], contract=ES_M2026)
    b = register(catalog, storage, "b.parquet", [trade(offset=1)], contract=ES_U2026)
    with pytest.raises(DatasetNotComparableError, match="different listed contracts"):
        compare(catalog, storage, a, b)


def test_different_record_types_are_not_comparable(roots: tuple[Path, Path]) -> None:
    catalog, storage = roots
    a = register(catalog, storage, "a.parquet", [trade(offset=1)])
    b = register(catalog, storage, "b.parquet", [quote(offset=1)], record_type=RecordType.QUOTE)
    with pytest.raises(DatasetNotComparableError, match="different kinds of data"):
        compare(catalog, storage, a, b)


def test_a_missing_artifact_fails_typed(roots: tuple[Path, Path]) -> None:
    catalog, storage = roots
    a = register(catalog, storage, "a.parquet", [trade(offset=1)])
    b = register(catalog, storage, "b.parquet", [trade(offset=2)])
    (storage / "b.parquet").unlink()
    with pytest.raises(MissingArtifactError):
        compare(catalog, storage, a, b)


def test_a_replaced_artifact_is_not_compared(roots: tuple[Path, Path]) -> None:
    """Comparing bytes nobody stored would produce confident nonsense.

    The index still points at the path, so this surfaces as the precise failure
    — the bytes there are not the bytes the manifest describes — rather than as
    a missing file.
    """
    catalog, storage = roots
    a = register(catalog, storage, "a.parquet", [trade(offset=1)])
    b = register(catalog, storage, "b.parquet", [trade(offset=2)])
    write_trades_v3(storage / "b.parquet", [trade(offset=99)], contract=ES_M2026)
    with pytest.raises(PhysicalArtifactHashMismatchError):
        compare(catalog, storage, a, b)


# --------------------------------------------------------------------------
# The semantic-hash shortcut
# --------------------------------------------------------------------------


def test_provider_difference_is_not_a_difference_in_data(roots: tuple[Path, Path]) -> None:
    catalog, storage = roots
    a = register(catalog, storage, "a.parquet", provider_token="DATABENTO")
    b = register(catalog, storage, "b.parquet", provider_token="THETADATA")
    result = compare(catalog, storage, a, b)
    assert result.semantically_identical
    assert result.same_sequence and result.same_multiset
    assert not result.reordered_only
    assert published_relations(catalog) == ()


def test_physical_re_encoding_is_not_a_difference_in_data(roots: tuple[Path, Path]) -> None:
    catalog, storage = roots
    a = register(catalog, storage, "a.parquet")

    reencoded = storage / "b.parquet"
    pq.write_table(
        pq.read_table(storage / "a.parquet"), reencoded, compression="gzip", row_group_size=1
    )
    from dataset_fixtures import three_trades

    manifest = manifest_for(reencoded, three_trades())
    register_dataset(
        catalog_root=catalog, storage_root=storage, artifact_path=reencoded, manifest=manifest
    )
    assert manifest.physical_hash != a.physical_hash
    assert manifest.semantic_hash == a.semantic_hash

    result = compare_datasets(
        catalog_root=catalog,
        storage_root=storage,
        left=a.manifest_hash,
        right=manifest.manifest_hash,
    )
    assert result.semantically_identical


# --------------------------------------------------------------------------
# Trade and quote: exact rows only
# --------------------------------------------------------------------------


def test_duplicate_multiplicity_is_preserved(roots: tuple[Path, Path]) -> None:
    """``[X, X, Y]`` and ``[X, Y]`` are not the same multiset (ADR-003)."""
    catalog, storage = roots
    x, y = trade(offset=1), trade(offset=2)
    a = register(catalog, storage, "a.parquet", [x, x, y])
    b = register(catalog, storage, "b.parquet", [x, y])

    result = compare(catalog, storage, a, b)
    assert not result.same_multiset
    assert result.left_row_count == 3 and result.right_row_count == 2
    assert result.exact_row_overlap_count == 2
    assert result.left_only_exact_row_count == 1
    assert result.right_only_exact_row_count == 0


def test_reordering_is_reported_as_reordering(roots: tuple[Path, Path]) -> None:
    """Same rows, different order: not a correction, and no lineage follows."""
    catalog, storage = roots
    x, y = trade(offset=1), trade(offset=2)
    a = register(catalog, storage, "a.parquet", [x, y])
    b = register(catalog, storage, "b.parquet", [y, x])
    assert a.semantic_hash != b.semantic_hash

    result = compare(catalog, storage, a, b)
    assert result.same_multiset
    assert not result.same_sequence
    assert result.reordered_only
    assert result.exact_row_overlap_count == 2
    assert published_relations(catalog) == ()


def test_a_shared_trade_timestamp_is_not_a_logical_event_identity(
    roots: tuple[Path, Path],
) -> None:
    """The ADR-003 trap, asserted directly.

    Two trades at one instant with different price and size are reported as two
    different exact rows and nothing more. Calling them a correction would be
    the inference that destroyed data when it was tried inside one batch.
    """
    catalog, storage = roots
    left = trade(offset=5, price="4500.25", size=1)
    right = trade(offset=5, price="4501.75", size=4)
    assert left.timestamp == right.timestamp

    a = register(catalog, storage, "a.parquet", [left])
    b = register(catalog, storage, "b.parquet", [right])

    result = compare(catalog, storage, a, b)
    assert result.exact_row_overlap_count == 0
    assert not result.has_exact_row_overlap
    assert result.left_only_exact_row_count == 1
    assert result.right_only_exact_row_count == 1
    assert result.bars is None
    # Nothing anywhere names this a correction, a conflict, or a candidate.
    assert not any(
        token in name
        for name in type(result).model_fields
        for token in ("correction", "conflict", "supersed", "verdict", "prefer")
    )
    assert published_relations(catalog) == ()


def test_a_shared_quote_timestamp_is_not_a_logical_event_identity(
    roots: tuple[Path, Path],
) -> None:
    catalog, storage = roots
    left = quote(offset=5, bid="4500.00", ask="4500.25")
    right = quote(offset=5, bid="4499.75", ask="4500.50")
    assert left.timestamp == right.timestamp

    a = register(catalog, storage, "a.parquet", [left], record_type=RecordType.QUOTE)
    b = register(catalog, storage, "b.parquet", [right], record_type=RecordType.QUOTE)

    result = compare(catalog, storage, a, b)
    assert result.exact_row_overlap_count == 0
    assert result.left_only_exact_row_count == 1
    assert result.right_only_exact_row_count == 1
    assert result.bars is None


def test_no_exact_overlap_does_not_claim_disjoint_events(roots: tuple[Path, Path]) -> None:
    """The wording distinction that keeps a future reader honest.

    Overlapping time ranges with no identical row is exactly the state where a
    correction is *possible* and unprovable, so the result says only that no
    exact row matched.
    """
    catalog, storage = roots
    a = register(catalog, storage, "a.parquet", [trade(offset=5, price="4500.25")])
    b = register(catalog, storage, "b.parquet", [trade(offset=5, price="4501.75")])
    result = compare(catalog, storage, a, b)
    assert result.exact_row_overlap_count == 0
    assert not result.temporal_ranges_disjoint


def test_temporal_ranges_disjoint_is_a_prefilter_only(roots: tuple[Path, Path]) -> None:
    catalog, storage = roots
    a = register(catalog, storage, "a.parquet", [trade(offset=0), trade(offset=1)])
    b = register(catalog, storage, "b.parquet", [trade(offset=500), trade(offset=501)])
    result = compare(catalog, storage, a, b)
    assert result.temporal_ranges_disjoint
    assert result.exact_row_overlap_count == 0


def test_the_vendor_alias_does_not_affect_row_equality(roots: tuple[Path, Path]) -> None:
    """``vendor_symbol`` is provenance (ADR-012) and never decides sameness."""
    catalog, storage = roots
    a = register(catalog, storage, "a.parquet", [trade(offset=1, symbol="ESM6")])
    b = register(catalog, storage, "b.parquet", [trade(offset=1, symbol="ES M6")])
    result = compare(catalog, storage, a, b)
    assert result.semantically_identical


def test_the_side_vocabulary_is_compared_by_meaning(roots: tuple[Path, Path]) -> None:
    catalog, storage = roots
    a = register(catalog, storage, "a.parquet", [trade(offset=1, side=TradeSide.BUY)])
    b = register(catalog, storage, "b.parquet", [trade(offset=1, side=TradeSide.SELL)])
    result = compare(catalog, storage, a, b)
    assert result.exact_row_overlap_count == 0


# --------------------------------------------------------------------------
# Bars: the one record type with a proven natural key
# --------------------------------------------------------------------------


def test_bars_agreeing_on_a_period_are_identical_overlap(roots: tuple[Path, Path]) -> None:
    catalog, storage = roots
    shared = keyed_bar(minute=0)
    a = register(
        catalog, storage, "a.parquet", [shared, keyed_bar(minute=1)], record_type=RecordType.BAR
    )
    b = register(
        catalog, storage, "b.parquet", [shared, keyed_bar(minute=2)], record_type=RecordType.BAR
    )

    result = compare(catalog, storage, a, b)
    assert result.bars is not None
    assert result.bars.identical_overlap_count == 1
    assert result.bars.conflict_count == 0
    assert result.bars.left_only_count == 1
    assert result.bars.right_only_count == 1


def test_bars_disagreeing_on_one_period_are_a_proven_conflict(
    roots: tuple[Path, Path],
) -> None:
    """Same contract, same interval_start, same interval, different OHLCV."""
    catalog, storage = roots
    a = register(
        catalog,
        storage,
        "a.parquet",
        [keyed_bar(minute=0, close="4501.00")],
        record_type=RecordType.BAR,
    )
    b = register(
        catalog,
        storage,
        "b.parquet",
        [keyed_bar(minute=0, close="4499.00")],
        record_type=RecordType.BAR,
    )

    result = compare(catalog, storage, a, b)
    assert result.bars is not None
    assert result.bars.conflict_count == 1
    assert result.bars.identical_overlap_count == 0
    key = result.bars.conflicting_keys[0]
    assert key.interval_microseconds == 60_000_000
    # A conflict is evidence, not a direction, and it publishes nothing.
    assert published_relations(catalog) == ()


def test_a_duplicate_bar_key_is_refused_not_resolved(roots: tuple[Path, Path]) -> None:
    """First-wins and last-wins are both guesses about which claim counts.

    Storage preserves duplicate rows and version-3 writes do not pass through
    the import validator that rejects them (ADR-005), so this is reachable.
    """
    catalog, storage = roots
    duplicated = [keyed_bar(minute=0, close="4501.00"), keyed_bar(minute=0, close="4499.00")]
    a = register(catalog, storage, "a.parquet", duplicated, record_type=RecordType.BAR)
    b = register(catalog, storage, "b.parquet", [keyed_bar(minute=0)], record_type=RecordType.BAR)
    with pytest.raises(AmbiguousBarKeyError, match="more than one row"):
        compare(catalog, storage, a, b)


def test_bar_keys_are_reported_in_deterministic_order(roots: tuple[Path, Path]) -> None:
    catalog, storage = roots
    a = register(
        catalog,
        storage,
        "a.parquet",
        [keyed_bar(minute=index) for index in (5, 1, 9, 3)],
        record_type=RecordType.BAR,
    )
    b = register(catalog, storage, "b.parquet", [keyed_bar(minute=99)], record_type=RecordType.BAR)
    result = compare(catalog, storage, a, b)
    assert result.bars is not None
    starts = [key.interval_start_microseconds for key in result.bars.left_only_keys]
    assert starts == sorted(starts)


def test_bar_comparison_is_unaffected_by_a_different_vendor_alias(
    roots: tuple[Path, Path],
) -> None:
    catalog, storage = roots
    left = keyed_bar(minute=0)
    right = left.model_copy(update={"instrument_symbol": "ES M6"})
    a = register(catalog, storage, "a.parquet", [left], record_type=RecordType.BAR)
    b = register(catalog, storage, "b.parquet", [right], record_type=RecordType.BAR)
    result = compare(catalog, storage, a, b)
    assert result.semantically_identical


# --------------------------------------------------------------------------
# Comparison creates nothing
# --------------------------------------------------------------------------


def test_comparison_is_ephemeral_and_publishes_nothing(roots: tuple[Path, Path]) -> None:
    catalog, storage = roots
    a = register(catalog, storage, "a.parquet", [trade(offset=1)])
    b = register(catalog, storage, "b.parquet", [trade(offset=2)])
    before = sorted(path.name for path in catalog.rglob("*"))

    compare(catalog, storage, a, b)

    assert sorted(path.name for path in catalog.rglob("*")) == before
    assert published_relations(catalog) == ()


def test_a_comparison_result_has_no_identity_or_persistence_hook(
    roots: tuple[Path, Path],
) -> None:
    """A measurement is not a new immutable fact about the world."""
    catalog, storage = roots
    a = register(catalog, storage, "a.parquet", [trade(offset=1)])
    b = register(catalog, storage, "b.parquet", [trade(offset=2)])
    result = compare(catalog, storage, a, b)
    fields = {name.lower() for name in type(result).model_fields}
    assert not any(
        token in name for name in fields for token in ("hash", "path", "location", "created")
    )
    assert not hasattr(result, "publish")


def test_comparison_never_merges_or_rewrites_an_artifact(roots: tuple[Path, Path]) -> None:
    catalog, storage = roots
    a = register(catalog, storage, "a.parquet", [trade(offset=1)])
    b = register(catalog, storage, "b.parquet", [trade(offset=2)])
    before = {path: path.read_bytes() for path in sorted(storage.rglob("*.parquet"))}

    compare(catalog, storage, a, b)

    assert {path: path.read_bytes() for path in sorted(storage.rglob("*.parquet"))} == before


# --------------------------------------------------------------------------
# Falsification pass 2 regressions
# --------------------------------------------------------------------------


def test_a_stale_index_entry_does_not_defeat_a_good_copy(roots: tuple[Path, Path]) -> None:
    """The index is explicitly the part allowed to be wrong.

    Byte-identical copies are interchangeable, so one bad entry must not turn a
    comparison a surviving copy could answer into a hard failure. Trying only
    the lexicographically-first location did exactly that.
    """
    catalog, storage = roots
    left = register(catalog, storage, "left.parquet", [trade(offset=1)])
    good = register(catalog, storage, "zz_good.parquet", [trade(offset=2)])

    stale = storage / "aa_stale.parquet"
    shutil.copy2(storage / "zz_good.parquet", stale)
    register_dataset(
        catalog_root=catalog,
        storage_root=storage,
        artifact_path=stale,
        manifest=manifest_for(stale, [trade(offset=2)]),
    )
    write_trades_v3(stale, [trade(offset=77)], contract=ES_M2026)

    locations = load_index(catalog).find_locations(good.physical_hash)
    assert locations[0] == "aa_stale.parquet"

    result = compare(catalog, storage, left, good)
    assert result.right_row_count == 1


def test_every_location_failing_reports_the_real_diagnosis(roots: tuple[Path, Path]) -> None:
    catalog, storage = roots
    a = register(catalog, storage, "a.parquet", [trade(offset=1)])
    b = register(catalog, storage, "b.parquet", [trade(offset=2)])
    write_trades_v3(storage / "b.parquet", [trade(offset=99)], contract=ES_M2026)
    with pytest.raises(PhysicalArtifactHashMismatchError, match="physical hash"):
        compare(catalog, storage, a, b)


def test_identical_bar_datasets_still_report_a_bar_section(roots: tuple[Path, Path]) -> None:
    """``bars is None`` must mean "not a bar dataset", never "identical bars".

    An early return on semantic equality made the field's meaning depend on a
    flag the caller had to check first.
    """
    catalog, storage = roots
    shared = [keyed_bar(minute=0), keyed_bar(minute=1)]
    a = register(catalog, storage, "a.parquet", shared, record_type=RecordType.BAR)
    b = register(
        catalog,
        storage,
        "b.parquet",
        shared,
        record_type=RecordType.BAR,
        provider_token="THETADATA",
    )
    assert a.semantic_hash == b.semantic_hash

    result = compare(catalog, storage, a, b)
    assert result.semantically_identical
    assert result.bars is not None
    assert result.bars.identical_overlap_count == 2
    assert result.bars.conflict_count == 0


def test_the_duplicate_key_refusal_does_not_depend_on_the_other_dataset(
    roots: tuple[Path, Path],
) -> None:
    """A guarantee that holds only for some comparison partners is not one.

    The shortcut previously skipped the bar-key analysis whenever the semantic
    hashes matched, so an ambiguous dataset compared cleanly against a copy of
    itself and raised only against a different one.
    """
    catalog, storage = roots
    duplicated = [keyed_bar(minute=0, close="4501.00"), keyed_bar(minute=0, close="4499.00")]
    a = register(catalog, storage, "a.parquet", duplicated, record_type=RecordType.BAR)
    b = register(
        catalog,
        storage,
        "b.parquet",
        duplicated,
        record_type=RecordType.BAR,
        provider_token="THETADATA",
    )
    assert a.semantic_hash == b.semantic_hash
    with pytest.raises(AmbiguousBarKeyError):
        compare(catalog, storage, a, b)


def test_absent_time_bounds_do_not_raise(roots: tuple[Path, Path]) -> None:
    """An empty dataset has no bounds; the prefilter must return, not unpack."""
    catalog, storage = roots
    empty = register(catalog, storage, "empty.parquet", [])
    full = register(catalog, storage, "full.parquet", [trade(offset=1)])
    result = compare(catalog, storage, empty, full)
    assert result.temporal_ranges_disjoint is False
    assert result.left_row_count == 0
    assert result.right_only_exact_row_count == 1


def test_a_comparison_refuses_a_digest_subclass() -> None:
    from quant_research_terminal.catalog import DatasetComparison
    from quant_research_terminal.domain.dataset_identity import ManifestHash

    class Forged(ManifestHash):  # type: ignore[misc]
        def canonical(self) -> str:
            return "0" * 64

    with pytest.raises(TypeError, match="exactly"):
        DatasetComparison(
            left=Forged(value="a" * 64),
            right=ManifestHash(value="b" * 64),
            record_type=RecordType.TRADE,
            semantically_identical=False,
            same_sequence=False,
            same_multiset=False,
            reordered_only=False,
            temporal_ranges_disjoint=False,
            left_row_count=0,
            right_row_count=0,
            exact_row_overlap_count=0,
            left_only_exact_row_count=0,
            right_only_exact_row_count=0,
            bars=None,
        )
