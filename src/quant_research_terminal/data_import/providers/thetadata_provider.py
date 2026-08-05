"""ThetaData **archived delimited export** decoding — **EXPERIMENTAL**.

Verification status
-------------------
This decoder was written **without inspecting a real ThetaData export**. Every
vendor-specific detail it relies on — column names, the ``YYYYMMDD`` date form,
the millisecond unit of ``ms_of_day``, exchange-local time, decimal prices,
zero-as-absent — is an *assumption*, not a verified fact. None has been
confirmed against an official primary source or a sample file.

It is therefore **not production-compatible**. Treat output as provisional
until the assumptions are checked against a real export.

The project's own ThetaData tests do **not** constitute vendor verification:
they were written from this implementation, so they prove internal consistency
and nothing about fidelity to the vendor.

To settle the assumptions, run the read-only inspector over a small real
export and compare its report with the table in ``docs/data-import.md``::

    python -m quant_research_terminal.data_import.providers.thetadata_inspection FILE

Exact scope
-----------
This module implements one narrow capability. The class name should not be read
as claiming a general ThetaData integration:

========================================== ===============
Capability                                 Status
========================================== ===============
Archived delimited (CSV) export decoding   implemented
Trade, quote, and OHLC schemas             implemented
Live / Theta Terminal API access           not implemented
Credential management                      not implemented
Options, Greeks, implied volatility        not implemented
Order-book depth                           not implemented
========================================== ===============

Options are ThetaData's primary product and remain out of scope: the domain
models no option contract, so declaring support would mean inventing semantics
the rest of the system cannot represent. Only the three record types the domain
models today are decoded.

Why acquisition is out of scope
-------------------------------
Reading an archived file and fetching from a vendor endpoint have incompatible
properties, and mixing them would compromise reproducibility: decoding a fixed
archived file returns the same records forever, whereas an endpoint returns
whatever the vendor currently holds. This module never authenticates and never
opens a socket, so it needs no secrets.

Temporal semantics
------------------
ThetaData splits time across ``date`` and ``ms_of_day``, expressed in
**exchange-local wall-clock time**. Turning that into an instant requires a
timezone, and this provider never assumes one — see ``session_timezone``.

Bars carry their **interval-close** timestamp, so a completed bar can never be
observed at its interval start. See
``docs/adr/ADR-002-bar-availability-time.md``.
"""

from __future__ import annotations

from collections.abc import Generator, Mapping
from datetime import timedelta, tzinfo
from pathlib import Path
from typing import Any, Final

from quant_research_terminal.data_import.contracts import ImportRecordType
from quant_research_terminal.data_import.providers.file_source import (
    is_excluded_by_request,
    iter_delimited_rows,
    reconcile_symbols,
    require_non_blank_symbol,
)
from quant_research_terminal.data_import.providers.provider import (
    ProviderCapabilities,
    ProviderCapabilityError,
    ProviderDecodeError,
    ProviderRequest,
    RecordStream,
)
from quant_research_terminal.data_import.providers.thetadata_decoding import (
    SCHEMA_RECORD_TYPES,
    SYMBOL_COLUMN,
    BarTimestampMeaning,
    ThetaDataSchema,
    decode_bar_fields,
    decode_quote_fields,
    decode_trade_fields,
)
from quant_research_terminal.data_import.raw_record import RawRecord
from quant_research_terminal.domain.models import TradeSide

PROVIDER_NAME: Final = "thetadata"
DEFAULT_ENCODING: Final = "utf-8"

#: What this provider actually reads. Recorded as data so the limitation is
#: introspectable rather than only stated in prose.
INPUT_FORMAT: Final = "archived-delimited-export"

#: Whether this decoder's vendor assumptions have been checked against a real
#: export or an official schema. Recorded as data so a caller can gate on it
#: rather than having to read the docstring.
#:
#: ``"experimental-unverified"`` means no assumption has been confirmed by a
#: primary source or a sample file. It changes to a verified status only when
#: that evidence exists — not when the tests pass, since the tests were written
#: from this implementation.
VERIFICATION_STATUS: Final = "experimental-unverified"


