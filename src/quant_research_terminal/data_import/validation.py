"""Validation rules for the data-import pipeline.

This module is the single authoritative home for every import validation rule.
Each validator answers exactly one question about a batch of raw records and
returns issues; none of them mutate records, raise on bad data, or decide what
to do about a defect. Deciding — reject the row, keep the first duplicate,
abort the batch — is policy, and policy belongs to the orchestration layer in
:mod:`quant_research_terminal.data_import.pipeline`.

============================= =============================================
Validator                     Question
============================= =============================================
:class:`SchemaValidator`      Are the right fields present, and only those?
:class:`TimestampValidator`   Is every timestamp fixed-offset UTC?
:class:`ValueValidator`       Are prices, sizes, and OHLC internally sound?
:class:`DuplicateValidator`   Does any record repeat an earlier identity?
:class:`OrderingValidator`    Do timestamps advance monotonically?
============================= =============================================

Each validator reads ``record.record_type`` from the record itself rather than
being told what to expect, so a validator instance is stateless, reusable, and
correct for mixed batches.

Validators are independent by construction, but they deliberately stay silent
where another validator already owns the diagnosis. A record missing its
timestamp column is reported once by :class:`SchemaValidator`; the timestamp,
value, duplicate, and ordering validators skip it rather than emitting five
issues for one defect.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Final, Protocol, runtime_checkable

from quant_research_terminal.data_import.contracts import (
    ImportRecordType,
    ValidationIssue,
    ValidationIssueCode,
    ValidationSeverity,
)
from quant_research_terminal.data_import.numeric_semantics import (
    NumericViolation,
    check_price,
    check_quantity,
    to_decimal,
    violation_message,
)
from quant_research_terminal.data_import.raw_record import RawRecord
from quant_research_terminal.data_import.record_fields import (
    INTERVAL_FIELD,
    TIMESTAMP_FIELD,
    price_fields,
    quantity_fields,
    required_fields,
)
from quant_research_terminal.data_import.time_semantics import (
    TimestampStatus,
    classify_timestamp,
    status_message,
)
from quant_research_terminal.domain.models import parse_trade_side

#: Maps a failing timestamp status onto the issue code reported for it.
#:
#: The three failure modes carry distinct codes because they call for different
#: corrective action: a non-datetime means the source was never parsed, a naive
#: datetime means the source omitted its offset, and a non-UTC datetime means
#: the source used a real but wrong zone. Collapsing them would leave an
#: operator unable to tell a parsing bug from a timezone bug.
_TIMESTAMP_CODES: Final[dict[TimestampStatus, ValidationIssueCode]] = {
    TimestampStatus.NOT_DATETIME: ValidationIssueCode.NON_DATETIME_TIMESTAMP,
    TimestampStatus.NAIVE: ValidationIssueCode.NAIVE_DATETIME,
    TimestampStatus.NON_UTC: ValidationIssueCode.NON_UTC_TIMESTAMP,
}


@runtime_checkable
class RecordBatchValidator(Protocol):
    """One composable validation rule over a batch of raw records.

    Implementations must be pure and deterministic: the same records in the
    same order always produce the same issues in the same order.
    """

    @property
    def name(self) -> str:
        """Return a stable identifier used in diagnostics."""
        ...

    def validate(self, records: Sequence[RawRecord]) -> tuple[ValidationIssue, ...]:
        """Return every issue this rule finds, in record order."""
        ...


def _issue(
    *,
    severity: ValidationSeverity,
    code: ValidationIssueCode,
    message: str,
    row_index: int | None = None,
    field_name: str | None = None,
) -> ValidationIssue:
    return ValidationIssue(
        severity=severity,
        code=code,
        row_index=row_index,
        field_name=field_name,
        message=message,
    )


#: Issue code reported for each envelope violation of a **price** field.
#:
#: ``NOT_A_NUMBER`` and ``NOT_POSITIVE`` keep the long-standing
#: ``non_decimal_price`` code so existing diagnostics stay stable; the
#: conditions the envelope newly distinguishes get their own codes.
_PRICE_ISSUE_CODES: Final[dict[NumericViolation, ValidationIssueCode]] = {
    NumericViolation.NOT_A_NUMBER: ValidationIssueCode.NON_DECIMAL_PRICE,
    NumericViolation.NOT_POSITIVE: ValidationIssueCode.NON_DECIMAL_PRICE,
    NumericViolation.NON_FINITE: ValidationIssueCode.NON_FINITE_VALUE,
    NumericViolation.MAGNITUDE_TOO_LARGE: ValidationIssueCode.MAGNITUDE_EXCEEDED,
    NumericViolation.TOO_MANY_FRACTIONAL_DIGITS: ValidationIssueCode.PRECISION_EXCEEDED,
    NumericViolation.NOT_WHOLE: ValidationIssueCode.NON_INTEGER_QUANTITY,
}

#: Issue code reported for each envelope violation of a **quantity** field.
#:
#: Quantities keep ``negative_value`` for the not-a-number and not-positive
#: cases, matching the code this layer has always reported for sizes. Bar
#: volume is a quantity and now reports these codes too; it previously
#: reported ``non_decimal_price``, which misdescribed a count as a price.
_QUANTITY_ISSUE_CODES: Final[dict[NumericViolation, ValidationIssueCode]] = {
    NumericViolation.NOT_A_NUMBER: ValidationIssueCode.NEGATIVE_VALUE,
    NumericViolation.NOT_POSITIVE: ValidationIssueCode.NEGATIVE_VALUE,
    NumericViolation.NON_FINITE: ValidationIssueCode.NON_FINITE_VALUE,
    NumericViolation.MAGNITUDE_TOO_LARGE: ValidationIssueCode.MAGNITUDE_EXCEEDED,
    NumericViolation.TOO_MANY_FRACTIONAL_DIGITS: ValidationIssueCode.PRECISION_EXCEEDED,
    NumericViolation.NOT_WHOLE: ValidationIssueCode.NON_INTEGER_QUANTITY,
}


#: Record types whose identity cannot be established from their attributes.
#:
#: A trade is a *countable event*. Two separate executions can legitimately
#: agree on timestamp, instrument, price, size, and side — one-lot fills at the
#: same price inside the same microsecond are ordinary in real tick data — and
#: the domain carries no venue, sequence number, or trade identifier that could
#: tell them apart. Treating attribute equality as proof of duplication
#: therefore deletes real executions and understates volume.
#:
#: A quote and a bar are different in kind. A quote is a *state observation*:
#: two identical top-of-book snapshots for the same instant carry the same
#: information, so collapsing them loses nothing. A bar is a *summary keyed by
#: its period*: two identical bars for one interval are the same bar. Exact
#: duplicate removal stays enabled for both.
#:
#: This is an interim policy. ADR-003 proposes the trade-identity contract that
#: would let duplicate detection be restored for trades on a sound basis.
UNIDENTIFIABLE_RECORD_TYPES: Final[frozenset[ImportRecordType]] = frozenset(
    {ImportRecordType.TRADE}
)


def record_identity(record: RawRecord) -> tuple[object, ...] | None:
    """Return the value identifying ``record`` for duplicate comparison.

    Identity is the record type plus every required field, so two records are
    duplicates only when they are indistinguishable in every field the domain
    models care about.

    This is the one authoritative definition of record identity. Duplicate
    *detection* and duplicate *policy* live in different layers, and both call
    this function so they can never disagree about what a duplicate is.

    Returns:
        The identity tuple, or ``None`` when the record cannot participate in
        comparison. ``None`` is returned when the record type has no
        attribute-based identity (see :data:`UNIDENTIFIABLE_RECORD_TYPES`),
        when a required field is missing, or when a decoded value is
        unhashable. The latter two are reported by other validators, so
        returning ``None`` here avoids a duplicate diagnosis.
    """
    if record.record_type in UNIDENTIFIABLE_RECORD_TYPES:
        return None

    expected = required_fields(record.record_type)
    if not record.has_fields(expected):
        return None

    identity = (record.record_type.value, *(record.value(field) for field in expected))
    try:
        hash(identity)
    except TypeError:
        return None
    return identity


class SchemaValidator:
    """Checks that each record carries exactly the fields its type requires."""

    def __init__(self, *, strict_fields: bool = True) -> None:
        """Configure field checking.

        Args:
            strict_fields: When true, fields beyond the required set are
                reported. Unknown columns usually mean a source file changed
                shape, which is worth surfacing before the data is trusted.
        """
        self._strict_fields = strict_fields

    @property
    def name(self) -> str:
        """Return the validator identifier."""
        return "schema"

    def validate(self, records: Sequence[RawRecord]) -> tuple[ValidationIssue, ...]:
        """Report missing required fields and, optionally, unknown fields."""
        issues: list[ValidationIssue] = []
        for record in records:
            expected = required_fields(record.record_type)
            present = set(record.fields)

            missing = [field for field in expected if field not in present]
            if missing:
                issues.append(
                    _issue(
                        severity=ValidationSeverity.ERROR,
                        code=ValidationIssueCode.MISSING_REQUIRED_FIELD,
                        message=f"missing required fields: {', '.join(missing)}",
                        row_index=record.source_index,
                        field_name=missing[0],
                    )
                )

            if self._strict_fields:
                unknown = sorted(present - set(expected))
                if unknown:
                    issues.append(
                        _issue(
                            severity=ValidationSeverity.ERROR,
                            code=ValidationIssueCode.UNKNOWN_FIELD,
                            message=f"unknown fields: {', '.join(unknown)}",
                            row_index=record.source_index,
                            field_name=unknown[0],
                        )
                    )
        return tuple(issues)


class TimestampValidator:
    """Checks that every timestamp is a fixed-offset UTC datetime."""

    @property
    def name(self) -> str:
        """Return the validator identifier."""
        return "timestamp"

    def validate(self, records: Sequence[RawRecord]) -> tuple[ValidationIssue, ...]:
        """Report naive, non-UTC, and non-datetime timestamps."""
        issues: list[ValidationIssue] = []
        for record in records:
            if TIMESTAMP_FIELD not in record.fields:
                # Absence is the schema validator's diagnosis, not ours.
                continue

            status = classify_timestamp(record.value(TIMESTAMP_FIELD))
            if status is TimestampStatus.VALID:
                continue

            issues.append(
                _issue(
                    severity=ValidationSeverity.ERROR,
                    code=_TIMESTAMP_CODES[status],
                    message=status_message(status),
                    row_index=record.source_index,
                    field_name=TIMESTAMP_FIELD,
                )
            )
        return tuple(issues)


class ValueValidator:
    """Checks that numeric values are exact, positive, and mutually consistent.

    Three questions, by record type: is every numeric field a value that
    converts to :class:`~decimal.Decimal` without loss, is it positive, and do
    the fields agree with each other — a quote's bid not above its ask, a bar's
    open and close inside its high-low range.

    Every quantity — trade size, quote sizes, bar volume — must be **strictly
    positive**. This mirrors the domain contract exactly: ``Trade.size``,
    ``Quote.bid_size``, ``Quote.ask_size``, and ``Bar.volume`` are all declared
    ``PositiveDecimal`` (``Field(gt=0)``), so a zero or negative quantity has
    no domain representation. Validating it here means such a row is rejected
    with a diagnosable issue instead of raising a raw model error during
    normalization.

    A zero-volume bar is therefore not importable, and an empty period must be
    represented by the absence of a bar rather than by a bar with no volume.
    That follows the approved Phase 1.1 domain contract; changing it is a
    domain-layer decision, not an import-layer one.

    At most one issue is reported per record. The checks are ordered from most
    to least fundamental, and a later check cannot be evaluated meaningfully
    once an earlier one has failed.
    """

    @property
    def name(self) -> str:
        """Return the validator identifier."""
        return "value"

    def validate(self, records: Sequence[RawRecord]) -> tuple[ValidationIssue, ...]:
        """Report the first numeric or consistency defect in each record."""
        issues: list[ValidationIssue] = []
        for record in records:
            if not record.has_fields(required_fields(record.record_type)):
                # Incomplete records are the schema validator's diagnosis.
                continue

            issue = self._record_issue(record)
            if issue is not None:
                issues.append(issue)
        return tuple(issues)

    def _envelope_issue(self, record: RawRecord) -> ValidationIssue | None:
        """Report the first numeric field that falls outside the envelope.

        Delegates every rule to the canonical envelope in
        :mod:`quant_research_terminal.domain.numeric` and only translates the
        answer into this layer's issue codes. Because the domain models enforce
        the same envelope, a record that passes here constructs successfully
        and encodes exactly into storage.

        Prices are checked before quantities, and each group in its declared
        field order, so the field named in a diagnostic is stable rather than
        dependent on set iteration order.
        """
        record_type = record.record_type

        for field_name in price_fields(record_type):
            price_violation = check_price(record.value(field_name))
            if price_violation is not None:
                return _issue(
                    severity=ValidationSeverity.ERROR,
                    code=_PRICE_ISSUE_CODES[price_violation],
                    message=violation_message(price_violation, field_name),
                    row_index=record.source_index,
                    field_name=field_name,
                )

        for field_name in quantity_fields(record_type):
            quantity_violation = check_quantity(record.value(field_name))
            if quantity_violation is not None:
                return _issue(
                    severity=ValidationSeverity.ERROR,
                    code=_QUANTITY_ISSUE_CODES[quantity_violation],
                    message=violation_message(quantity_violation, field_name),
                    row_index=record.source_index,
                    field_name=field_name,
                )
        return None

    def _record_issue(self, record: RawRecord) -> ValidationIssue | None:
        envelope = self._envelope_issue(record)
        if envelope is not None:
            return envelope

        if record.record_type is ImportRecordType.TRADE:
            return self._trade_issue(record)
        if record.record_type is ImportRecordType.QUOTE:
            return self._quote_issue(record)
        return self._bar_issue(record)

    def _trade_issue(self, record: RawRecord) -> ValidationIssue | None:
        try:
            parse_trade_side(record.value("side"))
        except ValueError as exc:
            # Reported here so an unrecognised vendor code is a diagnosable
            # rejection rather than a raw model error during normalization.
            return _issue(
                severity=ValidationSeverity.ERROR,
                code=ValidationIssueCode.INVALID_TRADE_SIDE,
                message=str(exc),
                row_index=record.source_index,
                field_name="side",
            )
        return None

    def _quote_issue(self, record: RawRecord) -> ValidationIssue | None:
        bid_value = to_decimal(record.value("bid"))
        ask_value = to_decimal(record.value("ask"))
        if bid_value > ask_value:
            return _issue(
                severity=ValidationSeverity.ERROR,
                code=ValidationIssueCode.BID_ASK_INVERSION,
                message="bid must be less than or equal to ask",
                row_index=record.source_index,
                field_name="bid",
            )
        return None

    def _bar_issue(self, record: RawRecord) -> ValidationIssue | None:
        interval = record.value(INTERVAL_FIELD)
        if not isinstance(interval, timedelta):
            return self._interval_issue(record, "interval must be a timedelta")
        if interval <= timedelta(0):
            # A non-positive interval would place the bar's availability at or
            # before its own start, reintroducing look-ahead.
            return self._interval_issue(record, "interval must be strictly positive")

        range_issue = self._interval_range_issue(record, interval)
        if range_issue is not None:
            return range_issue

        open_value, high, low, close = (
            to_decimal(record.value(field)) for field in ("open", "high", "low", "close")
        )
        if self._ohlc_inconsistent(open_value=open_value, high=high, low=low, close=close):
            return _issue(
                severity=ValidationSeverity.ERROR,
                code=ValidationIssueCode.INVALID_OHLC,
                message="OHLC values are inconsistent",
                row_index=record.source_index,
                field_name="high",
            )
        return None

    @staticmethod
    def _ohlc_inconsistent(
        *, open_value: Decimal, high: Decimal, low: Decimal, close: Decimal
    ) -> bool:
        return high < low or open_value < low or open_value > high or close < low or close > high

    @staticmethod
    def _interval_range_issue(record: RawRecord, interval: timedelta) -> ValidationIssue | None:
        """Report an interval that cannot be applied to the record's timestamp.

        The record carries availability time; normalization derives interval
        start by subtracting the interval, and the domain recomputes
        availability by adding it back. Either step can leave the representable
        datetime range, which raises rather than returning a value. Checking
        both here converts that into a diagnosable rejection instead of an
        exception escaping the import API.

        A timestamp that is not a datetime is left alone — the timestamp
        validator owns that diagnosis.
        """
        timestamp = record.value(TIMESTAMP_FIELD)
        if not isinstance(timestamp, datetime):
            return None

        try:
            interval_start = timestamp - interval
            interval_start + interval
        except OverflowError:
            return _issue(
                severity=ValidationSeverity.ERROR,
                code=ValidationIssueCode.INTERVAL_OUT_OF_RANGE,
                message=(
                    f"interval {interval} cannot be applied to timestamp "
                    f"{timestamp.isoformat()} without leaving the representable "
                    f"datetime range"
                ),
                row_index=record.source_index,
                field_name=INTERVAL_FIELD,
            )
        return None

    @staticmethod
    def _interval_issue(record: RawRecord, message: str) -> ValidationIssue:
        return _issue(
            severity=ValidationSeverity.ERROR,
            code=ValidationIssueCode.INVALID_BAR_INTERVAL,
            message=message,
            row_index=record.source_index,
            field_name=INTERVAL_FIELD,
        )


class DuplicateValidator:
    """Detects records that repeat the identity of an earlier record.

    Identity comes from :func:`record_identity`. This validator only *reports*
    repeats; which copy survives is decided by
    :class:`~quant_research_terminal.data_import.contracts.DuplicatePolicy` in
    the orchestration layer.

    Record types in :data:`UNIDENTIFIABLE_RECORD_TYPES` are skipped entirely.
    Without an identity, attribute equality is not evidence of duplication, and
    reporting it would invite a policy that deletes genuine records.
    """

    @property
    def name(self) -> str:
        """Return the validator identifier."""
        return "duplicate"

    def validate(self, records: Sequence[RawRecord]) -> tuple[ValidationIssue, ...]:
        """Report each record whose identity already appeared earlier."""
        issues: list[ValidationIssue] = []
        seen: dict[tuple[object, ...], int] = {}

        for record in records:
            identity = record_identity(record)
            if identity is None:
                continue

            first_index = seen.get(identity)
            if first_index is None:
                seen[identity] = record.source_index
                continue

            issues.append(
                _issue(
                    severity=ValidationSeverity.WARNING,
                    code=ValidationIssueCode.DUPLICATE_ROW,
                    message=(
                        f"identical to the record at source index {first_index}; "
                        f"one copy will be discarded by the batch's duplicate policy"
                    ),
                    row_index=record.source_index,
                )
            )
        return tuple(issues)


class OrderingValidator:
    """Checks that timestamps advance monotonically through the batch.

    Deterministic replay requires a total order over events. Records that move
    backwards in time break that order and are reported as errors. Records
    sharing a timestamp are legitimate — several trades can occur in the same
    microsecond — and are reported only as information, because
    ``RawRecord.source_index`` supplies a deterministic tiebreak.

    Records without a valid UTC timestamp are skipped: they carry no position
    on the timeline, and their defect is already reported by
    :class:`TimestampValidator`.
    """

    @property
    def name(self) -> str:
        """Return the validator identifier."""
        return "ordering"

    def validate(self, records: Sequence[RawRecord]) -> tuple[ValidationIssue, ...]:
        """Report descending timestamps and flag equal-timestamp neighbours."""
        issues: list[ValidationIssue] = []
        previous: datetime | None = None

        for record in records:
            value = record.value(TIMESTAMP_FIELD)
            if classify_timestamp(value) is not TimestampStatus.VALID:
                continue
            if not isinstance(value, datetime):
                continue

            if previous is not None:
                if value < previous:
                    issues.append(
                        _issue(
                            severity=ValidationSeverity.ERROR,
                            code=ValidationIssueCode.DESCENDING_TIMESTAMP,
                            message=(
                                f"timestamp {value.isoformat()} precedes {previous.isoformat()}"
                            ),
                            row_index=record.source_index,
                            field_name=TIMESTAMP_FIELD,
                        )
                    )
                elif value == previous:
                    issues.append(
                        _issue(
                            severity=ValidationSeverity.INFO,
                            code=ValidationIssueCode.AMBIGUOUS_TIMESTAMP_ORDER,
                            message=(
                                f"timestamp {value.isoformat()} repeats; "
                                "order resolved by source index"
                            ),
                            row_index=record.source_index,
                            field_name=TIMESTAMP_FIELD,
                        )
                    )

            previous = value
        return tuple(issues)


class ValidationPipeline:
    """Runs a fixed sequence of validators and aggregates their issues.

    The pipeline is intentionally not smart: it does not short-circuit, reorder,
    or deduplicate. Running every validator over every batch means a single run
    surfaces all defects at once instead of revealing them one fix at a time,
    and keeps the output a pure function of the validator sequence.
    """

    def __init__(self, validators: Sequence[RecordBatchValidator]) -> None:
        """Compose a pipeline from validators, run in the given order."""
        self._validators: tuple[RecordBatchValidator, ...] = tuple(validators)

    @property
    def validators(self) -> tuple[RecordBatchValidator, ...]:
        """Return the composed validators in execution order."""
        return self._validators

    def validate(self, records: Sequence[RawRecord]) -> tuple[ValidationIssue, ...]:
        """Return every issue found, grouped by validator in execution order.

        An empty batch produces a single ``EMPTY_BATCH`` issue and no validator
        is run, because every rule here is vacuously satisfied by no records
        and an empty import is far more likely to be a mistake than an intent.
        """
        if not records:
            return (
                _issue(
                    severity=ValidationSeverity.ERROR,
                    code=ValidationIssueCode.EMPTY_BATCH,
                    message="batch contains no rows",
                ),
            )

        issues: list[ValidationIssue] = []
        for validator in self._validators:
            issues.extend(validator.validate(records))
        return tuple(issues)


def _core_validators(*, strict_fields: bool) -> tuple[RecordBatchValidator, ...]:
    """Return the validators every import path shares, in execution order."""
    return (
        SchemaValidator(strict_fields=strict_fields),
        TimestampValidator(),
        ValueValidator(),
        DuplicateValidator(),
    )


def default_validation_pipeline(*, strict_fields: bool = True) -> ValidationPipeline:
    """Build the pipeline for a provider record stream.

    Schema runs first so that later validators can rely on field presence, then
    timestamps, then values, then duplicates, then ordering. The order affects
    only the sequence of reported issues, never whether a defect is found.

    Ordering is enforced here because a provider stream is consumed in the
    order it arrives: nothing downstream will re-sort it, so a record that
    moves backwards in time is a genuine defect in the source.
    """
    return ValidationPipeline((*_core_validators(strict_fields=strict_fields), OrderingValidator()))


def batch_validation_pipeline(*, strict_fields: bool = True) -> ValidationPipeline:
    """Build the pipeline for an eager :class:`ImportBatch`.

    Identical to :func:`default_validation_pipeline` except that ordering is
    not enforced. The batch API's contract is to *sort* its output into
    deterministic order rather than to reject unsorted input, so reporting
    descending timestamps there would flag rows the caller never claimed were
    ordered. Sorting is applied by the orchestration layer.
    """
    return ValidationPipeline(_core_validators(strict_fields=strict_fields))
