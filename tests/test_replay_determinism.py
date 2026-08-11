"""Determinism: the same pinned inputs produce the same stream, everywhere.

The property under attack is the phase's headline claim:

    the same exact ``ManifestHash`` input set, plus the same replay range,
    produces the same ``ReplayFrame`` stream — across repeated calls, fresh
    preparations, separate processes, hash seeds, storage layouts, later lineage
    claims, and a child process in which every clock read raises.

The comparisons run over a canonical digest of the stream rather than over model
``repr``. A repr is a formatting choice that can change without any semantic
change, so comparing two runs by it would test the formatter.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from dataset_fixtures import ES_M2026, NQ_M2026
from replay_fixtures import (
    at,
    bar_starting,
    frame_digest,
    publish_dataset,
    quote_at,
    trade_at,
)

from quant_research_terminal.catalog.lineage import publish_supersedes_relation
from quant_research_terminal.catalog.store import rebuild_index
from quant_research_terminal.domain.dataset_identity import ManifestHash, RecordType
from quant_research_terminal.domain.replay import ReplayConfig, ReplayRange
from quant_research_terminal.replay import prepare_replay

TESTS_DIRECTORY = Path(__file__).parent

#: Replays a prepared catalog and prints a canonical digest of the stream.
#:
#: Deliberately a *child* of a catalog the parent built: rebuilding the fixtures
#: in the child would compare two constructions rather than two replays, and the
#: claim under test is that identical stored inputs replay identically.
CHILD_SCRIPT = """
import json
import sys
from pathlib import Path

sys.path.insert(0, sys.argv[1])

from replay_fixtures import frame_digest

from quant_research_terminal.domain.dataset_identity import ManifestHash
from quant_research_terminal.domain.replay import ReplayConfig
from quant_research_terminal.replay import prepare_replay

catalog = Path(sys.argv[2])
storage = Path(sys.argv[3])
hashes = tuple(ManifestHash(value=digest) for digest in sys.argv[4:])

replay = prepare_replay(
    catalog_root=catalog,
    storage_root=storage,
    config=ReplayConfig(manifest_hashes=hashes, replay_range=None),
)
print(json.dumps(frame_digest(list(replay.frames())), sort_keys=True))
"""

#: Installs a process-wide clock denial. Shared by the child and its canary.
#:
#: Applied **after** imports and before any replay work, which is deliberate and
#: is the only workable placement. Denying before imports fails immediately for
#: an unrelated reason — the standard library's own ``logging`` module evaluates
#: ``time.time()`` at import, reached here through ``polars`` — so a pre-import
#: denial would test the import graph rather than replay. Import-time clock reads
#: *in replay's own modules* are covered instead by the AST sweep in
#: ``test_architecture_boundaries.py``, which is what that sweep is for.
#:
#: ``datetime.datetime`` is rebound on the module rather than before import so
#: that already-bound ``from datetime import datetime`` references keep the real
#: class. Rebinding it earlier would break every ``isinstance`` check in the
#: domain — a real datetime is not an instance of a subclass — and the run would
#: fail for a reason that has nothing to do with clocks.
CLOCK_DENIAL = """
import datetime as _datetime
import time as _time


def _forbidden(*args, **kwargs):
    raise AssertionError("replay read the wall clock")


class _NoClockDatetime(_datetime.datetime):
    now = classmethod(_forbidden)
    utcnow = classmethod(_forbidden)
    today = classmethod(_forbidden)
    fromtimestamp = classmethod(_forbidden)
    utcfromtimestamp = classmethod(_forbidden)


_datetime.datetime = _NoClockDatetime
_time.time = _forbidden
_time.time_ns = _forbidden
_time.monotonic = _forbidden
_time.perf_counter = _forbidden
_time.localtime = _forbidden
_time.gmtime = _forbidden
_time.sleep = _forbidden
"""

#: The same replay, in a process where every clock read raises.
#:
#: Written because the timezone parametrisation below proves nothing on this
#: project's primary platform: ``TZ`` does not affect the C runtime's notion of
#: local time on Windows, and ``time.tzset`` does not exist there, so all three
#: parametrised runs execute in one effective zone. A test that cannot fail is
#: worse than no test, and Phase 2.4 already recorded one probe that matched
#: docstrings rather than code.
#:
#: Denying the clock is strictly stronger than varying it, and platform
#: independent: it does not ask whether replay reads a *different* time, it
#: asserts that replay reads no time at all.
CLOCK_DENIED_SCRIPT = f"""
import json
import sys
from pathlib import Path

