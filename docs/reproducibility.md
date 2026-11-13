# What changes a result: measured

"I set the seed" is a common answer to "can you reproduce this?". This page measures how far that
answer gets on the demo, a scikit-learn classifier of handwritten digits. Each number comes from
[`scripts/nondeterminism.py`](../scripts/nondeterminism.py), which drives the real `exprun` command
in a fresh folder. The script was run twice, before and after the code was committed. Counts that
depend on thread scheduling are given as the range over both runs; every outcome was the same both
times.

**Setup:** Linux, 2 CPUs, Python 3.13, scikit-learn 1.9.1, NumPy 2.5.4. The demo writes every
predicted probability with 17 significant digits, so a change in the last bit of any of the 4,500
numbers (450 test images × 10 classes) makes its output file differ. Differences are counted in
**ULPs**, units in the last place: 1 ULP is the smallest possible change to a floating-point number.

## Results

| What changed | Reruns | Outcome | Size of the difference |
|---|---|---|---|
| Nothing (logistic regression) | 5 | identical, 5 of 5 | none |
| Nothing (random forest, `n_jobs=1`) | 5 | identical, 5 of 5 | none |
| Nothing, but the forest runs on 2 cores (`n_jobs=2`) | 8 (twice) | **metrics identical, outputs differ**, 8 of 8 both times | each time, 9 runs gave 9 different probability files; at most 6 to 7 of 4,500 values differ, by at most 1 to 2 ULP |
| BLAS threads 1 → 2 (logistic regression) | 3 | identical, 3 of 3 | none |
| A task whose result depends on set order, under exprun | 5 | identical, 5 of 5 | none |
| The same task run directly with Python's default random hash seed | 10 (twice) | — | 4 and 5 distinct results in 10 runs |
| A task that ignores `ctx.seed` | 3 | **different**, 3 of 3 | its metric changes every run |
| The data file, same name, one value changed | — | refused before running | the content hash changed |
| scikit-learn 1.9.1 → 1.8.0 (random forest) | 1 | identical | none |
| scikit-learn 1.9.1 → 1.8.0 (logistic regression) | 1 | **metrics identical, outputs differ** | 1,164 of 4,500 values differ, by at most 60 ULP |

## What it shows

- **A seed is necessary, not sufficient.** Every row in bold had the same seed, data and config.
- **Parallelism breaks bit-for-bit reproducibility even with a fixed seed.** With two worker
  threads, the forest adds its trees' probabilities in whatever order the threads finish, and
  floating-point addition isn't associative, so the last bits vary from run to run. On one core,
  the order is fixed and the output repeats exactly.
- **It rarely changes a metric.** In every row except the unseeded task, accuracy, F1 and log loss
  were identical: differences of 1 to 60 ULP didn't move a single prediction. That is why `exprun
  rerun` reports "metrics identical, outputs differ" as its own outcome, and why `--tolerance` exists.
- **A library upgrade can change results without changing any code.** The logistic regression's
  optimizer took a slightly different numerical path in scikit-learn 1.8.0. The rerun report
  names the package whose version differs.
- **Python randomizes `str` hashing per process by default,** so anything that iterates a set or
  dict of strings in hash order can vary between runs. exprun fixes `PYTHONHASHSEED=0` for the
  task, and records it.
- **Changed data is caught by content, not name:** the rerun is refused rather than silently run
  on different data.
- **Not every setting matters on this machine:** two BLAS threads made no difference to logistic
  regression on a problem this small. That isn't a promise about other machines or larger
  problems. The thread settings are recorded so a difference can be traced.

## Sweeps in parallel

Running several tasks at once is a different kind of parallelism from the forest's threads: each
run is its own process with its own seed, and nothing is added up across processes. A 12-run
sweep of the demo (`--grid logistic.C=0.01,0.1,1,10 --grid seed=1,2,3`) on the same 2-CPU machine,
each setting measured twice:

| Workers | Wall time | Each run's duration (mean) |
|---|---|---|
| 1 | 15.0 s and 15.4 s | 1.09 s |
| 2 | 9.5 s and 9.8 s | 1.09 s |
| 3 | 8.9 s and 10.1 s | 1.58 s |

- **Two workers took about 1.6 times less wall time,** not 2: each run also starts a Python
  process and imports scikit-learn, and the sweep waits for its slowest run.
- **A third worker gained nothing on 2 CPUs.** Each run just took longer, as three processes
  shared two cores.
- **Every output file was byte-identical** between the 1-worker and 2-worker sweeps, at all 12
  points: running runs side by side didn't change any of them.

## Reproducing these numbers

```bash
pip install -e ".[dev,demo]"
python scripts/nondeterminism.py
```

For the last two rows, make a second environment with another scikit-learn and pass its interpreter:

```bash
python -m venv /tmp/sk18 && /tmp/sk18/bin/pip install "scikit-learn==1.8.0" -e .
python scripts/nondeterminism.py --other-python /tmp/sk18/bin/python
```

Counts that depend on thread scheduling, such as how many probabilities differ with `n_jobs=2`,
vary from run to run and machine to machine. The pattern (identical metrics, last-bit output
differences, every rerun different) is what repeats.
