"""Structured provenance: where a dataset came from and what was done to it.

Provenance answers questions the semantic hash deliberately cannot. Two
artifacts holding byte-identical canonical records have the **same** semantic
hash whichever vendor supplied them — that is the point — so "which provider
was this?" has to live somewhere else. It lives here, and it reaches the
manifest hash but never the semantic one.

What is deliberately absent
---------------------------
No path, no filename, no file size, no modification time, no wall clock, no
machine name, no user, no library version, and no free-form notes.

The path exclusion is not a detail. The manifest promises that moving an
artifact changes no identity, and a manifest carrying a source filename would
break that promise the first time someone reorganised a directory. If a
human-readable source label is genuinely wanted, ``provider_token`` is an
operator-declared opaque label in the spirit of ``Venue.code`` — not a
filesystem location, and never resolved as one.

Free-form notes are excluded outright rather than excluded-from-the-hash,
because a text field on an immutable model invites exactly one question — does
editing it change identity? — and the cheapest way to answer it correctly is
for the field not to exist.
"""

from __future__ import annotations

import re
from enum import StrEnum, unique
from typing import Final, final

from pydantic import field_validator, model_validator

from quant_research_terminal.domain.common import CanonicalModel
from quant_research_terminal.domain.dataset_identity import PhysicalArtifactHash
from quant_research_terminal.domain.futures_contract import (
    FuturesContractId,
    require_listed_contract,
)

#: An operator-declared source label: upper-case ASCII, digits, underscore.
#:
#: Deliberately not a Python class name, module path, or ``__qualname__``. Those
#: are implementation details that change under refactoring, and a dataset's
#: recorded origin must not move because someone renamed a class.
_PROVIDER_TOKEN_PATTERN: Final[re.Pattern[str]] = re.compile(r"[A-Z0-9_]{1,32}")


@unique
class DatasetOrigin(StrEnum):
    """How a canonical artifact came to exist."""

    #: Decoded from a vendor export through the import pipeline.
    IMPORTED = "imported"
    #: Produced by migrating a version-2 artifact with an explicit mapping.
    MIGRATED_FROM_V2 = "migrated-from-v2"


@unique
class TransformationKind(StrEnum):
    """What was done to produce this artifact from its source."""

    #: No transformation beyond ordinary decoding and normalization.
    NONE = "none"
    #: Vendor aliases resolved to canonical identity by an explicit mapping.
    SYMBOL_MAPPING = "symbol-mapping"


@final
class SourceProvenance(CanonicalModel):
    """Where a dataset's records came from.

    ``vendor_symbols`` is a **sorted tuple of the distinct aliases** actually
    present in the artifact, not a single scalar. Several vendor spellings can
    legitimately map to one contract — that is the main case the migration
    exists to serve — so a single alias field would either be a lie or force an
    artificial one-alias-per-artifact rule. It is sorted so that the value
    cannot depend on row order or set iteration, and it is recomputed from the
    artifact during verification rather than trusted.
    """

    origin: DatasetOrigin
    provider_token: str | None
    source_artifact_hash: PhysicalArtifactHash | None
    vendor_symbols: tuple[str, ...]

    @field_validator("provider_token")
    @classmethod
    def _validate_provider_token(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if _PROVIDER_TOKEN_PATTERN.fullmatch(value) is None:
            raise ValueError(
                f"provider token must be 1-32 characters of upper-case ASCII letters, "
                f"digits, and underscores; got {value!r}. It is an operator-declared "
                f"label, never a path and never a Python class name"
            )
        return value

    @field_validator("vendor_symbols")
    @classmethod
    def _validate_vendor_symbols(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        for symbol in value:
            if not isinstance(symbol, str) or not symbol.strip():
                raise ValueError(f"vendor symbol must be a non-empty string; got {symbol!r}")
        if list(value) != sorted(set(value)):
            raise ValueError(
                f"vendor symbols must be unique and sorted so the summary cannot depend on "
                f"row order or set iteration; got {list(value)}"
            )
        return value

    def canonical_payload(self) -> dict[str, object]:
        """Return the deterministic payload this provenance contributes."""
        return {
            "origin": self.origin.value,
            "provider_token": self.provider_token,
            "source_artifact_hash": (
                None if self.source_artifact_hash is None else self.source_artifact_hash.canonical()
            ),
            "vendor_symbols": list(self.vendor_symbols),
        }


@final
class TransformationProvenance(CanonicalModel):
    """What was done to the source to produce this artifact.

    For a migration this pins the **entire declared mapping**, not just the
    entries that happened to be used. Two declarations that agree on this file
    may disagree on the next one, so they are different transformations even
    when they produce byte-identical output — which is the cleanest available
    demonstration that semantic, physical, and manifest identity are three
    separate things.
    """

    kind: TransformationKind
    symbol_mapping: tuple[tuple[str, FuturesContractId], ...]
    source_schema_version: int | None

    @field_validator("symbol_mapping")
    @classmethod
    def _validate_mapping(
        cls, value: tuple[tuple[str, FuturesContractId], ...]
    ) -> tuple[tuple[str, FuturesContractId], ...]:
        symbols = [symbol for symbol, _ in value]
        if list(symbols) != sorted(symbols):
            raise ValueError(
                f"symbol mapping must be sorted by vendor symbol so its serialization "
                f"cannot depend on dict insertion order; got {symbols}"
            )
        if len(set(symbols)) != len(symbols):
            raise ValueError("symbol mapping must not repeat a vendor symbol")
        for symbol, contract in value:
            if not isinstance(symbol, str) or not symbol.strip():
                raise ValueError(f"vendor symbol must be a non-empty string; got {symbol!r}")
            require_listed_contract(contract)
        return value

    @model_validator(mode="after")
    def _validate_kind(self) -> TransformationProvenance:
        if self.kind is TransformationKind.NONE and self.symbol_mapping:
            raise ValueError("an untransformed dataset carries no symbol mapping")
        # A ``SYMBOL_MAPPING`` transformation with an *empty* mapping is
        # deliberately allowed. Migrating an empty version-2 artifact is a real
        # migration — the artifact's origin and schema version both changed —
        # and its mapping is legitimately empty because there were no aliases
        # to resolve. Requiring a non-empty mapping here made that case
        # unrepresentable, which surfaced as a failure the first time an empty
        # source was migrated. The invariant that matters runs the other way:
        # an *untransformed* dataset must not claim a mapping.
        return self

    def canonical_payload(self) -> dict[str, object]:
        """Return the deterministic payload this provenance contributes.

        The mapping serializes as a **list of pairs**, not an object, so the
        ordering is explicit in the structure rather than inherited from a
        JSON serializer's key sorting.
        """
        return {
            "kind": self.kind.value,
            "symbol_mapping": [
                [symbol, contract.canonical()] for symbol, contract in self.symbol_mapping
            ],
            "source_schema_version": self.source_schema_version,
        }


def untransformed(
    *,
    origin: DatasetOrigin = DatasetOrigin.IMPORTED,
    provider_token: str | None = None,
    source_artifact_hash: PhysicalArtifactHash | None = None,
    vendor_symbols: tuple[str, ...] = (),
) -> tuple[SourceProvenance, TransformationProvenance]:
    """Return provenance for a dataset produced without a mapping."""
    return (
        SourceProvenance(
            origin=origin,
            provider_token=provider_token,
            source_artifact_hash=source_artifact_hash,
            vendor_symbols=vendor_symbols,
        ),
        TransformationProvenance(
            kind=TransformationKind.NONE, symbol_mapping=(), source_schema_version=None
        ),
    )
