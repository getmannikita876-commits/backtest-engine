"""Databento **archived delimited export** decoding.

Exact scope
-----------
This module implements one narrow capability. It is not a general Databento
integration, and the class name should not be read as claiming one:

===================================== ===============
Capability                            Status
===================================== ===============
Archived delimited (CSV) export decoding   implemented
Binary DBN decoding                        not implemented
Historical or live API acquisition         not implemented
Credential management                      not implemented
===================================== ===============

Why acquisition is out of scope
-------------------------------
Reading an archived file and fetching from a vendor API are different problems
with incompatible properties, and mixing them would compromise the one that
matters most here:

* Research must be reproducible. Decoding a fixed archived file returns the
  same records forever; a live API returns whatever the vendor currently holds,
  so a replay built on it could never be deterministic.
* The charter forbids secrets in source control, and this module needs none
  because it never authenticates.

Acquisition — subscribing, requesting a range, downloading — is a legitimate
future component. It simply belongs outside deterministic decoding, so that the
archived file remains the reproducible unit of research input.

A future binary-DBN backend may use the vendor SDK as an **optional**
dependency confined to this provider package; that does not compromise provider
independence, because the engine depends only on
:class:`~...provider.MarketDataProvider`. See ``docs/data-import.md``.

Vendor field semantics live in
:mod:`~quant_research_terminal.data_import.providers.databento_decoding`, which
is pure and independently testable. This module owns only file reading,
symbology, filtering, and streaming.

Temporal semantics
------------------
Trade and quote records default to ``ts_recv`` — when the data became
observable — rather than ``ts_event``, which would admit look-ahead bias.

Bars carry their **interval-close** timestamp, because a Databento ``ohlcv``
record is stamped at interval start while its values are not determined until
interval end. The interval must be supplied explicitly; it is never guessed.
See :func:`~...databento_decoding.bar_availability_timestamp` and
``docs/adr/ADR-002-bar-availability-time.md``.
"""

from __future__ import annotations

from collections.abc import Generator, Mapping
from datetime import timedelta
from pathlib import Path
from typing import Any, Final

from quant_research_terminal.data_import.contracts import ImportRecordType
from quant_research_terminal.data_import.providers.databento_decoding import (
    DEFAULT_TIMESTAMP_FIELDS,
    INSTRUMENT_ID_COLUMN,
    SCHEMA_RECORD_TYPES,
    SYMBOL_COLUMN,
    DatabentoSchema,
    DatabentoTimestampField,
    SubMicrosecondPolicy,
    decode_bar_fields,
    decode_quote_fields,
    decode_trade_fields,
)
from quant_research_terminal.data_import.providers.file_source import (
    is_excluded_by_request,
    iter_delimited_rows,
)
from quant_research_terminal.data_import.providers.provider import (
    ProviderCapabilities,
    ProviderCapabilityError,
    ProviderDecodeError,
    ProviderRequest,
    RecordStream,
)
from quant_research_terminal.data_import.raw_record import RawRecord

PROVIDER_NAME: Final = "databento"
DEFAULT_ENCODING: Final = "utf-8"

#: What this provider actually reads. Recorded as data so the limitation is
#: introspectable rather than only stated in prose.
INPUT_FORMAT: Final = "archived-delimited-export"


def _validated_symbol_mapping(mapping: Mapping[int, str] | None) -> dict[int, str]:
    """Return a defensive copy of a symbol mapping, rejecting blank symbols.

    A blank or whitespace-only symbol would satisfy the domain's ``min_length``
    check while carrying no instrument identity at all. Rejecting it here fails
    at the call site instead of days later inside a research result.
    """
    validated: dict[int, str] = {}
    for instrument_id, symbol in (mapping or {}).items():
        if not symbol.strip():
            raise ValueError(
                f"symbol_by_instrument_id[{instrument_id}] is blank; "
                f"an instrument symbol must contain a non-whitespace value"
            )
        validated[instrument_id] = symbol
    return validated


