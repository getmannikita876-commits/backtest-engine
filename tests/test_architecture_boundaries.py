"""Enforces the layering rules from PROJECT_RULES.md as executable tests.

Dependency direction is an architectural invariant, and an invariant that is
only written down erodes quietly: one convenient import at a time, each
defensible on its own. Parsing the import graph turns the rule into something
CI can fail on.

Allowed direction::

    Providers -> Validation -> Normalization -> Domain -> Storage

The checks read import statements statically rather than importing the modules,
so a violation is reported as a boundary breach rather than surfacing later as
a circular-import error.
"""

from __future__ import annotations

import ast
from pathlib import Path

import quant_research_terminal

PACKAGE_ROOT = Path(quant_research_terminal.__file__).parent
ROOT_MODULE = "quant_research_terminal"

STORAGE = f"{ROOT_MODULE}.data"
DOMAIN = f"{ROOT_MODULE}.domain"
UI = f"{ROOT_MODULE}.ui"
DATA_IMPORT = f"{ROOT_MODULE}.data_import"
PROVIDERS = f"{DATA_IMPORT}.providers"
APPLICATION = f"{ROOT_MODULE}.application"

#: The one vendor-neutral module inside the providers package. The application
#: layer may import the provider *interface* from it; every other module under
#: ``providers`` is a concrete implementation the application must never touch.
PROVIDER_INTERFACE = f"{PROVIDERS}.provider"


def _module_name(path: Path) -> str:
    relative = path.relative_to(PACKAGE_ROOT).with_suffix("")
    parts = [part for part in relative.parts if part != "__init__"]
    return ".".join([ROOT_MODULE, *parts])


def _resolve_relative(module: str, node: ast.ImportFrom) -> str:
    """Return the absolute package a relative ``from . import x`` refers to.

    ``from .bar import Bar`` inside ``quant_research_terminal.domain.__init__``
    means ``quant_research_terminal.domain.bar``. Resolving it matters: the
    checks below match on absolute prefixes, so an unresolved relative import is
    an invisible import, and a boundary breach written as ``from ..data import
    x`` would pass every test in this file while violating the rule it claims to
    enforce.

    ``node.level`` counts leading dots. One dot means the module's own package,
    so ``level`` parts are dropped from the *package* path — which for a package
    ``__init__`` is the module name itself, since ``_module_name`` already
    strips ``__init__``.
    """
    parts = module.split(".")
    package = parts if _is_package_init(module) else parts[:-1]
    trimmed = package[: len(package) - (node.level - 1)] if node.level > 1 else package
    return ".".join([*trimmed, node.module]) if node.module else ".".join(trimmed)


def _is_package_init(module: str) -> bool:
    """Return whether ``module`` names a package rather than a plain module."""
    relative = Path(*module.split(".")[1:])
    return (PACKAGE_ROOT / relative / "__init__.py").is_file() or module == ROOT_MODULE


