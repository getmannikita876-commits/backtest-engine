"""Explicit comparison of two independently persisted datasets.

Comparison is a call someone makes, never something that happens. Nothing in
import or registration consults it, so an import produces the same result
regardless of what else the local catalog happens to contain — there is no
ambient cross-batch state anywhere in the platform.

What this reports, and what it refuses to
-----------------------------------------
It reports **evidence**: which exact canonical rows both datasets hold, with
multiplicity; whether the same rows appear in a different order; and, for bars,
which periods agree and which contradict each other. It does not report which
dataset is right, which is newer, or which corrects which. That is a claim, and
claims are operator-declared and published as lineage relations.

The limit is not conservatism for its own sake — it is what the stored records
can prove. A trade has **no persisted logical event identity** (ADR-003): the
domain carries no venue, no exchange sequence number, and no vendor trade
identifier, and identifying trades by their attributes was found to *destroy
data*, because one-lot fills at the same price inside the same microsecond are
ordinary tick data rather than an edge case. A quote is in the same position.

So for trades and quotes this module answers only "are these the same rows?" and
never "does this row correct that one" — and in particular a shared timestamp is
**not** treated as a logical event identity. Two rows at one instant may be a
correction, two independent events, or a backfill, and nothing persisted
distinguishes them.

Bars are different, and the difference is earned. ADR-005 established that a bar
is a summary *keyed by its period*, with OHLCV as claims about that period rather
than part of its identity. Schema v3 then made the contract a dataset-level fact.
So two bars can be compared by key, and disagreement about one period is a
provable conflict — still with no direction implied.

Ephemeral by design
-------------------
The result is a plain value object. It is not published, not hashed, and not
indexed: a comparison is a measurement of two artifacts at a moment, not a new
immutable fact about the world, and persisting it would create a second artifact
subsystem to keep consistent with the first.
"""

from __future__ import annotations

from collections import Counter
from datetime import timedelta
from pathlib import Path
from typing import Any, Final, final

from pydantic import model_validator

from quant_research_terminal.catalog.errors import (
    AmbiguousBarKeyError,
    CatalogError,
    DatasetNotComparableError,
    MissingArtifactError,
)
from quant_research_terminal.catalog.manifest import DatasetManifest
from quant_research_terminal.catalog.store import (
    locate_manifest,
    read_manifest,
    verify_artifact_against_manifest,
)
from quant_research_terminal.data.parquet_store import read_records_v3
from quant_research_terminal.domain.common import CanonicalModel
from quant_research_terminal.domain.dataset_identity import (
    MICROSECOND_UNIT,
    ManifestHash,
    RecordType,
    canonical_record_bytes,
    require_digest,
)
from quant_research_terminal.domain.models import Bar
from quant_research_terminal.domain.time import epoch_microseconds

#: Imported rather than redefined. Two spellings of one unit in two modules is
#: precisely the drift ``canonical_record_bytes`` exists to prevent.
_MICROSECOND_UNIT: Final[timedelta] = MICROSECOND_UNIT


@final
class BarKey(CanonicalModel):
    """A bar's period, which is its identity (ADR-005).

    ADR-005 defines bar identity as ``(instrument_symbol, interval_start,
    interval)``. This substitutes the **canonical contract** for the vendor
    alias, which is what schema v3 makes possible and what ADR-012 requires:
    ``vendor_symbol`` is provenance and must never decide whether two rows
    describe the same thing.

    The contract itself is not a field here. It is a dataset-level fact in
    version 3 — one value in the schema metadata, verified equal on every row —
    and comparison establishes it once for both datasets before any key is
    built, so carrying it per key would store the same constant a million times.
    """

    interval_start_microseconds: int
    interval_microseconds: int

    def sort_key(self) -> tuple[int, int]:
        """Return the deterministic ordering key."""
        return (self.interval_start_microseconds, self.interval_microseconds)


