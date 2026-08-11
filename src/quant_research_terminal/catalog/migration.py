"""Explicit, never-inferred migration of version-2 artifacts to version 3.

A version-2 artifact records a vendor alias — ``"ESM6"`` — and nothing else.
Turning that into ``CME:ES:M2026`` requires a venue the file does not contain
and a decade the symbol does not carry, so this module **refuses to guess**.
The caller supplies a mapping from alias to exact contract, every alias in the
file must resolve through it, and an unmapped alias is a hard failure. There is
no fallback that parses a symbol, no current-year default, and no month-cycle
arithmetic anywhere in this file.

Migration creates a new artifact
--------------------------------
The version-2 input is opened read-only and never modified. The output is a
new version-3 artifact with its own identity, and the link back to the source
is recorded as provenance: the source artifact's physical hash and the full
declared mapping. That is lineage, not a correction graph — supersedes chains
and revision history are deliberately absent.

What migration does and does not preserve
-----------------------------------------
Records survive exactly: the same values, in the same order. Bytes do not, and
cannot — reading normalises where storage was lenient, so a source that stored
``"BUY"`` produces a version-3 file holding ``"buy"``. The two physical hashes
are therefore unrelated, and the honest link between them is the recorded
source hash rather than any byte comparison.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Final

from quant_research_terminal.catalog.errors import UnsupportedMigrationError
from quant_research_terminal.catalog.manifest import DatasetManifest, build_manifest
from quant_research_terminal.catalog.provenance import (
    DatasetOrigin,
    SourceProvenance,
    TransformationKind,
    TransformationProvenance,
)
from quant_research_terminal.data.artifact_hash import physical_artifact_hash
from quant_research_terminal.data.contracts import LEGACY_SCHEMA_VERSION, SCHEMA_VERSION
from quant_research_terminal.data.parquet_store import (
    StorageError,
    read_bars,
    read_quotes,
    read_trades,
    write_bars_v3,
    write_quotes_v3,
    write_trades_v3,
)
from quant_research_terminal.domain.dataset_identity import (
    RecordType,
    dataset_time_bounds,
    require_record_type,
    semantic_dataset_hash,
)
from quant_research_terminal.domain.futures_contract import (
    FuturesContractId,
    require_listed_contract,
)

_V2_READERS: Final[Mapping[RecordType, object]] = {
    RecordType.TRADE: read_trades,
    RecordType.QUOTE: read_quotes,
    RecordType.BAR: read_bars,
}

_V3_WRITERS: Final[Mapping[RecordType, object]] = {
    RecordType.TRADE: write_trades_v3,
    RecordType.QUOTE: write_quotes_v3,
    RecordType.BAR: write_bars_v3,
}


def normalize_symbol_mapping(
    mapping: Mapping[str, FuturesContractId],
) -> tuple[tuple[str, FuturesContractId], ...]:
    """Return a mapping in deterministic, canonical order.

    Sorted by vendor symbol using plain code-point comparison — no locale, no
    case folding — so the serialization cannot depend on dict insertion order
    or on the machine's collation.
    """
    normalized: list[tuple[str, FuturesContractId]] = []
    for symbol, contract in mapping.items():
        if not isinstance(symbol, str) or not symbol.strip():
            raise UnsupportedMigrationError(
                f"a mapping key must be a non-empty vendor symbol; got {symbol!r}"
            )
        try:
            require_listed_contract(contract)
        except TypeError as error:
            raise UnsupportedMigrationError(
                f"the mapping target for {symbol!r} is not a listed contract: {error}"
            ) from error
        normalized.append((symbol, contract))
    return tuple(sorted(normalized, key=lambda item: item[0]))


def _require_distinct_paths(source: Path, destination: Path) -> None:
    """Refuse a migration that would write over its own input.

    "Opened read-only; never modified" is a claim about the source, and without
    this check it was only true when the caller happened to pass two different
    paths. Writing the output over the input destroys the version-2 artifact
    and leaves the recorded source hash describing the migration's own result —
    a lineage record pointing at itself.

    Compared after ``resolve()``, so a relative path, a trailing ``.``, or a
    symlink cannot smuggle the same file past a string comparison.

    Raises:
        UnsupportedMigrationError: if the two paths denote one file.
    """
    if source.resolve() == destination.resolve():
        raise UnsupportedMigrationError(
            f"the migration destination {destination} is the source artifact; migration "
            f"produces a new artifact and never overwrites its input"
        )


def migrate_v2_to_v3(
    *,
    source: Path,
    destination: Path,
    record_type: RecordType,
    contract: FuturesContractId,
    mapping: Mapping[str, FuturesContractId],
    allow_unused: bool = False,
    provider_token: str | None = None,
) -> DatasetManifest:
    """Migrate one version-2 artifact into a new version-3 artifact.

    Args:
        source: The version-2 artifact. Opened read-only; never modified.
        destination: Where the new version-3 artifact is written.
        record_type: Which record type the source holds.
        contract: The contract the output declares. Every alias in the source
            must map to exactly this contract — several aliases mapping to one
            contract is the ordinary case and is fine, but a source spanning
            two contracts cannot become one single-instrument artifact and is
            refused rather than silently split.
        mapping: Alias to exact contract. Explicit and complete; nothing is
            inferred from a symbol's spelling.
        allow_unused: Whether mapping entries never seen in the source are
            tolerated. Defaults to refusing them, because an unused entry is
            usually a typo whose real symbol will then trip the unmapped check
            and produce a confusing second-order error.
        provider_token: Optional operator-declared source label.

    Returns:
        The manifest describing the new artifact. It is **not** registered;
        registration is a separate, verifying step.

    Raises:
        UnsupportedMigrationError: for any alias that does not resolve, any
            alias resolving to a different contract, unused entries when they
            are not allowed, or an unreadable source.
    """
    require_record_type(record_type)
    require_listed_contract(contract)
    _require_distinct_paths(source, destination)
    normalized = normalize_symbol_mapping(mapping)
    lookup = dict(normalized)

    reader = _V2_READERS[record_type]
    try:
        records = reader(source)  # type: ignore[operator]
    except StorageError as error:
        raise UnsupportedMigrationError(
            f"{source} is not a readable version-{LEGACY_SCHEMA_VERSION} "
            f"{record_type.value} artifact: {error}"
        ) from error

    aliases = {record.instrument_symbol for record in records}
    unmapped = sorted(alias for alias in aliases if alias not in lookup)
    if unmapped:
        raise UnsupportedMigrationError(
            f"{source} carries vendor symbols with no explicit mapping: {unmapped}. A "
            f"canonical identity is never inferred from an alias — supply the mapping"
        )

    divergent = sorted(
        {alias for alias in aliases if lookup[alias] != contract},
    )
    if divergent:
        targets = sorted({lookup[alias].canonical() for alias in divergent})
        raise UnsupportedMigrationError(
            f"{source} maps to more than one contract ({targets}); a version-3 artifact "
            f"holds exactly one, so this file cannot be migrated as {contract.canonical()}"
        )

    unused = sorted(set(lookup) - aliases)
    if unused and not allow_unused:
        raise UnsupportedMigrationError(
            f"mapping declares entries not present in {source}: {unused}. Pass "
            f"allow_unused=True to accept them deliberately"
        )

    # Hashed *before* the write, not after. Even with the same-path guard above,
    # deriving the source's provenance hash after producing the output makes the
    # recorded lineage depend on the write having left the source alone — and a
    # provenance field that can come to describe its own output is worse than no
    # provenance field at all.
    source_hash = physical_artifact_hash(source)

    writer = _V3_WRITERS[record_type]
    writer(destination, records, contract=contract)  # type: ignore[operator]

    earliest, latest = dataset_time_bounds(records)
    return build_manifest(
        semantic_hash=semantic_dataset_hash(
            record_type=record_type,
            contract=contract,
            records=records,
            expected_row_count=len(records),
        ),
        physical_hash=physical_artifact_hash(destination),
        schema_version=SCHEMA_VERSION,
        record_type=record_type,
        contract=contract,
        row_count=len(records),
        min_timestamp=earliest,
        max_timestamp=latest,
        source=SourceProvenance(
            origin=DatasetOrigin.MIGRATED_FROM_V2,
            provider_token=provider_token,
            source_artifact_hash=source_hash,
            vendor_symbols=tuple(sorted(aliases)),
        ),
        transformation=TransformationProvenance(
            kind=TransformationKind.SYMBOL_MAPPING,
            symbol_mapping=normalized,
            source_schema_version=LEGACY_SCHEMA_VERSION,
        ),
    )
