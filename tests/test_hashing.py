import hashlib
import json
import os
import random

import pytest
from hypothesis import given
from hypothesis import strategies as st

from exprun.hashing import (
    InputError,
    canonical_json,
    digest,
    fingerprint,
    hash_file,
    hash_input,
    hash_value,
)

SHA = "sha256:" + "0" * 64


def test_canonical_json_sorts_keys_and_drops_whitespace():
    assert canonical_json({"b": 1, "a": [1.5, "é"]}) == '{"a":[1.5,"é"],"b":1}'.encode()


def test_canonical_json_refuses_nan():
    with pytest.raises(ValueError):
        canonical_json({"x": float("nan")})


def test_types_stay_distinct():
    assert len({hash_value(1), hash_value(1.0), hash_value(True), hash_value("1"), hash_value([1])}) == 5
    assert hash_value(0.0) != hash_value(-0.0)


json_values = st.recursive(
    st.one_of(st.none(), st.booleans(), st.integers(), st.text(), st.floats(allow_nan=False, allow_infinity=False)),
    lambda inner: st.one_of(st.lists(inner, max_size=4), st.dictionaries(st.text(max_size=5), inner, max_size=4)),
    max_leaves=15)


@given(st.dictionaries(st.text(max_size=5), json_values, max_size=6), st.randoms())
def test_key_order_never_changes_the_hash(value, rng):
    def shuffled(v):
        if isinstance(v, dict):
            items = list(v.items())
            rng.shuffle(items)
            return {k: shuffled(x) for k, x in items}
        if isinstance(v, list):
            return [shuffled(x) for x in v]
        return v

    assert hash_value(shuffled(value)) == hash_value(value)
    assert json.loads(canonical_json(value)) == value


@given(st.dictionaries(st.text(max_size=5), st.integers(), min_size=1, max_size=6), st.data())
def test_changing_any_value_changes_the_hash(value, data):
    key = data.draw(st.sampled_from(sorted(value)))
    changed = dict(value, **{key: value[key] + data.draw(st.integers(1, 10**6))})
    assert hash_value(changed) != hash_value(value)


def test_a_file_hash_is_the_sha256_of_its_bytes_whatever_the_chunk_size(tmp_path):
    data = random.Random(1).randbytes(3 * 64 + 1)
    path = tmp_path / "f.bin"
    path.write_bytes(data)
    expected = "sha256:" + hashlib.sha256(data).hexdigest()
    for chunk in (1, 64, 65, 1 << 20):
        assert hash_file(path, chunk) == (expected, len(data))
    assert digest(data) == expected


def test_an_empty_file_hashes(tmp_path):
    (tmp_path / "e").write_bytes(b"")
    assert hash_input(tmp_path / "e") == {"kind": "file", "sha256": digest(b""), "bytes": 0, "files": 1}


def tree(root, files):
    for rel, content in files.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_bytes(content)
    return root


FILES = {"a.csv": b"1,2\n", "sub/b.txt": b"hello", "sub/deeper/c": b"", "z.bin": b"\x00\x01"}


def test_a_folder_hash_depends_on_names_and_content_not_creation_order(tmp_path):
    first = hash_input(tree(tmp_path / "one", FILES))
    second = hash_input(tree(tmp_path / "two", dict(reversed(list(FILES.items())))))
    assert first == second
    assert first["kind"] == "folder" and first["files"] == 4 and first["bytes"] == 11


def test_the_folder_hash_does_not_depend_on_the_order_the_filesystem_lists_entries(tmp_path, monkeypatch):
    root = tree(tmp_path / "d", {**FILES, "sub/a2": b"2", "b0": b"0"})
    expected = hash_input(root)
    real_walk = os.walk

    def reversed_walk(top, **kwargs):     # filesystems list entries in different orders (by name, by creation)
        for folder, dirs, files in real_walk(top, **kwargs):
            dirs.reverse()
            yield folder, dirs, list(reversed(files))

    monkeypatch.setattr("exprun.hashing.os.walk", reversed_walk)
    assert hash_input(root) == expected


