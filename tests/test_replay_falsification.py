"""Regressions for every defect the Phase 3 falsification passes reproduced.

Kept in one file, separate from the feature suites, so the phase's adversarial
record is readable as a record: each test names the attack it reruns. The first
group covers invariants that were genuinely breakable — silently, and then only
partially — through the documented ``NamedTuple`` bypass; the rest are attacks
that held first time and are pinned here so they keep holding.
"""

from __future__ import annotations

import shutil
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import MappingProxyType

import pytest
from dataset_fixtures import ES_M2026, NQ_M2026
from replay_fixtures import (
    at,
    bar_starting,
    publish_dataset,
    quote_at,
    trade_at,
    write_artifact,
)

from quant_research_terminal.catalog.errors import (
    MissingArtifactError,
    PhysicalArtifactHashMismatchError,
)
from quant_research_terminal.catalog.store import rebuild_index
from quant_research_terminal.domain.continuous_series import ContinuousSeriesId
from quant_research_terminal.domain.dataset_identity import (
    ManifestHash,
    PhysicalArtifactHash,
    RecordType,
    SemanticDatasetHash,
)
from quant_research_terminal.domain.replay import (
    ReplayConfig,
    ReplayEvent,
    ReplayFrame,
    ReplayPayload,
    ReplayRange,
    frame_event_sort_key,
)
from quant_research_terminal.replay import (
    AmbiguousReplayOverlapError,
    NonMonotonicReplaySourceError,
    PreparedRow,
    PreparedSource,
    ReplayArtifactVerificationError,
    ReplayInvariantError,
    frame_timeline,
    prepare_replay,
)

SOURCE_HASH = ManifestHash(value="a" * 64)


@pytest.fixture
def roots(tmp_path: Path) -> tuple[Path, Path]:
    storage = tmp_path / "storage"
    catalog = tmp_path / "catalog"
    storage.mkdir()
    return catalog, storage


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


def event_at(payload: ReplayPayload, record_type: RecordType, contract: object) -> ReplayEvent:
    return ReplayEvent(
        availability_time=payload.timestamp,
        record_type=record_type,
        contract=contract,  # type: ignore[arg-type]
        source_manifest=SOURCE_HASH,
        row_ordinal=0,
        payload=payload,
    )


# --------------------------------------------------------------------------
# The `NamedTuple` bypass, and the two invariants it could break silently
#
# `PreparedSource` and `PreparedRow` do not validate — an explicit, visible
# bypass in the spirit of `model_construct`. Everything `build_prepared_source`
# produces satisfies both invariants below, so for a while they merely
# *followed* from the inputs and were not checked at the output.
#
# Both consequences were reachable through the public API and both were
# **silent, then partial**: the stream emitted good frames first and only failed
# later — the exact shape `replay/prepare.py` calls out as worse than a failure,
# because it looks like a replay.
#
# `frame_timeline` therefore validates every source eagerly, before returning an
# iterator at all. These tests pin that no frame escapes first.
# --------------------------------------------------------------------------


def hand_built(*rows: PreparedRow) -> PreparedSource:
    """Assemble a source without `build_prepared_source`, bypassing validation."""
    return PreparedSource(
        manifest_hash=SOURCE_HASH,
        semantic_hash=SemanticDatasetHash(value="b" * 64),
        contract=ES_M2026,
        record_type=RecordType.TRADE,
        rows=rows,
    )


def test_a_source_whose_rows_run_backwards_yields_no_frame_at_all() -> None:
    """Replay time running backwards is the worst outcome available here."""
    out_of_order = hand_built(
        PreparedRow(0, at(seconds=5), trade_at(at(seconds=5))),
        PreparedRow(1, at(seconds=1), trade_at(at(seconds=1))),
    )

    with pytest.raises(NonMonotonicReplaySourceError):
        frame_timeline([out_of_order])


def test_a_cached_availability_time_that_lies_yields_no_frame_at_all() -> None:
    """The sibling of the invariant above, and the more insidious of the two.

    Every decision the interleave makes — which instant is next, which rows are
    drained into it, whether time advanced — reads the *cached* availability
    time. A row whose cache disagrees with its payload therefore steers the whole
    timeline, and used to be caught only when `ReplayEvent` was finally
    constructed: after earlier frames had been handed to the consumer, and as a
    raw pydantic `ValidationError` rather than a `ReplayError`.
    """
    lying = hand_built(
        PreparedRow(0, at(seconds=1), trade_at(at(seconds=1))),
        PreparedRow(1, at(seconds=5), trade_at(at(seconds=9))),
    )

    with pytest.raises(ReplayInvariantError, match="second opinion"):
        frame_timeline([lying])


