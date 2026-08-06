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
    "MAX_FIXED_POINT_VALUE",
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
SCHEMA_VERSION: Final[int] = 2
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
