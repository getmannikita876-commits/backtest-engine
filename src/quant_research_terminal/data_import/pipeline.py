"""Import orchestration.

This module coordinates the import stages and owns none of their rules::

    ImportBatch -> Raw Records -> Validation -> Policy -> Normalization -> Domain

Every rule it applies is defined elsewhere and imported: field requirements
from :mod:`~quant_research_terminal.data_import.record_fields`, UTC semantics
from :mod:`~quant_research_terminal.data_import.time_semantics`, numeric
semantics from :mod:`~quant_research_terminal.data_import.numeric_semantics`,
the validators themselves from
:mod:`~quant_research_terminal.data_import.validation`, and domain construction
from :mod:`~quant_research_terminal.data_import.normalization`.

What orchestration *does* own is the sequencing and the decisions that only
make sense once every rule has spoken:

* whether the batch is processable at all — an incompatible storage schema
  version is batch-fatal and stops the run before validation begins,
* which rows survive — a row is rejected when any issue attributed to it is an
  error or fatal,
* which copy of a duplicate survives — resolved by the batch's
  :class:`DuplicatePolicy`,
* what order accepted records come back in — the complete key from
  :mod:`~quant_research_terminal.data_import.event_order`: timestamp, then
  event-type precedence, then source position.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Sequence
from datetime import datetime

from quant_research_terminal.data.contracts import SCHEMA_VERSION
from quant_research_terminal.data_import.contracts import (
    DuplicatePolicy,
    ImportBatch,
    ValidationIssue,
    ValidationIssueCode,
    ValidationReport,
    ValidationSeverity,
)
from quant_research_terminal.data_import.event_order import event_ordering_key
from quant_research_terminal.data_import.normalization import DefaultRecordNormalizer, DomainRecord
from quant_research_terminal.data_import.raw_record import RawRecord, import_batch_to_raw_records
from quant_research_terminal.data_import.validation import (
    batch_validation_pipeline,
    record_identity,
)

#: Severities that cause the row they are attributed to be rejected.
_REJECTING_SEVERITIES = frozenset({ValidationSeverity.ERROR, ValidationSeverity.FATAL})


def _schema_version_issue(batch: ImportBatch) -> ValidationIssue | None:
    """Report an incompatible storage schema version, once for the batch.

    This is a property of the batch, not of any row, so it is raised once
    regardless of how many rows the batch carries.
    """
    if batch.schema_version == SCHEMA_VERSION:
        return None
    return ValidationIssue(
        severity=ValidationSeverity.FATAL,
        code=ValidationIssueCode.UNSUPPORTED_SCHEMA_VERSION,
        message="schema version is incompatible",
    )


def _rejected_rows(issues: Sequence[ValidationIssue]) -> set[int]:
    """Return the source indices of rows carrying an error or fatal issue."""
    return {
        issue.row_index
        for issue in issues
        if issue.row_index is not None and issue.severity in _REJECTING_SEVERITIES
    }


def _apply_duplicate_policy(
    records: Sequence[RawRecord], policy: DuplicatePolicy
) -> list[RawRecord]:
    """Reduce each group of duplicate records to the single surviving copy.

    Detection already happened in the validation stage; this only decides which
    copy to keep, using the same :func:`record_identity` definition, so the two
    can never disagree about what a duplicate is.
    """
    survivor_positions: dict[tuple[object, ...], int] = {}
    survivors: list[RawRecord] = []

    for record in records:
        identity = record_identity(record)
        if identity is None:
            # Not comparable, so it cannot be a duplicate of anything.
            survivors.append(record)
            continue

        position = survivor_positions.get(identity)
        if position is None:
            survivor_positions[identity] = len(survivors)
            survivors.append(record)
        elif policy is DuplicatePolicy.KEEP_LAST:
            survivors[position] = record

    return survivors


def _count(issues: Sequence[ValidationIssue], severity: ValidationSeverity) -> int:
    return sum(1 for issue in issues if issue.severity is severity)


def _build_report(
    *, total_rows: int, accepted_rows: int, issues: Sequence[ValidationIssue]
) -> ValidationReport:
    """Summarise a completed batch run."""
    error_count = _count(issues, ValidationSeverity.ERROR)
    fatal_count = _count(issues, ValidationSeverity.FATAL)
    return ValidationReport(
        total_rows=total_rows,
        accepted_rows=accepted_rows,
        rejected_rows=total_rows - accepted_rows,
        warning_count=_count(issues, ValidationSeverity.WARNING),
        error_count=error_count,
        fatal_count=fatal_count,
        issues=tuple(issues),
        success=(fatal_count == 0 and error_count == 0),
    )


def validate_import_batch(batch: ImportBatch) -> tuple[list[DomainRecord], ValidationReport]:
    """Validate and normalize one import batch.

    Args:
        batch: The rows to import, with their schema version and policies.

    Returns:
        The accepted domain objects in the deterministic order defined by
        :func:`~quant_research_terminal.data_import.event_order.event_ordering_key`
        — timestamp, then event-type precedence, then source position —
        together with the report describing the run. The caller's rows are
        never modified.

        An incompatible schema version yields no accepted objects and a report
        carrying exactly one fatal issue.
    """
    schema_issue = _schema_version_issue(batch)
    if schema_issue is not None:
        # Batch-fatal: the rows were written against a schema this build does
        # not understand, so every field meaning is in doubt. Validating them
        # would produce findings derived from an unknown layout, and
        # normalizing them could manufacture domain objects from
        # misinterpreted values. Stop before either can happen.
        return [], _build_report(total_rows=len(batch.rows), accepted_rows=0, issues=[schema_issue])

    records = import_batch_to_raw_records(batch)
    issues: list[ValidationIssue] = list(
        batch_validation_pipeline(strict_fields=batch.strict_fields).validate(records)
    )

    if not batch.rows:
        return [], _build_report(total_rows=0, accepted_rows=0, issues=issues)

    rejected = _rejected_rows(issues)
    candidates = [record for record in records if record.source_index not in rejected]
    survivors = _apply_duplicate_policy(candidates, batch.duplicate_policy)

    normalizer = DefaultRecordNormalizer()
    normalized: list[tuple[tuple[datetime, int, int], DomainRecord]] = []
    for record in survivors:
        domain_record = normalizer.normalize(record)
        ordering_key = event_ordering_key(
            record_type=record.record_type,
            timestamp=domain_record.timestamp,
            source_index=record.source_index,
        )
        normalized.append((ordering_key, domain_record))

    normalized.sort(key=lambda item: item[0])
    accepted = [domain_record for _, domain_record in normalized]

    return accepted, _build_report(
        total_rows=len(batch.rows), accepted_rows=len(accepted), issues=issues
    )


def group_issues_by_row(
    issues: Sequence[ValidationIssue],
) -> dict[int | None, tuple[ValidationIssue, ...]]:
    """Group issues by the row they are attributed to.

    Batch-level findings, which carry no row index, are grouped under ``None``.
    Useful for presenting a report row by row without re-deriving the mapping
    at each call site.
    """
    grouped: dict[int | None, list[ValidationIssue]] = defaultdict(list)
    for issue in issues:
        grouped[issue.row_index].append(issue)
    return {row_index: tuple(row_issues) for row_index, row_issues in grouped.items()}
