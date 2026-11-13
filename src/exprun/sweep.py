"""Parameter sweeps: one run per point of a grid, a bounded number at a time.

`exprun sweep exp.toml --grid model.C=0.1,1,10 --grid seed=1,2,3` makes 9 runs, one for each
combination. Each is an ordinary run: its own folder, manifest, lock and child process. What a
sweep adds:

- **The grid is checked before anything runs.** Every point's config is built first, so a typo in
  a key or a bad value fails the whole sweep at once instead of after the first hours of it.
- **Each point is recorded as an override** (`model.C=0.1`), exactly like `--set`, so `exprun
  rerun` works on a run from a sweep without knowing about sweeps.
- **Seeds:** `seed` may be swept like a param. Otherwise every point uses the same seed (the
  config's, or one drawn for the whole sweep), so that points differ only in what was swept.
- **Bounded parallelism:** at most `workers` tasks run at once, each in its own process. The
  runner waits for its children from worker threads, which only wait on processes and so don't
  compete for the GIL.
- **Stopping:** signals reach only the main thread. Ctrl-C or SIGTERM there stops starting new
  points and passes a `StopRequest` to the runs in progress, which stop as a single run does; a
  second Ctrl-C kills them. Points that never started are reported as such.
"""

from __future__ import annotations

import itertools
import math
import queue
import secrets
import signal
import threading
import time as clock
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from exprun.config import SEED_LIMIT, Config, ConfigError, load
from exprun.identity import identify
from exprun.runner import StopRequest, Terminated, execute
from exprun.store import RunFolder, RunStore, now

SEED = "seed"


class SweepError(ValueError):
    """A grid that can't be run: bad syntax, a repeated key, a value that isn't allowed."""


def parse_grid(items: list[str]) -> list[tuple[str, list]]:
    """`["model.C=0.1,1,10", "seed=1,2"]` -> `[("model.C", [0.1, 1, 10]), ("seed", [1, 2])]`.

    The values are a TOML array without its brackets, so strings are quoted and arrays nest:
    `name="a","b"` and `layers=[64,64],[128]` both work."""
    grid: list[tuple[str, list]] = []
    for item in items:
        key, sep, raw = item.partition("=")
        key = key.strip()
        if not sep or not key:
            raise SweepError(f"--grid {item!r} must look like key.path=value,value,..., such as model.C=0.1,1,10")
        if any(key == seen for seen, _ in grid):
            raise SweepError(f"--grid {key} is given twice")
        try:
            values = tomllib.loads(f"v = [{raw}]")["v"]
        except tomllib.TOMLDecodeError:
            raise SweepError(f"--grid {key}: {raw.strip()!r} isn't a comma-separated list of TOML values; "
                             f"quote strings, as in {key}='\"a\",\"b\"'") from None
        if not values:
            raise SweepError(f"--grid {key} has no values")
        for value in values:
            try:
                toml_value(value)
            except TypeError:
                raise SweepError(f"--grid {key}: {value} is a TOML date or time; write it as a string instead") from None
        canonical = [toml_value(v) for v in values]        # tells 1, 1.0, true and "1" apart
        repeated = next((v for v, c in zip(values, canonical, strict=True) if canonical.count(c) > 1), None)
        if repeated is not None:
            raise SweepError(f"--grid {key} lists {toml_value(repeated)} twice")
        if key == SEED:
            bad = next((v for v in values if type(v) is not int or not 0 <= v < SEED_LIMIT), None)
            if bad is not None:
                raise SweepError(f"--grid seed: {toml_value(bad)} isn't a seed (a whole number from 0 to 2**32 - 1)")
        grid.append((key, values))
    return grid


