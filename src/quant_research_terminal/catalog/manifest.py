"""The immutable claims that identify one dataset artifact.

A manifest records what an artifact *is*: which contract, which record type,
how many rows, what time range, which semantic and physical identity, and where
it came from. It records nothing about where the artifact is **stored**.

That omission is the design. Because the model has no location field at all,
moving an artifact cannot change its identity — not by policy, not by a test
that might be deleted, but because there is nothing to change. The path lives
in the catalog's location index, which is derived state.

Three identities, never collapsed
---------------------------------
``semantic_hash`` — the data. ``physical_hash`` — the bytes.
``manifest_hash`` — these claims plus provenance. A re-encode changes the
physical hash alone; a different provider changes the manifest hash alone; only
a change to the records changes the semantic hash. Collapsing any pair would
make it impossible to tell a harmless re-encode from an edited dataset.

The manifest hash covers every field except itself. It is re-derived on
construction and compared, so a manifest carrying a hash that does not describe
its own contents is unconstructible rather than merely wrong — the pattern
established for materialized calendars (ADR-010) and roll schedules (ADR-011).
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import UTC, datetime, timedelta
from typing import Any, Final, final

from pydantic import field_validator, model_validator

from quant_research_terminal.catalog.errors import ManifestHashMismatchError
from quant_research_terminal.catalog.provenance import (
    SourceProvenance,
    TransformationProvenance,
)
from quant_research_terminal.data.contracts import SUPPORTED_SCHEMA_VERSIONS
from quant_research_terminal.domain.common import CanonicalModel
from quant_research_terminal.domain.dataset_identity import (
    ManifestHash,
    PhysicalArtifactHash,
    RecordType,
    SemanticDatasetHash,
    require_digest,
    require_record_type,
)
from quant_research_terminal.domain.futures_contract import (
    FuturesContractId,
    require_listed_contract,
)
from quant_research_terminal.domain.time import epoch_microseconds, validate_utc_datetime

#: Bumping this re-keys every manifest, which is exactly what a change to the
#: manifest's canonical serialization must do.
MANIFEST_FORMAT: Final[str] = "qrt-dataset-manifest/1"

_HASH_PATTERN: Final[re.Pattern[str]] = re.compile(r"[0-9a-f]{64}")

_EPOCH: Final[datetime] = datetime(1970, 1, 1, tzinfo=UTC)


def manifest_canonical_payload(
    *,
    semantic_hash: SemanticDatasetHash,
    physical_hash: PhysicalArtifactHash,
    schema_version: int,
    record_type: RecordType,
    contract: FuturesContractId,
    row_count: int,
    min_timestamp: datetime | None,
    max_timestamp: datetime | None,
    source: SourceProvenance,
    transformation: TransformationProvenance,
) -> dict[str, Any]:
    """Return the deterministic payload a manifest hash is computed over.

    Excludes the manifest hash itself — a hash cannot cover its own value — and
    excludes every storage location, which is why relocation is identity-free.
    """
    return {
        "format": MANIFEST_FORMAT,
        "semantic_hash": semantic_hash.canonical(),
        "physical_hash": physical_hash.canonical(),
        "schema_version": schema_version,
        "record_type": record_type.value,
        "contract": contract.canonical(),
        "row_count": row_count,
        "min_timestamp": None if min_timestamp is None else epoch_microseconds(min_timestamp),
        "max_timestamp": None if max_timestamp is None else epoch_microseconds(max_timestamp),
        "source": source.canonical_payload(),
        "transformation": transformation.canonical_payload(),
    }


def canonical_manifest_bytes(payload: dict[str, Any]) -> bytes:
    """Serialize a manifest payload deterministically.

    ``ensure_ascii`` stays on. Vendor symbols are not constrained to ASCII
    anywhere in the platform, and the established hashing idiom encodes the
    result as ASCII — turning escaping off "for readability" would make a
    non-ASCII alias crash the serializer.
    """
    return json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False
    ).encode("ascii")


def compute_manifest_hash(payload: dict[str, Any]) -> ManifestHash:
    """Return the SHA-256 of a manifest's canonical serialization."""
    return ManifestHash(value=hashlib.sha256(canonical_manifest_bytes(payload)).hexdigest())


