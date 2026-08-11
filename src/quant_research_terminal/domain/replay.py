"""The pure value types of a deterministic replay timeline.

Replay answers exactly one question — *what information becomes available
next?* — and this module holds the vocabulary for the answer. It owns no IO, no
catalog knowledge, and no policy about which datasets a run should consume; the
orchestration that resolves and verifies artifacts lives in
:mod:`quant_research_terminal.replay`, which depends on this module and not the
other way round.

The information-arrival unit is a frame, not an event
----------------------------------------------------
:class:`ReplayFrame` is the fundamental unit, and that is the anti-look-ahead
decision this module exists to make. Every observation that becomes available at
one instant is delivered in **one** frame.

The alternative — a flat stream of individual events — looks harmless and is
not. If a trade and a bar both become knowable at 10:01:00.000000, handing a
consumer the trade, letting it decide, and only then handing it the bar invents
a causal boundary the data does not contain. Nothing persisted proves the trade
was knowable first. A strategy that acted in that gap would be acting on a
sequencing artifact, and every timestamp in the resulting log would still look
correct. That is deterministic look-ahead, which is harder to find than
nondeterminism because it reproduces perfectly.

So a frame is atomic: a consumer receives all of it, or none of it, and there is
no point *inside* a frame at which a decision may be taken.

Frame event order is technical, never causal
--------------------------------------------
:attr:`ReplayFrame.events` is stored in the deterministic order
``(source_manifest.canonical(), row_ordinal)`` — see
:func:`frame_event_sort_key`. That order exists so a frame has one serialization,
one repr, and one comparable form in a test. It carries **no** claim about
exchange sequence, and the key is deliberately built from provenance rather than
from market meaning precisely so that nobody can mistake it for one: a manifest
digest is a content hash, and reading market causality out of a SHA-256 is
obviously wrong in a way that reading it out of "trades before quotes" is not.

There is deliberately no record-type priority, no contract priority, no provider
priority, and no path tie-break. Any of those would be a fabricated causal order
wearing the costume of a convention.

Availability time
-----------------
A replay event's ``availability_time`` is the instant the observation became
knowable, and it is always exactly the payload's own canonical ``timestamp``:

* :class:`~quant_research_terminal.domain.trade.Trade` — ``timestamp``
* :class:`~quant_research_terminal.domain.quote.Quote` — ``timestamp``
* :class:`~quant_research_terminal.domain.bar.Bar` — ``timestamp``, which is a
  derived property equal to ``interval_start + interval`` (ADR-002), so a bar is
  observable at its interval **close** and never at its start.

For trades and quotes that equality is a property of the **current data model**,
not a claim about market microstructure. The platform persists one timestamp per
trade and one per quote, so there is no second coordinate from which a delivery
or dissemination delay could be read. This is not an assertion that feed latency
is zero, nor that exchange occurrence time equals researcher availability time;
it is the honest statement that Phase 3 has no evidence with which to say
anything else. A richer contract carrying both coordinates would change the
mapping here and nothing else in this module.

The caller cannot lie about it either: :class:`ReplayEvent` rejects an
``availability_time`` that disagrees with its payload, so the field is a
convenience for ordering rather than an independently settable claim.
"""

from __future__ import annotations

from datetime import datetime
from typing import final

from pydantic import field_validator, model_validator

from quant_research_terminal.domain.common import CanonicalModel
from quant_research_terminal.domain.dataset_identity import (
    RECORD_PAYLOAD_TYPES,
    ManifestHash,
    RecordType,
    require_digest,
    require_record_type,
)
from quant_research_terminal.domain.futures_contract import (
    FuturesContractId,
    require_listed_contract,
)
from quant_research_terminal.domain.models import Bar, Quote, Trade
from quant_research_terminal.domain.time import validate_utc_datetime

#: The market observations replay delivers, and exactly those.
#:
#: Not :data:`typing.Any`, and not a replay-local copy of the market-data models:
#: a second Trade would be a second definition of what a trade is, and the two
#: would drift. Replay transports the canonical domain records unchanged.
type ReplayPayload = Trade | Quote | Bar


def frame_event_sort_key(event: ReplayEvent) -> tuple[str, int]:
    """Return the deterministic technical order key for an event in a frame.

    ``(source manifest digest, original row ordinal)``. Total over any set of
    events drawn from distinct source positions, because a manifest digest is
    unique per source and an ordinal is unique within one.

    **This order is not causal.** Every event in a frame became available at the
    same instant, and nothing persisted proves which was knowable first. The key
    exists so a frame has one canonical serialization for tests, logs, and
    equality — not so a consumer can infer sequence from it. See the module
    docstring.
    """
    return (event.source_manifest.canonical(), event.row_ordinal)


