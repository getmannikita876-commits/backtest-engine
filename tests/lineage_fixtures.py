"""Shared builders for the Phase 2.4 lineage and comparison tests.

Registering a dataset takes an artifact, a manifest, and two roots, so a test
that wants "two datasets of one contract that differ" would otherwise spend ten
lines saying so before it could say anything interesting.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from dataset_fixtures import ES_M2026, manifest_for, three_trades

from quant_research_terminal.catalog import (
    DatasetManifest,
    register_dataset,
)
from quant_research_terminal.data.parquet_store import (
    write_bars_v3,
    write_quotes_v3,
    write_trades_v3,
)
from quant_research_terminal.domain.dataset_identity import RecordType
from quant_research_terminal.domain.futures_contract import FuturesContractId

_WRITERS: dict[RecordType, Any] = {
    RecordType.TRADE: write_trades_v3,
    RecordType.QUOTE: write_quotes_v3,
    RecordType.BAR: write_bars_v3,
}


def register(
    catalog: Path,
    storage: Path,
    name: str,
    records: list[Any] | None = None,
    *,
    record_type: RecordType = RecordType.TRADE,
    contract: FuturesContractId = ES_M2026,
    provider_token: str | None = "DATABENTO",
) -> DatasetManifest:
    """Write a version-3 artifact, register it, and return its manifest."""
    rows = three_trades() if records is None else records
    path = storage / name
    path.parent.mkdir(parents=True, exist_ok=True)
    _WRITERS[record_type](path, rows, contract=contract)
    manifest = manifest_for(
        path,
        rows,
        record_type=record_type,
        contract=contract,
        provider_token=provider_token,
    )
    register_dataset(
        catalog_root=catalog, storage_root=storage, artifact_path=path, manifest=manifest
    )
    return manifest