@final
class DatasetManifest(CanonicalModel):
    """The immutable, verifiable description of one dataset artifact."""

    manifest_format: str
    semantic_hash: SemanticDatasetHash
    physical_hash: PhysicalArtifactHash
    schema_version: int
    record_type: RecordType
    contract: FuturesContractId
    row_count: int
    min_timestamp: datetime | None
    max_timestamp: datetime | None
    source: SourceProvenance
    transformation: TransformationProvenance
    manifest_hash: ManifestHash

    @field_validator("min_timestamp", "max_timestamp", mode="before")
    @classmethod
    def _validate_bounds(cls, value: object) -> datetime | None:
        if value is None:
            return None
        return validate_utc_datetime(value)

    @field_validator("row_count")
    @classmethod
    def _validate_row_count(cls, value: int) -> int:
        if value < 0:
            raise ValueError(f"row count cannot be negative; got {value}")
        return value

    @field_validator("schema_version")
    @classmethod
    def _validate_schema_version(cls, value: int) -> int:
        """Reject a version the platform has never defined.

        Registration separately requires the current version, but a manifest is
        constructible and serializable without ever being registered, so a
        version validated only at the registration boundary is a claim that can
        be built, hashed, and published unchallenged.
        """
        if value not in SUPPORTED_SCHEMA_VERSIONS:
            raise ValueError(
                f"schema version must be one of {sorted(SUPPORTED_SCHEMA_VERSIONS)}; got {value}"
            )
        return value

    @model_validator(mode="after")
    def _validate_manifest(self) -> DatasetManifest:
        if self.manifest_format != MANIFEST_FORMAT:
            raise ValueError(
                f"manifest format must be {MANIFEST_FORMAT!r}; got {self.manifest_format!r}"
            )
        # Exact types, for the ADR-009 D4 reason: a subclass overriding
        # ``canonical()`` would let the hashed identity diverge from the object.
        # The three digests need the same guard as the contract and the record
        # type — ``@final`` binds a type checker, not the interpreter.
        require_record_type(self.record_type)
        require_listed_contract(self.contract)
        require_digest(self.semantic_hash, SemanticDatasetHash)
        require_digest(self.physical_hash, PhysicalArtifactHash)
        require_digest(self.manifest_hash, ManifestHash)

        empty = self.row_count == 0
        has_bounds = self.min_timestamp is not None or self.max_timestamp is not None
        if empty and has_bounds:
            raise ValueError(
                "an empty dataset has no timestamps; min_timestamp and max_timestamp "
                "must both be absent"
            )
        if not empty and (self.min_timestamp is None or self.max_timestamp is None):
            raise ValueError(
                f"a dataset of {self.row_count} rows has timestamps; both bounds are required"
            )
        if (
            self.min_timestamp is not None
            and self.max_timestamp is not None
            and self.min_timestamp > self.max_timestamp
        ):
            raise ValueError("min_timestamp must not be after max_timestamp")

        expected = compute_manifest_hash(
            manifest_canonical_payload(
                semantic_hash=self.semantic_hash,
                physical_hash=self.physical_hash,
                schema_version=self.schema_version,
                record_type=self.record_type,
                contract=self.contract,
                row_count=self.row_count,
                min_timestamp=self.min_timestamp,
                max_timestamp=self.max_timestamp,
                source=self.source,
                transformation=self.transformation,
            )
        )
        if self.manifest_hash != expected:
            raise ValueError(
                f"manifest_hash does not describe this manifest; expected "
                f"{expected.canonical()}, got {self.manifest_hash.canonical()}"
            )
        return self

    def canonical_payload(self) -> dict[str, Any]:
        """Return the payload this manifest's hash is computed over."""
        return manifest_canonical_payload(
            semantic_hash=self.semantic_hash,
            physical_hash=self.physical_hash,
            schema_version=self.schema_version,
            record_type=self.record_type,
            contract=self.contract,
            row_count=self.row_count,
            min_timestamp=self.min_timestamp,
            max_timestamp=self.max_timestamp,
            source=self.source,
            transformation=self.transformation,
        )


