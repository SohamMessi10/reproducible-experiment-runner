import math

import pytest

from exprun.task import MetricsError, check_metrics


def test_valid_metrics_pass_through():
    metrics = {"accuracy": 0.9, "n": 3, "converged": True, "solver": "lbfgs", "loss": -0.0}
    assert check_metrics(metrics) == metrics and check_metrics(None) == {}


@pytest.mark.parametrize("value, message", [
    ([0.9], "the task returned list; return a dict of metrics, or nothing"),
    (0.9, "the task returned float"),
    ({1: 0.9}, "metric names must be non-empty strings"),
    ({"": 0.9}, "metric names must be non-empty strings"),
    ({"loss": math.nan}, "metric 'loss' is nan; metrics must be finite numbers"),
    ({"loss": math.inf}, "metric 'loss' is inf"),
    ({"scores": [1, 2]}, "metric 'scores' is a list; use a number, boolean or string"),
    ({"nested": {"a": 1}}, "metric 'nested' is a dict"),
    ({"none": None}, "metric 'none' is a NoneType"),
])
def test_invalid_metrics_are_refused_with_a_reason(value, message):
    with pytest.raises(MetricsError, match=message.replace("(", r"\(").replace(")", r"\)")):
        check_metrics(value)


def test_numpy_scalars_become_plain_python_values():
    numpy = pytest.importorskip("numpy")
    metrics = check_metrics({"f64": numpy.float64(0.9), "f32": numpy.float32(0.1), "i64": numpy.int64(7),
                             "flag": numpy.bool_(True), "s": numpy.str_("x")})
    assert metrics == {"f64": 0.9, "f32": float(numpy.float32(0.1)), "i64": 7, "flag": True, "s": "x"}
    assert [type(v) for v in metrics.values()] == [float, float, int, bool, str]
    assert metrics["f32"] == 0.10000000149011612          # the float32 value exactly, not rounded to 0.1


def test_numpy_arrays_and_nan_are_refused():
    numpy = pytest.importorskip("numpy")
    with pytest.raises(MetricsError, match="a NumPy array: return one number"):
        check_metrics({"scores": numpy.array([0.9, 0.8])})
    with pytest.raises(MetricsError, match="metric 'loss' is nan"):
        check_metrics({"loss": numpy.float32("nan")})
