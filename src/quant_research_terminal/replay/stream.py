"""Prepared sources, and the interleaving that turns them into a frame timeline.

Pure with respect to the filesystem: everything here operates on records that
have already been read and verified. Keeping this free of IO is what makes the
timeline properties — no event loss, no duplication, strictly increasing frame
times, complete simultaneity — testable without a catalog on disk.

A word on vocabulary, because one word is dangerous here: what follows is a k-way
*merge* in the sorting sense — several already-ordered sequences read in one
pass. It is emphatically not a merge in this platform's other sense, the ADR-013
one, where merging means reconciling two datasets' disagreeing claims into a
third. Nothing here combines, prefers, corrects, or discards an observation.
Every input row appears in the output exactly as it was stored; only their
interleaving is decided. :func:`frame_timeline` is named to keep that
distinction visible at every call site.

The interleaving
----------------
A k-way merge over per-source cursors, with one rule that makes it a *frame*
merge rather than an event merge:

    once the next availability time ``T`` is known, every source is drained of
    **all** its consecutive rows at ``T`` before anything is emitted.

Draining fully is what guarantees an instant is never split across two frames.
Emitting greedily — taking one row per source per step, or emitting as soon as
the first source runs out at ``T`` — would produce a stream that is still
deterministic and still monotonic, and would still be wrong: a consumer would
receive part of an information boundary, act, and then receive the rest. The
whole point of :class:`~...domain.replay.ReplayFrame` is that this is
impossible.

The drain is correct because each source's rows are validated non-decreasing
before it becomes a :class:`PreparedSource`, so a source's rows at ``T`` are
necessarily contiguous.

No heap is used. The source count in a replay configuration is small — one per
dataset, not one per row — so a linear scan for the minimum is the simpler
implementation, and simpler is the correct default until profiling says
otherwise. No speed claim is made either way: nothing here has been profiled.
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from datetime import datetime
from typing import NamedTuple, final

from quant_research_terminal.domain.dataset_identity import (
    ManifestHash,
    RecordType,
    SemanticDatasetHash,
)
from quant_research_terminal.domain.futures_contract import FuturesContractId
from quant_research_terminal.domain.replay import (
    ReplayEvent,
    ReplayFrame,
    ReplayPayload,
    ReplayRange,
    frame_event_sort_key,
)
from quant_research_terminal.replay.errors import (
    NonMonotonicReplaySourceError,
    ReplayInvariantError,
)


@final
class PreparedRow(NamedTuple):
    """One selected observation, with its position in the original artifact.

    ``row_ordinal`` is the row's zero-based index in the source artifact's
    **original** persisted logical row sequence, and it survives range
    filtering unchanged. Selecting rows 100 through 105 yields ordinals 100
    through 105, never 0 through 5: the ordinal is provenance — where this
    observation sits in an immutable artifact — not a counter for however many
    rows this particular run happened to want.

    It is emphatically **not** a market event id, a vendor sequence number, a
    trade id, or a correction id. No such identity is persisted (ADR-003), and
    presenting a positional index as one would invent an identity the data does
    not carry.

    ``availability_time`` is cached from the payload rather than recomputed per
    comparison, and it must equal ``payload.timestamp``. A ``NamedTuple`` cannot
    enforce that, so :func:`frame_timeline` checks it across every source before
    it yields anything — see that function for why the check has to happen there
    rather than when the event is finally built.
    """

    row_ordinal: int
    availability_time: datetime
    payload: ReplayPayload


@final
class PreparedSource(NamedTuple):
    """One verified dataset, reduced to the rows a run will actually replay.

    A snapshot. The rows were read from a copy whose bytes and semantics were
    both verified, and they are held in memory from that point on, so relocating,
    re-encoding, or corrupting the file afterwards cannot change what this run
    replays. The file path is deliberately not retained: nothing downstream may
    depend on where the bytes happened to live.

    ``contract`` and ``record_type`` are the manifest's claims, each checked
    against the version-3 artifact's own canonical metadata before the source is
    built — so they are the listed identity the artifact declares, never a
    vendor alias read off a row. Stating it that way rather than "taken from the
    artifact" matters: the two are equal because verification proves them equal,
    and a reader should be able to see which fact is doing the work.
    """

    manifest_hash: ManifestHash
    semantic_hash: SemanticDatasetHash
    contract: FuturesContractId
    record_type: RecordType
    rows: tuple[PreparedRow, ...]

    def selected_span(self) -> tuple[datetime, datetime] | None:
        """Return the first and last selected availability times, or ``None``.

        Reading the ends is sound here, unlike in
        :func:`~...dataset_identity.dataset_time_bounds`, precisely because a
        prepared source has already been proven non-decreasing. An empty
        selection has no span, which is what makes an empty source overlap
        nothing.
        """
        if not self.rows:
            return None
        return (self.rows[0].availability_time, self.rows[-1].availability_time)


def validate_source_monotonicity(
    rows: Sequence[PreparedRow], *, manifest_hash: ManifestHash
) -> None:
    """Reject a source whose availability times decrease in persisted order.

    Equal consecutive times are fine and common — several observations can share
    a microsecond, and that simultaneity is exactly what a frame preserves.

    Args:
        rows: every row of the artifact, in original persisted order. The whole
            artifact, not a selection: see
            :class:`~...replay.errors.NonMonotonicReplaySourceError`.
        manifest_hash: the source being validated, for the error message.

    Raises:
        NonMonotonicReplaySourceError: at the first decrease.
    """
    for previous, current in zip(rows, rows[1:], strict=False):
        if current.availability_time < previous.availability_time:
            raise NonMonotonicReplaySourceError(
                f"dataset {manifest_hash.canonical()} is not a valid replay source: row "
                f"{current.row_ordinal} became available at "
                f"{current.availability_time.isoformat()}, before row "
                f"{previous.row_ordinal} at {previous.availability_time.isoformat()}. "
                f"The persisted row order is part of this dataset's semantic identity and "
                f"is never sorted to make it replayable"
            )


def build_prepared_source(
    *,
    manifest_hash: ManifestHash,
    semantic_hash: SemanticDatasetHash,
    contract: FuturesContractId,
    record_type: RecordType,
    records: Sequence[ReplayPayload],
    replay_range: ReplayRange | None,
) -> PreparedSource:
    """Validate a whole artifact and reduce it to the rows a run will replay.

    Order of operations matters and is deliberate: the artifact is validated in
    full **first**, and only then filtered. A source that is out of order
    somewhere a particular run would not have looked is still not a valid replay
    source, and making that depend on the requested window would mean the same
    dataset was acceptable or unacceptable according to who asked.

    Args:
        records: every record of the artifact, in original persisted file order.
            Never sorted, here or anywhere downstream.

    Raises:
        NonMonotonicReplaySourceError: if availability times decrease.
    """
    rows = tuple(
        PreparedRow(
            row_ordinal=ordinal,
            availability_time=record.timestamp,
            payload=record,
        )
        for ordinal, record in enumerate(records)
    )
    validate_source_monotonicity(rows, manifest_hash=manifest_hash)

    selected = (
        rows
        if replay_range is None
        else tuple(row for row in rows if replay_range.contains(row.availability_time))
    )
    return PreparedSource(
        manifest_hash=manifest_hash,
        semantic_hash=semantic_hash,
        contract=contract,
        record_type=record_type,
        rows=selected,
    )


def frame_timeline(sources: Sequence[PreparedSource]) -> Iterator[ReplayFrame]:
    """Return the frame timeline of a set of prepared sources.

    Every call produces a fresh iterator with its own cursors. There is no cursor
    on the sources themselves and nothing to reset: replaying twice is calling
    this twice, and the two streams are equal.

    Guarantees, each covered by a unit test:

    * every selected row is emitted exactly once, under its original ordinal;
    * no source position is emitted twice;
    * frame availability times strictly increase;
    * every row sharing an availability time lands in the same frame;
    * every event in a frame has exactly that frame's availability time.

    **Not a generator function.** The sources are validated eagerly here and the
    iterator is built separately, so a malformed source raises from this call
    rather than from part-way through iteration. That distinction is the whole
    point: this module's sibling states that "a partial replay is worse than a
    failed one, because it looks like a replay", and a generator that validated
    lazily would hand a consumer three good frames and then raise.

    Raises:
        ReplayInvariantError: if a source violates an invariant
            :func:`build_prepared_source` would have enforced. Reachable only by
            hand-assembling a :class:`PreparedSource`, which a ``NamedTuple``
            cannot prevent.
    """
    _validate_prepared_sources(sources)
    return _interleave(sources)


def _validate_prepared_sources(sources: Sequence[PreparedSource]) -> None:
    """Re-establish, before any frame exists, what a hand-built source may break.

    Everything :func:`build_prepared_source` produces already satisfies both
    checks. They exist because :class:`PreparedSource` and :class:`PreparedRow`
    are ``NamedTuple``\\ s and therefore do not validate — an explicit, visible
    bypass, but one whose consequences here are silent rather than loud:

    * a **cached availability time that disagrees with its payload** steers every
      decision the interleave makes — the minimum selection, the whole-instant
      drain, the monotonicity of the result — and would then be caught only when
      :class:`~...domain.replay.ReplayEvent` is finally constructed, which is
      after earlier frames have already been handed to the consumer, and as a
      raw ``ValidationError`` rather than a :class:`ReplayError`;
    * **rows that are not non-decreasing** make replay time run backwards, which
      is the worst outcome this package can produce.

    Both are checked across *all* sources up front, so either failure costs the
    caller a raised exception and no frames at all.
    """
    for source in sources:
        for row in source.rows:
            if row.availability_time != row.payload.timestamp:
                raise ReplayInvariantError(
                    f"dataset {source.manifest_hash.canonical()} row {row.row_ordinal} caches "
                    f"availability {row.availability_time.isoformat()} but its payload became "
                    f"knowable at {row.payload.timestamp.isoformat()}. The cache must never "
                    f"become a second opinion about when an observation was available"
                )
        validate_source_monotonicity(source.rows, manifest_hash=source.manifest_hash)


def _interleave(sources: Sequence[PreparedSource]) -> Iterator[ReplayFrame]:
    """Interleave validated sources into one frame timeline."""
    cursors = [0] * len(sources)
    previous_time: datetime | None = None

    while True:
        next_time: datetime | None = None
        for index, source in enumerate(sources):
            cursor = cursors[index]
            if cursor < len(source.rows):
                candidate = source.rows[cursor].availability_time
                if next_time is None or candidate < next_time:
                    next_time = candidate
        if next_time is None:
            return

        # Belt and braces over the eager check above, and cheap: one comparison
        # per frame. It is not unreachable code standing in for a guarantee — it
        # is the invariant the *output* must satisfy, asserted where the output
        # is produced, so a future change to the interleave cannot break it
        # without failing here.
        if previous_time is not None and next_time <= previous_time:
            raise ReplayInvariantError(
                f"replay time moved from {previous_time.isoformat()} to "
                f"{next_time.isoformat()}: frame availability times must strictly increase"
            )
        previous_time = next_time

        events: list[ReplayEvent] = []
        for index, source in enumerate(sources):
            cursor = cursors[index]
            # Drain *all* consecutive rows at this instant, not one. Rows at one
            # time are contiguous because the source was validated
            # non-decreasing, so this collects the whole boundary.
            while cursor < len(source.rows) and source.rows[cursor].availability_time == next_time:
                row = source.rows[cursor]
                events.append(
                    ReplayEvent(
                        availability_time=row.availability_time,
                        record_type=source.record_type,
                        contract=source.contract,
                        source_manifest=source.manifest_hash,
                        row_ordinal=row.row_ordinal,
                        payload=row.payload,
                    )
                )
                cursor += 1
            cursors[index] = cursor

        events.sort(key=frame_event_sort_key)
        yield ReplayFrame(availability_time=next_time, events=tuple(events))