sys.path.insert(0, sys.argv[1])

from replay_fixtures import frame_digest

from quant_research_terminal.domain.dataset_identity import ManifestHash
from quant_research_terminal.domain.replay import ReplayConfig
from quant_research_terminal.replay import prepare_replay

catalog = Path(sys.argv[2])
storage = Path(sys.argv[3])
hashes = tuple(ManifestHash(value=digest) for digest in sys.argv[4:])

{CLOCK_DENIAL}

replay = prepare_replay(
    catalog_root=catalog,
    storage_root=storage,
    config=ReplayConfig(manifest_hashes=hashes, replay_range=None),
)
print(json.dumps(frame_digest(list(replay.frames())), sort_keys=True))
"""


@pytest.fixture
def roots(tmp_path: Path) -> tuple[Path, Path]:
    storage = tmp_path / "storage"
    catalog = tmp_path / "catalog"
    storage.mkdir()
    return catalog, storage


def publish(
    roots: tuple[Path, Path],
    name: str,
    records: list[object],
    *,
    record_type: RecordType = RecordType.TRADE,
    contract: object = ES_M2026,
) -> ManifestHash:
    catalog, storage = roots
    return publish_dataset(
        catalog_root=catalog,
        storage_root=storage,
        name=name,
        records=records,  # type: ignore[arg-type]
        record_type=record_type,
        contract=contract,  # type: ignore[arg-type]
    ).manifest_hash


def digest_of(
    roots: tuple[Path, Path], *hashes: ManifestHash, replay_range: ReplayRange | None = None
) -> list[dict[str, object]]:
    catalog, storage = roots
    replay = prepare_replay(
        catalog_root=catalog,
        storage_root=storage,
        config=ReplayConfig(manifest_hashes=hashes, replay_range=replay_range),
    )
    return frame_digest(list(replay.frames()))


def mixed_sources(roots: tuple[Path, Path]) -> tuple[ManifestHash, ...]:
    """Three sources spanning every record type, two contracts, and shared instants."""
    return (
        publish(roots, "trades.parquet", [trade_at(at(seconds=n)) for n in (0, 1, 1, 3)]),
        publish(
            roots,
            "quotes.parquet",
            [quote_at(at(seconds=n)) for n in (0, 1, 2)],
            record_type=RecordType.QUOTE,
        ),
        publish(
            roots,
            "bars.parquet",
            [bar_starting(at(seconds=0)), bar_starting(at(minutes=1))],
            record_type=RecordType.BAR,
            contract=NQ_M2026,
        ),
    )


# --------------------------------------------------------------------------
# Same process
# --------------------------------------------------------------------------


def test_a_fresh_preparation_produces_the_same_stream(roots: tuple[Path, Path]) -> None:
    sources = mixed_sources(roots)

    assert digest_of(roots, *sources) == digest_of(roots, *sources)


def test_every_caller_permutation_produces_the_same_stream(roots: tuple[Path, Path]) -> None:
    first, second, third = mixed_sources(roots)
    orderings = [
        (first, second, third),
        (first, third, second),
        (second, first, third),
        (second, third, first),
        (third, first, second),
        (third, second, first),
    ]

    digests = [digest_of(roots, *order) for order in orderings]

    assert all(digest == digests[0] for digest in digests)
    assert digests[0] != []


def test_relocating_every_artifact_produces_the_same_stream(roots: tuple[Path, Path]) -> None:
    catalog, storage = roots
    sources = mixed_sources(roots)
    before = digest_of(roots, *sources)

    nested = storage / "archive" / "2026"
    nested.mkdir(parents=True)
    for artifact in sorted(storage.glob("*.parquet")):
        artifact.rename(nested / artifact.name)
    rebuild_index(catalog_root=catalog, storage_root=storage)

    assert digest_of(roots, *sources) == before


def test_a_second_byte_identical_copy_produces_the_same_stream(
    roots: tuple[Path, Path],
) -> None:
    catalog, storage = roots
    source = publish(roots, "a.parquet", [trade_at(at(seconds=n)) for n in (0, 1)])
    before = digest_of(roots, source)

    (storage / "mirror").mkdir()
    (storage / "mirror" / "a.parquet").write_bytes((storage / "a.parquet").read_bytes())
    rebuild_index(catalog_root=catalog, storage_root=storage)

    assert digest_of(roots, source) == before


def test_the_replay_range_is_part_of_what_makes_a_run_reproducible(
    roots: tuple[Path, Path],
) -> None:
    source = publish(roots, "a.parquet", [trade_at(at(seconds=n)) for n in range(6)])
    window = ReplayRange(start=at(seconds=1), end=at(seconds=4))

    assert digest_of(roots, source, replay_range=window) == digest_of(
        roots, source, replay_range=window
    )
    assert digest_of(roots, source, replay_range=window) != digest_of(roots, source)


# --------------------------------------------------------------------------
# Lineage independence
# --------------------------------------------------------------------------


def test_publishing_a_successor_does_not_change_the_predecessors_replay(
    roots: tuple[Path, Path],
) -> None:
    """A run that pinned some bytes consumed those bytes, whatever is said later.

    Replay never consults the supersedes graph — the package imports no lineage
    navigation at all — so this is a structural guarantee rather than a policy.
    """
    catalog, storage = roots
    original = publish_dataset(
        catalog_root=catalog,
        storage_root=storage,
        name="original.parquet",
        records=[trade_at(at(seconds=0)), trade_at(at(seconds=1))],
        record_type=RecordType.TRADE,
        contract=ES_M2026,
    )
    before = digest_of(roots, original.manifest_hash)

    correction = publish_dataset(
        catalog_root=catalog,
        storage_root=storage,
        name="correction.parquet",
        records=[trade_at(at(seconds=0)), trade_at(at(seconds=1), price="4600.00")],
        record_type=RecordType.TRADE,
        contract=ES_M2026,
    )
    publish_supersedes_relation(
        catalog_root=catalog,
        successor=correction.manifest_hash,
        predecessors=[original.manifest_hash],
    )

    after = digest_of(roots, original.manifest_hash)

    assert after == before
    assert digest_of(roots, correction.manifest_hash) != before


def test_the_replay_package_imports_no_lineage_navigation() -> None:
    """The structural half of the claim above, asserted rather than assumed."""
    import quant_research_terminal.replay as replay_package

    source = Path(replay_package.__file__).parent
    for module in sorted(source.glob("*.py")):
        text = module.read_text(encoding="utf-8")
        assert "catalog.lineage" not in text, module.name
        assert "catalog.comparison" not in text, module.name


# --------------------------------------------------------------------------
# Cross-process
# --------------------------------------------------------------------------


def run_child(
    roots: tuple[Path, Path], script: Path, *hashes: ManifestHash, env: dict[str, str]
) -> list[dict[str, object]]:
    catalog, storage = roots
    completed = subprocess.run(  # noqa: S603
        [
            sys.executable,
            str(script),
            str(TESTS_DIRECTORY),
            str(catalog),
            str(storage),
            *[digest.canonical() for digest in hashes],
        ],
        capture_output=True,
        text=True,
        check=True,
        env={**os.environ, **env},
    )
    parsed: list[dict[str, object]] = json.loads(completed.stdout)
    return parsed


@pytest.mark.parametrize("hash_seed", ["0", "1", "12345"])
def test_the_stream_survives_a_different_python_hash_seed(
    roots: tuple[Path, Path], tmp_path: Path, hash_seed: str
) -> None:
    sources = mixed_sources(roots)
    script = tmp_path / "child.py"
    script.write_text(CHILD_SCRIPT, encoding="utf-8")

    child = run_child(roots, script, *sources, env={"PYTHONHASHSEED": hash_seed})

    assert child == digest_of(roots, *sources)


def test_the_stream_is_produced_with_every_clock_read_denied(
    roots: tuple[Path, Path], tmp_path: Path
) -> None:
    """The real proof that replay time is not machine time.

    The child raises on `datetime.now`, `utcnow`, `today`, `fromtimestamp`,
    `time.time`, `time_ns`, `monotonic`, `perf_counter`, `localtime`, `gmtime`,
    and `sleep`. If replay consulted a clock during preparation, verification, or
    frame production, the child would die and the digests could not match.

    Scope, stated exactly: the denial is installed after imports (see
    `CLOCK_DENIAL` for why it cannot be earlier), so it covers replay's *work*
    and not its import. Import-time reads are covered by the AST sweep in
    `test_architecture_boundaries.py`.

    This supersedes the timezone parametrisation it replaced, which could not
    fail on Windows: `TZ` does not affect the C runtime's local time there and
    `time.tzset` does not exist, so all its cases ran in one effective zone.
    """
    sources = mixed_sources(roots)
    script = tmp_path / "clock_denied.py"
    script.write_text(CLOCK_DENIED_SCRIPT, encoding="utf-8")

    child = run_child(roots, script, *sources, env={})

    assert child == digest_of(roots, *sources)
    assert child != []


def test_the_clock_denial_harness_would_actually_catch_a_clock_read() -> None:
    """A canary for the denial itself — otherwise the test above proves nothing.

    Runs the exact denial the child runs, then reads a clock each of the two
    ways replay code could: through the `datetime` module and through `time`.
    Both must abort the process.
    """
    for read in ("import datetime\ndatetime.datetime.now()", "import time\ntime.time()"):
        completed = subprocess.run(  # noqa: S603
            [sys.executable, "-c", f"{CLOCK_DENIAL}\n{read}\n"],
            capture_output=True,
            text=True,
            check=False,
        )

        assert completed.returncode != 0, read
        assert "replay read the wall clock" in completed.stderr, read


@pytest.mark.parametrize("timezone_name", ["UTC", "America/New_York", "Asia/Tokyo"])
def test_the_stream_survives_a_different_tz_environment_variable(
    roots: tuple[Path, Path], tmp_path: Path, timezone_name: str
) -> None:
    """Retained as a POSIX smoke check, and named for what it actually tests.

    On Linux and macOS this genuinely varies the process timezone. On Windows —
    this project's primary platform — `TZ` is inert, so here it varies only an
    environment variable. It is kept because it costs nothing and does test
    something real off-Windows, but the guarantee is carried by the
    clock-denial test above, not by this one.
    """
    sources = mixed_sources(roots)
    script = tmp_path / "child.py"
    script.write_text(CHILD_SCRIPT, encoding="utf-8")

    child = run_child(roots, script, *sources, env={"TZ": timezone_name})

    assert child == digest_of(roots, *sources)


def test_a_child_process_reproduces_the_stream_under_a_permuted_input_order(
    roots: tuple[Path, Path], tmp_path: Path
) -> None:
    first, second, third = mixed_sources(roots)
    script = tmp_path / "child.py"
    script.write_text(CHILD_SCRIPT, encoding="utf-8")

    forward = run_child(roots, script, first, second, third, env={"PYTHONHASHSEED": "7"})
    reverse = run_child(roots, script, third, second, first, env={"PYTHONHASHSEED": "99"})

    assert forward == reverse
    assert forward == digest_of(roots, first, second, third)


def test_the_child_script_would_actually_notice_a_difference(
    roots: tuple[Path, Path], tmp_path: Path
) -> None:
    """A canary: a comparison that can never fail proves nothing.

    Two genuinely different input sets must produce different digests, or the
    determinism tests above would pass on a digest function that returned a
    constant.
    """
    first, second, third = mixed_sources(roots)
    script = tmp_path / "child.py"
    script.write_text(CHILD_SCRIPT, encoding="utf-8")

    everything = run_child(roots, script, first, second, third, env={})
    subset = run_child(roots, script, first, env={})

    assert everything != subset
