from __future__ import annotations

import polars as pl
import pyarrow as pa  # type: ignore[import-untyped]

from quant_research_terminal.data.contracts import (
    PRICE_ENCODING,
    PRICE_PRECISION,
    PRICE_SCALE,
    SCHEMA_NAME,
    SCHEMA_VERSION,
    TIMESTAMP_TIMEZONE,
)


def _arrow_schema_metadata() -> dict[str, bytes]:
    return {
        "schema_name": SCHEMA_NAME.encode("utf-8"),
        "schema_version": str(SCHEMA_VERSION).encode("utf-8"),
        "timestamp_timezone": TIMESTAMP_TIMEZONE.encode("utf-8"),
        "price_encoding": PRICE_ENCODING.encode("utf-8"),
        "price_precision": str(PRICE_PRECISION).encode("utf-8"),
        "price_scale": str(PRICE_SCALE).encode("utf-8"),
    }


TRADE_ARROW_SCHEMA: pa.Schema = pa.schema(
    [
        pa.field("timestamp", pa.timestamp("us", tz="UTC")),
        pa.field("instrument_symbol", pa.utf8()),
        pa.field("price", pa.int64()),
        pa.field("size", pa.uint64()),
        pa.field("side", pa.utf8()),
    ],
    metadata=_arrow_schema_metadata(),
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
    metadata=_arrow_schema_metadata(),
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
    metadata=_arrow_schema_metadata(),
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