def toml_value(value) -> str:
    """A TOML value as text, so that `tomllib` reads back exactly `value`."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        if not math.isfinite(value):
            return {math.inf: "inf", -math.inf: "-inf"}.get(value, "nan")
        return repr(value)            # always a TOML float: "1.0", "1e-05", "1e+16"
    if isinstance(value, str):
        return _toml_string(value)
    if isinstance(value, list):
        return "[" + ", ".join(toml_value(v) for v in value) + "]"
    if isinstance(value, dict):
        return "{" + ", ".join(f"{_toml_string(str(k))} = {toml_value(v)}"
                               for k, v in value.items()) + "}"
    raise TypeError(f"{type(value).__name__} isn't a TOML value")


def _toml_string(text: str) -> str:
    """A TOML basic string. Not JSON's: TOML also refuses a raw DEL (U+007F)."""
    escaped = "".join(f"\\u{ord(c):04X}" if ord(c) < 0x20 or c == "\x7f" else "\\" + c if c in '"\\' else c
                      for c in text)
    return f'"{escaped}"'


@dataclass
class Point:
    """One combination: `values` maps each grid key to its value here."""
    index: int
    values: dict
    config: Config
    overrides: list[str]
    seed: int


def plan(config_path: Path, overrides: list[str], grid: list[tuple[str, list]]) -> tuple[list[Point], str]:
    """Every point of the grid, with its config already loaded and checked, and where the seed comes
    from: "sweep" (a grid key), "config" or "drawn" (once, for all points). Raises ConfigError for
    a point whose config is invalid, TaskError or InputError for a missing task or input, and
    SweepError for a grid key also given with --set."""
    if not grid:
        raise SweepError("a sweep needs at least one --grid key=values")
    set_keys = {item.partition("=")[0].strip() for item in overrides}
    for key, _ in grid:
        if key in set_keys:
            raise SweepError(f"{key} is given with both --set and --grid")
    base = load(config_path, overrides)
    identify(base, 0)          # the task and the inputs: a TaskError or InputError now, not once per point
    if any(key == SEED for key, _ in grid):
        seed_source, shared_seed = "sweep", None
    elif base.seed is not None:
        seed_source, shared_seed = "config", base.seed
    else:
        seed_source, shared_seed = "drawn", secrets.randbelow(SEED_LIMIT)
    keys = [key for key, _ in grid]
    points = []
    for index, combination in enumerate(itertools.product(*(values for _, values in grid))):
        values = dict(zip(keys, combination, strict=True))
        extra = [f"{key}={toml_value(value)}" for key, value in values.items() if key != SEED]
        try:
            config = load(config_path, overrides + extra) if extra else base
        except ConfigError as exc:
            message = str(exc)
            for key in values:
                message = message.replace(f"--set {key}:", f"--grid {key}:")
            raise ConfigError(f"{message} (at the grid point {describe(values)})") from None
        points.append(Point(index, values, config, overrides + extra, values.get(SEED, shared_seed)))
    return points, seed_source


def describe(values: dict) -> str:
    return " ".join(f"{key}={toml_value(value)}" for key, value in values.items())


def new_sweep_id() -> str:
    return f"{now():%Y%m%dT%H%M%SZ}-{secrets.token_hex(3)}"


@dataclass
class Outcome:
    point: Point
    folder: RunFolder | None = None
    manifest: dict | None = None
    error: BaseException | None = None       # the runner couldn't start or record the run


@dataclass
class Sweep:
    id: str
    points: list[Point]
    seed_source: str
    grid: list[tuple[str, list]]
    outcomes: list[Outcome] = field(default_factory=list)     # in the order they finished
    stopped: bool = False

    @property
    def not_started(self) -> list[Point]:
        done = {outcome.point.index for outcome in self.outcomes}
        return [p for p in self.points if p.index not in done]


