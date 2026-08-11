"""Adversarial tests for the canonical semantic dataset hash.

The properties under attack are the ones a reproducible backtest cannot survive
losing:

* two datasets that mean different things must never share a hash;
* two artifacts that mean the same thing must share one, whatever vendor,
  provider, codec, or path produced them;
* row order is part of the meaning, and nothing sorts;
* no hidden ``source_index``, wall clock, or randomness is required.
"""

from __future__ import annotations

import os
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
from dataset_fixtures import (
    ES_M2026,
    ES_U2026,
    NQ_M2026,
    bar,
    quote,
    three_trades,
    trade,
)
from pydantic import ValidationError

from quant_research_terminal.data_import.contracts import ImportRecordType
from quant_research_terminal.domain.dataset_identity import (
    ManifestHash,
    PhysicalArtifactHash,
    RecordType,
    SemanticDatasetHash,
    SemanticDatasetHasher,
    SemanticEncodingError,
    dataset_time_bounds,
    require_record_type,
    schema_token,
    semantic_dataset_hash,
)
from quant_research_terminal.domain.futures_contract import FuturesContractId
from quant_research_terminal.domain.models import Trade
from quant_research_terminal.domain.trade_side import TradeSide


def digest(
    records: Any,
    *,
    record_type: RecordType = RecordType.TRADE,
    contract: FuturesContractId = ES_M2026,
) -> str:
    return semantic_dataset_hash(
        record_type=record_type, contract=contract, records=records
    ).canonical()


# --------------------------------------------------------------------------
# Injectivity: different meaning, different hash
# --------------------------------------------------------------------------


def test_the_same_dataset_hashes_the_same_every_time() -> None:
    rows = three_trades()
    assert digest(rows) == digest(rows)


def test_row_order_is_part_of_the_identity() -> None:
    rows = three_trades()
    assert digest(rows) != digest(list(reversed(rows)))


def test_a_changed_value_changes_the_hash() -> None:
    rows = three_trades()
    mutated = [*rows[:-1], trade(offset=2, price="9999.00")]
    assert digest(rows) != digest(mutated)


def test_a_different_contract_changes_the_hash() -> None:
    rows = three_trades()
    assert digest(rows, contract=ES_M2026) != digest(rows, contract=ES_U2026)
    assert digest(rows, contract=ES_M2026) != digest(rows, contract=NQ_M2026)


def test_a_different_record_type_changes_the_hash() -> None:
    assert digest([], record_type=RecordType.TRADE) != digest([], record_type=RecordType.QUOTE)
    assert digest([], record_type=RecordType.QUOTE) != digest([], record_type=RecordType.BAR)


def test_duplicate_rows_are_preserved_not_collapsed() -> None:
    """ADR-003 makes repeated identical trades legitimate; collapsing loses volume."""
    one = [trade()]
    two = [trade(), trade()]
    assert digest(one) != digest(two)


def test_cross_type_byte_aliasing_is_prevented_by_the_record_type_token() -> None:
    """Trade rows are 25 bytes and quote rows 40, and 25*8 == 40*5 == 200.

    Eight trades therefore occupy exactly as many row bytes as five quotes, so
    the stream must carry something outside the rows to tell them apart. The
    **record-type token in the header** is that something, and the second
    assertion isolates it: with the row-count footer stripped from both streams,
    the two are still distinct, which shows the token alone carries the
    guarantee. Attributing it jointly to the footer would be an overclaim — see
    :func:`test_a_truncated_sequence_cannot_produce_a_valid_digest` for what the
    footer actually buys.
    """
    eight_trades = [trade(offset=index) for index in range(8)]
    five_quotes = [quote(offset=index) for index in range(5)]
    assert digest(eight_trades, record_type=RecordType.TRADE) != digest(
        five_quotes, record_type=RecordType.QUOTE
    )

    def without_footer(records: list[Any], record_type: RecordType) -> bytes:
        hasher = SemanticDatasetHasher(record_type=record_type, contract=ES_M2026)
        for record in records:
            hasher.update(record)
        return hasher._digest.digest()  # noqa: SLF001

    assert without_footer(eight_trades, RecordType.TRADE) != without_footer(
        five_quotes, RecordType.QUOTE
    )


# --------------------------------------------------------------------------
# Neutrality: same meaning, same hash
# --------------------------------------------------------------------------


def test_the_vendor_alias_does_not_affect_the_hash() -> None:
    """The single most important neutrality property in the phase."""
    a = [trade(offset=index, symbol="ESM6") for index in range(3)]
    b = [trade(offset=index, symbol="ES M6") for index in range(3)]
    assert digest(a) == digest(b)


