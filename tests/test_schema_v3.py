"""Schema v3: canonical persisted identity, and v2 left exactly as it was.

The two properties this file exists to hold apart:

* a version-3 artifact names the listed contract it holds, unambiguously and
  without inference;
* a version-2 artifact still means precisely what it always meant.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pyarrow as pa  # type: ignore[import-untyped]
import pyarrow.parquet as pq  # type: ignore[import-untyped]
import pytest
from dataset_fixtures import ES_M2026, ES_U2026, bar, quote, three_trades, trade

from quant_research_terminal.data.contracts import (
    LEGACY_SCHEMA_VERSION,
    SCHEMA_VERSION,
    SUPPORTED_SCHEMA_VERSIONS,
)
from quant_research_terminal.data.conversion import (
    validate_storage_schema,
    validate_storage_schema_v3,
)
from quant_research_terminal.data.parquet_store import (
    StorageContractError,
    read_bars_v3,
    read_dataset_descriptor,
    read_quotes_v3,
    read_records_v3,
    read_schema_metadata,
    read_trades,
    read_trades_v3,
    write_bars_v3,
    write_quotes_v3,
    write_trades,
    write_trades_v3,
)
from quant_research_terminal.data.schemas import (
    BAR_ARROW_SCHEMA,
    QUOTE_ARROW_SCHEMA,
    TRADE_ARROW_SCHEMA,
    arrow_schema_v3,
    v3_column_names,
)
from quant_research_terminal.domain.dataset_identity import RecordType

# --------------------------------------------------------------------------
# Version constants and version-2 stability
# --------------------------------------------------------------------------


def test_the_current_version_is_three_and_legacy_is_two() -> None:
    assert SCHEMA_VERSION == 3
    assert LEGACY_SCHEMA_VERSION == 2
    assert SUPPORTED_SCHEMA_VERSIONS == frozenset({2, 3})


@pytest.mark.parametrize(
    "schema",
    [TRADE_ARROW_SCHEMA, QUOTE_ARROW_SCHEMA, BAR_ARROW_SCHEMA],
    ids=["trade", "quote", "bar"],
)
def test_the_v2_schemas_still_declare_version_two(schema: pa.Schema) -> None:
    """All three, because all three share one metadata factory.

    Literals, deliberately: an assertion written against the constant would have
    followed the bump and silently ratified v2 writers emitting version 3.
    """
    assert schema.metadata[b"schema_version"] == b"2"


def test_a_v2_artifact_still_round_trips(tmp_path: Path) -> None:
    path = tmp_path / "v2.parquet"
    rows = three_trades()
    write_trades(path, rows)
    assert read_trades(path) == tuple(rows)
    assert read_schema_metadata(path)["schema_version"] == "2"


def test_validate_storage_schema_defaults_to_legacy() -> None:
    """Defaulting to the *current* version is how a v2 file gets read as v3."""
    validate_storage_schema(TRADE_ARROW_SCHEMA)
    with pytest.raises(ValueError, match="schema version"):
        validate_storage_schema(TRADE_ARROW_SCHEMA, expected_version=SCHEMA_VERSION)


def test_a_v2_artifact_cannot_be_read_as_v3(tmp_path: Path) -> None:
    path = tmp_path / "v2.parquet"
    write_trades(path, three_trades())
    with pytest.raises(StorageContractError):
        read_trades_v3(path)


def test_a_v3_artifact_cannot_be_read_as_v2(tmp_path: Path) -> None:
    path = tmp_path / "v3.parquet"
    write_trades_v3(path, three_trades(), contract=ES_M2026)
    with pytest.raises(StorageContractError):
        read_trades(path)


# --------------------------------------------------------------------------
# Version-3 shape
# --------------------------------------------------------------------------


def test_v3_columns_replace_the_alias_with_two_identity_columns() -> None:
    assert v3_column_names(RecordType.TRADE) == (
        "timestamp",
        "canonical_identity",
        "vendor_symbol",
        "price",
        "size",
        "side",
    )
    schema = arrow_schema_v3(RecordType.TRADE, ES_M2026)
    assert schema.field("canonical_identity").type == pa.utf8()
    assert schema.field("vendor_symbol").type == pa.utf8()
    assert "instrument_symbol" not in schema.names


def test_v3_metadata_carries_the_record_type_and_contract() -> None:
    schema = arrow_schema_v3(RecordType.QUOTE, ES_M2026)
    assert schema.metadata[b"schema_version"] == b"3"
    assert schema.metadata[b"record_type"] == b"quote"
    assert schema.metadata[b"canonical_identity"] == b"CME:ES:M2026"


@pytest.mark.parametrize(
    ("writer", "reader", "records", "record_type"),
    [
        (write_trades_v3, read_trades_v3, three_trades(), RecordType.TRADE),
        (write_quotes_v3, read_quotes_v3, [quote(offset=i) for i in range(3)], RecordType.QUOTE),
        (write_bars_v3, read_bars_v3, [bar(offset=i) for i in range(3)], RecordType.BAR),
    ],
)
def test_every_record_type_round_trips_with_its_contract(
    tmp_path: Path,
    writer: Any,
    reader: Any,
    records: Any,
    record_type: RecordType,
) -> None:
    path = tmp_path / "v3.parquet"
    writer(path, records, contract=ES_M2026)
    dataset = reader(path)
    assert dataset.contract == ES_M2026
    assert dataset.records == tuple(records)
    assert read_dataset_descriptor(path) == (record_type, ES_M2026)


def test_a_read_reports_the_record_type_it_found(tmp_path: Path) -> None:
    """Discarding it made a false record-type claim about an empty artifact
    unfalsifiable: with no rows, the declared type is the only evidence."""
    path = tmp_path / "v3.parquet"
    write_quotes_v3(path, [], contract=ES_M2026)
    dataset = read_records_v3(path)
    assert dataset.record_type is RecordType.QUOTE
    assert dataset.contract == ES_M2026
    assert dataset.records == ()


def test_vendor_aliases_are_preserved_per_row(tmp_path: Path) -> None:
    """Several aliases may legitimately map to one contract."""
    path = tmp_path / "v3.parquet"
    rows = [trade(offset=0, symbol="ESM6"), trade(offset=1, symbol="ES M6")]
    write_trades_v3(path, rows, contract=ES_M2026)
    dataset = read_trades_v3(path)
    assert [record.instrument_symbol for record in dataset.records] == ["ESM6", "ES M6"]
    assert dataset.contract == ES_M2026


def test_an_empty_v3_artifact_still_names_its_contract(tmp_path: Path) -> None:
    """With no rows, metadata is the only place identity can live."""
    path = tmp_path / "empty.parquet"
    write_trades_v3(path, [], contract=ES_M2026)
    dataset = read_trades_v3(path)
    assert dataset.records == ()
    assert dataset.contract == ES_M2026


def test_row_order_is_preserved(tmp_path: Path) -> None:
    path = tmp_path / "v3.parquet"
    rows = [trade(offset=5), trade(offset=1), trade(offset=9)]
    write_trades_v3(path, rows, contract=ES_M2026)
    assert [r.timestamp for r in read_trades_v3(path).records] == [r.timestamp for r in rows]


def test_duplicate_rows_are_preserved(tmp_path: Path) -> None:
    path = tmp_path / "v3.parquet"
    write_trades_v3(path, [trade(), trade(), trade()], contract=ES_M2026)
    assert len(read_trades_v3(path).records) == 3


# --------------------------------------------------------------------------
# Identity validation on read
# --------------------------------------------------------------------------


def test_reading_with_the_wrong_expected_contract_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "v3.parquet"
    write_trades_v3(path, three_trades(), contract=ES_M2026)
    with pytest.raises(StorageContractError, match="CME:ES:M2026"):
        read_trades_v3(path, contract=ES_U2026)


def test_reading_the_wrong_record_type_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "v3.parquet"
    write_trades_v3(path, three_trades(), contract=ES_M2026)
    with pytest.raises(StorageContractError, match="trade records, not quote"):
        read_quotes_v3(path)


def _rewrite_with_metadata(path: Path, target: Path, metadata: dict[bytes, bytes]) -> None:
    table = pq.read_table(path)
    pq.write_table(table.replace_schema_metadata(metadata), target)


def test_a_non_canonical_identity_in_metadata_is_refused(tmp_path: Path) -> None:
    """Self-consistent but unparseable: every row could match and still be wrong."""
    source = tmp_path / "v3.parquet"
    write_trades_v3(source, three_trades(), contract=ES_M2026)
    metadata = dict(pq.read_schema(source).metadata)
    metadata[b"canonical_identity"] = b"cme:es:m2026"
    target = tmp_path / "bad.parquet"
    _rewrite_with_metadata(source, target, metadata)
    with pytest.raises(StorageContractError):
        read_trades_v3(target)


def test_a_record_type_contradicting_the_columns_is_refused(tmp_path: Path) -> None:
    source = tmp_path / "v3.parquet"
    write_trades_v3(source, three_trades(), contract=ES_M2026)
    metadata = dict(pq.read_schema(source).metadata)
    metadata[b"record_type"] = b"quote"
    target = tmp_path / "bad.parquet"
    _rewrite_with_metadata(source, target, metadata)
    with pytest.raises(StorageContractError):
        read_records_v3(target)


def test_an_unknown_extra_v3_metadata_key_is_refused(tmp_path: Path) -> None:
    source = tmp_path / "v3.parquet"
    write_trades_v3(source, three_trades(), contract=ES_M2026)
    metadata = dict(pq.read_schema(source).metadata)
    metadata[b"surprise"] = b"1"
    target = tmp_path / "bad.parquet"
    _rewrite_with_metadata(source, target, metadata)
    with pytest.raises(StorageContractError, match="documented metadata keys"):
        read_trades_v3(target)


def test_an_unknown_extra_v2_metadata_key_is_still_tolerated(tmp_path: Path) -> None:
    """Version 2's leniency is grandfathered; files already exist under it."""
    source = tmp_path / "v2.parquet"
    write_trades(source, three_trades())
    metadata = dict(pq.read_schema(source).metadata)
    metadata[b"surprise"] = b"1"
    target = tmp_path / "extra.parquet"
    _rewrite_with_metadata(source, target, metadata)
    assert read_trades(target) == tuple(three_trades())


