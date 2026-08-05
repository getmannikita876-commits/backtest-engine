\# Replay Rules



\# Quant Research Terminal



\## Purpose



This document defines the deterministic replay rules used throughout the Quant Research Terminal.



Every replay implementation must comply with these rules.



Replay correctness has higher priority than replay performance.



\---



\# Fundamental Principle



The replay engine must always produce identical results when given identical inputs.



Replay must never depend on:



\- CPU speed

\- machine

\- operating system

\- execution timing

\- thread scheduling



Replay should be fully deterministic.



\---



\# Determinism



Identical:



\- market data

\- configuration

\- random seed

\- strategy version



must always produce identical:



\- event ordering

\- fills

\- portfolio state

\- statistics

\- reports



\---



\# Event Ordering



Every event must have deterministic ordering.



Sorting priority:



1\. Timestamp (UTC)



2\. Event Type Priority



3\. Sequence Number



Never depend on:



\- insertion order

\- dictionary order

\- filesystem order



\---



\# Time



UTC only.



Never use:



\- local timezone

\- daylight saving adjustments

\- machine clock



Historical timestamps are immutable.



\---



\# Replay Clock



Replay owns the clock.



Strategies must never ask the operating system for current time.



The replay engine is the only time authority.



\---



\# Look-Ahead Bias



Look-ahead bias is forbidden.



Future information may never influence:



\- indicators



\- entries



\- exits



\- filters



\- execution



Examples:



❌ next bar



❌ future quote



❌ future volatility



❌ future option Greeks



❌ future fill



\---



\# Bar Replay



Bars become visible only after they close.



Never expose incomplete historical bars unless explicitly replaying live bar construction.



\---



\# Tick Replay



Ticks must preserve exchange ordering.



No reordering.



No batching that changes semantics.



\---



\# Quote Replay



Quotes preserve:



\- bid



\- ask



\- timestamp



\- sequence



\---



\# Trade Replay



Trades preserve:



\- timestamp



\- price



\- quantity



\- aggressor



when available.



\---



\# Event Queue



Replay processes one event at a time.



Processing order must be stable.



\---



\# Randomness



Random behavior is prohibited unless:



\- explicitly enabled



and



\- initialized using a reproducible seed.



\---



\# Latency



Latency simulation must be deterministic.



Never sample random latency without a reproducible seed.



\---



\# Replay Validation



Replay correctness should be verified through regression tests.



Every replay bug must introduce a regression test.



\---



\# Reproducibility



Replay results should remain reproducible across:



\- Windows



\- Linux



\- macOS



where supported.



\---



\# Logging



Replay logs should contain sufficient information to reproduce failures.



\---



\# Performance



Performance improvements must never change replay semantics.



Correctness always wins.



\---



\# Acceptance Criteria



A replay implementation is considered correct only if:



✓ deterministic



✓ reproducible



✓ testable



✓ documented



✓ free from look-ahead bias

