"""Regression tests for raw-record immutability and provider independence.

A raw record travels through validation and normalization unchanged. These
tests pin down that no stage — and no caller — can alter one after the fact,
including through the dictionary it was built from.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import pytest

from quant_research_terminal.data_import import ImportRecordType, RawRecord

BASE_TIME = datetime(2024, 1, 2, 12, 0, 0, tzinfo=UTC)


def _fields() -> dict[str, Any]:
    return {
        "timestamp": BASE_TIME,
        "instrument_symbol": "ES",
        "price": Decimal("5000.25"),
        "size": Decimal("2"),
        "side": "buy",
    }


def _record(fields: dict[str, Any] | None = None) -> RawRecord:
    return RawRecord(
        record_type=ImportRecordType.TRADE,
        source_index=0,
        provider_name="test",
        fields=_fields() if fields is None else fields,
    )


def test_record_attributes_are_frozen() -> None:
    record = _record()

    with pytest.raises(ValueError, match="frozen"):
        record.source_index = 5


def test_fields_mapping_rejects_item_assignment() -> None:
    record = _record()

    with pytest.raises(TypeError):
        record.fields["price"] = Decimal("1")  # type: ignore[index]


def test_mutating_the_caller_dict_does_not_change_the_record() -> None:
    # A provider that reuses one scratch dict while streaming must not be able
    # to corrupt records it already emitted.
    source = _fields()
    record = _record(source)

    source["price"] = Decimal("9999.99")
    source["side"] = "sell"

    assert record.value("price") == Decimal("5000.25")
    assert record.value("side") == "buy"


def test_reusing_one_dict_produces_independent_records() -> None:
    scratch = _fields()
    first = _record(scratch)

    scratch["price"] = Decimal("5001.00")
    second = _record(scratch)

    assert first.value("price") == Decimal("5000.25")
    assert second.value("price") == Decimal("5001.00")


def test_as_row_returns_a_copy_the_caller_may_mutate() -> None:
    record = _record()

    row = record.as_row()
    row["price"] = Decimal("1")

    assert record.value("price") == Decimal("5000.25")


def test_records_with_equal_fields_compare_equal() -> None:
    assert _record() == _record()


def test_no_value_is_converted_to_float() -> None:
    record = _record()

    for field_name in ("price", "size"):
        assert isinstance(record.value(field_name), Decimal)
        assert not isinstance(record.value(field_name), float)


def test_record_carries_no_provider_specific_state() -> None:
    # Provider independence: the record names its producer but exposes nothing
    # vendor-shaped, so validation never needs to know where data came from.
    record = _record()

    assert set(RawRecord.model_fields) == {
        "record_type",
        "source_index",
        "provider_name",
        "fields",
    }
    assert isinstance(record.provider_name, str)
