import math

import pytest
from hypothesis import given
from hypothesis import strategies as st

from exprun.config import SEED_LIMIT, ConfigError, load

BASE = """\
[experiment]
name = "digits-1"
task = "tasks:train"
seed = 7
description = "a baseline"

[inputs]
train = "data/train.csv"

[params]
model = { C = 1.0, max_iter = 200 }
solver = "lbfgs"
layers = [64, 32]
"""


@pytest.fixture
def project(tmp_path):
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "train.csv").write_text("a,b\n1,2\n")
    (tmp_path / "tasks.py").write_text("def train(ctx):\n    return {}\n")
    return tmp_path


def write(project, text, name="exp.toml"):
    path = project / name
    path.write_text(text)
    return path


def error(project, text, overrides=None):
    with pytest.raises(ConfigError) as caught:
        load(write(project, text), overrides)
    message = str(caught.value)
    assert message.startswith(str(project / "exp.toml") + ": ")
    return message


def test_a_valid_config_loads(project):
    config = load(write(project, BASE))
    assert (config.name, config.task, config.seed, config.description) == ("digits-1", "tasks:train", 7, "a baseline")
    assert (config.module, config.function) == ("tasks", "train")
    assert config.inputs == {"train": project / "data" / "train.csv"}
    assert config.input_paths == {"train": "data/train.csv"}
    assert config.params == {"model": {"C": 1.0, "max_iter": 200}, "solver": "lbfgs", "layers": [64, 32]}
    assert config.source == BASE.encode() and config.folder == project


def test_only_the_experiment_table_is_required(project):
    config = load(write(project, '[experiment]\nname = "x"\ntask = "a.b.c:run"\n'))
    assert (config.seed, config.description, config.inputs, config.params) == (None, "", {}, {})


def test_the_spec_is_task_seed_and_params(project):
    config = load(write(project, BASE))
    assert config.spec() == {"task": "tasks:train", "seed": 7, "params": config.params}
    assert config.spec(seed=99)["seed"] == 99                   # an explicit seed (a sweep's) wins


def test_a_missing_seed_must_be_supplied_for_the_spec(project):
    config = load(write(project, BASE.replace("seed = 7\n", "")))
    assert config.spec(seed=123)["seed"] == 123
    with pytest.raises(ValueError, match="no seed"):
        config.spec()


def test_a_relative_config_path_and_inputs_resolve_against_the_config_folder(project, monkeypatch):
    write(project, BASE)
    elsewhere = project / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    config = load("../exp.toml")
    assert config.path.is_absolute() and config.inputs["train"] == project / "elsewhere" / ".." / "data" / "train.csv"
    assert config.inputs["train"].resolve() == (project / "data" / "train.csv").resolve()


def test_a_byte_order_mark_is_tolerated_and_kept_in_the_source(project):
    path = project / "exp.toml"
    path.write_bytes(b"\xef\xbb\xbf" + BASE.encode())
    config = load(path)
    assert config.name == "digits-1" and config.source.startswith(b"\xef\xbb\xbf")


@pytest.mark.parametrize("text, expected", [
    ("not [toml", "invalid TOML: "),
    ('[experiment]\nname = "a"\nname = "b"\n', "invalid TOML: Cannot overwrite a value (at line 3"),
    ("[params]\nx = 1\n", "missing the [experiment] table"),
    ('experiment = "x"\n', "missing the [experiment] table"),
    (BASE + "[extra]\n", "unknown table or key 'extra'"),
    (BASE + "[param]\n", "unknown table or key 'param' (did you mean params?)"),
    (BASE.replace("seed = 7", "sed = 7"), "unknown key experiment.sed (did you mean seed?)"),
    (BASE.replace('name = "digits-1"', "name = 5"), "experiment.name must be"),
    (BASE.replace('name = "digits-1"', 'name = "has space"'), "experiment.name must be"),
    (BASE.replace('name = "digits-1"', 'name = "-dash"'), "experiment.name must be"),
    (BASE.replace('name = "digits-1"', f'name = "{"x" * 65}"'), "experiment.name must be"),
    (BASE.replace('task = "tasks:train"', 'task = "tasks.train"'), "experiment.task must be module:function"),
    (BASE.replace('task = "tasks:train"', 'task = "tasks:"'), "experiment.task must be module:function"),
    (BASE.replace('task = "tasks:train"', 'task = "1tasks:train"'), "experiment.task must be module:function"),
    (BASE.replace('task = "tasks:train"', 'task = "../x:train"'), "experiment.task must be module:function"),
    (BASE.replace("seed = 7", "seed = -1"), "experiment.seed must be a whole number from 0 to 4294967295"),
    (BASE.replace("seed = 7", f"seed = {SEED_LIMIT}"), "experiment.seed must be"),
    (BASE.replace("seed = 7", "seed = 7.0"), "experiment.seed must be"),
    (BASE.replace("seed = 7", "seed = true"), "experiment.seed must be"),
    (BASE.replace("seed = 7", 'seed = "7"'), "experiment.seed must be"),
    (BASE.replace('description = "a baseline"', "description = 3"), "experiment.description must be a string"),
    (BASE.replace('train = "data/train.csv"', 'train = "data/missing.csv"'),
     "inputs.train: data/missing.csv doesn't exist"),
    (BASE.replace('train = "data/train.csv"', "train = 3"), "inputs.train must be a path in quotes"),
    (BASE.replace('train = "data/train.csv"', 'train = ""'), "inputs.train must be a path in quotes"),
    (BASE.replace('train = "data/train.csv"', '"bad name" = "data/train.csv"'), "input name 'bad name' must be"),
    (BASE.replace("[inputs]\ntrain", "inputs = 3\n[x]\ntrain"), "unknown table or key 'x'"),
    ('inputs = 3\n[experiment]\nname = "a"\ntask = "t:f"\n', "[inputs] must be a table"),
    ('params = 3\n[experiment]\nname = "a"\ntask = "t:f"\n', "[params] must be a table"),
    (BASE + "when = 1979-05-27\n", "params.when is a TOML date or time; write it as a string"),
    (BASE + "deep = { at = [1, 07:32:00] }\n", "params.deep.at[1] is a TOML date or time"),
    (BASE + "x = nan\n", "params.x is nan; params must be finite"),
    (BASE + "x = [1.0, -inf]\n", "params.x[1] is -inf"),
])
def test_invalid_configs_are_refused_with_the_key_named(project, text, expected):
    assert expected in error(project, text)