@pytest.mark.parametrize("change", ["content", "rename", "add", "remove", "move"])
def test_any_change_to_a_folder_changes_its_hash(tmp_path, change):
    root = tree(tmp_path / "d", FILES)
    before = hash_input(root)["sha256"]
    if change == "content":
        (root / "sub/b.txt").write_bytes(b"hellO")
    elif change == "rename":
        (root / "a.csv").rename(root / "A.csv")
    elif change == "add":
        (root / "new").write_bytes(b"")
    elif change == "remove":
        (root / "z.bin").unlink()
    else:
        (root / "sub/b.txt").rename(root / "b.txt")
    assert hash_input(root)["sha256"] != before


def test_an_empty_file_is_not_the_same_as_no_file(tmp_path):
    with_empty = hash_input(tree(tmp_path / "a", {"x": b"1", "empty": b""}))
    without = hash_input(tree(tmp_path / "b", {"x": b"1"}))
    assert with_empty["sha256"] != without["sha256"]


def test_empty_folders_and_ds_store_files_do_not_count(tmp_path):
    plain = hash_input(tree(tmp_path / "a", FILES))
    noisy = tree(tmp_path / "b", {**FILES, ".DS_Store": b"finder", "sub/.DS_Store": b"x"})
    (noisy / "empty-folder").mkdir()
    assert hash_input(noisy) == plain


def test_an_empty_folder_hashes(tmp_path):
    (tmp_path / "e").mkdir()
    assert hash_input(tmp_path / "e") == {"kind": "folder", "sha256": digest(b""), "bytes": 0, "files": 0}


def test_symlinked_files_are_hashed_by_content(tmp_path):
    real = tree(tmp_path / "real", FILES)
    linked = tmp_path / "linked"
    linked.mkdir()
    for rel in FILES:
        (linked / rel).parent.mkdir(parents=True, exist_ok=True)
        os.symlink(real / rel, linked / rel)
    assert hash_input(linked) == hash_input(real)
    os.symlink(real / "a.csv", tmp_path / "single")
    assert hash_input(tmp_path / "single")["sha256"] == hash_input(real / "a.csv")["sha256"]


def test_symlinked_folders_and_broken_links_are_refused(tmp_path):
    real = tree(tmp_path / "real", FILES)
    os.symlink(real, tmp_path / "alias")
    with pytest.raises(InputError, match="symlink to a folder; give the folder itself"):
        hash_input(tmp_path / "alias")
    inside = tree(tmp_path / "inside", {"x": b"1"})
    os.symlink(real / "sub", inside / "sub")
    with pytest.raises(InputError, match="symlink to a folder inside an input"):
        hash_input(inside)
    broken = tree(tmp_path / "broken", {"x": b"1"})
    os.symlink(tmp_path / "nowhere", broken / "dangling")
    with pytest.raises(InputError, match="isn't a regular file"):
        hash_input(broken)


def test_a_special_file_is_refused(tmp_path):
    os.mkfifo(tmp_path / "pipe")
    with pytest.raises(InputError, match="isn't a regular file or folder"):
        hash_input(tmp_path / "pipe")


def test_the_fingerprint_covers_config_inputs_and_code_but_not_paths_or_untracked_files():
    inputs = {"train": {"path": "data/a.csv", "sha256": SHA}}
    code = {"source_sha256": SHA, "file": "/x/tasks.py",
            "git": {"commit": "abc", "dirty": False, "diff_sha256": None, "untracked": 0, "root": "/x"}}
    base = fingerprint(SHA, inputs, code)
    other = "sha256:" + "1" * 64
    assert fingerprint(other, inputs, code) != base
    assert fingerprint(SHA, {"train": {"path": "data/a.csv", "sha256": other}}, code) != base
    assert fingerprint(SHA, {"test": {"path": "data/a.csv", "sha256": SHA}}, code) != base     # renamed input
    assert fingerprint(SHA, inputs, {**code, "source_sha256": other}) != base
    for git in ({"commit": "abd"}, {"dirty": True}, {"diff_sha256": SHA}):
        assert fingerprint(SHA, inputs, {**code, "git": {**code["git"], **git}}) != base
    assert fingerprint(SHA, {"train": {"path": "moved/a.csv", "sha256": SHA}}, code) == base
    assert fingerprint(SHA, inputs, {**code, "file": "/y/tasks.py", "git": {**code["git"], "untracked": 9,
                                                                           "root": "/y"}}) == base
    assert fingerprint(SHA, inputs, {**code, "git": None}) != base
