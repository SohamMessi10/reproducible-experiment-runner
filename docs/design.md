# Reproducible Experiment Runner: design

## Problem

A data scientist runs a model, gets an accuracy of 0.947, changes a few things, and a week later
can't say which config, which version of the data, which seed or which code produced 0.947. Saying
"I set the seed" isn't enough:

- the data file may have been regenerated with the same name;
- the code may have changed since, with uncommitted edits at the time;
- a different library version or thread count can change results;
- a failed or interrupted run often leaves no record at all, or a half-written one that looks
  finished.

**Reproducible Experiment Runner** (`exprun`) runs an experiment from a config file and writes a
manifest of everything needed to reproduce and compare it:
- the config, resolved and hashed;
- a content hash of every input file;
- the seed it used, even when none was given;
- the task's code version (git commit, uncommitted changes, a source hash);
- the environment (Python, platform, installed packages, thread settings);
- status, timings, metrics and a hash of every output file.

It can then re-run a past run from its manifest and say exactly what reproduced and what didn't.

**User:** a data scientist or student running many experiments on one machine.
**Value:** results can be traced to their exact inputs, and their reproducibility is checked
rather than assumed.

## Scope

**MVP**
- A TOML config: experiment name, task, input files, parameters, seed. Parsing is strict: an
  unknown key, a wrong type or a missing input stops with a clear message.
- Hashes:
  - the config, in a canonical form, so key order and formatting don't matter but any change of
    value does;
  - every input file's content (SHA-256, streamed), with folders hashed from their sorted files;
  - a run fingerprint over config, data and code.
- `exprun run`:
  - creates a run folder and executes the task in a child process;
  - records status (running, succeeded, failed, interrupted) with writes that are never seen
    half-written;
  - captures stdout and stderr, and on failure keeps the run and the error.
- A run killed outright (power loss, `kill -9`) is recognized as interrupted, not left "running"
  forever.
- `exprun list`, `exprun show RUN` and `exprun compare RUN RUN ...`.
  - **List:** newest first, filterable. It records runs whose runner died as interrupted.
  - **Compare** opens with a one-line verdict:
    - the same fingerprint and the same metrics;
    - the same fingerprint but *different* metrics, so something outside config, data and code
      changed the result;
    - or which of the config, the inputs' content and the code differ.

    Then it shows only the values that differ (params by dotted path, seed, input hashes, code
    version, Python, platform, packages, recorded variables), and every metric with its change from
    the first run.
- `exprun rerun RUN` re-executes from the manifest, refuses if the inputs' content has changed, and
  reports what reproduced:
  - metrics: exactly, or only within a tolerance;
  - outputs: byte for byte or not;
  - which environment differences might explain any change.
- A demo task (scikit-learn on a dataset that ships with it), so everything runs offline.

**Stretch:** parameter sweeps with a bounded number of parallel workers; an HTML comparison report.

**Not in scope:** remote execution, a server, a database service, distributed workers, or
promising bit-for-bit reproduction everywhere. Platform and library behaviour can change results,
and the tool's job is to detect and explain that, not deny it.

## User flow

```
$ exprun demo data/                      # writes a demo dataset (from scikit-learn's bundled data)
$ exprun run experiments/digits.toml     # runs it; prints the run id, status, metrics
$ exprun run experiments/digits.toml --set model.C=10
$ exprun list                            # runs, newest first: id, experiment, status, key metrics
$ exprun compare 20261111-... 20261111-...  # what differs: config, data, code, environment, metrics
$ exprun rerun 20261111-...              # re-executes and reports: identical / within tolerance / differs
```

## Architecture

```
config.toml ─► config (strict parse, canonical JSON, hash)
data files  ─► hashing (streamed SHA-256; folders from sorted files)
task code   ─► code version (git commit + uncommitted changes + source hash)
environment ─► capture (Python, platform, packages, thread settings)
                     │
                     ▼
              runner ─► run folder: manifest.json (atomic writes), config, logs, outputs/
                     │     │
                     │     └─ child process runs the task (seed, params, inputs, output folder)
                     │        and writes metrics; the runner holds a lock for the run's lifetime
                     ▼
              list / show / compare (read manifests) · rerun (re-execute, then compare)
```

- **Manifests are the source of truth.** Each run folder is self-describing JSON plus files. Listing
  scans folders, which is simple and can't drift out of sync with the runs. An index could be
  rebuilt from them later if listing ever gets slow.
- **The task runs in a child process.**
  - A crashing or hanging task can't corrupt the runner's records.
  - stdout and stderr can be captured.
  - Each run starts from a fresh interpreter, so state doesn't leak between runs.
- **Liveness by lock, not by guess.**
  - The runner holds an OS file lock (`flock`) on the run, and passes it to the task's process, so
    it stays held until *both* have ended.
  - The OS releases the lock when the last holder dies, however it dies.
  - So a run marked "running" whose lock can be taken has been interrupted. Readers report it as
    such, and `reconcile` records it, taking the lock first so it can't race a live runner.
  - Two cases are told apart: a runner killed with `kill -9` while its task is still running stays
    "running" until the task ends, because the run isn't over yet.
- **Stopping.**
  - Ctrl-C reaches the whole process group. The task records its own interruption, and the runner
    waits for it.
  - SIGTERM reaches only the runner, which passes it on.
  - A time limit sends SIGTERM.
  - If the task hasn't ended within a grace period (5 s), it gets SIGTERM, then SIGKILL. A second
    Ctrl-C kills it at once.
