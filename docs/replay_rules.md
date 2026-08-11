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



\*\*Amended by ADR-014 (Phase 3).\*\* The original priority list read \"1. Timestamp (UTC) 2. Event Type Priority 3. Sequence Number\". The implemented ordering is:



1\. \*\*Availability time (UTC)\*\* decides which ReplayFrame an observation belongs to. Every observation sharing an instant is delivered in one frame.



2\. \*\*Within a frame\*\*, events are stored by (source manifest digest, original row ordinal). That order is deterministic and \*\*explicitly non-causal\*\*: it exists for serialization, debugging, and test equality, and carries no claim about exchange sequence.



\*\*Event Type Priority does not exist and must not be added.\*\* The repository holds no evidence that a trade is knowable before a quote at the same persisted microsecond, so a priority table would be a fabricated causal order presented as a convention.



\*\*Sequence Number does not exist.\*\* Trades and quotes carry no persisted logical event identity (ADR-003), so there is no sequence to sort by. A row ordinal is provenance — a position in an immutable artifact — and is never presented as a market event id.



Never depend on:



\- insertion order

\- dictionary order

\- filesystem order

\- caller argument order

\- provider, vendor symbol, file path, or physical encoding



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



\*\*Amended by ADR-014 (Phase 3).\*\* There is no clock object to own.



Replay time \*is\* the ReplayFrame's availability time. A separate mutable clock would be a second, weaker copy of it, and wall-clock pacing would make a research run's output depend on CPU speed. No tick(), advance(), sleep(), or speed() exists.



The intent of the original rule is preserved and strengthened: consumers must never ask the operating system for the current time, and the frame's availability time is the only time authority.



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



\*\*Amended by ADR-014 (Phase 3).\*\* Replay processes one \*frame\* at a time.



A frame holds every observation that became available at one instant, and is processed atomically. Per-event delivery is precisely the look-ahead mechanism ADR-014 rejects: it would create a decision boundary between simultaneous observations that the data does not license.



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

