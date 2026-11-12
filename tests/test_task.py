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


def test_numpy_values_get_a_hint():
    numpy = pytest.importorskip("numpy")
    with pytest.raises(MetricsError, match=r"convert NumPy values with float\(\) or int\(\)"):
        check_metrics({"accuracy": numpy.float64(0.9)})
