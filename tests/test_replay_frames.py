"""Frame construction: simultaneity, ordering, ranges, and the stream properties.

The properties under attack:

* every observation that became available at one instant is delivered in one
  frame, and an instant is never split across two;
* the order inside a frame is deterministic and is decided by provenance alone —
  no record type, contract, provider, or path priority exists;
* frame times strictly increase;
* every selected row is emitted exactly once, under its original ordinal, with
  no loss, no duplication, and no deduplication of genuinely repeated rows;
* a bar appears at its interval close and at no earlier instant;
* a replay range includes its start, excludes its end, and never clips a frame
  in half.
"""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pytest
from dataset_fixtures import ES_M2026, NQ_M2026
from replay_fixtures import (
    at,
    bar_starting,
    frame_digest,
    publish_dataset,
    quote_at,
    trade_at,
)

from quant_research_terminal.domain.dataset_identity import ManifestHash, RecordType
from quant_research_terminal.domain.replay import (
    ReplayConfig,
    ReplayFrame,
    ReplayRange,
    frame_event_sort_key,
)
from quant_research_terminal.replay import prepare_replay


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


def publish(
    roots: tuple[Path, Path],
    name: str,
    records: list[object],
    *,
    record_type: RecordType = RecordType.TRADE,
    contract: object = ES_M2026,
) -> ManifestHash:
    catalog, storage = roots
    return publish_dataset(
        catalog_root=catalog,
        storage_root=storage,
        name=name,
        records=records,  # type: ignore[arg-type]
        record_type=record_type,
        contract=contract,  # type: ignore[arg-type]
    ).manifest_hash


# --------------------------------------------------------------------------
# Simultaneity
# --------------------------------------------------------------------------


def test_one_observation_becomes_one_frame(roots: tuple[Path, Path]) -> None:
    source = publish(roots, "one.parquet", [trade_at(at(seconds=0))])
    frames = frames_of(roots, source)

    assert len(frames) == 1
    assert len(frames[0].events) == 1


def test_several_rows_of_one_source_at_one_instant_share_a_frame(
    roots: tuple[Path, Path],
) -> None:
    source = publish(
        roots,
        "burst.parquet",
        [
            trade_at(at(seconds=0)),
            trade_at(at(seconds=1), price="4500.50"),
            trade_at(at(seconds=1), price="4500.75"),
            trade_at(at(seconds=1), price="4501.00"),
            trade_at(at(seconds=2)),
        ],
    )

    frames = frames_of(roots, source)

    assert [len(frame.events) for frame in frames] == [1, 3, 1]
    assert [event.row_ordinal for event in frames[1].events] == [1, 2, 3]


def test_a_trade_and_a_quote_at_one_instant_share_a_frame(roots: tuple[Path, Path]) -> None:
    trades = publish(roots, "t.parquet", [trade_at(at(seconds=0))])
    quotes = publish(roots, "q.parquet", [quote_at(at(seconds=0))], record_type=RecordType.QUOTE)

    frames = frames_of(roots, trades, quotes)

    assert len(frames) == 1
    assert {event.record_type for event in frames[0].events} == {
        RecordType.TRADE,
        RecordType.QUOTE,
    }


def test_a_trade_and_a_bar_at_one_instant_share_a_frame(roots: tuple[Path, Path]) -> None:
    """The bar's availability is its interval close, which is the trade's instant."""
    trades = publish(roots, "t.parquet", [trade_at(at(minutes=1))])
    bars = publish(roots, "b.parquet", [bar_starting(at(seconds=0))], record_type=RecordType.BAR)

    frames = frames_of(roots, trades, bars)

    assert len(frames) == 1
    assert frames[0].availability_time == at(minutes=1)
    assert len(frames[0].events) == 2


def test_a_trade_a_quote_and_a_bar_at_one_instant_share_a_frame(
    roots: tuple[Path, Path],
) -> None:
    trades = publish(roots, "t.parquet", [trade_at(at(minutes=1))])
    quotes = publish(roots, "q.parquet", [quote_at(at(minutes=1))], record_type=RecordType.QUOTE)
    bars = publish(roots, "b.parquet", [bar_starting(at(seconds=0))], record_type=RecordType.BAR)

    frames = frames_of(roots, trades, quotes, bars)

    assert len(frames) == 1
    assert {event.record_type for event in frames[0].events} == {
        RecordType.TRADE,
        RecordType.QUOTE,
        RecordType.BAR,
    }


