import subprocess
import sys

import pytest

from exprun.codeversion import TaskError, code_version, git_state, locate, search_path
from exprun.hashing import digest


def git(folder, *args):
    subprocess.run(["git", "-C", str(folder), "-c", "user.name=t", "-c", "user.email=t@example.com",
                    "-c", "commit.gpgsign=false", *args], check=True, capture_output=True)


@pytest.fixture
def package(tmp_path):
    """A package whose __init__ would fail if anything imported it."""
    (tmp_path / "pkg" / "sub").mkdir(parents=True)
    (tmp_path / "pkg" / "__init__.py").write_text("raise RuntimeError('imported!')\n")
    (tmp_path / "pkg" / "sub" / "__init__.py").write_text("")
    (tmp_path / "pkg" / "sub" / "mod.py").write_text("def run(ctx):\n    return {}\n")
    (tmp_path / "tasks.py").write_text("import math\n\ndef train(ctx):\n    return {}\n\nalias = train\n"
                                       "from math import sqrt as rooted\n")
    return tmp_path


def test_a_module_is_found_without_importing_it_or_its_packages(package):
    spec = locate("pkg.sub.mod", search_path(package))
    assert spec.origin == str(package / "pkg" / "sub" / "mod.py")
    assert "pkg" not in sys.modules


def test_missing_modules_and_modules_that_are_not_packages_are_not_found(package):
    path = search_path(package)
    assert locate("nope", path) is None
    assert locate("pkg.nope", path) is None
    assert locate("tasks.inner", path) is None              # tasks is a module, not a package


def test_the_config_folder_is_searched_first(package, tmp_path_factory):
    shadow = tmp_path_factory.mktemp("shadow")
    (shadow / "json.py").write_text("def loads(ctx):\n    return {}\n")
    assert code_version("json", "loads", shadow)["file"] == str(shadow / "json.py")


def test_the_code_version_records_the_file_and_its_hash(package):
    record = code_version("tasks", "train", package)
    source = (package / "tasks.py").read_bytes()
    assert record["module"] == "tasks" and record["function"] == "train"
    assert record["file"] == str(package / "tasks.py") and record["source_sha256"] == digest(source)
    assert record["git"] is None or "commit" in record["git"]


@pytest.mark.parametrize("function", ["train", "alias", "rooted"])
def test_functions_assignments_and_imports_count_as_defined(package, function):
    assert code_version("tasks", function, package)["function"] == function


def test_a_function_that_is_not_defined_is_refused(package):
    with pytest.raises(TaskError, match="doesn't define a top-level function 'trian'"):
        code_version("tasks", "trian", package)
    (package / "broken.py").write_text("def train(:\n")
    with pytest.raises(TaskError, match="doesn't define a top-level function 'train'"):
        code_version("broken", "train", package)


def test_a_missing_or_compiled_module_is_refused(package):
    with pytest.raises(TaskError, match="task module 'nope' not found"):
        code_version("nope", "f", package)
    with pytest.raises(TaskError, match="not found"):
        code_version("sys", "exit", package)                 # built in: no file at all
    (package / "native.so").write_bytes(b"not really a library")
    with pytest.raises(TaskError, match="not Python source"):
        code_version("native", "f", package)


def test_an_installed_module_records_its_distribution(package):
    record = code_version("hypothesis", "given", package)
    assert record["distribution"] == "hypothesis" and record["version"].startswith("6.")


def test_outside_a_git_repository_there_is_no_git_state(tmp_path):
    assert git_state(tmp_path) is None


def test_git_state_tracks_the_commit_and_uncommitted_changes(package):
    git(package, "init", "-q")
    assert git_state(package) == {"root": str(package), "commit": None, "dirty": False, "diff_sha256": None,
                                  "untracked": 2}       # no commits yet: pkg/ (a folder is one entry) and tasks.py
    git(package, "add", "-A")
    git(package, "commit", "-qm", "first")
    clean = git_state(package)
    assert len(clean["commit"]) == 40 and clean["dirty"] is False and clean["diff_sha256"] is None
    assert clean["untracked"] == 0

    (package / "notes.txt").write_text("scratch")
    assert git_state(package) == {**clean, "untracked": 1}          # untracked files don't make it dirty

    (package / "tasks.py").write_text("def train(ctx):\n    return {'changed': 1}\n")
    edited = git_state(package)
    assert edited["dirty"] is True and edited["commit"] == clean["commit"] and edited["diff_sha256"]
    (package / "tasks.py").write_text("def train(ctx):\n    return {'changed': 2}\n")
    assert git_state(package)["diff_sha256"] != edited["diff_sha256"]   # a different edit, a different hash

    git(package, "commit", "-qam", "second")
    assert git_state(package)["commit"] != clean["commit"]


def test_staged_changes_count_as_uncommitted(package):
    git(package, "init", "-q")
    git(package, "add", "-A")
    git(package, "commit", "-qm", "first")
    (package / "tasks.py").write_text("def train(ctx):\n    return {'staged': 1}\n")
    unstaged = git_state(package)
    git(package, "add", "tasks.py")
    staged = git_state(package)
    assert staged["dirty"] is True and staged["diff_sha256"] == unstaged["diff_sha256"] != digest(b"")


def test_without_git_installed_there_is_no_git_state(package, monkeypatch):
    git(package, "init", "-q")
    monkeypatch.setenv("PATH", "")
    assert git_state(package) is None
