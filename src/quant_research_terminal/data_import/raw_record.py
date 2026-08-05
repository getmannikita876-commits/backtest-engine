"""The raw record: the hand-off contract between providers and validation.

``RawRecord`` is deliberately defined outside the ``providers`` package so the
validation stage can consume provider output without importing any vendor code.
That keeps the documented pipeline direction one-way::

    Provider -> Raw Records -> Validation -> Normalization -> Domain Objects

A raw record is *decoded but not validated*. A provider is responsible only for
turning a source encoding (CSV text, a vendor payload) into Python values. It
never repairs, converts, or rejects data on semantic grounds: a naive timestamp
or a malformed price is carried through unchanged so the validation stage can
report it against a specific row. This is what keeps "reject invalid data
instead of silently fixing it" enforceable at a single, testable boundary.

This module also owns conversion between the streaming record form and the
eager :class:`ImportBatch` form, because both directions construct records and
the rules for doing so belong in one place.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from types import MappingProxyType
from typing import Any, Final

from pydantic import BaseModel, ConfigDict, field_validator

from quant_research_terminal.data_import.contracts import (
    DuplicatePolicy,
    ImportBatch,
    ImportRecordType,
)

#: Provider name recorded for records reconstructed from an :class:`ImportBatch`
#: that carries no source label of its own.
BATCH_PROVIDER_NAME: Final = "import_batch"


class RawRecord(BaseModel):
    """One decoded, not-yet-validated record produced by a provider.

    Attributes:
        record_type: Which record shape the producing provider claims this is.
        source_index: Zero-based position of the record within the provider's
            source stream, assigned before any filtering. It preserves
            provenance for diagnostics and gives the pipeline a deterministic
            tiebreak for records that share a timestamp.
        provider_name: Name of the provider that produced the record.
        fields: The decoded values keyed by import field name.

    The record is deeply immutable. The model itself is frozen, and ``fields``
    is copied into a read-only mapping on construction, so neither the record
    nor the caller's original dictionary can be changed through the other. A
    provider that keeps reusing one scratch dictionary while streaming cannot
    corrupt records it already emitted.

    The ``fields`` values are typed ``Any`` on purpose: they are untrusted
    until the validation stage runs, and a stricter annotation here would force
    providers to make semantic decisions that belong further down the pipeline.
    """

    model_config = ConfigDict(frozen=True, strict=True)

    record_type: ImportRecordType
    source_index: int
    provider_name: str
    fields: Mapping[str, Any]

    @field_validator("fields", mode="after")
    @classmethod
    def _freeze_fields(cls, value: Mapping[str, Any]) -> Mapping[str, Any]:
        # Copy first: a read-only view over a caller-owned dict would still
        # change underneath us when the caller mutates that dict.
        return MappingProxyType(dict(value))

    def value(self, field_name: str) -> Any:
        """Return a decoded field value, or ``None`` when the field is absent.

        Absence is reported by the schema validator rather than raised here, so
        that a single malformed record cannot abort validation of a whole batch.
        """
        return self.fields.get(field_name)

    def has_fields(self, field_names: Sequence[str]) -> bool:
        """Return whether every name in ``field_names`` is present."""
        return all(field_name in self.fields for field_name in field_names)

    def as_row(self) -> dict[str, Any]:
        """Return a mutable copy of the decoded fields.

        The copy is what makes this safe to hand to code that expects to own
        its input; the record itself stays unaffected by anything done to it.
        """
        return dict(self.fields)


def import_batch_to_raw_records(batch: ImportBatch) -> tuple[RawRecord, ...]:
    """Convert an eager :class:`ImportBatch` into raw records.

    This is what lets the batch API and the streaming API share one set of
    validators: both arrive at validation as a sequence of raw records.
    ``source_index`` is the row's position in the batch, preserving the
    provenance that diagnostics and duplicate policy depend on.
    """
    provider_name = batch.source_name or BATCH_PROVIDER_NAME
    return tuple(
        RawRecord(
            record_type=batch.record_type,
            source_index=index,
            provider_name=provider_name,
            fields=row,
        )
        for index, row in enumerate(batch.rows)
    )


def raw_records_to_import_batch(
    records: Sequence[RawRecord],
    *,
    strict_fields: bool = True,
    duplicate_policy: DuplicatePolicy = DuplicatePolicy.REJECT,
    source_name: str | None = None,
    source_metadata: Mapping[str, Any] | None = None,
) -> ImportBatch:
    """Adapt provider output to the eager :class:`ImportBatch` form.

    This is a structural adapter and nothing more: it copies decoded fields and
    preserves record order. It performs no validation, applies no policy, and
    changes no values, so batching cannot mask a defect that validation would
    otherwise report.

    Record order is preserved rather than sorted. Ordering is a replay concern
    with its own deterministic rules, and imposing an order here would hide the
    out-of-order source data that ``OrderingValidator`` exists to report.

    Args:
        records: Raw records to batch. Must be non-empty and share one record type.
        strict_fields: Passed through to the batch.
        duplicate_policy: Passed through to the batch.
        source_name: Optional provenance label for the batch.
        source_metadata: Optional provenance detail for the batch.

    Returns:
        An immutable batch carrying the records' fields as rows.

    Raises:
        ValueError: if ``records`` is empty, or mixes record types.
    """
    if not records:
        raise ValueError("cannot build an import batch from zero records")

    record_types = {record.record_type for record in records}
    if len(record_types) > 1:
        mixed = ", ".join(sorted(record_type.value for record_type in record_types))
        raise ValueError(f"cannot batch mixed record types: {mixed}")

    return ImportBatch(
        record_type=records[0].record_type,
        rows=tuple(record.as_row() for record in records),
        strict_fields=strict_fields,
        duplicate_policy=duplicate_policy,
        source_name=source_name,
        source_metadata=source_metadata,
    )
