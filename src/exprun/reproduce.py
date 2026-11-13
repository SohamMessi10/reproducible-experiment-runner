"""Re-running a past run from its manifest, and judging whether it reproduced.

**Before re-running,** each of these is refused, before anything runs:
- **The original didn't succeed:** there is no result to reproduce.
- **An input's content changed, or the input is missing.** The data can't be put back, and a run
  on different data isn't a reproduction.
- **The task's code changed** (its source file, its git commit, or its uncommitted changes), unless
  `allow_code_change` is set. Then the question becomes "does the current code reproduce the old
  result?", and the report says so.

The rerun uses the config bytes saved with the run (not the file as it is now), its recorded
overrides, and its recorded seed, even one that was drawn.

**After re-running,** the outcome is one of:
- **identical:** every metric equal (same type and value) and every output file byte-identical;
- **metrics identical, outputs differ:** the numbers agree, but some output file doesn't;
- **within tolerance:** only when a tolerance is given. Every numeric metric is within that relative
  difference, compared as numbers (so 100 and 100.0 agree), and every other metric is equal. Output
  files are reported but not judged, since floating-point noise reaches them too;
- **different;**
- **failed:** the rerun itself didn't succeed.

Only "identical" and "within tolerance" count as reproduced.
"""

from __future__ import annotations

from pathlib import Path

from exprun.codeversion import code_version
from exprun.config import Config, loads
from exprun.hashing import hash_input

REPRODUCED = ("identical", "within tolerance")


class CannotRerun(ValueError):
    """The run can't be re-run as recorded (exit code 2)."""


def config_for(manifest: dict, folder: Path) -> Config:
    """The run's config, from the bytes saved with it, resolved as if at its original path."""
    source = (folder / "config.toml").read_bytes()
    return loads(source, manifest["config"]["path"], manifest["config"].get("overrides") or [])


def check_inputs(manifest: dict, config: Config) -> None:
    """Every input must still hash as recorded. (A missing input already fails loading the config.)"""
    for name, record in manifest["inputs"].items():
        now = hash_input(config.inputs[name])["sha256"]
        if now != record["sha256"]:
            raise CannotRerun(f"input {name} ({record['path']}) has changed since the run: it was "
                              f"{_short(record['sha256'])} and is now {_short(now)}. A run on different data "
                              "isn't a reproduction")


def code_changes(manifest: dict, config: Config) -> list[str]:
    """How the task's code differs now from when the run was made (empty if it doesn't)."""
    then = manifest["code"]
    now = code_version(config.module, config.function, config.folder)
    changes = []
    if now["source_sha256"] != then["source_sha256"]:
        changes.append(f"{Path(now['file']).name} changed (source {_short(then['source_sha256'])} -> "
                       f"{_short(now['source_sha256'])})")
    git_then, git_now = then.get("git") or {}, now.get("git") or {}
    if git_then.get("commit") != git_now.get("commit"):
        changes.append(f"the git commit changed ({(git_then.get('commit') or 'none')[:7]} -> "
                       f"{(git_now.get('commit') or 'none')[:7]})")
    if (git_then.get("dirty"), git_then.get("diff_sha256")) != (git_now.get("dirty"), git_now.get("diff_sha256")):
        changes.append("the uncommitted changes differ")
    return changes


def prepare(manifest: dict, folder: Path, allow_code_change: bool = False) -> tuple[Config, list[str]]:
    """The config to re-run, and the code changes allowed through. Raises CannotRerun."""
    if manifest["status"] != "succeeded":
        raise CannotRerun(f"run {manifest['id']} {manifest['status']}; only a run that succeeded has a result "
                          "to reproduce")
    try:
        config = config_for(manifest, folder)
    except (OSError, ValueError) as exc:
        raise CannotRerun(f"the run's saved config can't be used any more: {exc}") from None
    check_inputs(manifest, config)
    try:
        changes = code_changes(manifest, config)
    except ValueError as exc:                 # the task module or function is gone
        raise CannotRerun(f"the task can't be found any more: {exc}") from None
    if changes and not allow_code_change:
        raise CannotRerun("the task's code has changed since the run: " + "; ".join(changes) + ". Restore it "
                          "(for example, check out the recorded commit), or pass --allow-code-change to test "
                          "whether the current code reproduces the result")
    return config, changes


def _short(sha: str | None) -> str:
    return (sha or "none").removeprefix("sha256:")[:12]


def relative_difference(a: float, b: float) -> float:
    """|a - b| relative to the larger magnitude; 0 when both are 0."""
    scale = max(abs(a), abs(b))
    return 0.0 if scale == 0 else abs(a - b) / scale


def _numeric(value) -> bool:
    return isinstance(value, int | float) and not isinstance(value, bool)


def judge(original: dict, rerun: dict, tolerance: float = 0.0) -> dict:
    """Compare a rerun with the original. Returns the outcome and the evidence for it."""
    if rerun["status"] != "succeeded":
        return {"outcome": "failed", "reproduced": False, "metric_differences": [], "output_differences": [],
                "max_relative_difference": None, "tolerance": tolerance}
    before, after = original.get("metrics") or {}, rerun.get("metrics") or {}
    metric_differences = []
    worst = 0.0
    within = True
    for name in dict.fromkeys([*before, *after]):
        a, b = before.get(name), after.get(name)
        if name in before and name in after and type(a) is type(b) and a == b:
            continue
        entry = {"metric": name, "original": a, "rerun": b}
        if name in before and name in after and _numeric(a) and _numeric(b):
            entry["relative_difference"] = relative_difference(a, b)
            worst = max(worst, entry["relative_difference"])
            within &= entry["relative_difference"] <= tolerance
        else:
            within = False                    # a missing metric, or a non-numeric one that changed
        metric_differences.append(entry)
    outputs_before, outputs_after = original.get("outputs") or {}, rerun.get("outputs") or {}
    output_differences = [path for path in sorted(set(outputs_before) | set(outputs_after))
                          if (outputs_before.get(path) or {}).get("sha256") != (outputs_after.get(path) or {}).get("sha256")]
    if not metric_differences and not output_differences:
        outcome = "identical"
    elif tolerance > 0 and within:            # with a tolerance, only the metrics are judged
        outcome = "within tolerance"
    elif not metric_differences:
        outcome = "metrics identical, outputs differ"
    else:
        outcome = "different"
    return {"outcome": outcome, "reproduced": outcome in REPRODUCED, "metric_differences": metric_differences,
            "output_differences": output_differences, "max_relative_difference": worst if metric_differences else 0.0,
            "tolerance": tolerance}
