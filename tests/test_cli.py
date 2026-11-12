import json
import subprocess
import sys

import pytest

from exprun.cli import run as main

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
def project(tmp_path):
    (tmp_path / "data" / "images").mkdir(parents=True)
    (tmp_path / "data" / "train.csv").write_text("a,b\n1,2\n")
    (tmp_path / "data" / "images" / "1.txt").write_text("x")
    (tmp_path / "data" / "images" / "2.txt").write_text("y")
    (tmp_path / "tasks.py").write_text("def train(ctx):\n    return {}\n")
    (tmp_path / "exp.toml").write_text(CONFIG)
    return tmp_path


def test_check_shows_what_identifies_the_run(project, capsys):
    assert main(["check", str(project / "exp.toml")]) == 0
    out = capsys.readouterr().out
    for expected in (f"config:      {project / 'exp.toml'}", "experiment:  digits-1 (a baseline)",
                     "task:        tasks:train  (tasks.py", "seed:        7", 'params:      {"model": {"C": 1.0}}',
                     "train   data/train.csv  (8 bytes)", "images  data/images  (2 bytes, 2 files)",
                     "config hash: ", "fingerprint: "):
        assert expected in out, expected


def test_check_as_json_gives_the_full_hashes(project, capsys):
    assert main(["check", str(project / "exp.toml"), "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["name"] == "digits-1" and data["seed"] == 7 and data["description"] == "a baseline"
    assert len(data["fingerprint"]) == len("sha256:") + 64
    assert data["inputs"]["images"]["kind"] == "folder" and data["code"]["function"] == "train"


def test_an_override_changes_the_fingerprint(project, capsys):
    def fingerprint(*extra):
        assert main(["check", str(project / "exp.toml"), "--json", *extra]) == 0
        return json.loads(capsys.readouterr().out)["fingerprint"]

    base = fingerprint()
    assert fingerprint("--set", "model.C=2.0") != base
    assert fingerprint("--set", "model.C=1.0") == base


def test_check_without_a_seed_says_when_it_is_drawn(project, capsys):
    (project / "exp.toml").write_text(CONFIG.replace("seed = 7\n", ""))
    assert main(["check", str(project / "exp.toml")]) == 0
    out = capsys.readouterr().out
    assert "seed:        not set: one is drawn and recorded at run time" in out
    assert "fingerprint: computed at run time, once the seed is drawn" in out


def test_check_names_the_git_commit_and_uncommitted_changes(project, capsys):
    def git(*args):
        subprocess.run(["git", "-C", str(project), "-c", "user.name=t", "-c", "user.email=t@example.com",
                        "-c", "commit.gpgsign=false", *args], check=True, capture_output=True)

    git("init", "-q")
    git("add", "-A")
    git("commit", "-qm", "first")
    assert main(["check", str(project / "exp.toml")]) == 0
    line = next(x for x in capsys.readouterr().out.splitlines() if x.startswith("task:"))
    assert ", git " in line and "uncommitted" not in line
    (project / "tasks.py").write_text("def train(ctx):\n    return {'v': 2}\n")
    assert main(["check", str(project / "exp.toml")]) == 0
    assert "+ uncommitted changes" in capsys.readouterr().out


def test_check_names_an_installed_task_package(project, capsys):
    (project / "exp.toml").write_text(CONFIG.replace('"tasks:train"', '"hypothesis:given"'))
    assert main(["check", str(project / "exp.toml")]) == 0
    assert "(__init__.py, hypothesis 6." in capsys.readouterr().out


def test_check_without_inputs(project, capsys):
    (project / "exp.toml").write_text('[experiment]\nname = "x"\ntask = "tasks:train"\nseed = 1\n')
    assert main(["check", str(project / "exp.toml")]) == 0
    assert "inputs:      none" in capsys.readouterr().out


@pytest.mark.parametrize("edit, args, message", [
    (lambda t: t.replace("seed = 7", "sed = 7"), [], "unknown key experiment.sed (did you mean seed?)"),
    (lambda t: t.replace('"tasks:train"', '"tasks:trian"'), [], "doesn't define a top-level function 'trian'"),
    (lambda t: t.replace('"tasks:train"', '"missing:train"'), [], "task module 'missing' not found"),
    (lambda t: t, ["--set", "model.c=1"], "params has no model.c (did you mean model.C?)"),
    (lambda t: t.replace("data/train.csv", "data/nope.csv"), [], "inputs.train: data/nope.csv doesn't exist"),
])
def test_invalid_configs_exit_2_before_anything_runs(project, capsys, edit, args, message):
    (project / "exp.toml").write_text(edit(CONFIG))
    assert main(["check", str(project / "exp.toml"), *args]) == 2
    captured = capsys.readouterr()
    assert captured.out == "" and message in captured.err and captured.err.startswith("exprun: ")


def test_an_input_that_cannot_be_hashed_exits_2(project, capsys):
    (project / "data" / "images" / "link").symlink_to(project / "data")
    assert main(["check", str(project / "exp.toml")]) == 2
    assert "symlink to a folder inside an input" in capsys.readouterr().err


def test_an_unreadable_config_exits_1(project, capsys):
    assert main(["check", str(project / "missing.toml")]) == 1
    assert "missing.toml can't be read" in capsys.readouterr().err


def test_the_command_is_installed_and_has_a_version():
    result = subprocess.run([sys.executable, "-m", "exprun.cli", "--version"], capture_output=True, text=True)
    assert result.returncode == 0 and result.stdout.startswith("exprun 0.")


def test_bad_usage_exits_2(capsys):
    with pytest.raises(SystemExit) as exited:
        main(["frobnicate"])
    assert exited.value.code == 2
