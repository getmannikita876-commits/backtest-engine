"""The pure replay value types: range, config, event, and frame.

The properties under attack:

* an availability time is exact UTC, and a caller cannot relocate an observation
  in time by disagreeing with its payload;
* a replay range is half-open, so a boundary instant belongs to exactly one side;
* a configuration is a canonical *set* of exact manifest hashes, so caller order
  cannot become hidden semantics and a repeat is never silently collapsed;
* a frame is one whole information boundary, in one deterministic technical
  order, with no source position delivered twice;
* none of it can be forged through ``model_copy`` or a runtime subclass.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal
from enum import StrEnum

import pytest
from dataset_fixtures import ES_M2026, NQ_M2026
from pydantic import ValidationError
from replay_fixtures import at, bar_starting, quote_at, trade_at

from quant_research_terminal.domain.dataset_identity import (
    ManifestHash,
    RecordType,
    SemanticDatasetHash,
)
from quant_research_terminal.domain.futures_contract import FuturesProduct, Venue
from quant_research_terminal.domain.models import Trade
from quant_research_terminal.domain.replay import (
    ReplayConfig,
    ReplayEvent,
    ReplayFrame,
    ReplayPayload,
    ReplayRange,
    frame_event_sort_key,
)
from quant_research_terminal.domain.trade_side import TradeSide

HASH_A = ManifestHash(value="a" * 64)
HASH_B = ManifestHash(value="b" * 64)
HASH_C = ManifestHash(value="c" * 64)


def event(
    *,
    payload: ReplayPayload | None = None,
    record_type: RecordType = RecordType.TRADE,
    source: ManifestHash = HASH_A,
    ordinal: int = 0,
    contract: object = ES_M2026,
    availability: datetime | None = None,
) -> ReplayEvent:
    record = trade_at(at(seconds=0)) if payload is None else payload
    return ReplayEvent(
        availability_time=record.timestamp if availability is None else availability,
        record_type=record_type,
        contract=contract,  # type: ignore[arg-type]
        source_manifest=source,
        row_ordinal=ordinal,
        payload=record,
    )


# --------------------------------------------------------------------------
# ReplayRange
# --------------------------------------------------------------------------


def test_a_replay_range_is_half_open() -> None:
    window = ReplayRange(start=at(seconds=0), end=at(seconds=10))

    assert window.contains(at(seconds=0))
    assert window.contains(at(seconds=9, microseconds=999999))
    assert not window.contains(at(seconds=10))
    assert not window.contains(at(seconds=-1, microseconds=999999))


def test_a_replay_range_rejects_a_naive_datetime() -> None:
    with pytest.raises(ValidationError):
        ReplayRange(start=datetime(2026, 6, 1, 10, 0), end=at(seconds=10))  # noqa: DTZ001


def test_a_replay_range_rejects_a_non_utc_datetime() -> None:
    eastern = timezone(timedelta(hours=-5))
    with pytest.raises(ValidationError):
        ReplayRange(start=datetime(2026, 6, 1, 5, 0, tzinfo=eastern), end=at(seconds=10))


def test_a_zero_offset_bound_is_normalized_to_utc_rather_than_kept_as_written() -> None:
    """Frame grouping compares instants, so the stored zone must be one zone.

    A ``timezone(timedelta(0), "GMT")`` denotes the same instants as UTC — the
    stdlib says so, since ``timezone`` equality is offset-only and the name is
    decoration. It is therefore accepted, and normalized, so that every
    availability time in a replay carries ``UTC`` itself and two equal instants
    are never distinguished by how a caller happened to spell the zone.
    """
    window = ReplayRange(
        start=datetime(2026, 6, 1, 10, 0, tzinfo=timezone(timedelta(0), "GMT")),
        end=at(seconds=10),
    )
    assert window.start.tzinfo is UTC
    assert window.start == at(seconds=0)


@pytest.mark.parametrize("end_offset", [0, -1])
def test_a_replay_range_rejects_an_empty_or_inverted_window(end_offset: int) -> None:
    with pytest.raises(ValidationError):
        ReplayRange(start=at(seconds=0), end=at(seconds=end_offset))


def test_a_replay_range_copy_is_revalidated() -> None:
    window = ReplayRange(start=at(seconds=0), end=at(seconds=10))
    with pytest.raises(ValidationError):
        window.model_copy(update={"end": at(seconds=-5)})


# --------------------------------------------------------------------------
# ReplayConfig
# --------------------------------------------------------------------------


def test_a_configuration_with_no_sources_is_unconstructible() -> None:
    with pytest.raises(ValidationError):
        ReplayConfig(manifest_hashes=(), replay_range=None)


def test_a_repeated_manifest_hash_is_refused_rather_than_deduplicated() -> None:
    with pytest.raises(ValidationError) as failure:
        ReplayConfig(manifest_hashes=(HASH_A, HASH_A), replay_range=None)

    assert "twice" in str(failure.value)


def test_caller_order_is_canonicalized_into_one_configuration() -> None:
    orderings = [
        (HASH_A, HASH_B, HASH_C),
        (HASH_C, HASH_A, HASH_B),
        (HASH_B, HASH_C, HASH_A),
        (HASH_C, HASH_B, HASH_A),
    ]
    configs = [ReplayConfig(manifest_hashes=order, replay_range=None) for order in orderings]

    assert all(config.manifest_hashes == (HASH_A, HASH_B, HASH_C) for config in configs)
    assert len(set(configs)) == 1


def test_a_replay_range_is_optional() -> None:
    assert ReplayConfig(manifest_hashes=(HASH_A,), replay_range=None).replay_range is None


def test_a_configuration_rejects_a_foreign_digest_type() -> None:
    """A semantic hash is not a manifest hash, even with the same 64 characters."""
    with pytest.raises(ValidationError):
        ReplayConfig(
            manifest_hashes=(SemanticDatasetHash(value="a" * 64),),  # type: ignore[arg-type]
            replay_range=None,
        )


def test_a_configuration_rejects_a_hostile_manifest_hash_subclass() -> None:
    """A subclass may override ``canonical()`` and sort under a digest it does not carry.

    ``TypeError`` rather than ``ValidationError``, matching the established
    exact-type guards: ``require_digest`` raises it and Pydantic converts only
    ``ValueError`` and ``AssertionError``, so the precise diagnosis survives to
    the caller instead of being flattened into a field error.
    """

    class ForgedHash(ManifestHash):  # type: ignore[misc]
        def canonical(self) -> str:
            return "0" * 64

    with pytest.raises(TypeError, match="exactly"):
        ReplayConfig(manifest_hashes=(ForgedHash(value="f" * 64),), replay_range=None)


def test_a_configuration_copy_is_revalidated() -> None:
    config = ReplayConfig(manifest_hashes=(HASH_A, HASH_B), replay_range=None)
    with pytest.raises(ValidationError):
        config.model_copy(update={"manifest_hashes": (HASH_A, HASH_A)})


def test_a_configuration_copy_recanonicalizes_the_input_set() -> None:
    config = ReplayConfig(manifest_hashes=(HASH_A,), replay_range=None)
    recopied = config.model_copy(update={"manifest_hashes": (HASH_C, HASH_A, HASH_B)})
    assert recopied.manifest_hashes == (HASH_A, HASH_B, HASH_C)


# --------------------------------------------------------------------------
# ReplayEvent
# --------------------------------------------------------------------------


def test_a_trade_event_takes_its_availability_from_the_trade_timestamp() -> None:
    trade = trade_at(at(seconds=5))
    assert event(payload=trade).availability_time == at(seconds=5)


def test_a_quote_event_takes_its_availability_from_the_quote_timestamp() -> None:
    quote = quote_at(at(seconds=5))
    assert event(payload=quote, record_type=RecordType.QUOTE).availability_time == at(seconds=5)


def test_a_bar_event_takes_its_availability_from_the_interval_close() -> None:
    """The whole ADR-002 property, restated where replay depends on it."""
    bar = bar_starting(at(seconds=0), interval=timedelta(minutes=1))
    replayed = event(payload=bar, record_type=RecordType.BAR)

    assert replayed.availability_time == at(minutes=1)
    assert replayed.availability_time != bar.interval_start


def test_a_bar_event_cannot_be_declared_available_at_its_interval_start() -> None:
    bar = bar_starting(at(seconds=0), interval=timedelta(minutes=1))
    with pytest.raises(ValidationError):
        event(payload=bar, record_type=RecordType.BAR, availability=bar.interval_start)


def test_a_bar_event_cannot_be_declared_available_one_microsecond_early() -> None:
    bar = bar_starting(at(seconds=0), interval=timedelta(minutes=1))
    with pytest.raises(ValidationError):
        event(
            payload=bar,
            record_type=RecordType.BAR,
            availability=at(minutes=1) - timedelta(microseconds=1),
        )


def test_a_record_type_that_mislabels_its_payload_is_rejected() -> None:
    with pytest.raises(ValidationError):
        event(payload=trade_at(at(seconds=0)), record_type=RecordType.QUOTE)
    with pytest.raises(ValidationError):
        event(payload=quote_at(at(seconds=0)), record_type=RecordType.BAR)


def test_an_availability_time_disagreeing_with_the_payload_is_rejected() -> None:
    with pytest.raises(ValidationError):
        event(payload=trade_at(at(seconds=5)), availability=at(seconds=4))


def test_a_negative_row_ordinal_is_rejected() -> None:
    with pytest.raises(ValidationError):
        event(ordinal=-1)


def test_a_boolean_row_ordinal_is_rejected() -> None:
    with pytest.raises(ValidationError):
        event(ordinal=True)


def test_a_product_is_not_an_acceptable_contract() -> None:
    with pytest.raises(ValidationError):
        event(contract=FuturesProduct(venue=Venue(code="CME"), root="ES"))


def test_a_hostile_payload_subclass_is_rejected() -> None:
    """A subclass overriding ``timestamp`` would relocate an observation in time."""

    class ForgedTrade(Trade):
        @property  # type: ignore[misc]
        def timestamp(self) -> datetime:  # type: ignore[override]
            return at(seconds=99)

    forged = ForgedTrade(
        timestamp=at(seconds=0),
        instrument_symbol="ESM6",
        price=Decimal("1"),
        size=Decimal(1),
        side=TradeSide.BUY,
    )
    with pytest.raises(ValidationError):
        ReplayEvent(
            availability_time=at(seconds=99),
            record_type=RecordType.TRADE,
            contract=ES_M2026,
            source_manifest=HASH_A,
            row_ordinal=0,
            payload=forged,
        )


def test_an_event_rejects_a_hostile_manifest_hash_subclass() -> None:
    class ForgedHash(ManifestHash):  # type: ignore[misc]
        def canonical(self) -> str:
            return "0" * 64

    with pytest.raises(TypeError, match="exactly"):
        event(source=ForgedHash(value="f" * 64))


def test_an_event_rejects_a_foreign_record_type_enum_member() -> None:
    """A ``StrEnum`` member of another enum compares equal to this one's value."""

    class ForeignRecordType(StrEnum):
        TRADE = "trade"

    with pytest.raises((TypeError, ValidationError)):
        event(record_type=ForeignRecordType.TRADE)  # type: ignore[arg-type]