- **Resource usage** comes from `wait4`, which reports the task process's own CPU time and peak
  memory. Peak memory is in bytes on macOS and KiB on Linux, and is converted to bytes.
- **Atomic writes.** The manifest is written to a temporary file, flushed to disk and renamed over
  the old one, so a reader sees either the old version or the new one, never half of each.

## Tech stack

- **Python 3.12–3.14.** The runner uses the standard library only: `tomllib`, `hashlib`, `json`,
  `subprocess`, `fcntl`, `importlib.metadata`.
- **Demo task:** scikit-learn and NumPy, an optional `demo` extra, so the runner never depends on
  them.
- **Tests:** pytest and Hypothesis; ruff for lint; GitHub Actions on Linux and macOS.
- **Platform:** POSIX only (macOS and Linux), because of `fcntl`. Windows is out of scope and the
  README says so.

## Data

The demo dataset is exported from data that ships inside scikit-learn (BSD-3-Clause), so it needs
no network and its origin is clear. Users point configs at their own files. The tool reads input
files only to hash them, and never modifies them.

## Run folder and manifest

```
runs/<run-id>/
  manifest.json   status, timestamps, config, hashes, seed, code, environment, metrics, outputs
  config.toml     the config exactly as given (bytes preserved)
  stdout.log, stderr.log
  outputs/        files the task writes; each one's hash is recorded at the end
  .lock           held (flock) by the runner and the task while the run is in progress
```

- **Run id:** `YYYYMMDDTHHMMSSZ-<first 6 hex of the fingerprint>`. It sorts by time and hints at
  identity; a suffix makes it unique when needed.
  - An id holds time only to the second, so runs started within one second are ordered by the
    microsecond start time in their manifests.
  - Commands accept a full id, a unique start of one, or `latest`.
- **The manifest has a schema version,** so later versions can read older runs or refuse them
  clearly.

## Reproducibility rules

- **Seed.** The config's `seed` is passed to the task. If there is none, the runner draws one,
  records it and passes it, so every run can be re-run. `PYTHONHASHSEED` is fixed to 0 in the
  child, so `str` hashing (and set iteration order) is repeatable.
  `PYTHONUNBUFFERED` is set too, so the task's output is logged as it is printed. It doesn't
  affect results.
- **Identity.** fingerprint = SHA-256 of the canonical config, input hashes and code version.
  - Two runs with the same fingerprint *should* match.
  - Different data can't pass as the same run: data is hashed by content, not by name.
- **Rerun outcomes:**
  - *identical*: metrics equal exactly, and every output byte-identical;
  - *metrics identical, outputs differ*;
  - *within tolerance*: metrics within a stated relative tolerance;
  - *different*.

  Each outcome comes with the environment differences between the two runs.
- **What is not promised:** bit-for-bit results across machines, library versions or thread
  counts. Those are recorded so a difference can be explained.

## Errors and exit codes

| Code | Meaning |
|---|---|
| 0 | Done (for `run` and `rerun`: the run succeeded) |
| 1 | The run failed or was interrupted, or a file couldn't be read or written |
| 2 | Bad usage or an invalid config, refused before anything runs |
| 3 | `rerun` ran, but the result didn't reproduce |

## Security

- **Config files are data:** TOML is parsed, never executed.
- **The task is code the user names** (`module:function`) and runs as the user, like `python
  script.py`. The tool doesn't fetch or run anything from elsewhere.
- **Environment capture records variable names relevant to reproducibility** (thread settings,
  `PYTHONHASHSEED`), never the whole environment, which may hold secrets.
- **Run folders are created under the given runs folder.** A run id given to `show` or `rerun` must
  name a run there; path tricks such as `../` are refused.

## Testing

- **Hashes:** stable across key order, formatting and runs; any value change changes them;
  Hypothesis round trips; folder hashes independent of listing order.
- **Atomic writes and states:** every transition, and a crash at each step leaves a readable
  manifest.
- **Interruption:** a real child killed with `SIGKILL` is recognized as interrupted; Ctrl-C (SIGINT)
  is recorded as interrupted.
- **Failure:** a task that raises keeps its run with status failed, exit code and error.
- **Reproduction:** a deterministic task reproduces as identical; a deliberately nondeterministic
  one is reported as different, with a reason; changed data is refused.
- **CLI:** every command, exit code and error message.
- **Mutation testing,** as in earlier projects.

## Observability

- Each run keeps its logs and timings: wall time, and the child's CPU time and peak memory where
  the platform reports them.
- `exprun show` prints them.

## GitHub strategy

`main` holds setup, this design and CI. Each milestone is a branch with focused commits and a pull
request merged after CI passes.

## Milestones

0. Setup, this design, CI.
1. Config and hashing: strict TOML parsing, canonical form and hash, data hashing, fingerprint.
2. The runner: run folders, child process, atomic manifest, states, logs, failures, interruption.
3. Reading runs: `list`, `show`, `compare`.
4. Reproduction: `rerun`, its outcomes, the demo task, and measured sources of nondeterminism.
5. Sweeps with bounded parallel workers (stretch).
6. README, results, final audit.

## Demo and portfolio evidence

- **Recreate a result from its manifest.**
- **Show the difference between "I set a seed" and reproducibility:** a run that reproduces, one
  whose data changed under the same file name (refused), and one that differs, with the recorded
  reason.
- **Measure, don't claim,** which settings change results on the demo task.
