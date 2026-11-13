import json
import subprocess
import sys

import pytest

from exprun.cli import run as main
from exprun.store import RunStore

CONFIG = """\
[experiment]
name = "digits-1"
task = "tasks:train"
seed = 7
description = "a baseline"

[inputs]
train = "data/train.csv"
images = "data/images"

[params]
model = { C = 1.0 }
"""


@pytest.fixture
def config_folder(tmp_path):
    (tmp_path / "data" / "images").mkdir(parents=True)
    (tmp_path / "data" / "train.csv").write_text("a,b\n1,2\n")
    (tmp_path / "data" / "images" / "1.txt").write_text("x")
    (tmp_path / "data" / "images" / "2.txt").write_text("y")
    (tmp_path / "tasks.py").write_text("def train(ctx):\n    return {}\n")
    (tmp_path / "exp.toml").write_text(CONFIG)
    return tmp_path


def test_check_shows_what_identifies_the_run(config_folder, capsys):
    assert main(["check", str(config_folder / "exp.toml")]) == 0
    out = capsys.readouterr().out
    for expected in (f"config:      {config_folder / 'exp.toml'}", "experiment:  digits-1 (a baseline)",
                     "task:        tasks:train  (tasks.py", "seed:        7", 'params:      {"model": {"C": 1.0}}',
                     "train   data/train.csv  (8 bytes)", "images  data/images  (2 bytes, 2 files)",
                     "config hash: ", "fingerprint: "):
        assert expected in out, expected


