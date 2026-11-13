"""The runs folder: run ids, atomic manifest writes, and the lock that says a run is in progress.

Layout:

    runs/<run-id>/
      .lock           held (flock) by the runner and the task's process while the run is in progress
      manifest.json   written atomically: a reader sees the old version or the new one, never half
      config.toml     the config's bytes exactly as given
      stdout.log, stderr.log
      outputs/        files the task writes

**Run ids** are `YYYYMMDDTHHMMSSZ-<first 6 hex digits of the fingerprint>`, with `-2`, `-3`, ...
added when two runs would share one. Creating the folder with `mkdir` is what claims an id, so
two runners can't take the same one.

**Atomic writes:** the manifest goes to a temporary file in the same folder, which is flushed to
disk and then renamed over the old one. A rename within a filesystem is atomic.

**Liveness:** a manifest that says "running" is trusted only while the run's lock is held. The OS
releases an `flock` when the last process holding it ends, however it ends (Ctrl-C, `kill -9`,
power loss). So "running" with a free lock means the run was interrupted without being able to
record it. `effective_status` reports that, and `reconcile` writes it down.
"""

from __future__ import annotations

import fcntl
import json
import os
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

MANIFEST = "manifest.json"
LOCK = ".lock"
SCHEMA = 1
RUN_ID = re.compile(r"\d{8}T\d{6}Z-[0-9a-f]{6}(?:-\d+)?")
_ID_PREFIX = re.compile(r"[0-9TZa-f-]+")
STATUSES = ("running", "succeeded", "failed", "interrupted")


class RunNotFound(LookupError):
    pass


class ManifestError(ValueError):
    """A manifest that isn't JSON, or is from a newer version of exprun."""


def now() -> datetime:
    return datetime.now(UTC)


def iso(moment: datetime) -> str:
    return moment.strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def write_json_atomic(path: Path, data: dict) -> None:
    """Write JSON so that a reader (or a crash) never sees a partial file."""
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        with open(temporary, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=1, ensure_ascii=False, allow_nan=False)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(temporary, path)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
    folder = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(folder)            # make the rename itself durable
    finally:
        os.close(folder)


