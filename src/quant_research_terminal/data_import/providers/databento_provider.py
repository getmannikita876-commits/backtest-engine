"""Interface-only Databento provider stub.

Phase 1.3 ships the *shape* of the Databento integration and none of its
behaviour. This module deliberately contains:

* no API keys, credentials, or configuration defaults that could hold one,
* no network calls,
* no dependency on a Databento SDK.

Its purpose is to prove that the vendor-neutral interfaces in
:mod:`quant_research_terminal.data_import.providers.provider` are sufficient to
describe a real commercial vendor before any vendor code is written. If a
future integration cannot be expressed through :class:`ProviderRequest` and
:class:`RawRecord`, that is a signal to revisit the interfaces rather than to
leak vendor concepts upward into the engine.

:meth:`DatabentoMarketDataProvider.fetch` raises
:class:`ProviderNotConfiguredError`. It never returns an empty iterator,
because "not implemented" and "no data in this window" must stay
distinguishable to callers.

Mapping notes for the future implementation
-------------------------------------------
Databento identifies data by dataset and schema (for example ``GLBX.MDP3``
with schema ``mbp-1`` or ``ohlcv-1m``) and returns nanosecond timestamps.
Translating those into this package's record types, and truncating nanosecond
timestamps to the microsecond precision fixed by the storage contract, is a
decision that must be made and documented when the integration is built —
truncation loses information and cannot be applied silently.
"""

from __future__ import annotations

from typing import Final

from quant_research_terminal.data_import.contracts import ImportRecordType
from quant_research_terminal.data_import.providers.provider import (
    ProviderCapabilities,
    ProviderNotConfiguredError,
    ProviderRequest,
    RecordStream,
)

PROVIDER_NAME: Final = "databento"

#: Record types the future integration is expected to serve. Declared now so
#: capability-driven callers can be written and tested against the stub.
SUPPORTED_RECORD_TYPES: Final[frozenset[ImportRecordType]] = frozenset(
    {ImportRecordType.TRADE, ImportRecordType.QUOTE, ImportRecordType.BAR}
)


class DatabentoMarketDataProvider:
    """Placeholder that satisfies :class:`MarketDataProvider` without behaviour."""

    @property
    def capabilities(self) -> ProviderCapabilities:
        """Return the declared, not-yet-implemented capability set."""
        return ProviderCapabilities(
            provider_name=PROVIDER_NAME,
            record_types=SUPPORTED_RECORD_TYPES,
            supports_time_window=True,
            requires_credentials=True,
            is_implemented=False,
        )

    def fetch(self, request: ProviderRequest) -> RecordStream:
        """Always raise: the Databento integration is not implemented.

        Raises:
            ProviderNotConfiguredError: unconditionally.
        """
        raise ProviderNotConfiguredError(
            f"provider {PROVIDER_NAME!r} is an interface-only stub in this phase; "
            f"cannot serve record type {request.record_type.value!r}"
        )