def test_a_lying_cache_is_caught_before_the_iterator_exists() -> None:
    """Not merely "raises eventually": the call itself fails.

    `frame_timeline` is deliberately not a generator function, because a
    generator would defer validation to the first `next()` and — worse — a
    caller that iterated would still receive whatever frames preceded the bad
    row. Calling it without iterating must already raise.
    """
    lying = hand_built(
        PreparedRow(0, at(seconds=1), trade_at(at(seconds=1))),
        PreparedRow(1, at(seconds=5), trade_at(at(seconds=9))),
    )

    with pytest.raises(ReplayInvariantError):
        frame_timeline([lying])  # no list(), no next()


def test_eager_validation_does_not_fire_on_a_legitimately_repeated_instant() -> None:
    """Equal times inside one frame are the normal case and must not trip it."""
    source = hand_built(
        PreparedRow(0, at(seconds=1), trade_at(at(seconds=1))),
        PreparedRow(1, at(seconds=1), trade_at(at(seconds=1), price="4500.50")),
        PreparedRow(2, at(seconds=2), trade_at(at(seconds=2))),
    )

    frames = list(frame_timeline([source]))

    assert [len(frame.events) for frame in frames] == [2, 1]


# --------------------------------------------------------------------------
# Attacks that held, pinned so they keep holding
# --------------------------------------------------------------------------


def test_the_frame_order_key_ignores_record_type_contract_and_payload() -> None:
    """D14 / D40 / D77: no physical, contract, or type component in the order.

    Three events differing in every market-facing way, sharing one source
    position, produce one key. There is no component of the ordering that a
    record type, a contract, or a payload could influence.
    """
    keys = {
        frame_event_sort_key(event)
        for event in (
            event_at(trade_at(at(seconds=0)), RecordType.TRADE, ES_M2026),
            event_at(quote_at(at(seconds=0)), RecordType.QUOTE, ES_M2026),
            event_at(bar_starting(at(seconds=-60)), RecordType.BAR, NQ_M2026),
        )
    }

    assert keys == {("a" * 64, 0)}


@pytest.mark.parametrize(
    "impostor",
    [
        "trades.parquet",
        "ESM6",
        SemanticDatasetHash(value="a" * 64),
        PhysicalArtifactHash(value="a" * 64),
        ES_M2026,
        ContinuousSeriesId.parse("CME:ES:CONTINUOUS:ACTIVE"),
    ],
)
def test_only_an_exact_manifest_hash_is_a_replay_input(impostor: object) -> None:
    """D20 / D21 / D60: no filename, alias, contract, or synthetic series."""
    with pytest.raises((TypeError, ValueError)):
        ReplayConfig(manifest_hashes=(impostor,), replay_range=None)  # type: ignore[arg-type]


def test_an_unusual_bar_interval_is_not_rounded_or_recomputed(
    roots: tuple[Path, Path],
) -> None:
    """D55: availability is the validated canonical timestamp, exactly.

    A 37-second-and-one-microsecond interval has no round number to drift
    towards, so any recomputation through seconds or floats would show here.
    """
    catalog, storage = roots
    interval = timedelta(seconds=37, microseconds=1)
    manifest = publish_dataset(
        catalog_root=catalog,
        storage_root=storage,
        name="odd.parquet",
        records=[bar_starting(at(seconds=0), interval=interval)],
        record_type=RecordType.BAR,
        contract=ES_M2026,
    )
    close = at(seconds=0) + interval

    assert [frame.availability_time for frame in frames_of(roots, manifest.manifest_hash)] == [
        close
    ]
    assert (
        frames_of(
            roots,
            manifest.manifest_hash,
            replay_range=ReplayRange(start=at(seconds=0), end=close),
        )
        == []
    )


