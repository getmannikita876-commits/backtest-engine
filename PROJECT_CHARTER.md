# Quant Research Terminal — Project Charter

## Mission

Build a local Windows desktop utility for professional quantitative research and
reproducible backtesting of ES/NQ, with options support considered only in a later phase.

## Engineering priorities

1. Correct data and time semantics.
2. Prevention of look-ahead bias and leakage.
3. Deterministic replay.
4. Realistic and explicitly documented execution simulation.
5. Reproducible experiments.
6. Testability and modularity.
7. Performance work only after profiling.
8. User-interface depth only after engine correctness.

## Phase 0 scope

Phase 0 delivers a runnable Python package, project tooling, documentation, a minimal
PySide6 navigation shell, and smoke tests. It does not implement data vendors, strategies,
execution, options, Rust, an AI assistant, Monte Carlo analysis, or optimization.

## Initial technology direction

Python and PySide6 form the initial application foundation. Polars, NumPy, DuckDB,
PyArrow, Pydantic, and PyYAML are intended for later phases when their owning components
are designed. Pytest, Ruff, and MyPy provide the initial quality gates.

## Non-negotiable rules

- Every defect fix receives a regression test.
- Architectural changes require rationale and an ADR.
- Execution-model simplifications must be visible and documented.
- Secrets and API keys never enter source control.
- A tested Python implementation precedes any justified Rust migration.

> Recovery note: this charter was reconstructed from the available project context because
> the previously referenced complete charter was not present in the active environment.

