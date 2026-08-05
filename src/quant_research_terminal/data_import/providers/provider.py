"""Vendor-neutral market-data provider interfaces.

The engine must never depend on a specific data vendor. Every provider —
CSV, Databento, ThetaData, or anything added later — satisfies the same
structural interface, so the import pipeline can be written and tested once
against :class:`MarketDataProvider` and reused unchanged for every vendor.

Three contracts make that possible:

* :class:`ProviderRequest` — what the caller wants, expressed in domain terms
  (record type, instrument, optional UTC window). It contains no vendor
  concepts such as dataset codes, symbol roots, or API parameters. Translating
  a request into vendor-specific arguments is each provider's private problem.
* :class:`ProviderCapabilities` — what a provider can actually do, declared as
  data so callers can branch on capability instead of on class identity.
* :class:`MarketDataProvider` — the fetch interface itself.

Providers are pure readers. They never write to storage, never mutate their
inputs, and never perform trading calculations.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import datetime
from types import TracebackType
from typing import Protocol, Self, runtime_checkable

from pydantic import BaseModel, ConfigDict, field_validator, model_validator

from quant_research_terminal.data_import.contracts import ImportRecordType
from quant_research_terminal.data_import.raw_record import RawRecord
from quant_research_terminal.data_import.time_semantics import UtcTimestampError, require_utc


class ProviderError(RuntimeError):
    """Base class for every provider-level failure."""


class ProviderNotConfiguredError(ProviderError):
    """Raised when a provider is used before it has a working implementation.

    Vendor integrations that ship as interface-only stubs raise this instead of
    returning empty results, so an unconfigured vendor can never be mistaken for
    a vendor that legitimately returned no data.
    """


class ProviderCapabilityError(ProviderError):
    """Raised when a request asks for something the provider does not support."""


class ProviderDecodeError(ProviderError):
    """Raised when a source is structurally unreadable.

    This covers defects in the *container* — a missing header, a row with the
    wrong number of columns — not defects in the data values themselves. Bad
    values are passed through as raw fields and reported by validation.
    """


class ProviderCapabilities(BaseModel):
    """Immutable declaration of what a provider supports.

    Attributes:
        provider_name: Stable identifier recorded on every produced record.
        record_types: Record types the provider can serve.
        supports_time_window: Whether ``start``/``end`` on a request are honoured.
        requires_credentials: Whether real use needs operator-supplied secrets.
            Credentials are never read, stored, or defaulted by this package.
        is_implemented: ``False`` for interface-only vendor stubs. Callers can
            use this to skip vendors that cannot yet return data.
    """

    model_config = ConfigDict(frozen=True, strict=True)

    provider_name: str
    record_types: frozenset[ImportRecordType]
    supports_time_window: bool = False
    requires_credentials: bool = False
    is_implemented: bool = False

    def supports(self, record_type: ImportRecordType) -> bool:
        """Return whether ``record_type`` is served by this provider."""
        return record_type in self.record_types


class ProviderRequest(BaseModel):
    """Immutable, vendor-neutral description of one data request.

    The optional time window is half-open, ``[start, end)``, so that adjacent
    windows tile a timeline without overlapping or dropping a boundary record.

    Both bounds must be fixed-offset UTC datetimes. They are validated on
    construction and never converted: a naive or non-UTC bound is a caller
    defect and is rejected at once rather than silently reinterpreted.
    """

    model_config = ConfigDict(frozen=True, strict=True)

    record_type: ImportRecordType
    instrument_symbol: str
    start: datetime | None = None
    end: datetime | None = None

    @field_validator("instrument_symbol")
    @classmethod
    def _validate_symbol(cls, value: str) -> str:
        if not value:
            raise ValueError("instrument_symbol must not be empty")
        if value != value.strip():
            # Reject rather than strip: silently trimming an identifier would
            # let two different source spellings collapse into one instrument.
            raise ValueError("instrument_symbol must not have surrounding whitespace")
        return value

    @field_validator("start", "end")
    @classmethod
    def _validate_utc_bound(cls, value: datetime | None) -> datetime | None:
        if value is None:
            return None
        try:
            return require_utc(value)
        except UtcTimestampError as exc:
            raise ValueError(str(exc)) from exc

    @model_validator(mode="after")
    def _validate_window(self) -> Self:
        if self.start is not None and self.end is not None and self.end <= self.start:
            raise ValueError("end must be strictly after start")
        return self

    def contains(self, timestamp: datetime) -> bool:
        """Return whether ``timestamp`` falls inside the half-open window.

        An unbounded side always matches, so a request with no window at all
        accepts every timestamp.
        """
        if self.start is not None and timestamp < self.start:
            return False
        return not (self.end is not None and timestamp >= self.end)


class RecordStream:
    """A closeable, context-managed stream of raw records.

    A streaming provider holds an operating-system resource — a file handle, a
    socket — for as long as the caller is reading. Returning a bare
    :class:`~collections.abc.Iterator` leaves no way to release that resource
    through the interface, so a caller that stops early can only rely on
    garbage collection to close it. That is not deterministic, and on a long
    research run it exhausts handles.

    This type makes release explicit and is the declared return type of
    :meth:`MarketDataProvider.fetch`, so every provider — not just the CSV
    one — offers the same guarantee. It supports three usage styles:

        # 1. Read to completion; the stream closes itself.
        records = list(provider.fetch(request))

        # 2. As a context manager.
        with provider.fetch(request) as stream:
            for record in stream:
                ...

        # 3. With contextlib.closing, or an explicit close().
        with closing(provider.fetch(request)) as stream:
            ...

    Closing is idempotent and terminal: a closed stream yields nothing further
    rather than raising, so a consumer that closes early and then iterates once
    more sees a clean end of stream instead of an error.
    """

    def __init__(self, records: Iterator[RawRecord]) -> None:
        """Wrap ``records``, taking ownership of whatever backs it."""
        self._records = records
        self._closed = False

    @property
    def closed(self) -> bool:
        """Return whether the stream has been closed."""
        return self._closed

    def __iter__(self) -> RecordStream:
        """Return self; a stream is its own iterator."""
        return self

    def __next__(self) -> RawRecord:
        """Return the next record, closing the stream once it is exhausted."""
        if self._closed:
            raise StopIteration
        try:
            return next(self._records)
        except BaseException:
            # Covers normal exhaustion and any error raised while producing a
            # record. Either way the underlying resource must be released
            # before the exception reaches the caller.
            self.close()
            raise

    def close(self) -> None:
        """Release the underlying resource. Safe to call more than once."""
        if self._closed:
            return
        self._closed = True
        closer = getattr(self._records, "close", None)
        if callable(closer):
            closer()

    def __enter__(self) -> RecordStream:
        """Enter a context that closes the stream on exit."""
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        """Close the stream, whether or not the block raised."""
        self.close()


@runtime_checkable
class MarketDataProvider(Protocol):
    """Structural interface that every market-data provider satisfies.

    Implementations must be deterministic: fetching twice from an unchanged
    source yields identical records in an identical order. Providers must not
    depend on wall-clock time, locale, filesystem iteration order, or any
    other ambient machine state.
    """

    @property
    def capabilities(self) -> ProviderCapabilities:
        """Return this provider's immutable capability declaration."""
        ...

    def fetch(self, request: ProviderRequest) -> RecordStream:
        """Return a closeable stream of records matching ``request``.

        The stream is lazy: no resource is acquired until it is iterated, and
        a request rejected on capability grounds acquires none at all. The
        caller owns the returned stream and must close it if it stops early;
        see :class:`RecordStream`.

        Raises:
            ProviderCapabilityError: if the request asks for an unsupported
                record type.
            ProviderNotConfiguredError: if the provider is an interface-only stub.
            ProviderDecodeError: if the underlying source is structurally unreadable.
        """
        ...
