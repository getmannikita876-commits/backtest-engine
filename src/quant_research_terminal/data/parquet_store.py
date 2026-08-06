"""Deterministic Parquet persistence for domain records.

This is the first module in the project that touches a real file. Everything
below it — the schemas, the fixed-point encoding, the metadata contract — was
previously exercised only in memory, so its guarantees were theoretical. This
module makes them observable:

    domain records -> Arrow table -> Parquet file -> Arrow table -> domain records

Scope
-----
One record type per file. A Parquet file written here holds trades, or quotes,
or bars, never a mixture. The three Arrow schemas have distinct column sets and
carry no discriminator field, so a mixed file could not be read back without
inventing one — and inventing a schema-level discriminator is a contract change
this phase does not make. The record type is therefore supplied by the caller
on read and checked against the file's actual schema.

There is no dataset catalogue, no partitioning scheme, no DuckDB, and no
caching. A path in, a path out.

Determinism
-----------
This module guarantees **semantic** determinism: the same records written with
the same configuration read back as equal records, in the same order. Byte-level
identity of the Parquet file is a separate and weaker claim — the footer records
the writing library's version string — and is asserted only where the tests
verify it directly.

Nothing here reads the wall clock, generates a random identifier, or depends on
dictionary iteration order or the Python hash seed.

Timestamps without a zone database
----------------------------------
Arrow stores timestamps as an integer count of microseconds plus a zone name.
Converting one back to a Python ``datetime`` through PyArrow's ``as_py()``
resolves that zone name through :mod:`zoneinfo`, which needs the ``tzdata``
package — absent on a stock Windows install, where it raises
``ZoneInfoNotFoundError`` for the name ``UTC`` itself.

Rather than take a dependency to convert a value we already know is UTC, this
module reads the underlying microsecond count and rebuilds the ``datetime``
against :data:`datetime.UTC` directly. That is exact, needs no zone database,
and cannot silently pick up a different zone. The ``timestamp_timezone``
metadata is still validated on read, so the file's own claim is checked.
"""

from __future__ import annotations

import os
from collections.abc import Iterable, Mapping, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Final

import pyarrow as pa  # type: ignore[import-untyped]
import pyarrow.parquet as pq  # type: ignore[import-untyped]

from quant_research_terminal.data.conversion import (
    bar_from_storage_row,
    bar_to_storage_row,
    quote_from_storage_row,
    quote_to_storage_row,
    trade_from_storage_row,
    trade_to_storage_row,
    validate_storage_schema,
)
from quant_research_terminal.data.schemas import (
    BAR_ARROW_SCHEMA,
    QUOTE_ARROW_SCHEMA,
    TRADE_ARROW_SCHEMA,
)
from quant_research_terminal.domain.models import Bar, Quote, Trade

# --------------------------------------------------------------------------
# Parquet configuration
#
# Chosen for stability and predictability, not for speed. Performance tuning is
# out of scope; these are the settings a reader needs to know to reproduce a
# file, stated explicitly rather than inherited from library defaults that may
# change between releases.
# --------------------------------------------------------------------------

#: Compression codec. Snappy is the most widely supported Parquet codec and is
#: lossless, so it cannot affect a value. No compression *level* applies to it.
COMPRESSION: Final[str] = "snappy"

#: Parquet format version. Pinned so a file's layout does not shift when the
#: writing library changes its default.
PARQUET_VERSION: Final[str] = "2.6"

#: Dictionary encoding is disabled to keep every column's encoding uniform and
#: the file trivially predictable. This is a reproducibility choice, not a
#: space or speed one.
USE_DICTIONARY: Final[bool] = False

#: Rows per row group. Fixed so grouping does not vary with input size.
ROW_GROUP_SIZE: Final[int] = 65_536

#: Suffix for the temporary file used during an atomic write. Derived from the
#: target name rather than randomised, so a failed write leaves a predictable
#: artefact and never a uniquely-named orphan.
PARTIAL_SUFFIX: Final[str] = ".partial"

_EPOCH: Final[datetime] = datetime(1970, 1, 1, tzinfo=UTC)


class StorageError(Exception):
    """Base class for storage-layer failures."""


