"""Running an experiment: one run folder, one child process, a manifest that is always readable.

The sequence, and what a crash at each point leaves behind:

1. Resolve the seed (drawing one if the config has none), hash config, inputs and code. An error
   here creates nothing.
2. Claim the run folder and take its lock. The manifest is written with status "running" before
   the task starts. A crash after this leaves "running" with a free lock, which readers report as
   interrupted (see store.py).
3. Start the task in a child process (`exprun._child`). The child gets the lock's file
   descriptor, so the run stays "in progress" until both processes have ended. Its stdout and
   stderr are copied to log files, and to the terminal unless quiet.
4. Wait for it. Its CPU time and peak memory come from `wait4`, which reports that one child's
   usage exactly. Ctrl-C, SIGTERM and a time limit stop the child in stages: let it finish
   reacting, then SIGTERM, then SIGKILL.
5. Record the outcome: succeeded, failed or interrupted, with metrics or the error, the exit code,
   output file hashes and timings. Then release the lock.
"""

from __future__ import annotations

import json
import os
import secrets
import signal
import subprocess
import sys
import threading
import time as clock
from contextlib import contextmanager, suppress
from pathlib import Path

from exprun import __version__
from exprun._child import RESULT
from exprun.config import SEED_LIMIT, Config
from exprun.environment import capture
from exprun.hashing import digest, hash_input
from exprun.identity import identify
from exprun.store import SCHEMA, RunFolder, RunStore, iso, now

POLL_S = 0.02
GRACE_S = 5.0            # how long a stopped child gets at each stage before the next, harder one
STDERR_TAIL_LINES = 20


class Terminated(BaseException):
    """The runner received SIGTERM."""


