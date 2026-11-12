import pytest

from exprun.config import load
from exprun.identity import identify

CONFIG = """\
[experiment]
name = "{name}"
task = "tasks:train"
{seed}

[inputs]
train = "{train}"

[params]
C = 1.0
"""


@pytest.fixture
def project(tmp_path):
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "train.csv").write_text("a,b\n1,2\n")
    (tmp_path / "tasks.py").write_text("def train(ctx):\n    return {}\n")
    return tmp_path


def config(project, name="exp", seed="seed = 7", train="data/train.csv", file="exp.toml", overrides=None):
    path = project / file
    path.write_text(CONFIG.format(name=name, seed=seed, train=train))
    return load(path, overrides)


def test_identity_records_spec_inputs_code_and_fingerprint(project):
    identity = identify(config(project))
    assert identity.spec == {"task": "tasks:train", "seed": 7, "params": {"C": 1.0}}
    assert identity.inputs["train"]["path"] == "data/train.csv" and identity.inputs["train"]["files"] == 1
    assert identity.code["module"] == "tasks"
    assert identity.config_hash.startswith("sha256:") and identity.fingerprint.startswith("sha256:")
    assert set(identity.to_dict()) == {"spec", "config_hash", "inputs", "code", "fingerprint"}


def test_labels_and_paths_do_not_change_the_fingerprint(project):
    base = identify(config(project)).fingerprint
    assert identify(config(project, name="renamed", file="other.toml")).fingerprint == base
    (project / "moved").mkdir()
    (project / "moved" / "copy.csv").write_bytes((project / "data" / "train.csv").read_bytes())
    assert identify(config(project, train="moved/copy.csv")).fingerprint == base


def test_the_computation_data_and_code_do(project):
    base = identify(config(project))
    assert identify(config(project, seed="seed = 8")).fingerprint != base.fingerprint
    assert identify(config(project, overrides=["C=2.0"])).fingerprint != base.fingerprint
    (project / "data" / "train.csv").write_text("a,b\n1,3\n")            # same name, new content
    assert identify(config(project)).fingerprint != base.fingerprint
    (project / "data" / "train.csv").write_text("a,b\n1,2\n")
    assert identify(config(project)).fingerprint == base.fingerprint
    (project / "tasks.py").write_text("def train(ctx):\n    return {'v': 2}\n")
    assert identify(config(project)).fingerprint != base.fingerprint


def test_without_a_seed_the_hashes_wait_for_the_drawn_one(project):
    unseeded = config(project, seed="")
    pending = identify(unseeded)
    assert pending.spec is None and pending.config_hash is None and pending.fingerprint is None
    assert pending.inputs and pending.code
    drawn = identify(unseeded, seed=7)
    assert drawn.fingerprint == identify(config(project)).fingerprint     # same as seed = 7 in the config
