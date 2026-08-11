"""Shared builders for the Phase 2.3 dataset-identity tests.

One place to construct contracts, records, and manifests, so the same eleven
consistent manifest fields are not hand-maintained in four test modules.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from quant_research_terminal.catalog.manifest import DatasetManifest, build_manifest
from quant_research_terminal.catalog.provenance import (
    DatasetOrigin,
    SourceProvenance,
    TransformationKind,
    TransformationProvenance,
)
from quant_research_terminal.data.artifact_hash import physical_artifact_hash
from quant_research_terminal.data.contracts import SCHEMA_VERSION
from quant_research_terminal.domain.contract_month import ContractMonth
from quant_research_terminal.domain.dataset_identity import (
    RecordType,
    dataset_time_bounds,
    semantic_dataset_hash,
)
from quant_research_terminal.domain.futures_contract import (
    FuturesContractId,
    FuturesProduct,
    Venue,
)
from quant_research_terminal.domain.models import Bar, Quote, Trade
from quant_research_terminal.domain.trade_side import TradeSide

ES = FuturesProduct(venue=Venue(code="CME"), root="ES")
NQ = FuturesProduct(venue=Venue(code="CME"), root="NQ")

ES_M2026 = FuturesContractId(product=ES, contract_month=ContractMonth.JUNE, contract_year=2026)
ES_U2026 = FuturesContractId(product=ES, contract_month=ContractMonth.SEPTEMBER, contract_year=2026)
NQ_M2026 = FuturesContractId(product=NQ, contract_month=ContractMonth.JUNE, contract_year=2026)

BASE_TIME = datetime(2026, 6, 1, 14, 0, tzinfo=UTC)


def trade(
    *,
    offset: int = 0,
    price: str = "4500.25",
    size: int = 1,
    side: TradeSide = TradeSide.BUY,
    symbol: str = "ESM6",
) -> Trade:
    return Trade(
        timestamp=BASE_TIME + timedelta(seconds=offset),
        instrument_symbol=symbol,
        price=Decimal(price),
        size=Decimal(size),
        side=side,
    )


def quote(*, offset: int = 0, bid: str = "4500.00", ask: str = "4500.25") -> Quote:
    return Quote(
        timestamp=BASE_TIME + timedelta(seconds=offset),
        instrument_symbol="ESM6",
        bid=Decimal(bid),
        ask=Decimal(ask),
        bid_size=Decimal(1),
        ask_size=Decimal(2),
    )


def bar(*, offset: int = 0, close: str = "4501.00") -> Bar:
    return Bar(
        instrument_symbol="ESM6",
        interval_start=BASE_TIME + timedelta(minutes=offset),
        interval=timedelta(minutes=1),
        open=Decimal("4499.50"),
        high=Decimal("4502.00"),
        low=Decimal("4498.75"),
        close=Decimal(close),
        volume=Decimal(1000),
    )


def three_trades() -> list[Trade]:
    return [
        trade(offset=0, price="4500.25"),
        trade(offset=1, price="4500.50", side=TradeSide.SELL),
        trade(offset=2, price="4500.75", size=3),
    ]


def manifest_for(
    artifact: Path,
    records: list[Trade] | list[Quote] | list[Bar],
    *,
    record_type: RecordType = RecordType.TRADE,
    contract: FuturesContractId = ES_M2026,
    provider_token: str | None = "DATABENTO",
    vendor_symbols: tuple[str, ...] | None = None,
    origin: DatasetOrigin = DatasetOrigin.IMPORTED,
) -> DatasetManifest:
    """Build a manifest whose claims match ``artifact`` exactly."""
    earliest, latest = dataset_time_bounds(records)
    aliases = (
        tuple(sorted({record.instrument_symbol for record in records}))
        if vendor_symbols is None
        else vendor_symbols
    )
    return build_manifest(
        semantic_hash=semantic_dataset_hash(
            record_type=record_type, contract=contract, records=records
        ),
        physical_hash=physical_artifact_hash(artifact),
        schema_version=SCHEMA_VERSION,
        record_type=record_type,
        contract=contract,
        row_count=len(records),
        min_timestamp=earliest,
        max_timestamp=latest,
        source=SourceProvenance(
            origin=origin,
            provider_token=provider_token,
            source_artifact_hash=None,
            vendor_symbols=aliases,
        ),
        transformation=TransformationProvenance(
            kind=TransformationKind.NONE, symbol_mapping=(), source_schema_version=None
        ),
    )