def test_an_event_copy_cannot_bypass_the_availability_invariant() -> None:
    original = event(payload=trade_at(at(seconds=5)))
    with pytest.raises(ValidationError):
        original.model_copy(update={"availability_time": at(seconds=1)})


def test_an_event_copy_cannot_bypass_the_record_type_invariant() -> None:
    original = event(payload=trade_at(at(seconds=5)))
    with pytest.raises(ValidationError):
        original.model_copy(update={"record_type": RecordType.BAR})


def test_the_frame_sort_key_is_the_manifest_digest_and_the_ordinal() -> None:
    assert frame_event_sort_key(event(source=HASH_B, ordinal=7)) == ("b" * 64, 7)


# --------------------------------------------------------------------------
# ReplayFrame
# --------------------------------------------------------------------------


def test_an_empty_frame_is_unconstructible() -> None:
    with pytest.raises(ValidationError):
        ReplayFrame(availability_time=at(seconds=0), events=())


def test_a_frame_rejects_an_event_from_another_instant() -> None:
    with pytest.raises(ValidationError):
        ReplayFrame(
            availability_time=at(seconds=0),
            events=(event(payload=trade_at(at(seconds=1))),),
        )


def test_a_frame_rejects_a_repeated_source_position() -> None:
    twice = event(source=HASH_A, ordinal=3)
    with pytest.raises(ValidationError):
        ReplayFrame(availability_time=at(seconds=0), events=(twice, twice))