def test_pre_epoch_availability_times_replay_in_order(roots: tuple[Path, Path]) -> None:
    """Negative epoch microseconds are ordinary historical data, not an edge case."""
    catalog, storage = roots
    old = datetime(1968, 4, 2, 9, 30, tzinfo=UTC)
    manifest = publish_dataset(
        catalog_root=catalog,
        storage_root=storage,
        name="old.parquet",
        records=[trade_at(old), trade_at(old + timedelta(seconds=1))],
        record_type=RecordType.TRADE,
        contract=ES_M2026,
    )

    assert [frame.availability_time for frame in frames_of(roots, manifest.manifest_hash)] == [
        old,
        old + timedelta(seconds=1),
    ]


def test_a_large_simultaneous_burst_stays_one_frame(roots: tuple[Path, Path]) -> None:
    """D61: an instant is never chunked, however many observations it holds."""
    catalog, storage = roots
    manifest = publish_dataset(
        catalog_root=catalog,
        storage_root=storage,
        name="burst.parquet",
        records=[trade_at(at(seconds=0), price=f"{4000 + n}.25") for n in range(2000)],
        record_type=RecordType.TRADE,
        contract=ES_M2026,
    )

    frames = frames_of(roots, manifest.manifest_hash)

    assert len(frames) == 1
    assert [event.row_ordinal for event in frames[0].events] == list(range(2000))


def test_a_prepared_replay_survives_destruction_of_its_source_file(
    roots: tuple[Path, Path],
) -> None:
    """D59/TOCTOU: preparation captures a snapshot, so the file stops mattering.

    Deliberately *not* a claim that the file is protected — nothing is locked.
    The claim is narrower and is the useful one: this run no longer depends on it.
    """
    catalog, storage = roots
    manifest = publish_dataset(
        catalog_root=catalog,
        storage_root=storage,
        name="snap.parquet",
        records=[trade_at(at(seconds=0)), trade_at(at(seconds=1))],
        record_type=RecordType.TRADE,
        contract=ES_M2026,
    )
    replay = prepare_replay(
        catalog_root=catalog,
        storage_root=storage,
        config=ReplayConfig(manifest_hashes=(manifest.manifest_hash,), replay_range=None),
    )
    before = list(replay.frames())

    (storage / "snap.parquet").write_bytes(b"destroyed")

    assert list(replay.frames()) == before
    assert len(before) == 2


def test_every_permutation_of_three_simultaneous_sources_is_one_frame(
    roots: tuple[Path, Path],
) -> None:
    """D11 / D35 / D38, at the hardest point: everything sharing one instant."""
    catalog, storage = roots
    first = publish_dataset(
        catalog_root=catalog,
        storage_root=storage,
        name="a.parquet",
        records=[trade_at(at(seconds=0)), trade_at(at(seconds=0), price="4500.50")],
        record_type=RecordType.TRADE,
        contract=ES_M2026,
    ).manifest_hash
    second = publish_dataset(
        catalog_root=catalog,
        storage_root=storage,
        name="b.parquet",
        records=[quote_at(at(seconds=0)), quote_at(at(seconds=0), bid="4499.00")],
        record_type=RecordType.QUOTE,
        contract=ES_M2026,
    ).manifest_hash
    third = publish_dataset(
        catalog_root=catalog,
        storage_root=storage,
        name="c.parquet",
        records=[trade_at(at(seconds=0))],
        record_type=RecordType.TRADE,
        contract=NQ_M2026,
    ).manifest_hash

    outputs = {
        tuple(
            (event.source_manifest.canonical(), event.row_ordinal)
            for frame in frames_of(roots, *order)
            for event in frame.events
        )
        for order in (
            (first, second, third),
            (first, third, second),
            (second, first, third),
            (second, third, first),
            (third, first, second),
            (third, second, first),
        )
    }

    assert len(outputs) == 1
    assert len(next(iter(outputs))) == 5


# --------------------------------------------------------------------------
# Pass 2 — an independent hostile review of the whole diff
# --------------------------------------------------------------------------


