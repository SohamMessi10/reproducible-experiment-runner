# Progress

## Status

**Milestone 1 of 6 complete: config and hashing.** Next: milestone 2, the runner. The plan is in
[`docs/design.md`](docs/design.md).

## Milestones

### Milestone 1: config and hashing

- **`config.py`** reads a TOML experiment config strictly. A message names the file and the key,
  with a suggestion for a likely typo. It refuses:
  - an unknown table or key;
  - a value of the wrong type;
  - a seed outside 0 to 2³² − 1;
  - a missing input;
  - a TOML date or time, or `nan` or infinity, in the params;
  - a file that isn't UTF-8.

  A byte-order mark is tolerated.
- **`--set model.C=10` overrides a param with a TOML value.**
  - The key must already exist, so a typo is an error rather than a new, ignored param.
  - Strings need quotes, and the error message shows how.
  - Overriding a whole table, or the same key twice, is refused.
- **`hashing.py`:**
  - **Canonical JSON:** sorted keys, no whitespace, shortest round-trip floats, so equal values
    hash equally however they were written, while 1 and 1.0 stay distinct.
  - **Files** are hashed by streaming their bytes.
  - **Folders** are hashed from sorted (relative path, file hash) pairs, skipping `.DS_Store`.
    Symlinked folders and broken links are refused.
  - **The fingerprint** combines the config hash, input content hashes and the code version.
    Input paths, the run's name and untracked files don't change it.
- **`codeversion.py`** finds the task's module without running it. It walks packages with
  `PathFinder`, so no `__init__.py` executes. Then it:
  - checks that the function is defined, by reading the module's syntax tree;
  - hashes the module's source;
  - records the installed package's version, if it belongs to one;
  - records git state: commit, uncommitted tracked changes (staged or not) and their diff hash,
    and the number of untracked entries.
- **`exprun check CONFIG [--set KEY=VALUE] [--json]`** shows what identifies the run before
  running it. It exits 0 for a valid config, 2 for an invalid one, and 1 for an unreadable file.
- **Fixed while building:**
  - A suggestion missed `model.C` for `model.c`, because Python's fuzzy matching is
    case-sensitive. Suggestions now try a case-insensitive match first and give the full key path.
  - Hypothesis found a bug in my test helper, not in the code: TOML forbids an unescaped DEL
    character in strings.
  - I had expected git to count an untracked folder's files; it lists the folder once.

**Tests (112):**
- Every config error, with Hypothesis round trips of arbitrary params through TOML, types
  included.
- Hashes:
  - unaffected by key order (Hypothesis), changed by any value change;
  - file hashes independent of chunk size;
  - folder hashes changed by any content change, rename, addition, removal or move;
  - symlinks and special files handled as above.
- Module lookup without import; real git repositories (no commits, clean, untracked, edited,
  staged, no git installed); the fingerprint's inputs; every command output and exit code.

**Mutation-checked:** 36 deliberate bugs. 33 were caught at once and 3 survived:
- **Sorting a folder's entries was untested.** This filesystem lists entries by name whatever
  order they were created in, so my test couldn't see a missing sort, though other filesystems
  differ. A test now reverses the listing itself.
- **`git diff` (unstaged only) versus `git diff HEAD`** went unnoticed, because my test only
  checked that some diff hash existed. A staged edit must now hash like the same edit unstaged.
- **Copying params before applying overrides** was equivalent: nothing else holds the parsed
  config. I removed the copy.

### Milestone 0: setup and design

- Packaging (`pyproject.toml`): the runner itself uses the standard library only. The demo task's
  scikit-learn and NumPy are an optional `demo` extra; pytest, Hypothesis and ruff are the `dev`
  extra. Lower bounds are releases at least two weeks old on 2026-11-11.
- `docs/design.md`: the problem, scope, run folder and manifest, reproducibility rules, exit
  codes, security, testing and milestones.
- CI: lint and tests on Python 3.12, 3.13 and 3.14 (Ubuntu) and 3.13 (macOS).

## Commands verified

```
python -m pip install -e ".[dev]"
python -m ruff check src tests
python -m pytest
exprun check path/to/experiment.toml --set model.C=10   (verified on example configs in a temporary folder)
```

## Concepts to review

- What a seed does and doesn't fix; content hashes versus file names.
- Canonical serialization: why key order and float formatting must be fixed before hashing.
- Finding a module without importing it (`PathFinder`), and why checking a config shouldn't run code.
- What `git diff HEAD` covers that `git diff` doesn't; why untracked files stay out of the fingerprint.
