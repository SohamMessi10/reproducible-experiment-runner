import copy

import pytest

from exprun.config import load
from exprun.reproduce import CannotRerun, judge, prepare, relative_difference
from exprun.runner import execute
from exprun.store import RunStore

SHA_A, SHA_B = "sha256:" + "a" * 64, "sha256:" + "b" * 64


def result(metrics, outputs=None, status="succeeded"):
    return {"status": status, "metrics": metrics, "outputs": outputs if outputs is not None else {"p.csv": {"sha256": SHA_A}}}


BASE = result({"accuracy": 0.95, "n": 100, "converged": True, "solver": "lbfgs"})


def test_identical_means_every_metric_and_output_equal():
    verdict = judge(BASE, copy.deepcopy(BASE))
    assert verdict["outcome"] == "identical" and verdict["reproduced"]
    assert verdict["metric_differences"] == [] and verdict["output_differences"] == []


def test_equal_metrics_with_a_different_output_file_is_not_reproduced():
    verdict = judge(BASE, result(BASE["metrics"], {"p.csv": {"sha256": SHA_B}}))
    assert verdict["outcome"] == "metrics identical, outputs differ" and not verdict["reproduced"]
    assert verdict["output_differences"] == ["p.csv"]


@pytest.mark.parametrize("outputs", [{}, {"p.csv": {"sha256": SHA_A}, "extra.txt": {"sha256": SHA_A}}])
def test_a_missing_or_extra_output_file_counts_as_a_difference(outputs):
    assert judge(BASE, result(BASE["metrics"], outputs))["outcome"] == "metrics identical, outputs differ"


def test_any_metric_change_without_a_tolerance_is_different():
    verdict = judge(BASE, result({**BASE["metrics"], "accuracy": 0.95 + 1e-15}))
    assert verdict["outcome"] == "different" and not verdict["reproduced"]
    (entry,) = verdict["metric_differences"]
    assert entry["metric"] == "accuracy" and entry["relative_difference"] == pytest.approx(1.05e-15, rel=0.1)


def test_a_tolerance_judges_numeric_metrics_only():
    close = result({**BASE["metrics"], "accuracy": 0.95 * (1 + 1e-10), "n": 100}, {"p.csv": {"sha256": SHA_B}})
    verdict = judge(BASE, close, tolerance=1e-9)
    assert verdict["outcome"] == "within tolerance" and verdict["reproduced"]
    assert verdict["output_differences"] == ["p.csv"]                 # reported, not judged
    assert verdict["max_relative_difference"] == pytest.approx(1e-10, rel=1e-3)
    assert judge(BASE, close, tolerance=1e-11)["outcome"] == "different"


def test_a_tolerance_also_accepts_equal_metrics_with_differing_outputs():
    verdict = judge(BASE, result(BASE["metrics"], {"p.csv": {"sha256": SHA_B}}), tolerance=1e-9)
    assert verdict["outcome"] == "within tolerance" and verdict["max_relative_difference"] == 0.0


@pytest.mark.parametrize("change", [{"solver": "saga"}, {"converged": False}])
def test_string_or_boolean_changes_are_never_within_tolerance(change):
    verdict = judge(BASE, result({**BASE["metrics"], **change}), tolerance=0.5)
    assert verdict["outcome"] == "different"


def test_an_int_that_became_an_equal_float_differs_exactly_but_is_within_any_tolerance():
    retyped = result({**BASE["metrics"], "n": 100.0})
    assert judge(BASE, retyped)["outcome"] == "different"                  # exact: types must match too
    verdict = judge(BASE, retyped, tolerance=1e-12)                         # a tolerance compares numbers
    assert verdict["outcome"] == "within tolerance" and verdict["max_relative_difference"] == 0.0


def test_a_missing_or_new_metric_is_different_even_with_a_tolerance():
    fewer = {k: v for k, v in BASE["metrics"].items() if k != "n"}
    assert judge(BASE, result(fewer), tolerance=0.5)["outcome"] == "different"
    assert judge(BASE, result({**BASE["metrics"], "extra": 1}), tolerance=0.5)["outcome"] == "different"


def test_a_rerun_that_failed_is_reported_as_failed():
    verdict = judge(BASE, result(None, {}, status="failed"))
    assert verdict["outcome"] == "failed" and not verdict["reproduced"]