class StopRequest:
    """Stops runs from another thread. Signals reach only the main thread, so a sweep running tasks
    from worker threads passes one of these to each run: the first request stops the run in stages,
    like Ctrl-C does a single run, and a second kills it at once."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.count = 0
        self.first_signal: int | None = None

    def request(self, first_signal: int | None) -> None:
        """`first_signal` goes to the task at once: None for Ctrl-C, which the task got itself."""
        with self._lock:
            self.count += 1
            if self.count == 1:
                self.first_signal = first_signal

    @property
    def requested(self) -> bool:
        return self.count > 0


@contextmanager
def _sigterm_raises():
    """Turn SIGTERM into an exception, so the runner can stop the task and record it. Only possible
    in the main thread; elsewhere SIGTERM keeps its default meaning."""
    if threading.current_thread() is not threading.main_thread():
        yield
        return

    def handler(signum, frame):
        raise Terminated()

    previous = signal.signal(signal.SIGTERM, handler)
    try:
        yield
    finally:
        signal.signal(signal.SIGTERM, previous)


def _copy(source_fd: int, log: Path, echo) -> None:
    """Copy a pipe to a log file, and to the terminal unless `echo` is None."""
    with open(log, "wb") as out:
        while chunk := os.read(source_fd, 65536):
            out.write(chunk)
            out.flush()
            if echo is not None:
                try:
                    echo.write(chunk)
                    echo.flush()
                except (OSError, ValueError):     # the terminal went away: keep logging
                    echo = None
    os.close(source_fd)


def _tail(path: Path, lines: int = STDERR_TAIL_LINES) -> str:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    return "\n".join(text.splitlines()[-lines:])


def _hash_outputs(folder: Path) -> dict:
    outputs = {}
    if folder.is_dir():
        for path in sorted(p for p in folder.rglob("*") if p.is_file()):
            record = hash_input(path)
            outputs[path.relative_to(folder).as_posix()] = {"sha256": record["sha256"], "bytes": record["bytes"]}
    return outputs


def child_environment() -> dict[str, str]:
    env = dict(os.environ)
    env["PYTHONHASHSEED"] = "0"          # repeatable str hashing, so set iteration order repeats too
    env["PYTHONUNBUFFERED"] = "1"        # the task's output reaches the log and terminal as it is printed
    return env


def execute(config: Config, runs: RunStore, *, overrides: list[str] | None = None, timeout: float | None = None,
            quiet: bool = False, on_start=None, rerun_of: dict | None = None, seed: int | None = None,
            seed_source: str = "sweep", sweep: dict | None = None,
            stop: StopRequest | None = None) -> tuple[RunFolder, dict]:
    """Run the experiment. Returns its folder and final manifest. `on_start(folder, manifest)` is
    called once the run exists, before the task starts (the CLI uses it to print the run id).
    `rerun_of` is the manifest of a run being re-run: its seed is reused, drawn or not. `seed`
    replaces the config's, recorded as coming from `seed_source`. `sweep` is recorded in the
    manifest as is. `stop` lets another thread stop the run."""
    if rerun_of is not None:
        seed, seed_source = rerun_of["spec"]["seed"], rerun_of["seed_source"]
    elif seed is not None:
        pass
    elif config.seed is not None:
        seed, seed_source = config.seed, "config"
    else:
        seed, seed_source = secrets.randbelow(SEED_LIMIT), "drawn"
    identity = identify(config, seed)
    env = child_environment()
    started = now()
    folder, lock = runs.create(identity.fingerprint, started)
    try:
        return folder, _run(config, folder, lock, identity, seed_source, env, started, overrides or [], timeout,
                            quiet, on_start, rerun_of["id"] if rerun_of is not None else None, sweep, stop)
    finally:
        lock.release()


def _run(config, folder, lock, identity, seed_source, env, started, overrides, timeout, quiet, on_start,
         rerun_of, sweep, stop) -> dict:
    (folder.path / "config.toml").write_bytes(config.source)
    folder.outputs.mkdir()
    inputs = {name: {**record, "resolved": str(config.inputs[name])} for name, record in identity.inputs.items()}
    manifest = {
        "schema": SCHEMA,
        "id": folder.id,
        "status": "running",
        "experiment": {"name": config.name, "description": config.description},
        "config": {"path": str(config.path), "folder": str(config.folder), "source_sha256": digest(config.source),
                   "overrides": overrides},
        "spec": identity.spec,
        "seed_source": seed_source,
        "rerun_of": rerun_of,
        "sweep": sweep,
        "config_hash": identity.config_hash,
        "inputs": inputs,
        "code": identity.code,
        "fingerprint": identity.fingerprint,
        "environment": capture(env),
        "runner": {"version": __version__, "pid": os.getpid(), "timeout_s": timeout},
        "started_at": iso(started),
        "finished_at": None,
        "duration_s": None,
        "exit_code": None,
        "signal": None,
        "metrics": None,
        "error": None,
        "outputs": {},
        "resources": None,
    }
    folder.write(manifest)
    if on_start is not None:
        on_start(folder, manifest)

    stopped_by = None
    returncode, rusage = None, None
    began = clock.monotonic()
    try:
        with _sigterm_raises():
            stdout_r, stdout_w = os.pipe()
            stderr_r, stderr_w = os.pipe()
            try:
                process = _spawn(folder, config, env, lock, stdout_w, stderr_w)
            finally:
                os.close(stdout_w)
                os.close(stderr_w)
            copiers = [threading.Thread(target=_copy, args=(stdout_r, folder.path / "stdout.log",
                                                            None if quiet else sys.stdout.buffer), daemon=True),
                       threading.Thread(target=_copy, args=(stderr_r, folder.path / "stderr.log",
                                                            None if quiet else sys.stderr.buffer), daemon=True)]
            for thread in copiers:
                thread.start()
            returncode, rusage, stopped_by = _wait(process.pid, began, timeout, stop)
            process.returncode = returncode      # reaped by wait4: tell Popen, so it doesn't think it still runs
            for thread in copiers:
                thread.join()
    except BaseException as exc:     # the runner itself failed: record what it can, then re-raise
        manifest.update(status="failed", error={"type": type(exc).__name__, "message": f"the runner failed: {exc}"})
        _finish(folder, manifest, began, returncode, rusage)
        raise
    result = _read_result(folder)
    manifest["metrics"] = result.get("metrics")
    manifest["error"] = result.get("error")
    # The task can get Ctrl-C directly (it shares the terminal) while this runner wasn't the process
    # interrupted: in a sweep, only the main thread sees KeyboardInterrupt. Its own report says so.
    interrupted_itself = (result.get("error") or {}).get("type") == "KeyboardInterrupt"
    if stopped_by == "interrupt" or interrupted_itself:
        manifest["status"] = "interrupted"
        manifest["error"] = manifest["error"] or {"type": "Interrupted", "message": "the run was stopped (Ctrl-C or SIGTERM)"}
    elif stopped_by == "timeout":
        manifest["status"] = "failed"
        manifest["error"] = {"type": "Timeout", "message": f"stopped after the time limit of {timeout:g} s"}
    elif returncode == 0 and result.get("error") is None and "metrics" in result:
        manifest["status"] = "succeeded"
    else:
        manifest["status"] = "failed"
        if manifest["error"] is None:
            if returncode is not None and returncode < 0:
                message = f"the task's process was killed by {signal.Signals(-returncode).name}"
            else:
                message = f"the task's process exited with code {returncode} without reporting a result"
            manifest["error"] = {"type": "NoResult", "message": message, "stderr_tail": _tail(folder.path / "stderr.log")}
    _finish(folder, manifest, began, returncode, rusage)
    return manifest


def _spawn(folder, config, env, lock, stdout_w, stderr_w) -> subprocess.Popen:
    return subprocess.Popen([sys.executable, "-m", "exprun._child", str(folder.path)], cwd=config.folder, env=env,
                               stdin=subprocess.DEVNULL, stdout=stdout_w, stderr=stderr_w, pass_fds=(lock.fd,))


def _wait(pid: int, began: float, timeout: float | None, request: StopRequest | None = None):
    """Wait for the child, reaping it with wait4 to get its own resource usage. Returns
    (exit code, usage, None | "interrupt" | "timeout")."""
    stopped_by = None
    requests_seen = 0
    stages: list[tuple[float, int | None]] = []       # (deadline, signal to send then)

    def stop(reason: str, first_signal: int | None) -> None:
        nonlocal stopped_by, stages
        if stopped_by is None:
            stopped_by = reason
            start = clock.monotonic()
            stages = [(start, first_signal), (start + GRACE_S, signal.SIGTERM), (start + 2 * GRACE_S, signal.SIGKILL)]
        else:
            stages = [(clock.monotonic(), signal.SIGKILL)]       # asked twice: stop now

    while True:
        try:
            done, status, rusage = os.wait4(pid, os.WNOHANG)
            if done:
                return os.waitstatus_to_exitcode(status), rusage, stopped_by
            if timeout is not None and stopped_by is None and clock.monotonic() - began > timeout:
                stop("timeout", signal.SIGTERM)
            if request is not None and request.count > requests_seen:
                if requests_seen == 0 and stopped_by is None:
                    stop("interrupt", request.first_signal)
                if request.count > 1:         # asked again: stop() kills a run it is already stopping
                    stop("interrupt", None)
                requests_seen = request.count
            while stages and clock.monotonic() >= stages[0][0]:
                _, sig = stages.pop(0)
                if sig is not None:
                    with suppress(ProcessLookupError):      # it ended in the meantime
                        os.kill(pid, sig)
            clock.sleep(POLL_S)
        except (KeyboardInterrupt, Terminated) as exc:
            # Ctrl-C reaches the whole process group, so the child usually has it already: give it the
            # grace period to record its interruption. SIGTERM reaches only us: pass it on.
            stop("interrupt", signal.SIGTERM if isinstance(exc, Terminated) else None)


def _read_result(folder: RunFolder) -> dict:
    path = folder.path / RESULT
    try:
        result = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, ValueError):
        return {}
    path.unlink()
    return result if isinstance(result, dict) else {}


def _finish(folder: RunFolder, manifest: dict, began: float, returncode: int | None, rusage) -> None:
    finished = now()
    manifest["finished_at"] = iso(finished)
    manifest["duration_s"] = round(clock.monotonic() - began, 6)
    manifest["exit_code"] = returncode
    manifest["signal"] = signal.Signals(-returncode).name if returncode is not None and returncode < 0 else None
    if rusage is not None:
        scale = 1 if sys.platform == "darwin" else 1024      # macOS reports bytes, Linux KiB
        manifest["resources"] = {"cpu_user_s": round(rusage.ru_utime, 6), "cpu_system_s": round(rusage.ru_stime, 6),
                                 "max_rss_bytes": rusage.ru_maxrss * scale}
    manifest["outputs"] = _hash_outputs(folder.outputs)
    folder.write(manifest)

