from __future__ import annotations

from decimal import Decimal
from typing import Final

SCHEMA_NAME: Final[str] = "quant_research_terminal.storage"
SCHEMA_VERSION: Final[int] = 1
TIMESTAMP_TIMEZONE: Final[str] = "UTC"
PRICE_ENCODING: Final[str] = "fixed_scale_decimal"
PRICE_PRECISION: Final[int] = 18
PRICE_SCALE: Final[int] = 6
PRICE_QUANTUM: Final[Decimal] = Decimal("1e-6")

UINT64_MAX: Final[int] = 2**64 - 1
MAX_FIXED_POINT_VALUE: Final[int] = 10**PRICE_PRECISION - 1
