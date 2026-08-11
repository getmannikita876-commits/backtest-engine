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

A file is untrusted input
-------------------------
Arrow stores a timestamp as a signed 64-bit microsecond count, which spans far
more instants than a Python ``datetime`` can represent, and stores a bar's
interval as an unsigned 64-bit microsecond count, which spans roughly 584 000
years. Neither type constrains its values to anything this module can rebuild.
A file that was not written here — or one that was corrupted after it was — can
therefore carry values that are perfectly legal Parquet and still have no
``datetime`` counterpart.

Two distinct guards exist because there are two distinct kinds of failure:

* A **timestamp** has a fixed representable range, known ahead of time and
  independent of every other column. It is range-checked *before*
  reconstruction, in :func:`_timestamp_from_microseconds`, so the error names
  the column, the row, and the offending value.
* A bar's **interval** has no such fixed bound: whether
  ``availability_time - interval`` is representable depends on both columns
  together, so no per-column check can decide it. That arithmetic is allowed to
  fail and its :class:`OverflowError` is translated at the boundary.

The invariant both uphold, stated once:

    No public function in this module raises :class:`OverflowError`. A stored
    value that cannot be rebuilt is a contract violation and is reported as
    :class:`StorageContractError`.

``OverflowError`` is an :class:`ArithmeticError`, not a :class:`ValueError`, so
it is named explicitly wherever stored values are turned back into ``datetime``
or ``timedelta`` objects. Catching ``ValueError`` and ``TypeError`` alone does
not cover it.
"""

from __future__ import annotations

import os
from collections.abc import Iterable, Mapping, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Final, NamedTuple, final

import pyarrow as pa  # type: ignore[import-untyped]
import pyarrow.parquet as pq  # type: ignore[import-untyped]

from quant_research_terminal.data.conversion import (
    bar_from_storage_row,
    bar_from_storage_row_v3,
    bar_to_storage_row,
    bar_to_storage_row_v3,
    quote_from_storage_row,
    quote_from_storage_row_v3,
    quote_to_storage_row,
    quote_to_storage_row_v3,
    trade_from_storage_row,
    trade_from_storage_row_v3,
    trade_to_storage_row,
    trade_to_storage_row_v3,
    validate_storage_schema,
    validate_storage_schema_v3,
)
from quant_research_terminal.data.schemas import (
    BAR_ARROW_SCHEMA,
    QUOTE_ARROW_SCHEMA,
    TRADE_ARROW_SCHEMA,
    arrow_schema_v3,
)
from quant_research_terminal.domain.dataset_identity import RecordType, require_record_type
from quant_research_terminal.domain.futures_contract import (
    FuturesContractId,
    require_listed_contract,
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

_MICROSECOND: Final[timedelta] = timedelta(microseconds=1)

#: Inclusive bounds, in microseconds since the epoch, of the instants a stored
#: timestamp may denote.
#:
#: Derived from :class:`~datetime.datetime` itself rather than written out, so
#: they cannot drift from the type they describe. They are a property of the
#: Python type, not of the storage schema: no schema version depends on them,
#: and widening them is not possible without a different in-memory
#: representation.
#:
#: The range is far narrower than the ``int64`` Arrow uses to hold a
#: microsecond count — roughly year 1 to year 9999, against a physical type
#: that reaches some 292 000 years either side of the epoch — which is why a
#: syntactically valid file can still hold an unrepresentable instant.
MIN_TIMESTAMP_MICROSECONDS: Final[int] = (datetime.min.replace(tzinfo=UTC) - _EPOCH) // _MICROSECOND
MAX_TIMESTAMP_MICROSECONDS: Final[int] = (datetime.max.replace(tzinfo=UTC) - _EPOCH) // _MICROSECOND


class StorageError(Exception):
    """Base class for storage-layer failures."""


class StorageContractError(StorageError):
    """Raised when data does not satisfy the storage contract.

    On read this covers a missing or unsupported schema version, absent or
    wrong metadata, a missing or mistyped column, a null in a required column,
    reading a file as the wrong record type, and a stored value that no
    ``datetime`` or ``timedelta`` can represent. On write it covers a record
    the storage encoding cannot hold. These are contract violations with a
    clear cause, so they are reported as such rather than surfacing as a raw
    Arrow, Parquet, or arithmetic error.

    Genuine filesystem failures — a missing file, a permission error — are not
    wrapped: they are not contract violations and their own exceptions say more.
    """


def _record_label(schema: pa.Schema) -> str:
    """Return the human-readable record type a schema describes.

    Identity comparison against the version-2 singletons, with an explicit
    fallback for anything else. A version-3 schema is **not** a module
    constant — its metadata carries the dataset's contract, so it is built per
    dataset — which is why every version-3 path passes its
    :class:`RecordType` explicitly to :func:`_record_label_for` instead of
    relying on this function. Before that split existed, every version-3 error
    message would have claimed the file held bars.
    """
    if schema is TRADE_ARROW_SCHEMA:
        return "trade"
    if schema is QUOTE_ARROW_SCHEMA:
        return "quote"
    if schema is BAR_ARROW_SCHEMA:
        return "bar"
    return "unknown-record-type"


def _record_label_for(record_type: RecordType) -> str:
    """Return the human-readable label for an explicitly known record type."""
    return require_record_type(record_type).value


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


def _validate_columns(
    schema: pa.Schema, expected: pa.Schema, path: Path, label: str | None = None
) -> None:
    """Check that the file's columns match the expected record type exactly."""
    actual_names = tuple(schema.names)
    expected_names = tuple(expected.names)

    if actual_names != expected_names:
        raise StorageContractError(
            f"{path} does not hold {label or _record_label(expected)} records: expected "
            f"columns {expected_names}, found {actual_names}"
        )

    for field in expected:
        actual_type = schema.field(field.name).type
        if actual_type != field.type:
            raise StorageContractError(
                f"{path} column {field.name!r} has type {actual_type}, expected {field.type}"
            )