def build_manifest(
    *,
    semantic_hash: SemanticDatasetHash,
    physical_hash: PhysicalArtifactHash,
    schema_version: int,
    record_type: RecordType,
    contract: FuturesContractId,
    row_count: int,
    min_timestamp: datetime | None,
    max_timestamp: datetime | None,
    source: SourceProvenance,
    transformation: TransformationProvenance,
) -> DatasetManifest:
    """Build a manifest, computing its hash from its own content."""
    payload = manifest_canonical_payload(
        semantic_hash=semantic_hash,
        physical_hash=physical_hash,
        schema_version=schema_version,
        record_type=record_type,
        contract=contract,
        row_count=row_count,
        min_timestamp=min_timestamp,
        max_timestamp=max_timestamp,
        source=source,
        transformation=transformation,
    )
    return DatasetManifest(
        manifest_format=MANIFEST_FORMAT,
        semantic_hash=semantic_hash,
        physical_hash=physical_hash,
        schema_version=schema_version,
        record_type=record_type,
        contract=contract,
        row_count=row_count,
        min_timestamp=min_timestamp,
        max_timestamp=max_timestamp,
        source=source,
        transformation=transformation,
        manifest_hash=compute_manifest_hash(payload),
    )


def manifest_to_json_bytes(manifest: DatasetManifest) -> bytes:
    """Serialize a manifest for publication, hash included.

    The stored document is the canonical payload plus the manifest hash, so a
    reader can verify the file without reconstructing the model first.
    """
    document = dict(manifest.canonical_payload())
    document["manifest_hash"] = manifest.manifest_hash.canonical()
    return json.dumps(
        document, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False
    ).encode("ascii")


def manifest_from_json_bytes(payload: bytes) -> DatasetManifest:
    """Parse and fully re-verify a published manifest document.

    The stored hash is checked against a hash recomputed from the stored
    content, so a hand-edited manifest is rejected rather than believed.
    """
    from quant_research_terminal.catalog.provenance import (
        DatasetOrigin,
        TransformationKind,
    )

    try:
        document = json.loads(payload.decode("ascii"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ManifestHashMismatchError(f"manifest document is unreadable: {error}") from error
    if not isinstance(document, dict):
        raise ManifestHashMismatchError("manifest document must be a JSON object")

    try:
        stored_hash = str(document["manifest_hash"])
        source_payload = document["source"]
        transformation_payload = document["transformation"]
        source = SourceProvenance(
            origin=DatasetOrigin(source_payload["origin"]),
            provider_token=source_payload["provider_token"],
            source_artifact_hash=(
                None
                if source_payload["source_artifact_hash"] is None
                else PhysicalArtifactHash(value=source_payload["source_artifact_hash"])
            ),
            vendor_symbols=tuple(source_payload["vendor_symbols"]),
        )
        transformation = TransformationProvenance(
            kind=TransformationKind(transformation_payload["kind"]),
            symbol_mapping=tuple(
                (symbol, FuturesContractId.parse(canonical))
                for symbol, canonical in transformation_payload["symbol_mapping"]
            ),
            source_schema_version=transformation_payload["source_schema_version"],
        )
        manifest = DatasetManifest(
            manifest_format=str(document["format"]),
            semantic_hash=SemanticDatasetHash(value=document["semantic_hash"]),
            physical_hash=PhysicalArtifactHash(value=document["physical_hash"]),
            schema_version=int(document["schema_version"]),
            record_type=RecordType(document["record_type"]),
            contract=FuturesContractId.parse(document["contract"]),
            row_count=int(document["row_count"]),
            min_timestamp=_instant(document["min_timestamp"]),
            max_timestamp=_instant(document["max_timestamp"]),
            source=source,
            transformation=transformation,
            manifest_hash=ManifestHash(value=stored_hash),
        )
    except ManifestHashMismatchError:
        raise
    except (KeyError, TypeError, ValueError, OverflowError) as error:
        # ``OverflowError`` is an ``ArithmeticError``, not a ``ValueError`` — the
        # same trap the storage layer names explicitly. A corrupt document must
        # not escape this taxonomy just because its integer was absurd.
        raise ManifestHashMismatchError(f"manifest document is invalid: {error}") from error

    if manifest_to_json_bytes(manifest) != payload:
        raise ManifestHashMismatchError(
            "manifest document is not in canonical form; re-serializing it produces "
            "different bytes, so its stored hash cannot be trusted"
        )
    return manifest


def _instant(value: object) -> datetime | None:
    """Rebuild a UTC instant from the stored epoch-microsecond integer.

    An out-of-range count raises ``ValueError``. ``timedelta`` and ``datetime``
    signal that with ``OverflowError``, which is an ``ArithmeticError`` and so
    would slip past every ``except ValueError`` between here and the caller.
    """
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"a stored instant must be an integer microsecond count; got {value!r}")
    try:
        return _EPOCH + timedelta(microseconds=value)
    except (OverflowError, OSError) as error:
        raise ValueError(
            f"stored instant {value} is outside the representable range: {error}"
        ) from error