def test_a_row_disagreeing_with_the_metadata_identity_is_refused(tmp_path: Path) -> None:
    """All rows are checked, not the first: one bad row is the corruption case."""
    source = tmp_path / "v3.parquet"
    rows = [trade(offset=index) for index in range(10)]
    write_trades_v3(source, rows, contract=ES_M2026)

    table = pq.read_table(source)
    identities = table.column("canonical_identity").to_pylist()
    identities[7] = ES_U2026.canonical()
    patched = table.set_column(
        table.schema.get_field_index("canonical_identity"),
        table.schema.field("canonical_identity"),
        pa.array(identities, type=pa.utf8()),
    )
    target = tmp_path / "bad.parquet"
    pq.write_table(patched.replace_schema_metadata(dict(table.schema.metadata)), target)

    with pytest.raises(StorageContractError, match="index 7"):
        read_trades_v3(target)


def test_validate_storage_schema_v3_returns_what_it_verified() -> None:
    schema = arrow_schema_v3(RecordType.BAR, ES_U2026)
    assert validate_storage_schema_v3(schema) == (RecordType.BAR, ES_U2026)


def test_a_continuous_identity_cannot_be_a_v3_contract() -> None:
    """The v3 identity is a *listed* contract; a synthetic series is not one."""
    from quant_research_terminal.domain.continuous_series import ContinuousSeriesId
    from quant_research_terminal.domain.futures_contract import FuturesProduct, Venue

    series = ContinuousSeriesId(
        product=FuturesProduct(venue=Venue(code="CME"), root="ES"), token="ACTIVE"
    )
    with pytest.raises(TypeError):
        arrow_schema_v3(RecordType.TRADE, series)  # type: ignore[arg-type]


