import json
import os
import signal
import subprocess
import threading
import time

import pytest

from exprun.codeversion import TaskError
from exprun.config import SEED_LIMIT, load
from exprun.hashing import digest
from exprun.runner import StopRequest, execute
from exprun.store import RunStore


def run(project, task="train", quiet=True, **kwargs):
    config = load(project.write(task, **{k: kwargs.pop(k) for k in ("seed", "sleep") if k in kwargs}))
    runs = RunStore(project.root / "runs")
    folder, manifest = execute(config, runs, quiet=quiet, **kwargs)
    assert folder.read() == manifest                     # what is returned is what was written
    return folder, manifest


def test_a_successful_run_records_everything(project):
    folder, m = run(project)
    assert m["status"] == "succeeded" and m["exit_code"] == 0 and m["signal"] is None and m["error"] is None
    assert m["metrics"] == {"accuracy": 0.9, "rows": 4, "seed": 42}
    assert m["spec"] == {"task": "tasks:train", "seed": 42, "params": {"C": 1.0, "sleep": 30}}
    assert m["seed_source"] == "config" and m["schema"] == 1 and m["id"] == folder.id and m["sweep"] is None
    assert m["experiment"] == {"name": "demo", "description": ""}
    assert m["config"]["source_sha256"] == digest((project.root / "exp.toml").read_bytes())
    assert m["inputs"]["data"]["path"] == "data.csv" and m["inputs"]["data"]["resolved"] == str(project.root / "data.csv")
    assert m["code"]["module"] == "tasks" and m["fingerprint"].startswith("sha256:")
    assert folder.id.endswith("-" + m["fingerprint"].removeprefix("sha256:")[:6])
    assert m["started_at"] < m["finished_at"] and 0 < m["duration_s"] < 30
    assert m["resources"]["max_rss_bytes"] > 5 * 2**20 and m["resources"]["cpu_user_s"] >= 0
    assert (folder.path / "config.toml").read_bytes() == (project.root / "exp.toml").read_bytes()
    assert not (folder.path / "result.json").exists()


def test_outputs_are_hashed_including_subfolders(project):
    folder, m = run(project)
    assert m["outputs"] == {
        "model.txt": {"sha256": digest(b"seed 42\n"), "bytes": 8},
        "plots/curve.csv": {"sha256": digest(b"1,2\n"), "bytes": 4},
    }


def test_the_task_output_is_logged_and_echoed_unless_quiet(project, capsys):
    folder, _ = run(project, quiet=False)
    assert (folder.path / "stdout.log").read_text() == "training on 4 rows, C=1.0\n"
    assert (folder.path / "stderr.log").read_text() == "a note on stderr\n"
    captured = capsys.readouterr()
    assert "training on 4 rows" in captured.out and "a note on stderr" in captured.err
    run(project, quiet=True)
    assert capsys.readouterr().out == ""


def test_the_task_runs_in_the_config_folder_with_a_fixed_hash_seed_and_a_running_manifest(project):
    folder, m = run(project, "inspect")
    assert m["metrics"]["cwd"] == str(project.root)
    assert m["metrics"]["status_while_running"] == "running" and m["metrics"]["run_id"] == folder.id
    assert m["metrics"]["hashseed"] == "0" and m["environment"]["variables"]["PYTHONHASHSEED"] == "0"
    assert run(project, "inspect")[1]["metrics"]["hash_abc"] == m["metrics"]["hash_abc"]


def test_a_missing_seed_is_drawn_and_recorded(project):
    _, first = run(project, seed="")
    _, second = run(project, seed="")
    assert first["seed_source"] == second["seed_source"] == "drawn"
    assert 0 <= first["spec"]["seed"] < SEED_LIMIT and first["metrics"]["seed"] == first["spec"]["seed"]
    assert first["spec"]["seed"] != second["spec"]["seed"]          # 1 in 4 billion chance of a clash


def test_a_task_that_returns_nothing_succeeds_with_no_metrics(project):
    assert run(project, "nothing")[1]["metrics"] == {}


@pytest.mark.parametrize("task, error_type, message, exit_code", [
    ("boom", "ValueError", "bad learning rate: -1", 1),
    ("bad_metrics", "MetricsError", "metric 'loss' is nan", 1),
    ("exits", "SystemExit", "the task called sys.exit(3) instead of returning its metrics", 3),
    ("exits_zero", "SystemExit", "sys.exit(0) instead of returning", 1),
    ("vanishes", "NoResult", "exited with code 0 without reporting a result", 0),
    ("killed", "NoResult", "the task's process was killed by SIGKILL", -9),
])
def test_failures_are_kept_with_their_cause(project, task, error_type, message, exit_code):
    folder, m = run(project, task)
    assert m["status"] == "failed" and m["metrics"] is None
    assert m["error"]["type"] == error_type and message in m["error"]["message"]
    assert m["exit_code"] == exit_code
    assert m["signal"] == ("SIGKILL" if exit_code == -9 else None)
    assert m["finished_at"] is not None and m["resources"] is not None


def test_a_task_exception_keeps_a_traceback_from_the_task_onwards(project):
    folder, m = run(project, "boom")
    traceback = m["error"]["traceback"]
    assert traceback.startswith("Traceback (most recent call last):") and "tasks.py" in traceback
    assert "_child.py" not in traceback
    assert "ValueError: bad learning rate: -1" in (folder.path / "stderr.log").read_text()


