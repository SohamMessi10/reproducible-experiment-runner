# Progress

## Status

**Milestone 4 of 6 complete: reproduction.** Next: milestone 5, parameter sweeps with bounded
parallel workers. The plan is in
[`docs/design.md`](docs/design.md).

## Milestones

### Milestone 4: reproduction

- **`exprun rerun RUN [--tolerance REL] [--allow-code-change]`** re-runs from the manifest: the saved
  config bytes, the recorded overrides and seed (even a drawn one). It refuses, with exit code 2:
  - a run that didn't succeed;
  - changed or missing data;
  - changed code (the task's source, the git commit, or uncommitted edits anywhere in the
    repository), unless `--allow-code-change`.

  It reports one of these outcomes:
  - identical;
  - metrics identical, outputs differ;
  - within tolerance (metrics only, compared as numbers);
  - different;
  - failed.

  It lists the metric changes, differing output files and other differences (code, Python,
  packages, recorded variables). When nothing recorded differs, it says the task itself isn't
  deterministic. It exits 0 if reproduced and 3 if not, and the rerun's manifest records
  `rerun_of` and the judgement, which `exprun show` displays.
- **`exprun demo FOLDER`** writes handwritten digits from scikit-learn (the UCI dataset, CC BY 4.0,
  with its citation) and two configs, logistic regression and a random forest. The task
  `exprun.demo:train` splits with the run's seed, reports accuracy, macro F1, log loss and
  convergence, and writes every probability at 17 significant digits, so the byte comparison
  sees last-bit changes. The demo needs the optional `demo` extra, which CI now installs.
- **NumPy scalar metrics** (float64, float32, int64, bool_) are converted to plain values with
  `.item()`, which is exact. Arrays are refused with a hint.
- **Measured** (`scripts/nondeterminism.py`, written up in `docs/reproducibility.md`):
  - plain reruns are identical;
  - a random forest on 2 cores never repeats its last bits (9 runs, 9 different probability files,
    at most 2 ULP, metrics identical), while on 1 core it repeats exactly;
  - scikit-learn 1.8.0 versus 1.9.1 changes logistic regression's probabilities (1,164 of 4,500,
    up to 60 ULP) but no metric;
  - Python's default random hash seed gives 4 distinct results in 10 runs of an order-dependent
    task, against 5 identical ones under exprun;
  - two BLAS threads changed nothing here.
- **Fixed while building:**
  - **Stale bytecode** (the `fix:` commit). An edit to the task that kept its size, within the same
    second as the previous compile, ran the old code while the manifest recorded the new source's
    hash. Python's bytecode cache trusts the modification time in seconds plus the size. A flaky
    test exposed it; a deterministic test now forces an identical timestamp. The task module is
    compiled in checked-hash mode (PEP 552) before import.
  - **NumPy scalars were inconsistent:** `numpy.float64` (a `float` subclass) was accepted while
    `float32` and `int64` were refused. Installing NumPy un-skipped the test that showed it.
  - **A rerun's report first sliced the printed `compare` page,** which would break with any layout
    change. It now shares the comparison's section function.
  - **One of my tests could have failed about once in a thousand runs** (a random metric near 0
    against a 0.999 tolerance). The random values are now bounded.

**Tests (262):**
- Every outcome and boundary of the judgement, and every refusal, with real runs: changed data,
  changed code, a new commit, other uncommitted edits, a failed original, a vanished task.
- Saved-config, override and seed reuse; `rerun` end to end, including a task that ignores its
  seed, a failed rerun, and environment differences.
- The demo end to end (export, both models, reruns, full precision, convergence), and its
  message when scikit-learn is missing.

**Mutation-checked:** 29 deliberate bugs. 23 were caught at once and 6 survived:
- 5 were test gaps, now closed: the git-commit and other-uncommitted-edit checks, the tolerance
  boundary, the overrides recorded in a rerun, and the demo's full precision and convergence
  metric.