def test_the_import_pipeline_still_accepts_a_version_two_batch() -> None:
    """The version bump must not change import acceptance semantics."""
    from quant_research_terminal.data_import.contracts import ImportBatch, ImportRecordType
    from quant_research_terminal.data_import.pipeline import validate_import_batch

    batch = ImportBatch(record_type=ImportRecordType.TRADE, rows=(), schema_version=2)
    _, report = validate_import_batch(batch)
    assert not any(issue.code.value == "unsupported_schema_version" for issue in report.issues)

    rejected = ImportBatch(record_type=ImportRecordType.TRADE, rows=(), schema_version=999)
    _, bad_report = validate_import_batch(rejected)
    assert any(issue.code.value == "unsupported_schema_version" for issue in bad_report.issues)


# --------------------------------------------------------------------------
# A version-3 read opens the file exactly once
#
# Found while building Phase 3's replay preflight, which resolves a manifest
# through *any* byte-identical copy the catalog knows of, and therefore needs
# every per-copy failure to be a typed storage error it can attribute to that
# copy and move on from.
#
# ``_read_v3`` validated the schema and declared identity from one
# ``read_table`` and then re-opened the file to fetch the rows — and that second
# call was the one ``pq.read_table`` in the module with no wrapping. Two
# consequences: the rows were not provably the rows whose identity had just been
# validated, and a file replaced between the two reads produced a raw
# ``pyarrow.lib.ArrowInvalid: Parquet magic bytes not found in footer`` instead
# of a ``StorageContractError``, contradicting the module's stated contract that
# it never leaks raw Arrow or Parquet errors.
# --------------------------------------------------------------------------