@final
class ReplayRange(CanonicalModel):
    """A half-open ``[start, end)`` window of availability time.

    Half-open rather than closed for the reason every interval in this platform
    is half-open (ADR-010's materialized windows, ADR-011's roll boundaries):
    adjacent ranges then tile without overlapping, so ``[a, b)`` followed by
    ``[b, c)`` covers everything once and nothing twice. A closed upper bound
    would replay whatever sits exactly at ``end`` in both halves of a split run.

    The bound is applied to whole frames as a consequence rather than as a
    separate rule: every event in a frame shares one availability time, so a
    frame is entirely inside the window or entirely outside it. A frame is never
    partially clipped.
    """

    start: datetime
    end: datetime

    @field_validator("start", "end", mode="before")
    @classmethod
    def _validate_bound(cls, value: object) -> datetime:
        return validate_utc_datetime(value)

    @model_validator(mode="after")
    def _validate_range(self) -> ReplayRange:
        if self.start >= self.end:
            raise ValueError(
                f"a replay range must be a non-empty half-open [start, end) window; got "
                f"start {self.start.isoformat()} and end {self.end.isoformat()}. An empty "
                f"or inverted range is a configuration mistake, not a run that emits nothing"
            )
        return self

    def contains(self, instant: datetime) -> bool:
        """Return whether an availability time falls inside ``[start, end)``."""
        return self.start <= validate_utc_datetime(instant) < self.end


@final
class ReplayConfig(CanonicalModel):
    """Which exact datasets to replay, and over which window.

    Inputs are **exact** :class:`~...dataset_identity.ManifestHash` values. Not a
    filename, not a vendor symbol, not a contract, not a dataset family, not
    "the latest revision", and not a continuous series — every one of those
    would make the same configuration mean different things as the catalog grew,
    which is the opposite of a reproducible research input.

    The manifest inputs are semantically an unordered **set**, so they are stored
    sorted by digest. Canonicalising rather than preserving caller order is what
    makes ``ReplayConfig(A, B, C)`` and ``ReplayConfig(C, A, B)`` the same
    configuration — and therefore produce the same frames — rather than two
    configurations that happen to agree. Sorting a set of content hashes is not
    an economic judgement about the data; it is the absence of one.

    Two structural invariants make bad input unconstructible rather than merely
    rejected somewhere later, following ADR-013's treatment of
    ``SupersedesRelation``:

    * **No sources at all** is a configuration error, and is deliberately
      distinct from a valid configuration whose range selects nothing. The first
      is a caller mistake; the second is a legitimate answer.
    * **A repeated manifest hash** is refused rather than silently deduplicated.
      A caller who supplies one twice expected two sources; collapsing them
      would answer a question that was not asked, and doubling them would
      double-count one dataset's every observation.
    """

    manifest_hashes: tuple[ManifestHash, ...]
    replay_range: ReplayRange | None

    @field_validator("manifest_hashes")
    @classmethod
    def _canonicalize(cls, value: tuple[ManifestHash, ...]) -> tuple[ManifestHash, ...]:
        # Exact types before anything reads ``canonical()``: a subclass may
        # override it and would then sort under one digest while carrying
        # another (ADR-009 D4, applied to the ordering key itself).
        for candidate in value:
            require_digest(candidate, ManifestHash)
        if not value:
            raise ValueError(
                "a replay configuration must name at least one dataset manifest; a run "
                "with no sources is a configuration error, not a run that emits nothing"
            )
        digests = [candidate.canonical() for candidate in value]
        if len(set(digests)) != len(digests):
            repeated = sorted({digest for digest in digests if digests.count(digest) > 1})
            raise ValueError(
                f"a manifest hash must not be supplied twice; replay inputs are a set of "
                f"distinct datasets and a repeat is never deduplicated silently. "
                f"Repeated: {repeated}"
            )
        return tuple(sorted(value, key=lambda candidate: candidate.canonical()))


