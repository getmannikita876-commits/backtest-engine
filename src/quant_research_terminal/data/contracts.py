from __future__ import annotations

from typing import Final

from quant_research_terminal.domain.numeric import (
    MAX_PRICE_FIXED_POINT,
    MAX_QUANTITY_INTEGER,
    PRICE_PRECISION,
    PRICE_QUANTUM,
    PRICE_SCALE,
)

__all__ = [
    "CANONICAL_IDENTITY_METADATA_KEY",
    "LEGACY_SCHEMA_VERSION",
    "MAX_FIXED_POINT_VALUE",
    "RECORD_TYPE_METADATA_KEY",
    "SUPPORTED_SCHEMA_VERSIONS",
    "PRICE_ENCODING",
    "PRICE_PRECISION",
    "PRICE_QUANTUM",
    "PRICE_SCALE",
    "SCHEMA_NAME",
    "SCHEMA_VERSION",
    "TIMESTAMP_TIMEZONE",
    "UINT64_MAX",
]

SCHEMA_NAME: Final[str] = "quant_research_terminal.storage"

#: Storage schema version.
#:
#: Version 2 (Phase 1.5) added the bar interval column and constrained the
#: trade ``side`` column to the ``TradeSide`` vocabulary. Version 1 data is
#: rejected rather than read, because a version-1 bar records no interval and
#: its single timestamp cannot be resolved into interval start and
#: availability without one. See ``docs/data-contracts.md`` for migration.
SCHEMA_VERSION: Final[int] = 3

#: The schema version legacy artifacts carry, and which the legacy writers
#: continue to emit.
#:
#: Version 3 (Phase 2.3) replaced the single ``instrument_symbol`` column with
#: ``canonical_identity`` plus ``vendor_symbol``, so a stored dataset finally
#: names the listed contract it holds instead of a vendor alias. It is an
#: **additive** capability: the version-2 writers and readers are unchanged and
#: keep emitting and accepting version 2, because a version-3 artifact requires
#: a ``FuturesContractId`` that the import pipeline has no evidenced way to
#: derive. A version-2 artifact is never reinterpreted as version 3; converting
#: one requires the explicit mapping in ``catalog.migration``.
LEGACY_SCHEMA_VERSION: Final[int] = 2

#: The schema versions this build can read.
SUPPORTED_SCHEMA_VERSIONS: Final[frozenset[int]] = frozenset(
    {LEGACY_SCHEMA_VERSION, SCHEMA_VERSION}
)

#: Metadata keys a version-3 schema carries, and exactly those.
#:
#: Version 2 tolerates unknown extra keys and keeps doing so, because files
#: already exist under that rule. Version 3 requires an exact key set: an
#: unrecognised key there means either corruption or a writer this build does
#: not understand, and the version number exists precisely so that guessing is
#: unnecessary.
RECORD_TYPE_METADATA_KEY: Final[bytes] = b"record_type"
CANONICAL_IDENTITY_METADATA_KEY: Final[bytes] = b"canonical_identity"
TIMESTAMP_TIMEZONE: Final[str] = "UTC"
PRICE_ENCODING: Final[str] = "fixed_scale_decimal"

# PRICE_PRECISION, PRICE_SCALE, and PRICE_QUANTUM are re-exported from the
# domain package above. The numeric envelope is defined there because the
# domain may not depend on storage, so schema metadata and storage code read
# the bounds from one source rather than restating them.
#
# These two aliases preserve the storage-facing names for the same values.
UINT64_MAX: Final[int] = MAX_QUANTITY_INTEGER
MAX_FIXED_POINT_VALUE: Final[int] = MAX_PRICE_FIXED_POINT