def test_a_version_three_read_opens_the_file_exactly_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Structural regression: one read means there is no window to race.

    Counts reads rather than asserting on an error message, because the property
    that closes the hole is that the second read no longer exists.
    """
    path = tmp_path / "once.parquet"
    write_trades_v3(path, three_trades(), contract=ES_M2026)

    # ``parquet_store`` does ``import pyarrow.parquet as pq``, so this module's
    # own ``pq`` is the same module object: patching it here patches it there.
    reads: list[Path] = []
    real_read_table = pq.read_table

    def counting_read_table(source: Path, *args: Any, **kwargs: Any) -> pa.Table:
        reads.append(source)
        return real_read_table(source, *args, **kwargs)

    monkeypatch.setattr(pq, "read_table", counting_read_table)
    dataset = read_records_v3(path)

    assert len(dataset.records) == 3
    assert reads == [path]


def test_no_raw_arrow_error_escapes_a_version_three_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Whatever the file does, a caller sees ``StorageContractError``.

    The failure is injected at ``read_table`` rather than by corrupting bytes,
    because the case being pinned is the *second* read failing after the first
    succeeded. That is unreachable now, which is precisely what the first
    assertion states.
    """
    path = tmp_path / "flaky.parquet"
    write_trades_v3(path, three_trades(), contract=ES_M2026)

    calls = {"n": 0}
    real_read_table = pq.read_table

    def flaky_read_table(source: Path, *args: Any, **kwargs: Any) -> pa.Table:
        calls["n"] += 1
        if calls["n"] >= 2:
            raise pa.ArrowInvalid("Parquet magic bytes not found in footer")
        return real_read_table(source, *args, **kwargs)

    monkeypatch.setattr(pq, "read_table", flaky_read_table)

    assert len(read_records_v3(path).records) == 3

    calls["n"] = 1
    with pytest.raises(StorageContractError):
        read_records_v3(path)
