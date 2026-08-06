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

## Addendum (2026-08-06)

"Python 3.11+" above no longer describes the project. The supported version is
**Python 3.12 exactly** (`requires-python = ">=3.12,<3.13"` in
`pyproject.toml`): 3.11 was dropped without ever being exercised by CI, and
3.13 is excluded until it is explicitly tested. Every tool target — Ruff,
MyPy, the CI matrix — is aligned to 3.12; the alignment and the environment
reproducibility policy are recorded in ADR-008. The rest of this decision
(src layout, PySide6 shell, Hatchling, quality gates) stands unchanged.

