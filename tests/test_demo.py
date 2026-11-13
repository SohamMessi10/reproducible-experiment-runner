"""The demo end to end: export, run, rerun. Skipped where the demo extra isn't installed."""

import json

import pytest

from exprun.cli import run as main
from exprun.store import RunStore

pytest.importorskip("sklearn")


@pytest.fixture(scope="module")
def demo(tmp_path_factory):
    folder = tmp_path_factory.mktemp("demo")
    assert main(["demo", str(folder)]) == 0
    return folder


def test_the_demo_folder_has_data_attribution_and_configs(demo):
    lines = (demo / "data" / "digits.csv").read_text().splitlines()
    assert len(lines) == 1798 and lines[0].startswith("pixel_0,pixel_1,") and lines[0].endswith(",label")
    assert all(len(line.split(",")) == 65 for line in lines[1:])
    source = (demo / "data" / "SOURCE.txt").read_text()
    assert "https://doi.org/10.24432/C50P49" in source and "CC BY 4.0" in source
    assert (demo / "logistic.toml").is_file() and (demo / "forest.toml").is_file()


def test_the_demo_never_overwrites(demo, capsys):
    assert main(["demo", str(demo)]) == 2
    assert "already exists; choose an empty folder" in capsys.readouterr().err


@pytest.mark.parametrize("config, extra, expected", [
    ("logistic.toml", [], {"n_train": 1347, "n_test": 450, "converged": True}),
    ("forest.toml", ["--set", "forest.n_estimators=20"], {"n_train": 1347, "n_test": 450}),
])
def test_each_model_runs_and_reproduces(demo, capsys, config, extra, expected):
    runs = ["--runs", str(demo / "runs")]
    assert main(["run", str(demo / config), *runs, "--quiet", *extra]) == 0
    original = RunStore(demo / "runs").all()[-1].read()
    metrics = original["metrics"]
    assert {k: metrics[k] for k in expected} == expected and 0.9 < metrics["accuracy"] <= 1.0
    assert set(original["outputs"]) == {"probabilities.csv"}
    probabilities = (demo / "runs" / original["id"] / "outputs" / "probabilities.csv").read_text().splitlines()
    assert len(probabilities) == 450 and len(probabilities[0].split(",")) == 10
    capsys.readouterr()
    assert main(["rerun", original["id"], *runs, "--quiet"]) == 0
    assert capsys.readouterr().out.startswith("reproduced: identical")


def test_an_unknown_model_fails_the_run_with_a_reason(demo, capsys):
    runs = ["--runs", str(demo / "runs")]
    assert main(["run", str(demo / "logistic.toml"), *runs, "--quiet", "--set", "model='svm'"]) == 1
    m = RunStore(demo / "runs").all()[-1].read()
    assert m["error"]["type"] == "ValueError" and "params.model must be" in m["error"]["message"]
    assert json.loads(json.dumps(m))["status"] == "failed"


def test_probabilities_are_written_at_full_precision(demo, capsys):
    runs = ["--runs", str(demo / "runs")]
    assert main(["run", str(demo / "logistic.toml"), *runs, "--quiet"]) == 0
    m = RunStore(demo / "runs").all()[-1].read()
    text = (demo / "runs" / m["id"] / "outputs" / "probabilities.csv").read_text()
    values = [v for line in text.splitlines() for v in line.split(",")]
    digits = max(len(v.split("e")[0].replace(".", "").replace("-", "").lstrip("0")) for v in values)
    assert digits >= 16                                 # 17 significant digits round-trip any double


def test_too_few_iterations_are_reported_as_not_converged(demo, capsys):
    runs = ["--runs", str(demo / "runs")]
    assert main(["run", str(demo / "logistic.toml"), *runs, "--quiet", "--set", "logistic.max_iter=5"]) == 0
    m = RunStore(demo / "runs").all()[-1].read()
    assert m["metrics"]["converged"] is False and m["metrics"]["iterations"] == 5