def _validated_bar_settings(
    schema: ThetaDataSchema,
    interval: timedelta | None,
    meaning: BarTimestampMeaning | None,
) -> tuple[timedelta | None, BarTimestampMeaning | None]:
    """Validate the OHLC-only settings against the schema they apply to."""
    if schema is not ThetaDataSchema.OHLC:
        if interval is not None or meaning is not None:
            raise ValueError(
                f"bar_interval and bar_timestamp_meaning do not apply to schema "
                f"{schema.value!r}; they are only meaningful for "
                f"{ThetaDataSchema.OHLC.value!r}"
            )
        return None, None

    if interval is None:
        raise ValueError(
            f"schema {ThetaDataSchema.OHLC.value!r} requires an explicit bar_interval: "
            f"without it there is no way to know when a bar's values became available"
        )
    if interval <= timedelta(0):
        raise ValueError(f"bar_interval must be strictly positive, got {interval!r}")
    if meaning is None:
        raise ValueError(
            f"schema {ThetaDataSchema.OHLC.value!r} requires an explicit "
            f"bar_timestamp_meaning: reading an interval-end timestamp as an "
            f"interval start would shift every bar by one interval and create "
            f"look-ahead bias"
        )
    return interval, meaning


class ThetaDataMarketDataProvider:
    """Reads one ThetaData schema from one archived export file.

    A provider instance is bound to a single file and a single schema, because
    an export's header fixes the shape of every row in it. Importing several
    schemas means constructing several providers, which keeps each instance's
    decoding rules trivial and independently testable.
    """

    def __init__(
        self,
        *,
        path: Path,
        schema: ThetaDataSchema,
        session_timezone: tzinfo,
        instrument_symbol: str | None = None,
        bar_interval: timedelta | None = None,
        bar_timestamp_meaning: BarTimestampMeaning | None = None,
        side_by_vendor_code: Mapping[str, TradeSide] | None = None,
        provider_name: str = PROVIDER_NAME,
        encoding: str = DEFAULT_ENCODING,
    ) -> None:
        """Bind the provider to an archived export.

        Args:
            path: The export file. It is not opened until :meth:`fetch` is
                iterated.
            schema: Which ThetaData schema the file contains.
            session_timezone: The zone the file's wall-clock times are
                expressed in. **Required**, because the vendor's ``date`` and
                ``ms_of_day`` columns are exchange-local rather than UTC, and
                assuming a zone would silently shift every record. ThetaData
                publishes US Eastern times; pass
                ``zoneinfo.ZoneInfo("America/New_York")`` where the platform
                provides the zone database, or an explicit fixed offset where
                the data is known not to span a transition.
            instrument_symbol: The instrument the whole file describes. Needed
                unless the export carries its own ``symbol`` column.
            bar_interval: The duration each OHLC row covers. **Required** for
                the OHLC schema and rejected for the others.
            bar_timestamp_meaning: Whether an OHLC row's timestamp marks the
                interval's start or its end. **Required** for the OHLC schema;
                there is no default because a wrong assumption shifts every bar
                by one interval and is invisible.
            side_by_vendor_code: Optional map from a vendor side code to a
                :class:`TradeSide`. Without it every trade decodes to
                ``UNKNOWN``, which is what the archived trade schema actually
                supports.
            provider_name: Identifier stamped onto each produced record.
            encoding: Text encoding used to read the file.

        Raises:
            ValueError: if the OHLC settings are missing, misapplied, or not
                strictly positive, or if ``instrument_symbol`` is blank.
        """
        self._path = path
        self._schema = schema
        self._record_type = SCHEMA_RECORD_TYPES[schema]
        self._session_timezone = session_timezone
        self._instrument_symbol = (
            require_non_blank_symbol(instrument_symbol, "instrument_symbol")
            if instrument_symbol is not None
            else None
        )
        self._bar_interval, self._bar_timestamp_meaning = _validated_bar_settings(
            schema, bar_interval, bar_timestamp_meaning
        )
        self._side_by_vendor_code = (
            dict(side_by_vendor_code) if side_by_vendor_code is not None else None
        )
        self._provider_name = provider_name
        self._encoding = encoding

    @property
    def path(self) -> Path:
        """Return the bound export path."""
        return self._path

    @property
    def schema(self) -> ThetaDataSchema:
        """Return the ThetaData schema this instance decodes."""
        return self._schema

    @property
    def input_format(self) -> str:
        """Return the input this provider decodes. See :data:`INPUT_FORMAT`."""
        return INPUT_FORMAT

    @property
    def verification_status(self) -> str:
        """Return whether the vendor assumptions have been verified.

        See :data:`VERIFICATION_STATUS`. While this reads
        ``"experimental-unverified"`` the decoder is not production-compatible.
        """
        return VERIFICATION_STATUS

    @property
    def session_timezone(self) -> tzinfo:
        """Return the zone the file's wall-clock times are expressed in."""
        return self._session_timezone

    @property
    def bar_interval(self) -> timedelta | None:
        """Return the configured bar interval, or ``None`` for non-bar schemas.

        Emitted bar timestamps are interval *close*. Subtracting this interval
        recovers the interval start exactly.
        """
        return self._bar_interval

    @property
    def bar_timestamp_meaning(self) -> BarTimestampMeaning | None:
        """Return what an OHLC row's vendor timestamp refers to."""
        return self._bar_timestamp_meaning

    @property
    def capabilities(self) -> ProviderCapabilities:
        """Return the capability declaration for this provider instance.

        ``requires_credentials`` is false: reading an archived export needs no
        secrets. Obtaining that export does, but acquisition is not this
        provider's responsibility.
        """
        return ProviderCapabilities(
            provider_name=self._provider_name,
            record_types=frozenset({self._record_type}),
            supports_time_window=True,
            requires_credentials=False,
            is_implemented=True,
        )

    def fetch(self, request: ProviderRequest) -> RecordStream:
        """Return a closeable stream of raw records for ``request``, in file order.

        The request is checked eagerly, so an unsupported record type fails at
        the call site rather than on first iteration. The file is opened lazily
        on first iteration and the returned stream owns the handle; see
        :class:`RecordStream` for the ownership contract.

        Rows are filtered by instrument and by the request's half-open time
        window, and only ever on evidence: a row whose timestamp did not decode
        survives filtering so validation can report it.

        Raises:
            ProviderCapabilityError: if the request asks for a record type this
                instance's schema does not produce.
            ProviderDecodeError: if the export is structurally unreadable, or an
                instrument identity cannot be resolved.
        """
        if not self.capabilities.supports(request.record_type):
            raise ProviderCapabilityError(
                f"provider {self._provider_name!r} reading schema "
                f"{self._schema.value!r} does not serve record type "
                f"{request.record_type.value!r}"
            )
        return RecordStream(self._iter_records(request))

    def _iter_records(self, request: ProviderRequest) -> Generator[RawRecord, None, None]:
        for source_index, row in iter_delimited_rows(self._path, encoding=self._encoding):
            symbol = reconcile_symbols(
                embedded=row.get(SYMBOL_COLUMN),
                configured=self._instrument_symbol,
                context=f"{self._path} row {source_index}",
                configured_label="instrument_symbol",
            )
            fields = self._decode_fields(row, symbol=symbol)
            if is_excluded_by_request(fields, request):
                continue

            yield RawRecord(
                record_type=self._record_type,
                source_index=source_index,
                provider_name=self._provider_name,
                fields=fields,
            )

    def _decode_fields(self, row: Mapping[str, str], *, symbol: str) -> dict[str, Any]:
        """Dispatch to the field mapper for this instance's schema."""
        if self._schema is ThetaDataSchema.TRADE:
            return decode_trade_fields(
                row,
                symbol=symbol,
                session_timezone=self._session_timezone,
                side_by_vendor_code=self._side_by_vendor_code,
            )
        if self._schema is ThetaDataSchema.QUOTE:
            return decode_quote_fields(row, symbol=symbol, session_timezone=self._session_timezone)

        # Construction guarantees both settings for the OHLC schema.
        interval = self._bar_interval
        meaning = self._bar_timestamp_meaning
        if interval is None or meaning is None:  # pragma: no cover - unreachable
            raise ProviderDecodeError(
                "bar interval and timestamp meaning are required to decode OHLC rows"
            )
        return decode_bar_fields(
            row,
            symbol=symbol,
            session_timezone=self._session_timezone,
            interval=interval,
            meaning=meaning,
        )


def thetadata_record_type(schema: ThetaDataSchema) -> ImportRecordType:
    """Return the import record type a ThetaData schema produces."""
    return SCHEMA_RECORD_TYPES[schema]
