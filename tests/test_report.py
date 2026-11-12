import copy

import pytest

from exprun.report import (
    ABSENT,
    compare,
    compare_lines,
    flatten,
    list_row,
    seconds,
    show_lines,
    show_value,
    verdict,
)

SHA = "sha256:" + "a" * 64


def manifest(**changes):
    base = {
        "schema": 1, "id": "20261111T235959Z-aaaaaa", "status": "succeeded",
        "experiment": {"name": "digits", "description": "baseline"},
        "config": {"path": "/x/exp.toml", "folder": "/x", "source_sha256": SHA, "overrides": []},
        "spec": {"task": "tasks:train", "seed": 7, "params": {"C": 1.0, "model": {"depth": 3, "kind": "tree"}}},
        "seed_source": "config", "config_hash": SHA, "fingerprint": SHA,
        "inputs": {"train": {"path": "data/train.csv", "kind": "file", "sha256": SHA, "bytes": 2048, "files": 1,
                             "resolved": "/x/data/train.csv"}},
        "code": {"module": "tasks", "function": "train", "file": "/x/tasks.py", "source_sha256": SHA,
                 "git": {"root": "/x", "commit": "c" * 40, "dirty": False, "diff_sha256": None, "untracked": 0}},
        "environment": {"python": {"version": "3.13.5", "implementation": "CPython"},
                        "platform": {"system": "Darwin", "release": "25.1.0", "machine": "arm64", "cpu_count": 8},
                        "variables": {"PYTHONHASHSEED": "0"}, "packages": {"numpy": "2.5.4", "pytest": "9.2.0"},
                        "summary_hash": SHA},
        "started_at": "2026-11-11T23:59:59.123456Z", "finished_at": "2026-11-12T00:01:14.000000Z",
        "duration_s": 74.9, "exit_code": 0, "signal": None,
        "metrics": {"accuracy": 0.95, "n": 1797, "converged": True, "solver": "lbfgs"},
        "error": None, "outputs": {"model.json": {"sha256": SHA, "bytes": 512}},
        "resources": {"cpu_user_s": 70.1, "cpu_system_s": 1.2, "max_rss_bytes": 300 * 2**20},
    }
    out = copy.deepcopy(base)
    for path, value in changes.items():
        target = out
        keys = path.split("__")
        for key in keys[:-1]:
            target = target[key]
        target[keys[-1]] = value
    return out


def test_flatten_gives_dotted_paths_and_keeps_lists_and_empty_tables():
    assert flatten({"a": 1, "b": {"c": [1, 2], "d": {"e": "x"}, "f": {}}}) == {"a": 1, "b.c": [1, 2], "b.d.e": "x",
                                                                              "b.f": {}}
    assert flatten({}) == {}


def test_values_print_so_types_are_distinguishable():
    assert [show_value(v) for v in (1, 1.0, "1", True, None, [1, "a"], ABSENT)] == \
        ["1", "1.0", '"1"', "true", "null", '[1, "a"]', "–"]


@pytest.mark.parametrize("value, expected", [(None, "–"), (0.5, "0.50 s"), (59.994, "59.99 s"), (75, "1 min 15 s"),
                                             (3725, "1 h 02 min")])
def test_durations_read_naturally(value, expected):
    assert seconds(value) == expected


def test_list_rows_show_a_few_metrics():
    row = list_row("id", manifest(), "succeeded", metrics_shown=2)
    assert row == ["id", "digits", "succeeded", "2026-11-11 23:59:59", "1 min 15 s", "accuracy=0.95  n=1797  +2 more"]
    assert list_row("id", None, "incomplete") == ["id", "–", "incomplete", "–", "–", ""]


def test_identical_runs_reproduce():
    result = compare([manifest(), manifest(id="b")])
    assert result["same_fingerprint"] and result["metrics_equal"] and result["differing_parts"] == []
    assert all(not section["rows"] for section in result["sections"].values())
    assert verdict(result, 2) == "same fingerprint and the same metrics: the two runs reproduce"


def test_the_same_fingerprint_with_different_metrics_is_called_out():
    result = compare([manifest(), manifest(metrics={"accuracy": 0.94, "n": 1797, "converged": True, "solver": "lbfgs"})])
    assert verdict(result, 2).startswith("same fingerprint but DIFFERENT metrics")
    accuracy = next(m for m in result["metrics"] if m["key"] == "accuracy")
    assert accuracy["deltas"][1] == pytest.approx(-0.01) and not accuracy["same"]


def test_a_failed_run_with_the_same_fingerprint_is_not_called_reproduced():
    result = compare([manifest(), manifest(status="failed", metrics=None)])
    assert verdict(result, 2) == "same fingerprint, but not every run succeeded"


@pytest.mark.parametrize("changes, expected", [
    ({"config_hash": "x", "fingerprint": "x"}, "different fingerprints: the config differs"),
    ({"inputs__train__sha256": "x", "fingerprint": "x"}, "different fingerprints: the inputs' content differs"),
    ({"code__git__dirty": True, "code__git__diff_sha256": SHA, "fingerprint": "x"},
     "different fingerprints: the code differs"),
    ({"config_hash": "x", "code__source_sha256": "y", "fingerprint": "x"},
     "different fingerprints: the config and the code differ"),
    ({"config_hash": "x", "inputs__train__sha256": "x", "code__source_sha256": "y", "fingerprint": "x"},
     "different fingerprints: the config, the inputs' content and the code differ"),
])
def test_the_verdict_names_what_differs(changes, expected):
    assert verdict(compare([manifest(), manifest(**changes)]), 2) == expected