class StorageContractError(StorageError):
    """Raised when a file does not satisfy the storage contract.

    Covers a missing or unsupported schema version, absent or wrong metadata, a
    missing or mistyped column, a null in a required column, and reading a file
    as the wrong record type. These are contract violations with a clear cause,
    so they are reported as such rather than surfacing as a raw Arrow or
    Parquet error.

    Genuine filesystem failures — a missing file, a permission error — are not
    wrapped: they are not contract violations and their own exceptions say more.
    """


def _record_label(schema: pa.Schema) -> str:
    """Return the human-readable record type a schema describes."""
    if schema is TRADE_ARROW_SCHEMA:
        return "trade"
    if schema is QUOTE_ARROW_SCHEMA:
        return "quote"
    return "bar"


def _build_table(rows: Sequence[Mapping[str, Any]], schema: pa.Schema) -> pa.Table:
    """Assemble an Arrow table from storage rows in the schema's column order.

    Columns are built by iterating the schema's fields, so the table's layout
    depends on the authoritative schema rather than on the insertion order of
    any dictionary.
    """
    columns = [[row[field.name] for row in rows] for field in schema]
    return pa.table(columns, schema=schema)


def _write_table_atomically(table: pa.Table, path: Path) -> None:
    """Write ``table`` to ``path``, replacing it only once fully written.

    A direct write would leave a truncated file at the target path if the
    process died mid-write, and a truncated Parquet file is not distinguishable
    from a valid one until it is read. Writing beside the target and then
    replacing means the target is either the previous file or the new one, never
    a partial one.

    ``os.replace`` is atomic on POSIX and on Windows for same-volume moves. The
    temporary file is created in the target's own directory so the replace never
    crosses a filesystem boundary.
    """
    partial = path.with_name(path.name + PARTIAL_SUFFIX)
    try:
        with pq.ParquetWriter(
            partial,
            table.schema,
            compression=COMPRESSION,
            version=PARQUET_VERSION,
            use_dictionary=USE_DICTIONARY,
        ) as writer:
            writer.write_table(table, row_group_size=ROW_GROUP_SIZE)
        os.replace(partial, path)
    except BaseException:
        # Any failure — including one raised while closing the writer — must
        # not leave the temporary file behind.
        partial.unlink(missing_ok=True)
        raise


def _read_table(path: Path, expected: pa.Schema) -> pa.Table:
    """Read a Parquet file and check it against the expected schema.

    Raises:
        StorageContractError: if the file's schema or metadata violates the
            contract, or if the file is unreadable as Parquet.
        OSError: if the file cannot be opened. Filesystem failures are not
            wrapped.
    """
    if not path.exists():
        raise FileNotFoundError(f"no such storage file: {path}")

    try:
        table = pq.read_table(path)
    except pa.ArrowInvalid as error:
        raise StorageContractError(f"{path} is not a readable Parquet file: {error}") from error

    _validate_metadata(table.schema, path)
    _validate_columns(table.schema, expected, path)
    return table


def _validate_metadata(schema: pa.Schema, path: Path) -> None:
    """Check the schema-level metadata contract."""
    if schema.metadata is None:
        raise StorageContractError(f"{path} carries no schema metadata")
    try:
        validate_storage_schema(schema)
    except ValueError as error:
        raise StorageContractError(f"{path} has incompatible schema metadata: {error}") from error


def _validate_columns(schema: pa.Schema, expected: pa.Schema, path: Path) -> None:
    """Check that the file's columns match the expected record type exactly."""
    actual_names = tuple(schema.names)
    expected_names = tuple(expected.names)

    if actual_names != expected_names:
        raise StorageContractError(
            f"{path} does not hold {_record_label(expected)} records: expected columns "
            f"{expected_names}, found {actual_names}"
        )

    for field in expected:
        actual_type = schema.field(field.name).type
        if actual_type != field.type:
            raise StorageContractError(
                f"{path} column {field.name!r} has type {actual_type}, expected {field.type}"
            )


