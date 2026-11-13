import json
import math
import os
import signal
import subprocess
import sys
import time
import tomllib

import pytest
from hypothesis import given
from hypothesis import strategies as st

from exprun.config import SEED_LIMIT, ConfigError
from exprun.report import sweep_table
from exprun.store import RunStore
from exprun.sweep import Sweep, SweepError, members, new_sweep_id, parse_grid, plan, resolve, run, toml_value

# --- the grid -----------------------------------------------------------------------------------


def test_a_grid_is_keys_with_lists_of_toml_values():
    assert parse_grid(["model.C=0.1,1,10", "seed=1,2"]) == [("model.C", [0.1, 1, 10]), ("seed", [1, 2])]
    assert parse_grid(['name="a", "b,c"']) == [("name", ["a", "b,c"])]
    assert parse_grid(["layers=[64,64],[128]", "flag=true,false"]) == [("layers", [[64, 64], [128]]),
                                                                       ("flag", [True, False])]
    assert parse_grid(["x = 1"]) == [("x", [1])]


@pytest.mark.parametrize("items, message", [
    (["C"], "must look like key.path=value"),
    (["=1,2"], "must look like key.path=value"),
    (["C=1", "C=2"], "--grid C is given twice"),
    (["C="], "has no values"),
    (["C=a,b"], "isn't a comma-separated list of TOML values"),
    (["C=1,2,1"], "lists 1 twice"),
    (['C="x","x"'], 'lists "x" twice'),
    (["seed=1,-1"], "-1 isn't a seed"),
    (["seed=1,1.5"], "1.5 isn't a seed"),
    (["seed=true"], "true isn't a seed"),
    ([f"seed={SEED_LIMIT}"], f"{SEED_LIMIT} isn't a seed"),
    (["when=2026-01-01"], "is a TOML date or time"),
])
def test_bad_grids_are_refused(items, message):
    with pytest.raises(SweepError, match=message.replace("(", r"\(")):
        parse_grid(items)


def test_values_of_different_types_are_different_points():
    assert parse_grid(['C=1,1.0,true,"1"']) == [("C", [1, 1.0, True, "1"])]


toml_values = st.recursive(
    st.booleans() | st.integers(-(2**63), 2**63 - 1) | st.floats(allow_nan=False, allow_infinity=False)
    | st.text(),
    lambda inner: st.lists(inner, max_size=3) | st.dictionaries(st.text(max_size=5), inner, max_size=3),
    max_leaves=8)


@given(toml_values)
def test_toml_value_reads_back_exactly(value):
    read = tomllib.loads(f"v = {toml_value(value)}")["v"]
    assert read == value and json.dumps(read) == json.dumps(value)      # json tells 1 from 1.0
    if isinstance(value, float):
        assert math.copysign(1, read) == math.copysign(1, value)       # even -0.0


# --- planning -----------------------------------------------------------------------------------

def test_the_plan_has_every_combination_in_order_each_recorded_as_overrides(project):
    points, seed_source = plan(project.write(), ["sleep=1"], parse_grid(["C=0.5,2.0", "seed=1,2,3"]))
    assert [p.values for p in points] == [{"C": c, "seed": s} for c in (0.5, 2.0) for s in (1, 2, 3)]
    assert [p.index for p in points] == list(range(6)) and seed_source == "sweep"
    assert points[4].overrides == ["sleep=1", "C=2.0"] and points[4].seed == 2
    assert points[4].config.params == {"C": 2.0, "sleep": 1}


def test_without_a_seed_in_the_grid_every_point_shares_one(project):
    points, source = plan(project.write(), [], parse_grid(["C=1.0,2.0"]))
    assert source == "config" and [p.seed for p in points] == [42, 42]
    points, source = plan(project.write(seed=""), [], parse_grid(["C=1.0,2.0,3.0"]))
    assert source == "drawn" and len({p.seed for p in points}) == 1 and 0 <= points[0].seed < SEED_LIMIT


