import os
import textwrap
from types import SimpleNamespace

import pytest
from hypothesis import settings

# Local runs stay fast; CI tries more random cases to search harder for counterexamples.
settings.register_profile("dev", max_examples=100)
settings.register_profile("ci", max_examples=1000)
settings.load_profile(os.environ.get("HYPOTHESIS_PROFILE", "dev"))

TASKS = '''
import json, math, os, signal, sys, time
from pathlib import Path


def train(ctx):
    rows = ctx.inputs["data"].read_text().split()
    print(f"training on {len(rows)} rows, C={ctx.params['C']}")
    print("a note on stderr", file=sys.stderr)
    (ctx.output_dir / "model.txt").write_text(f"seed {ctx.seed}\\n")
    (ctx.output_dir / "plots").mkdir()
    (ctx.output_dir / "plots" / "curve.csv").write_text("1,2\\n")
    return {"accuracy": 0.9, "rows": len(rows), "seed": ctx.seed}


def inspect(ctx):
    manifest = json.loads((ctx.output_dir.parent / "manifest.json").read_text())
    return {"cwd": os.getcwd(), "status_while_running": manifest["status"], "hash_abc": hash("abc"),
            "run_id": ctx.run_id, "hashseed": os.environ.get("PYTHONHASHSEED", "")}


def boom(ctx):
    raise ValueError("bad learning rate: -1")


def nothing(ctx):
    pass


def bad_metrics(ctx):
    return {"loss": math.nan}


def exits(ctx):
    sys.exit(3)


def exits_zero(ctx):
    sys.exit(0)


def vanishes(ctx):
    os._exit(0)


def killed(ctx):
    os.kill(os.getpid(), signal.SIGKILL)


def slow(ctx):
    print("started")
    Path(os.environ.get("EXPRUN_TEST_MARK", "started.mark")).write_text(str(os.getpid()))
    time.sleep(float(ctx.params.get("sleep", 30)))
    return {"slept": True}


def stubborn(ctx):
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    Path(os.environ.get("EXPRUN_TEST_MARK", "started.mark")).write_text(str(os.getpid()))
    time.sleep(60)
'''

CONFIG = """\
[experiment]
name = "demo"
task = "tasks:{task}"
{seed}

[inputs]
data = "data.csv"

[params]
C = 1.0
sleep = {sleep}
"""


@pytest.fixture
def project(tmp_path):
    """A folder (`.root`) with tasks.py and a data file, and `.write(task, ...)` to make a config."""
    (tmp_path / "tasks.py").write_text(textwrap.dedent(TASKS))
    (tmp_path / "data.csv").write_text("x\n1\n2\n3\n")

    def write(task="train", seed="seed = 42", sleep=30, name="exp.toml"):
        path = tmp_path / name
        path.write_text(CONFIG.format(task=task, seed=seed, sleep=sleep))
        return path

    return SimpleNamespace(root=tmp_path, write=write)


CARELESS = '''
import random


def careless(ctx):
    return {"score": 0.5 + random.random() / 2}        # in [0.5, 1): any two differ by under 50%
'''


@pytest.fixture
def careless_project(project):
    """The project, plus a task that ignores its seed."""
    (project.root / "careless.py").write_text(CARELESS)
    (project.root / "careless.toml").write_text('[experiment]\nname = "careless"\ntask = "careless:careless"\nseed = 1\n')
    return project