def test_a_config_that_is_not_utf8_is_refused(project):
    path = project / "exp.toml"
    path.write_bytes(BASE.encode().replace(b"a baseline", b"caf\xe9"))
    with pytest.raises(ConfigError, match="not UTF-8 text"):
        load(path)


def test_an_unreadable_config_is_an_os_error_not_a_config_error(project):
    with pytest.raises(OSError, match="can't be read"):
        load(project / "nope.toml")


# --- overrides ----------------------------------------------------------------------------------

def test_overrides_replace_params_with_toml_values(project):
    config = load(write(project, BASE), ["model.C=10", "solver='saga'", "layers=[8]", " model.max_iter = 5 "])
    assert config.params == {"model": {"C": 10, "max_iter": 5}, "solver": "saga", "layers": [8]}
    assert isinstance(config.params["model"]["C"], int)          # 10 is an integer, 10.0 a float


def test_overrides_do_not_change_the_source_bytes(project):
    assert load(write(project, BASE), ["model.C=10"]).source == BASE.encode()


@pytest.mark.parametrize("override, expected", [
    ("model.c=10", "params has no model.c (did you mean model.C?)"),
    ("model.max_iters=1", "params has no model.max_iters (did you mean model.max_iter?)"),
    ("modle.C=1", "params has no modle (did you mean model?)"),
    ("new=1", "params has no new"),
    ("solver.x=1", "params has no solver.x"),
    ("model=1", "params.model is a table; set one of its keys"),
    ("solver=saga", "'saga' isn't a TOML value; quote strings, as in solver='\"saga\"'"),
    ("model.C=", "isn't a TOML value"),
    ("model.C", "must look like key.path=value"),
    ("=3", "must look like key.path=value"),
    ("model.C=nan", "params.model.C is nan"),
])
def test_bad_overrides_are_refused(project, override, expected):
    assert expected in error(project, BASE, [override])


def test_an_override_given_twice_is_refused(project):
    assert "--set model.C is given twice" in error(project, BASE, ["model.C=1", "model.C=2"])


# --- properties ---------------------------------------------------------------------------------

keys = st.from_regex(r"[a-z][a-z0-9_]{0,8}", fullmatch=True)
scalars = st.one_of(st.integers(-10**12, 10**12), st.booleans(), st.text(max_size=8),
                    st.floats(allow_nan=False, allow_infinity=False))
values = st.recursive(scalars, lambda inner: st.one_of(st.lists(inner, max_size=3),
                                                        st.dictionaries(keys, inner, max_size=3)), max_leaves=12)


def toml_value(value) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return repr(value)
    if isinstance(value, str):
        # TOML basic strings must escape quotes, backslashes and control characters, DEL included.
        return '"' + "".join(f"\\u{ord(c):04x}" if c in '"\\' or ord(c) < 32 or ord(c) == 0x7F else c
                             for c in value) + '"'
    if isinstance(value, list):
        return "[" + ", ".join(toml_value(v) for v in value) + "]"
    return "{" + ", ".join(f"{k} = {toml_value(v)}" for k, v in value.items()) + "}"


@given(st.dictionaries(keys, values, max_size=5))
def test_any_params_round_trip_through_toml(params):
    import tempfile
    from pathlib import Path

    text = '[experiment]\nname = "p"\ntask = "t:f"\n[params]\n' + "".join(f"{k} = {toml_value(v)}\n" for k, v in params.items())
    with tempfile.TemporaryDirectory() as folder:
        path = Path(folder) / "exp.toml"
        path.write_text(text, encoding="utf-8")
        loaded = load(path).params
    assert loaded == params
    assert all(type(a) is type(b) for a, b in zip(_leaves(loaded), _leaves(params), strict=True))


def _leaves(value):
    if isinstance(value, dict):
        for k in sorted(value):
            yield from _leaves(value[k])
    elif isinstance(value, list):
        for v in value:
            yield from _leaves(v)
    else:
        yield value


def test_negative_zero_and_large_floats_survive(project):
    config = load(write(project, BASE + "z = -0.0\nbig = 1e300\n"))
    assert math.copysign(1, config.params["z"]) == -1 and config.params["big"] == 1e300
