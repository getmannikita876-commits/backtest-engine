"""Minimal CSV market-data provider.

This is the only provider in Phase 1.3 with real behaviour, and its job is
deliberately narrow: turn CSV text into typed Python values. It performs
*decoding*, never validation and never normalization.

Decoding rules
--------------
* The timestamp column is parsed with :meth:`datetime.datetime.fromisoformat`.
  The result is passed through exactly as parsed. A value without an offset
  stays naive and a value with a non-UTC offset keeps that offset — the
  provider never attaches or shifts a timezone. Validation reports those rows.
* Price and size columns are parsed with :class:`~decimal.Decimal` directly
  from the source text, so a tick value never passes through binary floating
  point and is never rounded on the way in.
* Every other column is left as the original string.
* Text that cannot be parsed is carried through unchanged as a string rather
  than being dropped or defaulted, so validation can report the offending row
  and column instead of the file silently losing data.

Structural defects in the file itself — a missing header row, or a data row
whose column count disagrees with the header — raise
:class:`ProviderDecodeError`, because those make row-to-column mapping
ambiguous and no per-row diagnosis is meaningful.
"""

from __future__ import annotations

from collections.abc import Generator
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Final

from quant_research_terminal.data_import.contracts import ImportRecordType
from quant_research_terminal.data_import.providers.file_source import (
    is_excluded_by_request,
    iter_delimited_rows,
)
from quant_research_terminal.data_import.providers.provider import (
    ProviderCapabilities,
    ProviderCapabilityError,
    ProviderRequest,
    RecordStream,
)
from quant_research_terminal.data_import.raw_record import RawRecord
from quant_research_terminal.data_import.record_fields import (
    TIMESTAMP_FIELD,
    decimal_fields,
)

DEFAULT_PROVIDER_NAME: Final = "csv"
DEFAULT_ENCODING: Final = "utf-8"


class CsvMarketDataProvider:
    """Reads one record type from one delimited text file.

    A provider instance is bound to a single file and a single record type,
    because a CSV file's header fixes the shape of every row in it. Importing
    several record types means constructing several providers, which keeps the
    decoding rules per instance trivial and independently testable.
    """

    def __init__(
        self,
        *,
        path: Path,
        record_type: ImportRecordType,
        provider_name: str = DEFAULT_PROVIDER_NAME,
        encoding: str = DEFAULT_ENCODING,
    ) -> None:
        """Bind the provider to a source file.

        Args:
            path: The CSV file to read. It is not opened until :meth:`fetch`.
            record_type: The record shape every row in the file describes.
            provider_name: Identifier stamped onto each produced record.
            encoding: Text encoding used to read the file.
        """
        self._path = path
        self._record_type = record_type
        self._provider_name = provider_name
        self._encoding = encoding
        self._decimal_fields = decimal_fields(record_type)

    @property
    def path(self) -> Path:
        """Return the bound source path."""
        return self._path

    @property
    def capabilities(self) -> ProviderCapabilities:
        """Return the capability declaration for this provider instance."""
        return ProviderCapabilities(
            provider_name=self._provider_name,
            record_types=frozenset({self._record_type}),
            supports_time_window=True,
            requires_credentials=False,
            is_implemented=True,
        )

    def fetch(self, request: ProviderRequest) -> RecordStream:
        """Return a closeable stream of raw records for ``request``, in file order.

        The request is checked eagerly so an unsupported record type fails at
        the call site rather than on first iteration. The file itself is opened
        lazily on first iteration, so a large source is streamed rather than
        materialised, and a rejected request never opens a handle at all.

        Rows are filtered by instrument and by the request's half-open time
        window. A row is only ever filtered out on evidence: if its instrument
        column is absent, or its timestamp did not decode to a valid UTC
        datetime, the row is yielded so validation can report it. Filtering
        never suppresses a defect.

        Resource ownership
        ------------------
        The returned :class:`RecordStream` owns the open file handle and closes
        it deterministically when iteration finishes, when an exception
        propagates out of it, or when the caller closes the stream. No handle
        is ever left to garbage collection. A caller that stops early must
        close the stream, as a context manager or explicitly:

            with provider.fetch(request) as records:
                for record in records:
                    ...

        Each call returns an independent stream with its own handle, so two
        concurrent reads of the same file cannot disturb each other's position.
        """
        if not self.capabilities.supports(request.record_type):
            raise ProviderCapabilityError(
                f"provider {self._provider_name!r} does not serve "
                f"record type {request.record_type.value!r}"
            )
        return RecordStream(self._iter_records(request))

    def _iter_records(self, request: ProviderRequest) -> Generator[RawRecord, None, None]:
        for source_index, row in iter_delimited_rows(self._path, encoding=self._encoding):
            fields = {name: self._decode(name, text) for name, text in row.items()}
            if is_excluded_by_request(fields, request):
                continue

            yield RawRecord(
                record_type=self._record_type,
                source_index=source_index,
                provider_name=self._provider_name,
                fields=fields,
            )

    def _decode(self, field_name: str, text: str) -> Any:
        if field_name == TIMESTAMP_FIELD:
            try:
                return datetime.fromisoformat(text)
            except ValueError:
                return text
        if field_name in self._decimal_fields:
            try:
                return Decimal(text)
            except InvalidOperation:
                return text
        return text