def test_check_as_json_gives_the_full_hashes(config_folder, capsys):
    assert main(["check", str(config_folder / "exp.toml"), "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["name"] == "digits-1" and data["seed"] == 7 and data["description"] == "a baseline"
    assert len(data["fingerprint"]) == len("sha256:") + 64
    assert data["inputs"]["images"]["kind"] == "folder" and data["code"]["function"] == "train"


def test_an_override_changes_the_fingerprint(config_folder, capsys):
    def fingerprint(*extra):
        assert main(["check", str(config_folder / "exp.toml"), "--json", *extra]) == 0
        return json.loads(capsys.readouterr().out)["fingerprint"]

    base = fingerprint()
    assert fingerprint("--set", "model.C=2.0") != base
    assert fingerprint("--set", "model.C=1.0") == base


def test_check_without_a_seed_says_when_it_is_drawn(config_folder, capsys):
    (config_folder / "exp.toml").write_text(CONFIG.replace("seed = 7\n", ""))
    assert main(["check", str(config_folder / "exp.toml")]) == 0
    out = capsys.readouterr().out
    assert "seed:        not set: one is drawn and recorded at run time" in out
    assert "fingerprint: computed at run time, once the seed is drawn" in out


def test_check_names_the_git_commit_and_uncommitted_changes(config_folder, capsys):
    def git(*args):
        subprocess.run(["git", "-C", str(config_folder), "-c", "user.name=t", "-c", "user.email=t@example.com",
                        "-c", "commit.gpgsign=false", *args], check=True, capture_output=True)

    git("init", "-q")
    git("add", "-A")
    git("commit", "-qm", "first")
    assert main(["check", str(config_folder / "exp.toml")]) == 0
    line = next(x for x in capsys.readouterr().out.splitlines() if x.startswith("task:"))
    assert ", git " in line and "uncommitted" not in line
    (config_folder / "tasks.py").write_text("def train(ctx):\n    return {'v': 2}\n")
    assert main(["check", str(config_folder / "exp.toml")]) == 0
    assert "+ uncommitted changes" in capsys.readouterr().out


def test_check_names_an_installed_task_package(config_folder, capsys):
    (config_folder / "exp.toml").write_text(CONFIG.replace('"tasks:train"', '"hypothesis:given"'))
    assert main(["check", str(config_folder / "exp.toml")]) == 0
    assert "(__init__.py, hypothesis 6." in capsys.readouterr().out


def test_check_without_inputs(config_folder, capsys):
    (config_folder / "exp.toml").write_text('[experiment]\nname = "x"\ntask = "tasks:train"\nseed = 1\n')
    assert main(["check", str(config_folder / "exp.toml")]) == 0
    assert "inputs:      none" in capsys.readouterr().out


@pytest.mark.parametrize("edit, args, message", [
    (lambda t: t.replace("seed = 7", "sed = 7"), [], "unknown key experiment.sed (did you mean seed?)"),
    (lambda t: t.replace('"tasks:train"', '"tasks:trian"'), [], "doesn't define a top-level function 'trian'"),
    (lambda t: t.replace('"tasks:train"', '"missing:train"'), [], "task module 'missing' not found"),
    (lambda t: t, ["--set", "model.c=1"], "params has no model.c (did you mean model.C?)"),
    (lambda t: t.replace("data/train.csv", "data/nope.csv"), [], "inputs.train: data/nope.csv doesn't exist"),
])
def test_invalid_configs_exit_2_before_anything_runs(config_folder, capsys, edit, args, message):
    (config_folder / "exp.toml").write_text(edit(CONFIG))
    assert main(["check", str(config_folder / "exp.toml"), *args]) == 2
    captured = capsys.readouterr()
    assert captured.out == "" and message in captured.err and captured.err.startswith("exprun: ")


def test_an_input_that_cannot_be_hashed_exits_2(config_folder, capsys):
    (config_folder / "data" / "images" / "link").symlink_to(config_folder / "data")
    assert main(["check", str(config_folder / "exp.toml")]) == 2
    assert "symlink to a folder inside an input" in capsys.readouterr().err


def test_an_unreadable_config_exits_1(config_folder, capsys):
    assert main(["check", str(config_folder / "missing.toml")]) == 1
    assert "missing.toml can't be read" in capsys.readouterr().err


def test_the_command_is_installed_and_has_a_version():
    result = subprocess.run([sys.executable, "-m", "exprun.cli", "--version"], capture_output=True, text=True)
    assert result.returncode == 0 and result.stdout.startswith("exprun 0.")


def test_bad_usage_exits_2(capsys):
    with pytest.raises(SystemExit) as exited:
        main(["frobnicate"])
    assert exited.value.code == 2


# --- run ----------------------------------------------------------------------------------------

def test_run_prints_the_run_its_status_and_metrics(project, capsys):
    assert main(["run", str(project.write()), "--runs", str(project.root / "runs"), "--quiet"]) == 0
    captured = capsys.readouterr()
    assert captured.out == "metrics: accuracy=0.9  rows=4  seed=42\n"
    lines = captured.err.splitlines()
    assert lines[0].startswith("run 20") and lines[0].endswith("  demo  (seed 42)")
    assert lines[1].startswith("succeeded in ") and lines[1].endswith(" s")
    assert lines[2].startswith(f"run folder: {project.root / 'runs'}/") and lines[2].endswith("(2 output files)")


def test_run_says_when_the_seed_was_drawn(project, capsys):
    assert main(["run", str(project.write(seed="")), "--runs", str(project.root / "runs"), "--quiet"]) == 0
    assert ", drawn)" in capsys.readouterr().err.splitlines()[0]


def test_run_with_an_override_records_it(project, capsys):
    assert main(["run", str(project.write()), "--runs", str(project.root / "runs"), "--quiet", "--set", "C=2.5"]) == 0
    (folder,) = RunStore(project.root / "runs").all()
    m = folder.read()
    assert m["spec"]["params"]["C"] == 2.5 and m["config"]["overrides"] == ["C=2.5"]


def test_run_works_with_relative_paths_from_another_folder(project, capsys, monkeypatch):
    # The task runs in its config's folder: the run folder and inputs must still be found from there.
    project.write()
    (project.root / "work").mkdir()
    monkeypatch.chdir(project.root / "work")
    assert main(["run", "../exp.toml", "--quiet"]) == 0
    (folder,) = RunStore(project.root / "work" / "runs").all()
    assert folder.read()["metrics"]["rows"] == 4 and (folder.outputs / "model.txt").exists()
    assert main(["show", "latest"]) == 0


def test_a_failed_run_exits_1_with_the_error(project, capsys):
    assert main(["run", str(project.write("boom")), "--runs", str(project.root / "runs"), "--quiet"]) == 1
    err = capsys.readouterr().err
    assert "failed in " in err and "error: ValueError: bad learning rate: -1" in err


def test_run_refuses_an_invalid_config_without_creating_a_run(project, capsys):
    assert main(["run", str(project.write("nope")), "--runs", str(project.root / "runs")]) == 2
    assert "doesn't define a top-level function 'nope'" in capsys.readouterr().err
    assert not (project.root / "runs").exists()
    assert main(["run", str(project.write()), "--runs", str(project.root / "runs"), "--set", "C=x"]) == 2


@pytest.mark.parametrize("value", ["0", "-1", "nan"])
def test_run_refuses_a_bad_time_limit(project, capsys, value):
    assert main(["run", str(project.write()), "--timeout", value]) == 2
    assert "--timeout must be a positive number of seconds" in capsys.readouterr().err


def test_run_with_a_time_limit(project, capsys):
    args = ["run", str(project.write("slow")), "--runs", str(project.root / "runs"), "--quiet", "--timeout", "0.5"]
    assert main(args) == 1
    assert "error: Timeout: stopped after the time limit of 0.5 s" in capsys.readouterr().err


def test_run_reports_an_unreadable_config_and_an_unwritable_runs_folder(project, capsys):
    assert main(["run", str(project.root / "missing.toml")]) == 1
    assert "can't be read" in capsys.readouterr().err
    (project.root / "blocker").write_text("a file, not a folder")
    assert main(["run", str(project.write()), "--runs", str(project.root / "blocker" / "runs")]) == 1
    assert "exprun: " in capsys.readouterr().err


# --- list, show, compare ------------------------------------------------------------------------

def make_run(project, *extra, task="train", config="exp.toml"):
    args = ["run", str(project.write(task, name=config)), "--runs", str(project.root / "runs"), "--quiet", *extra]
    main(args)
    return RunStore(project.root / "runs").all()[-1].id


def runs_args(project):
    return ["--runs", str(project.root / "runs")]


def test_list_with_no_runs_says_how_to_start(project, capsys):
    assert main(["list", *runs_args(project)]) == 0
    assert capsys.readouterr().out == f"no runs in {project.root / 'runs'}: start one with `exprun run CONFIG`\n"


def test_list_shows_runs_newest_first(project, capsys):
    first = make_run(project)
    second = make_run(project, "--set", "C=2.0")
    failed = make_run(project, task="boom", config="boom.toml")
    capsys.readouterr()
    assert main(["list", *runs_args(project)]) == 0
    lines = capsys.readouterr().out.splitlines()
    assert lines[0].split() == ["RUN", "EXPERIMENT", "STATUS", "STARTED", "(UTC)", "DURATION", "METRICS"]
    assert [line.split()[0] for line in lines[1:]] == [failed, second, first]
    assert lines[1].split()[2] == "failed" and "accuracy=0.9  rows=4  seed=42" in lines[2]


def test_list_filters_and_limits(project, capsys):
    make_run(project)
    make_run(project)
    make_run(project, task="boom", config="boom.toml")
    capsys.readouterr()
    assert main(["list", *runs_args(project), "--status", "failed"]) == 0
    assert len(capsys.readouterr().out.splitlines()) == 2
    assert main(["list", *runs_args(project), "--limit", "1"]) == 0
    out = capsys.readouterr().out.splitlines()
    assert len(out) == 3 and out[-1] == "(showing the newest 1 of 3; --limit 0 shows all)"
    assert main(["list", *runs_args(project), "--experiment", "other"]) == 0
    assert capsys.readouterr().out.startswith("no runs matching those filters in ")
    assert main(["list", *runs_args(project), "--limit", "-1"]) == 2


def test_list_as_json(project, capsys):
    run_id = make_run(project)
    capsys.readouterr()
    assert main(["list", *runs_args(project), "--json"]) == 0
    (record,) = json.loads(capsys.readouterr().out)
    assert record["id"] == run_id and record["status"] == "succeeded" and record["metrics"]["rows"] == 4


def test_list_records_a_dead_runner_and_survives_a_broken_manifest(project, capsys):
    dead = make_run(project)
    folder = RunStore(project.root / "runs").get(dead)
    folder.write({**folder.read(), "status": "running"})        # as if the runner had been killed
    broken = make_run(project)
    (project.root / "runs" / broken / "manifest.json").write_text("{oops")
    capsys.readouterr()
    assert main(["list", *runs_args(project)]) == 0
    out = capsys.readouterr().out
    assert f"{broken}  –           unreadable" in out and "interrupted" in out
    assert folder.read()["status"] == "interrupted" and folder.read()["error"]["type"] == "RunnerDied"


def test_show_by_prefix_or_latest(project, capsys):
    run_id = make_run(project)
    capsys.readouterr()
    for reference in (run_id, run_id[:18], "latest"):
        assert main(["show", reference, *runs_args(project)]) == 0
        out = capsys.readouterr().out
        assert out.startswith(f"run         {run_id}\nstatus      succeeded\n") and "  accuracy  0.9" in out
    assert main(["show", "latest", *runs_args(project), "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["id"] == run_id


@pytest.mark.parametrize("reference, code, message", [
    ("2099", 2, "no run in"),
    ("../etc", 2, "isn't a run id"),
])
def test_show_refuses_unknown_runs(project, capsys, reference, code, message):
    make_run(project)
    capsys.readouterr()
    assert main(["show", reference, *runs_args(project)]) == code
    assert message in capsys.readouterr().err


def test_show_and_compare_report_broken_and_incomplete_runs(project, capsys):
    good = make_run(project)
    broken = make_run(project)
    (project.root / "runs" / broken / "manifest.json").write_text('{"schema": 99}')
    capsys.readouterr()
    assert main(["show", broken, *runs_args(project)]) == 1
    assert "has schema version 99" in capsys.readouterr().err
    assert main(["compare", good, broken, *runs_args(project)]) == 1
    (project.root / "runs" / broken / "manifest.json").unlink()
    assert main(["show", broken, *runs_args(project)]) == 1
    assert "has no manifest yet (incomplete)" in capsys.readouterr().err


def test_compare_shows_what_differs(project, capsys):
    first = make_run(project)
    second = make_run(project)
    changed = make_run(project, "--set", "C=2.0")
    capsys.readouterr()
    assert main(["compare", first, second, *runs_args(project)]) == 0
    assert "same fingerprint and the same metrics: the two runs reproduce" in capsys.readouterr().out
    assert main(["compare", first, changed, *runs_args(project)]) == 0
    out = capsys.readouterr().out
    assert "different fingerprints: the config differs" in out and "  C  1.0  2.0" in out
    assert main(["compare", first, second, "latest", *runs_args(project)]) == 0     # latest is the changed run
    out = capsys.readouterr().out
    assert f"  C  {changed}" in out and "     A    B    C\n  C  1.0  1.0  2.0" in out


def test_compare_needs_two_different_runs(project, capsys):
    run_id = make_run(project)
    capsys.readouterr()
    assert main(["compare", run_id, *runs_args(project)]) == 2
    assert "needs at least two runs" in capsys.readouterr().err
    assert main(["compare", run_id, "latest", *runs_args(project)]) == 2
    assert "the same run is named twice" in capsys.readouterr().err


def test_compare_as_json_has_no_placeholders(project, capsys):
    first = make_run(project)
    second = make_run(project, "--set", "C=2.0")
    capsys.readouterr()
    assert main(["compare", first, second, *runs_args(project), "--json", "--all"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["runs"] == [first, second] and data["same_fingerprint"] is False
    assert data["differing_parts"] == ["config"]
    params = {row["key"]: row for row in data["sections"]["params"]["rows"]}
    assert params["C"]["values"] == [1.0, 2.0] and params["C"]["same"] is False and params["sleep"]["same"] is True


# --- rerun --------------------------------------------------------------------------------------

def test_rerun_of_a_deterministic_run_reproduces_identically(project, capsys):
    original = make_run(project)
    capsys.readouterr()
    assert main(["rerun", original, *runs_args(project), "--quiet"]) == 0
    captured = capsys.readouterr()
    assert captured.out == "reproduced: identical (3 metrics equal, 2 output files byte-identical)\n"
    assert captured.err.startswith(f"rerun of {original} (seed 42) as run ")
    rerun = RunStore(project.root / "runs").all()[-1].read()
    assert rerun["rerun_of"] == original and rerun["reproduction"]["outcome"] == "identical"
    assert rerun["reproduction"]["of"] == original and rerun["reproduction"]["code_changes"] == []
    assert main(["show", "latest", *runs_args(project)]) == 0
    assert f"rerun of    {original}: identical" in capsys.readouterr().out


def test_a_rerun_records_the_original_overrides(project, capsys):
    original = make_run(project, "--set", "C=2.5")
    assert main(["rerun", original, *runs_args(project), "--quiet"]) == 0
    rerun = RunStore(project.root / "runs").all()[-1].read()
    assert rerun["config"]["overrides"] == ["C=2.5"] and rerun["spec"]["params"]["C"] == 2.5


def test_rerun_of_a_task_that_ignores_its_seed_is_not_reproduced(careless_project, capsys):
    project = careless_project
    main(["run", str(project.root / "careless.toml"), "--runs", str(project.root / "runs"), "--quiet"])
    original = RunStore(project.root / "runs").all()[-1].id
    capsys.readouterr()
    assert main(["rerun", original, *runs_args(project), "--quiet"]) == 3
    out = capsys.readouterr().out
    assert out.startswith("NOT reproduced: different\n  score: ")
    assert "config, data, code and environment are the same as recorded: the task itself isn't deterministic" in out
    assert main(["rerun", original, *runs_args(project), "--quiet", "--tolerance", "0.6"]) == 0
    assert capsys.readouterr().out.startswith("reproduced: within tolerance (largest relative difference ")


def test_rerun_shows_what_else_differs(project, capsys, monkeypatch):
    original = make_run(project)
    monkeypatch.setenv("OMP_NUM_THREADS", "3")
    (project.root / "tasks.py").write_text((project.root / "tasks.py").read_text().replace(
        '"accuracy": 0.9', '"accuracy": 0.8'))
    capsys.readouterr()
    assert main(["rerun", original, *runs_args(project), "--quiet", "--allow-code-change"]) == 3
    captured = capsys.readouterr()
    assert "note: tasks.py changed (source " in captured.err
    out = captured.out
    assert "NOT reproduced: different\n  accuracy: 0.9 -> 0.8  (relative difference 0.111)" in out
    assert "what else differs:" in out and "source" in out and "$OMP_NUM_THREADS  –         3" in out


@pytest.mark.parametrize("value", ["-0.1", "1", "nan"])
def test_rerun_refuses_a_bad_tolerance(project, capsys, value):
    assert main(["rerun", "latest", *runs_args(project), "--tolerance", value]) == 2
    assert "--tolerance must be a relative difference" in capsys.readouterr().err


def test_rerun_refuses_what_it_cannot_reproduce(project, capsys):
    failed = make_run(project, task="boom", config="boom.toml")
    capsys.readouterr()
    assert main(["rerun", failed, *runs_args(project)]) == 2
    assert f"can't rerun {failed}: run {failed} failed; only a run that succeeded" in capsys.readouterr().err
    good = make_run(project)
    (project.root / "data.csv").write_text("changed\n")
    capsys.readouterr()
    assert main(["rerun", good, *runs_args(project)]) == 2
    assert "input data (data.csv) has changed since the run" in capsys.readouterr().err
    assert main(["rerun", "2099", *runs_args(project)]) == 2
    assert main(["rerun", good, *runs_args(project), "--timeout", "0"]) == 2


def test_a_rerun_that_fails_is_not_reproduced(project, capsys):
    original = make_run(project)
    (project.root / "tasks.py").write_text((project.root / "tasks.py").read_text().replace(
        "def train(ctx):", "def train(ctx):\n    raise RuntimeError('broken now')"))
    capsys.readouterr()
    assert main(["rerun", original, *runs_args(project), "--quiet", "--allow-code-change"]) == 3
    out = capsys.readouterr().out
    assert out.startswith("NOT reproduced: failed\n  the rerun failed: RuntimeError: broken now")


# --- demo ---------------------------------------------------------------------------------------

def test_demo_needs_scikit_learn(tmp_path, capsys, monkeypatch):
    monkeypatch.setitem(sys.modules, "sklearn.datasets", None)       # as if it weren't installed
    assert main(["demo", str(tmp_path / "demo")]) == 2
    assert 'the demo needs scikit-learn and NumPy (sklearn.datasets is missing): pip install' in capsys.readouterr().err


# --- sweep --------------------------------------------------------------------------------------

def sweep(project, *extra, task="picky"):
    return main(["sweep", str(project.write(task)), "--runs", str(project.root / "runs"), *extra])


def test_sweep_runs_every_combination_and_prints_a_table(project, capsys):
    assert sweep(project, "--grid", "C=1.0,2.0", "--grid", "seed=3,4", "--workers", "2") == 0
    captured = capsys.readouterr()
    err = captured.err.splitlines()
    assert not any(line.startswith("[") for line in err)        # runs in parallel aren't announced one by one
    assert err[0].startswith("sweep 20") and err[0].endswith(": 4 runs (2 x 2), 2 at a time, swept")
    assert sum("done) C=" in line and "succeeded in" in line for line in err) == 4
    assert err[-1].endswith(": all 4 runs succeeded")
    lines = captured.out.splitlines()
    assert lines[1].split() == ["RUN", "C", "seed", "STATUS", "DURATION", "C", "seed"]
    assert [line.split()[1:4] for line in lines[2:]] == [["1.0", "3", "succeeded"], ["1.0", "4", "succeeded"],
                                                        ["2.0", "3", "succeeded"], ["2.0", "4", "succeeded"]]


def test_sweep_with_one_worker_names_each_run_as_it_starts_and_echoes_its_output(project, capsys):
    assert sweep(project, "--grid", "C=1.0,2.0", task="train") == 0
    captured = capsys.readouterr()
    assert "[1/2] run 20" in captured.err and "[2/2] run 20" in captured.err
    assert "training on 4 rows, C=1.0" in captured.out and "training on 4 rows, C=2.0" in captured.out


def test_parallel_runs_dont_echo_their_output(project, capsys):
    assert sweep(project, "--grid", "C=1.0,2.0", "--workers", "2", task="train") == 0
    captured = capsys.readouterr()
    assert "training on" not in captured.out and "a note on stderr" not in captured.err


def test_sweep_sorts_by_a_metric(project, capsys):
    assert sweep(project, "--grid", "C=2.0,1.0,3.0", "--sort", "C", "--descending") == 2     # C is swept
    assert "--sort takes a metric; C is a swept param" in capsys.readouterr().err
    assert sweep(project, "--grid", "seed=2,1,3", "--sort", "seed", "--descending") == 2
    capsys.readouterr()
    assert sweep(project, "--grid", "C=2.0,1.0,3.0", "--set", "sleep=0", "--sort", "seed") == 0
    rows = capsys.readouterr().out.splitlines()[2:]
    assert [row.split()[1] for row in rows] == ["2.0", "1.0", "3.0"]            # all equal: grid order kept


def test_sweep_exits_1_if_any_run_fails(project, capsys):
    assert sweep(project, "--grid", "C=1.0,-1.0") == 1
    captured = capsys.readouterr()
    assert "ValueError: C must not be negative" in captured.err
    assert "1 run did not succeed" in captured.err and "failed" in captured.out


@pytest.mark.parametrize("extra, message", [
    (["--grid", "C=1.0", "--workers", "0"], "--workers must be 1 or more"),
    (["--grid", "C=1.0", "--timeout", "0"], "--timeout must be a positive number"),
    (["--grid", "C"], "must look like key.path=value"),
    (["--grid", "D=1,2"], "--grid D: params has no D"),
    (["--grid", "C=1.0", "--set", "C=2.0"], "C is given with both --set and --grid"),
])
def test_sweep_refuses_bad_usage_before_running_anything(project, capsys, extra, message):
    assert sweep(project, *extra) == 2
    assert message in capsys.readouterr().err
    assert not (project.root / "runs").exists()


def test_sweep_needs_a_grid(project, capsys):
    with pytest.raises(SystemExit):
        sweep(project)
    assert "--grid" in capsys.readouterr().err


def test_sweep_exits_2_if_no_point_could_start(project, capsys):
    (project.root / "tasks.py").write_text("x = 1\n")       # the task is gone: refused at planning
    assert sweep(project, "--grid", "C=1.0,2.0") == 2
    assert "doesn't define a top-level function 'picky'" in capsys.readouterr().err


def test_sweep_checks_its_inputs_before_running_anything(project, capsys):
    (project.root / "data.csv").unlink()
    assert sweep(project, "--grid", "C=1.0,2.0") == 2
    assert "data.csv" in capsys.readouterr().err and not (project.root / "runs").exists()


def test_a_run_from_a_seed_sweep_reruns_with_its_own_seed(project, capsys):
    assert sweep(project, "--grid", "seed=11,12") == 0
    capsys.readouterr()
    assert main(["rerun", "latest", "--runs", str(project.root / "runs"), "--quiet"]) == 0
    assert "reproduced: identical" in capsys.readouterr().out
    rerun = RunStore(project.root / "runs").all()[-1].read()
    assert rerun["spec"]["seed"] == 12 and rerun["metrics"]["seed"] == 12 and rerun["sweep"] is None


def test_sweep_results_shows_a_past_sweep(project, capsys):
    runs = str(project.root / "runs")
    assert main(["sweep-results", "--runs", runs]) == 2
    assert "no sweeps in" in capsys.readouterr().err
    assert sweep(project, "--grid", "C=1.0,3.0,2.0") == 0
    capsys.readouterr()
    assert main(["sweep-results", "--runs", runs, "--sort", "C", "--descending"]) == 2
    assert main(["sweep-results", "--runs", runs, "--sort", "seed"]) == 0
    out = capsys.readouterr().out.splitlines()
    assert out[0].startswith("sweep 20") and out[0].endswith(": 3 of 3 runs recorded")
    assert [line.split()[1] for line in out[2:]] == ["1.0", "3.0", "2.0"]
    assert main(["sweep-results", "nope", "--runs", runs]) == 2
    assert "no sweep 'nope'" in capsys.readouterr().err


def test_sweep_results_as_json(project, capsys):
    assert sweep(project, "--grid", "C=1.0,2.0") == 0
    capsys.readouterr()
    assert main(["sweep-results", "latest", "--runs", str(project.root / "runs"), "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["grid"] == {"C": [1.0, 2.0]} and data["size"] == 2
    assert [(r["point"], r["status"], r["metrics"]) for r in data["runs"]] == [
        ({"C": 1.0}, "succeeded", {"C": 1.0, "seed": 42}), ({"C": 2.0}, "succeeded", {"C": 2.0, "seed": 42})]
