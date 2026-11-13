"""What a task receives, and what it may return.

A task is a plain function in your code, named in the config as `module:function`:

    def train(ctx):
        data = ctx.inputs["train"].read_text()
        rng = random.Random(ctx.seed)
        ...
        (ctx.output_dir / "model.json").write_text(...)
        return {"accuracy": 0.947, "n_train": 1200}

It runs in its own process, with the config's folder as the working directory and on the import
path. It returns its metrics: a flat dictionary from names to numbers, booleans or strings. Numbers
must be finite, so a metric is always comparable. NumPy scalars (an accuracy from scikit-learn is
usually one) are converted to the plain Python value with `.item()`, which is exact. Returning
nothing means no metrics. Files it writes to `ctx.output_dir` are kept with the run and hashed.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class Context:
    run_id: str
    seed: int
    params: dict
    inputs: dict[str, Path] = field(default_factory=dict)
    output_dir: Path = Path("outputs")


class MetricsError(TypeError):
    """A task returned something that isn't a flat dictionary of finite numbers, booleans or strings."""


def check_metrics(value) -> dict:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise MetricsError(f"the task returned {type(value).__name__}; return a dict of metrics, or nothing")
    metrics = {}
    for key, item in value.items():
        if not isinstance(key, str) or not key:
            raise MetricsError(f"metric names must be non-empty strings (got {key!r})")
        item = _plain(item)
        if isinstance(item, bool | str):
            metrics[key] = item
            continue
        if isinstance(item, int | float):
            if isinstance(item, float) and not math.isfinite(item):
                raise MetricsError(f"metric {key!r} is {item!r}; metrics must be finite numbers")
            metrics[key] = item
            continue
        type_name = type(item).__name__
        hint = " (a NumPy array: return one number, such as its mean)" if type(item).__module__ == "numpy" else ""
        raise MetricsError(f"metric {key!r} is a {type_name}; use a number, boolean or string{hint}")
    return metrics


def _plain(item):
    """A NumPy scalar (float64, float32, int64, bool_ ...) as the plain Python value; anything else as is.
    Exactly the built-in types are kept, so a float subclass like numpy.float64 is converted too."""
    if type(item) in (bool, int, float, str):
        return item
    item_method = getattr(item, "item", None)
    if type(item).__module__ == "numpy" and callable(item_method) and getattr(item, "ndim", None) == 0:
        return item_method()
    return item
