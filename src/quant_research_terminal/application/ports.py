"""The application layer's boundary contracts.

This module declares — in one reviewable place — exactly which lower-layer
interfaces the application layer is allowed to touch. It defines no new
abstraction: every name here already exists and is re-exported so that use
cases import their boundary from ``application.ports`` and the dependency
surface stays visible and enforceable.

Inbound data source
-------------------
Data enters through :class:`MarketDataProvider`, the existing structural
protocol. The application accepts any implementation of it and never imports a
concrete vendor module; the architecture tests enforce that. The protocol,
request, and stream types live in ``data_import.providers.provider``, which is
the vendor-neutral *interface* module of the providers package — importing it
pulls in no vendor code.

Storage boundary — a deliberate non-port
----------------------------------------
The use case calls the concrete public Parquet functions
(``write_trades``/``read_trades`` and their quote and bar counterparts)
directly, dispatched explicitly by record type. No storage protocol is
declared, on purpose: there is exactly one storage backend, the integration
tests are required to exercise the real one, and a port with a single
implementation and no substitution use is an unused abstraction. If a second
backend ever exists (DuckDB is a candidate in later phases), the port should
be introduced *then*, shaped by the real second implementation rather than
guessed now. Recorded in ADR-007.
"""

from __future__ import annotations

from quant_research_terminal.data_import.providers.provider import (
    MarketDataProvider,
    ProviderRequest,
    RecordStream,
)

__all__ = [
    "MarketDataProvider",
    "ProviderRequest",
    "RecordStream",
]