def test_relative_difference():
    assert relative_difference(0.0, 0.0) == 0.0
    assert relative_difference(1.0, 1.1) == pytest.approx(0.1 / 1.1)
    assert relative_difference(-2.0, 2.0) == 2.0


# --- preparing a rerun, with real runs ----------------------------------------------------------

def original_run(project, task="train", seed="seed = 42", overrides=None):
    config = load(project.write(task, seed=seed), overrides)
    folder, manifest = execute(config, RunStore(project.root / "runs"), overrides=overrides, quiet=True)
    return folder, manifest


def test_a_rerun_uses_the_saved_config_its_overrides_and_its_seed(project):
    folder, m = original_run(project, seed="", overrides=["C=2.5"])
    project.write().write_text("this file is now broken")              # later edits don't matter
    config, changes = prepare(m, folder.path)
    assert changes == [] and config.params["C"] == 2.5 and config.source == (folder.path / "config.toml").read_bytes()
    _, again = execute(config, RunStore(project.root / "runs"), overrides=m["config"]["overrides"], quiet=True,
                       rerun_of=m)
    assert again["spec"]["seed"] == m["spec"]["seed"] and again["seed_source"] == "drawn"
    assert again["rerun_of"] == m["id"] and again["fingerprint"] == m["fingerprint"]
    assert judge(m, again)["outcome"] == "identical"


def test_only_a_succeeded_run_can_be_rerun(project):
    folder, m = original_run(project, task="boom")
    with pytest.raises(CannotRerun, match="failed; only a run that succeeded has a result to reproduce"):
        prepare(m, folder.path)


def test_changed_or_missing_data_is_refused(project):
    folder, m = original_run(project)
    (project.root / "data.csv").write_text("x\n1\n2\n4\n")
    with pytest.raises(CannotRerun, match=r"input data \(data.csv\) has changed since the run: it was \w{12} and is now"):
        prepare(m, folder.path)
    (project.root / "data.csv").unlink()
    with pytest.raises(CannotRerun, match="saved config can't be used any more: .*inputs.data: data.csv doesn't exist"):
        prepare(m, folder.path)


def test_changed_code_is_refused_unless_allowed(project):
    folder, m = original_run(project)
    tasks = project.root / "tasks.py"
    tasks.write_text(tasks.read_text() + "\n# a later edit\n")
    with pytest.raises(CannotRerun, match=r"tasks.py changed \(source \w{12} -> \w{12}\).*--allow-code-change"):
        prepare(m, folder.path)
    _, changes = prepare(m, folder.path, allow_code_change=True)
    assert len(changes) == 1 and changes[0].startswith("tasks.py changed")


def test_a_task_that_no_longer_exists_is_refused(project):
    folder, m = original_run(project)
    tasks = project.root / "tasks.py"
    tasks.write_text(tasks.read_text().replace("def train(", "def renamed("))
    with pytest.raises(CannotRerun, match="the task can't be found any more"):
        prepare(m, folder.path, allow_code_change=True)


def test_a_missing_saved_config_is_refused(project):
    folder, m = original_run(project)
    (folder.path / "config.toml").unlink()
    with pytest.raises(CannotRerun, match="the run's saved config can't be used any more"):
        prepare(m, folder.path)


def test_a_difference_exactly_at_the_tolerance_is_within_it():
    verdict = judge(result({"x": 1.0}), result({"x": 0.5}), tolerance=0.5)        # relative difference 0.5
    assert verdict["outcome"] == "within tolerance"


def git(folder, *args):
    import subprocess

    subprocess.run(["git", "-C", str(folder), "-c", "user.name=t", "-c", "user.email=t@example.com",
                    "-c", "commit.gpgsign=false", *args], check=True, capture_output=True)


def test_a_new_commit_or_other_uncommitted_edits_count_as_code_changes(project):
    (project.root / ".gitignore").write_text("runs/\n__pycache__/\n")
    (project.root / "helper.py").write_text("SCALE = 1\n")
    git(project.root, "init", "-q")
    git(project.root, "add", "-A")
    git(project.root, "commit", "-qm", "first")
    folder, m = original_run(project)
    (project.root / "helper.py").write_text("SCALE = 2\n")                    # tasks.py itself is untouched
    with pytest.raises(CannotRerun, match="the uncommitted changes differ"):
        prepare(m, folder.path)
    git(project.root, "commit", "-qam", "second")
    with pytest.raises(CannotRerun, match=r"the git commit changed \(\w{7} -> \w{7}\)"):
        prepare(m, folder.path)
