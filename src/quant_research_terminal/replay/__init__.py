"""Deterministic replay: turning verified datasets into an availability timeline.

Answers one question — *what information becomes available next?* — and refuses
to answer any other. Replay does not decide what a strategy should do, does not
maintain market state, does not place orders, does not simulate fills, and does
not keep positions. None of those exist yet, and the boundary is enforced by the
architecture tests rather than by good intentions.

The layering is strict and one-directional::

    domain/replay.py   pure value types: range, config, event, frame
          ▲
    replay/            resolution, verification, snapshotting, frame merge
          │            (may read domain, data, and catalog)

Nothing in ``domain`` imports this package; nothing in ``data``, ``data_import``,
``catalog``, ``application``, or ``ui`` does either.

**Lineage is never consulted.** A replay input is an exact
:class:`~...dataset_identity.ManifestHash` and it is replayed as itself.
Publishing "B supersedes A" afterwards does not redirect a configuration that
pins A, does not change A's frames, and is not looked up at all — this package
imports no lineage navigation, so the guarantee is structural rather than a
policy someone has to remember. A run that pinned some bytes consumed those
bytes.

**Calendars and roll schedules are not required.** Replaying listed-contract
market data needs neither: no trading date is derived, no session open or close
is emitted, no halt or maintenance event is synthesised, and no contract is
switched automatically. Those are consumer concerns layered over an availability
timeline, not properties of one.
"""

from quant_research_terminal.domain.replay import (
    ReplayConfig,
    ReplayEvent,
    ReplayFrame,
    ReplayPayload,
    ReplayRange,
    frame_event_sort_key,
)
from quant_research_terminal.replay.errors import (
    AmbiguousReplayOverlapError,
    DuplicateReplaySemanticDatasetError,
    NonMonotonicReplaySourceError,
    ReplayArtifactVerificationError,
    ReplayError,
    ReplayInvariantError,
    UnsupportedReplaySchemaError,
)
from quant_research_terminal.replay.prepare import PreparedReplay, prepare_replay
from quant_research_terminal.replay.stream import (
    PreparedRow,
    PreparedSource,
    build_prepared_source,
    frame_timeline,
    validate_source_monotonicity,
)

__all__ = [
    "AmbiguousReplayOverlapError",
    "DuplicateReplaySemanticDatasetError",
    "NonMonotonicReplaySourceError",
    "PreparedReplay",
    "PreparedRow",
    "PreparedSource",
    "ReplayArtifactVerificationError",
    "ReplayConfig",
    "ReplayError",
    "ReplayEvent",
    "ReplayFrame",
    "ReplayInvariantError",
    "ReplayPayload",
    "ReplayRange",
    "UnsupportedReplaySchemaError",
    "build_prepared_source",
    "frame_event_sort_key",
    "frame_timeline",
    "prepare_replay",
    "validate_source_monotonicity",
]
