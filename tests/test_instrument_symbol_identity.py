"""Regression tests for the instrument-symbol coercion defect (ADR-009, D1).

The defect, reproduced before the fix
-------------------------------------
``DefaultRecordNormalizer`` built a domain object with
``str(record.value("instrument_symbol"))`` while ``record_identity`` compared
the **raw** decoded value. Two records carrying ``None`` and ``"None"``
therefore had different *import* identities but the same *domain* instrument,
so two bars for one period with **different volumes** were accepted together::

    rows submitted : 2   (same period, volumes 5 and 9)
    rows accepted  : 2
    issues         : none
    success flag   : True

That is the silent double-counting ADR-005 exists to prevent, reached through
the instrument field rather than the period fields.

These tests assert the defect stays fixed, and — just as importantly — that the
fix did not tighten the legacy symbol field beyond rejecting values that name
no instrument at all.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest

from quant_research_terminal.data_import.contracts import (
    ImportBatch,
    ImportRecordType,
    ValidationIssueCode,
)
from quant_research_terminal.data_import.instrument_semantics import (
    InstrumentSymbolViolation,
    check_instrument_symbol,
    require_instrument_symbol,
)
from quant_research_terminal.data_import.normalization import DefaultRecordNormalizer
from quant_research_terminal.data_import.pipeline import validate_import_batch
from quant_research_terminal.data_import.raw_record import RawRecord
from quant_research_terminal.data_import.validation import (
    InstrumentSymbolValidator,
    record_identity,
)

BASE_TIME = datetime(2024, 3, 4, 14, 30, tzinfo=UTC)
INTERVAL = timedelta(minutes=1)

#: Values that are not strings and therefore name no instrument. ``str()``
#: turns each into a plausible-looking symbol that collides with the genuine
#: text of the same spelling.
NON_STRING_VALUES: list[Any] = [None, 123, True, False, 4.5, ["ES"], ("ES",), {"s": "ES"}]

#: Strings carrying no non-whitespace character. Each satisfies the domain
#: model's ``min_length=1`` while identifying nothing.
BLANK_VALUES: list[str] = ["", " ", "   ", "\t", "\n", " \t\n ", " "]


def bar_row(symbol: Any, volume: int) -> dict[str, Any]:
    return {
        "timestamp": BASE_TIME,
        "instrument_symbol": symbol,
        "interval": INTERVAL,
        "open": Decimal("5102.00"),
        "high": Decimal("5103.00"),
        "low": Decimal("5101.00"),
        "close": Decimal("5102.50"),
        "volume": Decimal(volume),
    }


def trade_record(symbol: Any, source_index: int = 0) -> RawRecord:
    return RawRecord(
        record_type=ImportRecordType.TRADE,
        source_index=source_index,
        provider_name="test",
        fields={
            "timestamp": BASE_TIME,
            "instrument_symbol": symbol,
            "price": Decimal("5102.25"),
            "size": Decimal(1),
            "side": "buy",
        },
    )


# --------------------------------------------------------------------------
# The defect itself
# --------------------------------------------------------------------------


def test_none_and_the_text_none_no_longer_collide_into_one_bar() -> None:
    """The exact reproduction from ADR-009, now rejected."""
    batch = ImportBatch(
        record_type=ImportRecordType.BAR,
        rows=(bar_row(None, 5), bar_row("None", 9)),
    )

    accepted, report = validate_import_batch(batch)

    assert not report.success
    assert ValidationIssueCode.INVALID_INSTRUMENT_SYMBOL in {issue.code for issue in report.issues}
    assert [record.instrument_symbol for record in accepted] == ["None"]


@pytest.mark.parametrize(
    ("raw", "text"),
    [(None, "None"), (123, "123"), (True, "True"), (False, "False"), (4.5, "4.5")],
)
def test_no_non_string_value_can_impersonate_its_own_text_form(raw: Any, text: str) -> None:
    batch = ImportBatch(record_type=ImportRecordType.BAR, rows=(bar_row(raw, 5), bar_row(text, 9)))

    accepted, report = validate_import_batch(batch)

    assert not report.success
    assert len(accepted) == 1
    assert accepted[0].instrument_symbol == text


def test_a_genuine_conflicting_bar_is_still_detected_as_a_conflict() -> None:
    """Guards against the fix accidentally disabling ADR-005 conflict detection."""
    batch = ImportBatch(
        record_type=ImportRecordType.BAR,
        rows=(bar_row("ESM6", 5), bar_row("ESM6", 9)),
    )

    accepted, report = validate_import_batch(batch)

    assert not report.success
    assert len(accepted) == 0
    assert {issue.code for issue in report.issues} == {ValidationIssueCode.CONFLICTING_BAR}


def test_an_exact_duplicate_bar_is_still_a_duplicate_not_a_conflict() -> None:
    batch = ImportBatch(
        record_type=ImportRecordType.BAR,
        rows=(bar_row("ESM6", 5), bar_row("ESM6", 5)),
    )

    accepted, report = validate_import_batch(batch)

    assert report.success
    assert len(accepted) == 1
    assert {issue.code for issue in report.issues} == {ValidationIssueCode.DUPLICATE_ROW}


def test_identical_trades_are_still_never_deduplicated() -> None:
    """ADR-003 must survive the new validator."""
    trade_row = {
        "timestamp": BASE_TIME,
        "instrument_symbol": "ESM6",
        "price": Decimal("5102.25"),
        "size": Decimal(1),
        "side": "buy",
    }
    batch = ImportBatch(record_type=ImportRecordType.TRADE, rows=(trade_row, dict(trade_row)))

    accepted, report = validate_import_batch(batch)

    assert report.success
    assert len(accepted) == 2


# --------------------------------------------------------------------------
# The rule
# --------------------------------------------------------------------------


@pytest.mark.parametrize("value", NON_STRING_VALUES)
def test_non_string_values_violate_the_rule(value: Any) -> None:
    assert check_instrument_symbol(value) is InstrumentSymbolViolation.NOT_A_STRING


@pytest.mark.parametrize("value", BLANK_VALUES)
def test_blank_values_violate_the_rule(value: str) -> None:
    assert check_instrument_symbol(value) is InstrumentSymbolViolation.BLANK


@pytest.mark.parametrize("value", ["ES", "ESM6", "MESM6", "ES1!", " ESM6 ", "esm6", "6E", "a"])
def test_usable_symbols_pass_the_rule(value: str) -> None:
    assert check_instrument_symbol(value) is None
    assert require_instrument_symbol(value, "instrument_symbol") == value


def test_the_rule_does_not_normalize_a_usable_symbol() -> None:
    """The legacy field is left exactly as decoded — no trimming, no folding.

    Narrowing it further would change what an already-written schema-v2 file
    means when it is read back, which is a storage decision. Canonical identity
    is a separate, strictly validated type instead. See ADR-009.
    """
    for value in (" ESM6 ", "esm6", "ES M6", "\tESM6"):
        assert require_instrument_symbol(value, "instrument_symbol") == value


@pytest.mark.parametrize("value", [*NON_STRING_VALUES, *BLANK_VALUES])
def test_require_raises_for_unusable_values(value: Any) -> None:
    with pytest.raises(ValueError):
        require_instrument_symbol(value, "instrument_symbol")


# --------------------------------------------------------------------------
# The validator
# --------------------------------------------------------------------------


@pytest.mark.parametrize("value", [*NON_STRING_VALUES, *BLANK_VALUES])
def test_the_validator_reports_one_error_per_unusable_record(value: Any) -> None:
    issues = InstrumentSymbolValidator().validate([trade_record(value)])

    assert len(issues) == 1
    assert issues[0].code is ValidationIssueCode.INVALID_INSTRUMENT_SYMBOL
    assert issues[0].field_name == "instrument_symbol"
    assert issues[0].row_index == 0


def test_the_validator_is_silent_for_a_usable_symbol() -> None:
    assert InstrumentSymbolValidator().validate([trade_record("ESM6")]) == ()


def test_the_validator_leaves_a_missing_field_to_the_schema_validator() -> None:
    record = RawRecord(
        record_type=ImportRecordType.TRADE,
        source_index=0,
        provider_name="test",
        fields={"timestamp": BASE_TIME, "price": Decimal(1), "size": Decimal(1), "side": "buy"},
    )

    assert InstrumentSymbolValidator().validate([record]) == ()


def test_the_validator_reports_records_in_source_order() -> None:
    records = [trade_record(None, 0), trade_record("ESM6", 1), trade_record("  ", 2)]

    issues = InstrumentSymbolValidator().validate(records)

    assert [issue.row_index for issue in issues] == [0, 2]


def test_the_validator_is_deterministic_over_repeated_runs() -> None:
    records = [trade_record(value, index) for index, value in enumerate(NON_STRING_VALUES)]
    validator = InstrumentSymbolValidator()

    first = validator.validate(records)

    assert all(validator.validate(records) == first for _ in range(10))


# --------------------------------------------------------------------------
# Interaction with duplicate identity
# --------------------------------------------------------------------------


@pytest.mark.parametrize("value", [*NON_STRING_VALUES, *BLANK_VALUES])
def test_a_record_with_an_unusable_symbol_has_no_duplicate_identity(value: Any) -> None:
    """Silence where another validator owns the diagnosis.

    A ``duplicate_row`` warning saying "one copy will be discarded by policy"
    would be false for rows that are all being rejected.
    """
    record = RawRecord(
        record_type=ImportRecordType.BAR,
        source_index=0,
        provider_name="test",
        fields=bar_row(value, 5),
    )

    assert record_identity(record) is None


def test_a_usable_symbol_still_produces_a_bar_identity() -> None:
    record = RawRecord(
        record_type=ImportRecordType.BAR,
        source_index=0,
        provider_name="test",
        fields=bar_row("ESM6", 5),
    )

    assert record_identity(record) is not None


def test_two_unusable_bars_produce_no_duplicate_warning_only_errors() -> None:
    batch = ImportBatch(record_type=ImportRecordType.BAR, rows=(bar_row(None, 5), bar_row(None, 5)))

    _, report = validate_import_batch(batch)

    assert {issue.code for issue in report.issues} == {
        ValidationIssueCode.INVALID_INSTRUMENT_SYMBOL
    }


# --------------------------------------------------------------------------
# Normalization no longer coerces
# --------------------------------------------------------------------------


@pytest.mark.parametrize("value", [*NON_STRING_VALUES, *BLANK_VALUES])
def test_normalization_refuses_rather_than_coercing(value: Any) -> None:
    with pytest.raises(ValueError):
        DefaultRecordNormalizer().normalize(trade_record(value))


def test_normalization_preserves_a_usable_symbol_exactly() -> None:
    for value in ("ESM6", " ESM6 ", "esm6"):
        record = trade_record(value)

        assert DefaultRecordNormalizer().normalize(record).instrument_symbol == value


# --------------------------------------------------------------------------
# Regression: a str subclass must not split one instrument in two (D3)
#
# A ``str`` subclass may override ``__eq__``/``__hash__``. Two records carrying
# such a value compared *unequal* in the import layer while Pydantic stored
# ordinary equal strings in the domain — so two conflicting bars for one period
# were accepted together, exactly as in the None/"None" case one level up.
# --------------------------------------------------------------------------


class _UnequalStr(str):
    """A string that refuses to equal anything, including itself."""

    def __eq__(self, other: object) -> bool:
        return False

    def __ne__(self, other: object) -> bool:
        return True

    def __hash__(self) -> int:
        return id(self)


class _RelabelledStr(str):
    """A string whose ``__str__`` disagrees with its own characters."""

    def __str__(self) -> str:
        return "OTHER"


def test_a_str_subclass_with_broken_equality_cannot_split_one_instrument() -> None:
    batch = ImportBatch(
        record_type=ImportRecordType.BAR,
        rows=(bar_row(_UnequalStr("ESM6"), 5), bar_row(_UnequalStr("ESM6"), 9)),
    )

    accepted, report = validate_import_batch(batch)

    assert not report.success
    assert len(accepted) == 0
    assert ValidationIssueCode.CONFLICTING_BAR in {issue.code for issue in report.issues}


def test_a_str_subclass_duplicate_is_still_reported_as_a_duplicate() -> None:
    batch = ImportBatch(
        record_type=ImportRecordType.BAR,
        rows=(bar_row(_UnequalStr("ESM6"), 5), bar_row(_UnequalStr("ESM6"), 5)),
    )

    accepted, report = validate_import_batch(batch)

    assert report.success
    assert len(accepted) == 1


@pytest.mark.parametrize("subclass", [_UnequalStr, _RelabelledStr])
def test_identity_and_the_domain_object_agree_for_a_str_subclass(subclass: type) -> None:
    record = RawRecord(
        record_type=ImportRecordType.BAR,
        source_index=0,
        provider_name="test",
        fields=bar_row(subclass("ESM6"), 5),
    )

    identity = record_identity(record)
    normalized = DefaultRecordNormalizer().normalize(record)

    assert identity is not None
    assert identity[1] == normalized.instrument_symbol
    assert type(normalized.instrument_symbol) is str


def test_the_canonical_symbol_form_is_an_exact_str() -> None:
    for value in ("ESM6", _UnequalStr("ESM6")):
        result = require_instrument_symbol(value, "instrument_symbol")

        assert type(result) is str
        assert result == "ESM6"


@pytest.mark.parametrize(
    "symbol",
    ["ESM6", " ESM6 ", "esm6", "ES M6", "E", "\tES", "ES\n", "ES:M6", "ES1!", "0", "ESM26"],
)
def test_duplicate_identity_always_agrees_with_the_normalized_symbol(symbol: str) -> None:
    """The invariant whose violation was the original defect.

    For every record that passes the symbol rule, the instrument component of
    the duplicate-identity tuple must be exactly the ``instrument_symbol`` the
    domain object ends up holding. When these two disagree, records that are
    distinct to duplicate detection collapse into one instrument downstream.
    """
    record = RawRecord(
        record_type=ImportRecordType.BAR,
        source_index=0,
        provider_name="test",
        fields=bar_row(symbol, 5),
    )

    identity = record_identity(record)
    normalized = DefaultRecordNormalizer().normalize(record)

    assert identity is not None
    assert identity[1] == normalized.instrument_symbol


def test_padded_and_unpadded_symbols_remain_distinct_instruments() -> None:
    """Documented limitation, asserted so it cannot change unnoticed.

    The import layer does not merge ``" ESM6 "`` into ``"ESM6"``. Merging them
    would be exactly the silent identity change this layer must not perform;
    distinguishing them honestly is what canonical identity exists to replace.
    """
    padded = DefaultRecordNormalizer().normalize(trade_record(" ESM6 "))
    plain = DefaultRecordNormalizer().normalize(trade_record("ESM6"))

    assert padded.instrument_symbol != plain.instrument_symbol