def test_the_worst_failure_is_reported_not_the_first(roots: tuple[Path, Path]) -> None:
    """Two bad copies: one merely absent, one *altered*. The alteration is the news.

    Keeping the first failure encountered discarded exactly the distinction
    ADR-012 keeps the physical and semantic identities apart to preserve. The
    index lists `absent.parquet` before `altered.parquet`, so first-wins reported
    a missing file while a surviving copy of the dataset had been replaced.
    """
    catalog, storage = roots
    manifest = publish_dataset(
        catalog_root=catalog,
        storage_root=storage,
        name="absent.parquet",
        records=[trade_at(at(seconds=0))],
        record_type=RecordType.TRADE,
        contract=ES_M2026,
    )
    shutil.copy2(storage / "absent.parquet", storage / "altered.parquet")
    index = rebuild_index(catalog_root=catalog, storage_root=storage)
    assert index.find_locations(manifest.physical_hash) == ("absent.parquet", "altered.parquet")

    (storage / "absent.parquet").unlink()
    write_artifact(
        storage / "altered.parquet",
        [trade_at(at(seconds=0), price="9999.00")],
        record_type=RecordType.TRADE,
        contract=ES_M2026,
    )

    with pytest.raises(ReplayArtifactVerificationError) as failure:
        prepare_replay(
            catalog_root=catalog,
            storage_root=storage,
            config=ReplayConfig(manifest_hashes=(manifest.manifest_hash,), replay_range=None),
        )

    assert isinstance(failure.value.__cause__, PhysicalArtifactHashMismatchError)
    assert "altered.parquet" in str(failure.value)
    # Both are still reported; only the *chained* cause is ranked.
    assert "absent.parquet" in str(failure.value)


def test_an_unrecognised_copy_failure_outranks_a_merely_missing_file() -> None:
    """Unknown failure modes surface rather than hiding behind an absent file."""
    from quant_research_terminal.replay.prepare import _severity

    class Unanticipated(Exception):
        pass

    assert _severity(Unanticipated()) < _severity(MissingArtifactError("gone"))


def test_a_replay_range_can_select_disjoint_slices_of_overlapping_histories(
    roots: tuple[Path, Path],
) -> None:
    """A documented consequence of judging overlap on the selection, pinned.

    Two datasets that overlap globally — and disagree at a shared instant — are
    refused outright. A narrower window whose *selected* rows do not overlap is
    accepted, and the resulting timeline draws from both.

    This is composition of disjoint shards, which invents nothing, and it is the
    behaviour the range rule specifies. It is pinned here because the guarantee a
    reader should take from `AmbiguousReplayOverlapError` is narrower than it
    first appears: replay refuses to interleave overlapping histories *that a run
    selected*, not to notice that its inputs overlap elsewhere. `docs/replay.md`
    says so; this test makes the statement executable.
    """
    catalog, storage = roots
    first = publish_dataset(
        catalog_root=catalog,
        storage_root=storage,
        name="first.parquet",
        records=[trade_at(at(seconds=n)) for n in (0, 10, 20)],
        record_type=RecordType.TRADE,
        contract=ES_M2026,
    ).manifest_hash
    second = publish_dataset(
        catalog_root=catalog,
        storage_root=storage,
        name="second.parquet",
        records=[trade_at(at(seconds=n), price="9999.00") for n in (10, 30)],
        record_type=RecordType.TRADE,
        contract=ES_M2026,
    ).manifest_hash

    # Globally overlapping, and disagreeing at 10s: refused.
    with pytest.raises(AmbiguousReplayOverlapError):
        frames_of(roots, first, second)

    # A window in which the selections are disjoint: accepted, drawing on both.
    stitched = frames_of(
        roots, first, second, replay_range=ReplayRange(start=at(seconds=15), end=at(seconds=40))
    )

    assert [frame.availability_time for frame in stitched] == [at(seconds=20), at(seconds=30)]
    assert {event.source_manifest for frame in stitched for event in frame.events} == {
        first,
        second,
    }


def test_the_published_record_type_table_cannot_be_mutated() -> None:
    """`Final` binds a type checker; this table decides two identities at runtime.

    Publishing it for replay's payload discriminator put a mapping that selects
    the semantic encoder *and* validates a replay payload behind a public name.
    One assignment into a plain `dict` would have corrupted canonical dataset
    identity and replay's type invariant together.
    """
    from quant_research_terminal.domain.dataset_identity import (
        RECORD_PAYLOAD_TYPES,
        SEMANTIC_FIELD_ORDER,
        SIDE_CODE,
    )

    for table in (RECORD_PAYLOAD_TYPES, SEMANTIC_FIELD_ORDER, SIDE_CODE):
        assert isinstance(table, MappingProxyType)
        with pytest.raises(TypeError):
            table[next(iter(table))] = "forged"  # type: ignore[index]
