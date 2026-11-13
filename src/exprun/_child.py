"""The process a task runs in: `python -m exprun._child RUN_FOLDER`.

It reads the run's manifest, imports the task from the config's folder, calls it with a
`Context`, checks what it returned and writes `result.json` (atomically) with either the metrics
or the error. The runner turns that and the exit code into the run's final status.

Exit codes: 0 the task returned metrics; 1 it raised or returned something invalid; 130 it was
interrupted (Ctrl-C); a task that calls sys.exit(n) gets exit code n (or 1 for sys.exit(0) without
returning, which is treated as a failure, since there is no result).
"""

from __future__ import annotations

import importlib
import json
import py_compile
import sys
import traceback
from contextlib import suppress
from importlib.util import cache_from_source
from pathlib import Path

from exprun.store import write_json_atomic
from exprun.task import Context, check_metrics

RESULT = "result.json"


def _fresh_bytecode(source_file: str) -> None:
    """Make sure the task module runs the source whose hash was recorded.

    Python reuses `__pycache__` bytecode when the source's modification time (in whole seconds) and
    size match what the cache file records. An edit that keeps the size, within the same second as
    the last compile, would run stale code while the manifest records the new source's hash.
    Compiling the cache in "checked hash" mode (PEP 552) makes Python validate it against a hash of
    the source instead. Other modules the task imports keep the usual timestamp check (a documented
    limitation); if the cache can't be written, Python falls back to compiling from source.
    """
    # An unwritable folder, or a syntax error the import itself will report properly.
    with suppress(OSError, py_compile.PyCompileError):
        py_compile.compile(source_file, cfile=cache_from_source(source_file), doraise=True,
                           invalidation_mode=py_compile.PycInvalidationMode.CHECKED_HASH)


def _traceback(exc: BaseException) -> str:
    """The traceback from the task's code onwards: this module's own frames are noise to a user."""
    tb = exc.__traceback__
    while tb is not None and tb.tb_next is not None and tb.tb_frame.f_code.co_filename == __file__:
        tb = tb.tb_next
    return "".join(traceback.format_exception(type(exc), exc, tb))


def _error(exc: BaseException, message: str | None = None) -> dict:
    return {"type": type(exc).__name__, "message": message or str(exc), "traceback": _traceback(exc)}


def main(run_folder: Path) -> int:
    manifest = json.loads((run_folder / "manifest.json").read_text(encoding="utf-8"))
    result_path = run_folder / RESULT
    module, function = manifest["spec"]["task"].split(":")
    sys.path.insert(0, manifest["config"]["folder"])
    _fresh_bytecode(manifest["code"]["file"])
    try:
        task = getattr(importlib.import_module(module), function)
        ctx = Context(run_id=manifest["id"], seed=manifest["spec"]["seed"], params=manifest["spec"]["params"],
                      inputs={name: Path(rec["resolved"]) for name, rec in manifest["inputs"].items()},
                      output_dir=run_folder / "outputs")
        metrics = check_metrics(task(ctx))
    except KeyboardInterrupt as exc:
        write_json_atomic(result_path, {"metrics": None, "error": _error(exc, "interrupted (Ctrl-C)")})
        return 130
    except SystemExit as exc:
        code = exc.code if isinstance(exc.code, int) else 1
        message = f"the task called sys.exit({exc.code!r}) instead of returning its metrics"
        write_json_atomic(result_path, {"metrics": None, "error": _error(exc, message)})
        return code if code != 0 else 1
    except BaseException as exc:      # report every failure, whatever the task raised
        print(_traceback(exc), end="", file=sys.stderr)
        write_json_atomic(result_path, {"metrics": None, "error": _error(exc)})
        return 1
    write_json_atomic(result_path, {"metrics": metrics, "error": None})
    return 0


if __name__ == "__main__":
    sys.exit(main(Path(sys.argv[1])))