@final
class ReplayEvent(CanonicalModel):
    """One market observation, with the provenance that places it in a timeline.

    Every field earns its place:

    ``availability_time``
        The information boundary. Validated to equal the payload's canonical
        timestamp, so it cannot be used to move an observation in time.
    ``record_type``
        An explicit, stable discriminator. A consumer should not have to
        ``isinstance``-test a union to learn what it received, and the field is
        checked against the payload's runtime type so it cannot mislabel one.
    ``contract``
        The canonical listed identity, taken from the verified version-3
        artifact's own metadata. Never a vendor alias: ``payload
        .instrument_symbol`` is provenance and two vendors may spell one
        contract differently, or one spelling may denote two contracts.
    ``source_manifest``
        The exact immutable dataset this observation came from. A future
        ``RunManifest`` pins its inputs with these.
    ``row_ordinal``
        The observation's zero-based position in the source artifact's original
        persisted logical row sequence.
    ``payload``
        The canonical market observation, unchanged.

    Deliberately **absent**: the semantic and physical hashes, the schema
    version, the provider, the vendor symbol, the file path, a calendar, a roll
    schedule, and a trading date. None is needed to place an observation in
    time, and each would either duplicate the manifest's own immutable claims or
    make replay output depend on something that is not the data.
    """

    availability_time: datetime
    record_type: RecordType
    contract: FuturesContractId
    source_manifest: ManifestHash
    row_ordinal: int
    payload: ReplayPayload

    @field_validator("availability_time", mode="before")
    @classmethod
    def _validate_availability(cls, value: object) -> datetime:
        return validate_utc_datetime(value)

    @field_validator("row_ordinal")
    @classmethod
    def _validate_ordinal(cls, value: int) -> int:
        # ``bool`` is an ``int`` subclass. Strict mode already refuses it, and
        # the explicit test costs nothing and does not depend on that staying
        # true: ``row_ordinal=True`` silently meaning row 1 would be a provenance
        # claim invented by a type coercion.
        if isinstance(value, bool):
            raise ValueError("a row ordinal must be an integer, not a boolean")
        if value < 0:
            raise ValueError(
                f"a row ordinal is a zero-based position in the source artifact's original "
                f"row sequence and cannot be negative; got {value}"
            )
        return value

    @model_validator(mode="after")
    def _validate_event(self) -> ReplayEvent:
        # Exact runtime types throughout. ``@final`` binds a type checker, not
        # the interpreter, and every one of these has been reached by a runtime
        # subclass in an earlier phase: a subclass carrying hidden state, or
        # overriding ``canonical()``, or overriding ``timestamp``, would pass an
        # ``isinstance`` check and then disagree with what was validated here.
        require_record_type(self.record_type)
        require_listed_contract(self.contract)
        require_digest(self.source_manifest, ManifestHash)

        expected_payload = RECORD_PAYLOAD_TYPES[self.record_type]
        if type(self.payload) is not expected_payload:
            raise ValueError(
                f"a {self.record_type.value} event carries a {expected_payload.__name__} "
                f"payload, exactly; got {type(self.payload).__name__}. The record type is a "
                f"discriminator a consumer relies on and must never mislabel the payload"
            )

        canonical_availability = self.payload.timestamp
        if self.availability_time != canonical_availability:
            raise ValueError(
                f"availability_time must be the payload's own canonical timestamp "
                f"{canonical_availability.isoformat()}; got "
                f"{self.availability_time.isoformat()}. Replay reports when an observation "
                f"became knowable and never relocates one in time"
            )
        return self


@final
class ReplayFrame(CanonicalModel):
    """Every observation that became available at one instant.

    The unit a consumer processes atomically. There is no supported way to act
    between two events of one frame, because the data contains no evidence that
    one was knowable before the other — see the module docstring.

    Invariants, all enforced on construction and re-enforced on
    :meth:`~...common.CanonicalModel.model_copy`:

    * non-empty — an instant at which nothing happened is not a frame;
    * every event is exactly a :class:`ReplayEvent`;
    * every event's ``availability_time`` equals the frame's;
    * no source position ``(source_manifest, row_ordinal)`` appears twice, so a
      single persisted row can never be delivered as two observations;
    * events are stored in :func:`frame_event_sort_key` order, which is checked
      rather than imposed — a caller assembling a frame out of order has almost
      certainly assembled it from the wrong place.
    """

    availability_time: datetime
    events: tuple[ReplayEvent, ...]

    @field_validator("availability_time", mode="before")
    @classmethod
    def _validate_availability(cls, value: object) -> datetime:
        return validate_utc_datetime(value)

    @model_validator(mode="after")
    def _validate_frame(self) -> ReplayFrame:
        if not self.events:
            raise ValueError(
                "a replay frame holds at least one observation; an instant at which "
                "nothing became available is not a frame and is simply not emitted"
            )

        for index, event in enumerate(self.events):
            if type(event) is not ReplayEvent:
                raise ValueError(
                    f"event {index} is a {type(event).__name__}, not exactly a ReplayEvent; "
                    f"a subclass may carry state these invariants cannot see"
                )
            if event.availability_time != self.availability_time:
                raise ValueError(
                    f"event {index} became available at "
                    f"{event.availability_time.isoformat()}, but this frame is the "
                    f"{self.availability_time.isoformat()} information boundary. A frame "
                    f"holds exactly the observations of one instant"
                )

        keys = [frame_event_sort_key(event) for event in self.events]
        if len(set(keys)) != len(keys):
            raise ValueError(
                "one source position must not appear twice in a frame; a persisted row is "
                "one observation and is never delivered as two"
            )
        if keys != sorted(keys):
            raise ValueError(
                "frame events must be in canonical technical order (source manifest digest, "
                "row ordinal). The order carries no causal meaning, but it must be the same "
                "order every time or two runs of one configuration are not comparable"
            )
        return self