def test_two_contracts_observed_at_one_instant_share_a_frame(roots: tuple[Path, Path]) -> None:
    es = publish(roots, "es.parquet", [trade_at(at(seconds=0))])
    nq = publish(roots, "nq.parquet", [trade_at(at(seconds=0))], contract=NQ_M2026)

    frames = frames_of(roots, es, nq)

    assert len(frames) == 1
    assert {event.contract for event in frames[0].events} == {ES_M2026, NQ_M2026}


def test_an_instant_is_never_split_across_two_frames(roots: tuple[Path, Path]) -> None:
    """The whole-boundary drain, tested where a greedy merge would fail it.

    The first source has three rows at the shared instant and the second has one.
    A merge that emitted as soon as any source ran out would produce two frames
    for one instant, and a consumer could act in between.
    """
    many = publish(
        roots,
        "many.parquet",
        [
            trade_at(at(seconds=0)),
            trade_at(at(seconds=0), price="4500.50"),
            trade_at(at(seconds=0), price="4500.75"),
        ],
    )
    one = publish(roots, "one.parquet", [quote_at(at(seconds=0))], record_type=RecordType.QUOTE)

    frames = frames_of(roots, many, one)

    assert len(frames) == 1
    assert len(frames[0].events) == 4


# --------------------------------------------------------------------------
# Ordering
# --------------------------------------------------------------------------


def test_frame_times_strictly_increase(roots: tuple[Path, Path]) -> None:
    first = publish(roots, "a.parquet", [trade_at(at(seconds=n)) for n in (0, 2, 4)])
    second = publish(
        roots,
        "b.parquet",
        [quote_at(at(seconds=n)) for n in (1, 2, 3)],
        record_type=RecordType.QUOTE,
    )

    times = [frame.availability_time for frame in frames_of(roots, first, second)]

    assert times == sorted(times)
    assert len(set(times)) == len(times)


def test_events_in_a_frame_are_ordered_by_manifest_digest_then_ordinal(
    roots: tuple[Path, Path],
) -> None:
    first = publish(
        roots, "a.parquet", [trade_at(at(seconds=0)), trade_at(at(seconds=0), price="4500.50")]
    )
    second = publish(roots, "b.parquet", [quote_at(at(seconds=0))], record_type=RecordType.QUOTE)

    frame = frames_of(roots, first, second)[0]
    keys = [frame_event_sort_key(event) for event in frame.events]

    assert keys == sorted(keys)
    assert keys == sorted([(first.canonical(), 0), (first.canonical(), 1), (second.canonical(), 0)])


def test_the_record_type_does_not_decide_the_order_within_a_frame(
    roots: tuple[Path, Path],
) -> None:
    """No trade-before-quote, quote-before-trade, or bar-priority rule exists.

    Which record type sorts first is decided entirely by the manifest digests,
    which are content hashes of provenance — so across a set of datasets both
    orders occur, and neither is a rule.
    """
    trades = publish(roots, "t.parquet", [trade_at(at(seconds=0))])
    quotes = publish(roots, "q.parquet", [quote_at(at(seconds=0))], record_type=RecordType.QUOTE)

    frame = frames_of(roots, trades, quotes)[0]
    expected_first = (
        RecordType.TRADE if trades.canonical() < quotes.canonical() else RecordType.QUOTE
    )

    assert frame.events[0].record_type is expected_first


def test_the_caller_order_of_sources_does_not_change_the_frames(
    roots: tuple[Path, Path],
) -> None:
    first = publish(roots, "a.parquet", [trade_at(at(seconds=n)) for n in (0, 1)])
    second = publish(
        roots,
        "b.parquet",
        [quote_at(at(seconds=n)) for n in (0, 1)],
        record_type=RecordType.QUOTE,
    )
    third = publish(roots, "c.parquet", [trade_at(at(seconds=1))], contract=NQ_M2026)

    orderings = [
        (first, second, third),
        (third, first, second),
        (second, third, first),
        (third, second, first),
    ]
    digests = [frame_digest(frames_of(roots, *order)) for order in orderings]

    assert all(digest == digests[0] for digest in digests)


# --------------------------------------------------------------------------
# Stream properties
# --------------------------------------------------------------------------


