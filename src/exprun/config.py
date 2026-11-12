"""Experiment configs: strict TOML parsing, overrides, and the canonical form that is hashed.

A config looks like this:

    [experiment]
    name = "digits-logreg"           # required: letters, digits, '.', '_' or '-'
    task = "tasks:train"             # required: module:function
    seed = 42                        # optional: 0 to 2**32 - 1; drawn and recorded when absent
    description = "baseline"         # optional

    [inputs]                         # optional: name = path, relative to the config's folder
    train = "data/digits.csv"

    [params]                         # optional: anything TOML can say, except dates and nan/inf
    model = { C = 1.0, max_iter = 200 }

Parsing is strict. An unknown table or key, a value of the wrong type, or an input that doesn't
exist stops with a message naming the file and the key, before anything runs: a typo that is
silently ignored can make two different experiments look identical.

What identifies the computation is the task, the seed and the params (`Config.spec`). The name
and description are labels, and input *paths* don't matter, only their content (hashed
separately), so renaming a run or moving a file doesn't change its identity.
"""

from __future__ import annotations

import difflib
import math
import re
import tomllib
from dataclasses import dataclass, field
from datetime import date, datetime, time
from pathlib import Path

SEED_LIMIT = 2**32                      # seeds are 0 .. 2**32 - 1, which every common RNG accepts
_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")
_INPUT_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_-]{0,63}")
_IDENTIFIER = r"[A-Za-z_][A-Za-z0-9_]*"
_TASK = re.compile(rf"({_IDENTIFIER}(?:\.{_IDENTIFIER})*):({_IDENTIFIER})")

_TABLES = {"experiment", "inputs", "params"}
_EXPERIMENT_KEYS = {"name", "task", "seed", "description"}


class ConfigError(ValueError):
    """The config is invalid: refused before anything runs (exit code 2)."""


@dataclass(frozen=True)
class Config:
    path: Path                         # the config file, absolute
    source: bytes                      # its bytes exactly as read, kept with each run
    name: str
    task: str                          # "module:function"
    seed: int | None
    description: str
    inputs: dict[str, Path] = field(default_factory=dict)    # name -> absolute path
    input_paths: dict[str, str] = field(default_factory=dict)  # name -> path as written
    params: dict = field(default_factory=dict)

    @property
    def folder(self) -> Path:
        return self.path.parent

    @property
    def module(self) -> str:
        return self.task.split(":")[0]

    @property
    def function(self) -> str:
        return self.task.split(":")[1]

    def spec(self, seed: int | None = None) -> dict:
        """What determines the computation: task, seed and params. `seed` fills in a missing one."""
        chosen = self.seed if self.seed is not None else seed
        if chosen is None:
            raise ValueError("the config sets no seed: pass the one drawn for this run")
        return {"task": self.task, "seed": chosen, "params": self.params}


def load(path: Path | str, overrides: list[str] | None = None) -> Config:
    """Read and validate a config file, applying `--set key.path=value` overrides to its params."""
    path = Path(path).absolute()
    try:
        source = path.read_bytes()
    except OSError as exc:
        raise OSError(f"{path} can't be read ({exc.strerror})") from None
    try:
        text = source.decode("utf-8-sig")   # tolerate a byte-order mark, which tomllib refuses
    except UnicodeDecodeError:
        raise ConfigError(f"{path}: not UTF-8 text") from None
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"{path}: invalid TOML: {exc}") from None
    return _build(path, source, data, overrides or [])