def _imports(path: Path, module: str) -> set[str]:
    """Return every module name imported by the file at ``path``.

    Both absolute and relative ``from`` imports are reported, as absolute
    names.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    found: set[str] = set()

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            base = node.module if node.level == 0 else _resolve_relative(module, node)
            if not base:
                continue
            found.add(base)
            found.update(f"{base}.{alias.name}" for alias in node.names)

    return found


def _modules_under(prefix: str) -> list[tuple[str, set[str]]]:
    """Return ``(module_name, imported_names)`` for every module under ``prefix``."""
    return [
        (name, _imports(path, name))
        for path in sorted(PACKAGE_ROOT.rglob("*.py"))
        if (name := _module_name(path)) == prefix or name.startswith(f"{prefix}.")
    ]


def _violations(*, source_prefix: str, forbidden_prefix: str) -> list[str]:
    return [
        f"{module} imports {imported_name}"
        for module, imported in _modules_under(source_prefix)
        for imported_name in sorted(imported)
        if imported_name == forbidden_prefix or imported_name.startswith(f"{forbidden_prefix}.")
    ]


def test_the_package_is_actually_being_scanned() -> None:
    # Guards the tests below from silently passing on an empty module list.
    assert len(_modules_under(DATA_IMPORT)) >= 8
    assert len(_modules_under(PROVIDERS)) >= 4


def test_storage_does_not_import_data_import() -> None:
    assert _violations(source_prefix=STORAGE, forbidden_prefix=DATA_IMPORT) == []


def test_domain_does_not_import_data_import() -> None:
    assert _violations(source_prefix=DOMAIN, forbidden_prefix=DATA_IMPORT) == []


def test_domain_does_not_import_storage() -> None:
    # The domain layer is the foundation: it depends on nothing above it.
    assert _violations(source_prefix=DOMAIN, forbidden_prefix=STORAGE) == []


def test_ui_does_not_import_data_import() -> None:
    assert _violations(source_prefix=UI, forbidden_prefix=DATA_IMPORT) == []


def test_ui_does_not_import_storage() -> None:
    assert _violations(source_prefix=UI, forbidden_prefix=STORAGE) == []


def test_validation_does_not_import_providers() -> None:
    # RawRecord lives outside the providers package precisely so that
    # validation can consume provider output without importing vendor code.
    assert _violations(source_prefix=f"{DATA_IMPORT}.validation", forbidden_prefix=PROVIDERS) == []


def test_normalization_does_not_import_providers() -> None:
    assert (
        _violations(source_prefix=f"{DATA_IMPORT}.normalization", forbidden_prefix=PROVIDERS) == []
    )


def test_raw_record_does_not_import_providers() -> None:
    assert _violations(source_prefix=f"{DATA_IMPORT}.raw_record", forbidden_prefix=PROVIDERS) == []


def test_event_order_does_not_import_providers() -> None:
    assert _violations(source_prefix=f"{DATA_IMPORT}.event_order", forbidden_prefix=PROVIDERS) == []


def test_orchestration_does_not_import_providers() -> None:
    # pipeline.py coordinates stages that are handed to it; binding it to a
    # concrete provider would make the batch API untestable without a vendor.
    assert _violations(source_prefix=f"{DATA_IMPORT}.pipeline", forbidden_prefix=PROVIDERS) == []


def test_providers_do_not_import_storage_directly() -> None:
    # Provider output is storage-independent; anything a provider needs from
    # the storage contract reaches it through the import contracts instead.
    assert _violations(source_prefix=PROVIDERS, forbidden_prefix=STORAGE) == []


def test_rule_modules_do_not_import_orchestration() -> None:
    # Rules must not depend on the layer that sequences them, or the two
    # become impossible to test apart.
    for module in ("validation", "normalization", "raw_record", "record_fields", "event_order"):
        assert (
            _violations(
                source_prefix=f"{DATA_IMPORT}.{module}",
                forbidden_prefix=f"{DATA_IMPORT}.pipeline",
            )
            == []
        )


def test_semantics_modules_depend_on_nothing_in_the_import_layer() -> None:
    # time_semantics, numeric_semantics, and instrument_semantics are the
    # authoritative leaf rules; they must stay free of every stage that
    # consumes them.
    for module in ("time_semantics", "numeric_semantics", "instrument_semantics"):
        _, imported = _modules_under(f"{DATA_IMPORT}.{module}")[0]
        internal = {name for name in imported if name.startswith(f"{DATA_IMPORT}.")}
        assert internal == set()


def test_relative_imports_are_resolved_by_the_boundary_checks() -> None:
    """The checks must see relative imports, or they can be evaded by syntax.

    ``domain/__init__.py`` imports its siblings relatively. If the import graph
    were built from absolute ``from`` statements only, those edges would be
    invisible and ``from ..data import x`` in that file would breach the
    domain-does-not-import-storage rule while every test here still passed.
    """
    domain_init = dict(_modules_under(DOMAIN))[DOMAIN]

    assert f"{DOMAIN}.futures_contract" in domain_init
    assert f"{DOMAIN}.futures_contract.FuturesContractId" in domain_init


# --------------------------------------------------------------------------
# Futures identity (Phase 2.0, ADR-009)
#
# Identity belongs at the lowest layer. It must be usable by replay,
# execution, and cataloguing later without any of them dragging in providers,
# import infrastructure, or storage.
# --------------------------------------------------------------------------

IDENTITY_MODULES = (f"{DOMAIN}.futures_contract", f"{DOMAIN}.contract_month")


def test_identity_modules_exist_and_are_scanned() -> None:
    for module in IDENTITY_MODULES:
        assert _modules_under(module), f"{module} was not scanned"


def test_identity_modules_depend_on_nothing_above_the_domain() -> None:
    # Enumerated rather than expressed as "not storage": the point is that
    # identity depends on *nothing* upward, so a layer added later is covered
    # without this test being remembered.
    for module in IDENTITY_MODULES:
        _, imported = _modules_under(module)[0]
        outward = {
            name
            for name in imported
            if name.startswith(f"{ROOT_MODULE}.") and not name.startswith(f"{DOMAIN}.")
        }
        assert outward == set(), f"{module} depends outward on {sorted(outward)}"


def test_identity_modules_do_not_read_the_wall_clock() -> None:
    """No current date, time, or timezone may influence an identity.

    A parser that resolves ``ESM6`` using the current decade produces a
    different identity in 2026 than in 2036 for the same input, which makes
    stored research irreproducible. The prohibition is structural: the modules
    do not import the machinery at all.
    """
    forbidden = {"datetime", "time", "calendar", "zoneinfo", "random", "uuid", "os", "locale"}

    for module in IDENTITY_MODULES:
        _, imported = _modules_under(module)[0]
        roots = {name.split(".")[0] for name in imported}
        assert roots & forbidden == set(), f"{module} imports {sorted(roots & forbidden)}"


# --------------------------------------------------------------------------
# Application layer (Phase 1.9, ADR-007)
#
# Application orchestrates downward — data_import, storage, domain — and is
# depended on only from above (UI, later). Nothing below it may know it
# exists, and it may not reach into vendor code or the UI.
# --------------------------------------------------------------------------


def test_the_application_package_is_actually_being_scanned() -> None:
    assert len(_modules_under(APPLICATION)) >= 3


def test_application_does_not_import_ui() -> None:
    assert _violations(source_prefix=APPLICATION, forbidden_prefix=UI) == []


def test_domain_does_not_import_application() -> None:
    assert _violations(source_prefix=DOMAIN, forbidden_prefix=APPLICATION) == []


def test_storage_does_not_import_application() -> None:
    assert _violations(source_prefix=STORAGE, forbidden_prefix=APPLICATION) == []


def test_data_import_does_not_import_application() -> None:
    # Covers the providers subpackage too: nothing below the application
    # layer may depend on it, or the dependency direction inverts.
    assert _violations(source_prefix=DATA_IMPORT, forbidden_prefix=APPLICATION) == []


# --------------------------------------------------------------------------
# Exchange calendars (Phase 2.1, ADR-010)
#
# Calendar semantics live in the domain; calendar *data* (TOML definitions and
# their loader) lives in the `calendars` package, which depends on the domain
# and on nothing else in the project. The domain must never depend back on the
# data package, or facts and semantics become circular.
# --------------------------------------------------------------------------

CALENDARS = f"{ROOT_MODULE}.calendars"

CALENDAR_DOMAIN_MODULES = (
    f"{DOMAIN}.exchange_calendar",
    f"{DOMAIN}.calendar_definition",
    f"{DOMAIN}.calendar_materialization",
)


def test_calendar_modules_exist_and_are_scanned() -> None:
    for module in (*CALENDAR_DOMAIN_MODULES, CALENDARS):
        assert _modules_under(module), f"{module} was not scanned"


def test_domain_does_not_import_the_calendars_data_package() -> None:
    assert _violations(source_prefix=DOMAIN, forbidden_prefix=CALENDARS) == []


def test_calendars_package_depends_only_on_the_domain() -> None:
    for module, imported in _modules_under(CALENDARS):
        outward = {
            name
            for name in imported
            if name.startswith(f"{ROOT_MODULE}.")
            and not name.startswith(f"{DOMAIN}.")
            and name != CALENDARS
            and not name.startswith(f"{CALENDARS}.")
        }
        assert outward == set(), f"{module} depends outward on {sorted(outward)}"


#: Phase 2.2 rollover modules (ADR-011). Listed for the canary below; the
#: sweep itself is token-based so a module added later is covered by default.
ROLLOVER_DOMAIN_MODULES = (
    f"{DOMAIN}.rollover",
    f"{DOMAIN}.rollover_definition",
    f"{DOMAIN}.rollover_materialization",
    f"{DOMAIN}.contract_lifecycle",
    f"{DOMAIN}.continuous_series",
)

#: Name tokens marking a module whose subject is time or instrument lifetime.
#: Every such module is held to the no-ambient-state and no-wall-clock rules.
#: ``time`` is in the list because ``domain/time.py`` owns the canonical UTC
#: validation *and* ``epoch_microseconds``, the instant encoding both content
#: hashes are built from. It is the single most wall-clock-sensitive module in
#: the package, and it matched none of the subject tokens.
_TIME_SENSITIVE_NAME_TOKENS = ("calendar", "rollover", "lifecycle", "continuous", "time")


def _calendar_modules() -> list[tuple[str, set[str]]]:
    """Every time-sensitive module, discovered by sweep rather than enumeration.

    A hard-coded module list covers exactly the files that exist on the day it
    is written; a `calendars/registry.py` or a new `domain/roll_*.py` added
    later would escape it. The sweep takes the whole `calendars` package plus
    every domain module whose name mentions one of the time-sensitive subjects,
    so additions are covered by default.
    """
    found = _modules_under(CALENDARS)
    for module, imported in _modules_under(DOMAIN):
        basename = module.rsplit(".", 1)[-1]
        if any(token in basename for token in _TIME_SENSITIVE_NAME_TOKENS):
            found.append((module, imported))
    return found


def test_the_calendar_sweep_finds_the_known_modules() -> None:
    names = {module for module, _ in _calendar_modules()}
    for module in (*CALENDAR_DOMAIN_MODULES, CALENDARS, f"{CALENDARS}.loader"):
        assert module in names, f"{module} escaped the calendar sweep"


def test_the_sweep_covers_the_shared_time_module() -> None:
    """``domain/time.py`` owns the instant encoding both content hashes use.

    It matched none of the subject tokens, so the module most in need of the
    no-wall-clock guarantee was the one module not covered by it.
    """
    names = {module for module, _ in _calendar_modules()}
    assert f"{DOMAIN}.time" in names


def test_the_sweep_also_finds_the_rollover_modules() -> None:
    """Regression for the sweep's own blind spot.

    None of the Phase 2.2 module names contain "calendar", so the Phase 2.1
    sweep would have covered none of them — the ambient-state ban and the
    wall-clock scan would have silently stopped applying to new code. That is
    ADR-010's defect D9 re-opening, and this canary is what keeps it shut.
    """
    names = {module for module, _ in _calendar_modules()}
    for module in ROLLOVER_DOMAIN_MODULES:
        assert module in names, f"{module} escaped the time-sensitive module sweep"


def test_rollover_modules_depend_on_nothing_above_the_domain() -> None:
    for module in ROLLOVER_DOMAIN_MODULES:
        _, imported = _modules_under(module)[0]
        outward = {
            name
            for name in imported
            if name.startswith(f"{ROOT_MODULE}.") and not name.startswith(f"{DOMAIN}.")
        }
        assert outward == set(), f"{module} depends outward on {sorted(outward)}"


def test_rollover_modules_do_not_import_the_calendars_data_package() -> None:
    # Rollover consumes the calendar *domain* types; the TOML definitions and
    # their loader are data the caller supplies, not something rollover reaches
    # for. Importing them would make a domain rule depend on shipped facts.
    for module in ROLLOVER_DOMAIN_MODULES:
        assert _violations(source_prefix=module, forbidden_prefix=CALENDARS) == []


def test_calendar_modules_do_not_import_ambient_state_machinery() -> None:
    # The calendar legitimately imports datetime and zoneinfo — it is the one
    # domain area whose subject *is* time — so the identity-module blanket ban
    # does not apply. What stays banned is machinery for ambient, run-varying
    # state: randomness, process identity, locale, and environment access.
    forbidden = {"random", "uuid", "os", "locale", "time"}
    for module, imported in _calendar_modules():
        roots = {name.split(".")[0] for name in imported}
        assert roots & forbidden == set(), f"{module} imports {sorted(roots & forbidden)}"


def test_calendar_modules_never_read_the_wall_clock() -> None:
    """`datetime.now`, `datetime.today`, `date.today`, and `datetime.utcnow`
    must not appear in any calendar module: a calendar answer that depends on
    when it was computed is irreproducible by construction. This is a source
    scan rather than an import check because the `datetime` module itself is
    legitimately imported."""
    banned_calls = {"now", "today", "utcnow", "fromtimestamp"}
    covered = {module for module, _ in _calendar_modules()}
    for path in sorted(PACKAGE_ROOT.rglob("*.py")):
        module = _module_name(path)
        if module not in covered:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr in banned_calls
            ):
                raise AssertionError(
                    f"{module} calls .{node.func.attr}() at line {node.lineno}; "
                    f"calendar code must never read the wall clock"
                )


def test_application_does_not_import_provider_implementations() -> None:
    # The application accepts any MarketDataProvider through the interface
    # module and must never bind to a vendor. Everything under ``providers``
    # except the interface module itself is a concrete implementation.
    violations = [
        violation
        for violation in _violations(source_prefix=APPLICATION, forbidden_prefix=PROVIDERS)
        if not violation.endswith(f"imports {PROVIDER_INTERFACE}")
        and f"imports {PROVIDER_INTERFACE}." not in violation
    ]
    assert violations == []
