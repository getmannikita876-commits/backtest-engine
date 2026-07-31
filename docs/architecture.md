# Architecture

Phase 0 uses a small `src`-layout Python package. `app.py` owns application lifecycle;
`ui/main_window.py` owns the desktop shell. Each visible section is currently a placeholder
page with no domain or infrastructure behavior.

Future boundaries should separate domain models, application services, infrastructure,
and UI. Dependencies must point inward toward domain concepts. Market timestamps,
exchange calendars, dataset provenance, configuration snapshots, and random seeds must be
explicit before replay or backtesting is implemented.

The UI must never become the source of research truth: experiments will eventually be
represented by serializable configurations and immutable result metadata so runs can be
repeated without the desktop interface.

