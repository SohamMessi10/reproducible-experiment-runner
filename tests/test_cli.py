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