def test_only_differing_values_are_shown_unless_all_are_asked_for():
    other = manifest(spec__params={"C": 1, "model": {"depth": 3, "kind": "tree"}, "new": True}, spec__seed=8)
    result = compare([manifest(), other])
    params = result["sections"]["params"]
    assert [(r["key"], r["values"]) for r in params["rows"]] == [("C", [1.0, 1]), ("new", [ABSENT, True])]
    assert params["hidden_same"] == 2
    assert [r["values"] for r in result["sections"]["seed"]["rows"]] == [[7, 8]]
    everything = compare([manifest(), other], show_all=True)["sections"]["params"]
    assert len(everything["rows"]) == 4 and everything["hidden_same"] == 0


def test_environment_differences_name_the_package_or_variable():
    other = manifest(environment__packages={"numpy": "2.5.5", "pytest": "9.2.0", "extra": "1.0"},
                     environment__variables={"PYTHONHASHSEED": "0", "OMP_NUM_THREADS": "4"})
    rows = {r["key"]: r["values"] for r in compare([manifest(), other])["sections"]["environment"]["rows"]}
    assert rows == {"package numpy": ["2.5.4", "2.5.5"], "package extra": [ABSENT, "1.0"],
                    "$OMP_NUM_THREADS": [ABSENT, "4"]}


def test_metric_deltas_are_for_numbers_only():
    other = manifest(metrics={"accuracy": 0.97, "n": 1800, "converged": False, "solver": "saga", "extra": 1})
    metrics = {m["key"]: m["deltas"] for m in compare([manifest(), other])["metrics"]}
    assert metrics["accuracy"][1] == pytest.approx(0.02) and metrics["n"][1] == 3
    assert metrics["converged"] == [None, None] and metrics["solver"] == [None, None]
    assert metrics["extra"] == [None, None]          # absent from the first run: nothing to subtract


def test_the_compare_page_labels_runs_and_shows_changes():
    other = manifest(id="20261112T000000Z-bbbbbb", spec__params={"C": 2.0, "model": {"depth": 3, "kind": "tree"}},
                     config_hash="x", fingerprint="x",
                     metrics={"accuracy": 0.96, "n": 1797, "converged": True, "solver": "lbfgs"})
    text = "\n".join(compare_lines([manifest(), other], ["succeeded", "succeeded"]))
    assert "  A  20261111T235959Z-aaaaaa  digits  succeeded" in text
    assert "  B  20261112T000000Z-bbbbbb  digits  succeeded" in text
    assert "different fingerprints: the config differs" in text
    assert "params  (2 identical not shown)\n     A    B\n  C  1.0  2.0\n" in text
    assert "  accuracy   0.95   0.96 (+0.01)\n  n          1797   1797\n" in text


def test_the_show_page_has_every_part():
    text = "\n".join(show_lines(manifest(), "succeeded", "runs/x"))
    for expected in ("run         20261111T235959Z-aaaaaa", "experiment  digits (baseline)",
                     "started     2026-11-11 23:59:59 UTC, took 1 min 15 s",
                     "task        tasks:train  (tasks.py, git ccccccc, source aaaaaaaa)",
                     "seed        7", "  model.depth  3", '  model.kind   "tree"', "  train  data/train.csv  2,048 bytes",
                     "  accuracy   0.95", "  model.json  512 bytes", "peak memory 300.0 MiB",
                     "environment Python 3.13.5 on Darwin arm64, 2 packages; PYTHONHASHSEED=0", "exit        0",
                     "folder      runs/x"):
        assert expected in text, expected


def test_the_show_page_for_a_failed_drawn_seed_interrupted_run():
    m = manifest(status="running", seed_source="drawn", spec__params={}, inputs={}, metrics=None, outputs={},
                 exit_code=130, error={"type": "KeyboardInterrupt", "message": "interrupted (Ctrl-C)",
                                       "traceback": "Traceback\n" + "".join(f"  line {i}\n" for i in range(30))},
                 config__overrides=["C=2"], code__git__dirty=True)
    text = "\n".join(show_lines(m, "interrupted", "runs/x"))
    assert "status      interrupted  (recorded as running)" in text
    assert "seed        7  (drawn)" in text and "params      none" in text and "inputs      none" in text
    assert "with --set C=2" in text and "+ uncommitted changes" in text
    assert "error       KeyboardInterrupt: interrupted (Ctrl-C)" in text
    assert "  line 29" in text and "  line 17" not in text          # the last 12 lines of the traceback
    assert "metrics" not in text and "outputs" not in text


def test_a_boolean_is_not_subtracted_from_a_number_either_way():
    first = manifest(metrics={"flag": 1, "other": True})
    second = manifest(metrics={"flag": True, "other": 1})
    deltas = {m["key"]: m["deltas"] for m in compare([first, second])["metrics"]}
    assert deltas == {"flag": [0, None], "other": [None, None]}


def test_hashes_and_versions_print_plainly_while_params_keep_their_quotes():
    other = manifest(spec__params={"C": 1.0, "model": {"depth": 3, "kind": "forest"}}, config_hash="x", fingerprint="x",
                     inputs__train__sha256="sha256:" + "b" * 64, environment__packages={"numpy": "2.5.5", "pytest": "9.2.0"})
    text = "\n".join(compare_lines([manifest(), other], ["succeeded", "succeeded"]))
    assert '  model.kind  "tree"  "forest"' in text
    assert "  train  aaaaaaaaaaaa  bbbbbbbbbbbb" in text and "  package numpy  2.5.4  2.5.5" in text
