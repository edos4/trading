"""Strict JSON values for browser payloads and exported trade records."""

import math
from typing import Any

import numpy as np


def json_safe(value: Any) -> Any:
    """Represent undefined metrics, including infinite profit factor, as null.

    NumPy scalars and arrays are converted to their JSON-native equivalents
    first. numpy.bool_ / numpy.int64 are not subclasses of bool / int, so they
    slip past both ``json.dumps`` and FastAPI's ``jsonable_encoder``: a single
    leaked scalar turns the whole payload into a 500 or stops a ledger write.
    """
    if isinstance(value, np.generic):
        value = value.item()
    elif isinstance(value, np.ndarray):
        value = value.tolist()
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if isinstance(value, dict):
        return {key: json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [json_safe(item) for item in value]
    return value