- 1 was a branch that couldn't be reached, since loading the config already refuses missing
  inputs. I removed it.

### Milestone 3: reading runs

- **`exprun list [--experiment NAME] [--status S] [--limit N] [--json]`** shows runs newest first,
  with each run's status, start time, duration and first metrics.
  - It records runs whose runner died as interrupted.
  - An unreadable manifest is listed as "unreadable" instead of stopping the listing.
  - With no runs, it says how to start one.
- **`exprun show RUN [--json]`** shows the whole run: the status (noting when it differs from what
  was recorded), experiment, timing, config and overrides, task and code version, seed and whether
  it was drawn, fingerprint, params, inputs, metrics, outputs, resources, environment, and the
  error with the end of its traceback.
- **`exprun compare RUN RUN ... [--all] [--json]`** opens with a verdict:
  - the same fingerprint and the same metrics ("the two runs reproduce");
  - the same fingerprint but DIFFERENT metrics;
  - or which of the config, the inputs' content and the code differ.

  Then it shows only differing values, side by side under labels A, B, C...:
  - params by dotted path, printed as JSON so 1, 1.0 and "1" are distinct;
  - seed, input content hashes, code version;
  - Python, platform, package versions and recorded variables.

  Every metric is listed with its change from run A. Booleans and strings get no change figure.
- **Runs are named** by full id, a unique start of one, or `latest`. An ambiguous start lists the
  matches. Anything that isn't id-shaped is refused, so a reference can't reach outside the runs
  folder.
- **Manifests are read defensively.** Invalid JSON, a non-object, or a newer schema version raise a
  clear error, so a run from a later exprun is refused rather than misread.
- **Fixed while building:** runs started within the same second (easy with fast tasks) were ordered
  by fingerprint, since ids hold time only to the second, so `latest` could pick the wrong run.
  Runs are now ordered by the microsecond start time in their manifests. The verdict's grammar
  was also fixed ("the config differs").

**Tests (222, one skipped until NumPy is installed):**
- The report functions on hand-built manifests: every verdict, hidden and `--all` rows, type-aware
  equality, metric changes, and both page layouts.
- Store lookup by prefix and `latest`, same-second ordering, and broken manifests.
- Every command with real runs: empty, filtered, limited and JSON listings; reconciling a dead
  run; a corrupt, a newer-schema and a missing manifest; three-way comparisons.

**Mutation-checked:** 27 deliberate bugs. 25 were caught at once. The 2 survivors were test gaps,
now closed: a metric that is `1` in one run and `true` in another, and the quote-free printing of
hashes next to quoted string params.

### Milestone 2: the runner

- **`exprun run CONFIG [--set KEY=VALUE] [--runs DIR] [--timeout S] [--quiet]`** runs the task and
  records the run in `runs/<YYYYMMDDTHHMMSSZ>-<fingerprint prefix>/`. The folder holds:
  - `manifest.json`;
  - the config's exact bytes;
  - `stdout.log` and `stderr.log`;
  - `outputs/`.

  It exits 0 if the run succeeded and 1 if it failed or was interrupted.
