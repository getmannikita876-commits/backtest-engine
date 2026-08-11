"""Shared builders for the Phase 3 deterministic-replay tests.

Replay's guarantees are about *real* artifacts resolved through a *real*
catalog, so these helpers build both on disk rather than faking a source. A
prepared replay assembled from hand-made in-memory rows would exercise the merge
and none of the verification, and verification is half the phase.

Timestamps are constructed from explicit offsets against one base instant, so a
test that cares about a microsecond can say so.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

from dataset_fixtures import manifest_for

from quant_research_terminal.catalog.manifest import (
    DatasetManifest,
    build_manifest,
    manifest_to_json_bytes,
)
from quant_research_terminal.catalog.provenance import (
    DatasetOrigin,
    SourceProvenance,
    TransformationKind,
    TransformationProvenance,
)
from quant_research_terminal.catalog.store import (
    manifest_path,
    publish_bytes_atomically,
    register_dataset,
)
from quant_research_terminal.data.artifact_hash import physical_artifact_hash
from quant_research_terminal.data.contracts import LEGACY_SCHEMA_VERSION
from quant_research_terminal.data.parquet_store import (
    write_bars_v3,
    write_quotes_v3,
    write_trades_v3,
)
from quant_research_terminal.domain.dataset_identity import (
    RecordType,
    dataset_time_bounds,
    semantic_dataset_hash,
)
from quant_research_terminal.domain.futures_contract import FuturesContractId
from quant_research_terminal.domain.models import Bar, Quote, Trade
from quant_research_terminal.domain.replay import ReplayFrame
from quant_research_terminal.domain.time import epoch_microseconds
from quant_research_terminal.domain.trade_side import TradeSide

#: The instant every fixture offset is measured from.
BASE: datetime = datetime(2026, 6, 1, 10, 0, tzinfo=UTC)

#: The interval every fixture bar covers unless a test says otherwise.
MINUTE: timedelta = timedelta(minutes=1)


def at(**offset: float) -> datetime:
    """Return ``BASE`` shifted by an explicit ``timedelta`` keyword offset."""
    return BASE + timedelta(**offset)


def trade_at(
    when: datetime,
    *,
    price: str = "4500.25",
    size: int = 1,
    side: TradeSide = TradeSide.BUY,
    symbol: str = "ESM6",
) -> Trade:
    """Return a trade whose availability time is ``when``."""
    return Trade(
        timestamp=when,
        instrument_symbol=symbol,
        price=Decimal(price),
        size=Decimal(size),
        side=side,
    )


def quote_at(
    when: datetime,
    *,
    bid: str = "4500.00",
    ask: str = "4500.25",
    symbol: str = "ESM6",
) -> Quote:
    """Return a quote whose availability time is ``when``."""
    return Quote(
        timestamp=when,
        instrument_symbol=symbol,
        bid=Decimal(bid),
        ask=Decimal(ask),
        bid_size=Decimal(1),
        ask_size=Decimal(2),
    )


def bar_starting(
    interval_start: datetime,
    *,
    interval: timedelta = MINUTE,
    close: str = "4501.00",
    symbol: str = "ESM6",
) -> Bar:
    """Return a bar covering ``[interval_start, interval_start + interval)``.

    Its availability time — and therefore the instant replay makes it visible —
    is the interval **close**, never ``interval_start`` (ADR-002).
    """
    return Bar(
        instrument_symbol=symbol,
        interval_start=interval_start,
        interval=interval,
        open=Decimal("4499.50"),
        high=Decimal("4502.00"),
        low=Decimal("4498.75"),
        close=Decimal(close),
        volume=Decimal(1000),
    )


def bar_available_at(
    when: datetime, *, interval: timedelta = MINUTE, close: str = "4501.00"
) -> Bar:
    """Return a bar that becomes visible exactly at ``when``."""
    return bar_starting(when - interval, interval=interval, close=close)


#: Annotated ``Any`` for the reason ``parquet_store._V3_ENCODERS`` is: the three
#: writers take different record sequences, so a precise callable type would be
#: a union no call site can narrow.
_WRITERS: Mapping[RecordType, Any] = {
    RecordType.TRADE: write_trades_v3,
    RecordType.QUOTE: write_quotes_v3,
    RecordType.BAR: write_bars_v3,
}


def write_artifact(
    path: Path,
    records: list[Trade] | list[Quote] | list[Bar],
    *,
    record_type: RecordType,
    contract: FuturesContractId,
) -> Path:
    """Write a version-3 artifact of ``record_type`` at ``path``."""
    path.parent.mkdir(parents=True, exist_ok=True)
    _WRITERS[record_type](path, records, contract=contract)
    return path


def publish_dataset(
    *,
    catalog_root: Path,
    storage_root: Path,
    name: str,
    records: list[Trade] | list[Quote] | list[Bar],
    record_type: RecordType,
    contract: FuturesContractId,
    provider_token: str | None = "DATABENTO",
) -> DatasetManifest:
    """Write, verify, and register one version-3 dataset, returning its manifest.

    ``provider_token`` is the knob for building two manifests over one dataset's
    semantics: provenance reaches the manifest hash and never the semantic one,
    so two tokens over identical records give two manifest identities pinning a
    single :class:`SemanticDatasetHash`.
    """
    artifact = write_artifact(
        storage_root / name, records, record_type=record_type, contract=contract
    )
    manifest = manifest_for(
        artifact,
        records,
        record_type=record_type,
        contract=contract,
        provider_token=provider_token,
    )
    register_dataset(
        catalog_root=catalog_root,
        storage_root=storage_root,
        artifact_path=artifact,
        manifest=manifest,
    )
    return manifest


def publish_legacy_v2_manifest(
    *,
    catalog_root: Path,
    artifact: Path,
    records: list[Trade],
    contract: FuturesContractId,
) -> DatasetManifest:
    """Publish a manifest claiming schema version 2, bypassing registration.

    Registration would refuse it — it requires the current schema version — so
    the document is published directly. That is the state a catalog written by an
    older build would be in, and it is the only way to reach replay's
    version-2 refusal without inventing a fake manifest.
    """
    earliest, latest = dataset_time_bounds(records)
    manifest = build_manifest(
        semantic_hash=semantic_dataset_hash(
            record_type=RecordType.TRADE, contract=contract, records=records
        ),
        physical_hash=physical_artifact_hash(artifact),
        schema_version=LEGACY_SCHEMA_VERSION,
        record_type=RecordType.TRADE,
        contract=contract,
        row_count=len(records),
        min_timestamp=earliest,
        max_timestamp=latest,
        source=SourceProvenance(
            origin=DatasetOrigin.IMPORTED,
            provider_token="LEGACY",
            source_artifact_hash=None,
            vendor_symbols=tuple(sorted({record.instrument_symbol for record in records})),
        ),
        transformation=TransformationProvenance(
            kind=TransformationKind.NONE, symbol_mapping=(), source_schema_version=None
        ),
    )
    publish_bytes_atomically(
        manifest_path(catalog_root, manifest.manifest_hash), manifest_to_json_bytes(manifest)
    )
    return manifest


def frame_digest(frames: list[ReplayFrame]) -> list[dict[str, object]]:
    """Return a stable, comparable representation of a frame stream.

    Built from canonical values — epoch microseconds, canonical identity
    strings, the digest, the ordinal — rather than from model ``repr``. A repr is
    a formatting choice that can change without any semantic change, so
    comparing two runs by it would test the formatter rather than the timeline.
    Used for the cross-process determinism tests, where the comparison has to
    survive JSON.
    """
    return [
        {
            "availability_us": epoch_microseconds(frame.availability_time),
            "events": [
                {
                    "record_type": event.record_type.value,
                    "contract": event.contract.canonical(),
                    "source_manifest": event.source_manifest.canonical(),
                    "row_ordinal": event.row_ordinal,
                    "payload_us": epoch_microseconds(event.payload.timestamp),
                }
                for event in frame.events
            ],
        }
        for frame in frames
    ]
