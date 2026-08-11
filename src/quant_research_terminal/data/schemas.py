from __future__ import annotations

from collections.abc import Mapping
from typing import Final

import polars as pl
import pyarrow as pa  # type: ignore[import-untyped]

from quant_research_terminal.data.contracts import (
    CANONICAL_IDENTITY_METADATA_KEY,
    LEGACY_SCHEMA_VERSION,
    PRICE_ENCODING,
    PRICE_PRECISION,
    PRICE_SCALE,
    RECORD_TYPE_METADATA_KEY,
    SCHEMA_NAME,
    SCHEMA_VERSION,
    TIMESTAMP_TIMEZONE,
)
from quant_research_terminal.domain.dataset_identity import RecordType, require_record_type
from quant_research_terminal.domain.futures_contract import (
    FuturesContractId,
    require_listed_contract,
)


def _arrow_schema_metadata(version: int) -> dict[str, bytes]:
    """Return the shared metadata block for a given schema version.

    The version is a **parameter**, not the module constant. The three
    version-2 schemas below are built from this same factory, so reading the
    current constant here would have silently made every version-2 file written
    after the version-3 bump claim to be version 3 — re-keying the entire
    existing corpus without a single line of the version-2 write path changing.
    """
    return {
        "schema_name": SCHEMA_NAME.encode("utf-8"),
        "schema_version": str(version).encode("utf-8"),
        "timestamp_timezone": TIMESTAMP_TIMEZONE.encode("utf-8"),
        "price_encoding": PRICE_ENCODING.encode("utf-8"),
        "price_precision": str(PRICE_PRECISION).encode("utf-8"),
        "price_scale": str(PRICE_SCALE).encode("utf-8"),
    }


#: Columns each version-3 record type carries after the identity columns.
#:
#: Version 3 replaces the single ``instrument_symbol`` with two columns that
#: answer two different questions: ``canonical_identity`` says *which listed
#: contract this is*, and ``vendor_symbol`` preserves *what the source called
#: it*. The second is provenance and never determines the first.
_V3_TAIL_FIELDS: Final[Mapping[RecordType, tuple[pa.Field, ...]]] = {
    RecordType.TRADE: (
        pa.field("price", pa.int64()),
        pa.field("size", pa.uint64()),
        pa.field("side", pa.utf8()),
    ),
    RecordType.QUOTE: (
        pa.field("bid", pa.int64()),
        pa.field("ask", pa.int64()),
        pa.field("bid_size", pa.uint64()),
        pa.field("ask_size", pa.uint64()),
    ),
    RecordType.BAR: (
        pa.field("interval_microseconds", pa.uint64()),
        pa.field("open", pa.int64()),
        pa.field("high", pa.int64()),
        pa.field("low", pa.int64()),
        pa.field("close", pa.int64()),
        pa.field("volume", pa.uint64()),
    ),
}


def v3_metadata(record_type: RecordType, contract: FuturesContractId) -> dict[str, bytes]:
    """Return the version-3 metadata block for one dataset.

    Carries the canonical identity as well as the per-row column. The
    redundancy is deliberate and load-bearing: empty artifacts are supported,
    and an artifact with no rows has nowhere else to record which contract it
    holds. On read the two are cross-checked, so they can never disagree
    silently.
    """
    metadata = _arrow_schema_metadata(SCHEMA_VERSION)
    metadata[RECORD_TYPE_METADATA_KEY.decode("ascii")] = record_type.value.encode("utf-8")
    metadata[CANONICAL_IDENTITY_METADATA_KEY.decode("ascii")] = contract.canonical().encode("utf-8")
    return metadata


def arrow_schema_v3(record_type: RecordType, contract: FuturesContractId) -> pa.Schema:
    """Return the version-3 Arrow schema for one record type and contract.

    Unlike the version-2 schemas this is **not** a module constant: the
    metadata carries the dataset's contract, so the schema is per-dataset.
    Anything that identified a record type by comparing against a module-level
    schema singleton must therefore take the record type explicitly instead.
    """
    require_record_type(record_type)
    require_listed_contract(contract)
    fields = (
        pa.field("timestamp", pa.timestamp("us", tz="UTC")),
        pa.field("canonical_identity", pa.utf8()),
        pa.field("vendor_symbol", pa.utf8()),
        *_V3_TAIL_FIELDS[record_type],
    )
    return pa.schema(list(fields), metadata=v3_metadata(record_type, contract))


def v3_column_names(record_type: RecordType) -> tuple[str, ...]:
    """Return the version-3 column names for a record type, in order."""
    require_record_type(record_type)
    return (
        "timestamp",
        "canonical_identity",
        "vendor_symbol",
        *(field.name for field in _V3_TAIL_FIELDS[record_type]),
    )


TRADE_ARROW_SCHEMA: pa.Schema = pa.schema(
    [
        pa.field("timestamp", pa.timestamp("us", tz="UTC")),
        pa.field("instrument_symbol", pa.utf8()),
        pa.field("price", pa.int64()),
        pa.field("size", pa.uint64()),
        pa.field("side", pa.utf8()),
    ],
    metadata=_arrow_schema_metadata(LEGACY_SCHEMA_VERSION),
)

QUOTE_ARROW_SCHEMA: pa.Schema = pa.schema(
    [
        pa.field("timestamp", pa.timestamp("us", tz="UTC")),
        pa.field("instrument_symbol", pa.utf8()),
        pa.field("bid", pa.int64()),
        pa.field("ask", pa.int64()),
        pa.field("bid_size", pa.uint64()),
        pa.field("ask_size", pa.uint64()),
    ],
    metadata=_arrow_schema_metadata(LEGACY_SCHEMA_VERSION),
)

#: Bars persist their availability timestamp plus the interval they cover.
#: Interval start is not stored separately because it is exactly
#: ``timestamp - interval``; storing both would allow the two to disagree.
BAR_ARROW_SCHEMA: pa.Schema = pa.schema(
    [
        pa.field("timestamp", pa.timestamp("us", tz="UTC")),
        pa.field("instrument_symbol", pa.utf8()),
        pa.field("interval_microseconds", pa.uint64()),
        pa.field("open", pa.int64()),
        pa.field("high", pa.int64()),
        pa.field("low", pa.int64()),
        pa.field("close", pa.int64()),
        pa.field("volume", pa.uint64()),
    ],
    metadata=_arrow_schema_metadata(LEGACY_SCHEMA_VERSION),
)

TRADE_POLARS_SCHEMA: pl.Schema = pl.Schema(
    [
        ("timestamp", pl.Datetime(time_unit="us", time_zone="UTC")),
        ("instrument_symbol", pl.Utf8),
        ("price", pl.Int64),
        ("size", pl.UInt64),
        ("side", pl.Utf8),
    ]
)

QUOTE_POLARS_SCHEMA: pl.Schema = pl.Schema(
    [
        ("timestamp", pl.Datetime(time_unit="us", time_zone="UTC")),
        ("instrument_symbol", pl.Utf8),
        ("bid", pl.Int64),
        ("ask", pl.Int64),
        ("bid_size", pl.UInt64),
        ("ask_size", pl.UInt64),
    ]
)

BAR_POLARS_SCHEMA: pl.Schema = pl.Schema(
    [
        ("timestamp", pl.Datetime(time_unit="us", time_zone="UTC")),
        ("instrument_symbol", pl.Utf8),
        ("interval_microseconds", pl.UInt64),
        ("open", pl.Int64),
        ("high", pl.Int64),
        ("low", pl.Int64),
        ("close", pl.Int64),
        ("volume", pl.UInt64),
    ]
)