def test_every_selected_row_is_emitted_exactly_once(roots: tuple[Path, Path]) -> None:
    first = publish(roots, "a.parquet", [trade_at(at(seconds=n)) for n in (0, 0, 1, 2, 2, 2)])
    second = publish(
        roots,
        "b.parquet",
        [quote_at(at(seconds=n)) for n in (0, 1, 1, 3)],
        record_type=RecordType.QUOTE,
    )

    frames = frames_of(roots, first, second)
    emitted = [
        (event.source_manifest.canonical(), event.row_ordinal)
        for frame in frames
        for event in frame.events
    ]

    assert sorted(emitted) == sorted(
        [(first.canonical(), n) for n in range(6)] + [(second.canonical(), n) for n in range(4)]
    )
    assert len(set(emitted)) == len(emitted)


def test_identical_repeated_rows_are_both_emitted(roots: tuple[Path, Path]) -> None:
    """ADR-003 makes a repeated identical trade legitimate data, never a duplicate."""
    identical = trade_at(at(seconds=0))
    source = publish(roots, "repeat.parquet", [identical, identical, identical])

    frames = frames_of(roots, source)

    assert len(frames) == 1
    assert [event.row_ordinal for event in frames[0].events] == [0, 1, 2]
    assert [event.payload for event in frames[0].events] == [identical] * 3


def test_every_event_in_a_frame_carries_exactly_that_frames_time(
    roots: tuple[Path, Path],
) -> None:
    first = publish(roots, "a.parquet", [trade_at(at(seconds=n)) for n in (0, 1, 1, 2)])
    second = publish(
        roots,
        "b.parquet",
        [quote_at(at(seconds=n)) for n in (1, 2)],
        record_type=RecordType.QUOTE,
    )

    for frame in frames_of(roots, first, second):
        assert all(event.availability_time == frame.availability_time for event in frame.events)


def test_every_row_sharing_a_time_lands_in_the_one_frame_for_that_time(
    roots: tuple[Path, Path],
) -> None:
    first = publish(roots, "a.parquet", [trade_at(at(seconds=n)) for n in (0, 1, 1, 2)])
    second = publish(
        roots,
        "b.parquet",
        [quote_at(at(seconds=n)) for n in (1, 1, 2)],
        record_type=RecordType.QUOTE,
    )

    frames = frames_of(roots, first, second)
    by_time = {frame.availability_time: frame for frame in frames}

    assert len(by_time) == len(frames)
    assert len(by_time[at(seconds=1)].events) == 4


def test_frames_can_be_iterated_more_than_once_with_the_same_result(
    roots: tuple[Path, Path],
) -> None:
    catalog, storage = roots
    source = publish(roots, "a.parquet", [trade_at(at(seconds=n)) for n in (0, 1, 2)])
    replay = prepare_replay(
        catalog_root=catalog,
        storage_root=storage,
        config=ReplayConfig(manifest_hashes=(source,), replay_range=None),
    )

    first = list(replay.frames())
    second = list(replay.frames())

    assert first == second
    assert len(first) == 3


def test_two_frame_iterators_do_not_share_a_cursor(roots: tuple[Path, Path]) -> None:
    """There is no cursor to reset, so interleaved iteration is well defined."""
    catalog, storage = roots
    source = publish(roots, "a.parquet", [trade_at(at(seconds=n)) for n in (0, 1, 2)])
    replay = prepare_replay(
        catalog_root=catalog,
        storage_root=storage,
        config=ReplayConfig(manifest_hashes=(source,), replay_range=None),
    )

    left = replay.frames()
    right = replay.frames()
    next(left)

    assert next(right).availability_time == at(seconds=0)
    assert next(left).availability_time == at(seconds=1)


# --------------------------------------------------------------------------
# Bar look-ahead
# --------------------------------------------------------------------------


def test_a_bar_is_absent_from_a_range_ending_before_its_interval_closes(
    roots: tuple[Path, Path],
) -> None:
    bars = publish(roots, "b.parquet", [bar_starting(at(seconds=0))], record_type=RecordType.BAR)

    frames = frames_of(
        roots, bars, replay_range=ReplayRange(start=at(seconds=0), end=at(minutes=1))
    )

    assert frames == []