def test_the_side_vocabulary_is_hashed_by_meaning_not_spelling() -> None:
    """``parse_trade_side`` normalises, so decoded records are what count."""
    assert digest([trade(side=TradeSide.BUY)]) != digest([trade(side=TradeSide.SELL)])
    assert digest([trade(side=TradeSide.BUY)]) == digest([trade(side=TradeSide.BUY)])


def test_equal_prices_written_differently_hash_the_same() -> None:
    """Trailing zeros carry no information; the envelope already accepts them."""
    assert digest([trade(price="4500.25")]) == digest([trade(price="4500.250000")])


# --------------------------------------------------------------------------
# Encoding edge cases the per-field signedness table exists for
# --------------------------------------------------------------------------


def test_a_pre_epoch_timestamp_encodes_without_raising() -> None:
    """Epoch microseconds go negative before 1970; unsigned encoding would raise."""
    old = Trade(
        timestamp=datetime(1969, 7, 20, 20, 17, tzinfo=UTC),
        instrument_symbol="ESM6",
        price=Decimal("1.5"),
        size=Decimal(1),
        side=TradeSide.UNKNOWN,
    )
    assert len(digest([old])) == 64


def test_the_maximum_quantity_encodes_without_raising() -> None:
    """``2**64 - 1`` does not fit a *signed* 8-byte integer."""
    biggest = trade(size=2**64 - 1)
    assert len(digest([biggest])) == 64


def test_bars_hash_availability_time_and_interval() -> None:
    """ADR-002: the stored coordinate is availability time plus interval."""
    assert digest([bar(offset=0)], record_type=RecordType.BAR) != digest(
        [bar(offset=1)], record_type=RecordType.BAR
    )


# --------------------------------------------------------------------------
# Empty datasets
# --------------------------------------------------------------------------


def test_empty_datasets_for_different_contracts_differ() -> None:
    assert digest([], contract=ES_M2026) != digest([], contract=ES_U2026)


def test_an_empty_dataset_differs_from_a_populated_one() -> None:
    assert digest([]) != digest([trade()])


def test_time_bounds_of_an_empty_dataset_are_absent() -> None:
    assert dataset_time_bounds([]) == (None, None)


def test_time_bounds_scan_rather_than_taking_the_ends() -> None:
    """Storage never sorts, so an unsorted artifact is legal and common."""
    unsorted_rows = [trade(offset=5), trade(offset=1), trade(offset=9), trade(offset=3)]
    earliest, latest = dataset_time_bounds(unsorted_rows)
    assert earliest == unsorted_rows[1].timestamp
    assert latest == unsorted_rows[2].timestamp


# --------------------------------------------------------------------------
# Streaming and truncation
# --------------------------------------------------------------------------


def test_a_truncated_sequence_cannot_produce_a_valid_digest() -> None:
    hasher = SemanticDatasetHasher(record_type=RecordType.TRADE, contract=ES_M2026)
    hasher.update(trade())
    with pytest.raises(SemanticEncodingError, match="truncated"):
        hasher.finalize(expected_row_count=3)


def test_the_hasher_counts_what_it_was_fed() -> None:
    hasher = SemanticDatasetHasher(record_type=RecordType.TRADE, contract=ES_M2026)
    for row in three_trades():
        hasher.update(row)
    assert hasher.row_count == 3
    assert hasher.finalize(expected_row_count=3).canonical() == digest(three_trades())


def test_feeding_the_wrong_record_type_is_rejected() -> None:
    hasher = SemanticDatasetHasher(record_type=RecordType.TRADE, contract=ES_M2026)
    with pytest.raises(SemanticEncodingError, match="trade records"):
        hasher.update(quote())


# --------------------------------------------------------------------------
# Record type vocabulary
# --------------------------------------------------------------------------


def test_the_domain_and_import_record_vocabularies_are_disjoint_as_strings() -> None:
    """StrEnum members with equal values compare and hash equal across enums.

    Keeping the value sets disjoint is what stops an import-layer member from
    silently selecting a domain encoding.
    """
    assert {str(member) for member in RecordType} & {
        str(member) for member in ImportRecordType
    } == set()


def test_an_equal_valued_string_is_not_a_record_type() -> None:
    with pytest.raises(TypeError, match="RecordType is required"):
        require_record_type("trade")