def _build(path: Path, source: bytes, data: dict, overrides: list[str]) -> Config:
    def fail(message: str) -> ConfigError:
        return ConfigError(f"{path}: {message}")

    for key in data:
        if key not in _TABLES:
            raise fail(f"unknown table or key {key!r}{_suggest(key, _TABLES)}; expected [experiment], "
                       "[inputs] and [params]")
    experiment = data.get("experiment")
    if not isinstance(experiment, dict):
        raise fail("missing the [experiment] table (with name and task)")
    for key in experiment:
        if key not in _EXPERIMENT_KEYS:
            raise fail(f"unknown key experiment.{key}{_suggest(key, _EXPERIMENT_KEYS)}")

    name = experiment.get("name")
    if not isinstance(name, str) or not _NAME.fullmatch(name):
        raise fail("experiment.name must be 1 to 64 letters, digits, '.', '_' or '-', starting with a letter "
                   f"or digit (got {name!r})")
    task = experiment.get("task")
    if not isinstance(task, str) or not _TASK.fullmatch(task):
        raise fail(f"experiment.task must be module:function, such as \"tasks:train\" (got {task!r})")
    seed = experiment.get("seed")
    if seed is not None and (isinstance(seed, bool) or not isinstance(seed, int) or not 0 <= seed < SEED_LIMIT):
        raise fail(f"experiment.seed must be a whole number from 0 to {SEED_LIMIT - 1} (got {seed!r})")
    description = experiment.get("description", "")
    if not isinstance(description, str):
        raise fail(f"experiment.description must be a string (got {description!r})")

    inputs: dict[str, Path] = {}
    input_paths: dict[str, str] = {}
    raw_inputs = data.get("inputs", {})
    if not isinstance(raw_inputs, dict):
        raise fail("[inputs] must be a table of name = \"path\"")
    for key, value in raw_inputs.items():
        if not _INPUT_NAME.fullmatch(key):
            raise fail(f"input name {key!r} must be letters, digits, '_' or '-', starting with a letter or '_'")
        if not isinstance(value, str) or not value:
            raise fail(f"inputs.{key} must be a path in quotes (got {value!r})")
        resolved = (path.parent / value).absolute()
        if not resolved.exists():
            raise fail(f"inputs.{key}: {value} doesn't exist (looked for {resolved})")
        inputs[key], input_paths[key] = resolved, value

    params = data.get("params", {})
    if not isinstance(params, dict):
        raise fail("[params] must be a table")
    params = _apply_overrides(params, overrides, fail)
    _check_values(params, "params", fail)
    return Config(path, source, name, task, seed, description, inputs, input_paths, params)


def _check_values(value, where: str, fail) -> None:
    """Params must be representable exactly in JSON: no dates and times, no nan or infinity."""
    if isinstance(value, dict):
        for key, item in value.items():
            _check_values(item, f"{where}.{key}", fail)
    elif isinstance(value, list):
        for i, item in enumerate(value):
            _check_values(item, f"{where}[{i}]", fail)
    elif isinstance(value, datetime | date | time):
        raise fail(f"{where} is a TOML date or time; write it as a string instead")
    elif isinstance(value, float) and not math.isfinite(value):
        raise fail(f"{where} is {value!r}; params must be finite numbers")


def _apply_overrides(params: dict, overrides: list[str], fail) -> dict:
    """`--set model.C=10`: replace an existing param by a TOML value. Keys must already exist, so a
    typo is an error rather than a new, ignored param. The parsed config belongs to nobody else, so
    it is changed in place."""
    seen: set[str] = set()
    for item in overrides:
        key, sep, raw = item.partition("=")
        key = key.strip()
        if not sep or not key:
            raise fail(f"--set {item!r} must look like key.path=value, such as model.C=10")
        if key in seen:
            raise fail(f"--set {key} is given twice")
        seen.add(key)
        try:
            value = tomllib.loads(f"v = {raw.strip()}")["v"]
        except tomllib.TOMLDecodeError:
            raise fail(f"--set {key}: {raw.strip()!r} isn't a TOML value; quote strings, as in "
                       f"{key}='\"{raw.strip()}\"'") from None
        parts = key.split(".")
        table = params
        for depth, part in enumerate(parts):
            here = ".".join(parts[: depth + 1])
            if not isinstance(table, dict) or part not in table:
                options = table.keys() if isinstance(table, dict) else []
                prefix = ".".join(parts[:depth])
                raise fail(f"--set {key}: params has no {here}{_suggest(part, options, prefix)}")
            if depth == len(parts) - 1:
                if isinstance(table[part], dict):
                    raise fail(f"--set {key}: params.{here} is a table; set one of its keys instead")
                table[part] = value
            else:
                table = table[part]
    return params


def _suggest(word: str, options, prefix: str = "") -> str:
    """' (did you mean ...?)' for the closest option: one differing only in case first, then by spelling."""
    options = [str(o) for o in options]
    same = [o for o in options if o.lower() == word.lower()]
    close = same or difflib.get_close_matches(word, options, n=1)
    if not close:
        return ""
    return f" (did you mean {prefix + '.' if prefix else ''}{close[0]}?)"
