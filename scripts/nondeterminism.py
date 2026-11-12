"""Measure what does and doesn't change the demo's results. The numbers in docs/reproducibility.md.

    python scripts/nondeterminism.py                       # the measurements
    python scripts/nondeterminism.py --other-python PATH   # also rerun under another environment

Everything goes through the real `exprun` command, in a fresh temporary folder:

1. **Plain reruns** of the logistic regression and the random forest (n_jobs=1): are they identical?
2. **A parallel random forest** (n_jobs=2), run repeatedly: how many distinct outputs?
3. **BLAS threads:** the logistic run, rerun with OPENBLAS_NUM_THREADS and OMP_NUM_THREADS set to 2.
4. **The hash seed:** a task whose result depends on set iteration order, run under exprun (which
   fixes PYTHONHASHSEED) and directly with Python's default random hash seed.
5. **An unseeded task:** one that ignores ctx.seed.
6. **Changed data** under the same file name: refused.
7. **Optional, another environment:** the original runs rerun with another interpreter that has
   exprun installed, typically one with a different scikit-learn version.

For output files, it reports how many probabilities differ and the largest difference in units in
the last place (ULPs), so "different" is quantified.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

HASH_TASK = '''
def order(ctx):
    # The order of a set of strings depends on the hash seed; float addition depends on the order.
    words = {f"feature-{i}" for i in range(200)}
    # Fixed values, of very different sizes, so rounding depends on the order they are added in.
    weights = {w: (1 + int(w.split("-")[1]) * 7919 % 997) / 997 * (1e16 if w.endswith("7") else 1.0) for w in words}
    total = 0.0
    for w in words:                                                       # added in set order
        total += weights[w]
    return {"total": total}
'''

CARELESS_TASK = '''
import random

def careless(ctx):
    return {"score": round(random.random(), 6)}           # ignores ctx.seed
'''


def exprun(python: str, folder: Path, *args: str, env: dict | None = None) -> subprocess.CompletedProcess:
    return subprocess.run([python, "-m", "exprun.cli", *args, "--runs", str(folder / "runs")], cwd=folder,
                          capture_output=True, text=True, env=env)


def latest(folder: Path) -> dict:
    runs = sorted((folder / "runs").iterdir())
    manifests = [json.loads((run / "manifest.json").read_text()) for run in runs]
    return max(manifests, key=lambda m: m["started_at"])


def outcome(result: subprocess.CompletedProcess) -> str:
    for line in result.stdout.splitlines():
        if line.startswith(("reproduced: ", "NOT reproduced: ")):
            return line.split(" (")[0]
    return (result.stderr.strip().splitlines() or ["no output"])[-1]


def probability_difference(folder: Path, a: dict, b: dict) -> tuple[int, int, float]:
    """(differing values, total values, largest difference in ULPs) between two runs' probabilities."""
    import numpy as np

    pa = np.loadtxt(folder / "runs" / a["id"] / "outputs" / "probabilities.csv", delimiter=",")
    pb = np.loadtxt(folder / "runs" / b["id"] / "outputs" / "probabilities.csv", delimiter=",")
    diff = np.abs(pa - pb)
    ulps = diff / np.spacing(np.maximum(np.abs(pa), np.abs(pb)))
    return int((diff > 0).sum()), diff.size, float(ulps.max())