def bar_key(bar: Bar) -> BarKey:
    """Return the period identifying one bar.

    Integer division by a one-microsecond ``timedelta``, exactly as the semantic
    encoder does it — ``total_seconds()`` would route the value through a float
    and could round two distinct intervals together.
    """
    return BarKey(
        interval_start_microseconds=epoch_microseconds(bar.interval_start),
        interval_microseconds=bar.interval // _MICROSECOND_UNIT,
    )


@final
class BarKeyComparison(CanonicalModel):
    """What two bar datasets say about the periods they share.

    ``conflicting`` is the load-bearing one: both datasets describe the same
    period and disagree about what happened in it. That is a proven
    contradiction and **no direction is implied** — neither side is newer,
    better, or a correction of the other. ADR-005 refused to guess a winner
    inside one batch for exactly this reason, and nothing across batches makes
    the guess safer.
    """

    identical_overlap_keys: tuple[BarKey, ...]
    conflicting_keys: tuple[BarKey, ...]
    left_only_keys: tuple[BarKey, ...]
    right_only_keys: tuple[BarKey, ...]

    @property
    def identical_overlap_count(self) -> int:
        """How many periods both datasets describe identically."""
        return len(self.identical_overlap_keys)

    @property
    def conflict_count(self) -> int:
        """How many periods both datasets describe differently."""
        return len(self.conflicting_keys)

    @property
    def left_only_count(self) -> int:
        """How many periods only the left dataset describes."""
        return len(self.left_only_keys)

    @property
    def right_only_count(self) -> int:
        """How many periods only the right dataset describes."""
        return len(self.right_only_keys)


@final
class DatasetComparison(CanonicalModel):
    """Evidence about two datasets. Ephemeral; never published.

    Every field states something the rows prove. There is deliberately no
    ``verdict``, ``status``, or ``correction`` field: naming a conclusion the
    data does not support is the failure this whole phase is arranged to avoid.
    """

    left: ManifestHash
    right: ManifestHash
    record_type: RecordType
    semantically_identical: bool
    same_sequence: bool
    same_multiset: bool
    reordered_only: bool
    temporal_ranges_disjoint: bool
    left_row_count: int
    right_row_count: int
    exact_row_overlap_count: int
    left_only_exact_row_count: int
    right_only_exact_row_count: int
    bars: BarKeyComparison | None

    @model_validator(mode="after")
    def _validate_comparison(self) -> DatasetComparison:
        # The same exact-type guard the relation and the manifest use. A digest
        # subclass overriding ``canonical()`` would make the reported identity
        # disagree with the object that was actually compared.
        require_digest(self.left, ManifestHash)
        require_digest(self.right, ManifestHash)
        return self

    @property
    def has_exact_row_overlap(self) -> bool:
        """Whether any exact canonical row appears in both datasets.

        Deliberately named for what it measures. ``exact_row_overlap_count == 0``
        means *no identical rows were found*; it does **not** mean the underlying
        market events are disjoint. Without a stable event identity, two
        different rows may still describe one event — one of them corrected —
        and no count can tell.
        """
        return self.exact_row_overlap_count > 0


def _ranges_disjoint(left: DatasetManifest, right: DatasetManifest) -> bool:
    """Return whether the manifests' time bounds prove no overlap.

    A prefilter only, and only in the direction that is safe: disjoint ranges
    prove there is no shared instant, while overlapping ranges prove nothing
    about the rows inside them. Coverage summaries never substitute for the
    exact row comparison.
    """
    left_min, left_max = left.min_timestamp, left.max_timestamp
    right_min, right_max = right.min_timestamp, right.max_timestamp
    # Explicit ``is None`` guards, never a filtering comprehension. Writing this
    # as a generator that drops non-``datetime`` bounds would turn a prefilter
    # documented to prove nothing into a raw unpacking ``ValueError`` the moment
    # a bound had an unexpected type.
    if left_min is None or left_max is None or right_min is None or right_max is None:
        return False
    return left_max < right_min or right_max < left_min


