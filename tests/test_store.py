import json
import os
from datetime import UTC, datetime
from pathlib import Path

import pytest

from exprun.store import RunFolder, RunLock, RunNotFound, RunStore, write_json_atomic

AT = datetime(2026, 11, 11, 23, 59, 59, tzinfo=UTC)
FP = "sha256:3f2a9c" + "0" * 58


def test_run_ids_sort_by_time_and_carry_the_fingerprint(tmp_path):
    store = RunStore(tmp_path / "runs")
    folder, lock = store.create(FP, AT)
    assert folder.id == "20261111T235959Z-3f2a9c" and folder.path.is_dir() and (store.root / folder.id).is_dir()
    lock.release()


def test_two_runs_in_the_same_second_with_the_same_fingerprint_get_different_ids(tmp_path):
    store = RunStore(tmp_path)
    ids = []
    for _ in range(3):
        folder, lock = store.create(FP, AT)
        ids.append(folder.id)
        lock.release()
    assert ids == ["20261111T235959Z-3f2a9c", "20261111T235959Z-3f2a9c-2", "20261111T235959Z-3f2a9c-3"]
    assert [f.id for f in store.all()] == ids


def test_get_accepts_only_run_ids_inside_the_runs_folder(tmp_path):
    store = RunStore(tmp_path / "runs")
    folder, lock = store.create(FP, AT)
    lock.release()
    assert store.get(folder.id).path == folder.path
    (tmp_path / "secret").mkdir()
    for bad in ("../secret", "secret", "20261111T235959Z-3f2a9c/..", "", "20261111T235959Z-3F2A9C"):
        with pytest.raises(RunNotFound, match="isn't a run id"):
            store.get(bad)
    with pytest.raises(RunNotFound, match="no run 20261111T235959Z-000000"):
        store.get("20261111T235959Z-000000")


def test_listing_skips_other_files_and_a_missing_folder(tmp_path):
    store = RunStore(tmp_path / "runs")
    assert store.all() == []
    folder, lock = store.create(FP, AT)
    lock.release()
    (store.root / "notes.txt").write_text("x")
    (store.root / "not-a-run").mkdir()
    assert [f.id for f in store.all()] == [folder.id]


def test_atomic_writes_leave_the_old_file_when_they_fail(tmp_path, monkeypatch):
    path = tmp_path / "manifest.json"
    write_json_atomic(path, {"status": "running"})

    def crash(src, dst):
        raise OSError("disk full")

    monkeypatch.setattr(os, "replace", crash)
    with pytest.raises(OSError, match="disk full"):
        write_json_atomic(path, {"status": "succeeded"})
    assert json.loads(path.read_text()) == {"status": "running"}
    assert [p.name for p in tmp_path.iterdir()] == ["manifest.json"]       # no temporary file left


def test_atomic_writes_refuse_nan(tmp_path):
    with pytest.raises(ValueError):
        write_json_atomic(tmp_path / "m.json", {"x": float("nan")})
    assert list(tmp_path.iterdir()) == []


def folder_with(tmp_path, manifest, locked):
    store = RunStore(tmp_path)
    folder, lock = store.create(FP, AT)
    if manifest is not None:
        folder.write(manifest)
    if not locked:
        lock.release()
    return folder, lock


@pytest.mark.parametrize("manifest, locked, expected", [
    ({"status": "running"}, True, "running"),
    ({"status": "running"}, False, "interrupted"),
    ({"status": "succeeded"}, False, "succeeded"),
    ({"status": "failed"}, True, "failed"),
    (None, True, "starting"),
    (None, False, "incomplete"),
])
def test_the_effective_status_trusts_running_only_while_the_lock_is_held(tmp_path, manifest, locked, expected):
    folder, lock = folder_with(tmp_path, manifest, locked)
    assert folder.effective_status() == expected
    lock.release()


def test_a_folder_without_a_lock_file_is_not_running(tmp_path):
    (tmp_path / "20261111T235959Z-3f2a9c").mkdir()
    folder = RunFolder(tmp_path / "20261111T235959Z-3f2a9c")
    assert not folder.lock_is_held() and folder.effective_status() == "incomplete"
    assert folder.reconcile() is None


def test_reconcile_records_a_dead_runner_but_never_touches_a_live_one(tmp_path):
    live, live_lock = folder_with(tmp_path / "a", {"status": "running"}, locked=True)
    assert live.reconcile() == {"status": "running"} and live.read() == {"status": "running"}
    live_lock.release()
    dead, _ = folder_with(tmp_path / "b", {"status": "running", "id": "x"}, locked=False)
    m = dead.reconcile()
    assert m["status"] == "interrupted" and m["error"]["type"] == "RunnerDied" and dead.read() == m
    done, _ = folder_with(tmp_path / "c", {"status": "succeeded"}, locked=False)
    assert done.reconcile() == {"status": "succeeded"}


def test_checking_a_lock_does_not_take_it_away_from_its_holder(tmp_path):
    folder, lock = folder_with(tmp_path, {"status": "running"}, locked=True)
    for _ in range(3):
        assert folder.lock_is_held()
    lock.release()
    assert not folder.lock_is_held()


def test_a_lock_cannot_be_taken_twice(tmp_path):
    folder, lock = folder_with(tmp_path, {"status": "running"}, locked=True)
    with pytest.raises(BlockingIOError):
        RunLock(folder.path)
    lock.release()
    lock.release()                                     # releasing twice is harmless
    RunLock(folder.path).release()


def test_reconcile_re_reads_under_the_lock_so_a_run_that_just_finished_keeps_its_status(tmp_path, monkeypatch):
    # The runner finishes between reconcile's first read ("running") and its taking the lock.
    folder, _ = folder_with(tmp_path, {"status": "succeeded"}, locked=False)
    stale = iter([{"status": "running"}])
    real_read = folder.read
    monkeypatch.setattr(folder, "read", lambda: next(stale, None) or real_read())
    assert folder.reconcile() == {"status": "succeeded"}
    assert real_read() == {"status": "succeeded"}


def test_runs_are_listed_oldest_first_whatever_order_the_filesystem_gives(tmp_path, monkeypatch):
    store = RunStore(tmp_path)
    ids = []
    for second in (1, 2, 3):
        folder, lock = store.create(FP, AT.replace(second=second))
        lock.release()
        ids.append(folder.id)
    real_iterdir = Path.iterdir
    monkeypatch.setattr(Path, "iterdir", lambda self: reversed(list(real_iterdir(self))))
    assert [f.id for f in store.all()] == ids
