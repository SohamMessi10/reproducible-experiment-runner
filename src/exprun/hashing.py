"""Content hashes: of configs (in a canonical form), of input files and folders, and of a run.

Every hash is SHA-256, written "sha256:<64 hex digits>".

- **Canonical JSON** makes equal values hash equally however they were written. Keys are sorted,
  there is no whitespace, text is UTF-8, and floats are in Python's shortest round-trip form.
  Types stay distinct: 1 and 1.0 hash differently, because a task may treat them differently.
- **A file** is hashed by streaming its bytes, so its size doesn't matter.
- **A folder** is hashed from the sorted list of (relative path, file hash) pairs, so it doesn't
  depend on the order the filesystem lists entries in. Renaming, adding or changing any file
  changes it.
  - `.DS_Store` files (macOS Finder metadata) are skipped.
  - Symlinked files are hashed by their content. A symlinked folder or a broken link is refused:
    following it would make the hash depend on somewhere else.
- **The fingerprint** of a run combines the config hash, each input's content hash and the task's
  code version. Two runs with the same fingerprint are meant to compute the same thing.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

CHUNK = 1 << 20
IGNORED_NAMES = frozenset({".DS_Store"})


class InputError(ValueError):
    """An input can't be hashed as given (a symlinked folder, a broken link, a special file)."""


def canonical_json(value) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode()


def digest(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def hash_value(value) -> str:
    """The hash of a JSON-representable value, independent of how it was written."""
    return digest(canonical_json(value))


def hash_file(path: Path, chunk: int = CHUNK) -> tuple[str, int]:
    """(hash, size in bytes) of a file's content, read in chunks."""
    h, size = hashlib.sha256(), 0
    with open(path, "rb") as f:
        while block := f.read(chunk):
            h.update(block)
            size += len(block)
    return "sha256:" + h.hexdigest(), size


def hash_input(path: Path, chunk: int = CHUNK) -> dict:
    """A record of an input's content: kind, hash, total bytes and number of files."""
    if path.is_dir():
        if path.is_symlink():
            raise InputError(f"{path} is a symlink to a folder; give the folder itself")
        return _hash_tree(path, chunk)
    if not path.is_file():
        raise InputError(f"{path} isn't a regular file or folder")
    sha, size = hash_file(path, chunk)
    return {"kind": "file", "sha256": sha, "bytes": size, "files": 1}


def _hash_tree(root: Path, chunk: int) -> dict:
    entries: list[tuple[str, str]] = []
    total = 0
    for folder, dirs, files in os.walk(root, followlinks=False):
        base = Path(folder)
        for name in dirs:
            if (base / name).is_symlink():
                raise InputError(f"{base / name} is a symlink to a folder inside an input; it isn't followed")
        for name in files:
            if name in IGNORED_NAMES:
                continue
            item = base / name
            if not item.is_file():   # a broken link, a socket, a device
                raise InputError(f"{item} isn't a regular file (a broken link?)")
            sha, size = hash_file(item, chunk)
            entries.append((item.relative_to(root).as_posix(), sha))
            total += size
    entries.sort()
    listing = "".join(f"{rel}\0{sha}\n" for rel, sha in entries).encode()
    return {"kind": "folder", "sha256": digest(listing), "bytes": total, "files": len(entries)}


def fingerprint(config_hash: str, inputs: dict[str, dict], code: dict) -> str:
    """The run's identity: its computation (config), the content of its inputs, and its code."""
    git = code.get("git") or {}
    return hash_value({
        "config": config_hash,
        "inputs": {name: record["sha256"] for name, record in inputs.items()},
        "code": {"source_sha256": code.get("source_sha256"), "commit": git.get("commit"),
                 "dirty": git.get("dirty"), "diff_sha256": git.get("diff_sha256")},
    })