def _resolve_artifact(catalog_root: Path, storage_root: Path, manifest: DatasetManifest) -> Path:
    """Return a verified artifact for a manifest, or raise.

    Every location the catalog offers is tried, not just the first. The index is
    explicitly the part of the catalog allowed to be wrong, and byte-identical
    copies are interchangeable, so one stale entry must not turn a comparison
    that a good surviving copy could answer into a hard failure. This matches
    :func:`~quant_research_terminal.catalog.store.verify_manifest`, which also
    accepts any location that satisfies the manifest.

    Nothing is read for comparison until the bytes on disk are proven to be the
    bytes the manifest describes: comparing a replaced or corrupted file would
    produce confident evidence about data nobody stored.
    """
    locations = locate_manifest(
        catalog_root=catalog_root, storage_root=storage_root, manifest=manifest
    )
    if not locations:
        raise MissingArtifactError(
            f"the catalog knows no current location holding the bytes "
            f"{manifest.physical_hash.canonical()} that manifest "
            f"{manifest.manifest_hash.canonical()} describes"
        )

    failure: CatalogError | None = None
    for artifact in locations:
        try:
            verify_artifact_against_manifest(artifact, manifest)
        except CatalogError as error:
            failure = error
            continue
        return artifact

    # Every known location failed. Re-raise the last diagnosis rather than a
    # generic one, so the message names an actual file and what was wrong with it.
    assert failure is not None
    raise failure


def compare_datasets(
    *,
    catalog_root: Path,
    storage_root: Path,
    left: ManifestHash,
    right: ManifestHash,
) -> DatasetComparison:
    """Compare two registered datasets and report what the rows prove.

    Args:
        catalog_root: Where manifests and the location index live.
        storage_root: Where artifacts live.
        left: Exact manifest hash of the first dataset.
        right: Exact manifest hash of the second dataset.

    Returns:
        Ephemeral evidence. Nothing is published, nothing is merged, and no
        lineage is created — publishing a correction claim stays an explicit,
        separate act.

    Raises:
        MissingManifestError: if either manifest is not published.
        DatasetNotComparableError: if the datasets describe different listed
            contracts or different record types.
        MissingArtifactError: if either artifact is not currently locatable.
        AmbiguousBarKeyError: if a bar dataset holds two rows for one period.
    """
    left_manifest = read_manifest(catalog_root, left)
    right_manifest = read_manifest(catalog_root, right)

    if left_manifest.record_type is not right_manifest.record_type:
        raise DatasetNotComparableError(
            f"{left.canonical()} holds {left_manifest.record_type.value} records and "
            f"{right.canonical()} holds {right_manifest.record_type.value}; these are "
            f"different kinds of data, not disagreeing versions of one dataset"
        )
    if left_manifest.contract != right_manifest.contract:
        raise DatasetNotComparableError(
            f"{left.canonical()} describes {left_manifest.contract.canonical()} and "
            f"{right.canonical()} describes {right_manifest.contract.canonical()}; "
            f"datasets for different listed contracts are unrelated, and calling them "
            f"conflicting would be a false statement about both"
        )

    record_type = left_manifest.record_type
    disjoint = _ranges_disjoint(left_manifest, right_manifest)

    left_artifact = _resolve_artifact(catalog_root, storage_root, left_manifest)
    right_artifact = _resolve_artifact(catalog_root, storage_root, right_manifest)

    # Phase 2.3 defines the semantic hash over the canonical ordered records, so
    # equality *is* equality of the logical dataset. Different physical encoding
    # or provenance does not make two datasets different data, and this is
    # emphatically not a revision.
    #
    # It is reported as a fact rather than used to skip the rest. An early return
    # would make ``bars`` mean "not a bar dataset" *or* "identical bars"
    # depending on a flag the caller had to check first, and — worse — it would
    # make the duplicate-period refusal below depend on which other dataset you
    # happened to compare against. A guarantee that holds only for some
    # comparison partners is not a guarantee.
    semantically_identical = left_manifest.semantic_hash == right_manifest.semantic_hash

    left_records = read_records_v3(left_artifact).records
    right_records = read_records_v3(right_artifact).records

    left_rows = [canonical_record_bytes(record_type, record) for record in left_records]
    right_rows = [canonical_record_bytes(record_type, record) for record in right_records]

    left_counts = Counter(left_rows)
    right_counts = Counter(right_rows)
    # Multiplicity is preserved throughout: ``[X, X, Y]`` and ``[X, Y]`` are not
    # the same multiset, and de-duplicating would erase real repeated executions
    # that ADR-003 established must never be collapsed.
    overlap = sum((left_counts & right_counts).values())

    same_multiset = left_counts == right_counts
    same_sequence = left_rows == right_rows

    # Computed before the result is constructed, not inside its argument list:
    # a bar-specific refusal must not be raised from a position where the
    # row-level evidence already in hand is silently discarded on the way out.
    bars = (
        _compare_bars(left_records, right_records, left=left, right=right)
        if record_type is RecordType.BAR
        else None
    )

    return DatasetComparison(
        left=left,
        right=right,
        record_type=record_type,
        semantically_identical=semantically_identical,
        same_sequence=same_sequence,
        same_multiset=same_multiset,
        reordered_only=same_multiset and not same_sequence,
        temporal_ranges_disjoint=disjoint,
        left_row_count=len(left_rows),
        right_row_count=len(right_rows),
        exact_row_overlap_count=overlap,
        left_only_exact_row_count=sum((left_counts - right_counts).values()),
        right_only_exact_row_count=sum((right_counts - left_counts).values()),
        bars=bars,
    )