def test_a_task_that_fails_on_import_is_recorded(project):
    (project.root / "tasks.py").write_text((project.root / "tasks.py").read_text() + "\nraise RuntimeError('at import')\n")
    _, m = run(project)
    assert m["status"] == "failed" and m["error"] == {**m["error"], "type": "RuntimeError", "message": "at import"}


def test_a_time_limit_stops_the_task(project):
    _, m = run(project, "slow", timeout=0.5)
    assert m["status"] == "failed" and m["error"]["type"] == "Timeout"
    assert m["error"]["message"] == "stopped after the time limit of 0.5 s"
    assert m["signal"] == "SIGTERM" and m["duration_s"] < 10


def test_config_errors_create_no_run(project):
    (project.root / "exp.toml").write_text(project.write().read_text().replace("tasks:train", "tasks:nope"))
    with pytest.raises(TaskError):
        execute(load(project.root / "exp.toml"), RunStore(project.root / "runs"))
    assert not (project.root / "runs").exists()


def test_a_failure_of_the_runner_itself_is_recorded_and_raised(project, monkeypatch):
    def broken(*args, **kwargs):
        raise OSError("no processes left")

    monkeypatch.setattr(subprocess, "Popen", broken)
    with pytest.raises(OSError, match="no processes left"):
        execute(load(project.write()), RunStore(project.root / "runs"), quiet=True)
    (folder,) = RunStore(project.root / "runs").all()
    m = folder.read()
    assert m["status"] == "failed" and m["error"]["message"] == "the runner failed: no processes left"
    assert not folder.lock_is_held()


def test_each_run_gets_its_own_folder_even_in_the_same_second(project):
    ids = {run(project)[0].id for _ in range(3)}
    assert len(ids) == 3


def test_the_manifest_is_valid_json_with_no_temporary_files_left(project):
    folder, _ = run(project)
    assert json.loads((folder.path / "manifest.json").read_text())
    assert sorted(p.name for p in folder.path.iterdir()) == [".lock", "config.toml", "manifest.json", "outputs",
                                                             "stderr.log", "stdout.log"]


def test_the_task_is_found_even_when_python_leaves_the_working_folder_off_the_path(project, monkeypatch):
    monkeypatch.setenv("PYTHONSAFEPATH", "1")           # Python 3.11+: don't put the current folder on sys.path
    assert run(project)[1]["status"] == "succeeded"


def test_an_edit_with_the_same_size_and_timestamp_still_runs_the_new_code(project):
    # Python's bytecode cache trusts (mtime in seconds, size). Without a hash check, this edit would
    # run the old code while the manifest records the new source's hash.
    tasks = project.root / "tasks.py"
    _, first = run(project)
    stamp = tasks.stat().st_mtime_ns
    tasks.write_text(tasks.read_text().replace('"accuracy": 0.9', '"accuracy": 0.8'))     # same size
    import os

    os.utime(tasks, ns=(stamp, stamp))
    _, second = run(project)
    assert first["metrics"]["accuracy"] == 0.9 and second["metrics"]["accuracy"] == 0.8
    assert second["code"]["source_sha256"] != first["code"]["source_sha256"]


def test_a_task_that_reports_its_own_ctrl_c_is_interrupted_even_if_the_runner_saw_none(project):
    # In a sweep, Ctrl-C reaches the task directly, and its worker thread may reap it before it
    # learns of the stop request from the main thread.
    _, m = run(project, "ctrl_c")
    assert m["status"] == "interrupted" and m["error"]["type"] == "KeyboardInterrupt" and m["exit_code"] == 130


# --- stopping a run from another thread ----------------------------------------------------------

def run_in_thread(project, task, stop):
    config = load(project.write(task))
    box = {}
    thread = threading.Thread(target=lambda: box.update(result=execute(
        config, RunStore(project.root / "runs"), quiet=True, stop=stop)))
    mark = project.root / "started.mark"
    os.environ["EXPRUN_TEST_MARK"] = str(mark)
    try:
        thread.start()
        deadline = time.monotonic() + 20
        while not mark.exists():
            assert time.monotonic() < deadline, "the task never started"
            time.sleep(0.02)
    finally:
        del os.environ["EXPRUN_TEST_MARK"]
    return thread, box


def test_a_stop_request_from_another_thread_stops_the_run(project):
    stop = StopRequest()
    thread, box = run_in_thread(project, "slow", stop)
    began = time.monotonic()
    stop.request(signal.SIGTERM)
    thread.join(20)
    _, m = box["result"]
    assert m["status"] == "interrupted" and m["signal"] == "SIGTERM" and time.monotonic() - began < 3


def test_a_second_stop_request_kills_a_task_that_ignores_the_first(project):
    stop = StopRequest()
    thread, box = run_in_thread(project, "stubborn", stop)
    stop.request(signal.SIGTERM)
    time.sleep(0.5)
    assert thread.is_alive()                    # ignored: it would get the grace period
    began = time.monotonic()
    stop.request(None)
    thread.join(20)
    _, m = box["result"]
    assert m["status"] == "interrupted" and m["signal"] == "SIGKILL" and time.monotonic() - began < 3


def test_a_run_that_starts_after_a_stop_request_is_stopped_at_once(project):
    stop = StopRequest()
    stop.request(signal.SIGTERM)
    began = time.monotonic()
    _, m = execute(load(project.write("slow")), RunStore(project.root / "runs"), quiet=True, stop=stop)
    assert m["status"] == "interrupted" and time.monotonic() - began < 5