def section(title: str) -> None:
    print(f"\n## {title}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--repeats", type=int, default=5)
    parser.add_argument("--other-python", help="an interpreter with exprun installed, to rerun the originals")
    args = parser.parse_args()
    python = sys.executable
    import numpy
    import sklearn

    print(f"Python {sys.version.split()[0]}, scikit-learn {sklearn.__version__}, NumPy {numpy.__version__}, "
          f"{os.cpu_count()} CPUs, {sys.platform}")
    with tempfile.TemporaryDirectory() as tmp:
        folder = Path(tmp)
        subprocess.run([python, "-m", "exprun.cli", "demo", str(folder)], check=True, capture_output=True)
        originals = {}
        for name in ("logistic", "forest"):
            exprun(python, folder, "run", f"{name}.toml", "--quiet")
            originals[name] = latest(folder)

        section(f"1. Plain reruns ({args.repeats} each)")
        for name, original in originals.items():
            outcomes = [outcome(exprun(python, folder, "rerun", original["id"], "--quiet")) for _ in range(args.repeats)]
            print(f"{name}: " + ", ".join(f"{outcomes.count(o)} x {o}" for o in dict.fromkeys(outcomes)))

        section(f"2. Random forest with n_jobs=2 (original and {args.repeats + 3} reruns)")
        exprun(python, folder, "run", "forest.toml", "--quiet", "--set", "forest.n_jobs=2")
        parallel = latest(folder)
        runs = [parallel]
        outcomes = []
        for _ in range(args.repeats + 3):
            outcomes.append(outcome(exprun(python, folder, "rerun", parallel["id"], "--quiet")))
            runs.append(latest(folder))
        hashes = {r["outputs"]["probabilities.csv"]["sha256"] for r in runs}
        metric_sets = {json.dumps(r["metrics"], sort_keys=True) for r in runs}
        worst = max(probability_difference(folder, parallel, r) for r in runs[1:])
        print(", ".join(f"{outcomes.count(o)} x {o}" for o in dict.fromkeys(outcomes)))
        print(f"{len(hashes)} distinct probability files and {len(metric_sets)} distinct metric sets in {len(runs)} "
              f"runs; versus the original, at most {worst[0]} of {worst[1]} probabilities differ, by at most "
              f"{worst[2]:.0f} ULP")
        single = originals["forest"]
        n, total, ulp = probability_difference(folder, single, parallel)
        print(f"n_jobs=1 against n_jobs=2: {n} of {total} probabilities differ, by at most {ulp:.0f} ULP; metrics "
              f"{'identical' if single['metrics'] == parallel['metrics'] else 'DIFFERENT'}")

        section("3. Logistic regression rerun with 2 BLAS threads")
        env = {**os.environ, "OPENBLAS_NUM_THREADS": "2", "OMP_NUM_THREADS": "2"}
        recorded = originals["logistic"]["environment"]["variables"]
        print(f"recorded with: {recorded or 'no thread variables set'}")
        for _ in range(3):
            print(outcome(exprun(python, folder, "rerun", originals["logistic"]["id"], "--quiet", env=env)))

        section("4. The hash seed")
        (folder / "hashing_task.py").write_text(HASH_TASK)
        (folder / "hash.toml").write_text('[experiment]\nname = "hash-order"\ntask = "hashing_task:order"\nseed = 1\n')
        exprun(python, folder, "run", "hash.toml", "--quiet")
        hashed = latest(folder)
        outcomes = [outcome(exprun(python, folder, "rerun", hashed["id"], "--quiet")) for _ in range(args.repeats)]
        print("under exprun (PYTHONHASHSEED=0): " + ", ".join(f"{outcomes.count(o)} x {o}" for o in dict.fromkeys(outcomes)))
        direct_env = {k: v for k, v in os.environ.items() if k != "PYTHONHASHSEED"}
        totals = set()
        for _ in range(args.repeats * 2):
            result = subprocess.run([python, "-c", "import hashing_task; print(repr(hashing_task.order(None)['total']))"],
                                    cwd=folder, capture_output=True, text=True, env=direct_env, check=True)
            totals.add(result.stdout.strip())
        print(f"run directly with a random hash seed: {len(totals)} distinct results in {args.repeats * 2} runs")

        section("5. A task that ignores its seed")
        (folder / "careless_task.py").write_text(CARELESS_TASK)
        (folder / "careless.toml").write_text('[experiment]\nname = "careless"\ntask = "careless_task:careless"\n'
                                              'seed = 1\n')
        exprun(python, folder, "run", "careless.toml", "--quiet")
        careless = latest(folder)
        outcomes = [outcome(exprun(python, folder, "rerun", careless["id"], "--quiet")) for _ in range(3)]
        print(", ".join(f"{outcomes.count(o)} x {o}" for o in dict.fromkeys(outcomes)))

        section("6. Changed data under the same file name")
        data = folder / "data" / "digits.csv"
        original_bytes = data.read_bytes()
        data.write_bytes(original_bytes.replace(b",0\n", b",1\n", 1))
        print(outcome(exprun(python, folder, "rerun", originals["logistic"]["id"], "--quiet")))
        data.write_bytes(original_bytes)

        if args.other_python:
            version = subprocess.run([args.other_python, "-c", "import sklearn, numpy; print(sklearn.__version__, "
                                      "numpy.__version__)"], capture_output=True, text=True, check=True).stdout.split()
            section(f"7. Rerun under another environment (scikit-learn {version[0]}, NumPy {version[1]})")
            for name, original in originals.items():
                result = exprun(args.other_python, folder, "rerun", original["id"], "--quiet")
                rerun = latest(folder)
                n, total, ulp = probability_difference(folder, original, rerun)
                metrics = "identical" if rerun["metrics"] == original["metrics"] else "DIFFERENT"
                print(f"{name}: {outcome(result)}; {n} of {total} probabilities differ (at most {ulp:.0f} ULP); "
                      f"metrics {metrics}")


if __name__ == "__main__":
    main()