def test_a_bad_point_fails_the_whole_plan_naming_the_grid_key(project):
    with pytest.raises(ConfigError, match=r"--grid c: params has no c \(did you mean C\?\) \(at the grid point c=1\)"):
        plan(project.write(), [], parse_grid(["c=1,2"]))
    with pytest.raises(ConfigError, match=r"must be finite.* \(at the grid point C=inf\)"):
        plan(project.write(), [], parse_grid(["C=1.0,inf"]))
    with pytest.raises(SweepError, match="C is given with both --set and --grid"):
        plan(project.write(), ["C=3.0"], parse_grid(["C=1.0"]))
    with pytest.raises(SweepError, match="at least one --grid"):
        plan(project.write(), [], [])


# --- running ------------------------------------------------------------------------------------

def sweep_of(project, task, grid, overrides=(), seed="seed = 42", sleep=0):
    points, source = plan(project.write(task, seed=seed, sleep=sleep), list(overrides), parse_grid(grid))
    return Sweep(new_sweep_id(), points, source, parse_grid(grid))


def test_every_point_runs_and_records_its_place_in_the_sweep(project):
    sweep = sweep_of(project, "picky", ["C=1.0,2.0", "seed=5,6"])
    run(sweep, RunStore(project.root / "runs"), workers=2)
    assert not sweep.stopped and not sweep.not_started and len(sweep.outcomes) == 4
    found = members(RunStore(project.root / "runs"), sweep.id)
    assert [m["sweep"]["index"] for _, m, _ in found] == [0, 1, 2, 3]
    for _, m, status in found:
        point = m["sweep"]["point"]
        assert status == "succeeded" and m["metrics"] == {"C": point["C"], "seed": point["seed"]}
        assert m["spec"]["seed"] == point["seed"] and m["seed_source"] == "sweep"
        assert m["sweep"]["grid"] == {"C": [1.0, 2.0], "seed": [5, 6]} and m["sweep"]["size"] == 4
        assert m["config"]["overrides"] == [f"C={toml_value(point['C'])}"]
    assert len({m["fingerprint"] for _, m, _ in found}) == 4


def test_at_most_the_given_number_of_tasks_run_at_once(project):
    sweep = sweep_of(project, "timed", ["C=1.0,2.0,3.0,4.0,5.0,6.0"], sleep=0.4)
    run(sweep, RunStore(project.root / "runs"), workers=2)
    spans = [(o.manifest["metrics"]["began"], o.manifest["metrics"]["ended"]) for o in sweep.outcomes]
    most = max(sum(b <= moment < e for b, e in spans) for moment, _ in spans)
    assert most == 2                            # two at a time: never three, and not one after another


def test_a_failing_point_does_not_stop_the_others(project):
    sweep = sweep_of(project, "picky", ["C=-1.0,1.0,2.0"])
    run(sweep, RunStore(project.root / "runs"), workers=1)
    statuses = {o.point.values["C"]: o.manifest["status"] for o in sweep.outcomes}
    assert statuses == {-1.0: "failed", 1.0: "succeeded", 2.0: "succeeded"}


def test_a_point_the_runner_cannot_start_is_recorded_and_the_rest_run(project):
    sweep = sweep_of(project, "picky", ["C=1.0,2.0"])
    (project.root / "data.csv").unlink()       # after planning: the first point can't hash its input
    run(sweep, RunStore(project.root / "runs"), workers=1)
    assert [o.manifest for o in sweep.outcomes] == [None, None]
    assert all("data.csv" in str(o.error) for o in sweep.outcomes)


def test_workers_must_be_positive(project):
    with pytest.raises(ValueError, match="at least 1"):
        run(sweep_of(project, "picky", ["C=1.0"]), RunStore(project.root / "runs"), workers=0)


# --- stopping a whole sweep: real processes, real signals ----------------------------------------

