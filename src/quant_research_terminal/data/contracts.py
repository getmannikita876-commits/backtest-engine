from __future__ import annotations

from decimal import Decimal
from typing import Final

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
PRICE_PRECISION: Final[int] = 18
PRICE_SCALE: Final[int] = 6
PRICE_QUANTUM: Final[Decimal] = Decimal("1e-6")

UINT64_MAX: Final[int] = 2**64 - 1
MAX_FIXED_POINT_VALUE: Final[int] = 10**PRICE_PRECISION - 1