def _timestamp_from_microseconds(value: int, column: str, index: int, path: Path) -> datetime:
    """Rebuild a UTC ``datetime`` from a stored microsecond count.

    The range is checked before the arithmetic rather than after it, so an
    unrepresentable instant is reported with the column, row, and value that
    caused it instead of as a bare "date value out of range" from deep inside
    ``timedelta``.

    Raises:
        StorageContractError: if no ``datetime`` can represent ``value``.
    """
    if not MIN_TIMESTAMP_MICROSECONDS <= value <= MAX_TIMESTAMP_MICROSECONDS:
        raise StorageContractError(
            f"{path} row {index} column {column!r} holds {value} microseconds since the epoch, "
            f"which is outside the representable range "
            f"[{MIN_TIMESTAMP_MICROSECONDS}, {MAX_TIMESTAMP_MICROSECONDS}]"
        )
    return _EPOCH + timedelta(microseconds=value)


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
            _timestamp_from_microseconds(value, field.name, index, path)
            for index, value in enumerate(column.cast(pa.int64()).to_pylist())
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
        except (ValueError, TypeError, OverflowError) as error:
            # OverflowError is an ArithmeticError, so it is named explicitly:
            # the two-name tuple would let it through. It reaches here from
            # arithmetic that no single column check can pre-empt — a bar's
            # interval start is ``availability_time - interval``, and whether
            # that is representable depends on both columns together.
            raise StorageContractError(
                f"{path} row {index} is not a valid {label} record: {error}"
            ) from error
    return records


def _encode_rows(
    records: Sequence[Any],
    encode: Any,
    label: str,
) -> list[Mapping[str, Any]]:
    """Encode domain records as storage rows.

    The mirror of :func:`_reconstruct`, and it exists for the same reason: the
    public API's promise not to raise :class:`OverflowError` has to hold in both
    directions, not only on read.

    No constructible record is currently known to overflow — the domain's own
    validation is narrower than the storage encoding — so this guard is
    defensive. Making the guarantee depend on that coincidence would be the
    weaker choice: it would hold only until the domain widened.
    """
    rows: list[Mapping[str, Any]] = []
    for index, record in enumerate(records):
        try:
            rows.append(encode(record))
        except OverflowError as error:
            raise StorageContractError(
                f"{label} record {index} cannot be encoded for storage: {error}"
            ) from error
    return rows


# --------------------------------------------------------------------------
# Public API
# --------------------------------------------------------------------------


def write_trades(path: Path, trades: Sequence[Trade]) -> None:
    """Write trades to ``path`` as Parquet, atomically.

    Row order is preserved exactly; no sorting is applied. Ordering is a replay
    concern with its own rules, and reordering here would hide whatever order
    the caller established.

    Raises:
        StorageContractError: if a record cannot be encoded for storage.
        OSError: if the file cannot be written. Filesystem failures are not
            wrapped.
    """
    _write_table_atomically(
        _build_table(_encode_rows(trades, trade_to_storage_row, "trade"), TRADE_ARROW_SCHEMA),
        path,
    )


