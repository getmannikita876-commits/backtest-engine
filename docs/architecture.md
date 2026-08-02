# Architecture

Phase 0 and Phase 1.2 use a small `src`-layout Python package. `app.py` owns application
lifecycle; `ui/main_window.py` owns the desktop shell. Each visible section is currently a
placeholder page with no engine or data-vendor behavior.

The domain package is intentionally independent of UI, storage, and data-vendor concerns.
Storage contracts convert domain models into deterministic Arrow/Polars-compatible rows with
explicit schema metadata and fixed-point decimal encoding. Dependencies must point inward
from storage and UI toward the domain layer. Market timestamps, exchange calendars, dataset
provenance, configuration snapshots, and random seeds must remain explicit before replay or
backtesting is implemented.

The UI must never become the source of research truth: experiments will eventually be
represented by serializable configurations and immutable result metadata so runs can be
repeated without the desktop interface.

