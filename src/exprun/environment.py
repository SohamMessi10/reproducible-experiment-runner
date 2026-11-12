"""What a run's environment was: the things that can change a result without changing the code.

- **Python:** version and implementation.
- **Platform:** OS, release, CPU architecture, CPU count.
- **Every installed distribution and its version,** from `importlib.metadata`, which is what
  `pip list` shows.
- **The settings of a fixed list of environment variables** that change numerical results or
  their order: thread counts for the common math libraries, and `PYTHONHASHSEED`.

Only those variable names are read. The rest of the environment can hold credentials, so it is
never recorded. Nor is the host name or user name.

`summary_hash` hashes everything above, so two runs' environments can be compared at a glance.
"""

from __future__ import annotations

import os
import platform
from importlib import metadata

from exprun.hashing import hash_value

# Variables that can change results: thread counts change the order of floating-point sums.
RECORDED_VARIABLES = (
    "PYTHONHASHSEED",
    "OMP_NUM_THREADS",
    "OPENBLAS_NUM_THREADS",
    "MKL_NUM_THREADS",
    "VECLIB_MAXIMUM_THREADS",
    "NUMEXPR_NUM_THREADS",
    "CUDA_VISIBLE_DEVICES",
)


def installed_packages() -> dict[str, str]:
    """Every installed distribution, by normalized name, with its version."""
    packages: dict[str, str] = {}
    for dist in metadata.distributions():
        name = dist.metadata["Name"]
        if name:
            packages.setdefault(name.lower().replace("_", "-"), dist.version)
    return dict(sorted(packages.items()))


def capture(child_environment: dict[str, str] | None = None) -> dict:
    """The environment a task runs in. `child_environment` is the child's variables, when they differ
    from this process's (the runner fixes PYTHONHASHSEED for the child)."""
    env = os.environ if child_environment is None else child_environment
    record = {
        "python": {"version": platform.python_version(), "implementation": platform.python_implementation()},
        "platform": {"system": platform.system(), "release": platform.release(), "machine": platform.machine(),
                     "cpu_count": os.cpu_count()},
        "variables": {name: env[name] for name in RECORDED_VARIABLES if name in env},
        "packages": installed_packages(),
    }
    record["summary_hash"] = hash_value({k: v for k, v in record.items() if k != "summary_hash"})
    return record

