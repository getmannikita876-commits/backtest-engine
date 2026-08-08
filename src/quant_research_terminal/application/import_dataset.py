"""The first vertical slice: import one dataset from a provider to Parquet.

This use case is orchestration only. Every rule it applies is defined in — and
imported from — the layer that owns it::

    MarketDataProvider          (data_import.providers.provider — interface)
      -> RawRecord stream       (provider-owned resource, closed here)
      -> validation             (data_import.validation, via the batch API)
      -> normalization          (data_import.normalization, via the batch API)
      -> deterministic ordering (data_import.event_order, via the batch API)
      -> Parquet write          (data.parquet_store)
      -> Parquet read           (data.parquet_store)
      -> structural equality    (domain model equality, checked here)
      -> ImportDatasetResult

Nothing here validates a price, decides a duplicate's fate, derives a bar's
identity, or encodes a fixed-point value. The one comparison the application
owns — written records equal read-back records — is a fact about the *whole
pipeline* that no single lower layer is positioned to state.

Transaction boundary
--------------------
A dataset either appears at the output path fully written and verified, or the
output path is untouched. The use case stages the file beside the target
(written through the storage layer's own atomic write), reads it back through
the real read path, verifies structural equality, and only then promotes it
onto the target with one atomic ``os.replace``. Validation failure, a
conflicting bar, a provider decode error, a storage failure, or a verification
mismatch therefore never replaces an existing target and never leaves a
staging artifact behind. The storage layer's atomicity guarantees one *file*
is never half-written; this boundary composes that same primitive into a
larger guarantee — never *published unverified* — without reimplementing it.

Current implementation note
---------------------------
The stream is materialised into memory before validation, because the batch
API (which owns policy, ordering, and reporting) is eager. That is an
implementation detail of this phase, not part of the use-case contract: the
public API accepts a provider and returns a result, and says nothing about
materialisation. Tick-scale streaming imports are explicitly out of scope
until measured (ADR-007).
"""

from __future__ import annotations

import os
from collections.abc import Sequence
from pathlib import Path
from typing import Final, cast

from pydantic import BaseModel, ConfigDict

from quant_research_terminal.application.ports import MarketDataProvider, ProviderRequest
from quant_research_terminal.data.parquet_store import (
    read_bars,
    read_quotes,
    read_trades,
    write_bars,
    write_quotes,
    write_trades,
)
from quant_research_terminal.data_import.contracts import (
    DuplicatePolicy,
    ImportBatch,
    ImportRecordType,
    ValidationReport,
)
from quant_research_terminal.data_import.normalization import DomainRecord
from quant_research_terminal.data_import.pipeline import validate_import_batch
from quant_research_terminal.data_import.raw_record import (
    RawRecord,
    raw_records_to_import_batch,
)
from quant_research_terminal.domain.models import Bar, Quote, Trade

#: Suffix of the staging file written beside the target during an import.
#: Distinct from the storage layer's ``.partial`` (which guards a single write)
#: — this guards the publish step, so the two never collide.
STAGING_SUFFIX: Final[str] = ".importing"


class ApplicationError(Exception):
    """Base class for application-layer failures."""


class ImportDatasetError(ApplicationError):
    """Raised when a dataset import cannot proceed to persistence.

    Covers a validation failure (the :attr:`report` carries the full
    accounting, including conflicting-bar and duplicate diagnostics) and a
    record stream that violates the one-record-type-per-dataset policy, in
    which case :attr:`report` is ``None`` because validation never ran.
    """

    def __init__(self, message: str, *, report: ValidationReport | None = None) -> None:
        super().__init__(message)
        self.report = report


class VerificationError(ApplicationError):
    """Raised when read-back records do not equal the records just written.

    Reaching this means the write path, the file, and the read path disagree —
    a storage defect, not a data defect — so the staged file is discarded and
    the target is left untouched.
    """


class ImportDatasetResult(BaseModel):
    """Immutable summary of one successful dataset import.

    A result exists only for a run that validated, persisted, and verified;
    every failure raises instead. The counts restate the validation report's
    accounting so a caller can log the outcome without unpacking the report,
    and the report itself is carried in full — warnings such as resolved
    duplicates are facts about the dataset, not noise.

    This is not a dataset manifest. It records what this use case actually
    did; identifiers, environment capture, and cataloguing belong to later
    phases (ADR-007).
    """

    model_config = ConfigDict(frozen=True, strict=True)

    success: bool
    record_type: ImportRecordType
    output_path: Path
    total_rows: int
    accepted_rows: int
    rejected_rows: int
    warning_count: int
    error_count: int
    records_written: int
    records_verified: int
    validation_report: ValidationReport


