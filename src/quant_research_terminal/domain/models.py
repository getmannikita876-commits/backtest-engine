"""Backwards-compatible module re-export for domain models."""

from quant_research_terminal.domain.bar import Bar
from quant_research_terminal.domain.contract_month import ContractMonth
from quant_research_terminal.domain.futures_contract import (
    FuturesContractId,
    FuturesProduct,
    Venue,
    require_listed_contract,
    resolve_abbreviated_contract_year,
)
from quant_research_terminal.domain.instrument import Instrument
from quant_research_terminal.domain.quote import Quote
from quant_research_terminal.domain.trade import Trade
from quant_research_terminal.domain.trade_side import TradeSide, parse_trade_side

__all__ = [
    "Bar",
    "ContractMonth",
    "FuturesContractId",
    "FuturesProduct",
    "Instrument",
    "Quote",
    "Trade",
    "TradeSide",
    "Venue",
    "parse_trade_side",
    "require_listed_contract",
    "resolve_abbreviated_contract_year",
]
