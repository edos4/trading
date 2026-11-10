"""Browser payloads and exports must survive numpy-typed calculations."""
from __future__ import annotations

import json

import numpy as np

from utils.json_values import json_safe


def test_json_safe_converts_numpy_scalars_and_arrays():
    """numpy.bool_/int64 are not bool/int subclasses and break both encoders."""
    clean = json_safe({
        "fit": {"passed": np.bool_(True), "count": np.int64(3), "ratio": np.float64(2.5)},
        "series": np.array([1.5, 2.5]),
        "rows": [np.bool_(False)],
    })
    assert clean["fit"]["passed"] is True
    assert clean["fit"]["count"] == 3 and type(clean["fit"]["count"]) is int
    assert clean["fit"]["ratio"] == 2.5 and type(clean["fit"]["ratio"]) is float
    assert clean["series"] == [1.5, 2.5]
    assert clean["rows"] == [False]
    # Same encoder contract the endpoints rely on.
    json.dumps(clean, allow_nan=False)


def test_json_safe_nulls_undefined_metrics_including_numpy_floats():
    payload = {"profit_factor": float("inf"), "numpy_nan": np.float64("nan")}
    assert json_safe(payload) == {"profit_factor": None, "numpy_nan": None}
    assert payload["profit_factor"] == float("inf")


def test_json_safe_leaves_ordinary_values_alone():
    payload = {"a": [1, 2, {"b": "c"}], "d": True, "e": None, "f": 1.25}
    assert json_safe(payload) == payload