def _validated_bar_interval(
    schema: DatabentoSchema, interval: timedelta | None
) -> timedelta | None:
    """Validate the bar interval against the schema it will be applied to."""
    if schema is not DatabentoSchema.OHLCV:
        if interval is not None:
            raise ValueError(
                f"bar_interval does not apply to schema {schema.value!r}; "
                f"it is only meaningful for {DatabentoSchema.OHLCV.value!r}"
            )
        return None

    if interval is None:
        raise ValueError(
            f"schema {DatabentoSchema.OHLCV.value!r} requires an explicit bar_interval: "
            f"a Databento bar is stamped at its interval start, so without the interval "
            f"there is no way to know when its values became available"
        )
    if interval <= timedelta(0):
        raise ValueError(f"bar_interval must be strictly positive, got {interval!r}")
    return interval


class DatabentoMarketDataProvider:
    """Reads one Databento schema from one archived export file.

    A provider instance is bound to a single file and a single schema, because
    an export's header fixes the shape of every row in it. Importing several
    schemas means constructing several providers, which keeps each instance's
    decoding rules trivial and independently testable.
    """

    def __init__(
        self,
        *,
        path: Path,
        schema: DatabentoSchema,
        symbol_by_instrument_id: Mapping[int, str] | None = None,
        bar_interval: timedelta | None = None,
        timestamp_field: DatabentoTimestampField | None = None,
        sub_microsecond_policy: SubMicrosecondPolicy = SubMicrosecondPolicy.REJECT,
        provider_name: str = PROVIDER_NAME,
        encoding: str = DEFAULT_ENCODING,
    ) -> None:
        """Bind the provider to an archived export.

        Args:
            path: The export file. It is not opened until :meth:`fetch` is
                iterated.
            schema: Which Databento schema the file contains.
            symbol_by_instrument_id: Resolves the vendor's numeric
                ``instrument_id`` to an instrument symbol. Required only when
                the export carries no ``symbol`` column, which is the case
                unless it was requested with symbol mapping enabled. Symbols
                are validated eagerly; a blank one is rejected here rather than
                allowed to become an instrument identity.
            bar_interval: The duration each ``ohlcv`` row covers. **Required**
                for the OHLCV schema and rejected for the others. It cannot be
                guessed: without it there is no way to know when a bar's values
                became available, and assuming one would silently reintroduce
                look-ahead bias.
            timestamp_field: Which vendor timestamp becomes the record
                timestamp. Defaults per schema — ``ts_recv`` where available,
                because ``ts_event`` would admit look-ahead bias.
            sub_microsecond_policy: How to handle nanosecond precision that
                cannot be represented. Defaults to rejecting rather than
                silently discarding it.
            provider_name: Identifier stamped onto each produced record.
            encoding: Text encoding used to read the file.

        Raises:
            ValueError: if ``bar_interval`` is missing for the OHLCV schema,
                supplied for a non-bar schema, not strictly positive, or if a
                supplied symbol mapping contains a blank symbol.
        """
        self._path = path
        self._schema = schema
        self._record_type = SCHEMA_RECORD_TYPES[schema]
        self._symbol_by_instrument_id = _validated_symbol_mapping(symbol_by_instrument_id)
        self._bar_interval = _validated_bar_interval(schema, bar_interval)
        self._timestamp_field = timestamp_field or DEFAULT_TIMESTAMP_FIELDS[schema]
        self._policy = sub_microsecond_policy
        self._provider_name = provider_name
        self._encoding = encoding

    @property
    def path(self) -> Path:
        """Return the bound export path."""
        return self._path

    @property
    def schema(self) -> DatabentoSchema:
        """Return the Databento schema this instance decodes."""
        return self._schema

    @property
    def input_format(self) -> str:
        """Return the input this provider decodes. See :data:`INPUT_FORMAT`."""
        return INPUT_FORMAT

    @property
    def bar_interval(self) -> timedelta | None:
        """Return the configured bar interval, or ``None`` for non-bar schemas.

        Emitted bar timestamps are interval *close*. Subtracting this interval
        recovers the vendor's original interval-start timestamp exactly, which
        is what keeps the shift a reversible decode rather than a mutation.
        """
        return self._bar_interval

    @property
    def timestamp_field(self) -> DatabentoTimestampField:
        """Return the vendor timestamp used as the record timestamp."""
        return self._timestamp_field

    @property
    def sub_microsecond_policy(self) -> SubMicrosecondPolicy:
        """Return the configured sub-microsecond precision policy."""
        return self._policy

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
            symbol = self._resolve_symbol(row, source_index)
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
        if self._schema is DatabentoSchema.TRADES:
            return decode_trade_fields(
                row,
                symbol=symbol,
                timestamp_field=self._timestamp_field,
                policy=self._policy,
            )
        if self._schema is DatabentoSchema.MBP_1:
            return decode_quote_fields(
                row,
                symbol=symbol,
                timestamp_field=self._timestamp_field,
                policy=self._policy,
            )

        # Construction guarantees an interval for the OHLCV schema.
        interval = self._bar_interval
        if interval is None:  # pragma: no cover - unreachable via the constructor
            raise ProviderDecodeError("bar interval is required to decode OHLCV rows")
        return decode_bar_fields(
            row,
            symbol=symbol,
            timestamp_field=self._timestamp_field,
            policy=self._policy,
            interval=interval,
        )

    def _resolve_symbol(self, row: Mapping[str, str], source_index: int) -> str:
        """Return the instrument symbol for a vendor row.

        Databento records identify an instrument by a numeric, vendor-local
        ``instrument_id``. That identifier is not an instrument symbol, and
        emitting it as one would corrupt instrument identity across the whole
        platform — nothing downstream validates the symbol's shape, so a bad
        value would flow silently into the domain and storage.

        Resolution is therefore strict and never guesses. Both available
        sources are consulted rather than short-circuiting on the first, so a
        disagreement between the export and the caller's mapping is reported
        instead of being silently resolved in favour of one of them: two
        sources contradicting each other about instrument identity is a defect
        worth stopping for.

        Raises:
            ProviderDecodeError: if the instrument cannot be resolved, or if the
                export and the supplied mapping disagree about it.
        """
        embedded = row.get(SYMBOL_COLUMN, "").strip()
        mapped = self._mapped_symbol(row, source_index)

        if embedded and mapped is not None and embedded != mapped:
            raise ProviderDecodeError(
                f"{self._path} row {source_index} has conflicting instrument identity: "
                f"the export says {embedded!r} but symbol_by_instrument_id says {mapped!r}"
            )

        resolved = embedded or mapped
        if not resolved:
            raise ProviderDecodeError(
                f"{self._path} row {source_index} has no usable {SYMBOL_COLUMN!r} value and "
                f"no {INSTRUMENT_ID_COLUMN!r} mapping; the instrument cannot be identified"
            )
        return resolved

    def _mapped_symbol(self, row: Mapping[str, str], source_index: int) -> str | None:
        """Return the symbol the caller's mapping gives this row, if any."""
        raw_id = row.get(INSTRUMENT_ID_COLUMN, "").strip()
        if not raw_id:
            return None

        try:
            instrument_id = int(raw_id)
        except ValueError as exc:
            raise ProviderDecodeError(
                f"{self._path} row {source_index} has a non-numeric "
                f"{INSTRUMENT_ID_COLUMN} {raw_id!r}"
            ) from exc

        mapped = self._symbol_by_instrument_id.get(instrument_id)
        if mapped is None and not row.get(SYMBOL_COLUMN, "").strip():
            raise ProviderDecodeError(
                f"{self._path} row {source_index} references unmapped "
                f"{INSTRUMENT_ID_COLUMN} {instrument_id}; supply symbol_by_instrument_id "
                f"or export with symbol mapping enabled"
            )
        return mapped


def databento_record_type(schema: DatabentoSchema) -> ImportRecordType:
    """Return the import record type a Databento schema produces."""
    return SCHEMA_RECORD_TYPES[schema]