def test_a_frame_rejects_events_that_are_not_in_canonical_technical_order() -> None:
    first = event(source=HASH_A, ordinal=0)
    second = event(source=HASH_B, ordinal=0)
    with pytest.raises(ValidationError):
        ReplayFrame(availability_time=at(seconds=0), events=(second, first))


def test_a_frame_accepts_events_in_canonical_technical_order() -> None:
    first = event(source=HASH_A, ordinal=0)
    second = event(source=HASH_B, ordinal=0)
    frame = ReplayFrame(availability_time=at(seconds=0), events=(first, second))
    assert frame.events == (first, second)


def test_the_technical_order_ignores_record_type_and_contract() -> None:
    """No trade-before-quote rule, and no cross-contract priority, exists.

    Both events are simultaneous; the only thing that decides their stored order
    is provenance, which carries no market meaning.
    """
    trade_event = event(payload=trade_at(at(seconds=0)), source=HASH_B, contract=ES_M2026)
    quote_event = event(
        payload=quote_at(at(seconds=0)),
        record_type=RecordType.QUOTE,
        source=HASH_A,
        contract=NQ_M2026,
    )
    frame = ReplayFrame(availability_time=at(seconds=0), events=(quote_event, trade_event))

    assert [item.record_type for item in frame.events] == [RecordType.QUOTE, RecordType.TRADE]