def read_trades(path: Path) -> tuple[Trade, ...]:
    """Read trades from a Parquet file, in file order.

    Raises:
        StorageContractError: if the file is not a valid trade file. This
            includes a stored timestamp no ``datetime`` can represent: an
            unreadable value is a property of the file, not a fault in this
            process, so it is never reported as a raw ``OverflowError``.
        FileNotFoundError: if the path does not exist.
    """
    rows = _read_rows(path, TRADE_ARROW_SCHEMA)
    return tuple(_reconstruct(rows, trade_from_storage_row, path, "trade"))


def write_quotes(path: Path, quotes: Sequence[Quote]) -> None:
    """Write quotes to ``path`` as Parquet, atomically. See :func:`write_trades`."""
    _write_table_atomically(
        _build_table(_encode_rows(quotes, quote_to_storage_row, "quote"), QUOTE_ARROW_SCHEMA),
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
        _build_table(_encode_rows(bars, bar_to_storage_row, "bar"), BAR_ARROW_SCHEMA),
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


# --------------------------------------------------------------------------
# Schema v3: canonical persisted identity
#
# Version 3 answers a question version 2 cannot: *which listed contract is
# this?* The contract is supplied explicitly by the caller and written into
# both the schema metadata and a per-row column. It is never derived from a
# vendor alias — no venue, decade, or delivery year is inferred from a symbol
# spelling anywhere in this module.
# --------------------------------------------------------------------------


@final
class DatasetV3(NamedTuple):
    """What a version-3 artifact declares, and its records in file order.

    ``record_type`` is carried rather than discarded. A caller that reads an
    artifact of unknown type through :func:`read_records_v3` needs to know which
    type it got, and a caller checking an artifact against a claim needs to be
    able to compare. Dropping it here was how a manifest claiming the wrong
    record type could describe an *empty* artifact and be believed: with no rows
    to contradict it, the declared type is the only evidence there is.
    """

    record_type: RecordType
    contract: FuturesContractId
    records: tuple[Any, ...]


_V3_ENCODERS: Final[Mapping[RecordType, Any]] = {
    RecordType.TRADE: trade_to_storage_row_v3,
    RecordType.QUOTE: quote_to_storage_row_v3,
    RecordType.BAR: bar_to_storage_row_v3,
}

_V3_DECODERS: Final[Mapping[RecordType, Any]] = {
    RecordType.TRADE: trade_from_storage_row_v3,
    RecordType.QUOTE: quote_from_storage_row_v3,
    RecordType.BAR: bar_from_storage_row_v3,
}


def _write_v3(
    path: Path,
    records: Sequence[Any],
    *,
    record_type: RecordType,
    contract: FuturesContractId,
) -> None:
    require_record_type(record_type)
    require_listed_contract(contract)
    label = _record_label_for(record_type)
    encode = _V3_ENCODERS[record_type]
    rows: list[Mapping[str, Any]] = []
    for index, record in enumerate(records):
        try:
            rows.append(encode(record, contract))
        except (ValueError, TypeError) as error:
            raise StorageContractError(
                f"{label} record {index} is not storable: {error}"
            ) from error
        except OverflowError as error:
            raise StorageContractError(
                f"{label} record {index} cannot be encoded for storage: {error}"
            ) from error
    schema = arrow_schema_v3(record_type, contract)
    _write_table_atomically(_build_table(rows, schema), path)


def _read_v3(
    path: Path, *, record_type: RecordType, contract: FuturesContractId | None
) -> DatasetV3:
    """Read a version-3 artifact, validating identity before touching a row.

    The order of checks matters and is deliberate: everything that can be
    decided from metadata is decided first, so a file naming the wrong contract
    costs one footer read rather than a full scan.
    """
    require_record_type(record_type)
    if contract is not None:
        require_listed_contract(contract)
    label = _record_label_for(record_type)

    if not path.exists():
        raise FileNotFoundError(f"no such storage file: {path}")
    try:
        table = pq.read_table(path)
    except pa.ArrowInvalid as error:
        raise StorageContractError(f"{path} is not a readable Parquet file: {error}") from error

    try:
        declared_type, declared_contract = validate_storage_schema_v3(table.schema)
    except ValueError as error:
        raise StorageContractError(f"{path} has incompatible schema metadata: {error}") from error

    expected_schema = arrow_schema_v3(declared_type, declared_contract)
    _validate_columns(table.schema, expected_schema, path, label=_record_label_for(declared_type))

    if declared_type is not record_type:
        raise StorageContractError(
            f"{path} holds {_record_label_for(declared_type)} records, not {label}"
        )
    if contract is not None and declared_contract != contract:
        raise StorageContractError(
            f"{path} holds {declared_contract.canonical()}, not the expected {contract.canonical()}"
        )

    rows = _rows_from_table(table, expected_schema, path)

    # Every row, not the first: a single divergent row is exactly the corruption
    # this column exists to expose, and an empty artifact makes the check
    # vacuous, which is why the metadata carries the identity too.
    expected_identity = declared_contract.canonical()
    offenders = [
        index for index, row in enumerate(rows) if row["canonical_identity"] != expected_identity
    ]
    if offenders:
        raise StorageContractError(
            f"{path} claims to hold {expected_identity} but {len(offenders)} row(s) disagree; "
            f"first at index {offenders[0]} with {rows[offenders[0]]['canonical_identity']!r}"
        )

    decode = _V3_DECODERS[declared_type]
    return DatasetV3(
        record_type=declared_type,
        contract=declared_contract,
        records=tuple(_reconstruct(rows, decode, path, label)),
    )


def _rows_from_table(table: pa.Table, expected: pa.Schema, path: Path) -> list[dict[str, Any]]:
    """Materialize storage rows from a table that has already been read.

    Takes the table rather than the path, and that is the point. This previously
    re-opened the file, which meant a version-3 read touched the same path twice:
    once to validate the schema and identity, and again to fetch the rows. Two
    consequences, one wasteful and one a genuine contract hole:

    * the second read is redundant — the caller already holds the table;
    * ``pq.read_table`` was called here **unwrapped**, so if the file changed
      between the two reads a raw ``pyarrow.lib.ArrowInvalid`` escaped, despite
      this module's stated contract that it never leaks raw Arrow or Parquet
      errors. Reproduced before the change: a replaced file produced
      ``pyarrow.lib.ArrowInvalid: Parquet magic bytes not found in footer``
      rather than a :class:`StorageContractError`.

    Reading once removes both. The rows are now provably the rows whose schema
    and declared identity were validated, rather than the rows of whatever was at
    that path a moment later.
    """
    columns = {field.name: _column_values(table, field, path) for field in expected}
    return [
        {name: values[index] for name, values in columns.items()} for index in range(table.num_rows)
    ]


def write_trades_v3(path: Path, trades: Sequence[Trade], *, contract: FuturesContractId) -> None:
    """Write trades as a version-3 artifact naming ``contract``.

    Row order is preserved exactly; no sorting is applied.
    """
    _write_v3(path, trades, record_type=RecordType.TRADE, contract=contract)


def read_trades_v3(path: Path, *, contract: FuturesContractId | None = None) -> DatasetV3:
    """Read a version-3 trade artifact and the contract it names."""
    return _read_v3(path, record_type=RecordType.TRADE, contract=contract)


def write_quotes_v3(path: Path, quotes: Sequence[Quote], *, contract: FuturesContractId) -> None:
    """Write quotes as a version-3 artifact naming ``contract``."""
    _write_v3(path, quotes, record_type=RecordType.QUOTE, contract=contract)


def read_quotes_v3(path: Path, *, contract: FuturesContractId | None = None) -> DatasetV3:
    """Read a version-3 quote artifact and the contract it names."""
    return _read_v3(path, record_type=RecordType.QUOTE, contract=contract)


def write_bars_v3(path: Path, bars: Sequence[Bar], *, contract: FuturesContractId) -> None:
    """Write bars as a version-3 artifact naming ``contract``."""
    _write_v3(path, bars, record_type=RecordType.BAR, contract=contract)


def read_bars_v3(path: Path, *, contract: FuturesContractId | None = None) -> DatasetV3:
    """Read a version-3 bar artifact and the contract it names."""
    return _read_v3(path, record_type=RecordType.BAR, contract=contract)


def read_dataset_descriptor(path: Path) -> tuple[RecordType, FuturesContractId]:
    """Return what a version-3 artifact says it holds, without reading rows."""
    if not path.exists():
        raise FileNotFoundError(f"no such storage file: {path}")
    try:
        schema = pq.read_schema(path)
    except pa.ArrowInvalid as error:
        raise StorageContractError(f"{path} is not a readable Parquet file: {error}") from error
    try:
        return validate_storage_schema_v3(schema)
    except ValueError as error:
        raise StorageContractError(f"{path} has incompatible schema metadata: {error}") from error


def read_records_v3(path: Path) -> DatasetV3:
    """Read a version-3 artifact of whichever record type it declares."""
    record_type, _ = read_dataset_descriptor(path)
    return _read_v3(path, record_type=record_type, contract=None)