def start_sweep(project, workers=2, points="1.0,2.0,3.0,4.0"):
    config = project.write("waits", sleep=30)
    marks = project.root / "marks"
    env = {**os.environ, "EXPRUN_TEST_MARKS": str(marks)}
    env.pop("PYTHONUNBUFFERED", None)
    runner = subprocess.Popen([sys.executable, "-m", "exprun.cli", "sweep", str(config), "--grid", f"C={points}",
                               "--workers", str(workers), "--runs", str(project.root / "runs")],
                              cwd=project.root, env=env, start_new_session=True,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    deadline = time.monotonic() + 30
    while not marks.is_dir() or len(list(marks.iterdir())) < workers:
        assert runner.poll() is None, runner.communicate()
        assert time.monotonic() < deadline, "the tasks never started"
        time.sleep(0.02)
    return runner, [int(p.name) for p in marks.iterdir()]


def alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


@pytest.mark.parametrize("how", ["ctrl-c", "sigterm"])
def test_stopping_a_sweep_stops_its_runs_and_starts_no_more(project, how):
    runner, pids = start_sweep(project)
    began = time.monotonic()
    if how == "ctrl-c":
        os.killpg(runner.pid, signal.SIGINT)       # a terminal signals the whole group
    else:
        os.kill(runner.pid, signal.SIGTERM)        # only the sweep: it must stop the tasks itself
    out, err = runner.communicate(timeout=20)
    assert time.monotonic() - began < 4
    assert runner.returncode == 1 and "2 runs did not succeed, 2 not started (stopped)" in err.decode()
    assert out.decode().count("not started") == 2 and out.decode().count("interrupted") == 2
    runs = RunStore(project.root / "runs").all()
    assert len(runs) == 2 and all(f.read()["status"] == "interrupted" for f in runs)
    assert not any(alive(pid) for pid in pids)


# --- the results table --------------------------------------------------------------------------

def manifest(run_id, metrics, duration=1.0):
    return {"id": run_id, "metrics": metrics, "duration_s": duration}


def test_the_table_shows_swept_values_status_and_every_metric():
    rows = [(manifest("r1", {"acc": 0.5}), "succeeded", {"C": 1, "kind": "a"}),
            (manifest("r2", {"acc": 0.9, "loss": 0.1}), "succeeded", {"C": 1.0, "kind": "b"}),
            (None, "not started", {"C": 2, "kind": "a"})]
    header, lines = sweep_table(["C", "kind"], rows)
    assert header == ["RUN", "C", "kind", "STATUS", "DURATION", "acc", "loss"]
    assert lines == [["r1", "1", '"a"', "succeeded", "1.00 s", "0.5", "–"],
                     ["r2", "1.0", '"b"', "succeeded", "1.00 s", "0.9", "0.1"],
                     ["–", "2", '"a"', "not started", "–", "–", "–"]]


def test_sorting_puts_rows_without_a_number_last_in_grid_order():
    rows = [(manifest("a", {"acc": 0.7}), "succeeded", {}), (manifest("b", {"acc": True}), "succeeded", {}),
            (manifest("c", {"acc": 0.9}), "succeeded", {}), (manifest("d", None), "failed", {}),
            (manifest("e", {"acc": 0.8}), "succeeded", {})]
    assert [line[0] for line in sweep_table([], rows, "acc")[1]] == ["a", "e", "c", "b", "d"]
    assert [line[0] for line in sweep_table([], rows, "acc", descending=True)[1]] == ["c", "e", "a", "b", "d"]


# --- finding a sweep ----------------------------------------------------------------------------

def test_a_sweep_is_found_by_id_prefix_or_latest(project):
    store = RunStore(project.root / "runs")
    with pytest.raises(LookupError, match="no sweeps in"):
        resolve(store, "latest")
    first = sweep_of(project, "picky", ["C=1.0"])
    first.id = "20260101T000000Z-aaaaaa"
    run(first, store, workers=1)
    second = sweep_of(project, "picky", ["C=2.0,3.0"])          # two runs, still one sweep
    second.id = "20260101T000000Z-aaaabb"
    run(second, store, workers=1)
    assert resolve(store, "latest") == second.id and resolve(store, "20260101T000000Z-aaaab") == second.id
    with pytest.raises(LookupError, match="matches 2 sweeps"):
        resolve(store, "20260101T000000Z-aaaa")
    with pytest.raises(LookupError, match="no sweep 'zzz'"):
        resolve(store, "zzz")


def test_members_are_in_grid_order_whatever_order_they_started_in(project):
    store = RunStore(project.root / "runs")
    sweep = sweep_of(project, "picky", ["C=1.0,2.0"])
    run(sweep, store, workers=1)
    first, second = store.all()                  # started in grid order: swap their places in the grid
    for folder, index in ((first, 1), (second, 0)):
        m = folder.read()
        m["sweep"]["index"] = index
        folder.write(m)
    assert [folder.id for folder, _, _ in members(store, sweep.id)] == [second.id, first.id]
