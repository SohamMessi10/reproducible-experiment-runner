"""The task's code version, found without running any of it.

`locate` finds the task's module the way `import` would, searching the config's folder first and
then the usual places. It walks a dotted name one package at a time with `PathFinder`, so no
package's `__init__.py` runs. Checking a config never executes user code.

The record says:
- **module, function, file**, and `source_sha256`, the hash of the module's file;
- **distribution and version**, when the module belongs to an installed package (for example the
  built-in demo task);
- **git**, when the file is in a git repository:
  - `commit`;
  - `dirty`: tracked files changed since the commit;
  - `diff_sha256`: a hash of those changes;
  - `untracked`: the number of untracked entries git lists (an untracked folder counts once).

Limitations, stated rather than hidden:
- The source hash covers the task's own file, not the other modules it imports. The commit and
  the hash of uncommitted changes cover the rest of a git repository.
- Untracked files are counted, not hashed. They can't change a fingerprint, since a runs folder
  inside the repository would otherwise change it on every run.
"""

from __future__ import annotations

import ast
import subprocess
import sys
from importlib import metadata
from importlib.machinery import ModuleSpec, PathFinder
from pathlib import Path

from exprun.hashing import digest

GIT_TIMEOUT_S = 10


def search_path(config_folder: Path) -> list[str]:
    """Where a task module is looked for: the config's folder, then the interpreter's path."""
    return [str(config_folder)] + [p for p in sys.path if p]


def locate(module: str, path: list[str]) -> ModuleSpec | None:
    """Find a module's spec without importing it or any package above it."""
    parts = module.split(".")
    spec: ModuleSpec | None = None
    for depth in range(len(parts)):
        spec = PathFinder.find_spec(".".join(parts[: depth + 1]), path)
        if spec is None:
            return None
        if depth < len(parts) - 1:
            if spec.submodule_search_locations is None:   # a module, not a package
                return None
            path = list(spec.submodule_search_locations)
    return spec


class TaskError(ValueError):
    """The task named in the config can't be found."""


def code_version(module: str, function: str, config_folder: Path) -> dict:
    spec = locate(module, search_path(config_folder))
    if spec is None or spec.origin is None or not spec.has_location:
        raise TaskError(f"task module {module!r} not found (searched the config's folder, then the Python path)")
    file = Path(spec.origin)
    if file.suffix != ".py":
        raise TaskError(f"task module {module!r} is {file.name}, not Python source; its code can't be hashed")
    source = file.read_bytes()
    if not _defines(source, function):
        raise TaskError(f"{file} doesn't define a top-level function {function!r}")
    record = {"module": module, "function": function, "file": str(file), "source_sha256": digest(source)}
    record.update(_distribution(module))
    record["git"] = git_state(file.parent)
    return record


def _defines(source: bytes, function: str) -> bool:
    """Whether the module defines `function` at top level, by reading its syntax tree."""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return False
    for node in tree.body:
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef) and node.name == function:
            return True
        if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == function for t in node.targets):
            return True
        if isinstance(node, ast.ImportFrom) and any((a.asname or a.name) == function for a in node.names):
            return True
    return False


def _distribution(module: str) -> dict:
    names = metadata.packages_distributions().get(module.split(".")[0], [])
    if not names:
        return {}
    try:
        return {"distribution": names[0], "version": metadata.version(names[0])}
    except metadata.PackageNotFoundError:
        return {}


def _git(folder: Path, *args: str) -> subprocess.CompletedProcess | None:
    try:
        return subprocess.run(["git", "-C", str(folder), *args], capture_output=True, timeout=GIT_TIMEOUT_S)
    except (OSError, subprocess.TimeoutExpired):   # git not installed, or stuck
        return None


def git_state(folder: Path) -> dict | None:
    """The git commit and uncommitted changes of the repository containing `folder`, if any."""
    top = _git(folder, "rev-parse", "--show-toplevel")
    if top is None or top.returncode != 0:
        return None
    head = _git(folder, "rev-parse", "--verify", "--quiet", "HEAD")
    commit = head.stdout.decode().strip() if head is not None and head.returncode == 0 else None
    status = _git(folder, "status", "--porcelain=v1", "--untracked-files=normal")
    lines = status.stdout.decode(errors="replace").splitlines() if status is not None and status.returncode == 0 else []
    untracked = sum(1 for line in lines if line.startswith("??"))
    dirty = any(not line.startswith("??") for line in lines)
    diff_sha256 = None
    if dirty and commit is not None:
        diff = _git(folder, "diff", "HEAD", "--binary")
        if diff is not None and diff.returncode == 0:
            diff_sha256 = digest(diff.stdout)
    return {"root": top.stdout.decode().strip(), "commit": commit, "dirty": dirty, "diff_sha256": diff_sha256,
            "untracked": untracked}