def test_a_bar_appears_exactly_at_its_interval_close(roots: tuple[Path, Path]) -> None:
    bars = publish(roots, "b.parquet", [bar_starting(at(seconds=0))], record_type=RecordType.BAR)

    frames = frames_of(
        roots,
        bars,
        replay_range=ReplayRange(start=at(minutes=1), end=at(minutes=2)),
    )

    assert [frame.availability_time for frame in frames] == [at(minutes=1)]


def test_a_bar_is_absent_one_microsecond_before_its_interval_closes(
    roots: tuple[Path, Path],
) -> None:
    bars = publish(roots, "b.parquet", [bar_starting(at(seconds=0))], record_type=RecordType.BAR)

    frames = frames_of(
        roots,
        bars,
        replay_range=ReplayRange(
            start=at(seconds=0), end=at(minutes=1) - timedelta(microseconds=1)
        ),
    )

    assert frames == []


def test_a_bar_is_never_emitted_at_its_interval_start(roots: tuple[Path, Path]) -> None:
    bar = bar_starting(at(seconds=0), interval=timedelta(minutes=5))
    bars = publish(roots, "b.parquet", [bar], record_type=RecordType.BAR)

    times = [frame.availability_time for frame in frames_of(roots, bars)]

    assert times == [at(minutes=5)]
    assert bar.interval_start not in times


# --------------------------------------------------------------------------
# Replay range
# --------------------------------------------------------------------------


def test_a_frame_exactly_at_the_range_start_is_included(roots: tuple[Path, Path]) -> None:
    source = publish(roots, "a.parquet", [trade_at(at(seconds=n)) for n in (0, 1, 2)])

    frames = frames_of(
        roots, source, replay_range=ReplayRange(start=at(seconds=1), end=at(seconds=5))
    )

    assert [frame.availability_time for frame in frames] == [at(seconds=1), at(seconds=2)]


def test_a_frame_exactly_at_the_range_end_is_excluded(roots: tuple[Path, Path]) -> None:
    source = publish(roots, "a.parquet", [trade_at(at(seconds=n)) for n in (0, 1, 2)])

    frames = frames_of(
        roots, source, replay_range=ReplayRange(start=at(seconds=0), end=at(seconds=2))
    )

    assert [frame.availability_time for frame in frames] == [at(seconds=0), at(seconds=1)]


def test_a_range_never_clips_a_frame_in_half(roots: tuple[Path, Path]) -> None:
    """Every event of an instant shares that instant, so a bound cannot separate them."""
    source = publish(
        roots,
        "a.parquet",
        [
            trade_at(at(seconds=1)),
            trade_at(at(seconds=1), price="4500.50"),
            trade_at(at(seconds=1), price="4500.75"),
        ],
    )

    included = frames_of(
        roots, source, replay_range=ReplayRange(start=at(seconds=1), end=at(seconds=2))
    )
    excluded = frames_of(
        roots, source, replay_range=ReplayRange(start=at(seconds=2), end=at(seconds=3))
    )

    assert len(included) == 1
    assert len(included[0].events) == 3
    assert excluded == []


def test_a_valid_range_selecting_nothing_is_an_empty_stream_not_an_error(
    roots: tuple[Path, Path],
) -> None:
    source = publish(roots, "a.parquet", [trade_at(at(seconds=0))])

    assert (
        frames_of(roots, source, replay_range=ReplayRange(start=at(days=1), end=at(days=2))) == []
    )


def test_row_ordinals_are_not_renumbered_after_a_range_filter(
    roots: tuple[Path, Path],
) -> None:
    """An ordinal is the row's place in an immutable artifact, not a run's counter."""
    source = publish(roots, "a.parquet", [trade_at(at(seconds=n)) for n in range(10)])

    frames = frames_of(
        roots, source, replay_range=ReplayRange(start=at(seconds=4), end=at(seconds=7))
    )
    ordinals = [event.row_ordinal for frame in frames for event in frame.events]

    assert ordinals == [4, 5, 6]


def test_a_range_covering_everything_matches_no_range_at_all(
    roots: tuple[Path, Path],
) -> None:
    source = publish(roots, "a.parquet", [trade_at(at(seconds=n)) for n in (0, 1, 2)])

    assert frame_digest(frames_of(roots, source)) == frame_digest(
        frames_of(roots, source, replay_range=ReplayRange(start=at(seconds=-1), end=at(seconds=10)))
    )