def test_the_schema_token_is_derived_from_the_field_order() -> None:
    assert schema_token(RecordType.TRADE) == "trade/timestamp:price:size:side"


# --------------------------------------------------------------------------
# Hash value objects
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "value",
    ["", "abc", "A" * 64, "g" * 64, " " + "a" * 63, "a" * 63, "a" * 65],
)
def test_malformed_digests_are_rejected(value: str) -> None:
    with pytest.raises(ValidationError):
        SemanticDatasetHash(value=value)


def test_the_three_hash_types_are_distinct_even_with_equal_digests() -> None:
    """Three separate types, so one can never be passed where another is meant."""
    hexed = "a" * 64
    semantic: object = SemanticDatasetHash(value=hexed)
    physical: object = PhysicalArtifactHash(value=hexed)
    manifest: object = ManifestHash(value=hexed)
    assert semantic != physical
    assert physical != manifest
    assert type(semantic) is not type(physical)


def test_a_hash_model_copy_revalidates() -> None:
    value = SemanticDatasetHash(value="a" * 64)
    with pytest.raises(ValidationError):
        value.model_copy(update={"value": "NOT-A-HASH"})


# --------------------------------------------------------------------------
# Cross-process determinism
# --------------------------------------------------------------------------

_DETERMINISM_SCRIPT = """
import sys
sys.path.insert(0, r"{tests_dir}")
from dataset_fixtures import ES_M2026, three_trades, quote, bar
from quant_research_terminal.domain.dataset_identity import RecordType, semantic_dataset_hash

for record_type, rows in (
    (RecordType.TRADE, three_trades()),
    (RecordType.QUOTE, [quote(offset=i) for i in range(3)]),
    (RecordType.BAR, [bar(offset=i) for i in range(3)]),
    (RecordType.TRADE, []),
):
    print(record_type.value, semantic_dataset_hash(
        record_type=record_type, contract=ES_M2026, records=rows).canonical())
"""


def test_semantic_hashes_are_identical_across_hash_seeds_and_timezones() -> None:
    script = _DETERMINISM_SCRIPT.format(tests_dir=os.path.dirname(os.path.abspath(__file__)))
    outputs = set()
    for seed, zone in (
        ("0", "UTC"),
        ("1", "America/New_York"),
        ("42", "Asia/Tokyo"),
        ("random", "Australia/Eucla"),
    ):
        completed = subprocess.run(  # noqa: S603
            [sys.executable, "-c", script],
            capture_output=True,
            text=True,
            check=True,
            env={**os.environ, "PYTHONHASHSEED": seed, "TZ": zone},
        )
        outputs.add(completed.stdout)
    assert len(outputs) == 1


def test_the_semantic_hash_is_pinned() -> None:
    """A golden value.

    If this moves, the canonical encoding changed and every previously recorded
    dataset identity changed with it. That is a semantic event and must be a
    deliberate edit here, accompanied by a bump of the encoding format token.
    """
    rows = [
        trade(offset=0, price="4500.25", size=1, side=TradeSide.BUY, symbol="ESM6"),
        trade(offset=1, price="4500.50", size=2, side=TradeSide.SELL, symbol="ES M6"),
    ]
    assert digest(rows) == "735474d8ceb9b2294e6d448ca770fcf986eda91b66a7395c7c75556281497f77"


def test_the_pinned_hash_ignores_the_process_timezone() -> None:
    rows = [trade(offset=0), trade(offset=1)]
    first = digest(rows)
    original = os.environ.get("TZ")
    try:
        os.environ["TZ"] = "Pacific/Auckland"
        assert digest(rows) == first
    finally:
        if original is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = original


def test_hashing_requires_no_source_index() -> None:
    """``source_index`` is discarded at the pipeline boundary and is not needed.

    The records carry no such attribute, so a hash that required one could not
    be computed at all — this asserts the absence rather than assuming it.
    """
    rows = three_trades()
    assert not any(hasattr(record, "source_index") for record in rows)
    assert len(digest(rows)) == 64


def test_bar_interval_participates_in_the_hash() -> None:
    from quant_research_terminal.domain.models import Bar

    minute = bar()
    two_minute = Bar(
        instrument_symbol=minute.instrument_symbol,
        interval_start=minute.interval_start,
        interval=timedelta(minutes=2),
        open=minute.open,
        high=minute.high,
        low=minute.low,
        close=minute.close,
        volume=minute.volume,
    )
    assert digest([minute], record_type=RecordType.BAR) != digest(
        [two_minute], record_type=RecordType.BAR
    )