- **The manifest records:** status, spec (task, seed and where it came from, params), config hash,
  inputs (content hashes), code version, fingerprint, environment, start and end times, duration,
  exit code or signal, metrics or the error (with a traceback trimmed to the task's own frames),
  output file hashes, and the task's CPU time and peak memory.
- **The task runs in a child process,** in the config's folder, with `PYTHONHASHSEED=0` and
  unbuffered output. It returns a flat dict of finite numbers, booleans or strings; anything else
  fails the run with a reason, including a hint for NumPy values. A task that raises, calls
  `sys.exit`, ends without reporting, or is killed is recorded as failed, with its cause.
- **Atomic manifest writes:** temporary file, fsync, rename, then fsync of the folder. A test
  makes the rename fail and checks that the old manifest survives and no temporary file is left.
- **Liveness by lock.** The runner holds an `flock` and passes it to the task's process, so a run
  counts as in progress until both have ended. "running" with a free lock means the runner died,
  and `reconcile` records that under the lock.
- **Stopping:**
  - Ctrl-C reaches the task, which records its interruption.
  - SIGTERM to the runner alone is passed on to the task.
  - A time limit sends SIGTERM.
  - A task that ignores these gets SIGTERM and then SIGKILL after 5-second grace periods; a second
    Ctrl-C kills it at once.
- **Environment capture:** Python, platform, CPU count, every installed package (with names
  normalized), and only a fixed list of variables that can change results (thread counts,
  `PYTHONHASHSEED`). Nothing else from the environment is recorded, since it may hold secrets.
- **Fixed while building:**
  - **The task's stdout arrived only when it ended,** because Python buffers output to a pipe.
    `PYTHONUNBUFFERED` is now set for the task.
  - **Reaping the task with `os.wait4`** (for its own resource usage) left `subprocess.Popen`
    believing it still ran. The test suite's ResourceWarning-as-error setting caught it, and the
    exit code is now handed back to `Popen`.
  - **Tracebacks began inside exprun's own code;** they now start at the task.
- **Verified before relying on them:** an inherited `flock` stays held until the child ends;
  `wait4` reports that one child's usage; `ru_maxrss` is in KiB on Linux and bytes on macOS.

**Tests (181, one skipped until NumPy is installed in milestone 4):**
- Success, outputs in subfolders, logs, live echo and `--quiet`.
- The working folder, `PYTHONHASHSEED`, the manifest reading "running" during the run, drawn seeds.
- Six kinds of task failure, an import error, time limits, and an invalid config creating no run.
- A failure of the runner itself, run ids in the same second, atomic writes, and every effective
  status.
- **Real signals:** Ctrl-C to the process group, SIGTERM to the runner alone, a double Ctrl-C, a
  task ignoring SIGTERM, and `kill -9` of the runner while its task still runs. Run 10 times in a
  row without a failure.

**Mutation-checked:** 34 deliberate bugs. 28 were caught at once, and the 6 survivors were all
test gaps, now closed:
- **A race in `reconcile`:** without re-reading under the lock, a run that finished just before
  could be overwritten as interrupted. A test now simulates that stale first read.
- **This machine sets `PYTHONUNBUFFERED=1` globally,** so the live-log test couldn't fail here. The
  test now removes it.
- **SIGTERM forwarding had no time bound,** and the grace stage made up for its absence. Now it
  must take under 3 s.
- **The config folder on the import path** mattered only with `PYTHONSAFEPATH=1`, which is now
  tested.
- **Listing order** depended on the filesystem, and **package-name normalization** was unchecked.

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
exprun run path/to/experiment.toml --set model.C=10 --timeout 60
exprun list --status succeeded
exprun show latest
exprun compare 20261112T0229 latest
exprun demo demo && cd demo && exprun run logistic.toml && exprun rerun latest
python scripts/nondeterminism.py --other-python /path/to/env-with-scikit-learn-1.8/bin/python
```

## Concepts to review

- What a seed does and doesn't fix; content hashes versus file names.
- Canonical serialization: why key order and float formatting must be fixed before hashing.
- Finding a module without importing it (`PathFinder`), and why checking a config shouldn't run code.
- What `git diff HEAD` covers that `git diff` doesn't; why untracked files stay out of the fingerprint.
- Atomic writes (temp file, fsync, rename, fsync the folder) and what each step protects against.
- `flock`: why a lock the OS releases on death detects a killed process, and why the child shares it.
- Process groups and signals: why Ctrl-C reaches the task but SIGTERM to the runner doesn't.
- `wait4` and per-child resource usage; buffered output on pipes.
- Why "same fingerprint, different metrics" is the interesting case: something unrecorded changed
  the result.
- Floating-point addition isn't associative, so parallel reductions vary in the last bits; ULPs.
- Python's hash randomization, and bytecode cache validation (timestamp versus hash, PEP 552).