def _compare_bars(
    left_records: tuple[Any, ...],
    right_records: tuple[Any, ...],
    *,
    left: ManifestHash,
    right: ManifestHash,
) -> BarKeyComparison:
    """Compare two bar datasets by period.

    Raises:
        AmbiguousBarKeyError: if either dataset holds more than one row for one
            period. Key-based comparison is undefined over a multiset-keyed
            relation, and both available shortcuts — first wins, last wins — are
            guesses about which claim counts.
    """
    left_map = _bar_map(left_records, manifest_hash=left)
    right_map = _bar_map(right_records, manifest_hash=right)

    shared = sorted(set(left_map) & set(right_map))
    identical = tuple(key for key in shared if left_map[key][1] == right_map[key][1])
    conflicting = tuple(key for key in shared if left_map[key][1] != right_map[key][1])

    return BarKeyComparison(
        identical_overlap_keys=tuple(left_map[key][0] for key in identical),
        conflicting_keys=tuple(left_map[key][0] for key in conflicting),
        left_only_keys=tuple(left_map[key][0] for key in sorted(set(left_map) - set(right_map))),
        right_only_keys=tuple(right_map[key][0] for key in sorted(set(right_map) - set(left_map))),
    )


def _bar_map(
    records: tuple[Any, ...], *, manifest_hash: ManifestHash
) -> dict[tuple[int, int], tuple[BarKey, bytes]]:
    """Return period → (key, canonical payload), refusing duplicate periods."""
    mapping: dict[tuple[int, int], tuple[BarKey, bytes]] = {}
    duplicates: list[BarKey] = []
    for record in records:
        key = bar_key(record)
        payload = canonical_record_bytes(RecordType.BAR, record)
        if key.sort_key() in mapping:
            duplicates.append(key)
            continue
        mapping[key.sort_key()] = (key, payload)
    if duplicates:
        named = sorted({item.sort_key() for item in duplicates})
        raise AmbiguousBarKeyError(
            f"the dataset described by {manifest_hash.canonical()} holds more than one row "
            f"for {len(named)} bar period(s), first at interval_start "
            f"{named[0][0]}us with interval {named[0][1]}us. Comparing by period would "
            f"require choosing which row counts, and both first-wins and last-wins are "
            f"guesses"
        )
    return mapping


__all__ = [
    "BarKey",
    "BarKeyComparison",
    "DatasetComparison",
    "bar_key",
    "compare_datasets",
]