@dataclass
class RunFolder:
    path: Path

    @property
    def id(self) -> str:
        return self.path.name

    @property
    def manifest_path(self) -> Path:
        return self.path / MANIFEST

    @property
    def outputs(self) -> Path:
        return self.path / "outputs"

    def read(self) -> dict | None:
        """The manifest, or None if the run is so new (or so broken) that it has none yet. A manifest
        that can't be parsed, or that a newer exprun wrote, raises ManifestError."""
        try:
            data = json.loads(self.manifest_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        except (ValueError, UnicodeDecodeError) as exc:
            raise ManifestError(f"{self.manifest_path} isn't readable JSON ({exc})") from None
        if not isinstance(data, dict):
            raise ManifestError(f"{self.manifest_path} isn't a manifest")
        if data.get("schema", SCHEMA) != SCHEMA:
            raise ManifestError(f"{self.manifest_path} has schema version {data.get('schema')!r}; this exprun "
                                f"reads version {SCHEMA}")
        return data

    def write(self, manifest: dict) -> None:
        write_json_atomic(self.manifest_path, manifest)

    def lock_is_held(self) -> bool:
        """Whether some process holds the run's lock, meaning it is still in progress."""
        try:
            fd = os.open(self.path / LOCK, os.O_RDWR)
        except FileNotFoundError:
            return False
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        finally:
            os.close(fd)                 # closing releases the lock if we took it
        return False

    def effective_status(self, manifest: dict | None = None) -> str:
        """The recorded status, except that "running" without a held lock is "interrupted", and a
        folder with no manifest is "starting" (lock held) or "incomplete"."""
        manifest = self.read() if manifest is None else manifest
        if manifest is None:
            return "starting" if self.lock_is_held() else "incomplete"
        status = manifest.get("status")
        if status == "running" and not self.lock_is_held():
            return "interrupted"
        return status

    def reconcile(self) -> dict | None:
        """Record "interrupted" for a run whose runner died while it was running. Done under the run's
        lock, so it can't race a runner that is still alive."""
        manifest = self.read()
        if manifest is None or manifest.get("status") != "running":
            return manifest
        try:
            fd = os.open(self.path / LOCK, os.O_RDWR)
        except FileNotFoundError:
            return manifest
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return manifest          # still running
            manifest = self.read()       # re-read under the lock
            if manifest is not None and manifest.get("status") == "running":
                manifest["status"] = "interrupted"
                manifest["error"] = {"type": "RunnerDied", "message": "the runner stopped without recording "
                                     "an end (killed, or the machine stopped); found " + iso(now())}
                self.write(manifest)
            return manifest
        finally:
            os.close(fd)


class RunLock:
    """The lock a runner holds for a run's lifetime. Its file descriptor is passed to the task's
    process, so the run counts as in progress until both have ended."""

    def __init__(self, folder: Path):
        self.fd = os.open(folder / LOCK, os.O_RDWR | os.O_CREAT, 0o644)
        fcntl.flock(self.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)

    def release(self) -> None:
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None


class RunStore:
    def __init__(self, root: Path | str):
        # Absolute, because the task runs in its config's folder: a relative path to its run folder
        # would point somewhere else from there.
        self.root = Path(root).absolute()

    def create(self, fingerprint: str, at: datetime) -> tuple[RunFolder, RunLock]:
        """Claim a new run id and folder, and lock it."""
        self.root.mkdir(parents=True, exist_ok=True)
        base = f"{at.strftime('%Y%m%dT%H%M%SZ')}-{fingerprint.removeprefix('sha256:')[:6]}"
        for n in range(1, 10_000):
            run_id = base if n == 1 else f"{base}-{n}"
            try:
                (self.root / run_id).mkdir()
            except FileExistsError:
                continue
            folder = RunFolder(self.root / run_id)
            return folder, RunLock(folder.path)
        raise RuntimeError(f"too many runs named {base}")

    def get(self, run_id: str) -> RunFolder:
        """A run by id. Only well-formed ids are accepted, so an id can't reach outside the runs folder."""
        if not RUN_ID.fullmatch(run_id):
            raise RunNotFound(f"{run_id!r} isn't a run id (they look like 20261111T235959Z-3f2a9c)")
        path = self.root / run_id
        if not path.is_dir():
            raise RunNotFound(f"no run {run_id} in {self.root}")
        return RunFolder(path)

    def resolve(self, reference: str) -> RunFolder:
        """A run by full id, by a prefix of exactly one id, or "latest" (the newest run)."""
        runs = self.all()
        if reference == "latest":
            if not runs:
                raise RunNotFound(f"no runs in {self.root} yet")
            return runs[-1]
        if RUN_ID.fullmatch(reference):
            return self.get(reference)
        if not _ID_PREFIX.fullmatch(reference):
            raise RunNotFound(f"{reference!r} isn't a run id (they look like 20261111T235959Z-3f2a9c)")
        matches = [run for run in runs if run.id.startswith(reference)]
        if not matches:
            raise RunNotFound(f"no run in {self.root} starts with {reference}")
        if len(matches) > 1:
            shown = ", ".join(run.id for run in matches[:5]) + (", ..." if len(matches) > 5 else "")
            raise RunNotFound(f"{reference} matches {len(matches)} runs ({shown}); give more of the id")
        return matches[0]

    def all(self) -> list[RunFolder]:
        """Every run, oldest first. An id carries its start time only to the second, so runs started
        within the same second are ordered by the microsecond start time in their manifests."""
        if not self.root.is_dir():
            return []
        runs = [RunFolder(p) for p in self.root.iterdir() if p.is_dir() and RUN_ID.fullmatch(p.name)]
        return sorted(runs, key=_start_order)


def _start_order(folder: RunFolder) -> tuple[str, str, str]:
    try:
        manifest = folder.read() or {}
    except ManifestError:
        manifest = {}
    return folder.id[:16], str(manifest.get("started_at") or ""), folder.id
