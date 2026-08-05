\# PROJECT\_RULES.md



\# Quant Research Terminal



\## Repository Development Rules



This document defines the engineering rules for the entire repository.



Every module must follow these rules.



\---



\# Primary Objective



Build a reproducible institutional-grade quantitative research platform.



Not a retail trading application.



Not a charting platform.



Not a discretionary trading tool.



\---



\# Repository Philosophy



Everything should be:



\- deterministic

\- reproducible

\- testable

\- modular

\- documented



\---



\# Module Independence



Every module should have one responsibility.



Avoid circular dependencies.



Preferred dependency direction:



UI

↓



Application



↓



Replay / Execution / Research



↓



Data Import



↓



Storage



↓



Domain



Never reverse this dependency flow.



\---



\# Domain Layer



The domain layer is the foundation.



Rules:



\- immutable models

\- validation only

\- no business logic

\- no IO

\- no GUI

\- no provider-specific code



\---



\# Storage Layer



Responsible only for storage contracts.



No strategy logic.



No replay logic.



No execution logic.



\---



\# Data Import Layer



Responsible for:



\- validation

\- ordering

\- duplicate detection

\- schema compatibility

\- conversion into domain objects



Never perform trading calculations.



\---



\# Replay Layer



Responsible only for deterministic event replay.



Must never:



\- predict

\- optimize

\- execute strategies



\---



\# Execution Layer



Responsible only for execution simulation.



Includes:



\- fills

\- commissions

\- latency

\- slippage

\- queue priority

\- execution assumptions



\---



\# Strategy Layer



Strategies must not know:



\- storage implementation

\- UI

\- provider implementation



Strategies receive only clean market events.



\---



\# UI Layer



UI must never contain business logic.



UI only:



\- displays data

\- starts operations

\- receives results



\---



\# Provider Independence



Never tightly couple code to:



\- Databento

\- ThetaData

\- Polygon

\- Interactive Brokers

\- any future provider



Always program against interfaces.



\---



\# Time Rules



UTC only.



Never convert timestamps automatically.



Reject invalid timestamps.



Never use local system time.



\---



\# Event Ordering



Event ordering must be deterministic.



Ordering should always be documented.



Never rely on:



\- dict ordering

\- hash ordering

\- filesystem ordering



\---



\# Error Handling



Never silently ignore errors.



Every important validation failure should produce:



\- machine-readable code

\- human-readable message



\---



\# Testing Rules



Every feature:



Unit tests.



Every bug:



Regression test.



Every critical subsystem:



Integration tests.



\---



\# Documentation Rules



Every architectural change:



Update documentation.



Every important decision:



Document rationale.



\---



\# CI Rules



Every Pull Request should pass:



\- Ruff format

\- Ruff lint

\- MyPy

\- Pytest



No failing CI may be merged.



\---



\# Pull Request Checklist



Before merging:



\- architecture preserved

\- tests pass

\- documentation updated

\- CI green

\- no unrelated changes

\- no duplicated logic



\---



\# Coding Principles



Prefer:



small modules



explicit APIs



dependency injection



immutable data



typed interfaces



Avoid:



global mutable state



magic constants



hidden behavior



implicit conversions



\---



\# Long-Term Goal



The repository should remain maintainable after:



\- 100,000+ lines of code

\- years of research

\- multiple contributors



Every implementation should support this goal.