def run(sweep: Sweep, runs: RunStore, *, workers: int, timeout: float | None = None, quiet: bool = True,
        on_start=None, on_done=None) -> Sweep:
    """Run every point, at most `workers` at a time. Call from the main thread: it handles Ctrl-C
    and SIGTERM. `on_start(point, folder, manifest)` and `on_done(outcome)` are called from worker
    threads, one at a time."""
    if workers < 1:
        raise ValueError("workers must be at least 1")
    todo: queue.Queue[Point] = queue.Queue()
    for point in sweep.points:
        todo.put(point)
    stop = StopRequest()
    report = threading.Lock()
    recorded = {"id": sweep.id, "grid": {key: values for key, values in sweep.grid}, "size": len(sweep.points)}

    def started(point):
        def call(folder, manifest):
            if on_start is not None:
                with report:
                    on_start(point, folder, manifest)
        return call

    def work() -> None:
        try:
            take_points()
        finally:
            with report:
                running[0] -= 1

    def take_points() -> None:
        while not stop.requested:
            try:
                point = todo.get_nowait()
            except queue.Empty:
                return
            outcome = Outcome(point)
            try:
                outcome.folder, outcome.manifest = execute(
                    point.config, runs, overrides=point.overrides, timeout=timeout, quiet=quiet,
                    on_start=started(point), seed=point.seed, seed_source=sweep.seed_source,
                    sweep={**recorded, "index": point.index, "point": point.values}, stop=stop)
            except Exception as exc:            # recorded per point: the other points still run
                outcome.error = exc
            with report:
                sweep.outcomes.append(outcome)
                if on_done is not None:
                    on_done(outcome)

    threads = [threading.Thread(target=work, name=f"sweep-worker-{i}") for i in range(min(workers, len(sweep.points)))]
    running = [len(threads)]          # workers not yet finished: counted by the workers themselves

    def terminated(signum, frame):
        raise Terminated()

    previous = signal.signal(signal.SIGTERM, terminated)
    try:
        for thread in threads:
            thread.start()
        # Polled with sleep, not Thread.join: before Python 3.13, a join interrupted by Ctrl-C could
        # leave is_alive() False for a thread still running, and the sweep would end without its run.
        while running[0] > 0:
            try:
                clock.sleep(0.05)
            except (KeyboardInterrupt, Terminated) as exc:
                sweep.stopped = True
                # Ctrl-C reached the tasks too (one process group); SIGTERM reached only us: pass it on.
                stop.request(signal.SIGTERM if isinstance(exc, Terminated) else None)
    finally:
        signal.signal(signal.SIGTERM, previous)
    return sweep


def members(store: RunStore, sweep_id: str) -> list[tuple[RunFolder, dict, str]]:
    """The runs of one sweep, in grid order: (folder, manifest, status)."""
    found = []
    for folder in store.all():
        try:
            manifest = folder.reconcile()
        except ValueError:
            continue
        if manifest is not None and (manifest.get("sweep") or {}).get("id") == sweep_id:
            found.append((folder, manifest, folder.effective_status(manifest)))
    return sorted(found, key=lambda item: item[1]["sweep"]["index"])


def sweep_ids(store: RunStore) -> list[str]:
    """Every sweep in the runs folder, oldest first (by its first run)."""
    ids: list[str] = []
    for folder in store.all():
        try:
            manifest = folder.read()
        except ValueError:
            continue
        sweep_id = ((manifest or {}).get("sweep") or {}).get("id")
        if sweep_id and sweep_id not in ids:
            ids.append(sweep_id)
    return ids


def resolve(store: RunStore, reference: str) -> str:
    """A sweep id, a unique start of one, or 'latest'. Raises LookupError."""
    ids = sweep_ids(store)
    if not ids:
        raise LookupError(f"no sweeps in {store.root}: start one with `exprun sweep CONFIG --grid key=values`")
    if reference == "latest":
        return ids[-1]
    matches = [i for i in ids if i.startswith(reference)]
    if len(matches) == 1:
        return matches[0]
    if not matches:
        raise LookupError(f"no sweep {reference!r} in {store.root}")
    raise LookupError(f"{reference!r} matches {len(matches)} sweeps: {', '.join(matches)}")