class ImportDatasetUseCase:
    """Imports one dataset: provider in, verified Parquet file out."""

    def run(
        self,
        *,
        provider: MarketDataProvider,
        request: ProviderRequest,
        output_path: Path,
        strict_fields: bool = True,
        duplicate_policy: DuplicatePolicy = DuplicatePolicy.REJECT,
    ) -> ImportDatasetResult:
        """Import the records matching ``request`` into ``output_path``.

        Args:
            provider: Any :class:`MarketDataProvider`. Its stream is closed
                deterministically whether the run succeeds or fails.
            request: What to import, in vendor-neutral terms. The request's
                record type is the single type the dataset may contain.
            output_path: Target Parquet file. Replaced atomically on success,
                untouched on any failure.
            strict_fields: Passed to the import batch; unknown source fields
                are reported when true.
            duplicate_policy: Which copy of an exact duplicate survives.
                Applies to exact duplicates only; conflicts reject (ADR-005).

        Returns:
            The result of a fully verified import.

        Raises:
            ImportDatasetError: if validation fails or the stream mixes
                record types.
            VerificationError: if read-back does not equal what was written.
            ProviderError: from the provider, unchanged — a structurally
                unreadable source names itself better than a wrapper would.
            StorageError: from the storage layer, unchanged, for the same
                reason.
        """
        with provider.fetch(request) as stream:
            records = list(stream)

        batch = self._build_batch(
            records,
            request=request,
            strict_fields=strict_fields,
            duplicate_policy=duplicate_policy,
            source_name=provider.capabilities.provider_name,
        )

        accepted, report = validate_import_batch(batch)
        if not report.success:
            raise ImportDatasetError(
                f"validation failed: {report.error_count} error(s), "
                f"{report.fatal_count} fatal issue(s) across {report.total_rows} row(s)",
                report=report,
            )

        written = self._persist_verified(accepted, request.record_type, output_path)

        return ImportDatasetResult(
            success=True,
            record_type=request.record_type,
            output_path=output_path,
            total_rows=report.total_rows,
            accepted_rows=report.accepted_rows,
            rejected_rows=report.rejected_rows,
            warning_count=report.warning_count,
            error_count=report.error_count,
            records_written=len(accepted),
            records_verified=written,
            validation_report=report,
        )

    @staticmethod
    def _build_batch(
        records: Sequence[RawRecord],
        *,
        request: ProviderRequest,
        strict_fields: bool,
        duplicate_policy: DuplicatePolicy,
        source_name: str,
    ) -> ImportBatch:
        """Adapt the stream to the batch API, enforcing one record type.

        The dataset's type is the *request's* type: a provider that yields
        anything else — mixed or uniformly wrong — is refused here, before
        validation, because no downstream layer re-checks it. An empty stream
        becomes an empty batch rather than an error here, so the emptiness is
        diagnosed by the validation pipeline's own ``EMPTY_BATCH`` rule and
        reported through the same contract as every other defect.
        """
        unexpected = sorted(
            {
                record.record_type.value
                for record in records
                if record.record_type is not request.record_type
            }
        )
        if unexpected:
            raise ImportDatasetError(
                f"provider produced record type(s) {', '.join(unexpected)} for a "
                f"{request.record_type.value} import; a dataset holds exactly one "
                f"record type per file"
            )

        if not records:
            return ImportBatch(
                record_type=request.record_type,
                rows=(),
                strict_fields=strict_fields,
                duplicate_policy=duplicate_policy,
                source_name=source_name,
            )

        return raw_records_to_import_batch(
            records,
            strict_fields=strict_fields,
            duplicate_policy=duplicate_policy,
            source_name=source_name,
        )

    def _persist_verified(
        self,
        accepted: Sequence[DomainRecord],
        record_type: ImportRecordType,
        output_path: Path,
    ) -> int:
        """Write, read back, verify, and atomically publish the dataset.

        The staging file lives in the target's directory so the final
        ``os.replace`` never crosses a filesystem boundary. The ``finally``
        unlink is a no-op after a successful replace and removes the staging
        file on every failure path, so no partial application artifact can
        remain regardless of where the failure occurred.
        """
        staging = output_path.with_name(output_path.name + STAGING_SUFFIX)
        try:
            self._write(record_type, staging, accepted)
            restored = self._read(record_type, staging)
            self._verify(accepted, restored, output_path)
            os.replace(staging, output_path)
        finally:
            staging.unlink(missing_ok=True)
        return len(restored)

    @staticmethod
    def _write(record_type: ImportRecordType, path: Path, records: Sequence[DomainRecord]) -> None:
        # The casts are sound: _build_batch guaranteed a single record type,
        # and the batch pipeline normalizes each record to the model matching
        # its declared type.
        if record_type is ImportRecordType.TRADE:
            write_trades(path, cast("Sequence[Trade]", records))
        elif record_type is ImportRecordType.QUOTE:
            write_quotes(path, cast("Sequence[Quote]", records))
        else:
            write_bars(path, cast("Sequence[Bar]", records))

    @staticmethod
    def _read(record_type: ImportRecordType, path: Path) -> tuple[DomainRecord, ...]:
        if record_type is ImportRecordType.TRADE:
            return read_trades(path)
        if record_type is ImportRecordType.QUOTE:
            return read_quotes(path)
        return read_bars(path)

    @staticmethod
    def _verify(
        expected: Sequence[DomainRecord],
        restored: Sequence[DomainRecord],
        output_path: Path,
    ) -> None:
        """Require the read-back records to equal the written records exactly.

        Comparison is domain-model equality — every field of every record, in
        order, with ``Decimal`` values compared as exact numbers — not a file
        size, a hash, or a stringified form. A mismatch is reported with the
        first offending position so the defect is locatable.
        """
        if len(restored) != len(expected):
            raise VerificationError(
                f"read-back of {output_path} returned {len(restored)} record(s) "
                f"where {len(expected)} were written"
            )
        for index, (written, read_back) in enumerate(zip(expected, restored, strict=True)):
            if written != read_back:
                raise VerificationError(
                    f"read-back of {output_path} differs from the written dataset "
                    f"at position {index}: wrote {written!r}, read {read_back!r}"
                )
