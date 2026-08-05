\# Execution Model



\# Quant Research Terminal



\## Purpose



This document defines how orders are executed inside the Quant Research Terminal.



Execution realism is one of the highest priorities of the project.



The engine should never claim unrealistic fills.



Every assumption must be explicit.



\---



\# Fundamental Principle



The execution engine simulates what could reasonably happen on a real exchange.



It must never assume perfect execution.



\---



\# Supported Order Types



Initially:



\- Market

\- Limit

\- Stop

\- Stop Limit



Future:



\- Iceberg

\- TWAP

\- VWAP

\- Pegged

\- Bracket

\- OCO



\---



\# Market Orders



Market orders execute against available liquidity.



They do not guarantee the requested price.



Slippage may occur.



\---



\# Limit Orders



Limit orders execute only at the specified price or better.



No price improvement should be assumed unless explicitly modeled.



\---



\# Stop Orders



Stop orders become market orders after activation.



Trigger logic must be deterministic.



\---



\# Partial Fills



Partial fills are supported.



A single order may generate multiple execution events.



Every fill must preserve:



\- timestamp

\- executed quantity

\- executed price

\- remaining quantity



\---



\# Queue Priority



Execution priority should be deterministic.



Future implementations may support:



\- FIFO

\- Pro-Rata

\- Exchange-specific models



The chosen model must always be documented.



\---



\# Slippage



Slippage is configurable.



Examples:



\- Fixed ticks

\- Percentage

\- Volatility-based

\- Liquidity-based



Never hide slippage assumptions.



\---



\# Latency



Latency must be modeled explicitly.



Possible components:



\- strategy latency

\- network latency

\- exchange latency



Random latency requires a reproducible seed.



\---



\# Commission



Commission model is configurable.



Possible models:



\- Fixed

\- Per contract

\- Per share

\- Exchange fee

\- Broker fee



\---



\# Rejections



Orders may be rejected.



Possible reasons:



\- insufficient capital

\- invalid quantity

\- invalid price

\- exchange restrictions



Rejections must be deterministic.



\---



\# Position Management



Execution updates:



\- cash

\- realized PnL

\- unrealized PnL

\- average price

\- position size



\---



\# Portfolio Synchronization



Portfolio state must remain internally consistent after every execution.



\---



\# Risk Controls



Future execution engine should support:



\- maximum position size

\- daily loss limits

\- exposure limits

\- margin validation

\- leverage validation



\---



\# Event Flow



Typical order lifecycle:



Signal



↓



Order Created



↓



Submitted



↓



Accepted



↓



Queued



↓



Executed



↓



Portfolio Updated



↓



Strategy Notified



\---



\# Determinism



Execution must always produce identical results for identical replay input.



\---



\# Validation



Every execution feature requires:



\- unit tests

\- regression tests

\- replay tests



\---



\# Acceptance Criteria



Execution model is considered correct when it is:



✓ deterministic



✓ reproducible



✓ configurable



✓ documented



✓ realistic

