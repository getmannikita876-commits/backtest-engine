# ADR-001: Python package and thin PySide6 shell

- Status: Accepted
- Date: 2026-07-31

## Context

The project needs a runnable, testable foundation before quantitative domain behavior is
introduced. Desktop concerns must not dictate future backtest-engine architecture.

## Decision

Use Python 3.11+, a `src` package layout, PySide6 for the desktop shell, and Hatchling for
standards-based packaging. Keep the initial window intentionally thin and populate its
navigation with placeholder pages. Use Pytest, Ruff, and MyPy as quality gates.

## Consequences

The application can be installed and launched immediately, while later domain modules can
be introduced behind explicit boundaries. PySide6 is an installation dependency even for
UI smoke tests; headless CI must set `QT_QPA_PLATFORM=offscreen`.