def test_a_frame_rejects_a_hostile_event_subclass() -> None:
    class ForgedEvent(ReplayEvent):  # type: ignore[misc]
        pass

    forged = ForgedEvent(
        availability_time=at(seconds=0),
        record_type=RecordType.TRADE,
        contract=ES_M2026,
        source_manifest=HASH_A,
        row_ordinal=0,
        payload=trade_at(at(seconds=0)),
    )
    with pytest.raises(ValidationError):
        ReplayFrame(availability_time=at(seconds=0), events=(forged,))


def test_a_frame_copy_is_revalidated() -> None:
    frame = ReplayFrame(availability_time=at(seconds=0), events=(event(),))
    with pytest.raises(ValidationError):
        frame.model_copy(update={"events": ()})


def test_a_frame_is_frozen() -> None:
    frame = ReplayFrame(availability_time=at(seconds=0), events=(event(),))
    with pytest.raises(ValidationError):
        frame.availability_time = at(seconds=1)
    assert isinstance(frame.events, tuple)


def test_a_frame_time_must_be_utc() -> None:
    with pytest.raises(ValidationError):
        ReplayFrame(availability_time=datetime(2026, 6, 1, 10, 0), events=(event(),))  # noqa: DTZ001


def test_the_replay_types_reject_unknown_fields() -> None:
    """``extra="forbid"`` throughout, so a retired keyword is never ignored."""
    with pytest.raises(ValidationError):
        ReplayRange(start=at(seconds=0), end=at(seconds=1), speed=2)  # type: ignore[call-arg]
    with pytest.raises(ValidationError):
        ReplayConfig(manifest_hashes=(HASH_A,), replay_range=None, seed=1)  # type: ignore[call-arg]


def test_the_configuration_carries_no_seed_calendar_or_clock_field() -> None:
    """Nothing in a replay configuration can make one run differ from another."""
    assert set(ReplayConfig.model_fields) == {"manifest_hashes", "replay_range"}


def test_a_replay_event_carries_no_path_provider_or_physical_hash_field() -> None:
    assert set(ReplayEvent.model_fields) == {
        "availability_time",
        "record_type",
        "contract",
        "source_manifest",
        "row_ordinal",
        "payload",
    }


def test_the_utc_range_boundary_is_microsecond_exact() -> None:
    """The precision claim is exercised, not merely stated."""
    window = ReplayRange(start=at(seconds=0), end=at(seconds=1))
    assert window.contains(at(seconds=0, microseconds=999999))
    assert not window.contains(at(seconds=1, microseconds=0))
    assert at(seconds=1).tzinfo is UTC
