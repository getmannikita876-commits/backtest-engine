"""Regression tests for the Phase 1.3 hardened import contracts.

Each section pins down one decision that was made explicitly rather than
inherited: schema-version fatality, quantity positivity, duplicate-policy
typing, and the complete event ordering key.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
from pydantic import ValidationError

from quant_research_terminal.data_import import (
    EVENT_TYPE_PRECEDENCE,
    DuplicatePolicy,
    ImportBatch,
    ImportRecordType,
    RawRecord,
    ValidationSeverity,
    ValueValidator,
    event_ordering_key,
    event_type_precedence,
    validate_import_batch,
)
from quant_research_terminal.data_import import pipeline as pipeline_module
from quant_research_terminal.data_import.validation import batch_validation_pipeline
from quant_research_terminal.domain.models import Trade

BASE_TIME = datetime(2024, 1, 2, 12, 0, 0, tzinfo=UTC)


def _trade_row(**overrides: object) -> dict[str, Any]:
    row: dict[str, Any] = {
        "timestamp": BASE_TIME,
        "instrument_symbol": "ES",
        "price": Decimal("5000.25"),
        "size": Decimal("2"),
        "side": "buy",
    }
    row.update(overrides)
    return row


def _quote_row(**overrides: object) -> dict[str, Any]:
    row: dict[str, Any] = {
        "timestamp": BASE_TIME,
        "instrument_symbol": "ES",
        "bid": Decimal("5000.00"),
        "ask": Decimal("5000.25"),
        "bid_size": Decimal("1"),
        "ask_size": Decimal("1"),
    }
    row.update(overrides)
    return row


def _bar_row(**overrides: object) -> dict[str, Any]:
    row: dict[str, Any] = {
        "timestamp": BASE_TIME,
        "instrument_symbol": "ES",
        "interval": timedelta(minutes=1),
        "open": Decimal("4999.50"),
        "high": Decimal("5002.00"),
        "low": Decimal("4998.75"),
        "close": Decimal("5001.25"),
        "volume": Decimal("12345"),
    }
    row.update(overrides)
    return row


def _record(record_type: ImportRecordType, fields: dict[str, Any], index: int = 0) -> RawRecord:
    return RawRecord(
        record_type=record_type,
        source_index=index,
        provider_name="test",
        fields=fields,
    )


def _codes(issues: tuple[Any, ...]) -> list[str]:
    return [issue.code.value for issue in issues]


# --------------------------------------------------------------------------
# Schema version is batch-fatal
# --------------------------------------------------------------------------


def test_schema_mismatch_emits_exactly_one_fatal_issue() -> None:
    batch = ImportBatch(
        record_type=ImportRecordType.TRADE,
        rows=(_trade_row(), _trade_row(), _trade_row()),
        schema_version=999,
    )

    accepted, report = validate_import_batch(batch)

    assert accepted == []
    assert len(report.issues) == 1
    assert report.issues[0].severity is ValidationSeverity.FATAL
    assert report.issues[0].code.value == "unsupported_schema_version"
    assert report.issues[0].row_index is None
    assert report.fatal_count == 1
    assert report.success is False


def test_schema_mismatch_reports_every_row_as_rejected() -> None:
    batch = ImportBatch(
        record_type=ImportRecordType.TRADE,
        rows=(_trade_row(), _trade_row()),
        schema_version=999,
    )

    _, report = validate_import_batch(batch)

    assert report.total_rows == 2
    assert report.accepted_rows == 0
    assert report.rejected_rows == 2


def test_schema_mismatch_does_not_invoke_validators_or_normalizers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The rows were written against an unknown layout, so every field meaning
    # is in doubt. Neither stage may run against them.
    def _fail_validation(**_kwargs: object) -> object:
        raise AssertionError("validation must not run for a schema-fatal batch")

    class _FailingNormalizer:
        def normalize(self, record: RawRecord) -> object:
            raise AssertionError("normalization must not run for a schema-fatal batch")

    monkeypatch.setattr(pipeline_module, "batch_validation_pipeline", _fail_validation)
    monkeypatch.setattr(pipeline_module, "DefaultRecordNormalizer", _FailingNormalizer)

    batch = ImportBatch(
        record_type=ImportRecordType.TRADE,
        # Rows that would otherwise produce many findings.
        rows=(_trade_row(price=1.25), _trade_row(timestamp=datetime(2024, 1, 2, 12))),
        schema_version=999,
    )

    accepted, report = validate_import_batch(batch)

    assert accepted == []
    assert len(report.issues) == 1


def test_matching_schema_version_still_validates_normally(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Guards the test above from passing because validation never runs at all.
    calls: list[bool] = []

    def _tracked(**kwargs: Any) -> Any:
        calls.append(True)
        return batch_validation_pipeline(**kwargs)

    monkeypatch.setattr(pipeline_module, "batch_validation_pipeline", _tracked)

    accepted, report = validate_import_batch(
        ImportBatch(record_type=ImportRecordType.TRADE, rows=(_trade_row(),))
    )

    assert calls == [True]
    assert len(accepted) == 1
    assert report.success is True


# --------------------------------------------------------------------------
# Quote sizes: strictly positive, exact, no float
# --------------------------------------------------------------------------


@pytest.mark.parametrize("field_name", ["bid_size", "ask_size"])
def test_zero_quote_size_is_rejected(field_name: str) -> None:
    # The domain declares these PositiveDecimal (gt=0), so zero has no
    # representation and must be caught here rather than in the model.
    record = _record(ImportRecordType.QUOTE, _quote_row(**{field_name: Decimal("0")}))

    issues = ValueValidator().validate([record])

    assert _codes(issues) == ["negative_value"]
    assert issues[0].field_name == field_name
    assert "positive" in issues[0].message


@pytest.mark.parametrize("field_name", ["bid_size", "ask_size"])
def test_negative_quote_size_is_rejected(field_name: str) -> None:
    record = _record(ImportRecordType.QUOTE, _quote_row(**{field_name: Decimal("-1")}))

    issues = ValueValidator().validate([record])

    assert _codes(issues) == ["negative_value"]
    assert issues[0].field_name == field_name


@pytest.mark.parametrize("field_name", ["bid_size", "ask_size"])
def test_float_quote_size_is_rejected(field_name: str) -> None:
    record = _record(ImportRecordType.QUOTE, _quote_row(**{field_name: 1.0}))

    issues = ValueValidator().validate([record])

    assert _codes(issues) == ["negative_value"]
    assert "Decimal" in issues[0].message


def test_boolean_quote_size_is_rejected() -> None:
    # bool is an int subclass but is never a quantity.
    record = _record(ImportRecordType.QUOTE, _quote_row(bid_size=True))

    issues = ValueValidator().validate([record])

    assert _codes(issues) == ["negative_value"]


@pytest.mark.parametrize("size", [Decimal("1"), Decimal("100"), 7])
def test_valid_quote_sizes_are_accepted(size: object) -> None:
    record = _record(ImportRecordType.QUOTE, _quote_row(bid_size=size, ask_size=size))

    assert ValueValidator().validate([record]) == ()


def test_zero_quote_size_is_rejected_end_to_end_without_raising() -> None:
    # Before this rule existed the row reached the normalizer and raised a raw
    # pydantic error; it must now be a clean, diagnosable rejection.
    batch = ImportBatch(
        record_type=ImportRecordType.QUOTE,
        rows=(_quote_row(bid_size=Decimal("0")),),
    )

    accepted, report = validate_import_batch(batch)

    assert accepted == []
    assert report.success is False
    assert "negative_value" in _codes(report.issues)


# --------------------------------------------------------------------------
# Bar volume: strictly positive (decision, not inheritance)
# --------------------------------------------------------------------------


def test_zero_volume_bar_is_rejected() -> None:
    # Volume is a quantity in the canonical envelope, so an empty period is
    # represented by the absence of a bar. It reports a quantity code; it
    # previously reported non_decimal_price, which misdescribed a count.
    record = _record(ImportRecordType.BAR, _bar_row(volume=Decimal("0")))

    issues = ValueValidator().validate([record])

    assert _codes(issues) == ["negative_value"]
    assert "positive" in issues[0].message


def test_smallest_positive_volume_is_accepted() -> None:
    # Quantities are counts, so the smallest is one, not one storage tick.
    record = _record(ImportRecordType.BAR, _bar_row(volume=Decimal("1")))

    assert ValueValidator().validate([record]) == ()


def test_fractional_volume_is_rejected() -> None:
    record = _record(ImportRecordType.BAR, _bar_row(volume=Decimal("0.5")))

    assert _codes(ValueValidator().validate([record])) == ["non_integer_quantity"]


def test_volume_of_one_is_accepted() -> None:
    record = _record(ImportRecordType.BAR, _bar_row(volume=Decimal("1")))

    assert ValueValidator().validate([record]) == ()


def test_negative_volume_is_rejected() -> None:
    record = _record(ImportRecordType.BAR, _bar_row(volume=Decimal("-1")))

    assert _codes(ValueValidator().validate([record])) == ["negative_value"]


def test_zero_volume_bar_is_rejected_end_to_end() -> None:
    batch = ImportBatch(
        record_type=ImportRecordType.BAR,
        rows=(_bar_row(volume=Decimal("0")),),
    )

    accepted, report = validate_import_batch(batch)

    assert accepted == []
    assert report.success is False


# --------------------------------------------------------------------------
# Duplicate policy typing
# --------------------------------------------------------------------------


def test_enum_duplicate_policy_is_accepted() -> None:
    batch = ImportBatch(
        record_type=ImportRecordType.TRADE,
        rows=(_trade_row(),),
        duplicate_policy=DuplicatePolicy.KEEP_LAST,
    )

    assert batch.duplicate_policy is DuplicatePolicy.KEEP_LAST


def test_string_duplicate_policy_is_coerced_to_the_enum() -> None:
    batch = ImportBatch(
        record_type=ImportRecordType.TRADE,
        rows=(_trade_row(),),
        duplicate_policy="keep_first",  # type: ignore[arg-type]
    )

    # The validated field is always the enum, never a bare string.
    assert batch.duplicate_policy is DuplicatePolicy.KEEP_FIRST
    assert isinstance(batch.duplicate_policy, DuplicatePolicy)


def test_uppercase_string_duplicate_policy_is_coerced() -> None:
    batch = ImportBatch(
        record_type=ImportRecordType.TRADE,
        rows=(_trade_row(),),
        duplicate_policy="KEEP_LAST",  # type: ignore[arg-type]
    )

    assert batch.duplicate_policy is DuplicatePolicy.KEEP_LAST


def test_invalid_duplicate_policy_string_is_rejected() -> None:
    with pytest.raises(ValidationError, match="unsupported duplicate policy"):
        ImportBatch(
            record_type=ImportRecordType.TRADE,
            rows=(_trade_row(),),
            duplicate_policy="keep_best",  # type: ignore[arg-type]
        )


def test_default_duplicate_policy_is_the_enum() -> None:
    batch = ImportBatch(record_type=ImportRecordType.TRADE, rows=(_trade_row(),))

    assert batch.duplicate_policy is DuplicatePolicy.REJECT


# --------------------------------------------------------------------------
# Deterministic ordering key
# --------------------------------------------------------------------------


def test_event_type_precedence_orders_quote_before_trade_before_bar() -> None:
    # A quote publishes book state, a trade executes against it, and a bar
    # summarises a period that has already closed.
    assert (
        event_type_precedence(ImportRecordType.QUOTE)
        < event_type_precedence(ImportRecordType.TRADE)
        < event_type_precedence(ImportRecordType.BAR)
    )


def test_every_record_type_has_a_precedence() -> None:
    assert set(EVENT_TYPE_PRECEDENCE) == set(ImportRecordType)


def test_ordering_key_is_timestamp_then_type_then_source_index() -> None:
    key = event_ordering_key(
        record_type=ImportRecordType.TRADE, timestamp=BASE_TIME, source_index=7
    )

    assert key == (BASE_TIME, event_type_precedence(ImportRecordType.TRADE), 7)


def test_timestamp_dominates_event_type() -> None:
    later_quote = event_ordering_key(
        record_type=ImportRecordType.QUOTE,
        timestamp=BASE_TIME + timedelta(microseconds=1),
        source_index=0,
    )
    earlier_bar = event_ordering_key(
        record_type=ImportRecordType.BAR, timestamp=BASE_TIME, source_index=99
    )

    assert earlier_bar < later_quote


def test_event_type_dominates_source_index() -> None:
    quote_late_in_file = event_ordering_key(
        record_type=ImportRecordType.QUOTE, timestamp=BASE_TIME, source_index=99
    )
    trade_early_in_file = event_ordering_key(
        record_type=ImportRecordType.TRADE, timestamp=BASE_TIME, source_index=0
    )

    assert quote_late_in_file < trade_early_in_file


def test_source_index_breaks_remaining_ties() -> None:
    first = event_ordering_key(
        record_type=ImportRecordType.TRADE, timestamp=BASE_TIME, source_index=0
    )
    second = event_ordering_key(
        record_type=ImportRecordType.TRADE, timestamp=BASE_TIME, source_index=1
    )

    assert first < second


def test_mixed_event_types_sort_by_the_full_key() -> None:
    keys = [
        event_ordering_key(record_type=ImportRecordType.BAR, timestamp=BASE_TIME, source_index=0),
        event_ordering_key(record_type=ImportRecordType.TRADE, timestamp=BASE_TIME, source_index=1),
        event_ordering_key(record_type=ImportRecordType.QUOTE, timestamp=BASE_TIME, source_index=2),
    ]

    ordered = sorted(keys)

    assert [key[1] for key in ordered] == [
        event_type_precedence(ImportRecordType.QUOTE),
        event_type_precedence(ImportRecordType.TRADE),
        event_type_precedence(ImportRecordType.BAR),
    ]


def test_accepted_batch_output_uses_the_ordering_key() -> None:
    batch = ImportBatch(
        record_type=ImportRecordType.TRADE,
        rows=(
            _trade_row(timestamp=BASE_TIME + timedelta(seconds=2), price=Decimal("3")),
            _trade_row(timestamp=BASE_TIME, price=Decimal("1")),
            _trade_row(timestamp=BASE_TIME + timedelta(seconds=1), price=Decimal("2")),
        ),
    )

    accepted, report = validate_import_batch(batch)
    trades = [record for record in accepted if isinstance(record, Trade)]

    assert report.success is True
    assert [trade.price for trade in trades] == [Decimal("1"), Decimal("2"), Decimal("3")]
