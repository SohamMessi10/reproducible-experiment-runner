"""Stopping a run from outside, with real processes and real signals."""

import os
import signal
import subprocess
import sys
import time

from exprun.store import RunStore


def start(project, task, sleep=30):
    """Start `exprun run` in its own process group (like a terminal job) and wait for the task to start."""
    config = project.write(task, sleep=sleep)
    mark = project.root / f"{task}.mark"
    env = {**os.environ, "EXPRUN_TEST_MARK": str(mark)}
    env.pop("PYTHONUNBUFFERED", None)        # some machines set it; the runner must not depend on that
    runner = subprocess.Popen([sys.executable, "-m", "exprun.cli", "run", str(config), "--runs",
                               str(project.root / "runs"), "--quiet"], cwd=project.root, env=env,
                              start_new_session=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    deadline = time.monotonic() + 20
    while not mark.exists():
        assert runner.poll() is None, runner.communicate()
        assert time.monotonic() < deadline, "the task never started"
        time.sleep(0.02)
    (folder,) = RunStore(project.root / "runs").all()
    return runner, folder, int(mark.read_text())


def finish(runner, seconds=20):
    out, err = runner.communicate(timeout=seconds)
    return runner.returncode, err.decode()


def alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def test_ctrl_c_interrupts_the_run_and_records_it(project):
    runner, folder, _ = start(project, "slow")
    assert folder.read()["status"] == "running" and folder.lock_is_held()
    log, deadline = folder.path / "stdout.log", time.monotonic() + 5
    while log.read_text() != "started\n":             # logged as printed, not when the task ends (in 30 s)
        assert time.monotonic() < deadline, log.read_text()
        time.sleep(0.02)
    began = time.monotonic()
    os.killpg(runner.pid, signal.SIGINT)                # what a terminal does: the whole group gets it
    code, err = finish(runner)
    m = folder.read()
    assert code == 1 and "interrupted in" in err
    assert m["status"] == "interrupted" and m["error"]["type"] == "KeyboardInterrupt"
    assert m["error"]["message"] == "interrupted (Ctrl-C)" and m["exit_code"] == 130
    assert time.monotonic() - began < 5 and not folder.lock_is_held()


def test_sigterm_to_the_runner_alone_is_passed_on_to_the_task(project):
    runner, folder, task_pid = start(project, "slow")
    began = time.monotonic()
    os.kill(runner.pid, signal.SIGTERM)                 # only the runner: it must stop the task itself
    code, _ = finish(runner)
    assert time.monotonic() - began < 3                 # passed on at once, not after the grace period
    m = folder.read()
    assert code == 1 and m["status"] == "interrupted" and m["signal"] == "SIGTERM"
    assert m["error"]["type"] == "Interrupted" and not alive(task_pid)


def test_a_second_ctrl_c_kills_a_task_that_ignores_the_first(project):
    runner, folder, task_pid = start(project, "stubborn")
    os.killpg(runner.pid, signal.SIGINT)
    time.sleep(0.5)
    assert runner.poll() is None and alive(task_pid)    # the task ignores it, and gets a grace period
    began = time.monotonic()
    os.kill(runner.pid, signal.SIGINT)
    code, _ = finish(runner)
    m = folder.read()
    assert code == 1 and m["status"] == "interrupted" and m["signal"] == "SIGKILL" and not alive(task_pid)
    assert time.monotonic() - began < 3                 # at once, not after the grace periods


def test_a_task_that_ignores_sigterm_is_killed_after_the_grace_period(project, monkeypatch):
    # In-process, with a shorter grace period, so the test doesn't wait the full 2 x 5 seconds.
    from exprun import runner as runner_module
    from exprun.config import load

    monkeypatch.setattr(runner_module, "GRACE_S", 0.3)
    monkeypatch.setenv("EXPRUN_TEST_MARK", str(project.root / "stubborn.mark"))
    _, m = runner_module.execute(load(project.write("stubborn")), RunStore(project.root / "runs"), quiet=True,
                                 timeout=0.3)
    assert m["status"] == "failed" and m["error"]["type"] == "Timeout" and m["signal"] == "SIGKILL"
    assert m["duration_s"] < 5


def test_a_runner_killed_outright_is_found_interrupted_once_its_task_ends(project):
    runner, folder, task_pid = start(project, "slow", sleep=1.5)
    os.kill(runner.pid, signal.SIGKILL)                 # no chance to record anything
    runner.communicate(timeout=10)
    assert folder.read()["status"] == "running"
    assert folder.effective_status() == "running"       # the task still runs, holding the lock
    deadline = time.monotonic() + 20
    while alive(task_pid):
        assert time.monotonic() < deadline
        time.sleep(0.05)
    deadline = time.monotonic() + 5
    while folder.lock_is_held():                        # an orphan is reaped by init, which can take a moment
        assert time.monotonic() < deadline
        time.sleep(0.05)
    assert folder.read()["status"] == "running" and folder.effective_status() == "interrupted"
    m = folder.reconcile()
    assert m["status"] == "interrupted" and m["error"]["type"] == "RunnerDied"
    assert folder.read() == m and folder.reconcile() == m          # reconciling again changes nothing