def _column_values(table: pa.Table, field: pa.Field, path: Path) -> list[Any]:
    """Return one column as Python values, rejecting nulls.

    Timestamp columns bypass ``as_py()`` and are rebuilt from their microsecond
    count; see the module docstring.
    """
    column = table.column(field.name)
    if column.null_count:
        raise StorageContractError(
            f"{path} column {field.name!r} contains {column.null_count} null value(s); "
            f"every column in this contract is required"
        )

    if pa.types.is_timestamp(field.type):
        return [
            _EPOCH + timedelta(microseconds=value) for value in column.cast(pa.int64()).to_pylist()
        ]
    return list(column.to_pylist())


def _read_rows(path: Path, expected: pa.Schema) -> list[dict[str, Any]]:
    """Read a Parquet file into storage rows, preserving file order."""
    table = _read_table(path, expected)
    columns = {field.name: _column_values(table, field, path) for field in expected}
    return [
        {name: values[index] for name, values in columns.items()} for index in range(table.num_rows)
    ]


def _reconstruct(
    rows: Iterable[Mapping[str, Any]],
    convert: Any,
    path: Path,
    label: str,
) -> list[Any]:
    """Rebuild domain records from storage rows.

    Reconstruction goes through the ordinary domain constructors, so every
    stored value is revalidated. Nothing here uses ``model_construct`` or any
    other route that would bypass validation: a file is untrusted input.
    """
    records: list[Any] = []
    for index, row in enumerate(rows):
        try:
            records.append(convert(row))
        except (ValueError, TypeError) as error:
            raise StorageContractError(
                f"{path} row {index} is not a valid {label} record: {error}"
            ) from error
    return records


# --------------------------------------------------------------------------
# Public API
# --------------------------------------------------------------------------


def write_trades(path: Path, trades: Sequence[Trade]) -> None:
    """Write trades to ``path`` as Parquet, atomically.

    Row order is preserved exactly; no sorting is applied. Ordering is a replay
    concern with its own rules, and reordering here would hide whatever order
    the caller established.
    """
    _write_table_atomically(
        _build_table([trade_to_storage_row(trade) for trade in trades], TRADE_ARROW_SCHEMA),
        path,
    )


def read_trades(path: Path) -> tuple[Trade, ...]:
    """Read trades from a Parquet file, in file order.

    Raises:
        StorageContractError: if the file is not a valid trade file.
        FileNotFoundError: if the path does not exist.
    """
    rows = _read_rows(path, TRADE_ARROW_SCHEMA)
    return tuple(_reconstruct(rows, trade_from_storage_row, path, "trade"))


def write_quotes(path: Path, quotes: Sequence[Quote]) -> None:
    """Write quotes to ``path`` as Parquet, atomically. See :func:`write_trades`."""
    _write_table_atomically(
        _build_table([quote_to_storage_row(quote) for quote in quotes], QUOTE_ARROW_SCHEMA),
        path,
    )


def read_quotes(path: Path) -> tuple[Quote, ...]:
    """Read quotes from a Parquet file, in file order. See :func:`read_trades`."""
    rows = _read_rows(path, QUOTE_ARROW_SCHEMA)
    return tuple(_reconstruct(rows, quote_from_storage_row, path, "quote"))


def write_bars(path: Path, bars: Sequence[Bar]) -> None:
    """Write bars to ``path`` as Parquet, atomically. See :func:`write_trades`.

    A bar persists its availability time and its interval; interval start is
    recovered on read as ``timestamp - interval``. See ADR-002.
    """
    _write_table_atomically(
        _build_table([bar_to_storage_row(bar) for bar in bars], BAR_ARROW_SCHEMA),
        path,
    )


def read_bars(path: Path) -> tuple[Bar, ...]:
    """Read bars from a Parquet file, in file order. See :func:`read_trades`."""
    rows = _read_rows(path, BAR_ARROW_SCHEMA)
    return tuple(_reconstruct(rows, bar_from_storage_row, path, "bar"))


def read_schema_metadata(path: Path) -> dict[str, str]:
    """Return a Parquet file's schema metadata as decoded strings.

    Useful for inspecting a file's contract without reconstructing records.
    """
    if not path.exists():
        raise FileNotFoundError(f"no such storage file: {path}")
    schema = pq.read_schema(path)
    if schema.metadata is None:
        return {}
    return {key.decode("utf-8"): value.decode("utf-8") for key, value in schema.metadata.items()}
